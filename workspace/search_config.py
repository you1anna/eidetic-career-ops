"""CV choices and explicit search exclusions, independent of relevance scoring."""
from functools import lru_cache
from pathlib import Path

import normalise

ROOT = Path(__file__).resolve().parent.parent
CV_KEYS = ('Director', 'Manager', 'QE', 'QELead', 'FDE', 'Founding')
_cache = {}


def _read(name):
    import yaml
    path = ROOT / 'config' / name
    stamp = path.stat().st_mtime_ns
    previous = _cache.get(path)
    if previous is None or previous[0] != stamp:
        _cache[path] = (stamp, yaml.safe_load(path.read_text()) or {})
    return _cache[path][1]


def cvs():
    configured = _read('cvs.yml')
    return {key: dict(title=configured[key]['title'], url=configured[key].get('url') or '',
                      readiness=configured[key].get('readiness') or 'draft') for key in CV_KEYS}


def templates():
    """The owner's outreach template pack (config/templates.yml): the note box, the Templates tab and
    tools/note_context.py read the same templates and checks."""
    return _read('templates.yml')


def cv_key(value):
    if value is None:
        return None
    if value == 'HeadQE':
        return 'QE'
    if not isinstance(value, str) or value not in CV_KEYS:
        raise ValueError('Choose a registered CV or reset to the suggested CV')
    return value


@lru_cache(maxsize=16384)
def _board_scope(url):
    """The verified board scope an advert URL names, or None. A pure parse, so each URL is parsed once."""
    try:
        # Validate the actual host before interpreting a verified board path. Neither
        # lookalike domains nor company names in an arbitrary URL establish identity.
        provider, slug = normalise.board_identity(normalise.job_url(url))
    except ValueError:
        return None
    return f'board:{provider}:{slug}' if provider else None


def exclusions(records, links=(), sources=(), config=None, merges=()):
    """Exact employers, organisation names, declared board scope and explicit company links.

    A company exclusion never changes a person's relationship or spreads through an agency to
    its other clients. Keep the full graph available to constrain company-specific drafts.
    """
    rules = (config if config is not None else _read('relevance.yml')).get('exclusions') or []
    rows = {r['id']: r for r in records}
    scopes = {}
    for row in sources:
        scopes.setdefault(row['record_id'], set()).add(row['source_scope'])
    for rid, record in rows.items():
        if record['type'] == 'person':
            continue
        for field in ('url', 'detail_url', 'apply_url'):
            # job_url refuses anything but a string; caching needs one too.
            scope = _board_scope(record[field]) if record.get(field) and isinstance(record[field], str) else None
            if scope:
                scopes.setdefault(rid, set()).add(scope)
    out = {}
    for rule in rules:
        names = {normalise.org_key(n) for n in [rule['company'], *(rule.get('names') or [])]}
        boards = set(rule.get('source_scopes') or [])
        value = dict(company=rule['company'], reason=rule['reason'])
        for rid, record in rows.items():
            if record['type'] == 'person':
                continue
            named = normalise.org_key(record.get('organisation')) in names or normalise.org_key(record.get('name')) in names
            source = str(record.get('source_file') or '').removeprefix('source:')
            if named or source in boards or scopes.get(rid, set()) & boards:
                out[rid] = value
        # A recorded merge declares an alias, not a new employer. Follow its surviving name
        # before applying job links; ordinary links between organisations never do this.
        changed = True
        while changed:
            changed = False
            for source, target in merges:
                if (out.get(source) == value and target in rows and target not in out
                        and rows[target]['type'] in ('company', 'agency')):
                    out[target] = value
                    changed = True
        for link in links:
            if link.get('relation') not in ('hiring_for', 'advertised_by', 'buyer', 'related'):
                continue
            for a, b in ((link['from_id'], link['to_id']), (link['to_id'], link['from_id'])):
                if (a in rows and b in rows and rows[a]['type'] in ('job', 'tender', 'opportunity')
                        and rows[b]['type'] in ('company', 'agency') and out.get(b) == value):
                    out[a] = value
    return out
