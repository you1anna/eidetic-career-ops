#!/usr/bin/env python3
"""Measure historical status derivation or full approach projections on synthetic data.

Use --code-root with a saved Git archive for the baseline. Compare sha256 as well as
time/RSS; each invocation runs in its own process. No database or network access.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import resource
import statistics
import sys
import time


def fixture(count):
    records, outcomes, decisions = [], [], []
    for n in range(count):
        rid = f'P{n}'
        records.append(dict(id=rid, type='person', name=f'Synthetic person {n}',
                            relationship='warm', role='Engineering Director'))
        for h in range(8):
            action = f'action:{rid}:' if h % 3 else f'action:{rid}:other'
            outcomes.append(dict(id=n * 8 + h, record_id=rid, at=f'2026-10-{h+1:02}',
                kind=('sent', 'replied', 'submitted', 'unknown')[h % 4],
                corrected_at='2026-10-10' if h == 7 else None,
                snapshot={'action_id': action if h != 5 else None,
                          'brief': {'approach_route': {'kind': f'synthetic-{h}',
                                    'why': 'Recorded synthetic route', 'question': 'Synthetic question?'}}}))
        decisions.extend([dict(id=n * 2, record_id=rid, kind='draft', status='done', answer='hold'),
                          dict(id=n * 2 + 1, record_id=rid, kind='draft', status='pending')])
    return dict(records=records, links=[], outcomes=outcomes, decisions=decisions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--count', type=int, choices=(250, 1000, 3000), required=True)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--projection', action='store_true', help='Include advice, progress and weekly selection')
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    sys.path[:0] = [str(args.code_root / 'workspace'), str(args.code_root / 'tools')]
    import approaches
    import decision_intelligence
    import work_selection

    state, samples = fixture(args.count), []
    today = dt.date(2026, 10, 29)
    before = json.dumps(state, sort_keys=True)
    for _ in range(args.repeat):
        start = time.perf_counter()
        if args.projection:
            intel = decision_intelligence.build(state, today)
            assert len(intel['actions']) == args.count, 'Fixture must exercise current advice generation'
            intel = approaches.annotate(intel, state, today)
            result = dict(intelligence=intel, opportunities=work_selection.build(dict(state, intelligence=intel)))
        else:
            builder = decision_intelligence._Builder(state, today)
            result = [builder.status(dict(id=f'action:{r["id"]}:', stage='ready',
                                           readiness={}, brief={}), r, None) for r in state['records']]
        samples.append(round((time.perf_counter() - start) * 1000, 3))
    assert json.dumps(state, sort_keys=True) == before, 'Projection changed its inputs'
    encoded = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps(dict(records=args.count, outcomes=len(state['outcomes']), decisions=len(state['decisions']),
        projection=args.projection, samples_ms=samples, median_ms=statistics.median(samples),
        peak_rss_mib=round(peak / (1024**2 if sys.platform == 'darwin' else 1024), 2),
        bytes=len(encoded), sha256=hashlib.sha256(encoded).hexdigest())))


if __name__ == '__main__':
    main()
