"""Explicit deployment/recovery initialization, never an automatic dispatch fallback."""
import argparse
from pathlib import Path
import collection_budget
from backup_collection import restore

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['initialize','restore'])
    ap.add_argument('--path',type=Path,default=collection_budget.AUTHORITY)
    ap.add_argument('--backup',type=Path)
    args=ap.parse_args()
    if args.command=='initialize':collection_budget.initialize(args.path)
    else:
        if not args.backup:ap.error('--backup is required')
        restore(args.backup,args.path)
    print('Accounting created; API reads held until the next London day. Timers unchanged.')
