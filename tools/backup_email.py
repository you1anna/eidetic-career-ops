#!/usr/bin/env python3
"""Snapshot mail and its reader state together while the caller holds the ingest writer lock."""
import argparse
from pathlib import Path
import sqlite3
import tarfile
import tempfile

def snapshot(data,output):
 data=Path(data);spool=data/'email'/'mail.db'
 if not spool.exists():return False
 with tempfile.TemporaryDirectory(prefix='email-backup-',dir=data/'backups') as scratch:
  target=Path(scratch)/'mail.db'
  source=sqlite3.connect(spool.as_uri()+'?mode=ro',uri=True)
  dest=sqlite3.connect(target)
  target.chmod(0o600)
  try:source.backup(dest)
  finally:dest.close();source.close()
  with tarfile.open(output,'w:gz') as archive:
   archive.add(target,arcname='email/mail.db')
   for name in ('pending/alerts','pending/observations.json'):
    path=data/name
    if path.exists():archive.add(path,arcname=name)
 return True

if __name__=='__main__':
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('data',type=Path);ap.add_argument('output',type=Path)
 args=ap.parse_args();snapshot(args.data,args.output)
