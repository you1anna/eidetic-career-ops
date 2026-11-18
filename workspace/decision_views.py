"""Read-only decision projections, shared by timelines, state builds and decision commands.

Lookup batches and the snapshot boundary remain common to every reader.
"""
import datetime as dt
import json
import re

import record_read
import states


def decision_choices(kind):
    """The declared answers; none for a kind no longer declared, so an old decision cannot be answered
    with choices nobody defines, and the workspace still loads."""
    if kind not in states.DECISION_KINDS:
        return []
    return [dict(id=c[0], label=c[1], next=c[2], note=c[3]) for c in (*states.DECISION_KINDS[kind]['choices'], states.ASK_CHOICE)]



def _decision_lookups(conn, rows, policy=None):
    """Resolve related facts once per projection, without SQL parameter-count limits.

    `policy` is `record_read.record_policy(conn)` already read in the caller's snapshot."""
    record_ids = list({r['record_id'] for r in rows if r['record_id']})
    version_ids = list({r['draft_version_id'] for r in rows if r['draft_version_id']})
    subjects = list({r['subject'] for r in rows})
    records = {r['id']: r for r in conn.execute(
        'SELECT r.id,r.name,r.type,d.version_id FROM records r LEFT JOIN drafts d ON d.record_id=r.id '
        'WHERE r.id IN (SELECT value FROM json_each(?))', (json.dumps(record_ids),))} if record_ids else {}
    versions = {r['id']: r for r in conn.execute(
        'SELECT * FROM draft_versions WHERE id IN (SELECT value FROM json_each(?))',
        (json.dumps(version_ids),))} if version_ids else {}
    latest = {(r['subject'], r['kind']): r['id'] for r in conn.execute(
        'SELECT subject,kind,MAX(id) AS id FROM decisions WHERE subject IN (SELECT value FROM json_each(?)) '
        'GROUP BY subject,kind', (json.dumps(subjects),))} if subjects else {}
    excluded, contexts = policy if policy is not None else record_read.record_policy(conn)
    return dict(records=records, versions=versions, latest=latest, exclusions=excluded, exclusion_contexts=contexts)



def decision_view(conn, row, lookups=None):
    lookups = _decision_lookups(conn, [row]) if lookups is None else lookups
    d = {k: row[k] for k in row.keys() if k != 'thread_json'}
    d['thread'] = json.loads(row['thread_json'] or '[]')
    d['choices'] = [] if row['kind']=='draft' else decision_choices(row['kind'])
    d['read_only'] = row['kind']=='draft'
    d['kind_label'] = states.DECISION_KINDS.get(row['kind'], {}).get('label', 'Kind no longer declared')
    rec = lookups['records'].get(row['record_id'])
    d['record_name'] = rec['name'] if rec else ''
    d['area'] = 'search' if rec and rec['type'] not in states.OPERATIONS_RECORD_TYPES else 'operations'
    d['area_label'] = states.DECISION_AREAS[d['area']]
    version = lookups['versions'].get(d['draft_version_id'])
    d['draft'] = dict(version, version_id=version['id']) if version else None
    d['exclusion'] = lookups['exclusions'].get(row['record_id'])
    if not d['exclusion'] and rec:
        # The decision's own context (or frozen draft) must name the excluded company; an
        # employer link alone does not exclude a general referral or independent decision.
        context = ' '.join([version['title'] or '', version['text'] or ''] if version else [])
        context += ' ' + row['question'] + ' ' + row['context']
        d['exclusion'] = next((e for e in lookups['exclusion_contexts'].get(row['record_id'], [])
                               if re.search(r'(?<!\w)' + re.escape(e['company']) + r'(?!\w)', context, re.I)), None)
        if d['exclusion']:
            d['exclusion'] = {k: d['exclusion'][k] for k in ('company', 'reason')}
    d['draft_is_current'] = bool(version and rec and rec['version_id'] == d['draft_version_id'])
    d['can_reconfirm'] = _legacy_reconfirmable(conn, row, lookups)
    return d



def _legacy_reconfirmable(conn, row, lookups=None):
    if row['kind']!='draft' or row['status']!='done' or row['draft_binding']!='legacy' or row['answer']!='approve':
        return False
    if lookups is not None:
        current = lookups['records'].get(row['record_id'])
        return bool(current and current['version_id'] == row['draft_version_id'] and
                    lookups['latest'].get((row['subject'], row['kind'])) == row['id'])
    current=conn.execute('SELECT version_id FROM drafts WHERE record_id=?',(row['record_id'],)).fetchone()
    return bool(current and current['version_id']==row['draft_version_id'] and not conn.execute(
        'SELECT 1 FROM decisions WHERE subject=? AND kind=? AND id>?', (row['subject'],row['kind'],row['id'])).fetchone())



def get_decision(conn, did):
    with record_read.snapshot(conn):
        row = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
        if row is None:
            raise ValueError('Unknown decision')
        return decision_view(conn, row)



def decisions_history(conn, rid=None, *, policy=None):
    """Complete decisions, including replaced questions and approvals older than the inbox window."""
    with record_read.snapshot(conn):
        rows = conn.execute('SELECT * FROM decisions' + (' WHERE record_id=?' if rid else '') + ' ORDER BY id', (rid,) if rid else ()).fetchall()
        lookups = _decision_lookups(conn, rows, policy)
        return [decision_view(conn, row, lookups) for row in rows]



def decisions_state(conn, days=14, *, policy=None):
    """Live decisions, plus those processed in the last `days`, ordered as the owner should see them:
    kind urgency, then how the recommendation leans (earlier choices are the positive ones)."""
    with record_read.snapshot(conn):
        return _decisions_state(conn, days, policy)



def _decisions_state(conn, days, policy=None):
    since = (dt.datetime.now().astimezone() - dt.timedelta(days=days)).isoformat(timespec='seconds')  # db.now()'s format
    rows = conn.execute("SELECT * FROM decisions d WHERE status IN ('open','answered') OR processed_at>=? OR "
                        "(status='done' AND kind='draft' AND answer='approve' AND draft_binding='legacy' AND "
                        "EXISTS(SELECT 1 FROM drafts v WHERE v.record_id=d.record_id AND v.version_id=d.draft_version_id) AND "
                        "NOT EXISTS(SELECT 1 FROM decisions newer WHERE newer.subject=d.subject AND newer.kind=d.kind AND newer.id>d.id)) ORDER BY id", (since,)).fetchall()
    # A withdrawal the agent replaced with a re-worded question is not news to the owner; the new question is.
    lookups = _decision_lookups(conn, rows, policy)
    later = lookups['latest']
    rows = [r for r in rows if not (r['status'] == 'withdrawn' and later[(r['subject'], r['kind'])] > r['id'])]
    out = [decision_view(conn, r, lookups) for r in rows]
    def lean(d):
        ids = [c[0] for c in states.DECISION_KINDS.get(d['kind'], {}).get('choices', ())]
        return ids.index(d['recommendation']) if d['recommendation'] in ids else len(ids) / 2
    out.sort(key=lambda d: (states.DECISION_KINDS.get(d['kind'], {}).get('order', 99), lean(d), d['id']))
    return out
