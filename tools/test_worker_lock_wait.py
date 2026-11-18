"""Background workers must outwait another writer instead of losing their receipt (21 Oct research crash)."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
import db
import caretaker
import email_worker
import ingest
import research_worker


class WorkerLockWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / 'work-ops.db'
        conn = db.connect(self.path); db.migrate(conn); conn.close()

    def hold_write_lock(self, seconds):
        holder = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        holder.execute('BEGIN IMMEDIATE')
        timer = threading.Timer(seconds, lambda: (holder.execute('COMMIT'), holder.close()))
        timer.start(); self.addCleanup(timer.join)

    def receipts(self, prefix):
        conn = db.connect(self.path); self.addCleanup(conn.close)
        return conn.execute("SELECT status FROM scan_runs WHERE run_id LIKE ?", (prefix + ':%',)).fetchall()

    def research(self):
        with patch.object(research_worker, 'select_work', return_value=[]), \
                patch.object(research_worker.model, 'exchange') as api:
            receipt = research_worker.run(self.path, self.root, None, {'enabled': False})
        api.assert_not_called()
        return receipt

    def email(self):
        with patch.object(email_worker, 'collect', return_value=dict(pending=0, received=0)), \
                patch.object(email_worker.ingest, 'run', return_value={'status': 'ok', 'sources': {'alert:linkedin': {'state': 'empty'}}}):
            return email_worker.run(self.path, self.root, 'https://example.test', 'x' * 40)

    def caretaker(self):
        with patch.object(caretaker, 'inspect', return_value=[]):
            return caretaker.run(self.path, self.root, True)

    def test_each_worker_outwaits_a_writer_within_its_allowance(self):
        for name, prefix in (('research', 'research'), ('email', 'email'), ('caretaker', 'caretaker')):
            with self.subTest(worker=name), patch.object(db, 'WORKER_LOCK_WAIT', 5):
                self.hold_write_lock(0.5)
                receipt = getattr(self, name)()
                self.assertNotEqual(receipt['status'], 'running')
                self.assertEqual([r[0] for r in self.receipts(prefix)][-1], receipt['status'])

    def test_a_writer_held_past_the_allowance_still_fails_visibly(self):
        for name in ('research', 'email', 'caretaker'):
            with self.subTest(worker=name), patch.object(db, 'WORKER_LOCK_WAIT', 0.05):
                self.hold_write_lock(1.0)
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    getattr(self, name)()

    def test_allowance_covers_the_measured_import_lock(self):
        # Email runs took 21-25 s wall clock on the VPS, 20-22 Oct; the site keeps the shorter default.
        self.assertGreaterEqual(db.WORKER_LOCK_WAIT, 120)
        conn = db.connect(self.path); self.addCleanup(conn.close)
        self.assertEqual(db.lock_timeout(conn), 15)
        worker = db.connect(self.path, timeout=db.WORKER_LOCK_WAIT); self.addCleanup(worker.close)
        self.assertEqual(db.lock_timeout(worker), 120)

    def test_a_long_lived_connection_keeps_its_wait_after_many_later_connections(self):
        """A bounded table keyed by id() once forgot any connection older than 256 others,
        so its next contended write failed immediately instead of waiting."""
        worker = db.connect(self.path, timeout=5); self.addCleanup(worker.close)
        later = [db.connect(self.path) for _ in range(300)]  # kept open: no id or pointer is reused
        self.addCleanup(lambda: [conn.close() for conn in later])
        self.assertEqual(db.lock_timeout(worker), 5)
        self.assertEqual(db.lock_timeout(later[-1]), 15)
        self.hold_write_lock(0.5)
        started = time.monotonic()
        worker.execute('BEGIN IMMEDIATE')
        worker.execute('ROLLBACK')
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_collected_connections_leave_no_wait_behind(self):
        import gc
        if not db._sqlite_lib():
            self.skipTest('SQLite keeps its own busy timeout on this Python')
        gc.collect()
        before = len(db._BUSY_STATE)
        open_together = [db.connect(self.path) for _ in range(50)]  # fifty distinct handles
        self.assertEqual(len(db._BUSY_STATE), before + 50)
        for conn in open_together:
            conn.close()
        del open_together, conn
        gc.collect()
        self.assertEqual(len(db._BUSY_STATE), before)

    def test_busy_handler_logs_the_wait_then_gives_up(self):
        self.hold_write_lock(2.0)
        conn = db.connect(self.path, timeout=0.4); self.addCleanup(conn.close)
        started = time.monotonic()
        with patch.dict(os.environ, {'WO_UNIT': 'wo-research.service', 'WO_RUN_ID': 'research:test'}):
            with self.assertLogs(level='WARNING') as logs:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    conn.execute('BEGIN IMMEDIATE')
        self.assertLess(time.monotonic() - started, 1.5)
        text = '\n'.join(logs.output)
        self.assertIn('database is locked', text)
        self.assertIn('unit=wo-research.service', text)
        self.assertIn('run_id=research:test', text)
        self.assertIn('wait_ms=', text)
        self.assertNotIn('BEGIN', text)

    def test_writer_lock_logs_acquire_and_release(self):
        with patch.dict(os.environ, {'WO_RUN_ID': 'ingest:test'}):
            with self.assertLogs(level='INFO') as logs:
                with ingest.writer_lock(self.root):
                    pass
        text = '\n'.join(logs.output)
        self.assertIn('writer lock acquired', text)
        self.assertIn('writer lock released', text)
        self.assertIn('wait_ms=', text)
        self.assertIn('hold_ms=', text)
        self.assertIn('run_id=ingest:test', text)

    def test_email_receipt_keeps_import_phase_timings(self):
        result = {'status': 'ok', 'sources': {'alert:linkedin': {'state': 'empty'}},
                  'phase_seconds': {'backup': 1.2, 'adapters': 0.0, 'import': 3.4, 'checks': 0.2},
                  'seconds': 5.0, 'import': {'write_lock_seconds': 3.4}}
        with patch.object(email_worker, 'collect', return_value=dict(pending=0, received=0)), \
                patch.object(email_worker.ingest, 'run', return_value=result):
            receipt = email_worker.run(self.path, self.root, 'https://example.test', 'x' * 40)
        self.assertEqual(receipt['phase_seconds']['import'], 3.4)
        self.assertEqual(receipt['seconds'], 5.0)
        self.assertEqual(receipt['import']['write_lock_seconds'], 3.4)
        self.assertEqual(os.environ.get('WO_RUN_ID'), None)

    def test_a_research_pass_builds_workspace_state_once(self):
        calls = []
        real = research_worker.records.state_context
        def spy(conn):
            calls.append(conn)
            return real(conn)
        limits = {'enabled': True, 'focused': True, 'prepare': True, 'briefs': True, 'max_calls': 1}
        with patch.object(research_worker.records, 'state_context', spy), \
                patch.object(research_worker.model, 'exchange') as api:
            receipt = research_worker.run(self.path, self.root, None, limits)
        api.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(receipt['status'], 'running')
        self.assertIn('briefs', receipt)

    def test_a_python_with_sqlite_built_in_still_connects(self):
        # The VPS's uv Python compiles _sqlite3 in (no __file__) and hides SQLite's symbols (22 Oct outage).
        class BuiltIn: pass
        with patch.dict(sys.modules, {'_sqlite3': BuiltIn()}), patch.object(db, '_SQLITE', None):
            conn = db.connect(self.root / 'built-in.db')
            self.assertEqual(db.lock_timeout(conn), 15)
            self.assertEqual(conn.execute('select 1').fetchone()[0], 1)
            conn.close()


if __name__ == '__main__':
    unittest.main()
