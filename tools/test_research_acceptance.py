"""The one-time follow-up must not spend, repair records or mistake receipts for evidence."""
import datetime as dt
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workspace'))
import db
import records
from email_worker import save_receipt
import research_acceptance as acceptance
import research_worker as worker


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name);self.path=self.data/'test.db'
        self.conn=db.connect(self.path);db.migrate(self.conn);self.addCleanup(self.conn.close)
        self.day=dt.date(2026,10,19);self.now=dt.datetime(2026,10,19,9,5,tzinfo=dt.timezone.utc)
        self.started='2026-10-19T08:00:10Z';self.finished='2026-10-19T08:10:00Z'
        self.run_id='research:'+self.started+':test'
        self.receipt=dict(started_at=self.started,finished_at=self.finished,status='partial',
                          focused=True,people=5,total=7,checked=1,backlog=6,whole_queue=455,
                          outside_focus=453,current=0,reused=0,usage={'calls':0},items=[])

    def save(self):
        save_receipt(self.conn,self.run_id,self.receipt)
        state='waiting' if self.receipt.get('reason')=='usage_limit' else 'partial'
        save_receipt(self.conn,'caretaker:2026-10-19T08:15:00Z',dict(started_at='2026-10-19T08:15:00Z',
            finished_at='2026-10-19T08:15:01Z',status='partial',checks=[{'id':'research','status':state}]))

    def audit(self):
        return acceptance.audit(self.conn,self.data,self.day,self.now,'2026-10-19T08:00:09Z')

    def finding(self):
        job=records.create(self.conn,dict(type='job',name='Example Engineer',organisation='Example',
                           url='https://example.com/job',status='candidate'),actor='agent')['record']
        quote='Example Engineer at Example: apply now for this position.'
        at='2026-10-19T08:05:00Z'
        finding=dict(id=job['id'],reviewed_at=at,source_urls=[job['url']],facts=[quote],
            freshness_checks=[dict(scope='vacancy',status='confirmed',checked_at=at,
                source_url=job['url'],basis='Exact role. Evidence: '+quote)])
        batch=dict(finding=finding,sources=[dict(url=job['url'],checked_at=at,ok=True,text=quote)],note=None)
        name='a'*32+'.json';digest=worker.private_json(self.data/'research-server'/name,batch)
        with patch('evidence_checks.utc_now',return_value=self.now):
            job['context_snapshot']=worker.brief_research.snapshot(self.conn,dict(records.get(self.conn,job['id']),contacts=job.get('contacts',[])))
            result=worker.apply_finding(self.conn,job,finding,'research:vps:'+name,None)
        self.receipt['items']=[dict(id=job['id'],batch=name,sha256=digest,outcome='confirmed',import_=result)]
        self.receipt['items'][0]['import']=self.receipt['items'][0].pop('import_')
        return name,batch

    def test_missing_running_and_ambiguous_passes_are_not_accepted(self):
        self.assertEqual(self.audit()['status'],'not_observed')
        self.receipt.pop('finished_at');self.receipt['status']='running';self.save()
        self.assertEqual(self.audit()['status'],'incomplete')
        self.receipt['started_at']='2026-10-19T08:01:00Z'
        save_receipt(self.conn,'research:other',self.receipt)
        self.assertEqual(self.audit()['status'],'ambiguous')

    def test_real_canonical_evidence_is_required_and_no_database_writes_occur(self):
        self.finding();self.save();before=self.conn.total_changes
        report=self.audit()
        self.assertEqual(report['status'],'evidence_saved')
        self.assertEqual(report['evidence']['fresh_confirmations'],1)
        self.assertEqual(report['evidence']['supported_facts'],1)
        self.assertEqual(report['evidence']['canonical_batches'],1)
        self.assertEqual(self.conn.total_changes,before)
        text=acceptance.markdown(report)
        self.assertNotIn('Example Engineer',text);self.assertNotIn('https://example.com',text)
        self.assertNotIn(self.receipt['items'][0]['id'],text)
        self.conn.execute("DELETE FROM observations WHERE source LIKE 'research:vps:%'")
        report=self.audit()
        self.assertEqual(report['status'],'needs_attention')
        self.assertEqual(report['evidence']['fresh_confirmations'],0)

    def test_receipt_success_without_fresh_source_proof_is_not_useful_evidence(self):
        name,batch=self.finding()
        batch['sources'][0]['text']='The claimed quote is absent.'
        path=self.data/'research-server'/name;raw=json.dumps(batch).encode();path.write_bytes(raw)
        self.receipt['items'][0]['sha256']=hashlib.sha256(raw).hexdigest();self.receipt['status']='ok';self.save()
        report=self.audit()
        self.assertEqual(report['status'],'needs_attention')
        self.assertEqual(report['evidence']['fresh_confirmations'],0)
        self.assertEqual(report['evidence']['supported_facts'],0)

    def test_digest_change_and_path_traversal_are_rejected(self):
        name,batch=self.finding();self.save()
        (self.data/'research-server'/name).write_text('{}')
        self.assertIn('batch_integrity',self.audit()['issues'])
        self.receipt['items'][0]['batch']='../../secret';self.save()
        self.assertIn('batch_integrity',self.audit()['issues'])

    def test_stale_or_future_confirmations_cannot_count(self):
        name,batch=self.finding()
        for at in ('2026-10-17T08:05:00Z','2026-10-20T08:05:00Z'):
            with self.subTest(at=at):
                batch['sources'][0]['checked_at']=at
                batch['finding']['freshness_checks'][0]['checked_at']=at
                path=self.data/'research-server'/name;raw=json.dumps(batch).encode();path.write_bytes(raw)
                self.receipt['items'][0]['sha256']=hashlib.sha256(raw).hexdigest()
                self.conn.execute("UPDATE observations SET payload_json=? WHERE source=?",
                    (json.dumps(batch['finding']),'research:vps:'+name))
                self.save()
                self.assertEqual(self.audit()['evidence']['fresh_confirmations'],0)

    def test_durable_unknown_calls_and_all_lane_totals_bound_the_report(self):
        self.receipt['usage']['calls']=7;self.save()
        for i in range(7):
            self.conn.execute('INSERT INTO codex_invocations(id,run_id,lane,started_at,day,week) VALUES(?,?,?,?,?,?)',
                (str(i),self.run_id,'research',self.started,'2026-10-19','2026-10-19'))
        report=self.audit()
        self.assertFalse(report['caps']['within_limits']);self.assertEqual(report['caps']['unknown_calls'],7)
        self.assertIn('invocation_caps',report['issues'])
        for i in range(10):
            self.conn.execute('INSERT INTO codex_invocations(id,run_id,lane,started_at,day,week) VALUES(?,?,?,?,?,?)',
                ('other'+str(i),'other','evaluation',self.started,'2026-10-19','2026-10-19'))
        self.assertEqual(self.audit()['caps']['day'],17)

    def test_quota_wait_and_missing_timer_or_later_caretaker_stay_explicit(self):
        self.receipt.update(status='blocked',reason='usage_limit');self.save()
        self.assertEqual(self.audit()['status'],'waiting')
        report=acceptance.audit(self.conn,self.data,self.day,self.now,None)
        self.assertIn('timer_not_corroborated',report['issues'])
        self.conn.execute("DELETE FROM scan_runs WHERE run_id LIKE 'caretaker:%'")
        self.assertIn('later_caretaker_missing',self.audit()['issues'])

    def test_a_caretaker_receipt_that_stored_only_changes_still_reports_research(self):
        save_receipt(self.conn,self.run_id,self.receipt)
        base='caretaker:2026-10-19T08:05:00Z'
        save_receipt(self.conn,base,dict(started_at='2026-10-19T08:05:00Z',finished_at='2026-10-19T08:05:01Z',
            status='partial',checks=[{'id':'research','status':'partial'}]))
        save_receipt(self.conn,'caretaker:2026-10-19T08:15:00Z',dict(started_at='2026-10-19T08:15:00Z',
            finished_at='2026-10-19T08:15:01Z',status='partial',base=base,chain=1,checks=[]))
        report=self.audit()
        self.assertEqual(report['caretaker'],'partial')
        self.assertNotIn('caretaker_outcome_mismatch',report['issues'])

    def test_report_is_immutable_and_readback_verifies_hashes(self):
        self.save();report=self.audit()
        acceptance.publish(self.data,self.day,report)
        original=acceptance.read_report(self.data,self.day)
        acceptance.publish(self.data,self.day,dict(report,status='changed'))
        self.assertEqual(acceptance.read_report(self.data,self.day),original)
        folder=self.data/'research-acceptance'/self.day.isoformat()
        (folder/'report.md').write_text('altered')
        with self.assertRaises(ValueError):acceptance.read_report(self.data,self.day)

    def test_service_entrypoint_uses_a_readonly_snapshot_and_refuses_early_execution(self):
        self.save();before=self.conn.total_changes
        args=['acceptance','--db',str(self.path),'--data',str(self.data),'--date',self.day.isoformat()]
        with patch.object(sys,'argv',args), patch.object(acceptance,'timer_trigger',return_value='2026-10-19T08:00:09Z'), \
             patch.object(acceptance.dt,'datetime',wraps=dt.datetime) as clock, patch('builtins.print'):
            clock.now.return_value=self.now
            self.assertEqual(acceptance.main(),0)
            self.assertEqual(acceptance.main(),0)
        self.assertEqual(self.conn.total_changes,before)
        import wo
        with patch.object(wo.wodb,'connect',side_effect=AssertionError('Report read must not open DB')),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wo.main(['--db',str(self.path),'research-review']),0)
        self.assertIn('Canonical evidence',output.getvalue())
        other=dt.date(2026,10,20)
        with patch.object(sys,'argv',args[:-1]+[other.isoformat()]),redirect_stdout(io.StringIO()), \
             patch.object(acceptance.dt,'datetime',wraps=dt.datetime) as clock, patch('sys.stderr',io.StringIO()):
            clock.now.return_value=self.now
            with self.assertRaises(SystemExit):acceptance.main()
        self.assertFalse(acceptance.folder_for(self.data,other).exists())
