"""Server collection must acknowledge only durable mail, and preserve replay on failures."""
import base64
import datetime as dt
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import email_worker
from mail_intake import MailSpool

RAW=b'From: jobalerts-noreply@linkedin.com\r\nSubject: test\r\n\r\nfixture'
RECENT=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT12:00:00Z')
ITEM=dict(receipt_id=hashlib.sha256(RAW).hexdigest(), received_at=RECENT,
          envelope_from='forwarder@example.test',raw_base64=base64.b64encode(RAW).decode())

class CollectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.spool=MailSpool(Path(self.tmp.name)/'mail.db')
 def test_ack_is_after_durable_storage_and_lost_ack_replays_once(self):
  calls=[]
  def api(url,token,path,body=None):
   calls.append(path)
   if path=='/v1/messages':return dict(messages=[ITEM],pending=1,total_received=1,last_received_at=ITEM['received_at'])
   self.assertEqual(self.spool.info()['count'],1)
   raise RuntimeError('lost response')
  with patch.object(email_worker,'request',side_effect=api):
   with self.assertRaises(email_worker.CollectionError):email_worker.collect('https://example.test','token',self.spool)
   with self.assertRaises(email_worker.CollectionError):email_worker.collect('https://example.test','token',self.spool)
  self.assertEqual(self.spool.info()['count'],1)
 def test_hash_mismatch_does_not_acknowledge(self):
  with patch.object(email_worker,'request',return_value=dict(messages=[ITEM|{'receipt_id':'0'*64}],pending=1)) as req:
   with self.assertRaises(ValueError):email_worker.collect('https://example.test','token',self.spool)
   self.assertEqual(req.call_count,1)
  self.assertEqual(self.spool.info()['count'],0)
 def test_empty_buffer_is_not_evidence_of_received_mail(self):
  with patch.object(email_worker,'request',return_value=dict(messages=[],pending=0,total_received=0,last_received_at=None)):
   out=email_worker.collect('https://example.test','token',self.spool)
  self.assertEqual(out['received'],0);self.assertIsNone(out['last_received_at'])
 def test_bad_origin_is_rejected_before_credentials_leave(self):
  for url in ['http://example.test','https://u:p@example.test','https://example.test/elsewhere']:
   with self.subTest(url=url), self.assertRaises(ValueError):email_worker.request(url,'secret','/v1/messages')

 def buffered_api(self, count, bad_first=False):
  pending=[]
  for sequence in range(1,count+1):
   raw=RAW+str(sequence).encode()
   pending.append(ITEM|dict(sequence=sequence,receipt_id=hashlib.sha256(raw).hexdigest(),raw_base64=base64.b64encode(raw).decode()))
  if bad_first:pending[0]['receipt_id']='0'*64
  def api(url,token,path,body=None):
   if path.startswith('/v1/messages'):
    after=int(path.split('after=')[1]) if '?after=' in path else 0
    return dict(messages=[m for m in pending if m['sequence']>after][:10],pending=len(pending),total_received=count,last_received_at=ITEM['received_at'])
   ids=set(body['receipts'])
   pending[:]=[m for m in pending if m['receipt_id'] not in ids]
   return {'acknowledged':len(ids)}
  return pending,api

 def test_bad_first_message_cannot_starve_valid_messages_on_later_pages(self):
  pending,api=self.buffered_api(12,bad_first=True)
  with patch.object(email_worker,'request',side_effect=api):
   with self.assertRaises(ValueError) as caught:email_worker.collect('https://example.test','token',self.spool)
  self.assertEqual(self.spool.info()['count'],11)
  self.assertEqual([m['sequence'] for m in pending],[1])
  self.assertEqual(getattr(caught.exception,'collection',{}).get('received'),11)
  self.assertEqual(getattr(caught.exception,'collection',{}).get('failed'),1)

 def test_acknowledgment_must_confirm_each_retained_receipt(self):
  for response in ({'acknowledged':0},{'acknowledged':True},{},None):
   with self.subTest(response=response):
    def api(url,token,path,body=None):
     return dict(messages=[ITEM],pending=1) if path=='/v1/messages' else response
    with patch.object(email_worker,'request',side_effect=api),self.assertRaises(ValueError):
     email_worker.collect('https://example.test','token',self.spool)
  self.assertEqual(self.spool.info()['count'],1)

 def test_explicit_limit_bounds_actual_messages_not_just_pages(self):
  pending,api=self.buffered_api(10)
  with patch.object(email_worker,'request',side_effect=api):
   result=email_worker.collect('https://example.test','token',self.spool,limit=1)
  self.assertEqual(self.spool.info()['count'],1)
  self.assertEqual(len(pending),9)
  self.assertEqual(result['pending'],9)

 def test_old_buffer_without_sequences_still_collects_valid_mail(self):
  def api(url,token,path,body=None):
   if path=='/v1/messages':return dict(messages=[ITEM],pending=1)
   return {'acknowledged':1}
  with patch.object(email_worker,'request',side_effect=api):
   result=email_worker.collect('https://example.test','token',self.spool)
  self.assertEqual(result['received'],1)
  self.assertEqual(result['pending'],0)

 def test_unavailable_second_page_keeps_first_pages_success_counts(self):
  pending,api=self.buffered_api(11)
  def failed_api(url,token,path,body=None):
   if '?after=' in path or len(pending)==1:raise OSError('private upstream details')
   return api(url,token,path,body)
  with patch.object(email_worker,'request',side_effect=failed_api),self.assertRaises(email_worker.CollectionError) as caught:
   email_worker.collect('https://example.test','token',self.spool)
  self.assertEqual(self.spool.info()['count'],10)
  self.assertEqual(getattr(caught.exception,'collection',{}).get('received'),10)
  self.assertNotIn('private upstream details',str(caught.exception))

 def test_restart_resumes_beyond_a_capped_failed_prefix_then_returns_to_retry_it(self):
  pending,api=self.buffered_api(3,bad_first=True)
  with patch.object(email_worker,'request',side_effect=api):
   with self.assertRaises(ValueError) as caught:
    email_worker.collect('https://example.test','token',self.spool,limit=1)
   cursor=getattr(caught.exception,'collection',{}).get('resume_after')
   self.assertEqual(cursor,1)
   result=email_worker.collect('https://example.test','token',self.spool,limit=2,after=cursor)
   self.assertEqual(result['received'],2)
   self.assertEqual(result['pending'],1)
   end=email_worker.collect('https://example.test','token',self.spool,after=result['resume_after'])
   self.assertEqual(end['resume_after'],0)
  self.assertEqual([m['sequence'] for m in pending],[1])

class RunCase(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.database=self.root/'work-ops.db'
  import db
  conn=db.connect(self.database);db.migrate(conn);conn.close()


class RunTests(RunCase):
 def test_interrupted_poll_carries_source_success_into_next_empty_poll(self):
  from copy import deepcopy
  import db,json
  successful={'status':'ok','sources':{'alert:jackandjill':{'state':'ok','messages':1}},'import':{'new_observed':1}}
  empty={'status':'ok','sources':{'alert:jackandjill':{'state':'empty','messages':0}},'import_skipped':'no new items'}
  with patch.object(email_worker,'collect',return_value={'pending':0}),patch.object(email_worker.ingest,'run',return_value=deepcopy(successful)):
   first=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  with patch.object(email_worker,'collect',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):
   email_worker.run(self.database,self.root,'https://example.test','x'*40)
  conn=db.connect(self.database);self.addCleanup(conn.close)
  interrupted=json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email:%' ORDER BY run_id DESC LIMIT 1").fetchone()[0])
  self.assertEqual(interrupted.get('sources',{}).get('alert:jackandjill',{}).get('last_alert_processed'),first['finished_at'])
  with patch.object(email_worker,'collect',return_value={'pending':0}),patch.object(email_worker.ingest,'run',return_value=empty):
   final=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(final['sources']['alert:jackandjill']['last_alert_processed'],first['finished_at'])

 def test_collection_failure_keeps_successful_processing_but_failed_import_does_not(self):
  for imported,expected in [({'new_observed':1},True),({'error':'Failed to import'},False)]:
   result={'status':'ok' if expected else 'failed','sources':{'alert:jackandjill':{'state':'ok','messages':1}},'import':imported}
   with self.subTest(imported=imported),patch.object(email_worker,'collect',side_effect=email_worker.CollectionError({'pending':1,'received':1})),patch.object(email_worker.ingest,'run',return_value=result):
    receipt=email_worker.run(self.database,self.root,'https://example.test','x'*40)
   self.assertEqual(receipt['status'],'failed')
   if expected:successful_at=receipt['finished_at']
   self.assertEqual(receipt['sources']['alert:jackandjill']['last_alert_processed'],successful_at)

 def test_jack_only_and_mixed_failures_are_not_hidden_by_linkedin(self):
  for sources,status,counts in [
   ({'alert:jackandjill':{'state':'ok','messages':1,'jobs':5,'eligible':4}},'ok',(1,5,4)),
   ({'alert:linkedin':{'state':'ok','messages':1,'jobs':3,'eligible':2},
     'alert:jackandjill':{'state':'error','messages':0,'jobs':0,'eligible':0,'quarantined':1}},'partial',(1,3,2))]:
   with self.subTest(sources=sources), patch.object(email_worker,'collect',return_value=dict(pending=0,received=1)), patch.object(email_worker.ingest,'run',return_value={'status':'ok','sources':sources}):
    result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
   self.assertEqual(result['status'],status)
   self.assertEqual(tuple(result['processing'][k] for k in ('messages','jobs','eligible')),counts)
   self.assertEqual({scope:{k:v for k,v in row.items() if k!='last_alert_processed'} for scope,row in result['sources'].items()},sources)
   self.assertTrue(result['sources']['alert:jackandjill']['last_alert_processed'])
 def test_import_failure_is_not_a_success_and_has_a_durable_receipt(self):
  import db,json
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=1)), patch.object(email_worker.ingest,'run',side_effect=RuntimeError('fixture')):
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['status'],'failed');self.assertEqual(result['phase'],'processing')
  conn=db.connect(self.database)
  self.addCleanup(conn.close)
  saved=json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email:%'").fetchone()[0])
  self.assertEqual(saved['status'],'failed');self.assertIn('finished_at',saved)
 def test_disabled_scanner_cannot_report_success(self):
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=0)), patch.object(email_worker.ingest,'run',return_value=dict(status='ok',sources={},import_report={})):
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['status'],'partial')
 def test_success_requires_collection_and_scanning_and_import(self):
  memory={'cgroup':{'state':'unknown'},'process_peak_rss_bytes':1}
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=1)), patch.object(email_worker.ingest,'run',return_value={'status':'ok','sources':{'alert:linkedin':{'state':'ok'}},'import':{'created':2},'memory':memory}) as run:
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['status'],'ok');self.assertEqual(result['import']['created'],2)
  self.assertEqual(result['memory'],memory)
  self.assertEqual(run.call_args.kwargs['run_prefix'],'email-import')

 def test_logo_measurements_survive_the_existing_email_receipt(self):
  import db,json
  counts=dict(cards=6,logo_cards=2,logo_expired=1,logo_expiring_soon=0,logo_expiry_unknown=1)
  processing=dict(state='ok',**counts)
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=2)), patch.object(email_worker.ingest,'run',return_value={'status':'ok','sources':{'alert:linkedin':processing}}):
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['processing'],processing)
  conn=db.connect(self.database);self.addCleanup(conn.close)
  saved=json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email:%'").fetchone()[0])
  self.assertEqual(saved['processing'],processing)

class EndToEndTests(RunCase):
 def test_attended_quarantine_retry_runs_the_complete_pipeline_and_preserves_receipt(self):
  import json,scan_alerts
  from test_jackandjill_alerts import digest,radar_card,ONE
  spool=MailSpool(self.root/'email/mail.db')
  spool.receive(digest(radar_card()),envelope_from='forwarder@example.test',
                envelope_to='alerts@example.com',received_at=RECENT)
  with patch.dict(scan_alerts.PARSERS,jackandjill=lambda html: (_ for _ in ()).throw(ValueError('old parser'))):
   result=scan_alerts.run(email_worker.ROOT/'config/portals.yml',self.root,RECENT[:10],env={'WO_ALERTS_SPOOL':str(spool.path)})
  self.assertEqual(result['quarantined'],1)
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=0)):
   self.assertEqual(email_worker.run(self.database,self.root,'https://example.test','x'*40)['status'],'partial')
   recovered=email_worker.run(self.database,self.root,'https://example.test','x'*40,retry_quarantine=True)
   self.assertEqual((recovered['status'],recovered['processing']['messages'],recovered['processing']['quarantined']),('ok',1,0))
   self.assertTrue(recovered['retry_quarantine'])
   self.assertEqual(recovered['import']['new_observed'],1)
   again=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual((again['status'],again['processing']['messages']),('ok',0))
  row=json.loads((self.root/'pending/observations.json').read_text())['items']['alert:jackandjill\x1f'+ONE]
  self.assertEqual((row['first_seen_at'],row['last_seen_at']),(RECENT,RECENT))
  self.assertEqual((row['current']['pay_min'],row['current']['pay_max']),('50000','70000'))
  self.assertEqual(spool.info()['count'],1)

 def test_full_bad_page_cannot_block_collection_and_import_of_a_later_alert(self):
  from test_scan_alerts import alert_email
  import db
  raw=alert_email()
  good=ITEM|dict(sequence=11,receipt_id=hashlib.sha256(raw).hexdigest(),raw_base64=base64.b64encode(raw).decode())
  pending=[ITEM|dict(sequence=i,receipt_id='0'*64) for i in range(1,11)]+[good]
  def api(url,token,path,body=None):
   if path.startswith('/v1/messages'):
    after=int(path.split('after=')[1]) if '?after=' in path else 0
    return dict(messages=[m for m in pending if m['sequence']>after][:10],pending=len(pending),total_received=11)
   self.assertEqual(body['receipts'],[good['receipt_id']])
   pending.remove(good)
   return {'acknowledged':1}
  with patch.object(email_worker,'request',side_effect=api):
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['status'],'failed')
  self.assertEqual(result['collection']['received'],1)
  self.assertEqual(result['collection']['failed'],10)
  self.assertEqual(result['collection']['pending'],10)
  self.assertEqual(result['processing']['messages'],1)
  self.assertEqual(len(pending),10)
  conn=db.connect(self.database);self.addCleanup(conn.close)
  self.assertGreater(conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0],0)

 def test_real_parser_import_and_replay_do_not_duplicate_jobs(self):
  from test_scan_alerts import alert_email
  import db
  raw=alert_email()
  spool=MailSpool(self.root/'email'/'mail.db')
  spool.receive(raw,envelope_from='forwarder@example.test',envelope_to='alerts@example.com',received_at=RECENT)
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=1)):
   first=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(first['status'],'ok',first)
  self.assertEqual(first['processing']['messages'],1)
  conn=db.connect(self.database);self.addCleanup(conn.close)
  count=conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0]
  self.assertGreater(count,0)
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=0)):
   second=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(second['status'],'ok',second)
  self.assertEqual(second['processing']['messages'],0)
  self.assertEqual(conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0],count)

 def test_a_run_without_new_alert_jobs_records_the_read_without_a_backup_or_replay(self):
  from test_scan_alerts import alert_email
  import db,json
  spool=MailSpool(self.root/'email'/'mail.db')
  spool.receive(alert_email(),envelope_from='forwarder@example.test',envelope_to='alerts@example.com',received_at=RECENT)
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=1)):
   first=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertNotIn('import_skipped',first)
  conn=db.connect(self.database);self.addCleanup(conn.close)
  health=lambda:conn.execute("SELECT state,observed_at,last_success FROM source_health WHERE scope='alert:linkedin'").fetchone()[:]
  backups=lambda:len(list((self.root/'backups').glob('work-ops-pre-ingest-*.db')))
  before=health();reports=conn.execute('SELECT COUNT(*) FROM import_runs').fetchone()[0]
  self.assertEqual(backups(),1)
  with patch.object(email_worker,'collect',return_value=dict(pending=0,received=0)):
   quiet=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual((quiet['status'],quiet['import_skipped'],quiet['processing']['state']),('ok','no new items','empty'),quiet)
  self.assertEqual((backups(),conn.execute('SELECT COUNT(*) FROM import_runs').fetchone()[0]),(1,reports))
  # The parser's read still reaches source health, as the replay recorded it before.
  self.assertEqual(health(),('empty',quiet['processing']['observed_at'],quiet['processing']['observed_at']))
  self.assertGreater(health()[1],before[1])
  saved=json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email-import:%' ORDER BY run_id DESC LIMIT 1").fetchone()[0])
  self.assertEqual(saved['import_skipped'],'no new items');self.assertNotIn('import',saved)

class RecoveryTests(RunCase):
 def test_collection_failure_does_not_prevent_processing_previously_retained_mail(self):
  with patch.object(email_worker,'collect',side_effect=ValueError('fixture')), patch.object(email_worker.ingest,'run',return_value={'status':'ok','sources':{'alert:linkedin':{'state':'empty'}},'import':{}}) as process:
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  process.assert_called_once();self.assertEqual(result['status'],'failed')
  self.assertEqual(result['collection_error'],'ValueError')

 def test_partial_collection_counts_survive_in_the_failed_run_receipt(self):
  error=ValueError('fixture');error.collection=dict(received=9,duplicates=0,pending=1,failed=1)
  with patch.object(email_worker,'collect',side_effect=error),patch.object(email_worker.ingest,'run',return_value={'status':'ok','sources':{'alert:linkedin':{'state':'empty'}},'import':{}}):
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['status'],'failed')
  self.assertEqual(result.get('collection',{}).get('received'),9)
  self.assertEqual(result.get('collection',{}).get('failed'),1)

 def test_next_run_uses_last_saved_collection_cursor_without_losing_pending_mail(self):
  import db
  conn=db.connect(self.database)
  email_worker.save_receipt(conn,'email:2026-10-17T12:00:00Z',dict(started_at='2026-10-17T12:00:00Z',status='failed',collection={'resume_after':200}))
  conn.close()
  def collect(url,token,spool,**kwargs):
   if kwargs.get('after')!=200:raise ValueError('Cursor was not resumed')
   spool.receive(RAW,**dict(envelope_from='forwarder@example.test',envelope_to='alerts@example.com',received_at=ITEM['received_at']))
   return dict(received=1,pending=200,resume_after=201)
  with patch.object(email_worker,'collect',side_effect=collect),patch.object(email_worker.ingest,'run',return_value={'status':'ok','sources':{'alert:linkedin':{'state':'empty'}},'import':{}}):
   result=email_worker.run(self.database,self.root,'https://example.test','x'*40)
  self.assertEqual(result['spool']['count'],1)
  self.assertEqual(result['status'],'partial')
 def test_backup_restores_spool_and_checkpoint_together(self):
  import backup_email,tarfile
  spool=MailSpool(self.root/'email'/'mail.db')
  spool.receive(RAW,envelope_from='forwarder@example.test',envelope_to='alerts@example.com',received_at=ITEM['received_at'])
  (self.root/'backups').mkdir();(self.root/'pending'/'alerts').mkdir(parents=True)
  (self.root/'pending'/'alerts'/'spool-state.json').write_text('{"last_sequence":1}')
  archive=self.root/'backups'/'email.tar.gz';backup_email.snapshot(self.root,archive)
  restored=self.root/'restore';restored.mkdir()
  with tarfile.open(archive) as tar:tar.extractall(restored,filter='data')
  self.assertEqual(MailSpool(restored/'email'/'mail.db',readonly=True).info()['count'],1)
  self.assertEqual((restored/'pending'/'alerts'/'spool-state.json').read_text(),'{"last_sequence":1}')
