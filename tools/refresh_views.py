#!/usr/bin/env python3
"""Publish a consistent set of readable private views from the canonical database."""
import argparse
import json
from pathlib import Path
import tempfile
import os
import wo


def refresh(conn, root):
    root=Path(root)
    outputs={}
    with tempfile.TemporaryDirectory() as tmp:
        csv=Path(tmp)/'directory.csv'
        # One read snapshot, including WAL, keeps JSON and CSV in agreement during edits.
        with wo.store.transaction(conn):
            wo.store.write_csv(conn,csv)
            outputs['career-directory.csv']=csv.read_text()
            outputs['records/db/work-ops.json']=json.dumps(wo.committed_dump(conn),ensure_ascii=False,indent=1,sort_keys=True)+'\n'
            outputs['records/career-brief.md']=wo.cmd_brief(conn,True)
    for relative,content in outputs.items():
        dest=root/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(dir=dest.parent,prefix='.'+dest.name)
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream: stream.write(content)
            os.replace(temp,dest)
        finally:
            Path(temp).unlink(missing_ok=True)
    return list(outputs)


def check(root):
    root=Path(root)
    payload=json.loads((root/'records/db/work-ops.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        version=payload.get('schema_version')
        migrations={int(p.name[:3]):p for p in wo.wodb.MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql')}
        if type(version) is not int or version not in migrations:
            raise ValueError('Export names an unsupported schema version')
        # Reconstruct the export's own schema first; strict load_dump must not accept mismatched
        # columns. Only this disposable copy is upgraded for the current projection afterwards.
        matching=Path(tmp)/'migrations';matching.mkdir()
        for number,migration in migrations.items():
            if number<=version:
                (matching/migration.name).write_text(migration.read_text())
        conn=wo.wodb.connect(Path(tmp)/'check.db');wo.wodb.migrate(conn,matching)
        try:
            wo.load_dump(conn,payload,partial=True)
            wo.wodb.migrate(conn)
            path=Path(tmp)/'current.csv';wo.store.write_csv(conn,path)
            if path.read_bytes()!=(root/'career-directory.csv').read_bytes():
                raise ValueError('CSV differs from the current structured JSON export; regenerate all views together')
            return conn.execute('SELECT count(*) FROM records').fetchone()[0]
        finally: conn.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=wo.DEFAULT_DB)
    parser.add_argument('--root',type=Path,default=wo.ROOT)
    parser.add_argument('--check',action='store_true')
    a=parser.parse_args()
    if a.check:
        print(f'{check(a.root)} records: current JSON/CSV parity verified');return
    if not a.db.is_file(): parser.error('Existing database required')
    conn=wo.wodb.connect(a.db)
    try: print('\n'.join(refresh(conn,a.root)))
    finally:conn.close()

if __name__=='__main__': main()
