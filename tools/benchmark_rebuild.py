#!/usr/bin/env python3
"""Measure summary rebuilds on a disposable mixed synthetic fixture.

The parent creates the fixture with the current checkout's shared benchmark builder.
Fresh children load --code-root, so fixture construction and profiling cannot inflate
the reported measurement-process timings/RSS. Verification runs in a separate child.

Example: .venv/bin/python tools/benchmark_rebuild.py --count 1000 --people 1000
         --history 8 --repeat 3 --code-root /path/to/saved/archive --queries
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import itertools
import json
from pathlib import Path
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
STAMP = '2026-11-06T12:00:00+00:00'
DAY = '2026-11-06'
_DATE, _DATETIME = dt.date, dt.datetime


def progress(label, started=None):
    elapsed = f' ({time.perf_counter() - started:.2f}s)' if started is not None else ''
    print(f'benchmark_rebuild: {label}{elapsed}', file=sys.stderr, flush=True)


class FixedDate(_DATE):
    @classmethod
    def today(cls):
        return cls(2026, 11, 6)


class FixedDateTime(_DATETIME):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


@contextmanager
def fixed_clock():
    # Modules share datetime; freeze every date/availability clock, leaving monotonic
    # and perf_counter real. The fixture and both source revisions see the same instant.
    with patch.object(dt, 'date', FixedDate), patch.object(dt, 'datetime', FixedDateTime):
        yield


def code_modules(root):
    sys.path[:0] = [str(root / 'workspace'), str(root / 'tools')]
    import db
    import records
    import workspace_state
    if Path(workspace_state.__file__).resolve().parent != (root / 'workspace').resolve():
        raise RuntimeError('A code root requires a fresh process')
    return db, records, workspace_state


def build_fixture(destination, count, people, history):
    setup_started = time.perf_counter()
    progress(f'building {count} synthetic jobs and {count * history} historical observations')
    db, records, _ = code_modules(ROOT)
    from benchmark_week import build_fixture as jobs_fixture
    sequence = itertools.count(1)
    conn = db.connect(destination)
    try:
        with fixed_clock(), patch.object(db, 'now', return_value=STAMP), \
                patch.object(uuid, 'uuid4', side_effect=lambda: uuid.UUID(int=next(sequence) << 80)):
            db.migrate(conn)
            jobs = jobs_fixture(conn, records, count, history, DAY)
            progress('jobs built; adding people, links and interactions', setup_started)
            # db.connect uses autocommit: ``with conn`` alone does not begin a
            # transaction, and interaction() would then return an enriched record.
            conn.execute('BEGIN')
            with conn:
                for job in jobs[:count // 10]:
                    revision = conn.execute('SELECT revision FROM records WHERE id=?', (job,)).fetchone()[0]
                    records.transition(conn, job, dict(revision=revision, field='status', to='pursue'))
                organisations = {row['name']: row['id'] for row in conn.execute("SELECT id,name FROM records WHERE type='company'")}
                person_rows, links = [], []
                for number in range(people):
                    relationship = ('cold', 'none', 'dormant', 'warm', 'active', 'cold', 'none', 'dormant', 'cold', 'do_not_contact')[number % 10]
                    person = f'CP-benchmark-{number:06}'
                    organisation = f'Synthetic employer {number % max(1, count // 10)}'
                    person_rows.append((person, 'person', f'Synthetic contact {number}', organisation,
                        'Engineering Director', relationship, 'first' if number % 20 == 0 else None, STAMP, STAMP, 3))
                    links.extend([(person, organisations[organisation], 'works_at', 'Linked from person organisation'),
                        (person, jobs[number % count], 'related', '')])
                # Synthetic setup only: enriched create/link return values load the growing
                # directory and make fixture construction quadratic. Insert known valid rows
                # once, preserving canonical created/link events, edited fields and revisions.
                conn.executemany('INSERT INTO records(id,type,name,organisation,role,relationship,priority,created_at,updated_at,revision) '
                                 'VALUES(?,?,?,?,?,?,?,?,?,?)', person_rows)
                conn.executemany('INSERT INTO links(from_id,to_id,relation,notes) VALUES(?,?,?,?)', links)
                for row in person_rows:
                    person = row[0]
                    records.mark_edited(conn, person, ('name','organisation','role','relationship','priority'), actor='agent')
                    records.event(conn, person, 'created', None, 'person', actor='agent')
                for person, target, relation, _ in links:
                    records.event(conn, person, 'link', None, target, relation, actor='agent')
                for row in person_rows:
                    person = row[0]
                    for ordinal in range(8):
                        records.interaction(conn, dict(record_id=person,
                            revision=3 + ordinal, at=f'2026-10-{20 + ordinal:02}T10:00:00+00:00',
                            channel='email', direction='in' if ordinal % 2 else 'out',
                            summary=f'Synthetic engineering conversation {ordinal}'), actor='agent')
            metadata = dict(jobs=jobs, candidates=count, people=people, history_per_job=history,
                records=conn.execute('SELECT COUNT(*) FROM records').fetchone()[0],
                links=conn.execute('SELECT COUNT(*) FROM links').fetchone()[0],
                interactions=conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0],
                observations=conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0],
                pursued_jobs=count // 10,
                relationships=dict(conn.execute("SELECT relationship,COUNT(*) FROM records WHERE type='person' GROUP BY relationship").fetchall()),
                day=DAY)
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            assert not conn.execute('PRAGMA foreign_key_check').fetchall(), 'Synthetic fixture violates foreign keys'
            progress('fixture ready', setup_started)
            return metadata
    finally:
        conn.close()


def normalized(value):
    return {key: item for key, item in value.items() if key not in ('version', 'loaded')}


def digest(value):
    return hashlib.sha256(json.dumps(normalized(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def apply_delta(previous, delta):
    if not delta.get('delta'):
        return delta
    assert delta['base'] == previous['version'], 'Delta does not match its preceding summary'
    result = copy.deepcopy(previous)
    for key in delta['unset']:
        result.pop(key, None)
    result.update(delta['set'])
    intelligence = result['intelligence']
    for key in delta['intelligence']['unset']:
        intelligence.pop(key, None)
    intelligence.update(delta['intelligence']['set'])
    for key, items in (('records', result['records']), ('actions', intelligence['actions']), ('research', intelligence['research'])):
        change = delta['collections'][key]
        indexed = {item['id']: item for item in items}
        for removed in change['remove']:
            del indexed[removed]
        for item in change['upsert']:
            indexed[item['id']] = item
        rebuilt = [indexed[item] for item in change.get('order', [item['id'] for item in items])]
        (result if key == 'records' else intelligence)[key] = rebuilt
    result['version'] = delta['version']
    return result


def full_version(body):
    # Production full summaries append the version last. Avoid decoding/retaining a
    # full state dictionary in the timed child merely to select its next delta.
    match = re.search(rb',"version":"([^"\\]+)"\}$', body)
    if not match:
        raise AssertionError('Expected a full summary with its trailing version')
    return match[1].decode()


def worker(args):
    directory = Path(args.worker)
    metadata = json.loads((directory / 'fixture.json').read_text())
    database = directory / f'{args.mode}.db'
    shutil.copyfile(directory / 'fixture.db', database)
    db, records, workspace_state = code_modules(args.code_root.resolve())
    import request_timings
    conn = db.connect(database)
    if db.pending(conn):
        raise RuntimeError('The saved code root does not support the fixture schema')
    profiler = None
    if args.mode == 'profile':
        import cProfile
        profiler = cProfile.Profile()
    query_counts, hashes, samples, writes, sizes = [], [], [], [], []
    def read(since=None):
        queries = []
        before = conn.total_changes
        if args.mode == 'verify' and args.queries:
            conn.set_trace_callback(queries.append)
        try:
            started = time.perf_counter()
            response = workspace_state.summary_json(conn, since)
            elapsed = (time.perf_counter() - started) * 1000
        finally:
            conn.set_trace_callback(None)
        assert conn.total_changes == before, 'A summary read mutated the database'
        if args.mode == 'verify' and args.queries:
            query_counts.append(len(queries))
            assert not any(query.lstrip().split(' ', 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE') for query in queries)
        return response, elapsed
    try:
        with fixed_clock(), patch.object(db, 'now', return_value=STAMP):
            request_timings.clear()
            if profiler:
                profiler.enable()
            (raw, compressed), cold = read()
            cold_timing = request_timings.snapshot()['kinds']['build']
            assert cold_timing['count'] == 1, 'The cold summary must build once'
            request_timings.clear()
            version = full_version(raw)
            full_bytes, gzip_bytes = len(raw), len(compressed)
            if args.mode == 'verify':
                current = json.loads(raw)
                assert len(current['records']) == metadata['records']
                assert sum(record['type'] == 'person' for record in current['records']) == metadata['people']
                assert not current.get('relevance_error') and not current.get('intelligence_error')
                hashes.append(digest(current))
            del raw, compressed
            for number in range(args.repeat):
                rid = metadata['jobs'][number % len(metadata['jobs'])]
                revision = records.get(conn, rid)['revision']
                started = time.perf_counter()
                note = f'Synthetic rebuild measurement {number}'
                records.edit(conn, rid, dict(revision=revision, notes=note), actor='agent')
                writes.append((time.perf_counter() - started) * 1000)
                (raw, _), elapsed = read(version)
                change = json.loads(raw)
                assert change.get('delta') is True, 'A committed note edit must return a delta'
                assert change['base'] == version and change['version'] != version
                version = change['version']
                samples.append(elapsed); sizes.append(len(raw))
                if args.mode == 'verify':
                    current = apply_delta(current, change)
                    # A cache hit is the exact full counterpart of this delta. Exclude it
                    # from both timing and traced rebuild counts.
                    full = json.loads(workspace_state.summary_json(conn)[0])
                    assert current == full, 'Applying a delta differs from its full summary'
                    assert next(record for record in full['records'] if record['id'] == rid)['notes'] == note
                    assert not full.get('relevance_error') and not full.get('intelligence_error')
                    hashes.append(digest(full))
                del raw, change
            if profiler:
                profiler.disable()
                profiler.dump_stats(args.profile)
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            timing = request_timings.snapshot()['kinds']['build']
            result = dict(cold_full_ms=round(cold, 3), invalidated_ms=[round(value, 3) for value in samples],
                invalidated_median_ms=round(statistics.median(samples), 3),
                write_ms=[round(value, 3) for value in writes],
                full_bytes=full_bytes, full_gzip_bytes=gzip_bytes, delta_bytes=sizes,
                peak_rss_mib=round(peak / (1024**2 if sys.platform == 'darwin' else 1024), 2),
                build_count=cold_timing['count'] + timing['count'],
                cold_build_phase_ms=cold_timing.get('phase_median_ms', {}),
                invalidated_build_phase_median_ms=timing.get('phase_median_ms', {}))
            assert timing['count'] == args.repeat, 'Each committed change must rebuild once'
            if args.mode == 'verify':
                result = dict(sha256=hashes, full_delta_equal=True, read_only=True, query_counts=query_counts)
            print(json.dumps(result))
    finally:
        conn.close()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root', type=Path, default=ROOT)
    parser.add_argument('--count', type=int, choices=(100, 1000, 3000), required=True)
    parser.add_argument('--people', type=int, help='Default: same as --count; eight interactions per person')
    parser.add_argument('--history', type=int, choices=(0, 8), default=8)
    parser.add_argument('--repeat', type=int, default=3, help='1–50 invalidated builds (the timing sample window)')
    parser.add_argument('--queries', action='store_true', help='Count SQL in the separate verification child')
    parser.add_argument('--profile', type=Path, help='Write cProfile from a separate diagnostic child')
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--mode', choices=('measure', 'verify', 'profile'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.people is None:
        args.people = args.count
    if args.people < 0 or not 1 <= args.repeat <= 50:
        parser.error('--people must be nonnegative and --repeat must be between 1 and 50')
    if not (args.code_root / 'workspace' / 'workspace_state.py').is_file():
        parser.error('--code-root must contain workspace/workspace_state.py')
    if args.profile:
        args.profile = args.profile.resolve()
    return args


def main(argv=None):
    args = arguments(argv)
    if args.worker:
        worker(args)
        return
    with tempfile.TemporaryDirectory(prefix='workops-rebuild-') as raw:
        directory = Path(raw)
        metadata = build_fixture(directory / 'fixture.db', args.count, args.people, args.history)
        (directory / 'fixture.json').write_text(json.dumps(metadata))
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(directory),
            '--code-root', str(args.code_root.resolve()), '--count', str(args.count),
            '--people', str(args.people), '--history', str(args.history), '--repeat', str(args.repeat)]
        def run(mode, extra=()):
            started = time.perf_counter()
            progress(f'starting isolated {mode} child')
            try:
                completed = subprocess.run([*command, '--mode', mode, *extra], check=True, text=True, capture_output=True)
            except subprocess.CalledProcessError as error:
                print(error.stderr, file=sys.stderr, end='', flush=True)
                raise
            progress(f'{mode} child complete', started)
            return json.loads(completed.stdout)
        measured = run('measure')
        verified = run('verify', ['--queries'] if args.queries else [])
        if args.profile:
            run('profile', ['--profile', str(args.profile)])
        print(json.dumps(dict(code_root=str(args.code_root.resolve()),
            fixture={key: value for key, value in metadata.items() if key != 'jobs'},
            measurement=measured, verification=verified,
            profile=str(args.profile) if args.profile else None), indent=2))


if __name__ == '__main__':
    main()
