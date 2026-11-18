"""Demand-led task selection. Context comes from the canonical projection, not another queue."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import collection_budget as budget
import collection_policy
from collection_reads import day, instant

MANIFEST='collection-context.json'

def query_key(feed,phrase):
    # Feed names retain source identity; the phrase identifies one rotating first page.
    return 'feed:'+feed['provider']+':'+feed['name']+':'+hashlib.sha256((phrase or '').encode()).hexdigest()[:16]

def context(conn,state,at):
    import allowances
    cap=state['discovery_capacity'];pool=state.get('opportunities',{}).get('research_pool',[])
    full=len(pool)>=cap['research_pool_limit'] or len(allowances.ledger(conn,instant(at)))>=cap['additions_per_seven_days']
    chosen={x['record_id'] for lane in ('shortlisted','active') for x in state.get('opportunities',{}).get(lane,[])}
    priority=set();companies={};sources={};observed={}
    if chosen:
        marks=','.join('?' for _ in chosen)
        # Latest canonical observations only; exact record identities and timestamps survive reuse.
        for r in conn.execute(f'''SELECT record_id,source AS source_scope,observed_at,payload_json AS payload FROM (
            SELECT o.*,ROW_NUMBER() OVER(PARTITION BY record_id,source ORDER BY observed_at DESC,id DESC) position
            FROM observations o WHERE record_id IN ({marks})) WHERE position=1''',tuple(chosen)):
            key=(r['record_id'],r['source_scope'])
            if key in sources:continue
            sources[key]=True
            if r['source_scope'].startswith('board:'):priority.add(r['source_scope'])
            try:
                current=json.loads(r['payload'])
                if not isinstance(current,dict):continue
                current=current.get('current',current)
            except (ValueError,TypeError):continue
            if not isinstance(current,dict):continue
            keys=current.get('query_keys') or []
            if r['source_scope'].startswith('board:'):keys=[*keys,r['source_scope']]
            elif r['source_scope'].startswith(('tender:','award:','news:')):keys=[*keys,':'.join(r['source_scope'].split(':')[:2])]
            elif r['source_scope'].startswith('hn:'):keys=[*keys,'hn:current']
            priority.update(keys)
            for task in keys:observed[task]=min(observed.get(task,r['observed_at']),r['observed_at'])
    chosen_at={}
    if chosen:
        for r in conn.execute(f"SELECT entity_id,MAX(at) at FROM state_events WHERE entity_id IN ({marks}) AND (field='working_state' AND to_value='shortlisted' OR field='status' AND to_value IN ('applied','responded','interview','offer')) GROUP BY entity_id",tuple(chosen)):
            chosen_at[r['entity_id']]=r['at']
    orgs={}
    for link in state.get('links',[]):
        if link.get('relation') in ('hiring_for','advertised_by','buyer') and link['from_id'] in chosen:
            orgs.setdefault(link['from_id'],set()).add(link['to_id'])
    for r in state['records']:
        if r['id'] in chosen:
            targets={r['id']} if r.get('type') in ('company','agency') else orgs.get(r['id'],set())
            for org in targets:companies[org]=max(companies.get(org,''),chosen_at.get(r['id']) or r.get('created_at') or '1970-01-01T00:00:00Z')
    return dict(version=1,generated_at=at,full=full,priority=sorted(priority),companies=companies,observed=observed)

class Session:
    def __init__(self,data,offline=False):
        self.offline=offline;self.data=Path(data);self.reason=None;self.b=None
        self.context={}
        if offline:return
        try:
            self.b=budget.active();self.policy=self.b.policy
            raw=os.environ.get('WO_COLLECTION_CONTEXT')
            if not raw and budget._current.get() is None:raise ValueError('Collection context required')
            if raw:
                self.context=json.loads(Path(raw).read_text())
                if self.context.get('version')!=1 or instant(self.b.now())-instant(self.context['generated_at'])>dt.timedelta(hours=24):
                    raise ValueError('Invalid collection context')
        except (budget.Deferred,OSError,ValueError) as e:self.reason=getattr(e,'reason','invalid-context')
        self.deferred={}
    def choose(self,provider,keys,*,background=False):
        if self.offline:return list(keys)
        if self.reason:return []
        try:
            p=self.policy['providers'][provider]
            history_keys=list(keys)
            if background and provider in ('contracts_finder','find_a_tender'):history_keys+=['tender:'+provider,'award:'+provider]
            history=self.b.history(provider,history_keys);now=instant(self.b.now())
            current=self.b.summary()['providers'][provider]
            if current['used'] is None:return []
            left=max(0,min(p['daily_limit']-current['used'],p['scheduled_target']-current['scheduled_used']))
            if provider=='reed':left=min(left,max(0,p['search_limit']-current['search_used']))
            if background and self.context.get('full') and now.astimezone(__import__('zoneinfo').ZoneInfo('Europe/London')).strftime('%A') not in self.policy['background_days']:
                keys=[k for k in keys if k in self.context.get('priority',[])]
            def due(k):
                row=history.get(k,{})
                at=row.get('last_attempt') or row.get('last_success')
                # Never repeat a failed discovery task in the same calendar day.
                return not at or day(at)!=day(now)
            if background and provider in ('contracts_finder','find_a_tender') and len(keys)==1 and keys[0] not in self.context.get('priority',[]):
                other=('award:' if keys[0].startswith('tender:') else 'tender:')+provider
                if (history.get(other,{}).get('last_attempt') or '') < (history.get(keys[0],{}).get('last_attempt') or '') and due(other):
                    return []
            available=[k for k in keys if due(k)]
            order={k:i for i,k in enumerate(keys)}
            ordered=sorted(available,key=lambda k:(history.get(k,{}).get('last_attempt') or '',order[k]))
            def refresh_due(k):
                actual=max(filter(None,[history.get(k,{}).get('last_success'),self.context.get('observed',{}).get(k)]),default=None)
                return not actual or now-instant(actual)>=dt.timedelta(hours=self.policy['refresh_after_hours'])
            priority=[k for k in ordered if k in self.context.get('priority',[]) and refresh_due(k)]
            discovery=[k for k in ordered if k not in self.context.get('priority',[])]
            reserve=min(2,len(discovery),left)
            selected=priority[:max(0,left-reserve)]
            discovery_limit=p.get('full_discovery_limit',left) if self.context.get('full') else left
            selected+=discovery[:min(discovery_limit,left-len(selected))]
            return selected
        except (budget.Deferred,KeyError,ValueError) as e:
            self.reason=getattr(e,'reason','invalid-policy');return []
    def call(self,provider,key,fn):
        if self.offline:return fn()
        if self.reason:raise budget.Deferred(provider,self.reason)
        with budget.task(key):value=fn()
        if not isinstance(value,dict) or not (value.get('unread') or value.get('_error') or value.get('_stop_reason')):
            self.b.succeeded(provider,key)
        return value
    def receipt(self,provider,reason=None):
        reason=reason or self.reason or 'rotation-or-not-due'
        if self.b:self.b.defer(provider,reason)
        return dict(state='deferred',provider=provider,reason=reason,complete=False)


def dispatch_summary(status):
    """Additive adapter receipt; a deliberate skip is not scanner recovery evidence."""
    parts=[status] if status.get('state') else [part for group in ('boards','feeds','sources') for part in status.get(group,{}).values()]
    deferred={}
    for part in parts:
        if part.get('state')=='deferred':
            key=part.get('provider') or part.get('source') or 'collection'
            deferred[key]=deferred.get(key,0)+1
    return dict(state='deferred',collection_deferrals=deferred) if deferred else {}
