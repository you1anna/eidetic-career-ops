#!/usr/bin/env python3
"""Measure on-demand intake comparison in a fresh process, with synthetic records only."""
import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,choices=(100,1000,3000),required=True)
    parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'workspace')]
    from benchmark_rebuild import build_fixture, fixed_clock
    if args.worker:
        import db
        import intake_review
        conn=db.connect(args.worker/'fixture.db')
        try:
            before=conn.total_changes
            with fixed_clock():
                started=time.perf_counter();result=intake_review.compare(conn,args.worker);elapsed=time.perf_counter()-started
            assert conn.total_changes==before,'comparison wrote to the database'
            print(json.dumps(dict(seconds=round(elapsed,3),peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                payload_bytes=len(json.dumps(result).encode()),assessed_new=result['assessed_new'],complete=result['complete'],
                excluded_shown=len(result['excluded']),maintained_shown=len(result['maintained']),read_only=True)))
        finally:conn.close()
        return
    with tempfile.TemporaryDirectory(prefix='workops-intake-benchmark-') as raw:
        root=Path(raw);build_fixture(root/'fixture.db',args.count,0,0)
        pending=root/'pending';pending.mkdir();items={}
        for i in range(args.count):
            scope='alert:linkedin';sid=str(92000000+i)
            items[str(i)]=dict(kind='job',source_scope=scope,source_id=sid,source_lifecycle='active',
                first_seen_at='2026-11-06T10:00:00Z',last_seen_at='2026-11-06T10:00:00Z',
                current=dict(title=f'Engineering Manager {i}',company=f'Synthetic Prospect {i}',eligible=True,
                    location='London',url=f'https://www.linkedin.com/jobs/view/{sid}/',
                    text='Lead software engineering teams and improve release readiness.'))
        (pending/'observations.json').write_text(json.dumps(dict(items=items,sources={})))
        subprocess.run([sys.executable,__file__,'--count',str(args.count),'--worker',raw],check=True)


if __name__=='__main__':main()
