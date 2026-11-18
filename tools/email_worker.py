#!/usr/bin/env python3
"""VPS-only pull, durable receipt and canonical import of selected job-alert emails."""
from __future__ import annotations
import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'workspace')]
import credentials
import db
import ingest
import records
from mail_intake import MailSpool, RECIPIENT

class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):
  raise ValueError('Email buffer redirected; credentials not forwarded')

class CollectionError(ValueError):
 def __init__(self,collection):
  super().__init__('Some buffered mail could not be collected; unacknowledged mail remains queued')
  self.collection=dict(collection)

def request(origin, token, path, body=None):
 url=urllib.parse.urlsplit(origin)
 if url.scheme!='https' or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
  raise ValueError('Email buffer requires an HTTPS origin')
 data=None if body is None else json.dumps(body).encode()
 req=urllib.request.Request(origin.rstrip('/')+path,data=data,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','User-Agent':'work-ops-email-collector/1.0 (+https://work.example.com)'})
 with urllib.request.build_opener(NoRedirect).open(req,timeout=30) as response:
  raw=response.read(8*1024*1024+1)
  if len(raw)>8*1024*1024:raise ValueError('Oversized buffer response')
  return json.loads(raw)

def collect(origin,token,spool,limit=200,after=0):
 if type(limit) is not int or not 1<=limit<=200:raise ValueError('Invalid collection limit')
 if type(after) is not int or not 0<=after<=2**53-1:raise ValueError('Invalid collection cursor')
 out=dict(received=0,duplicates=0,failed=0,pending=None,total_received=None,last_received_at=None,resume_after=after)
 deadline=time.monotonic()+180
 attempted=0
 try:
  while attempted<limit and time.monotonic()<deadline:
   page=request(origin,token,'/v1/messages'+(f'?after={after}' if after else ''))
   if not isinstance(page,dict):raise ValueError('Invalid mail batch')
   messages=page.get('messages');pending=page.get('pending')
   if not isinstance(messages,list) or len(messages)>10 or type(pending) is not int or pending<len(messages):
    raise ValueError('Invalid mail batch')
   out.update({key:page.get(key) for key in ('pending','total_received','last_received_at')})
   if not messages:
    out['resume_after']=0  # Completed the sweep: retry retained failures on the next pass.
    break
   # Older buffers lack a cursor. Keep clean rolling releases working, but stop visibly on failure.
   sequenced=all(isinstance(item,dict) and 'sequence' in item for item in messages)
   if sequenced:
    previous=after
    for item in messages:
     sequence=item['sequence']
     if type(sequence) is not int or not previous<sequence<=2**53-1:raise ValueError('Invalid mail sequence')
     previous=sequence
   elif after or any(isinstance(item,dict) and 'sequence' in item for item in messages):
    raise ValueError('Invalid mail sequence')
   acks=[]
   for item in messages[:limit-attempted]:
    attempted+=1
    if sequenced:after=out['resume_after']=item['sequence']
    try:
     raw=base64.b64decode(item['raw_base64'],validate=True)
     if hashlib.sha256(raw).hexdigest()!=item['receipt_id']:raise ValueError('Mail digest mismatch')
     receipt=spool.receive(raw,envelope_from=item['envelope_from'],envelope_to=RECIPIENT,received_at=item['received_at'])
    except Exception:
     out['failed']+=1;continue
    out['duplicates' if receipt['duplicate'] else 'received']+=1
    if receipt['receipt_id'] not in acks:acks.append(receipt['receipt_id'])
   # No failed/unretained item is acknowledged. A cursor only skips it for this bounded pass.
   if acks:
    ack=request(origin,token,'/v1/ack',{'receipts':acks})
    if not isinstance(ack,dict) or type(ack.get('acknowledged')) is not int or ack['acknowledged']!=len(acks):
     raise ValueError('Invalid acknowledgment')
    out['pending']=max(0,out['pending']-len(acks))
   if not out['pending']:
    out['resume_after']=0
    break
   if out['failed'] and not sequenced:break
 except Exception:
  raise CollectionError(out) from None
 if out['failed']:raise CollectionError(out)
 return out

def save_receipt(conn,run_id,receipt):
 with records.transaction(conn):
  conn.execute('INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET receipt_json=excluded.receipt_json,status=excluded.status',
   (run_id,receipt['started_at'][:10],json.dumps(receipt,sort_keys=True),receipt['status']))

def processing_totals(sources):
 """Retain the legacy aggregate receipt while accounting for every configured email source."""
 if not sources:return {}
 if len(sources)==1:return dict(next(iter(sources.values())))
 rows=list(sources.values())
 state=next((s for s in ('error','config','partial','ok','empty') if any(r.get('state')==s for r in rows)),'error')
 out=dict(state=state,complete=all(r.get('complete',True) for r in rows))
 for key in ('messages','jobs','eligible','new','items','quarantined','cards','logo_cards','logo_expired','logo_expiring_soon','logo_expiry_unknown'):
  if any(key in r for r in rows):out[key]=sum(r.get(key,0) for r in rows)
 for key in ('last_sequence','high_watermark'):
  values=[r[key] for r in rows if type(r.get(key)) is int]
  if values:out[key]=max(values)
 observed=[r['observed_at'] for r in rows if r.get('observed_at')]
 if observed:out['observed_at']=max(observed)
 return out

def retain_source_success(conn,receipt,previous):
 """Carry the latest per-source success forward so views never scan the receipt archive."""
 old=previous.get('sources') or {}
 current=receipt.get('sources') or {scope:{'state':'unknown'} for scope in old}
 for scope,value in current.items():
  last=old.get(scope,{}).get('last_alert_processed')
  # One-time compatibility for the first upgraded run. Subsequent receipts carry this result.
  if not old and scope=='alert:linkedin':
   row=conn.execute("SELECT json_extract(receipt_json,'$.finished_at') FROM scan_runs "
                    "WHERE run_id >= 'email:' AND run_id < 'email;' AND status='ok' "
                    "AND json_extract(receipt_json,'$.processing.messages')>0 ORDER BY run_id DESC LIMIT 1").fetchone()
   last=row[0] if row else None
  if (receipt.get('phase')=='complete' and value.get('state')=='ok'
      and value.get('messages',0)>0 and not receipt.get('import',{}).get('error')):
   last=receipt['finished_at']
  value['last_alert_processed']=last
 receipt['sources']=current

def run(db_path,data,origin,token,*,retry_quarantine=False):
 db.configure_logging()
 data=Path(data);data.mkdir(parents=True,exist_ok=True)
 # Separate from ingest's writer lock: serialize polling too, without deadlocking the importer.
 with (data/'.email-worker.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  started=dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
  run_id='email:'+started; receipt=dict(started_at=started,status='running',phase='collection')
  if retry_quarantine:receipt['retry_quarantine']=True
  identity=db.activate_job(run_id,'wo-email.service')
  conn=None
  try:
   conn=db.connect(db_path,timeout=db.WORKER_LOCK_WAIT)
   prior=conn.execute("SELECT json_extract(receipt_json,'$.collection.resume_after') FROM scan_runs "
                      "WHERE run_id LIKE 'email:%' AND json_type(receipt_json,'$.collection.resume_after')='integer' ORDER BY run_id DESC LIMIT 1").fetchone()
   after=prior[0] if prior and type(prior[0]) is int and 0<=prior[0]<=2**53-1 else 0
   prior_run=conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id >= 'email:' AND run_id < 'email;' ORDER BY run_id DESC LIMIT 1").fetchone()
   previous=json.loads(prior_run[0]) if prior_run else {}
   # A killed poll leaves this receipt behind; retain source success before any external I/O.
   retain_source_success(conn,receipt,previous)
   save_receipt(conn,run_id,receipt)
   try:
    if not token or len(token)<32:raise ValueError('Missing buffer credential')
    spool=MailSpool(data/'email'/'mail.db')
    try:
     receipt['collection']=collect(origin,token,spool,after=after)
    except Exception as error:
     receipt['collection_error']=type(error).__name__
     if isinstance(getattr(error,'collection',None),dict):receipt['collection']=error.collection
     receipt['message']='Some mail could not be collected. Retained mail is still processed; collection will retry.'
    receipt['phase']='processing';save_receipt(conn,run_id,receipt)
    # One canonical importer and shared writer lock. No model/API spending or Gmail access.
    adapters=[('job alerts',[sys.executable,str(ROOT/'tools/scan_alerts.py'),'--portals','{portals}','--out','{data}'])]
    if retry_quarantine:adapters[0][1].append('--retry-quarantine')
    result=ingest.run(db_path,data,{'WO_ALERTS_SPOOL':str(spool.path)},
                      adapters=adapters,lock_wait=90,run_prefix='email-import',incremental=True)
    if result.get('import_skipped'):receipt['import_skipped']=result['import_skipped']
    receipt['sources']={scope:dict(detail) for scope,detail in result.get('sources',{}).items() if scope.startswith('alert:')}
    receipt['processing']=processing_totals(receipt['sources'])
    receipt['import']=dict(result.get('import') or {})
    if 'phase_seconds' in result: receipt['phase_seconds']=result['phase_seconds']
    if 'seconds' in result: receipt['seconds']=result['seconds']
    if 'memory' in result: receipt['memory']=result['memory']
    if receipt['import'].get('write_lock_seconds') is None and result.get('write_lock_seconds') is not None:
     receipt['import']['write_lock_seconds']=result['write_lock_seconds']
    receipt['status']='ok' if result.get('status')=='ok' and not receipt.get('collection',{}).get('pending') else 'partial'
    if not receipt['processing'] or receipt['processing'].get('state') not in ('ok','empty'):
     receipt['status']='partial'
    if receipt.get('collection_error'):receipt['status']='failed'
    receipt['phase']='complete'
    receipt['spool']=spool.info()
   except Exception as error:
    receipt.update(status='failed',error_kind=type(error).__name__,message='Email processing did not finish. Retained mail will be retried; inspect the service log.')
    # Safe exception type only; never raw mail, URL, token or provider response.
    print('email processing failed: '+type(error).__name__,file=sys.stderr)
   receipt['finished_at']=db.now()
   retain_source_success(conn,receipt,previous)
   save_receipt(conn,run_id,receipt)
  finally:
   try:
    if conn is not None: conn.close()
   finally:
    db.restore_job(identity)
 return receipt

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--db',type=Path,required=True);ap.add_argument('--data',type=Path,required=True)
 ap.add_argument('--buffer-url',required=True);ap.add_argument('--token-file',type=Path,required=True)
 ap.add_argument('--retry-quarantine',action='store_true',help='attended retry of retained parser failures after a reviewed fix')
 args=ap.parse_args()
 token=credentials.get(args.token_file.name,{'CREDENTIALS_DIRECTORY':str(args.token_file.parent)})
 with db.failures_noted(args.data,'wo-email.service'):
  receipt=run(args.db,args.data,args.buffer_url,token,retry_quarantine=args.retry_quarantine)
 print(json.dumps(receipt,sort_keys=True))
 return 0 if receipt['status']=='ok' else 1
if __name__=='__main__':raise SystemExit(main())
