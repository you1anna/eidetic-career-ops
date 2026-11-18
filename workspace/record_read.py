"""Shared read boundaries and exclusion policy; no mutation or UI service dependencies."""
from contextlib import contextmanager
import search_config


def record_policy(conn):
    """Read policy against the complete stored graph, including retained company history."""
    merges = [(r['entity_id'], r['to_value']) for r in conn.execute(
        "SELECT e.entity_id,e.to_value FROM state_events e JOIN records r ON r.id=e.entity_id "
        "WHERE r.archived_at IS NOT NULL AND r.type IN ('company','agency') AND e.field='archived' "
        "AND e.id=(SELECT MAX(a.id) FROM state_events a WHERE a.entity_id=e.entity_id AND a.field='archived') "
        "AND EXISTS(SELECT 1 FROM state_events m WHERE m.entity_id=e.to_value AND m.field='merged' AND m.from_value=e.entity_id)")]
    records = [dict(r) for r in conn.execute('SELECT id,type,name,organisation,source_file,url,detail_url,apply_url FROM records')]
    links = [dict(r) for r in conn.execute('SELECT from_id,to_id,relation FROM links')]
    excluded = search_config.exclusions(records, links,
        [dict(r) for r in conn.execute('SELECT record_id,source_scope FROM source_records')], merges=merges)
    contexts = {}
    for link in links:
        for rid, other in ((link['from_id'], link['to_id']), (link['to_id'], link['from_id'])):
            if other in excluded:
                contexts.setdefault(rid, {})[other] = dict(record_id=other, **excluded[other])
    return excluded, {rid: list(values.values()) for rid, values in contexts.items()}



def record_exclusions(conn):
    return record_policy(conn)[0]



@contextmanager
def snapshot(conn):
    if conn.in_transaction:
        yield
        return
    conn.execute('BEGIN')
    try:
        yield
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
