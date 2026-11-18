#!/usr/bin/env python3
"""Measure scan import time and SQLite writer hold with synthetic unrelated history.

Use --code-root with a saved Git archive for a baseline. Each run owns and removes
its database. This measures local process timings/RSS, not hosted contention.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--history', type=int, default=10000)
    parser.add_argument('--items', type=int, choices=(0, 1), default=1)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    if args.history < 0 or args.repeat < 1:
        parser.error('--history must be nonnegative and --repeat must be positive')
    sys.path[:0] = [str(args.code_root / 'workspace'), str(args.code_root / 'tools')]
    import db
    import evidence_checks
    import record_imports

    stamp = '2026-11-06T12:00:00Z'
    with tempfile.TemporaryDirectory(prefix='workops-import-benchmark-') as directory:
        folder = Path(directory)
        conn = db.connect(folder / 'fixture.db')
        try:
            db.migrate(conn)
            with conn:
                conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES"
                             "('CO-history','company','Synthetic history company',?,?)", (stamp, stamp))
                conn.executemany('INSERT INTO observations(record_id,source,observed_at,payload_json,payload_sha) '
                                 'VALUES(?,?,?,?,?)',
                    (('CO-history', 'fixture:history', stamp,
                      json.dumps({'kind': 'research', 'text': 'x' * 4096, 'ordinal': n}), str(n))
                     for n in range(args.history)))
            item = dict(source_scope='hn:benchmark', source_id='1', kind='job',
                        current=dict(title='Engineering Manager', company='Synthetic employer',
                                     url='https://example.org/synthetic-job', location='London', eligible=True),
                        last_seen_at=stamp, source_lifecycle='active')
            data = dict(generated_at=stamp, sources={}, items={'job': item} if args.items else {})
            (folder / 'observations.json').write_text(json.dumps(data))
            total, held = [], []
            transaction = record_imports.store.transaction
            @contextmanager
            def measured_transaction(connection):
                outer = connection.in_transaction
                started = None
                try:
                    with transaction(connection):
                        if not outer:
                            started = time.perf_counter()  # BEGIN has acquired the writer lock.
                        yield
                finally:
                    if started is not None:
                        held.append((time.perf_counter() - started) * 1000)  # Includes commit/rollback.
            with patch.object(db, 'now', return_value=stamp), patch.object(evidence_checks, 'utc_now',
                    return_value=dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)):
                record_imports.cmd_import_scans(conn, folder)  # Warm caches and retain the one synthetic role.
                with patch.object(record_imports.store, 'transaction', measured_transaction):
                    for _ in range(args.repeat):
                        start = time.perf_counter()
                        report = record_imports.cmd_import_scans(conn, folder)
                        total.append((time.perf_counter() - start) * 1000)
            assert len(held) == args.repeat, 'Each import must use exactly one writer transaction'
            assert report['observed'] == args.items and report['new_observed'] == 0
            assert conn.execute("SELECT COUNT(*) FROM observations WHERE source='fixture:history'").fetchone()[0] == args.history
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            print(json.dumps(dict(history_rows=args.history, payload_text_bytes=4096, imported_items=args.items,
                samples=len(total), median_total_ms=round(statistics.median(total), 3),
                median_write_hold_ms=round(statistics.median(held), 3),
                peak_process_rss_mib=round(peak / (1024**2 if sys.platform == 'darwin' else 1024), 2))))
        finally:
            conn.close()


if __name__ == '__main__':
    main()
