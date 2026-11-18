"""Read-only record projections shared by the workspace and CLI.

Enrichment uses a fixed number of queries. A deferred read transaction keeps records,
revisions and related evidence consistent while another connection writes in WAL mode.
"""
import json
import re
from urllib.parse import urlsplit

import db
from record_read import snapshot, record_policy, record_exclusions
import decision_views
import incoming_views
import evidence_checks
import normalise
import search_config
import evidence_briefs
import source_images
import email_sources


def flat_records(conn, rid=None):
    """Stored public fields and effective followups, without evidence enrichment."""
    where = 'r.id=?' if rid is not None else 'r.archived_at IS NULL'
    args = (rid,) if rid is not None else ()
    with snapshot(conn):
        records = {r['id']: {k: r[k] for k in r.keys() if k not in ('fields_json', 'status_raw')}
                   for r in conn.execute(f'SELECT r.* FROM records r WHERE {where} ORDER BY type,name,id', args)}
        if rid is not None and not records:
            raise ValueError('Unknown record')
        for r in records.values():
            r['followup'] = None
        for f in conn.execute(f'SELECT f.* FROM followups f JOIN records r ON r.id=f.record_id WHERE {where}', args):
            r = records[f['record_id']]
            r['followup'] = {k: f[k] for k in ('action', 'owner', 'status', 'reason', 'waiting_on', 'notes')}
            r.update(next_action=f['action'], owner=f['owner'], working_state=f['status'],
                     status_reason=f['reason'], waiting_on=f['waiting_on'])
        return list(records.values())


def _public_url(value):
    """Source links are evidence, never executable schemes or embedded credentials."""
    if not isinstance(value, str):
        return ''
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('http', 'https') and parsed.hostname and not (parsed.username or parsed.password) else ''
    except ValueError:
        return ''


def _source_contexts(conn, where, args):
    """Newest source identity per record; alert receipt dates never become listing evidence."""
    payload = "CASE WHEN json_valid(o.payload_json) THEN o.payload_json ELSE '{}' END"
    recovered = f"(o.source LIKE 'research:%' AND json_extract({payload},'$.alert_evidence.source') LIKE 'alert:%')"
    rows = conn.execute(
        f"WITH facts AS (SELECT o.id,o.record_id,CASE WHEN {recovered} THEN json_extract({payload},'$.alert_evidence.source') ELSE o.source END AS source,"
        f"COALESCE(json_extract({payload},'$.alert_evidence.url'),json_extract({payload},'$.current.url'),json_extract({payload},'$.url'),'') AS url,"
        f"CASE WHEN {recovered} THEN json_extract({payload},'$.alert_evidence.received_at') WHEN o.source LIKE 'alert:%' THEN "
        f"COALESCE(NULLIF(json_extract({payload},'$.first_seen_at'),''),NULLIF(json_extract({payload},'$.current.received_at'),''),o.observed_at) "
        "ELSE o.observed_at END AS at FROM observations o JOIN records r ON r.id=o.record_id "
        f"WHERE {where} AND (o.source LIKE 'alert:%' OR {recovered} OR {db.SCANNER_SQL})), "
        "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY record_id "
        "ORDER BY wo_observation_time(at) DESC,id DESC) AS rank FROM facts) "
        "SELECT record_id,source,url,at FROM ranked WHERE rank=1", args)
    for row in rows:
        yield row['record_id'], dict(source=row['source'], url=_public_url(row['url']), at=row['at'],
                                    basis='email' if row['source'].startswith('alert:') else 'listing')


def company_logos(conn, records):
    """One newest sourced logo per unambiguous company, shared by its displayed names.

    Only small logo fields cross the SQL boundary, never historic advert bodies. Explicit merge
    edges preserve identity; a plain archive, ambiguous name or agency/client mismatch gets no logo.
    """
    with snapshot(conn):
        organisations = {r['id']: dict(r) for r in conn.execute(
            "SELECT id,type,name,archived_at FROM records WHERE type IN ('company','agency')")}
        merges = {r['entity_id']: r['to_value'] for r in conn.execute(
            "SELECT e.entity_id,e.to_value FROM state_events e JOIN records r ON r.id=e.entity_id "
            "WHERE r.archived_at IS NOT NULL AND r.type IN ('company','agency') AND e.field='archived' "
            "AND e.id=(SELECT MAX(a.id) FROM state_events a WHERE a.entity_id=e.entity_id AND a.field='archived') "
            "AND EXISTS(SELECT 1 FROM state_events m WHERE m.entity_id=e.to_value AND m.field='merged' AND m.from_value=e.entity_id)")}

        def survivor(rid):
            seen = set()
            while rid in organisations and rid not in seen:
                seen.add(rid)
                if not organisations[rid]['archived_at']:
                    return rid
                rid = merges.get(rid)
            return None

        names = {}
        for organisation in organisations.values():
            rid = survivor(organisation['id'])
            if rid:
                names.setdefault(normalise.org_key(organisation['name']), set()).add(rid)

        def resolve(key):
            ids = names.get(key, set())
            if len(ids) != 1:
                return None
            rid = next(iter(ids))
            return rid if len(names.get(normalise.org_key(organisations[rid]['name']), set())) == 1 else None

        conn.create_function('wo_company_logo', 1, source_images.linkedin_company_logo, deterministic=True)
        conn.create_function('wo_logo_company_key', 1,
                             lambda value: normalise.org_key(value) if isinstance(value, str) else '', deterministic=True)
        payload = "CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END"
        rows = conn.execute(
            "WITH facts AS (SELECT id,source,observed_at,"
            f"wo_logo_company_key(json_extract({payload},'$.current.company')) AS company_key,"
            f"wo_company_logo(json_extract({payload},'$.current.company_logo_url')) AS url "
            f"FROM observations WHERE source='alert:linkedin' OR {db.SCANNER_SQL}), "
            "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY company_key "
            "ORDER BY wo_observation_time(observed_at) DESC,id DESC) AS rank FROM facts "
            "WHERE company_key<>'' AND url<>'') SELECT * FROM ranked WHERE rank=1 "
            "ORDER BY wo_observation_time(observed_at) DESC,id DESC")
        result = {}
        for row in rows:
            rid = resolve(row['company_key'])
            if rid and rid not in result:
                result[rid] = dict(url=row['url'], company_name=organisations[rid]['name'],
                                   observed_at=row['observed_at'], source=row['source'])
        for record in records:
            record.pop('logo_company_id', None)
            name = record.get('name') if record.get('type') in ('company', 'agency') else record.get('organisation')
            rid = resolve(normalise.org_key(name))
            if rid in result:
                record['logo_company_id'] = rid
        return result


def _commercial_signals(conn, where, args):
    """Latest evidence per native award identity, then ten identities per record."""
    rows = conn.execute(
        "WITH source_facts AS (SELECT o.*,wo_observation_time(o.observed_at) AS observed_time,"
        "COALESCE(NULLIF(json_extract(o.payload_json,'$.source_scope'),''),o.source) AS source_scope,"
        "json_extract(o.payload_json,'$.source_id') AS source_id FROM observations o "
        f"JOIN records r ON r.id=o.record_id WHERE {where} AND o.source LIKE 'award:%'), "
        "native_versions AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY record_id,source_scope,"
        "COALESCE(NULLIF(source_id,''),'observation:'||id) ORDER BY observed_time DESC,id DESC) AS native_rank FROM source_facts), "
        "latest AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY observed_time DESC,id DESC) AS rank "
        "FROM native_versions WHERE native_rank=1) SELECT * FROM latest WHERE rank<=10 ORDER BY record_id,rank", args)
    for row in rows:
        payload = json.loads(row['payload_json'])
        current = payload.get('current', payload)
        if not isinstance(current, dict):
            continue
        signal = dict(current, source_scope=row['source_scope'], source_id=row['source_id'] or '', observed_at=row['observed_at'])
        for field in ('url', 'source_url', 'evidence_url', 'buyer_url', 'supplier_url'):
            if field in signal:
                signal[field] = _public_url(signal[field])
        yield row['record_id'], signal


def _job_evidence_checks(conn, where, args):
    """Latest explicit check per subject across history, independent of the three-review window."""
    active_types = {row['id']: row['type'] for row in conn.execute(
        "SELECT id,type FROM records WHERE archived_at IS NULL AND type IN ('person','company','agency')")}
    rows = conn.execute(
        "SELECT o.record_id,r.type,json_extract(o.payload_json,'$.freshness_checks') AS checks,"
        "json_extract(o.payload_json,'$.source_urls') AS urls FROM observations o "
        f"JOIN records r ON r.id=o.record_id WHERE {where} AND r.type IN ('job','person','company','agency','opportunity','tender') "
        "AND o.source LIKE 'research:%' AND json_type(CASE WHEN json_valid(o.payload_json) "
        "THEN o.payload_json ELSE '{}' END,'$.freshness_checks')='array' "
        "ORDER BY wo_observation_time(o.observed_at),o.id", args)
    latest, now = {}, evidence_checks.utc_now()
    # Equal timestamps must never let a confirmation hide a known inability to confirm or closure.
    status_order = {'confirmed': 0, 'unverified': 1, 'closed': 2}
    for row in rows:
        for check in json.loads(row['checks']):
            try:
                if row['type']=='person' and (check.get('scope')!='contact_route' or check.get('person_id')!=row['record_id']):continue
                if row['type']!='job' and check.get('scope')=='vacancy':continue
                stamp = evidence_checks.validate_check(check, json.loads(row['urls'] or 'null'), now)
                if not evidence_checks.valid_targets(check, active_types):
                    continue
            except (TypeError, ValueError):
                continue
            key = (row['record_id'], check['scope'], check.get('person_id', ''), check.get('organisation_id', ''),
                   evidence_checks.route_class(check))
            rank = (stamp, status_order[check['status']])
            if key not in latest or rank >= latest[key][0]:
                latest[key] = (rank, check)
    for key in sorted(latest):
        yield key[0], latest[key][1]


def _job_briefing_fields(conn, where, args):
    """Latest job briefing fields and company contact route, independent of routine checks.

    Empty lists/text explicitly clear a field. Malformed legacy values are ignored. Each field
    keeps its own original review/source dates; this projection makes no freshness claim.
    """
    fields = ('facts', 'decisive_unknowns', 'public_contact_route')
    safe = "CASE WHEN json_valid(o.payload_json) THEN o.payload_json ELSE '{}' END"
    present = ' OR '.join(f"json_type({safe},'$.{field}') IS NOT NULL" for field in fields)
    rows = conn.execute(
        "SELECT o.record_id,o.source,o.observed_at,o.payload_json,r.type FROM observations o "
        f"JOIN records r ON r.id=o.record_id WHERE {where} AND (r.type='job' "
        f"OR (r.type='company' AND json_type({safe},'$.public_contact_route') IS NOT NULL)) "
        f"AND o.source LIKE 'research:%' AND json_type({safe})='object' AND ({present}) "
        "ORDER BY o.record_id,wo_observation_time(o.observed_at) DESC,o.id DESC", args)
    selected = {}
    for row in rows:
        wanted = ('public_contact_route',) if row['type'] == 'company' else fields
        supplied = selected.setdefault(row['record_id'], set())
        if len(supplied) == len(wanted):
            continue
        payload = json.loads(row['payload_json'])
        urls = []
        if isinstance(payload.get('source_urls'), list):
            for url in payload['source_urls']:
                if _public_url(url) and len(url) <= 2048 and url not in urls:
                    urls.append(url)
                    if len(urls) == 10:
                        break
        source_date = payload.get('source_date')
        provenance = dict(source=row['source'][:400], reviewed_at=row['observed_at'][:50],
            source_date=source_date if isinstance(source_date, str) and normalise.is_factual_date(source_date) else '',
            source_urls=urls)
        if row['type'] == 'company':
            provenance['verification'] = payload.get('verification') if isinstance(payload.get('verification'), str) else ''
        for field in wanted:
            if field in supplied or field not in payload:
                continue
            value = payload[field]
            if field == 'public_contact_route':
                if not isinstance(value, str):
                    continue
                value = value.strip()[:2000]
            else:
                if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                    continue
                value = [item.strip()[:1000] for item in value[:20]]
            supplied.add(field)
            yield row['record_id'], field, value, provenance


def load_records(conn, rid=None, *, brief_attempts=None, policy=None):
    """All active records, or one record including archives, with the same public facts.

    `policy` is `record_policy(conn)` when the caller has already read it in this snapshot."""
    where = 'r.id=?' if rid is not None else 'r.archived_at IS NULL'
    args = (rid,) if rid is not None else ()
    with snapshot(conn):
        records = {r['id']: r for r in flat_records(conn, rid)}
        incoming_views.attach(conn, records.values())
        excluded, contexts = policy if policy is not None else record_policy(conn)
        for r in records.values():
            r['exclusion'] = excluded.get(r['id'])
            r['exclusion_contexts'] = contexts.get(r['id'], [])
            r.update(research=[], signals=[], commercial_signals=[], registry=[], linkedin={}, advert={}, last_activity='')
            if r['type'] in ('job','person','company','agency','opportunity','tender'):
                r['evidence_checks'] = dict(vacancy=None, contacts=[])
            if r['type'] == 'job':
                r['briefing'] = dict(facts=[], decisive_unknowns=[], public_contact_route='', provenance={})
        for record_id, field, value, provenance in _job_briefing_fields(conn, where, args):
            briefing = records[record_id].setdefault('briefing', dict(public_contact_route='', provenance={}))
            briefing[field] = value
            briefing['provenance'][field] = provenance
        for record_id, check in _job_evidence_checks(conn, where, args):
            projected = records[record_id]['evidence_checks']
            if check['scope'] == 'vacancy':
                projected['vacancy'] = check
            else:
                projected['contacts'].append(check)
        for row in conn.execute(f"SELECT e.entity_id,MAX(e.at) AS at FROM state_events e JOIN records r ON r.id=e.entity_id "
                                f"WHERE {where} AND e.actor NOT IN ('source','research','migration','agent') GROUP BY e.entity_id", args):
            records[row['entity_id']]['last_activity'] = row['at']
        # Window limits bound both materialized payloads and Python parsing, per record.
        for kind, target in (('research:%', 'research'), ('news:%', 'signals'), ('companies-house', 'registry')):
            if target == 'research':
                # Reserve one of the existing three slots for an agency's newest explicit desk
                # assessment, including a withdrawal. Routine reviews must not displace it or
                # renew its date. Windowing keeps the read batched and payload parsing bounded.
                query = (
                    "WITH candidates AS (SELECT o.*,CASE WHEN r.type='agency' AND r.kind='agency' "
                    "AND json_type(CASE WHEN json_valid(o.payload_json) THEN o.payload_json ELSE '{}' END,"
                    "'$.desk_summary') IS NOT NULL THEN 1 ELSE 0 END AS is_desk FROM observations o "
                    f"JOIN records r ON r.id=o.record_id WHERE {where} AND o.source LIKE ?), "
                    "desks AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY record_id,is_desk "
                    "ORDER BY wo_observation_time(observed_at) DESC,id DESC) AS desk_rank FROM candidates), "
                    "bounded AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY "
                    "CASE WHEN is_desk=1 AND desk_rank=1 THEN 0 ELSE 1 END,"
                    "wo_observation_time(observed_at) DESC,id DESC) AS rank FROM desks) "
                    "SELECT * FROM bounded WHERE rank<=3 ORDER BY record_id,wo_observation_time(observed_at) DESC,id DESC")
            else:
                query = (f"SELECT * FROM (SELECT o.*,ROW_NUMBER() OVER (PARTITION BY o.record_id "
                         f"ORDER BY wo_observation_time(o.observed_at) DESC,o.id DESC) AS rank FROM observations o "
                         f"JOIN records r ON r.id=o.record_id WHERE {where} AND o.source LIKE ?) "
                         "WHERE rank<=3 ORDER BY record_id,rank")
            rows = conn.execute(query, (*args, kind))
            for row in rows:
                payload = json.loads(row['payload_json'])
                if target in ('research', 'registry'):
                    payload = {**payload, 'source': row['source'], 'observed_at': row['observed_at']}
                records[row['record_id']][target].append(payload)
        # Only relationship metadata, never messages, subjects, email addresses or conversation IDs.
        rows = conn.execute(f"SELECT * FROM (SELECT o.*,ROW_NUMBER() OVER (PARTITION BY o.record_id "
                            f"ORDER BY wo_observation_time(o.observed_at) DESC,o.id DESC) AS rank FROM observations o "
                            f"JOIN records r ON r.id=o.record_id WHERE {where} AND lower(o.source) LIKE 'linkedin%') "
                            "WHERE rank=1", args)
        for row in rows:
            p = json.loads(row['payload_json'])
            evidence = p.get('evidence') or {}
            threads = evidence.get('threads') or {}
            records[row['record_id']]['linkedin'] = dict(
                observed_at=row['observed_at'],
                observed_company=p.get('observed_company') or p.get('company') or '',
                observed_role=p.get('observed_role') or p.get('position') or '',
                flags=p.get('flags') or [], connected_on=p.get('connected_date') or p.get('connected_on') or '',
                evidence=dict(threads={k: threads[k] for k in ('count', 'two_way', 'last_two_way', 'sent', 'received', 'first', 'last', 'last_sent') if k in threads},
                              endorsed_you=evidence.get('endorsed_you', 0), you_endorsed=evidence.get('you_endorsed', 0),
                              recommendations=evidence.get('recommendations') or []))
        # The latest advert wording enables semantic qualification without another web request.
        rows = conn.execute(f"SELECT * FROM (SELECT o.*,ROW_NUMBER() OVER (PARTITION BY o.record_id "
                            f"ORDER BY wo_observation_time(o.observed_at) DESC,o.id DESC) AS rank FROM observations o "
                            f"JOIN records r ON r.id=o.record_id WHERE {where} AND {db.SCANNER_SQL}) "
                            "WHERE rank=1", args)
        for row in rows:
            payload = json.loads(row['payload_json'])
            p = payload.get('current') or payload
            if not isinstance(p, dict):
                continue
            allowed = ('title', 'text', 'description', 'body', 'employment_type', 'contract_type', 'engagement',
                       'category', 'department', 'url', 'date', 'published_at', 'location')
            # One sentinel character lets the bounded requirement extractor see omitted text;
            # otherwise a late essential clause could disappear into a false strong recommendation.
            advert = {k: str(p[k])[:16001 if k in ('text', 'description', 'body') else 16000]
                      for k in allowed if p.get(k) is not None}
            advert.update(source=row['source'], observed_at=row['observed_at'])
            records[row['record_id']]['advert'] = advert
        for record_id, context in _source_contexts(conn, where, args):
            records[record_id]['source_context'] = context
        for record_id, signal in _commercial_signals(conn, where, args):
            records[record_id]['commercial_signals'].append(signal)
        # Registry facts count only for the company number currently recorded (a corrected one hides them).
        numbers = {row['record_id']: row['source_id'] for row in conn.execute(
            "SELECT record_id,source_id FROM source_records WHERE source_scope='companies-house'")}
        for r in records.values():
            r['registry'] = [x for x in r['registry'] if x.get('company_number') == numbers.get(r['id'])]
            r['last_activity'] = max((x for x in (r['last_activity'], r['last_touch'])
                                      if x and re.match(r'^\d{4}-\d{2}-\d{2}(?:T|$)', x)), default='')
        # One availability answer for briefs, selection, research and the browser (evidence_checks owns the rule).
        now = evidence_checks.utc_now()
        for r in records.values():
            if r['type'] in evidence_checks.ADVERT_TYPES:
                r['availability'] = evidence_checks.advert_availability(r, now)
        evidence_briefs.attach(conn, list(records.values()), attempts=brief_attempts)
        return list(records.values())


def last_sightings(conn):
    """When a scanner last listed each active record. A re-sighting moves its observation's time forward."""
    return {row['record_id']: row['observed_at'] for row in conn.execute(
        'SELECT record_id,observed_at FROM (SELECT o.record_id,o.observed_at,ROW_NUMBER() OVER '
        '(PARTITION BY o.record_id ORDER BY wo_observation_time(o.observed_at) DESC,o.id DESC) AS rank '
        'FROM observations o JOIN records r ON r.id=o.record_id '
        f'WHERE r.archived_at IS NULL AND {db.SCANNER_SQL}) WHERE rank=1')}


def history(conn, rid=None, recent=False):
    where, args = ('e.entity_id=?', (rid,)) if rid is not None else ('r.archived_at IS NULL', ())
    if recent:
        where += " AND e.actor NOT IN ('source','research','migration','agent')"
    query = f'SELECT e.* FROM state_events e JOIN records r ON r.id=e.entity_id WHERE {where} ORDER BY e.at DESC,e.id DESC'
    if recent:
        query += ' LIMIT 12'
    return [dict(r, record_id=r['entity_id']) for r in conn.execute(query, args)]


# The mail archive is nearly full at either figure (the caretaker's email-capacity check uses the same).
SPOOL_WARN_BYTES = 48 * 1024 * 1024
SPOOL_WARN_COUNT = 750


def _email_source_status(latest, legacy_processed):
    """Only the latest receipt: the worker carries successes across empty/failed runs."""
    fields = ('state', 'complete', 'messages', 'jobs', 'eligible', 'new', 'quarantined', 'last_alert_processed')
    current = latest.get('sources') or {'alert:linkedin': dict(latest.get('processing') or {},
                                                            last_alert_processed=legacy_processed)}
    return {scope: dict(label=email_sources.label(scope), **{key: value.get(key) for key in fields})
            for scope, value in current.items() if email_sources.is_alert(scope) and isinstance(value, dict)}


def email_status(conn):
    """A bounded, non-personal view of actual VPS email runs, independent of other scans."""
    row = conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    if not row:
        return dict(status='not_started', last_attempt=None, last_success=None, last_alert_processed=None)
    latest = json.loads(row[0])
    success = conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email:%' AND status='ok' ORDER BY run_id DESC LIMIT 1").fetchone()
    alert = conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'email:%' AND status='ok' AND json_extract(receipt_json,'$.processing.messages') > 0 ORDER BY run_id DESC LIMIT 1").fetchone()
    arrival = conn.execute("SELECT json_extract(receipt_json,'$.collection.last_received_at') FROM scan_runs WHERE run_id LIKE 'email:%' AND json_extract(receipt_json,'$.collection.last_received_at') IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()
    result = dict(status=latest.get('status','failed'), last_attempt=latest.get('started_at'),
                  finished_at=latest.get('finished_at'), phase=latest.get('phase'),
                  last_success=json.loads(success[0]).get('finished_at') if success else None,
                  last_alert_processed=json.loads(alert[0]).get('finished_at') if alert else None,
                  last_received_at=arrival[0] if arrival else None,
                  message=latest.get('message',''), interval_minutes=15)
    result['sources'] = _email_source_status(latest, result['last_alert_processed'])
    for section, fields in [('collection', ('received','duplicates','pending','total_received')),
                            ('processing', ('messages','jobs','eligible','new','quarantined','last_sequence','high_watermark',
                                            'cards','logo_cards','logo_expired','logo_expiring_soon','logo_expiry_unknown'))]:
        result[section] = {key: latest.get(section,{}).get(key) for key in fields}
    # Keep the last actual import across empty polls, including imports whose collection was
    # partial. Project counts in SQL: neither record IDs nor arbitrary receipt keys reach the UI.
    fields = ('finished_at', 'processing.messages', 'processing.jobs', 'processing.eligible',
              'import.capacity.allowance_remaining')
    reasons = ('working_limit', 'seven_day_limit', 'unresolved_stale', 'not_recommended')
    expressions = [f"json_extract(receipt_json,'$.{field}')" for field in fields]
    expressions += ["CASE WHEN json_type(receipt_json,'$.import.capacity.admitted')='array' "
                    "THEN json_array_length(receipt_json,'$.import.capacity.admitted') END",
                    "json_type(receipt_json,'$.import.capacity.deferred')"]
    expressions += [f"json_extract(receipt_json,'$.import.capacity.deferred.{reason}')" for reason in reasons]
    imported = conn.execute('SELECT ' + ','.join(expressions) + " FROM scan_runs "
        "WHERE run_id >= 'email:' AND run_id < 'email;' "
        "AND json_extract(receipt_json,'$.processing.messages') > 0 "
        "AND json_extract(receipt_json,'$.finished_at') IS NOT NULL "
        "AND (json_type(receipt_json,'$.import.capacity')='object' "
        "OR json_type(receipt_json,'$.import.new')='integer') "
        "AND json_extract(receipt_json,'$.import.error') IS NULL "
        "ORDER BY run_id DESC LIMIT 1").fetchone()
    result['last_import'] = None
    if imported:
        def count(value):
            return value if type(value) is int and 0 <= value <= 2**53-1 else None
        result['last_import'] = dict(finished_at=imported[0],
            **{key:count(value) for key,value in zip(
                ('messages','jobs','eligible','allowance_remaining','admitted'), imported[1:6])},
            deferred={reason:count(value) if value is not None else 0
                      for reason,value in zip(reasons,imported[7:])} if imported[6]=='object' else None)
    spool=latest.get('spool',{})
    result['capacity_warning'] = spool.get('bytes',0) >= SPOOL_WARN_BYTES or spool.get('count',0) >= SPOOL_WARN_COUNT
    return result


def run_statuses(conn):
    """The run statuses the page shows. An open page re-reads only these, never the whole summary."""
    return dict(email_processing=email_status(conn), research_processing=research_status(conn),
                caretaker_processing=caretaker_status(conn))


QUIET_PASSES = 4      # two days of passes in a row that confirm nothing need attention
QUIET_WINDOW = 60     # recent research receipts read to find the last confirmation (30 days)


def research_pass_capped(receipt):
    """Whether a pass stopped at its own per-pass call cap rather than the Codex account allowance.

    Both share the `usage_limit` reason. A pass that made its full number of calls stopped at its
    cap (an account limit on that last permitted call reads the same; the next pass then makes none).
    """
    cap = (receipt.get('limits') or {}).get('max_calls')
    return (receipt.get('reason') == 'usage_limit' and bool(cap)
            and (receipt.get('usage') or {}).get('calls', 0) >= cap)


def research_confirmed(receipt):
    """Whether a pass confirmed anything: an advert open or closed, a contact route, or a target
    fully checked or still current. A model call that leaves every gap open confirms nothing."""
    return bool(receipt.get('verified') or receipt.get('contacts_verified')
                or any(item.get('outcome') in ('confirmed', 'closed', 'current') for item in receipt.get('items') or []))


def _quiet_passes(conn):
    """Completed passes since the last that confirmed anything, newest first within a bounded window.
    Blocked, interrupted, failed and running passes are execution states, not research results."""
    quiet = 0
    for (raw,) in conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'research:%' "
                               "ORDER BY run_id DESC LIMIT ?", (QUIET_WINDOW,)):
        receipt = json.loads(raw)
        if not (receipt.get('finished_at') and receipt.get('status') in ('ok', 'partial') and receipt.get('checked')):
            continue
        if research_confirmed(receipt):
            return quiet, receipt['finished_at']
        quiet += 1
    return quiet, None


def research_status(conn):
    """Safe operational projection: a process success cannot hide blocked sources or backlog."""
    row=conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'research:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    if not row: return dict(status='not_started', last_attempt=None, last_success=None)
    latest=json.loads(row[0])
    complete=conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'research:%' AND status='ok' ORDER BY run_id DESC LIMIT 1").fetchone()
    result={key:latest.get(key) for key in ('status','phase','finished_at','message','reason','total','checked','verified','unverified','backlog','contacts_total','contacts_verified','spend','limits','engine','usage','focused','whole_queue','outside_focus','people','current','reused','allowance')}
    if isinstance(result.get('allowance'),dict):
        import allowances
        try:
            policy=allowances.policy()
            result['allowance']={**result['allowance'], 'day_limit':policy['daily'],'week_limit':policy['weekly'],
                                 'research_day_limit':policy['lanes']['research'][0],'research_week_limit':policy['lanes']['research'][1]}
        except (ValueError,OSError):
            result['allowance']={k:v for k,v in result['allowance'].items() if not k.endswith('_limit')}
    result.update(last_attempt=latest.get('started_at'),last_success=json.loads(complete[0]).get('finished_at') if complete else None,
                  schedule='09:00 and 19:00 Europe/London')
    # The Research panel names a run of passes that confirm nothing; the caretaker leaves it there.
    quiet, last_confirmed = _quiet_passes(conn)
    result.update(pass_cap=research_pass_capped(latest), quiet_passes=quiet, last_confirmed=last_confirmed,
                  quiet_attention=quiet >= QUIET_PASSES)
    if isinstance(latest.get('briefs'),dict):
        result['briefs']={k:latest['briefs'].get(k,0) for k in ('total','sufficient','unresolved','blocking')}
    # IDs and fixed outcomes enable drill-down, with no original page/model payload in site status.
    # Preparation slots are explained in their own panel, not as research source gaps, unless they failed with an error.
    result['issues']=[{key:item.get(key) for key in ('id','outcome','reason','where')} for item in latest.get('items',[])
                      if item.get('outcome')=='failed' or (item.get('outcome') not in ('confirmed','closed','current') and not item.get('preparation'))][:30]
    # Items that raised an error inside an otherwise completed pass, grouped by type and code location.
    # The caretaker still treats a completed pass as recovery (22 October); the Dashboard lists these.
    failures = {}
    for item in latest.get('items', []):
        if item.get('outcome') == 'failed':
            key = (str(item.get('reason') or 'unknown')[:80], str(item.get('where') or '')[:300])
            failures[key] = failures.get(key, 0) + 1
    result['item_failures'] = [dict(reason=reason, where=where or None, count=count)
                               for (reason, where), count in sorted(failures.items(), key=lambda x: -x[1])][:10]
    result['waiting_ids']=latest.get('waiting_ids',[])[:30]
    return result


def receipts(conn):
    return list(iter_receipts(conn))


def iter_receipts(conn):
    """Complete receipt history in its public order, decoded one row at a time.

    The caller owns the read snapshot and must consume this before closing its connection.
    """
    for row in conn.execute('SELECT run_id,date,status,receipt_json FROM scan_runs ORDER BY date DESC,run_id DESC'):
        r = dict(row)
        r['receipt'] = json.loads(r.pop('receipt_json'))
        yield r


def timeline(conn, rid):
    with snapshot(conn):
        row = conn.execute('SELECT revision FROM records WHERE id=? AND archived_at IS NULL', (rid,)).fetchone()
        if not row:
            raise ValueError('Unknown record')
        return dict(record_id=rid, revision=row['revision'], history=history(conn, rid),
                    decisions=decision_views.decisions_history(conn, rid),
                    drafts=[dict(r) for r in conn.execute('SELECT * FROM draft_versions WHERE record_id=? ORDER BY id DESC', (rid,))],
                    outcomes=[outcome_view(r) for r in conn.execute('SELECT * FROM outcomes WHERE record_id=? ORDER BY at DESC,id DESC', (rid,))],
                    interactions=[dict(r) for r in conn.execute(LIVE_INTERACTIONS + ' AND record_id=? ORDER BY at DESC,id DESC', (rid,))])


# Caretaker receipts store only the checks that changed since the receipt named in `base`, with the
# full ordered `ids` when the set or order changed; a receipt without `base` holds the full list.
# An unchanged check verified at its run's start counts as verified again at the next run's start.
CARETAKER_FULL_EVERY = 96  # a full list at least once a day of 15-minute runs, bounding each rebuild


def _caretaker_receipt(conn, run_id):
    row = conn.execute('SELECT receipt_json FROM scan_runs WHERE run_id=?', (run_id,)).fetchone()
    try:
        receipt = json.loads(row[0]) if row else None
    except ValueError:
        return None
    return receipt if isinstance(receipt, dict) else None


def _carried(check, base_started, started):
    return dict(check, verified_at=started) if check.get('verified_at') == base_started else check


def _caretaker_apply(base_checks, base_started, receipt):
    checks = {c['id']: _carried(c, base_started, receipt.get('started_at')) for c in base_checks}
    checks.update({c['id']: c for c in receipt.get('checks', [])})
    return [checks[i] for i in receipt.get('ids') or [c['id'] for c in base_checks]]


def caretaker_checks(conn, run_id):
    """Every check a caretaker receipt recorded, rebuilt from its base receipts; None if one is missing."""
    chain, receipt = [], _caretaker_receipt(conn, run_id)
    while receipt is not None and receipt.get('base') and len(chain) <= 2 * CARETAKER_FULL_EVERY:
        chain.append(receipt)
        receipt = _caretaker_receipt(conn, receipt['base'])
    if receipt is None or receipt.get('base'):
        return None
    checks, started = list(receipt.get('checks', [])), receipt.get('started_at')
    try:
        for later in reversed(chain):
            checks, started = _caretaker_apply(checks, started, later), later.get('started_at')
    except (KeyError, TypeError):
        return None
    return checks


def compact_caretaker_receipt(conn, receipt):
    """The caretaker receipt to store: the checks changed since the latest receipt, or the full list
    when none can be rebuilt, the list is empty, a day's chain is complete or the changes would not
    rebuild it exactly. Call it inside the writer lock that serializes caretaker receipts."""
    row = conn.execute("SELECT run_id, receipt_json FROM scan_runs WHERE run_id >= 'caretaker:' AND run_id < 'caretaker;' "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    if not row or not receipt.get('checks'):
        return receipt
    base = _caretaker_receipt(conn, row[0]) or {}
    chain = base.get('chain', 0) + 1 if base.get('base') else 1
    base_checks = caretaker_checks(conn, row[0])
    if not base_checks or chain >= CARETAKER_FULL_EVERY:
        return receipt
    carried = {c['id']: _carried(c, base.get('started_at'), receipt.get('started_at')) for c in base_checks}
    ids = [c['id'] for c in receipt['checks']]
    changed = dict(receipt, base=row[0], chain=chain, checks=[c for c in receipt['checks'] if carried.get(c['id']) != c],
                   unchanged=sum(carried.get(c['id']) == c for c in receipt['checks']))
    if ids != [c['id'] for c in base_checks]:
        changed['ids'] = ids
    try:
        exact = _caretaker_apply(base_checks, base.get('started_at'), changed) == receipt['checks']
    except KeyError:
        exact = False
    return changed if exact else receipt


def caretaker_subject(check):
    """The incident a check's failure card is filed under: the source's own subject for a source check
    (the daily ingest raises those), otherwise the caretaker's. One rule for the card and the page."""
    return check.get('subject') or 'workspace:caretaker-' + str(check['id']).replace(':', '-').replace('.', '-')


def caretaker_status(conn):
    row=conn.execute("SELECT run_id, receipt_json FROM scan_runs WHERE run_id LIKE 'caretaker:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    if not row:return dict(status='not_started')
    latest=json.loads(row[1])
    checks=caretaker_checks(conn,row[0])
    if checks is None:
        checks=[dict(id='receipt',label='Caretaker checks',status='unknown',verified_at=None,
                     detail='This run\'s check list could not be rebuilt from earlier receipts; its overall status is shown above.')]
    return dict(status=latest.get('status'),last_attempt=latest.get('started_at'),finished_at=latest.get('finished_at'),
                checks=[{**{k:c.get(k) for k in ('id','label','status','detail','verified_at')},'subject':caretaker_subject(c)}
                        for c in checks][:100],model_calls=0)


def outcome_view(row):
    item=dict(row)
    item['snapshot']=json.loads(item.pop('snapshot_json','{}'))
    return item


# Interactions that still count as contact: an outcome corrected as a mistake takes its own with it.
LIVE_INTERACTIONS=('SELECT * FROM interactions WHERE id NOT IN (SELECT interaction_id FROM outcomes '
                   'WHERE corrected_at IS NOT NULL AND interaction_id IS NOT NULL)')
