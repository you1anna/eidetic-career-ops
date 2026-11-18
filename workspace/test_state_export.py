"""Full-state parity, bounded receipt memory and coherent concurrent read lifetimes."""
import copy
import datetime as dt
import errno
import gc
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import records
import record_views
import state_export
import workspace_state


class StateExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.conn = db.connect(self.path)
        db.migrate(self.conn)
        self.person = records.create(self.conn, {'type': 'person', 'name': 'Synthetic Zoë 🧪',
                                                'relationship': 'warm', 'kind': 'friend'})['record']
        self.clock = patch.object(db, 'now', return_value='2026-10-23T12:00:00+00:00')
        self.clock.start()
        self.clear_cache()

    def tearDown(self):
        self.clock.stop()
        self.clear_cache()
        self.conn.close()
        self.temp.cleanup()

    def clear_cache(self):
        for _, artifact in state_export._FILE_CACHE.values():
            artifact.close()
        state_export._FILE_CACHE.clear()
        records._STATE_CACHE.clear()
        records._JSON_CACHE.clear()

    def receipt(self, n, conn=None, text='café 🧪 \\ "quoted"\nnext line'):
        (conn or self.conn).execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
            (f'archive:{n:06}', f'2025-01-{n % 27 + 1:02}', 'ok',
             json.dumps({'number': n, 'nested': [None, True, {'text': text}]})))

    def test_full_serialization_matches_existing_json_including_unicode_order_and_empty_history(self):
        for count in (0, 31):
            for n in range(count): self.receipt(n)
            expected = records.state(self.conn)
            for ensure_ascii in (True, False):
                with self.subTest(count=count, ascii=ensure_ascii):
                    with state_export.state_json_file(self.conn, ensure_ascii=ensure_ascii) as stream:
                        self.assertFalse(self.conn.in_transaction)
                        self.assertEqual(stream.read(), json.dumps(expected, ensure_ascii=ensure_ascii).encode())
            self.assertEqual(len(expected['coverage']['runs']), count)

    def test_context_and_summary_keep_their_actions_scores_and_admission_without_decoding_history(self):
        for n in range(12): self.receipt(n)
        full = records.state(self.conn)
        expected = copy.deepcopy(full)
        expected['coverage']['runs'] = []
        summary = records.state(self.conn, compact=True)
        changes = self.conn.total_changes
        with patch.object(record_views, 'receipts', side_effect=AssertionError('Archive loaded')):
            self.assertEqual(records.state_context(self.conn), expected)
            self.assertEqual(records.cached_context(self.conn), expected)
            self.assertEqual(records.state(self.conn, compact=True), summary)
            with state_export.state_json_file(self.conn) as stream:
                self.assertEqual(json.load(stream), json.loads(json.dumps(full)))
        self.assertEqual(changes, self.conn.total_changes)

    def test_a_concurrent_commit_does_not_mix_receipts_with_an_older_record_snapshot(self):
        self.receipt(1)
        original = record_views.iter_receipts
        def during_export(conn):
            other = db.connect(self.path)
            try:
                records.edit(other, self.person['id'], {'revision': self.person['revision'], 'name': 'Changed'})
                self.receipt(2, conn=other)
            finally: other.close()
            yield from original(conn)
        with patch.object(record_views, 'iter_receipts', during_export):
            with state_export.state_json_file(self.conn) as stream:
                snapshot = json.load(stream)
        self.assertEqual(snapshot['records'][0]['name'], self.person['name'])
        self.assertEqual(len(snapshot['coverage']['runs']), 1)
        with state_export.state_json_file(self.conn) as stream:
            newer = json.load(stream)
        self.assertEqual(newer['records'][0]['name'], 'Changed')
        self.assertEqual(len(newer['coverage']['runs']), 2)

    def test_cache_reuses_one_artifact_and_evicts_it_without_interrupting_existing_readers(self):
        self.receipt(1)
        expected = json.dumps(records.state(self.conn)).encode()
        with patch.object(state_export, '_build_artifact', wraps=state_export._build_artifact) as built:
            with state_export.cached_state_file(self.conn) as old:
                prefix = old.read(17)
                with state_export.cached_state_file(self.conn, compress=True) as zipped:
                    self.assertEqual(gzip.decompress(zipped.read()), expected)
                self.assertEqual(built.call_count, 1)
                self.receipt(2)
                with state_export.cached_state_file(self.conn) as current:
                    self.assertEqual(len(json.loads(current.read())['coverage']['runs']), 2)
                self.assertEqual(prefix + old.read(), expected)
                self.assertEqual(built.call_count, 2)
                self.assertFalse(self.conn.in_transaction)
            self.assertIsNone(old.fd)
        self.assertNotIn(False, records._STATE_CACHE)

    def test_parallel_cold_requests_build_once_and_have_independent_offsets(self):
        self.receipt(1)
        barrier = threading.Barrier(4)
        results, errors = [], []
        def read():
            conn = db.connect(self.path)
            try:
                barrier.wait(timeout=10)
                with state_export.cached_state_file(conn) as stream:
                    parts=[]
                    while part := stream.read(97): parts.append(part)
                    results.append(b''.join(parts))
            except BaseException as exc: errors.append(exc)
            finally: conn.close()
        with patch.object(state_export, '_build_artifact', wraps=state_export._build_artifact) as built:
            threads=[threading.Thread(target=read) for _ in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=15)
            self.assertTrue(all(not t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 4)
            self.assertTrue(all(r == results[0] for r in results))
            self.assertEqual(built.call_count, 1)

    def test_uncommitted_state_does_not_pollute_the_shared_cache(self):
        with state_export.cached_state_file(self.conn) as stream: before = stream.read()
        self.conn.execute('BEGIN IMMEDIATE')
        self.receipt(99)
        try:
            with state_export.cached_state_file(self.conn) as stream:
                self.assertEqual(len(json.loads(stream.read())['coverage']['runs']), 1)
            self.assertEqual(len(records.cached_context(self.conn)['records']), 1)
        finally: self.conn.rollback()
        with state_export.cached_state_file(self.conn) as stream: self.assertEqual(stream.read(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0], 0)

    def test_config_and_london_date_invalidate_the_file_cache(self):
        import approaches
        root = Path(self.temp.name) / 'config-root'
        (root / 'config').mkdir(parents=True)
        with patch.object(workspace_state, 'ROOT', root), \
                patch.object(approaches, 'london_today', return_value=dt.date(2026, 10, 23)) as day, \
                patch.object(state_export, '_build_artifact', wraps=state_export._build_artifact) as built:
            with state_export.cached_state_file(self.conn): pass
            with state_export.cached_state_file(self.conn): pass
            self.assertEqual(built.call_count, 1)
            (root / 'config' / 'new.yml').write_text('version: 1\n')
            with state_export.cached_state_file(self.conn): pass
            self.assertEqual(built.call_count, 2)
            day.return_value = dt.date(2026, 10, 24)
            with state_export.cached_state_file(self.conn): pass
            self.assertEqual(built.call_count, 3)

    def test_outcome_and_week_commands_do_not_load_the_receipt_archive(self):
        self.receipt(1)
        with patch.object(record_views, 'receipts', side_effect=AssertionError('Archive loaded')):
            records.include_in_week(self.conn, self.person['id'])
            context = records.state_context(self.conn)
            action = next(a for a in context['intelligence']['actions'] + context['intelligence']['research']
                          if a.get('record_id') == self.person['id'])
            current = records.get(self.conn, self.person['id'])
            result = records.outcome(self.conn, current['id'],
                                     {'kind': 'sent', 'revision': current['revision'], 'action_id': action['id']})
        self.assertTrue(result['saved'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0], 1)

    def test_failed_export_closes_temporary_files_and_prints_no_partial_json(self):
        self.receipt(1)
        self.conn.execute("UPDATE scan_runs SET receipt_json='broken JSON'")
        opened=[]
        real=tempfile.TemporaryFile
        def tracked(*args, **kwargs):
            stream=real(*args, **kwargs);opened.append(stream);return stream
        output=io.StringIO()
        with patch.object(state_export.tempfile, 'TemporaryFile', tracked):
            with self.assertRaises(ValueError): state_export.print_state(self.conn, output)
            with self.assertRaises(ValueError):
                with state_export.cached_state_file(self.conn): self.fail('Must fail before yielding')
        self.assertEqual(output.getvalue(), '')
        self.assertTrue(opened and all(f.closed for f in opened))
        self.assertEqual(state_export._FILE_CACHE, {})
        self.assertFalse(self.conn.in_transaction)

    def test_full_temporary_disk_fails_before_output_and_a_later_request_recovers(self):
        self.receipt(1)
        opened = []
        real = tempfile.TemporaryFile
        def exhausted(*args, **kwargs):
            stream = real(*args, **kwargs)
            opened.append(stream)
            def fail_write(_):
                raise OSError(errno.ENOSPC, 'Synthetic temporary disk exhaustion')
            stream.write = fail_write
            return stream
        output = io.StringIO()
        with patch.object(state_export.tempfile, 'TemporaryFile', exhausted):
            with self.assertRaises(OSError) as error:
                state_export.print_state(self.conn, output)
            self.assertEqual(error.exception.errno, errno.ENOSPC)
            with self.assertRaises(OSError):
                with state_export.cached_state_file(self.conn): self.fail('Must fail before yielding')
        self.assertEqual(output.getvalue(), '')
        self.assertTrue(opened and all(f.closed for f in opened))
        self.assertEqual(state_export._FILE_CACHE, {})
        self.assertFalse(self.conn.in_transaction)
        with state_export.cached_state_file(self.conn) as stream:
            self.assertEqual(len(json.loads(stream.read())['coverage']['runs']), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0], 1)

    def test_interrupted_consumer_closes_its_reader_and_the_cache_still_works(self):
        with self.assertRaises(BrokenPipeError):
            with state_export.cached_state_file(self.conn) as stream:
                fd=stream.fd
                self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o600)
                raise BrokenPipeError()
        with self.assertRaises(OSError): os.fstat(fd)
        with state_export.cached_state_file(self.conn) as stream: self.assertTrue(json.loads(stream.read()))

    def test_cli_output_matches_existing_text_even_across_utf8_chunk_boundaries(self):
        self.receipt(1, text='🧪' * 40000)
        expected=json.dumps(records.state(self.conn), ensure_ascii=False)+'\n'
        output=io.StringIO()
        state_export.print_state(self.conn, output)
        self.assertEqual(output.getvalue(), expected)

    def test_incremental_memory_does_not_grow_with_the_receipt_archive(self):
        # Warm imports/configuration; measure serialization without retaining response bytes.
        with state_export.state_json_file(self.conn): pass
        peaks=[]
        for start, end in ((0, 20), (20, 400)):
            for n in range(start, end): self.receipt(n, text='synthetic ' * 1000)
            gc.collect();tracemalloc.start()
            try:
                with state_export.state_json_file(self.conn) as stream:
                    while stream.read(65536): pass
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally: tracemalloc.stop()
        self.assertLess(peaks[1] - peaks[0], 2 * 1024 * 1024, peaks)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0], 400)




class BoardScopeConfigTests(unittest.TestCase):
    """Every workspace build asks for the configured boards; portals.yml is parsed only when it changes."""

    def test_boards_are_reread_only_when_the_file_changes(self):
        import workspace_state
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config'
            config.mkdir()
            portals = config / 'portals.yml'
            board = lambda name, enabled=True: (f'- name: {name}\n  careers_url: https://jobs.ashbyhq.com/{name.lower()}\n'
                                               f'  enabled: {str(enabled).lower()}\n')
            portals.write_text('tracked_companies:\n' + board('Alpha') + board('Beta', False))
            with patch.object(workspace_state, 'ROOT', Path(tmp)), patch.dict(workspace_state._BOARD_SCOPES, clear=True):
                first = workspace_state.configured_board_scopes()
                self.assertEqual(first, {'board:ashby:alpha': 'Alpha'})
                first['board:ashby:mutated'] = 'by a caller'
                with patch('yaml.safe_load') as parse:
                    self.assertEqual(workspace_state.configured_board_scopes(), {'board:ashby:alpha': 'Alpha'})
                parse.assert_not_called()
                portals.write_text('tracked_companies:\n' + board('Alpha') + board('Beta'))
                os.utime(portals, ns=(portals.stat().st_atime_ns, portals.stat().st_mtime_ns + 1_000_000))
                self.assertEqual(workspace_state.configured_board_scopes(),
                                 {'board:ashby:alpha': 'Alpha', 'board:ashby:beta': 'Beta'})
                portals.unlink()
                self.assertIsNone(workspace_state.configured_board_scopes())


if __name__ == '__main__': unittest.main()
