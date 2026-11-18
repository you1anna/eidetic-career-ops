"""The review queue for people found in LinkedIn exports and the owner's list: queueing cards with one
dedupe key, the choices each card offers, and resolving them as accept, merge or reject.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
import csv
import hashlib
import io
import json

import db
import normalise
import profile_rules
from record_core import Conflict, add_observation, bump, event, get, safe_url, text, transaction
from record_mutations import create, edit, link
from record_identity import match_organisation, link_organisation


def person_matches(conn, payload, people=None):
    """Active people a review card may already be: the same profile or email, or the same name. The
    queue view and resolve_review share this, so the workspace never offers a choice it then refuses."""
    name=normalise.person_key(payload.get('name'))
    url=(payload.get('url') or '').strip().rstrip('/')
    email=(payload.get('email') or '').strip().casefold()
    found=[]
    if people is None:
        people = conn.execute("SELECT id,name,url,email FROM records WHERE type='person' AND archived_at IS NULL ORDER BY id")
    for r in people:
        strong=bool(url and (r['url'] or '').rstrip('/')==url) or bool(email and (r['email'] or '').strip().casefold()==email)
        if strong or (name and normalise.person_key(r['name'])==name):
            found.append({'id':r['id'],'name':r['name'],'match':'identifier' if strong else 'name only'})
    return sorted(found,key=lambda m:m['match']!='identifier')


def review_actions(matches):
    """The choices a review card offers. resolve_review accepts each one."""
    if matches:
        first=matches[0]
        actions=[dict(label=f"Merge into {first['name']}",resolution='merge',target_id=first['id'],primary=True)]
        if first['match']!='identifier':
            actions.append(dict(label='Accept as new',resolution='accept',accept_as_new=True))
    else:
        actions=[dict(label='Accept',resolution='accept',primary=True),dict(label='Merge',resolution='merge',pick_target=True)]
    return actions+[dict(label='Reject',resolution='reject')]


def canonical_profile(url):
    u=urlsplit(safe_url(url))
    if u.hostname not in ('linkedin.com','www.linkedin.com') or not u.path.startswith('/in/'):
        raise ValueError('Use a LinkedIn person profile link')
    return urlunsplit(('https','www.linkedin.com',u.path.rstrip('/'),'',''))


def _active_people(conn):
    return list(conn.execute("SELECT id,name,url,email FROM records WHERE type='person' AND archived_at IS NULL"))


def _linkedin_matches(people, name, url, email):
    matches=[]
    for r in people:
        strong=(url and r['url'].rstrip('/')==url) or (email and r['email'].casefold()==email)
        if strong or normalise.person_key(r['name'])==normalise.person_key(name):
            matches.append({'id':r['id'],'name':r['name'],'match':'identifier' if strong else 'name only'})
    return matches


def queue_linkedin_person(conn, row, source='LinkedIn Connections CSV', extra=None, people=None):
    """Queue one LinkedIn person for the owner's review. Returns True when a new card was queued."""
    return linkedin_card(conn, row, source, extra, people)[1]


def linkedin_card(conn, row, source='LinkedIn Connections CSV', extra=None, people=None):
    """The review card for one LinkedIn person: (its id, whether it is new). The Connections CSV upload,
    the full-export import and the owner's list all come through here, so they share one dedupe key: a
    repeated export is idempotent, and changed employment is a new observation for review, never an
    automatic overwrite."""
    name=text(' '.join(filter(None,[row.get('First Name',''),row.get('Last Name','')])),240)
    if not name: raise ValueError('Missing name')
    url=canonical_profile(row.get('URL','').strip()) if row.get('URL','').strip() else ''
    email=text(row.get('Email Address',''),254).lower()
    company=text(row.get('Company',''),240);position=text(row.get('Position',''),240)
    key=url or email or '|'.join((name.casefold(),company.casefold(),position.casefold()))
    key='linkedin:'+hashlib.sha256(json.dumps([key,company,position,email,name,row.get('Connected On','')]).encode()).hexdigest()
    matches=_linkedin_matches(_active_people(conn) if people is None else people, name, url, email)
    payload=dict(name=name,url=url,email=email,observed_company=company,observed_role=position,
                 connected_on=text(row.get('Connected On',''),100),matches=matches,
                 note='Employment is from an export and has not been verified as current.',**(extra or {}))
    cur=conn.execute("INSERT OR IGNORE INTO review_queue(kind,payload_json,source,created_at,dedupe_key) VALUES('linkedin_person',?,?,?,?)",
                     (json.dumps(payload,ensure_ascii=False),source,db.now(),key))
    qid=conn.execute('SELECT id FROM review_queue WHERE dedupe_key=?',(key,)).fetchone()[0]
    return qid,bool(cur.rowcount)


def advance_last_touch(conn, rid, date, note):
    """Move an evidenced-activity date forward from an import, never backwards and never over an edit."""
    # Only interactions mark last_touch as edited, and this only ever moves it forward, so a later
    # evidenced date may pass that mark.
    row=conn.execute('SELECT last_touch FROM records WHERE id=?',(rid,)).fetchone()
    if not date:
        return False
    if row['last_touch'] and normalise.is_factual_date(row['last_touch']) and row['last_touch'][:10]>=date[:10]:
        return False
    conn.execute('UPDATE records SET last_touch=? WHERE id=?',(date[:10],rid))
    event(conn,rid,'last_touch',row['last_touch'],date[:10],note,actor='source');bump(conn,rid)
    return True


def import_linkedin(conn, content):
    if not isinstance(content,str) or len(content.encode())>2_000_000: raise ValueError('CSV limit is 2 MB')
    lines=content.lstrip('\ufeff').splitlines()
    start=next((i for i,l in enumerate(lines) if 'First Name' in l and 'Last Name' in l),None)
    if start is None: raise ValueError('Choose the LinkedIn Connections CSV with First Name and Last Name columns')
    reader=csv.DictReader(io.StringIO('\n'.join(lines[start:])))
    parsed=[]
    for i,row in enumerate(reader,start+2):
        if i-start>10001: raise ValueError('Import at most 10,000 connections at a time')
        parsed.append((i,row))
    people=_active_people(conn)
    queued=0;existing=0;invalid=[]
    with transaction(conn):
        for i,row in parsed:
            try:
                if queue_linkedin_person(conn,row,people=people): queued+=1
                else: existing+=1
            except (ValueError,TypeError) as error:
                invalid.append({'line':i,'reason':str(error)})
    return {'saved':True,'queued':queued,'existing':existing,'invalid':invalid}


def resolve_review(conn, qid, payload, actor='review'):
    """Accept, merge or reject one review card. `actor` is 'review' for the owner's decision on the site, or
    'agent' when an agent applies his list (tools/network_list.py), which is not contact activity."""
    resolution=payload.get('resolution')
    if resolution not in ('accept','merge','reject'): raise ValueError('Choose accept, merge or reject')
    with transaction(conn):
        q=conn.execute('SELECT * FROM review_queue WHERE id=?',(qid,)).fetchone()
        if q is None: raise ValueError('Unknown review item')
        if q['resolved_at'] or q['revision']!=payload.get('revision'): raise Conflict('This review item changed. Reload it.')
        p=json.loads(q['payload_json']);record=None
        if resolution!='reject':
            company=p.get('observed_company','');role=p.get('observed_role','')
            context='Imported LinkedIn observation: '+', '.join(filter(None,[company,role]))+'. Current employment unverified.' if q['kind']=='linkedin_person' else p.get('contact_route','')
            rules=profile_rules.current()
            if rules.flag and rules.flag in p.get('flags',[]): context=(context+' '+rules.warning).strip()
            if resolution=='merge':
                record=get(conn,payload.get('target_id'))
                if record['type']!='person' or record['archived_at']: raise ValueError('Merge into an existing person')
                changes={k:p[k] for k in ('email','url') if p.get(k) and not record[k]}
                changes['known_context']='\n'.join(filter(None,[record['known_context'],context]))
                record=edit(conn,record['id'],dict(revision=record['revision'],**changes),actor=actor)['record']
            else:
                # Recheck live: another card may have been accepted since this one was shown.
                matches=person_matches(conn,p)
                if matches and matches[0]['match']=='identifier':
                    raise ValueError(f"{matches[0]['name']} ({matches[0]['id']}) already has this profile or email. Merge into them instead.")
                if matches and not payload.get('accept_as_new'):
                    raise ValueError(f"{matches[0]['name']} is already a person here ({matches[0]['id']}). Merge into them, or choose Accept as new if this is someone else.")
                record=create(conn,dict(type='person',name=p['name'],url=p.get('url',''),email=p.get('email',''),known_context=context),actor=actor)['record']
            rid=record['id']
            add_observation(conn,rid,q['source'],q['created_at'],q['payload_json'])
            if p.get('agency_id'):
                link(conn,dict(from_id=rid,to_id=p['agency_id'],relation='consultant_at',revision=record['revision']),actor=actor)
                record=get(conn,rid)
            org=p.get('org_match') or {}
            if not org.get('id') and company and not normalise.placeholder_employer(company):
                org=match_organisation(conn,company) or {}
            if org.get('id') and conn.execute('SELECT 1 FROM records WHERE id=? AND archived_at IS NULL',(org['id'],)).fetchone():
                rel=org.get('relation') or ('consultant_at' if org.get('type')=='agency' else 'works_at')
                link(conn,dict(from_id=rid,to_id=org['id'],relation=rel,revision=record['revision'],
                               notes=org.get('note','LinkedIn export; unverified')),actor=actor)
                record=get(conn,rid)
            elif company and payload.get('ensure_organisation') and not normalise.placeholder_employer(company):
                org_id=link_organisation(conn,rid,company,'works_at',q['source'],q['created_at'])
                if org_id:
                    conn.execute("UPDATE links SET notes='LinkedIn export; unverified' WHERE from_id=? AND to_id=? AND relation IN ('works_at','consultant_at') AND (notes IS NULL OR notes='')",
                                 (rid,org_id))
                    record=get(conn,rid)
            evidence=p.get('evidence') or {}
            if evidence.get('last_sent'):
                advance_last_touch(conn,rid,evidence['last_sent'],'Last LinkedIn conversation in which the owner sent a message')
                record=get(conn,rid)
        conn.execute('UPDATE review_queue SET resolved_at=?,resolution=?,revision=revision+1 WHERE id=?',
                     (db.now(),resolution+(':'+record['id'] if record else ''),qid))
    return {'saved':True,'record':record}
