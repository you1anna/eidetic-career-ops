"""Carry a consistent accounting snapshot inside the existing encrypted database backup.

Only the disposable backup receives this meta envelope; the working record DB never does.
The existing offsite transport and encryption format stay unchanged.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import collection_budget
from collection_reads import connect, SCHEMA

KEY='collection_backup_v1'
TABLES={'meta':2,'attempts':8,'tasks':4,'deferrals':4}

def capture(path):
    c=connect(path)
    try:
        c.execute('BEGIN')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Collection integrity failed')
        return dict(version=SCHEMA,tables={table:[list(r) for r in c.execute('SELECT * FROM '+table)] for table in TABLES})
    finally:c.close()

def embed(ledger,backup):
    value=capture(ledger)
    with sqlite3.connect(backup) as c:
        c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(KEY,json.dumps(value)))

def extract(backup):
    c=sqlite3.connect(Path(backup).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():return None
        r=c.execute('SELECT value FROM meta WHERE key=?',(KEY,)).fetchone()
    finally:c.close()
    if not r:return None
    value=json.loads(r[0])
    if not isinstance(value,dict) or value.get('version')!=SCHEMA or set(value.get('tables',{}))!=set(TABLES):raise ValueError('Invalid collection backup')
    for table,width in TABLES.items():
        rows=value['tables'][table]
        if not isinstance(rows,list) or any(not isinstance(row,list) or len(row)!=width for row in rows):raise ValueError('Invalid accounting rows')
    meta=dict(value['tables']['meta'])
    if not {'hold_until','clock'}<=set(meta):raise ValueError('Missing accounting recovery state')
    for key in ('hold_until','clock'):collection_budget.instant(meta[key])
    return value

def restore(backup,destination,*,hold_today=True,at=None):
    value=extract(backup)
    if value is None:raise ValueError('Backup has no collection accounting; initialize with a cutover hold')
    collection_budget.initialize(destination,at=at,hold_today=hold_today)
    with sqlite3.connect(destination) as c:
        hold=c.execute("SELECT value FROM meta WHERE key='hold_until'").fetchone()[0]
        for table,width in TABLES.items():
            c.execute('DELETE FROM '+table)
            c.executemany('INSERT INTO '+table+' VALUES('+','.join('?'*width)+')',value['tables'][table])
        if hold_today:
            c.execute("UPDATE meta SET value=MAX(value,?) WHERE key='hold_until'",(hold,))
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restored accounting integrity failed')
    return value

def verify(backup):
    if extract(backup) is None:return False # historical backups predate this feature
    with tempfile.TemporaryDirectory(prefix='collection-restore-check-') as tmp:
        target=Path(tmp)/'collection.db'
        value=restore(backup,target,hold_today=False)
        if capture(target)!=value:raise ValueError('Restored collection accounting differs')
    return True

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('ledger',type=Path);ap.add_argument('backup',type=Path)
    args=ap.parse_args();embed(args.ledger,args.backup)
