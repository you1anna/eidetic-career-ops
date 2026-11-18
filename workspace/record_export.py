"""The flat CSV export of records and their links (career-directory.csv)."""
from __future__ import annotations

from pathlib import Path
import csv

import record_views
from record_core import PERSON_CHOICES


def csv_rows(conn):
    """Export flat records and their links from one snapshot with a fixed three reads."""
    with record_views.snapshot(conn):
        rows = sorted(record_views.flat_records(conn), key=lambda r: (r['type'], r['id']))
        linked = {}
        for link in conn.execute(
            'SELECT l.from_id AS record_id,l.to_id AS related_id,n.name FROM links l '
            'JOIN records r ON r.id=l.from_id LEFT JOIN records n ON n.id=l.to_id WHERE r.archived_at IS NULL '
            'UNION SELECT l.to_id,l.from_id,n.name FROM links l '
            'JOIN records r ON r.id=l.to_id LEFT JOIN records n ON n.id=l.from_id WHERE r.archived_at IS NULL '
            'ORDER BY record_id,related_id'):
            if link['name'] is None:
                raise ValueError('Unknown linked record: ' + link['related_id'])
            linked.setdefault(link['record_id'], []).append(link)
        for r in rows:
            f = r.pop('followup')
            # Pre-009 (and pre-012) databases remain readable: an absent choice projects as blank
            # without migrating the source, matching the current CSV rebuilt from their JSON export.
            for k in ('cv_override', *PERSON_CHOICES):
                r.setdefault(k, None)
            for k in ('created_at', 'updated_at', 'archived_at', 'revision'):
                r.pop(k, None)
            r['related_ids'] = '; '.join(x['related_id'] for x in linked.get(r['id'], ()))
            r['related_names'] = '; '.join(x['name'] for x in linked.get(r['id'], ()))
            r['status'] = r.get('stage') or r.get('status') or r.get('relationship') or ''
            r['action'] = f['action'] if f else r.get('next_action', '')
            r['action_state'] = r['working_state']
            r['action_owner'] = r.get('owner') or ''
        return [{k: '\n'.join(line.rstrip() for line in v.splitlines()).strip() if isinstance(v, str) else v
                 for k, v in r.items()} for r in rows]


def write_csv(conn, path):
    rows=csv_rows(conn);path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    columns=['id','type','name','organisation','role','status','working_state','status_reason','waiting_on','action']
    columns+=sorted(set().union(*(r.keys() for r in rows))-set(columns)) if rows else []
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns,lineterminator='\n');writer.writeheader();writer.writerows(rows)
    return len(rows)
