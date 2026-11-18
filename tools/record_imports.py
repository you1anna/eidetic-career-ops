"""Attended source ingestion: shared normalization and canonical record mutations.

The CLI and scheduled ingest call these services; network reads and argument parsing stay
outside this module. Import identity, provenance and reports are handled in one place.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'workspace'))
import row_shape as ed
import db as wodb
import records as store
import normalise
import email_sources

# Compatibility exports: canonical identity and writes are shared with manual record changes.
from record_identity import (TEXT_COLUMNS, STATE_COLUMNS, IMPORT_INSERT_ONLY, upsert_record,
                             ensure_organisation, link_organisation)


def selected_candidate(item, candidate_urls, candidates):
    """Existing discovery selection, shared by admission preparation and canonical import."""
    current = item.get('current') or {}
    identity = (item.get('source_scope', ''), str(item.get('source_id', '')))
    return (((identity in candidates or current.get('url') in candidate_urls)
             and current.get('eligible') is not False) or current.get('eligible') is True)


def intake_decisions(items, terms=None, *, candidate_urls=None, candidates=()):
    """Compute source eligibility before the canonical write transaction."""
    import yaml
    import search_config
    from job_rules import intake_rejection
    portals = yaml.safe_load((ROOT / 'config/portals.yml').read_text())
    relevance = yaml.safe_load((ROOT / 'config/relevance.yml').read_text())
    decisions = {}
    for key, item in items.items():
        if (item.get('kind') != 'job' or item.get('compacted_at')
                or (candidate_urls is not None and not selected_candidate(item, candidate_urls, candidates))):
            continue
        current = {**(item.get('current') or {}), **(terms or {}).get(key, {})}
        reason = intake_rejection(current, portals, relevance['pay'], today=dt.datetime.now(dt.timezone.utc).date())
        row = dict(current, id='candidate', type='job', name=current.get('title', ''),
                   organisation=current.get('company', ''), source_file='source:' + item.get('source_scope', ''))
        if search_config.exclusions([row], config=relevance):
            reason = 'excluded'
        if item.get('source_lifecycle') == 'closed':
            reason = 'source-closed'
        decisions[key] = reason
    return decisions


# --- state derivation -------------------------------------------------------------------------

def derive(r: dict, scan: dict | None) -> dict:
    """Declared state for one scan row. NULL wherever the source does not say it."""
    out = {k: None for k in STATE_COLUMNS}
    out["status_raw"] = r["status"]
    if r["type"] == "job":
        out["status"] = "candidate" if r["id"].startswith("SCAN-") else None
        out["triage_reason"] = (scan or {}).get("why") or None
    return out


# --- writes -----------------------------------------------------------------------------------

def scan_inventory(folder: Path) -> list[tuple[Path, list[dict]]]:
    """One parse per dated TSV for this import; every run still replays all files."""
    return [(path, ed.scan_tsv(path)) for path in sorted(folder.glob('????-??-??.tsv'))]


def write_scans(conn: sqlite3.Connection, folder: Path, at: str, inventory=None) -> dict:
    """Daily-scan TSVs: one scan_runs receipt per file, keeping the raw advert columns."""
    receipt = folder / "last-run.json"
    last = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
    inventory = scan_inventory(folder) if inventory is None else inventory
    for path, rows in inventory:
        status = ""
        if last and last.get("date") == path.stem:
            status = "partial" if any(b.get("state") != "ok" for b in last.get("boards", {}).values()) else "complete"
        conn.execute("INSERT INTO scan_runs (run_id, date, receipt_json, status) VALUES (?,?,?,?) "
                     "ON CONFLICT(run_id) DO UPDATE SET date=excluded.date, receipt_json=excluded.receipt_json, "
                     "status=excluded.status",
                     (path.stem, path.stem, json.dumps({"file": path.name, "rows": rows}, ensure_ascii=False), status))
    if last is not None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('scan_status', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(last, ensure_ascii=False),))
    return {"files": len(inventory), "receipt": bool(last), "at": at}


# --- commands ---------------------------------------------------------------------------------

def _import_scans(conn: sqlite3.Connection, folder: Path, observed_urls=(), *, inventory=None, resolver=None, admission=None, capacity=None) -> dict:
    at = wodb.now()
    posix = folder.resolve().as_posix()
    prefix = f"{ed.BASE}/data/pending" if f"{ed.BASE}/data/pending" in posix else folder.name
    inventory = scan_inventory(folder) if inventory is None else inventory
    rows = ed.scan_rows(folder, prefix, inventory)
    # Modern observations own identity, facts and source timestamps. TSVs select candidates and
    # remain the fallback only for legacy rows without an observation.
    deferred = sum(r.get('url') in observed_urls for r in rows)
    rows = [r for r in rows if r.get('url') not in observed_urls]
    raw = {ed.scan_id(j["url"]): j for _, source_rows in inventory for j in source_rows if j.get("url")}
    rejected = {}
    new = owned = capacity_deferred = 0
    archived_organisations = set()
    url_index, url_conflicts = store.job_url_index(conn), []
    for r in rows:
        # One advert URL is one record: a URL already held by another active job is that job.
        try:
            holder = store.match_job_url(conn, r['url'], url_index) if r.get('url') else None
            if r.get('url'):
                r['url'] = normalise.job_url(r['url'])
        except (ValueError, store.Conflict) as error:
            url_conflicts.append(dict(record_id=r['id'], reason=str(error)))
            continue
        if holder and holder != r['id']:
            owned += 1
            continue
        reason = (admission or {}).get(r['id'], '')
        if reason and not conn.execute('SELECT 1 FROM records WHERE id=?', (r['id'],)).fetchone():
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        is_new = not conn.execute('SELECT 1 FROM records WHERE id=?', (r['id'],)).fetchone()
        if is_new and capacity is not None and not capacity.permit('legacy:' + r['id']):
            capacity_deferred += 1
            continue
        state = derive(r, raw.get(r["id"]))
        state["fields_json"] = json.dumps({"scan": raw[r["id"]]}, ensure_ascii=False) if r["id"] in raw else None
        created = upsert_record(conn, r, state, at)
        new += created
        if created and capacity is not None:
            capacity.added(r['id'])
        if r.get('url'):
            url_index.setdefault(r['url'], set()).add(r['id'])
        if r.get("organisation"):
            if not link_organisation(conn, r["id"], r["organisation"], "hiring_for", "scan", at, resolver=resolver):
                archived_organisations.add(r['organisation'])
    scan = write_scans(conn, folder, at, inventory)
    report = {"at": at, "command": "import-scans", "source": str(folder), "accepted": {"job": len(rows) - sum(rejected.values()) - capacity_deferred}, "total": len(rows),
              "new": new, "existing": len(rows) - new - owned - sum(rejected.values()) - capacity_deferred, "intake_rejected": rejected, "url_already_recorded": owned, "scan_files": scan["files"],
              "observation_candidates": deferred,
              "archived_organisations": sorted(archived_organisations),
              "url_conflicts": url_conflicts,
              "malformed": [], "duplicate_ids": [], "unresolved_links": []}
    conn.execute("INSERT INTO import_runs (at, command, source, report_json) VALUES (?,?,?,?)",
                 (at, "import-scans", str(folder), json.dumps(report, ensure_ascii=False)))
    return report


def observation_time(value):
    return normalise.observation_time(value)


# Sources that only report presence: a search window or an alert never proves an advert has gone.
PRESENCE_ONLY = ('feed:', 'alert:', 'news:', 'award:')
RECENT_SIGHTING = dt.timedelta(days=7)


def record_lifecycle(sightings, now):
    """One record's lifecycle across every source that has seen it: open while any source shows it
    open. A presence-only sighting counts only while recent, so it can neither close a role nor keep
    one open forever."""
    states = []
    for it in sightings:
        state = it.get('source_lifecycle') or 'unknown'
        if (it.get('source_scope') or '').startswith(PRESENCE_ONLY) and state == 'active' and \
                now - observation_time(it.get('last_seen_at')) > RECENT_SIGHTING:
            state = 'unknown'
        states.append(state)
    return next((s for s in ('active', 'absent', 'closed') if s in states), 'unknown')


def lifecycle_time(item):
    # An absence/closure is observed after the advert was last seen. A failed receipt
    # is not fresh evidence about that advert and must not advance this clock.
    return max([item.get('last_seen_at') or '', item.get('closed_at') or '',
                *(item.get('absence_observations') or [])], key=observation_time)


def advert_terms(data):
    """Source-stated terms of each board/feed advert still holding its text. Pure and slow (regexes
over every advert), so callers compute it before taking the write lock."""
    import yaml
    from job_rules import observed_terms
    engagement_filter=(yaml.safe_load((ROOT/'config/portals.yml').read_text()) or {}).get('engagement_filter',{})
    return {key: observed_terms(item.get('current',{}), engagement_filter)
            for key, item in data.get('items',{}).items()
            if not item.get('compacted_at') and str(item.get('source_scope','')).startswith(('board:','feed:'))}


def record_source_reads(conn, sources, stale_sources):
    """Each source's latest read into source_health; an older read never replaces a newer one."""
    for scope, source in sources.items():
        at = source.get('observed_at','')
        success = at if source.get('complete') and source.get('state') in ('ok','empty') else None
        previous = conn.execute('SELECT * FROM source_health WHERE scope=?',(scope,)).fetchone()
        if previous:
            success = max([previous['last_success'],success],key=observation_time)
            if observation_time(at) <= observation_time(previous['observed_at']):
                if observation_time(at) < observation_time(previous['observed_at']):
                    stale_sources.append({'scope':scope,'observation':source})
                conn.execute('UPDATE source_health SET last_success=? WHERE scope=?',(success,scope))
                continue
        conn.execute('INSERT INTO source_health(scope,provider,state,observed_at,last_success,item_count,detail) VALUES(?,?,?,?,?,?,?) '
                     'ON CONFLICT(scope) DO UPDATE SET provider=excluded.provider,state=excluded.state,observed_at=excluded.observed_at,'
                     'last_success=COALESCE(excluded.last_success,source_health.last_success),item_count=excluded.item_count,detail=excluded.detail',
                     (scope,source.get('provider',''),source.get('state','unknown'),at,success,len(source.get('item_ids',[])),json.dumps(source.get('detail',{}))))


def observation_facts(current, kind, url):
    """Canonical facts shared by the admission preview and the actual import."""
    from web_read import strip_html
    fact=dict(name=current.get('title',''),organisation=current.get('buyer') or current.get('company',''),url=url,
              summary=strip_html(current.get('summary') or current.get('text',''))[:600],location=current.get('location',''),
              date=current.get('published') or current.get('posted',''),engagement=current.get('engagement') or current.get('employment_type',''),
              published_deadline=current.get('deadline',''),pay='' if current.get('rate')=='unstated' else current.get('rate',''),
              contact=current.get('contact_name',''),email=current.get('contact_email',''))
    fact.update({k:str(current.get(k,'') or '') for k in ('pay_min','pay_max','pay_currency','pay_period','ir35','workplace')})
    if kind=='tender':
        fact['pay']=' '.join(str(current.get(k,'') or '') for k in ('value_currency','value_amount')).strip()
        fact['engagement']='procurement';fact['subtype']=current.get('notice_type') or current.get('status','')
    fact=normalise.normalise_values({k:(' '.join(v.split()) if isinstance(v,str) else v) for k,v in fact.items()})
    fact['summary']=fact['summary'][:600]
    return fact


def prepare_observations(folder, data):
    """Read legacy candidates before the write lock, preserving the source fallback."""
    import csv
    candidates = {}
    malformed = []
    for sub, key, kind in [('tenders','release_id','tender'),('hn','comment_id','job')]:
        for source in sorted((folder/sub).glob('????-??-??.tsv')):
            with source.open(newline='',encoding='utf-8') as stream:
                for line, row in enumerate(csv.DictReader(stream,delimiter='\t'),2):
                    sid = row.get(key) or row.get('ocid')
                    if None in row or not sid:
                        malformed.append({'file':str(source),'line':line,'reason':'Missing source identifier or malformed TSV'})
                        continue
                    scope = 'tender:'+row.get('source','') if kind=='tender' else 'hn:who-is-hiring:'+row.get('thread_id','')
                    candidates[(scope,sid)] = row
    items = dict(data.get('items',{}))
    present = {(x['source_scope'],x['source_id']) for x in items.values()}
    for (scope,sid), row in candidates.items():
        if (scope,sid) not in present:
            items[scope+'\x1f'+sid] = dict(source_scope=scope,source_id=sid,kind='tender' if scope.startswith('tender:') else 'job',
                current=row,last_seen_at=row.get('date_seen',''),source_lifecycle='unknown')
    return items, candidates, malformed


def _advance_observation_clock(clocks, item):
    """Keep one latest-time email identity for sequence ties; competing sources remain unordered."""
    at = observation_time(item.get('last_seen_at'))
    sequence = (item.get('current') or {}).get('receipt_sequence', 0)
    identity = ((item['source_scope'], str(item.get('source_id', '')), sequence)
                if email_sources.is_alert(item.get('source_scope')) and type(sequence) is int and sequence >= 0 else None)
    previous = clocks[2]
    if at > clocks[0]:
        previous = identity
    elif at == clocks[0]:
        previous = (max(previous, identity, key=lambda value: value[2])
                    if previous and identity and previous[:2] == identity[:2] else None)
    return [max(clocks[0], at), max(clocks[1], observation_time(lifecycle_time(item))), previous]


def _observation_watermarks(conn):
    """Read chronology once, preserving archive order and fail-closed payload parsing."""
    watermarks = {}
    for saved in conn.execute('SELECT record_id,payload_json FROM observations ORDER BY id'):
        payload = json.loads(saved['payload_json'])
        if not isinstance(payload, dict) or not payload.get('source_scope') or payload.get('kind') not in ('job', 'tender'):
            continue
        clocks = watermarks.get(saved['record_id'], [observation_time(None), observation_time(None), None])
        watermarks[saved['record_id']] = _advance_observation_clock(clocks, payload)
    return watermarks


def import_observations(conn, folder, data=None, candidate_urls=(), *, resolver=None, terms=None, admission=None, prepared=None, capacity=None, watermarks=None):
    """Connect current public observations to candidates without changing human decisions."""
    import hashlib
    import yaml
    from job_rules import observed_terms
    engagement_filter=(yaml.safe_load((ROOT/'config/portals.yml').read_text()) or {}).get('engagement_filter',{})
    path = folder / 'observations.json'
    if data is None:
        from observation_reader import load
        data = load(path)
    items, candidates, malformed = prepared if prepared is not None else prepare_observations(folder, data)
    admission = intake_decisions(items, terms, candidate_urls=candidate_urls, candidates=candidates) if admission is None else admission
    counts = {'intake_rejected':{}, 'observed':0,'new_observed':0,'filtered_observations':0,'stale_observations':0,
              'stale_sources':[],'observation_errors':malformed,'conflicts':[], 'archived_organisations':[]}
    record_source_reads(conn, data.get('sources',{}), counts['stale_sources'])
    # One role seen by several sources is one record. The match is exact (organisation and title
    # keys, active jobs only); ambiguous or same-source matches are flagged, never merged.
    now = observation_time(data.get('generated_at'))
    roles, sightings_of, record_facts = {}, {}, {}
    url_index = store.job_url_index(conn)
    for r in conn.execute("SELECT id,name,organisation,location,engagement FROM records WHERE type='job' AND archived_at IS NULL"):
        roles.setdefault((normalise.org_key(r['organisation']), normalise.org_key(r['name'])), []).append(r['id'])
        record_facts[r['id']] = dict(r)
    for r in conn.execute('SELECT source_scope,source_id,record_id FROM source_records'):
        sightings_of.setdefault(r['record_id'], set()).add((r['source_scope'], r['source_id']))
    by_identity = {(x.get('source_scope',''),str(x.get('source_id',''))): x for x in items.values()}
    counts.update(attached_sightings=0, possible_matches=0, signals=0, signal_organisations_created=0,
                  commercial_signals=0, commercial_organisations_created=0, registry=0, registry_skipped=0)
    if watermarks is None:
        watermarks = _observation_watermarks(conn)
    for key, item in items.items():
        if item.get('compacted_at'):
            continue  # its full observation is already in the database (compact_observations)
        scope,sid = item.get('source_scope',''),str(item.get('source_id',''))
        current=item.get('current',{});kind=item.get('kind')
        if scope.startswith(('board:','feed:')):
            stated = terms[key] if terms is not None and key in terms else observed_terms(current,engagement_filter)
            current={**current,**stated}
        if kind == 'signal' and scope and sid:
            import_signal(conn, item, counts, resolver=resolver); continue
        if kind == 'commercial_signal' and scope and sid:
            import_commercial_signal(conn, item, counts, resolver=resolver); continue
        if kind == 'registry' and scope and sid:
            import_registry(conn, item, counts); continue
        if not scope or not sid or kind not in ('job','tender'):
            counts['observation_errors'].append({'file':str(path),'line':0,'reason':'Unsupported observation identity or kind'});continue
        at=item.get('last_seen_at') or ''
        if observation_time(at) == observation_time(None):
            counts['observation_errors'].append({'file':str(path),'line':0,'reason':f'{scope}/{sid}: missing or invalid observation time'});continue
        mapped=conn.execute('SELECT record_id FROM source_records WHERE source_scope=? AND source_id=?',(scope,sid)).fetchone()
        url=current.get('url','')
        try:
            if kind == 'job':
                # URL identity precedes the weaker employer/title fallback and includes manual saves.
                existing = store.match_job_url(conn, url, url_index) if url else None
                rid = store.active_job_survivor(conn, mapped[0]) if mapped else existing
                if mapped and existing and rid != existing:
                    raise store.Conflict('The source identity and canonical job URL point to different records')
                if url:
                    url = normalise.job_url(url)
            else:
                existing = conn.execute("SELECT id FROM records WHERE type=? AND url=? AND archived_at IS NULL", (kind, url)).fetchone() if url else None
                rid = mapped[0] if mapped else existing[0] if existing else None
        except (ValueError, store.Conflict) as error:
            counts['conflicts'].append(dict(source=scope, source_id=sid, reason=str(error)))
            continue
        role=(normalise.org_key(current.get('company') or current.get('buyer','')),normalise.org_key(current.get('title','')))
        possible=[]
        if rid is None and kind=='job' and all(role):
            same=roles.get(role,[])
            elsewhere=[x for x in same if all(sc!=scope for sc,_ in sightings_of.get(x,()))]
            if len(same)==1 and elsewhere and normalise.compatible_adverts(record_facts[same[0]], current):
                rid=same[0];counts['attached_sightings']+=1
            else:
                possible=same[:3]
        selected = selected_candidate(item, candidate_urls, candidates)
        reason = admission.get(key, '') if kind == 'job' else ''
        if rid is None and selected and reason:
            counts['intake_rejected'][reason] = counts['intake_rejected'].get(reason, 0) + 1
            counts['filtered_observations'] += 1
            continue
        if rid is None and not selected:
            counts['filtered_observations']+=1;continue
        if not rid:
            rid=('TN-' if kind=='tender' else 'HN-')+hashlib.sha256((scope+'|'+sid).encode()).hexdigest()[:16]
        fact = observation_facts(current, kind, url)
        old=conn.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone()
        if old is None:
            if not fact['name']:
                counts['observation_errors'].append({'file':str(path),'line':0,'reason':f'{scope}/{sid}: missing title'});continue
            if capacity is not None and not capacity.permit(key):
                counts['filtered_observations'] += 1
                continue
            row=ed.row(id=rid,type=kind,source_file='source:'+scope,**{k:v for k,v in fact.items() if k in TEXT_COLUMNS})
            upsert_record(conn,row,{'status':'candidate'} if kind=='job' else {},at)
            counts['new_observed']+=1
            if capacity is not None:
                capacity.added(rid)
            for other in possible:
                conn.execute("INSERT OR IGNORE INTO links(from_id,to_id,relation) VALUES(?,?,'possible_match')",(rid,other))
                counts['possible_matches']+=1
            fact={k:(' '.join(v.split()) if isinstance(v,str) else v) for k,v in fact.items()}
        fact['summary']=fact['summary'][:600]
        old=conn.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone()
        # Receipt failure may preserve a previous item snapshot; never treat import time as activity.
        protected={r[0] for r in conn.execute('SELECT field FROM record_edits WHERE record_id=?',(rid,))}
        clocks = watermarks.get(rid, [observation_time(None), observation_time(None), None])
        sequence = current.get('receipt_sequence', 0)
        newer_email = (observation_time(at) == clocks[0] and clocks[2] is not None
                       and clocks[2][:2] == (scope, sid) and type(sequence) is int and sequence > clocks[2][2])
        fresh_facts = observation_time(at) > clocks[0] or newer_email
        lifecycle_at = lifecycle_time(item)
        fresh_lifecycle = observation_time(lifecycle_at) > clocks[1]
        if observation_time(at) < clocks[0] or observation_time(lifecycle_at) < clocks[1]:
            counts['stale_observations'] += 1
        changes={k:v for k,v in fact.items() if fresh_facts and old[k]!=v and k not in protected}
        conflicts={k:v for k,v in fact.items() if fresh_facts and old[k]!=v and k in protected}
        # Only the source that created the record, or one reporting its exact advert URL, replaces its
        # facts. A sighting matched by role adds evidence and fills gaps.
        if old['source_file']!='source:'+scope and not (old['url'] and old['url']==url):
            changes={k:v for k,v in changes.items() if not old[k]};conflicts={}
        sightings_of.setdefault(rid,set()).add((scope,sid))
        lifecycle=record_lifecycle([by_identity[x] for x in sightings_of[rid] if x in by_identity],now)
        if lifecycle not in ('unknown','active','absent','closed'): lifecycle='unknown'
        if fresh_lifecycle and old['source_lifecycle']!=lifecycle: changes['source_lifecycle']=lifecycle
        if changes:
            event_at = max([at,lifecycle_at] if 'source_lifecycle' in changes else [at],key=observation_time)
            updated_at = max([old['updated_at'],event_at],key=observation_time)
            conn.execute('UPDATE records SET '+','.join(k+'=?' for k in changes)+',revision=revision+1,updated_at=? WHERE id=?',[*changes.values(),updated_at,rid])
            store.event(conn, rid, 'source_observation', None, lifecycle, ', '.join(changes), actor='source',
                        source_scope=scope, changes={k: {'before': old[k], 'after': v} for k,v in changes.items()}, observed_at=event_at)
        if conflicts: counts['conflicts'].append({'record_id':rid,'source':scope,'fields':conflicts})
        accepted = {**dict(old), **changes}
        if kind == 'job' and not accepted['archived_at']:
            previous_fact = record_facts.get(rid)
            previous_role = ((normalise.org_key(previous_fact['organisation']), normalise.org_key(previous_fact['name']))
                             if previous_fact else None)
            accepted_role = (normalise.org_key(accepted['organisation']), normalise.org_key(accepted['name']))
            if previous_role != accepted_role and previous_role in roles:
                roles[previous_role] = [x for x in roles[previous_role] if x != rid]
            if rid not in roles.setdefault(accepted_role, []):
                roles[accepted_role].append(rid)
        record_facts[rid] = accepted
        if kind == 'job' and url:
            url_index.setdefault(url, set()).add(rid)
        store.add_observation(conn,rid,scope,at,json.dumps(item,sort_keys=True))
        conn.execute('INSERT OR IGNORE INTO source_records VALUES(?,?,?)',(scope,sid,rid))
        watermarks[rid] = _advance_observation_clock(clocks, item)
        buyer=fact['organisation']
        if buyer and fresh_facts:
            if not link_organisation(conn,rid,buyer,'buyer' if kind=='tender' else 'hiring_for',scope,at, resolver=resolver):
                counts['archived_organisations'].append(buyer)
        counts['observed']+=1
    counts['archived_organisations'] = sorted(set(counts['archived_organisations']))
    return counts


def import_signal(conn, item, counts, *, resolver=None):
    """A funding story is a fact about an organisation. It is stored as that organisation's observation,
    dated by publication, never as a record of its own and never as the organisation's description."""
    current, scope, sid = item.get('current') or {}, item['source_scope'], str(item['source_id'])
    if current.get('eligible') is not True or not current.get('company'):
        counts['filtered_observations'] += 1
        return
    published = current.get('published') or item.get('last_seen_at', '')
    mapped = conn.execute("SELECT s.record_id FROM source_records s JOIN records r ON r.id=s.record_id "
                          "WHERE s.source_scope=? AND s.source_id=? AND r.archived_at IS NULL", (scope, sid)).fetchone()
    oid, created = (mapped[0], False) if mapped else ensure_organisation(conn, current['company'], scope, item.get('last_seen_at') or published, resolver=resolver)
    if not oid:
        counts['archived_organisations'].append(current['company'])
        return
    story = {k: current.get(k, '') for k in ('feed', 'source', 'company', 'amount', 'currency', 'amount_millions', 'published', 'region', 'url', 'headline')}
    store.add_observation(conn, oid, scope, published, json.dumps(story, sort_keys=True))
    conn.execute('INSERT OR IGNORE INTO source_records VALUES(?,?,?)', (scope, sid, oid))
    counts['signals'] += 1
    counts['signal_organisations_created'] += int(created)


def import_commercial_signal(conn, item, counts, *, resolver=None):
    """An awarded supplier engagement is source evidence, never a vacancy or a funding story.

    Only an eligible new signal can discover a supplier. Once mapped, later corrections and
    ineligible/expired facts still replace its evidence so an earlier eligible claim cannot linger.
    The raw envelope retains publication, award and observation dates separately.
    """
    current, scope, sid = item.get('current') or {}, item['source_scope'], str(item['source_id'])
    at = item.get('last_observation_at') or item.get('last_seen_at') or ''
    if not scope.startswith('award:') or current.get('signal_type') != 'contract_award' or not current.get('company'):
        counts['filtered_observations'] += 1
        return
    if observation_time(at) == observation_time(None):
        counts['observation_errors'].append({'file': 'observations.json', 'line': 0,
                                            'reason': f'{scope}/{sid}: missing or invalid observation time'})
        return
    # Native identity precedes display names. An archived supplier remains excluded even when the
    # source renames it. Normal merges move source_records to the active survivor atomically.
    mapped = conn.execute("SELECT s.record_id,r.archived_at,r.type FROM source_records s JOIN records r ON r.id=s.record_id "
                          "WHERE s.source_scope=? AND s.source_id=?", (scope, sid)).fetchone()
    if mapped and mapped['archived_at']:
        counts['archived_organisations'].append(current['company'])
        return
    if mapped and mapped['type'] not in ('company', 'agency'):
        counts['filtered_observations'] += 1
        return
    if not mapped and current.get('eligible') is not True:
        counts['filtered_observations'] += 1
        return
    oid, created = (mapped[0], False) if mapped else ensure_organisation(conn, current['company'], scope, at, resolver=resolver)
    if not oid:
        counts['archived_organisations'].append(current['company'])
        return
    store.add_observation(conn, oid, scope, at, json.dumps(item, sort_keys=True, ensure_ascii=False))
    conn.execute('INSERT OR IGNORE INTO source_records VALUES(?,?,?)', (scope, sid, oid))
    counts['commercial_signals'] += 1
    counts['commercial_organisations_created'] += int(created)


def import_registry(conn, item, counts):
    """Companies House facts for a company number recorded on evidence (`wo company-number`): one
    observation on that record, added again only when the facts change. Facts for a number no record
    holds are skipped; nothing is ever matched by name, created, or turned into a person."""
    current, scope, sid = item.get('current') or {}, item['source_scope'], str(item['source_id'])
    mapped = conn.execute("SELECT s.record_id FROM source_records s JOIN records r ON r.id=s.record_id "
                          "WHERE s.source_scope=? AND s.source_id=? AND r.archived_at IS NULL", (scope, sid)).fetchone()
    if not mapped:
        counts['registry_skipped'] += 1
        return
    facts = {k: current[k] for k in ('company_name', 'company_number', 'company_status', 'incorporated_on', 'sic_codes',
                                     'officers', 'allotments', 'url') if k in current}
    store.add_observation(conn, mapped[0], scope, item.get('last_seen_at', ''), json.dumps(facts, sort_keys=True))
    counts['registry'] += 1


def import_research(conn, path):
    """Attach reviewed evidence without replacing human edits or inventing contact records."""
    return import_research_entries(conn, json.loads(path.read_text()), 'research:' + path.name)


def import_research_entries(conn, entries, source, *, append_evidence=False):
    """Shared in-memory intake. The caller owns source identity; input objects remain unchanged."""
    import copy
    import hashlib
    import research_intake
    entries = copy.deepcopy(entries)
    research_intake.validate(entries)
    report={'accepted':0,'created':0,'unknown_ids':[],'conflicts':[],'queued':0,'stale':0,
            'archived_organisations':[], 'archived_ids':[], 'items':[]}
    with store.transaction(conn):
        for index, r in enumerate(entries):
            item = dict(index=index, finding_id=r.get('finding_id'), record_id=r.get('id'),
                        outcome='unknown_id', task='none', conflicts=[])
            report['items'].append(item)
            created = False
            new=r.get('create')
            if new and not r.get('id'):
                if new.get('type') == 'job':
                    resolved, created = research_intake.resolve_job(conn, new, source, r['reviewed_at'],
                                                                   upsert_record, link_organisation)
                    item.update(resolved)
                    if resolved.get('archived_organisation'):
                        report['archived_organisations'].append(resolved['archived_organisation'])
                    if resolved.get('outcome'):
                        if resolved['outcome'] == 'archived': report['archived_ids'].extend(resolved['archived_ids'])
                        continue
                    r['id'] = resolved['record_id']
                else:
                    facts={k:new[k] for k in ('url','subtype','location') if new.get(k)}
                    r['id'],created=ensure_organisation(conn,new['name'],source,r['reviewed_at'],new.get('type','company'),**facts)
                report['created']+=int(created)
                item['record_id'] = r['id']
                if r['id'] is None:
                    report['archived_organisations'].append(new['name'])
                    item['outcome'] = 'archived'
                    continue
            rid=r['id'];old=conn.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone()
            if not old: report['unknown_ids'].append(rid);continue
            if old['archived_at']:
                report['archived_ids'].append(rid)
                item['outcome'] = 'archived'
                continue
            research_intake.validate_signal_targets(conn, rid, r)
            payload=json.dumps(r,sort_keys=True,ensure_ascii=False)
            result=store.research_observation(conn,rid,source,r,append_evidence=append_evidence)
            item['conflicts'] = result['conflicts']
            item['outcome'] = ('unchanged' if not result['saved'] else 'stale' if result['stale']
                               else 'created' if created else 'updated')
            item['revision'] = conn.execute('SELECT revision FROM records WHERE id=?', (rid,)).fetchone()[0]
            if not result['saved']: continue
            report['accepted']+=1
            report['conflicts'].extend(result['conflicts'])
            if result['stale']:
                report['stale']+=1
                continue
            item['task'] = research_intake.suggest_and_link(conn, rid, r)
            item['revision'] = conn.execute('SELECT revision FROM records WHERE id=?', (rid,)).fetchone()[0]
            named=r.get('named_consultant')
            if named:
                candidate=dict(name=named['name'],agency_id=rid,contact_route=named.get('public_route',''),
                               role=named.get('role',''),source_urls=r['source_urls'])
                if '@' in named.get('public_route','') and ' ' not in named['public_route']: candidate['email']=named['public_route']
                key='research-person:'+hashlib.sha256((rid+payload).encode()).hexdigest()
                q=conn.execute("INSERT OR IGNORE INTO review_queue(kind,payload_json,source,created_at,dedupe_key) VALUES('person_candidate',?,?,?,?)",(json.dumps(candidate),source,r['reviewed_at'],key))
                report['queued']+=q.rowcount
    return report


def compact_observations(conn, folder, at):
    """Drop old advert text from observations.json once the database holds that exact observation.
    An advert no record took (filtered out) has nothing in the database that depends on it."""
    from pending_store import ObservationStore
    def held(item):
        scope, sid = item['source_scope'], str(item['source_id'])
        mapped = [r[0] for r in conn.execute('SELECT record_id FROM source_records WHERE source_scope=? AND source_id=?', (scope, sid))]
        if not mapped:
            return True
        sha = wodb.observation_sha(scope, json.dumps(item, sort_keys=True))
        return conn.execute('SELECT 1 FROM observations WHERE record_id IN (%s) AND source=? AND payload_sha=? LIMIT 1'
                            % ','.join('?' * len(mapped)), (*mapped, scope, sha)).fetchone() is not None
    return ObservationStore(folder / 'observations.json').compact(at, held=held)


def protected_sources(conn):
    """What a board scan must keep whole: every identity, URL and role an import could match."""
    identities = conn.execute("SELECT source_scope,source_id FROM source_records "
                              "WHERE source_scope LIKE 'board:%' OR source_scope LIKE 'feed:%'").fetchall()
    roles = {(normalise.org_key(r['organisation'] or ''), normalise.org_key(r['name'] or ''))
             for r in conn.execute("SELECT name,organisation FROM records WHERE type='job' AND archived_at IS NULL")}
    return dict(identities=[tuple(r) for r in identities], urls=list(store.job_url_index(conn)),
                roles=[r for r in roles if all(r)])


def capacity_candidates(folder, prepared, terms, admission, inventory, legacy_admission, observed_urls):
    import hashlib
    candidates = []
    urls = {row['url'] for _, rows in inventory for row in rows if row.get('url')}
    for key, item in prepared[0].items():
        kind, scope, sid = item.get('kind'), item.get('source_scope', ''), str(item.get('source_id', ''))
        if (kind not in ('job','tender') or not scope or not sid or item.get('compacted_at') or admission.get(key)
                or not selected_candidate(item, urls, prepared[1])
                or observation_time(item.get('last_seen_at')) == observation_time(None)):
            continue
        current = {**item.get('current', {}), **terms.get(key, {})}
        rid = ('TN-' if kind == 'tender' else 'HN-') + hashlib.sha256((scope+'|'+sid).encode()).hexdigest()[:16]
        fact = observation_facts(current, kind, current.get('url', ''))
        row = dict(fact, id=rid, type=kind, status='candidate' if kind=='job' else '', source_file='source:'+scope,
                   source_lifecycle=item.get('source_lifecycle', 'unknown'), created_at=item['last_seen_at'])
        row['advert'] = {**current, 'text': str(current.get('text') or '')[:16001], 'source':scope, 'observed_at':item['last_seen_at']}
        if scope.startswith('alert:'):
            row['source_context'] = dict(source=scope, url=current.get('url',''), at=item.get('first_seen_at') or item['last_seen_at'], basis='email')
        candidates.append(dict(key=key, identity=(scope,sid), record=row))
    for row in ed.scan_rows(folder, folder.name, inventory):
        if row.get('url') in observed_urls or legacy_admission.get(row['id']):
            continue
        candidates.append(dict(key='legacy:'+row['id'], record=dict(row, status='candidate', created_at=wodb.now())))
    return candidates


def cmd_import_scans(conn, folder, run_id=""):
    path = folder / 'observations.json'
    from observation_reader import load
    data = load(path)
    observed_urls = {x.get('current',{}).get('url') for x in data.get('items',{}).values()
                     if x.get('kind') == 'job' and x.get('source_scope') and x.get('source_id')
                     and observation_time(x.get('last_seen_at')) != observation_time(None)} - {None, ''}
    inventory = scan_inventory(folder)
    candidate_urls = {j['url'] for _, source_rows in inventory for j in source_rows if j.get('url')}
    terms = advert_terms(data)
    prepared = prepare_observations(folder, data)
    admission = intake_decisions(prepared[0], terms, candidate_urls=candidate_urls, candidates=prepared[1])
    legacy_admission = intake_decisions({ed.scan_id(row['url']): dict(kind='job', current=row)
        for _, rows in inventory for row in rows if row.get('url')})
    import time
    import discovery_intake
    candidates = capacity_candidates(folder, prepared, terms, admission, inventory, legacy_admission, observed_urls)
    for attempt in range(3):
        capacity = discovery_intake.prepare(conn, candidates, discovery_intake.instant(wodb.now()))
        # Decode historical evidence before taking the writer lock. Budget.start rechecks the
        # data version captured before preparation; a concurrent writer retries both reads.
        # Legacy TSV import below adds only non-clock observations (such as field conflicts).
        watermarks = _observation_watermarks(conn)
        locked = time.monotonic()
        try:
            with store.transaction(conn):
                capacity.start(conn)
                first_event = conn.execute('SELECT COALESCE(MAX(id),0) FROM state_events').fetchone()[0]
                resolver = {}
                report = _import_scans(conn, folder, observed_urls, inventory=inventory, resolver=resolver,
                                       admission=legacy_admission, capacity=capacity)
                extra = import_observations(conn, folder, data, candidate_urls, resolver=resolver, terms=terms,
                                            admission=admission, prepared=prepared, capacity=capacity, watermarks=watermarks)
                extra['archived_organisations'] = sorted(set(report['archived_organisations']) | set(extra['archived_organisations']))
                for reason, count in report['intake_rejected'].items():
                    extra['intake_rejected'][reason] = extra['intake_rejected'].get(reason, 0) + count
                report.update(extra)
                report['capacity'] = capacity.finish(conn)
                report['malformed'] += report.pop('observation_errors', [])
                if run_id:
                    conn.execute('UPDATE state_events SET run_id=? WHERE id>?', (run_id, first_event))
                report['activity_detail'] = True
                conn.execute('UPDATE import_runs SET report_json=? WHERE id=(SELECT MAX(id) FROM import_runs)', (json.dumps(report),))
            report['write_lock_seconds'] = round(time.monotonic() - locked, 1)
            return report
        except discovery_intake.RetrySnapshot:
            if attempt == 2:
                raise store.Conflict('Workspace changed during admission; retry the import. No records were changed.')
