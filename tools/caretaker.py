#!/usr/bin/env python3
"""Deterministic VPS checks. No model, shell, service changes, retries or data repair."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'workspace')]
import db
import failure_tasks
import record_views
from source_coverage import coverage_limit
from email_worker import save_receipt
from ops_policy import CARETAKER_UNITS as UNITS

LANE_UNITS={'ingest':'wo-scan.service','email':'wo-email.service','research':'wo-research.service'}

def stamp(now):return now.isoformat()

def read_json(path):
    try:
        if path.stat().st_size>100_000:return {}
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}

def age(at,now):
    try:
        t=dt.datetime.fromisoformat(at.replace('Z','+00:00'))
        if not t.tzinfo:return None
        seconds=(now-t).total_seconds()
        return max(0,seconds) if seconds>=-300 else None
    except (ValueError,TypeError,AttributeError):return None

def services():
    try:
        result=subprocess.run(['systemctl','show',*UNITS,'--property=Id,ActiveState,Result'],capture_output=True,text=True,timeout=15)
        if result.returncode:return {}
        out={}
        for block in result.stdout.split('\n\n'):
            values=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
            if values.get('Id') in UNITS:out[values['Id']]=values
        return out
    except (OSError,subprocess.TimeoutExpired):return {}

def healthy_origin():
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8769/api/health',timeout=5) as response:
            value=json.loads(response.read(8192))
        return value.get('status')=='ok' and value.get('mode')=='database'
    except (OSError,ValueError):return False

def latest(conn,prefix):
    row=conn.execute('SELECT receipt_json FROM scan_runs WHERE run_id LIKE ? ORDER BY run_id DESC LIMIT 1',(prefix+':%',)).fetchone()
    return json.loads(row[0]) if row else {}

def inspect(conn,data,now=None,*,units=None,health=None):
    now=now or dt.datetime.now(dt.timezone.utc);data=Path(data);checks=[]
    paused=(data/'research.pause').exists()
    def add(id,label,status,detail,at=None,subject=None):
        checks.append(dict(id=id,label=label,status=status,detail=detail,verified_at=at,subject=subject,
                           fingerprint=hashlib.sha256(json.dumps([id,status,detail]).encode()).hexdigest()))
    units=services() if units is None else units
    for unit in UNITS:
        if paused and unit in ('wo-research.timer','wo-research.service','wo-research-codex.socket'):
            add('unit:'+unit,unit,'paused','Research is explicitly paused.');continue
        state=units.get(unit,{})
        oneshot=unit in ('wo-email.service','wo-research.service','wo-scan.service','wo-backup.service')
        good=bool(state) and state.get('ActiveState')!='failed' and state.get('Result','success')=='success'
        if not oneshot:good=good and state.get('ActiveState')=='active'
        cause,failures=db.failure_summary(data,unit,now)
        if good and failures>=3:
            add('unit:'+unit,unit,'failed','Failed 3 or more times in 24 hours although it ran again since.'+cause);continue
        add('unit:'+unit,unit,'ok' if good else 'failed','Service state observed healthy.' if good else 'Service/timer inactive, failed or unreadable; inspect systemctl and its journal.'+cause,stamp(now) if good else None)
    health=healthy_origin() if health is None else health
    add('origin','Site health','ok' if health else 'failed','Origin answered with healthy database mode.' if health else 'Origin health unavailable; inspect workspace and tunnel services.',stamp(now) if health else None)
    receipts={}
    for prefix,hours,max_run in (('ingest',30,120),('email',.75,15),('research',16,50)):
        r=receipts[prefix]=latest(conn,prefix)
        if prefix=='research' and paused:
            add(prefix,'Research','paused','Research is explicitly paused.');continue
        elapsed=age(r.get('started_at'),now);finished=age(r.get('finished_at'),now)
        status=r.get('status');state='failed';detail='No timely successful completion; inspect the latest receipt and service journal.'
        if elapsed is not None and status=='running' and elapsed<max_run*60:
            state='running';detail='A bounded run is in progress; this is not recovery evidence.'
        elif elapsed is not None and elapsed<hours*3600 and finished is not None:
            if prefix=='research' and r.get('reason')=='disabled':state='paused';detail='Research disabled by configuration or operator pause.'
            # A stop reason is never recovery evidence (CLAUDE.md); name which limit stopped the pass.
            elif prefix=='research' and record_views.research_pass_capped(r):state='waiting';detail='The last pass stopped at its per-pass call cap; remaining targets wait for the next pass. A stopped pass is not recovery evidence.'
            elif prefix=='research' and r.get('reason')=='collection_limit':state='waiting';detail='Waiting for provider collection allowance; existing evidence still expires.'
            elif prefix=='research' and r.get('reason')=='usage_limit':state='waiting';detail='Waiting for Codex allowance; existing evidence still expires.'
            elif status=='ok':state='ok';detail='A recent successful completion is recorded.'
            # 22 Oct: a completed research pass is recovery; its unverified routes show on Research.
            elif status=='partial' and not r.get('reason') and prefix=='research':state='ok';detail='A recent completed pass is recorded; unverified sources and routes remain visible on Research.'
            elif status=='partial' and not r.get('reason'):state='partial';detail='Run completed with coverage gaps; earlier failures are not cleared by partial coverage.'
        if state=='failed':
            recorded=r.get('error_kind') or r.get('reason')
            if recorded:detail+=f' Latest run {status}: {recorded}.'
            detail+=db.failure_summary(data,LANE_UNITS[prefix],now)[0]
        add(prefix,{'ingest':'Daily sources','email':'Job alert email processing','research':'Job and contact research'}[prefix],state,detail,r.get('finished_at') if state=='ok' else None)
    mail=receipts['email'];spool=mail.get('spool',{});processing=mail.get('processing',{});collection=mail.get('collection',{})
    fresh=age(mail.get('finished_at'),now)
    mail_ok=mail.get('status')=='ok' and fresh is not None and fresh<2700
    capacity=spool.get('count',0)>=750 or spool.get('bytes',0)>=48*1024*1024
    add('email-capacity','Mail archive capacity','failed' if capacity else 'ok' if mail_ok and spool else 'unknown',
        'Archive approaching its capacity: use the attended email archive/recovery procedure.' if capacity else 'Capacity is clear in a fresh receipt.' if mail_ok and spool else 'No fresh capacity evidence.',mail.get('finished_at') if mail_ok and spool and not capacity else None)
    lag=bool(collection.get('pending') or processing.get('quarantined') or (processing.get('high_watermark',0) or 0)>(processing.get('last_sequence',0) or 0))
    add('email-backlog','Mail collection and parser','failed' if lag else 'ok' if mail_ok else 'unknown',
        'Pending, quarantined or unprocessed mail needs inspection; preserve originals and checkpoints.' if lag else 'No pending/quarantined parser work in a successful receipt.' if mail_ok else 'No fresh successful parser receipt.',mail.get('finished_at') if mail_ok and not lag else None)
    backup=read_json(data/'backups'/'last-backup.json');elapsed=age(backup.get('at'),now)
    good=elapsed is not None and elapsed<30*3600
    add('backup','Nightly backup','ok' if good else 'failed','Recent completed backup receipt.' if good else 'Backup missing, invalid or older than 30 hours; inspect wo-backup before recovery.'+db.failure_summary(data,'wo-backup.service',now)[0],backup.get('at') if good else None)
    offsite=read_json(data/'offsite'/'last-run.json');elapsed=age(offsite.get('at'),now)
    good=elapsed is not None and elapsed<30*3600 and offsite.get('result')=='passed'
    add('offhost','Off-host copy and restore drill','ok' if good else 'failed' if offsite else 'unknown',
        'Fresh off-host restore receipt passed.' if good else 'Inspect the Mac off-host receipt; its restore check is not current/clean.' if offsite else 'Evidence remains on the Mac; the VPS cannot verify its off-host copy or restore drill.',offsite.get('at') if good else None)
    for scope,s in sorted(receipts['ingest'].get('sources',{}).items()):
        status=s.get('state')
        if status in ('budget','disabled','skipped'):continue
        complete=status in ('ok','empty') and s.get('complete') is True
        state='ok' if complete else 'partial' if status in ('ok','empty','partial') else 'failed'
        detail=('Source read completely.' if state=='ok' else
                f'Read partly ({coverage_limit(s)} cap); not a failure.' if state=='partial' else
                'Source failed; its failure decision on the Dashboard holds evidence and recovery.')
        add('source:'+scope,scope,state,detail,
            (s.get('observed_at') or receipts['ingest'].get('finished_at')) if complete else None,failure_tasks.subject_for(scope))
    return checks

def record(conn,checks,now):
    out={'raised':[],'cleared':[]}
    for c in checks:
        # Source failures already have their own transient/repeated-failure policy in for_ingest.
        if c['id'].startswith('source:'):continue
        subject=record_views.caretaker_subject(dict(c,subject=None))  # the projection uses the same rule
        key='caretaker_success:'+c['id']
        previous=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if c['status']=='failed':
            conn.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('caretaker_failure:'+c['id'],stamp(now)))
            if failure_tasks.raise_task(conn,subject,c['label']+' needs attention.',c['detail'],'fix',
                'Inspect the caretaker receipt and documented recovery; no automatic repair was attempted.',previous[0] if previous else None):
                out['raised'].append(c['id'])
        elif c['status']=='ok' and c['verified_at']:
            failed=conn.execute('SELECT value FROM meta WHERE key=?',('caretaker_failure:'+c['id'],)).fetchone()
            verified_age=age(c['verified_at'],now)
            failed_age=age(failed[0],now) if failed else None
            if verified_age is None or (failed and (failed_age is None or verified_age>=failed_age)):
                continue  # Restoring an old receipt or restarting a timer is not new recovery evidence.
            conn.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,c['verified_at']))
            if failure_tasks.clear(conn,subject,c['detail'],c['verified_at'],'caretaker_check'):out['cleared'].append(c['id'])
    return out

def run(path,data,write=False,now=None):
    db.configure_logging()
    now=now or dt.datetime.now(dt.timezone.utc);data=Path(data)
    run_id='caretaker:'+stamp(now)+':'+uuid.uuid4().hex[:8]
    with db.job_context(run_id,'wo-caretaker.service'):
        conn=db.connect(path,timeout=db.WORKER_LOCK_WAIT)
        try:
            if db.pending(conn):raise ValueError('Migrate before running caretaker')
            if (data/'caretaker.pause').exists():checks=[];status='paused'
            else:
                checks=inspect(conn,data,now);status='failed' if any(c['status']=='failed' for c in checks) else 'partial' if any(c['status'] in ('unknown','partial','running','waiting','paused') for c in checks) else 'ok'
            receipt=dict(started_at=stamp(now),finished_at=stamp(dt.datetime.now(dt.timezone.utc)),status=status,checks=checks,model_calls=0)
            if write:
                with (data/'.writer.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    receipt['incidents']=record(conn,checks,now)
                    # Stores only the checks that changed; the printed receipt keeps the full list.
                    save_receipt(conn,run_id,record_views.compact_caretaker_receipt(conn,receipt))
            import collection_reads
            try:receipt['collection']=collection_reads.summary(data/'collection.db',at=now)
            except (ValueError,OSError):receipt['collection']={'state':'unknown','reason':'invalid-policy'}
            return receipt
        finally:conn.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True);parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--record',action='store_true',help='publish a receipt and deduplicated operational incidents')
    args=parser.parse_args()
    if not args.db.is_file():parser.error('Database must already exist')
    with db.failures_noted(args.data,'wo-caretaker.service'):
        receipt=run(args.db,args.data,args.record)
    print(json.dumps(receipt))
    return 0 if receipt['status']!='failed' else 1

if __name__=='__main__':raise SystemExit(main())
