#!/usr/bin/env python3
"""One bounded daily product report. No source reads or career-record writes."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'workspace')]
import automation_usage as quota
import db
import records
import record_views
import research_model as model
from research_codex import validate_output
from research_schemas import DAILY_REVIEW
from email_worker import save_receipt


def admissions(rows):
    """Capacity admissions identify new automatic jobs; legacy import.new does not."""
    return sorted({rid for row in rows if not row['run_id'].startswith('email-import:')
                   for rid in row['receipt'].get('import',{}).get('capacity',{}).get('admitted',[])})


def packet(state, rows, at):
    by_id={r['id']:r for r in state['records']}
    out={'as_of':at,'policy':state.get('discovery_capacity',{}),
         'automatic_admissions':admissions(rows),'record_count':len(by_id),
         'opportunities':{},'runs':[],
         'limits_of_review':'Stored source evidence only; excluded candidates have not been independently re-researched.'}
    for group in ('discovery','needs_checking','shortlisted','active'):
        out['opportunities'][group]=[]
        for ref in state.get('opportunities',{}).get(group,[])[:10]:
            r=by_id.get(ref['record_id'],{})
            item={k:str(r.get(k,'') or '')[:600] for k in
                  ('id','name','organisation','pay','engagement','location','summary','url')}
            item.update(worth_a_look=ref.get('worth_a_look'),reason=ref.get('reason','')[:400],
                        uncertainty=ref.get('uncertainty','')[:600],availability=r.get('availability',{}))
            out['opportunities'][group].append(item)
    out['working_count']=sum(len(state.get('opportunities',{}).get(k,[]))
                             for k in ('discovery','needs_checking'))
    research=state.get('research_processing',{})
    out['research']={k:research.get(k) for k in ('status','finished_at','checked','verified','unverified',
        'backlog','quiet_passes','last_confirmed','pass_cap','allowance','issues')}
    out['research']['issues']=(research.get('issues') or [])[:10]
    # Receipts are evidence, not independent sources of new-job totals. Collapse empty email polls.
    for row in rows:
        r=row['receipt']; imp=r.get('import') or {}
        if (row['run_id'].startswith('email:') and r.get('status') in ('ok','empty')
                and not r.get('processing',{}).get('jobs') and not r.get('failures')):continue
        out['runs'].append({'id':row['run_id'],'status':r.get('status'),
            'finished_at':r.get('finished_at'),'admitted':imp.get('capacity',{}).get('admitted',[]),
            'deferred':imp.get('capacity',{}).get('deferred',{}),
            'allowance_remaining':imp.get('capacity',{}).get('allowance_remaining'),
            'rejected':imp.get('intake_rejected',{}),'source_caps':r.get('capped',[]),
            'failures':r.get('failures',[]),'seconds':r.get('seconds'),
            'write_lock_seconds':imp.get('write_lock_seconds'),'memory':r.get('memory')})
    # Never silently truncate facts into malformed JSON or send the entire archive.
    if len(json.dumps(out,ensure_ascii=False).encode())>44000:
        raise ValueError('Daily review packet exceeds its capacity')
    return out


def current_packet(conn, now):
    start=(now-dt.timedelta(days=1)).isoformat().replace('+00:00','Z')
    end=now.isoformat().replace('+00:00','Z')
    with record_views.snapshot(conn):
        state=records.state_context(conn)
        rows=[]
        for prefix in ('ingest:','email:'):
            recent = [{'run_id':r['run_id'],'receipt':json.loads(r['receipt_json'])}
                     for r in conn.execute('SELECT run_id,receipt_json FROM scan_runs '
                         'WHERE run_id>=? AND run_id<? ORDER BY run_id LIMIT 101',
                         (prefix+start,prefix+end))]
            if len(recent)>100:raise ValueError('Unexpected daily receipt volume')
            rows += recent
        if len(rows)>102:raise ValueError('Unexpected daily receipt volume')
    result=packet(state,rows,now.isoformat())
    result['interval']={'start':start,'end':end}
    return result


def write_report(directory, day, receipt, text):
    for suffix,body in (('md',text),('json',json.dumps(receipt,indent=2))):
        target=directory/(day+'.'+suffix);temp=target.with_suffix(target.suffix+'.tmp')
        with temp.open('w') as output:
            output.write(body);output.flush();os.fsync(output.fileno())
        temp.replace(target)


def run(conn, directory, now):
    directory=Path(directory)/'daily-review';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    day=now.astimezone(ZoneInfo('Europe/London')).date().isoformat();run_id='daily-review:'+day
    with (directory/'run.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'running','reason':'Another daily review is running'}
        prior=conn.execute('SELECT receipt_json FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()
        if prior:
            receipt=json.loads(prior[0])
            # Holding the run lock proves any persisted running attempt has stopped.
            # Do not repeat an uncertain model call, but make the interrupted state visible.
            if receipt['status']=='running':
                receipt.update(status='failed',reason='interrupted',phase='complete',
                    finished_at=now.isoformat(),report='Daily review was interrupted. No automatic retry was made; inspect its saved receipt.')
                save_receipt(conn,run_id,receipt)
            if 'report' in receipt:
                write_report(directory,day,receipt,receipt['report'])
            return receipt
        receipt={'started_at':now.isoformat(),'status':'running','phase':'prepare','model_calls':0}
        save_receipt(conn,run_id,receipt)
        report='Daily review did not finish. Inspect its saved receipt; no career records were changed.'
        try:
            context=current_packet(conn,now)
            receipt['automatic_admissions']=context.get('automatic_admissions',[])
            # Persist the attempt before dispatch; a timeout/crash cannot cause a free repeated call.
            with quota.serial(conn):
                reservation=quota.reserve(conn,run_id,'research',now.isoformat())
                receipt.update(model_calls=1,phase='model',reservation=reservation)
                save_receipt(conn,run_id,receipt)
                response=model.exchange({'operation':'daily_review','prompt':context})
                quota.finish(conn,reservation,response.get('usage'),False)
                if not response.get('ok'):raise RuntimeError('Report model did not complete')
                validate_output(response['result'],DAILY_REVIEW)
                quota.finish(conn,reservation,response.get('usage'),True)
            report=response['result']['report']
            receipt.update(status='ok',thread_id=response['result']['thread_id'],phase='complete')
        except quota.Limited:
            receipt.update(status='waiting',reason='existing_allowance_or_concurrency_limit',phase='complete')
            report='Daily review deferred: the existing research allowance or model lock is unavailable. No extra call was made.'
        except Exception as error:
            receipt.update(status='failed',reason=type(error).__name__,phase='complete')
        receipt['finished_at']=dt.datetime.now(dt.timezone.utc).isoformat()
        # The durable receipt owns the output; files can be regenerated after interruption.
        receipt['report']=report
        save_receipt(conn,run_id,receipt)
        write_report(directory,day,receipt,report)
        return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path);parser.add_argument('--data',required=True,type=Path)
    args=parser.parse_args()
    os.umask(0o077)
    with db.connect(args.db,timeout=db.WORKER_LOCK_WAIT) as conn:
        receipt=run(conn,args.data,dt.datetime.now(dt.timezone.utc))
    print(json.dumps(receipt))
    return 1 if receipt['status']=='failed' else 0


if __name__=='__main__':sys.exit(main())
