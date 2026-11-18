#!/usr/bin/env python3
"""One-time, zero-model research acceptance report. Reads evidence; never repairs or retries it."""
import argparse
from collections import Counter
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(1, str(Path(__file__).resolve().parents[1] / 'workspace'))
import automation_usage as quota
import record_views  # the one reader of stored caretaker checks

LONDON=ZoneInfo('Europe/London')


def instant(value):
    when=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if when.tzinfo is None:raise ValueError('Timestamp needs a timezone')
    return when


def normalized(value):
    return ' '.join(str(value).casefold().split())


def audit(conn,data,day,now,trigger):
    report=dict(date=day.isoformat(),reviewed_at=now.isoformat(),status='not_observed',model_calls=0,
                issues=[],scope={},coverage={},evidence={},caps={},caretaker='not_observed',
                timer_corroborated=False)
    candidates=[]
    for row in conn.execute("SELECT run_id,receipt_json FROM scan_runs WHERE run_id LIKE 'research:%'"):
        receipt=json.loads(row['receipt_json'])
        started=instant(receipt['started_at']).astimezone(LONDON)
        if started.date()==day and started.hour==9 and started.minute<2:candidates.append((row['run_id'],receipt))
    if len(candidates)!=1:
        report['status']='ambiguous' if candidates else 'not_observed'
        report['issues']=['multiple_matching_runs' if candidates else 'morning_run_missing']
        return report
    run_id,r=candidates[0];started=instant(r['started_at'])
    report['started_at']=r['started_at'];report['finished_at']=r.get('finished_at')
    if r.get('status')=='running' or not r.get('finished_at'):
        report.update(status='incomplete',issues=['morning_run_incomplete']);return report
    finished=instant(r['finished_at'])
    if not started<=finished<=now or finished-started>dt.timedelta(minutes=46):
        report['issues'].append('invalid_run_times')
    try:report['timer_corroborated']=abs((started-instant(trigger)).total_seconds())<=60
    except (ValueError,TypeError,AttributeError):pass
    if not report['timer_corroborated']:report['issues'].append('timer_not_corroborated')
    report['scope']={k:r.get(k) for k in ('focused','people','total','whole_queue','outside_focus')}
    if r.get('focused') is not True or not 0<=r.get('people',99)<=5 or not 0<=r.get('total',99)<=30:
        report['issues'].append('unexpected_scope')
    report['coverage']={k:r.get(k,0) for k in ('checked','verified','unverified','contacts_verified','contacts_total','backlog','current','reused')}
    totals=quota.summary(conn,started.isoformat())
    calls=conn.execute('SELECT COUNT(*),COALESCE(SUM(status!=?),0) FROM codex_invocations WHERE run_id=?',('completed',run_id)).fetchone()
    within=calls[0]<=6 and totals['research_day']<=12 and totals['research_week']<=84 and totals['day']<=16 and totals['week']<=100
    report['caps']=dict(totals,pass_calls=calls[0],unknown_calls=calls[1],within_limits=within)
    if not within:report['issues'].append('invocation_caps')
    if calls[0]!=r.get('usage',{}).get('calls'):report['issues'].append('usage_receipt_mismatch')
    evidence=Counter(canonical_batches=0,fresh_confirmations=0,explicit_closures=0,unverified_checks=0,
                     supported_facts=0,gap_items=0,saved_notes=0)
    for item in r.get('items',[])[:30]:
        if not item.get('batch'):continue
        name=item['batch']
        try:
            if not re.fullmatch(r'[a-f0-9]{32}\.json',name):raise ValueError('Batch name')
            path=Path(data)/'research-server'/name
            if path.is_symlink() or path.stat().st_size>5_000_000:raise ValueError('Batch size')
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=item.get('sha256'):raise ValueError('Batch digest')
            batch=json.loads(raw);finding=batch['finding']
            if finding.get('id')!=item['id']:raise ValueError('Target identity')
        except (OSError,ValueError,KeyError,TypeError):
            report['issues'].append('batch_integrity');continue
        # A retained result and an optimistic import receipt are insufficient without canonical readback.
        rows=conn.execute('SELECT payload_json FROM observations WHERE record_id=? AND source=?',
                          (item['id'],'research:vps:'+name)).fetchall()
        if not any(json.loads(row[0])==finding for row in rows):
            report['issues'].append('canonical_readback_missing');continue
        evidence['canonical_batches']+=1
        sources=batch.get('sources',[])
        for check in finding.get('freshness_checks',[]):
            if check.get('status')=='unverified':evidence['unverified_checks']+=1;continue
            try:
                checked=instant(check['checked_at'])
                fresh=started-dt.timedelta(minutes=5)<=checked<=finished+dt.timedelta(minutes=5) and dt.timedelta(0)<=now-checked<dt.timedelta(hours=24)
                quote=normalized(check['basis'].split(' Evidence: ',1)[1])
                supported=fresh and len(quote)>=18 and any(s.get('ok') and s.get('url')==check.get('source_url')
                    and s.get('checked_at')==check['checked_at'] and quote in normalized(s.get('text','')) for s in sources)
            except (KeyError,ValueError,IndexError,TypeError):supported=False
            if supported and check.get('status') in ('confirmed','closed'):
                evidence['fresh_confirmations' if check['status']=='confirmed' else 'explicit_closures']+=1
            else:report['issues'].append('source_support_unproven')
        for fact in finding.get('facts',[]):
            quote=normalized(fact)
            if len(quote)>=18 and any(s.get('ok') and quote in normalized(s.get('text','')) for s in sources):
                evidence['supported_facts']+=1
            else:report['issues'].append('fact_support_unproven')
        evidence['gap_items']+=len(finding.get('decisive_unknowns',[]))
        note=batch.get('note')
        if note and conn.execute('SELECT 1 FROM drafts WHERE record_id=? AND text=?',(item['id'],note.get('text'))).fetchone():
            evidence['saved_notes']+=1
    report['evidence']=dict(evidence)
    for row in conn.execute("SELECT run_id,receipt_json FROM scan_runs WHERE run_id LIKE 'caretaker:%' ORDER BY run_id"):
        c=json.loads(row[1])
        if c.get('finished_at') and finished<=instant(c['finished_at'])<=now:
            state=next((x.get('status') for x in record_views.caretaker_checks(conn,row[0]) or [] if x.get('id')=='research'),None)
            report['caretaker']=state if state in ('ok','failed','partial','waiting','paused','running') else 'unknown'
            report['caretaker_at']=c['finished_at'];break
    expected='waiting' if r.get('reason')=='usage_limit' else 'paused' if r.get('reason')=='disabled' else {'ok':'ok','partial':'partial','failed':'failed','blocked':'failed'}.get(r.get('status'))
    if report['caretaker']=='not_observed':report['issues'].append('later_caretaker_missing')
    elif report['caretaker']!=expected:report['issues'].append('caretaker_outcome_mismatch')
    if r.get('status') not in ('ok','partial') and r.get('reason') not in ('usage_limit','disabled'):
        report['issues'].append('research_failed')
    useful=evidence['fresh_confirmations']+evidence['explicit_closures']+evidence['supported_facts']
    report['issues']=sorted(set(report['issues']))
    report['status']='needs_attention' if report['issues'] else 'evidence_saved' if useful else 'waiting' if r.get('reason')=='usage_limit' else 'paused' if r.get('reason')=='disabled' else 'no_new_supported_evidence'
    return report


def markdown(report):
    lines=[f"# Monday research acceptance — {report['date']}",'',
           f"Reviewed: {report['reviewed_at']}",f"Outcome: **{report['status']}**",'',
           'Generated on the VPS. This check makes zero model calls, performs no source requests,',
           'does not retry research and changes no career records. Counts are from retained evidence.',
           'A supported-evidence count is not a human judgment that an approach is commercially useful.','']
    for label,key in (('Maintained scope','scope'),('Worker coverage (reported)','coverage'),
                      ('Canonical evidence independently read back','evidence'),('Durable invocation accounting','caps')):
        lines+=['## '+label,'','| Measure | Observed |','| --- | --- |']
        lines += [f'| {k} | {v} |' for k,v in report.get(key,{}).items()]
        lines.append('')
    lines += ['## Execution and remaining limits','',
        f"- Research started: {report.get('started_at','not observed')}; finished: {report.get('finished_at','not observed')}.",
        f"- Calendar trigger corroborated: {report.get('timer_corroborated',False)}.",
        f"- Later caretaker research check: {report.get('caretaker','not observed')} at {report.get('caretaker_at','not observed')}.",
        '- Caps: six/pass; research 12/day and 84/week; aggregate 16/day and 100/week, Europe/London.',
        '- Failed and unknown reservations count. No accounting was reset.',
        '- Fresh confirmation counts require a matching canonical import, retained batch hash, quoted source text and actual read time.',
        '- Current/reused/backlog counters remain worker-reported; current checks are not new source reads.',
        '- Historical note/workflow preservation cannot be reconstructed fully without a before-run snapshot; the worker invariant has automated tests.',
        '- Human usefulness, contact willingness and hiring authority remain unverified.',
        '- This immutable server report is documentation evidence. GitHub-ledger sync remains attended because the VPS GitHub key is read-only.','',
        'Issues: '+(', '.join(report['issues']) or 'none detected')+'.','']
    return '\n'.join(lines)


def folder_for(data,day):return Path(data)/'research-acceptance'/day.isoformat()


def read_report(data,day):
    folder=folder_for(data,day)
    manifest=json.loads((folder/'.done').read_text())
    for name in ('report.json','report.md'):
        path=folder/name
        if path.is_symlink() or path.stat().st_size>100_000:raise ValueError('Invalid report')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get(name):raise ValueError('Report digest mismatch')
    return (folder/'report.md').read_text()


def publish(data,day,report):
    folder=folder_for(data,day);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (folder/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (folder/'.done').exists():read_report(data,day);return
        values={'report.json':json.dumps(report,sort_keys=True,indent=2)+'\n','report.md':markdown(report)}
        hashes={}
        for name,value in values.items():
            raw=value.encode();hashes[name]=hashlib.sha256(raw).hexdigest()
            tmp=folder/(name+'.tmp')
            with tmp.open('wb') as out:
                os.chmod(tmp,0o600);out.write(raw);out.flush();os.fsync(out.fileno())
            os.replace(tmp,folder/name)
        marker=folder/'.done.tmp'
        with marker.open('w') as out:
            os.chmod(marker,0o600);json.dump(hashes,out);out.flush();os.fsync(out.fileno())
        os.replace(marker,folder/'.done')
        fd=os.open(folder,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


def timer_trigger():
    result=subprocess.run(['systemctl','show','wo-research.timer','--property=LastTriggerUSec','--value'],
                          capture_output=True,text=True,timeout=10,env={'PATH':'/usr/bin:/bin','TZ':'UTC','LC_ALL':'C'})
    if result.returncode:return None
    try:return dt.datetime.strptime(result.stdout.strip(),'%a %Y-%m-%d %H:%M:%S %Z').replace(tzinfo=dt.timezone.utc).isoformat()
    except ValueError:return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path);parser.add_argument('--data',required=True,type=Path)
    parser.add_argument('--date',required=True,type=dt.date.fromisoformat)
    args=parser.parse_args()
    now=dt.datetime.now(dt.timezone.utc)
    due=dt.datetime.combine(args.date,dt.time(10,5),LONDON)
    if now<due:parser.error('Acceptance is not due until 10:05 Europe/London on the selected date')
    if (folder_for(args.data,args.date)/'.done').exists():
        read_report(args.data,args.date);print('Acceptance report already complete; no work repeated.');return 0
    conn=sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        report=audit(conn,args.data,args.date,now,timer_trigger())
    finally:conn.close()
    publish(args.data,args.date,report)
    print(json.dumps({'date':report['date'],'status':report['status'],'model_calls':0}))
    return 0


if __name__=='__main__':raise SystemExit(main())
