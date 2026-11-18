#!/usr/bin/env python3
"""Measure the page summary on a private copy of an existing database, for one code root.

The synthetic benchmarks (`benchmark_rebuild.py`, `benchmark_save.py`) fix the workload; this
one measures a real-shaped store, where each record carries more evidence. It never opens the
database it is given: a consistent copy is made in a private temporary folder, a fresh child
process loads `--code-root` against that copy, and the copy is removed afterwards.

Output is one JSON object of aggregates only: counts, sizes, timings and digests. It never
prints a record's name, text, link or id, so results can be kept in a plan.

Compare two revisions by running it once per code root on the same day (ranking depends on the
London date) and comparing `digests`: equal digests mean identical summary and full-state output.

Example: git archive <baseline> | tar -x -C /private/tmp/baseline
         .venv/bin/python tools/benchmark_copy.py --db data/<copy>.db --code-root /private/tmp/baseline
         .venv/bin/python tools/benchmark_copy.py --db data/<copy>.db --payload
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
GROUPS = ('discovery', 'shortlisted', 'active', 'closed', 'other')


def private_copy(source: Path, folder: Path) -> Path:
    """A consistent copy, WAL included, read through a read-only connection."""
    if not source.is_file():
        raise ValueError('Database does not exist')
    target = folder / 'copy.db'
    reader = sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()
    target.chmod(0o600)
    return target


def normalized(value: dict) -> dict:
    """Everything except the per-process transport version and the load timestamp."""
    return {key: item for key, item in value.items() if key not in ('version', 'loaded')}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def size(value) -> int:
    return len(json.dumps(value, separators=(',', ':')))


def unpack(envelope: dict):
    """Independent reference decoder for the browser's string-sharing form."""
    strings = envelope['strings']

    def read(value):
        if isinstance(value, str) and value.startswith('~'):
            return value[1:] if value.startswith('~~') else strings[int(value[1:])]
        if isinstance(value, list):
            return [read(item) for item in value]
        if isinstance(value, dict):
            return {key: read(item) for key, item in value.items()}
        return value

    return read(envelope['value'])


def payload_shape(summary: dict) -> dict:
    """Where the page payload goes: sections, record fields and placement groups, in bytes."""
    records = {record['id']: record for record in summary.get('records') or []}
    fields, by_type = collections.Counter(), collections.Counter()
    for record in records.values():
        by_type[record.get('type') or ''] += size(record)
        for key, value in record.items():
            fields[key] += size(value) + len(key) + 4
    groups, rules = {}, collections.Counter()
    opportunities = summary.get('opportunities') or {}
    for group in GROUPS:
        references = opportunities.get(group) or []
        groups[group] = dict(records=len(references),
                             bytes=sum(size(records[ref['record_id']]) for ref in references
                                       if ref.get('record_id') in records))
        for reference in references:
            rules[reference.get('rule') or ''] += 1
    return dict(sections=dict(sorted(((key, size(value)) for key, value in summary.items()),
                                     key=lambda item: -item[1])[:10]),
                record_fields=dict(fields.most_common(16)), record_bytes_by_type=dict(by_type.most_common()),
                groups=groups, rules=dict(rules.most_common()))


def storage(conn) -> dict:
    """Rows per table, and bytes where this SQLite build exposes dbstat."""
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    rows = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    try:
        sizes = dict(conn.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name').fetchall())
    except sqlite3.Error:
        sizes = {}
    largest = sorted(tables, key=lambda table: -(sizes.get(table) or rows[table]))[:8]
    return {table: dict(rows=rows[table], bytes=sizes.get(table)) for table in largest}


def worker(args) -> dict:
    root = args.code_root.resolve()
    sys.path[:0] = [str(root / 'workspace'), str(root / 'tools')]
    import db
    import records
    import workspace_state
    if Path(workspace_state.__file__).resolve().parent != root / 'workspace':
        raise RuntimeError('A code root requires a fresh process')
    try:
        import request_timings
    except ImportError:  # a baseline older than the in-memory timings
        request_timings = None
    conn = db.connect(args.worker)
    try:
        if db.pending(conn):
            raise RuntimeError('This code root has migrations the copy lacks; measure a matching revision')

        def rebuild():
            workspace_state._JSON_CACHE.clear()
            started = time.perf_counter()
            # Wall time for the ordinary full form, including its compression. The server's own
            # build figure (`save_path_build_median`) is what a save's delta waits for.
            workspace_state.summary_json(conn)
            return (time.perf_counter() - started) * 1000

        before = conn.total_changes
        cold = rebuild()
        if request_timings:
            request_timings.clear()
        builds = [rebuild() for _ in range(args.repeat)]
        build = request_timings.snapshot()['kinds'].get('build', {}) if request_timings else {}
        encodes = []
        for _ in range(min(args.repeat, 5)):
            rebuild()
            started = time.perf_counter()
            workspace_state.summary_json(conn, None, 'strings-v1')
            encodes.append((time.perf_counter() - started) * 1000)
        # The high-water mark of serving alone: the verification below decodes whole summaries
        # and builds the full state, which the server never does for a page.
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        raw, compressed = workspace_state.summary_json(conn)
        packed, packed_compressed = workspace_state.summary_json(conn, None, 'strings-v1')
        summary, envelope = json.loads(raw), json.loads(packed)
        assert json.loads(gzip.decompress(compressed)) == summary, 'The compressed summary differs'
        assert gzip.decompress(packed_compressed) == packed, 'The compressed browser form differs'
        assert unpack(envelope) == summary, 'The browser form does not restore the summary'
        full = json.loads(json.dumps(records.state(conn)))
        assert conn.total_changes == before, 'A read changed the copy'
        types = collections.Counter(record.get('type') or '' for record in summary['records'])
        result = dict(
            schema=db.version(conn), active_records=len(summary['records']), records_by_type=dict(types.most_common()),
            errors=sorted(key for key in summary if key.endswith('_error')),
            timings_ms=dict(cold_build=round(cold), builds=[round(value) for value in builds],
                            build_with_plain_gzip_median=round(statistics.median(builds)),
                            save_path_build_median=build.get('median_ms'),
                            phases=build.get('phase_median_ms') or {},
                            first_page_encode_median=round(statistics.median(encodes))),
            peak_rss_mib=round(peak / (1024 ** 2 if sys.platform == 'darwin' else 1024), 1),
            bytes=dict(summary=len(raw), summary_gzip=len(compressed), browser=len(packed),
                       browser_gzip=len(packed_compressed)),
            digests=dict(summary=digest(normalized(summary)), browser_strings=digest(envelope['strings']),
                         browser_value=digest(normalized(envelope['value'])), full_state=digest(normalized(full))))
        if args.payload:
            result.update(payload=payload_shape(summary), storage=storage(conn))
        return result
    finally:
        conn.close()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, help='A development copy or synthetic fixture; it is copied, never opened for writing')
    parser.add_argument('--code-root', type=Path, default=ROOT)
    parser.add_argument('--repeat', type=int, default=7, help='1-50 rebuilds (the timing sample)')
    parser.add_argument('--payload', action='store_true', help='Add payload composition and table sizes')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 50:
        parser.error('--repeat must be between 1 and 50')
    if not args.worker and not args.db:
        parser.error('--db is required')
    if not (args.code_root / 'workspace' / 'workspace_state.py').is_file():
        parser.error('--code-root must be a checkout or archive of this repository')
    return args


def run(args) -> dict:
    """Copy, measure in a fresh process, remove the copy. Returns the child's aggregates."""
    with tempfile.TemporaryDirectory(prefix='work-ops-copy-') as folder:
        Path(folder).chmod(0o700)
        copy = private_copy(args.db, Path(folder))
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(copy),
                   '--code-root', str(args.code_root.resolve()), '--repeat', str(args.repeat)]
        if args.payload:
            command.append('--payload')
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode:
            # The child's traceback names code locations; it is not given record content to print.
            raise RuntimeError('Measurement failed:\n' + done.stderr[-2000:])
        return json.loads(done.stdout)


def main(argv=None) -> None:
    args = arguments(argv)
    try:
        result = worker(args) if args.worker else run(args)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
    print(json.dumps(result, indent=None if args.worker else 2))


if __name__ == '__main__':
    main()
