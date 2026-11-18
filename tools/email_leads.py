"""On-demand email lead review; pending observations remain the source of truth.

One serialized streaming read retains at most 20 results, never an archive-sized object.
Source parsing and mail access finish before the canonical short writer transaction.
"""
import hashlib
import json
import re
import threading
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path
from urllib.parse import quote

import email_sources
import normalise
import observation_reader
import records
from mail_intake import MailSpool, MailIntakeError

_READ_LOCK = threading.Lock()
LIMIT = 20


def _search_words(value):
    # Apply the same punctuation rules to both sides, retaining C++/C# as distinct terms.
    return re.findall(r'\w+(?:[+#]+)?', value.casefold())


def lead_id(item):
    return hashlib.sha256((item['source_scope'] + '\x1f' + str(item['source_id'])).encode()).hexdigest()


def _read(data_dir, visit):
    try:
        with _READ_LOCK, (Path(data_dir) / 'pending/observations.json').open(encoding='utf-8') as stream:
            observation_reader.read(stream, sources_only=True, visit_item=visit)
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('Email leads could not be read. Retry or check email processing; retained evidence is unchanged.') from None


def _row(item):
    if not isinstance(item, dict) or item.get('kind') != 'job' or not email_sources.is_alert(item.get('source_scope')):
        return None
    current = item.get('current') or {}
    if not isinstance(current, dict) or not item.get('source_id') or not current.get('title'):
        return None
    try:
        url = normalise.job_url(current.get('url', ''))
    except ValueError:
        return None
    return dict(id=lead_id(item), title=str(current['title'])[:500], company=str(current.get('company', ''))[:500],
                url=url, received_at=current.get('received_at') or item.get('last_seen_at', ''),
                source=item['source_scope'], source_label=email_sources.label(item['source_scope']),
                assessment='Unassessed', review_url='/#dashboard/lead/' + lead_id(item))


def search(conn, data_dir, query='', source=''):
    if not isinstance(query, str) or len(query) > 200 or source not in ('', *email_sources.SOURCES):
        raise ValueError('Use a search of up to 200 characters and a registered email source')
    # URL identity includes alternate sightings and merged survivors, as in manual save/import.
    urls = records.job_url_index(conn)
    mapped = {(r[0], str(r[1])) for r in conn.execute('SELECT source_scope,source_id FROM source_records')}
    chosen = {}
    words = _search_words(query)
    def visit(_key, item):
        row = _row(item)
        if not row or (source and row['source'] != 'alert:' + source): return
        if (item['source_scope'], str(item['source_id'])) in mapped or row['url'] in urls: return
        if item.get('current', {}).get('eligible') is not True or item.get('source_lifecycle') == 'closed': return
        haystack = (row['company'] + ' ' + row['title'] + ' ' + row['url']).casefold()
        tokens = _search_words(haystack)
        if not all(any(token.startswith(word) for token in tokens) for word in words): return
        order = (normalise.observation_time(row['received_at']), row['id'])
        old = chosen.get(row['url'])
        if old is None or order > old[0]:
            chosen[row['url']] = (order, row)
        if len(chosen) > LIMIT:
            earliest = min(chosen, key=lambda url: chosen[url][0])
            del chosen[earliest]
    _read(data_dir, visit)
    return dict(items=[row for _, row in sorted(chosen.values(), key=lambda entry: entry[0], reverse=True)],
                limit=LIMIT, sources=[dict(id=key, label=value['label']) for key, value in email_sources.SOURCES.items()])


def _find(data_dir, identity):
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
        raise ValueError('Invalid email lead link')
    found = None
    def visit(_key, item):
        nonlocal found
        if _row(item) and lead_id(item) == identity:
            found = item
    _read(data_dir, visit)
    if found is None: raise ValueError('This email lead is not in the retained source store. Search recent leads or open the original email.')
    return found


def _original(data_dir, item):
    receipt = (item.get('current') or {}).get('receipt_id')
    if not isinstance(receipt, str) or not re.fullmatch('[0-9a-f]{64}', receipt): return None
    try:
        return MailSpool(Path(data_dir) / 'email/mail.db', readonly=True).message(receipt)
    except MailIntakeError:
        return None


def _record_id(conn, item, url):
    mapped = conn.execute('SELECT record_id FROM source_records WHERE source_scope=? AND source_id=?',
                          (item['source_scope'], str(item['source_id']))).fetchone()
    by_url = records.match_job_url(conn, url)
    rid = records.active_job_survivor(conn, mapped[0]) if mapped else by_url
    if by_url and rid != by_url: raise records.Conflict('The email identity and source URL point to different records')
    return rid


def detail(conn, data_dir, identity):
    item = _find(data_dir, identity)
    row, current = _row(item), item['current']
    row.update(email_summary=str(current.get('email_summary') or current.get('text') or '')[:8000],
               pay_text=str(current.get('pay_text') or '')[:500], location=str(current.get('location') or '')[:500],
               record_id=_record_id(conn, item, row['url']), original_email=None, email_available=False,
               eligible=current.get('eligible') is True and item.get('source_lifecycle') != 'closed')
    if row['record_id']:
        record = records.get(conn, row['record_id'])
        row['record_status'] = record['status']
    original = _original(data_dir, item)
    if original:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(original['raw'])
        row['email_available'] = True
        row['email_subject'] = str(headers.get('Subject', ''))[:500]
        identifiers = headers.get_all('Message-ID', [])
        if len(identifiers) == 1 and re.fullmatch(r'<[^<>\s]{1,500}@[^<>\s]{1,250}>', str(identifiers[0])):
            row['original_email'] = 'https://mail.google.com/mail/u/0/#search/' + quote('rfc822msgid:' + str(identifiers[0])[1:-1], safe='')
    return row


def original_email(data_dir, identity):
    original = _original(data_dir, _find(data_dir, identity))
    if original is None: raise ValueError('The original email is unavailable in the retained spool. Open it in your mail app.')
    return original['raw']


def record_review(conn, rid):
    """Resolve a saved role's original alert without adding fields to every summary row."""
    record = records.get(conn, rid)
    context = record.get('source_context') or {}
    source = context.get('source')
    patterns = {'alert:linkedin': r'https://www\.linkedin\.com/jobs/view/(\d+)/',
                'alert:jackandjill': r'https://app\.jackandjill\.ai/jobs/([a-f0-9-]{36})/post'}
    pattern = patterns.get(source)
    match = re.fullmatch(pattern, context.get('url', '')) if pattern else None
    if not match: raise ValueError('No retained email lead identity is available for this record')
    return dict(id=lead_id(dict(source_scope=source, source_id=match[1])))


def shortlist(conn, data_dir, identity, *, actor='owner'):
    item = _find(data_dir, identity)
    row = _row(item)
    if item['current'].get('eligible') is not True or item.get('source_lifecycle') == 'closed':
        raise ValueError('This lead no longer passes the intake filters; review its source before saving it manually in Jobs.')
    with records.transaction(conn):
        rid = _record_id(conn, item, row['url'])
        result = dict(created=False, record=records.get(conn, rid)) if rid else records.save_job(conn,
                    dict(name=row['title'], organisation=row['company'], url=row['url']), actor=actor)
        record = result['record']
        if record.get('exclusion'):
            raise records.Conflict(record['exclusion']['reason'])
        if record['status'] in (None, '', 'candidate'):
            records.transition(conn, record['id'], dict(field='status', to='pursue', revision=record['revision'],
                               note='Manually shortlisted from an unassessed email lead'), actor=actor)
        # Evidence is an observation, never a verified field or personal fit summary.
        records.add_observation(conn, record['id'], item['source_scope'], item['last_seen_at'], json.dumps(item, sort_keys=True))
        conn.execute('INSERT OR IGNORE INTO source_records(source_scope,source_id,record_id) VALUES(?,?,?)',
                     (item['source_scope'], str(item['source_id']), record['id']))
        result['record'] = records.get(conn, record['id'])
    return result
