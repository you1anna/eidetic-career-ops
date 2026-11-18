"""Reuse interpretation, never old successful reads; failed reads retain their real timestamps."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import research_model as model
import research_sources as sources
from research_codex import validate_output

ROOT=Path(__file__).resolve().parents[1]

def instant(at): return dt.datetime.fromisoformat(at.replace('Z','+00:00'))

def policy():
    paths=['config/profile.yml','config/relevance.yml','config/research.yml',
           'tools/research_cache.py','tools/research_worker.py','tools/research_sources.py','tools/research_codex.py','tools/research_schemas.py','tools/research_context.py',
           'tools/brief_research.py','workspace/evidence_briefs.py']
    return hashlib.sha256(b''.join((ROOT/p).read_bytes() for p in paths)).hexdigest()

def fingerprint(value):
    def stable(v):
        if isinstance(v,dict):return {k:stable(x) for k,x in v.items() if k not in ('checked_at','reviewed_at')}
        if isinstance(v,list):return [stable(x) for x in v]
        return v
    return hashlib.sha256(json.dumps([policy(),stable(value)],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def get(conn,target,kind,key,at):
    row=conn.execute('SELECT * FROM research_cache WHERE target=? AND kind=?',(target,kind)).fetchone()
    if row and row['fingerprint']==key and instant(row['expires_at'])>instant(at):return json.loads(row['value_json'])

def put(conn,target,kind,key,value,at,hours=168):
    raw=json.dumps(value,ensure_ascii=False)
    if len(raw.encode())>200_000:raise ValueError('Research cache packet too large')
    expires=(instant(at)+dt.timedelta(hours=hours)).isoformat()
    conn.execute('INSERT OR REPLACE INTO research_cache VALUES(?,?,?,?,?)',(target,kind,key,raw,expires))

def read_source(conn,url,cache,at=None):
    at=at or sources.stamp()
    if url in cache:return cache[url]
    old=get(conn,url,'blocked',policy(),at)
    if old is not None:cache[url]=old;return old
    source=sources.read(url,cache)
    if not source['ok']:put(conn,url,'blocked',policy(),source,at,hours=24)
    else:conn.execute("DELETE FROM research_cache WHERE target=? AND kind='blocked'",(url,))
    return source

def result(conn,run_id,limits,target,kind,prompt,schema,search=False,purpose=''):
    at=sources.stamp();key=fingerprint(prompt)
    cached=get(conn,target,kind,key,at)
    if cached is not None:
        validate_output(cached['result'],schema)
        limits['_reused']=limits.get('_reused',0)+1
        return cached['result']
    value=model.call(conn,run_id,None,limits,at,prompt,schema,search=search,purpose=purpose)
    put(conn,target,kind,key,dict(result=value,prompt=prompt),at)
    return value

def rebase_revision(conn,target,previous,current):
    """Only after an unchanged-context canonical import; unrelated edits must miss the cache."""
    for row in conn.execute("SELECT * FROM research_cache WHERE target=? AND kind IN ('finding','person')",(target,)).fetchall():
        value=json.loads(row['value_json']);context=value['prompt'].get('job',{})
        if context.get('revision')==previous:
            context['revision']=current
            for contact in value['prompt'].get('contacts',[]):
                if contact.get('id')==target and contact.get('revision')==previous:contact['revision']=current
            conn.execute('UPDATE research_cache SET fingerprint=?,value_json=? WHERE target=? AND kind=?',
                         (fingerprint(value['prompt']),json.dumps(value),target,row['kind']))
