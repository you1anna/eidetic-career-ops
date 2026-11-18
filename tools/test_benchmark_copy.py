"""The real-shaped measurement never touches its source and reports aggregates only."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

import benchmark_copy as benchmark
import make_fixture


class CopyBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = Path(cls.temp.name) / 'fixture.db'
        make_fixture.build(cls.source)
        conn = sqlite3.connect(cls.source)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        cls.names = [row[0] for row in conn.execute('SELECT name FROM records WHERE length(name) >= 8')]
        cls.ids = [row[0] for row in conn.execute('SELECT id FROM records')]
        cls.active = conn.execute('SELECT COUNT(*) FROM records WHERE archived_at IS NULL').fetchone()[0]
        conn.close()
        cls.source.chmod(0o400)  # the service's own connection would reset this to 0600
        cls.sha = hashlib.sha256(cls.source.read_bytes()).hexdigest()
        cls.plain = benchmark.run(benchmark.arguments(['--db', str(cls.source), '--repeat', '1']))
        cls.detailed = benchmark.run(benchmark.arguments(['--db', str(cls.source), '--repeat', '1', '--payload']))

    def test_the_source_database_is_neither_reopened_for_writing_nor_changed(self):
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o400)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sha)
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir() if p.suffix == '.db'), ['fixture.db'])

    def test_two_runs_of_one_revision_agree_and_report_the_whole_active_store(self):
        self.assertEqual(self.plain['digests'], self.detailed['digests'])
        self.assertEqual(set(self.plain['digests']), {'summary', 'browser_strings', 'browser_value', 'full_state'})
        self.assertEqual(self.plain['active_records'], self.active)
        self.assertEqual(sum(self.plain['records_by_type'].values()), self.active)
        self.assertEqual(self.plain['errors'], [])
        self.assertGreater(self.plain['bytes']['summary'], self.plain['bytes']['summary_gzip'])
        self.assertEqual(len(self.plain['timings_ms']['builds']), 1)
        self.assertNotIn('payload', self.plain)

    def test_output_is_aggregate_only(self):
        self.assertGreater(len(self.names), 10)
        text = json.dumps(self.detailed)
        self.assertEqual([name for name in self.names if name in text], [])
        self.assertEqual([rid for rid in self.ids if json.dumps(rid) in text], [])

    def test_payload_groups_and_storage_are_counted_from_the_summary(self):
        payload, storage = self.detailed['payload'], self.detailed['storage']
        self.assertEqual(set(payload['groups']), set(benchmark.GROUPS))
        placed = sum(group['records'] for group in payload['groups'].values())
        self.assertEqual(placed, sum(payload['rules'].values()))
        self.assertLessEqual(placed, self.active)
        self.assertGreater(placed, 0)
        self.assertEqual(payload['sections']['records'], sum(payload['record_bytes_by_type'].values()) + self.active + 1)
        self.assertEqual(storage['records']['rows'], len(self.ids))

    def test_invalid_requests_are_refused(self):
        for argv in (['--repeat', '2'], ['--db', 'x.db', '--repeat', '0'], ['--db', 'x.db', '--repeat', '51'],
                     ['--db', 'x.db', '--code-root', self.temp.name]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    benchmark.arguments(argv)
                self.assertEqual(error.exception.code, 2)
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            benchmark.private_copy(Path(self.temp.name) / 'absent.db', Path(self.temp.name))

    def test_digest_and_reference_decoder_are_pinned_independently(self):
        state = {'version': 'a-1', 'loaded': 'clock', 'records': [{'id': 'P1', 'version': 4}]}
        expected = hashlib.sha256(b'{"records":[{"id":"P1","version":4}]}').hexdigest()
        self.assertEqual(benchmark.digest(benchmark.normalized(state)), expected)
        self.assertNotEqual(benchmark.digest(benchmark.normalized({**state, 'records': []})), expected)
        envelope = {'strings': ['shared evidence'], 'value': {'a': '~0', 'b': ['~~literal', '~0', 3, None], 'c': 'plain'}}
        self.assertEqual(benchmark.unpack(envelope),
                         {'a': 'shared evidence', 'b': ['~literal', 'shared evidence', 3, None], 'c': 'plain'})
        self.assertEqual(benchmark.size({'a': [1, 'b']}), len('{"a":[1,"b"]}'))


if __name__ == '__main__':
    unittest.main()
