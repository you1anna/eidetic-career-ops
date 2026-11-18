#!/usr/bin/env python3
"""Compare weekly projections using disposable synthetic data, never a live database.

Run the same script against a saved Git archive with --code-root to establish a baseline.
Every invocation is a separate process so peak RSS measurements are comparable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time


def build_fixture(conn, records, count, history, today):
    """Synthetic employers, adverts and history on a migrated disposable database; returns job ids.
    Shared by the weekly and save benchmarks so both measure the same data."""
    at = today + 'T06:00:00Z'
    logo = 'https://media.licdn.com/dms/image/v2/D4E0BAQ%03d/company-logo_200_200/company-logo_200_200/0/123456789?e=2145916800&v=beta&t=synthetic-%s'
    # Fixture setup is a single local transaction on a disposable database. Production
    # edit timings below run separately through the normal short canonical transaction.
    conn.execute('BEGIN')
    orgs = []
    for i in range(max(1, count // 10)):
        orgs.append(records.create(conn, dict(type='company', name=f'Synthetic employer {i}'))['record']['id'])
    jobs = []
    for i in range(count):
        company = f'Synthetic employer {i // 10}'
        title = f'Engineering Manager {i}'
        body = ('Lead software engineering teams and improve delivery quality. '
                'Essential: experience leading engineering teams. '
                'Desirable: familiarity with a regulated industry. '
                f'The team owns synthetic platform {i} and its release process.')
        url = f'https://jobs.example.com/{i}'
        rid = records.create(conn, dict(type='job', name=title, organisation=company,
            engagement='contract' if i % 2 else 'permanent', status='candidate',
            location='London', pay='£750/day' if i % 2 else '£100,000/year', date=today,
            summary=body, url=url))['record']['id']
        jobs.append(rid)
        records.link(conn, dict(from_id=rid, to_id=orgs[i // 10], relation='hiring_for',
                                revision=records.get(conn, rid)['revision']))
        records.add_observation(conn, rid, 'feed:adzuna', at, json.dumps(dict(current=dict(
            title=title, company=company, text=body, url=url,
            employment_type='contract' if i % 2 else 'permanent', date=today))))
        records.add_observation(conn, rid, 'alert:linkedin', at, json.dumps(dict(current=dict(
            title=title, company=company, url=f'https://www.linkedin.com/jobs/view/{100000+i}/',
            received_at=at, company_logo_url=logo % (i // 10, str(i // 10).zfill(96))))))
    # Synthetic historical observations grow independently of the current candidate set.
    # Canonical observation writes keep fixture setup out of the measured read path.
    with conn:
        for i, rid in enumerate(jobs):
            for h in range(history):
                records.add_observation(conn, rid, 'alert:linkedin', f'2025-01-{h+1:02}T06:00:00Z',
                    json.dumps(dict(current=dict(company=f'Synthetic employer {i // 10}',
                        text='Archived fixture wording ' * 40,
                        company_logo_url=logo % (i // 10, f'history-{h}')))))

    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--count', type=int, choices=(100, 1000, 3000), required=True)
    parser.add_argument('--history', type=int, choices=(0, 8), default=0)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--baseline-raw-bytes', type=int, help='Fail if browser raw payload grows over 10%%')
    parser.add_argument('--baseline-gzip-bytes', type=int, help='Fail if browser gzip payload grows over 10%%')
    args = parser.parse_args()
    sys.path[:0] = [str(args.code_root / 'workspace'), str(args.code_root / 'tools')]
    import db
    import records
    import workspace_state

    today = dt.date.today().isoformat()
    with tempfile.TemporaryDirectory(prefix='workops-week-measure-') as temp:
        conn = db.connect(Path(temp) / 'synthetic.db')
        try:
            db.migrate(conn)
            jobs = build_fixture(conn, records, args.count, args.history, today)
            samples, query_counts = [], []
            for _ in range(args.repeat):
                queries = []
                before = conn.total_changes
                conn.set_trace_callback(queries.append)
                start = time.perf_counter()
                summary = workspace_state.state(conn, compact=True, receipt_history=False)
                samples.append(round((time.perf_counter() - start) * 1000, 2))
                conn.set_trace_callback(None)
                assert conn.total_changes == before, 'A summary read wrote data'
                assert not summary.get('relevance_error'), summary.get('relevance_error')
                assert not summary.get('intelligence_error'), summary.get('intelligence_error')
                query_counts.append(len(queries))
            raw = json.dumps(summary, separators=(',', ':')).encode()
            compressed_bytes = len(gzip.compress(raw, compresslevel=5, mtime=0))
            # Measure the actual opt-in full browser response, including version and table.
            # Older code roots retain their ordinary JSON baseline without this module.
            transport_path = args.code_root / 'workspace' / 'summary_transport.py'
            wire_started = time.perf_counter()
            if transport_path.is_file():
                import summary_transport
                wire = summary_transport.encode({**summary, 'version': '12345678-1'})
            else:
                wire = raw
            wire_gzip = len(gzip.compress(wire, compresslevel=5, mtime=0))
            wire_ms = round((time.perf_counter() - wire_started) * 1000, 2)
            if args.baseline_raw_bytes:
                assert len(wire) <= args.baseline_raw_bytes * 1.1, 'Raw browser payload exceeds 10% growth'
            if args.baseline_gzip_bytes:
                assert wire_gzip <= args.baseline_gzip_bytes * 1.1, 'Gzip browser payload exceeds 10% growth'
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            logo_companies = len(summary.get('company_logos') or {})
            # Isolate the optional logo transport from the new suitability evidence. No image
            # request occurs: these are JSON bytes only, not browser bandwidth or decode cost.
            summary.pop('company_logos', None)
            for record in summary['records']:
                record.pop('logo_company_id', None)
            without_logos = json.dumps(summary, separators=(',', ':')).encode()
            writes = []
            for n in range(3):
                row = records.get(conn, jobs[0])
                start = time.perf_counter()
                records.edit(conn, jobs[0], dict(revision=row['revision'], notes=f'Synthetic measurement {n}'))
                writes.append(round((time.perf_counter() - start) * 1000, 2))
            print(json.dumps(dict(candidates=args.count, historical_observations=args.count * args.history,
                records=len(summary['records']), query_counts=query_counts, read_ms=samples,
                read_median_ms=round(statistics.median(samples), 2), raw_bytes=len(raw),
                gzip_bytes=compressed_bytes,
                browser_raw_bytes=len(wire), browser_gzip_bytes=wire_gzip, transport_ms=wire_ms,
                logo_raw_bytes=len(raw)-len(without_logos),
                logo_gzip_bytes=compressed_bytes-len(gzip.compress(without_logos, compresslevel=5, mtime=0)),
                peak_rss_mib=round(peak / (1024**2 if sys.platform == 'darwin' else 1024), 2),
                write_ms=writes, logo_companies=logo_companies,
                discovery=len(summary['opportunities']['discovery']))))
        finally:
            conn.close()


if __name__ == '__main__':
    main()
