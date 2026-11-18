"""Deterministic rebuild evidence and cheap synthetic setup, without timing thresholds."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import benchmark_rebuild as benchmark


class RebuildBenchmarkTests(unittest.TestCase):
    def test_invalid_sizes_and_repetitions_are_refused(self):
        for extra in (['--people', '-1'], ['--repeat', '0'], ['--repeat', '-1'], ['--repeat', '51'], ['--history', '-1']):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    benchmark.arguments(['--count', '100', *extra])
                self.assertEqual(error.exception.code, 2)
        self.assertEqual(benchmark.arguments(['--count', '100']).people, 100)
        self.assertEqual(benchmark.arguments(['--count', '100', '--repeat', '50']).repeat, 50)

    def test_digest_ignores_only_top_level_version_and_loaded(self):
        state = {'version': 'random-a', 'loaded': 'clock-a', 'records': [{'id': 'P1', 'version': 4, 'loaded': 'evidence', 'notes': 'Original'}]}
        expected = hashlib.sha256(b'{"records":[{"id":"P1","loaded":"evidence","notes":"Original","version":4}]}').hexdigest()
        self.assertEqual(benchmark.digest(state), expected)
        self.assertEqual(benchmark.digest(dict(state, version='random-b', loaded='clock-b')), expected)
        for key, value in [('notes', 'Changed'), ('version', 5), ('loaded', 'different evidence')]:
            changed = copy.deepcopy(state); changed['records'][0][key] = value
            self.assertNotEqual(benchmark.digest(changed), expected)

    def test_delta_reconstruction_pins_add_remove_reorder_and_unset_independently(self):
        original = {'version': 'v1', 'loaded': 'time', 'retired': True, 'records': [{'id': 'A', 'notes': 'old'}, {'id': 'B'}],
                    'intelligence': {'actions': [{'id': 'action-A'}], 'research': [{'id': 'research-B'}], 'old': 1}}
        delta = {'delta': True, 'base': 'v1', 'version': 'v2', 'unset': ['retired'], 'set': {'new': 2},
                 'intelligence': {'unset': ['old'], 'set': {'count': 1}}, 'collections': {
                    'records': {'remove': ['B'], 'upsert': [{'id': 'A', 'notes': 'new'}, {'id': 'C'}], 'order': ['C', 'A']},
                    'actions': {'remove': [], 'upsert': [{'id': 'action-A', 'stage': 'ready'}]},
                    'research': {'remove': ['research-B'], 'upsert': [], 'order': []}}}
        before = copy.deepcopy(original)
        self.assertEqual(benchmark.apply_delta(original, delta), {'version': 'v2', 'loaded': 'time', 'new': 2,
            'records': [{'id': 'C'}, {'id': 'A', 'notes': 'new'}],
            'intelligence': {'actions': [{'id': 'action-A', 'stage': 'ready'}], 'research': [], 'count': 1}})
        self.assertEqual(original, before)
        with self.assertRaises(AssertionError):
            benchmark.apply_delta(original, dict(delta, base='wrong'))

    def test_full_version_is_top_level_trailing_version(self):
        self.assertEqual(benchmark.full_version(b'{"records":[{"version":"nested"}],"version":"correct"}'), 'correct')
        with self.assertRaises(AssertionError):
            benchmark.full_version(b'{"records":[{"version":"nested"}]}')

    def test_fixed_clock_preserves_timezone_conversion_and_restores_classes(self):
        date_class, datetime_class = dt.date, dt.datetime
        with benchmark.fixed_clock():
            self.assertEqual(dt.date.today().isoformat(), '2026-11-06')
            self.assertEqual(dt.datetime.now(dt.timezone(dt.timedelta(hours=2))).isoformat(), '2026-11-06T14:00:00+02:00')
        self.assertIs(dt.date, date_class); self.assertIs(dt.datetime, datetime_class)

    def test_people_setup_never_uses_enriched_reads_per_interaction(self):
        _, records, _ = benchmark.code_modules(benchmark.ROOT)
        real = records.interaction
        visits = []
        def nested(conn, payload, actor):
            self.assertTrue(conn.in_transaction, 'fixture interaction must use its cheap nested return')
            visits.append((payload['record_id'], payload['revision']))
            return real(conn, payload, actor=actor)
        with tempfile.TemporaryDirectory(prefix='workops-rebuild-test-') as raw, \
                patch.object(records, 'interaction', side_effect=nested), contextlib.redirect_stderr(io.StringIO()):
            path = Path(raw) / 'fixture.db'
            metadata = benchmark.build_fixture(path, 10, 10, 0)
            self.assertEqual((metadata['records'], metadata['links'], metadata['interactions']), (21, 30, 80))
            self.assertEqual(len(visits), 80)
            self.assertEqual(visits[:8], [('CP-benchmark-000000', revision) for revision in range(3, 11)])
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT DISTINCT revision FROM records WHERE type='person'").fetchall(), [(11,)])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM records WHERE status='pursue'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT direction,COUNT(*) FROM interactions GROUP BY direction ORDER BY direction").fetchall(), [('in', 40), ('out', 40)])
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
