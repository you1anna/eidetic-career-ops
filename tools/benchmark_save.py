#!/usr/bin/env python3
"""Time what a save costs before the page is usable again, on disposable synthetic data.

A browser save is the POST (the canonical write) followed by the summary delta request, whose
server side rebuilds the whole projection. This times both, through the production functions,
for a note edit and a job status move, after one cold build (as after a restart or a background
receipt). Phase splits come from `request_timings` where the code root has it, so an older Git
archive passed as `--code-root` still gives end-to-end figures for comparison.

Never opens a live database. Synthetic records are simpler than real ones (no people, decisions or
interactions), so these figures bound the server cost of ranking and transport, not hosted latency.
Usage: benchmark_save.py --count 1000 [--history 8] [--repeat 5] [--code-root DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time


def version_of(body: bytes) -> str:
    data = json.loads(body)
    return data.get('version') or data['value']['version']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--count', type=int, choices=(100, 1000, 3000), required=True)
    parser.add_argument('--history', type=int, choices=(0, 8), default=0)
    parser.add_argument('--repeat', type=int, default=5)
    args = parser.parse_args()
    sys.path[:0] = [str(args.code_root / 'workspace'), str(args.code_root / 'tools')]
    import db
    import records
    import workspace_state
    from benchmark_week import build_fixture
    try:
        import request_timings
    except ImportError:
        request_timings = None

    ms = lambda started: round((time.perf_counter() - started) * 1000, 1)
    today = dt.date.today().isoformat()
    with tempfile.TemporaryDirectory(prefix='workops-save-measure-') as temp:
        conn = db.connect(Path(temp) / 'synthetic.db')
        try:
            db.migrate(conn)
            jobs = build_fixture(conn, records, args.count, args.history, today)
            started = time.perf_counter()
            workspace_state.summary_json(conn, None, 'strings-v1')
            cold_ms = ms(started)
            version = version_of(workspace_state.summary_json(conn)[0])
            if request_timings:
                request_timings.clear()
            results = {'note_edit': [], 'status_move': []}
            for n in range(args.repeat):
                for kind in results:
                    rid = jobs[n] if kind == 'note_edit' else jobs[-1 - n]
                    revision = records.get(conn, rid)['revision']
                    started = time.perf_counter()
                    if kind == 'note_edit':
                        records.edit(conn, rid, dict(revision=revision, notes=f'Synthetic measurement {n}'))
                    else:
                        records.transition(conn, rid, dict(revision=revision, field='status', to='pursue'))
                    write = ms(started)
                    started = time.perf_counter()
                    body, _ = workspace_state.summary_json(conn, version, 'strings-v1')
                    refresh = ms(started)
                    version = version_of(body)
                    results[kind].append(dict(write_ms=write, refresh_ms=refresh, delta_bytes=len(body)))
            build = request_timings.snapshot()['kinds']['build'] if request_timings else {}
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            median = lambda rows, key: round(statistics.median(r[key] for r in rows), 1)
            print(json.dumps(dict(
                candidates=args.count, historical_observations=args.count * args.history,
                repeat=args.repeat, cold_full_ms=cold_ms,
                **{f'{kind}_{key}': median(rows, key) for kind, rows in results.items()
                   for key in ('write_ms', 'refresh_ms', 'delta_bytes')},
                build_median_ms=build.get('median_ms'), build_phase_median_ms=build.get('phase_median_ms'),
                peak_rss_mib=round(peak / (1024**2 if sys.platform == 'darwin' else 1024), 1))))
        finally:
            conn.close()


if __name__ == '__main__':
    main()
