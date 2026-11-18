"""A job that dies leaves its cause where the caretaker reads it, and the card says why (22 Oct post-mortem)."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / "workspace")]
import db
import caretaker
import failure_tasks
import research_worker
from email_worker import save_receipt

NOW = dt.datetime(2026, 10, 22, 8, tzinfo=dt.timezone.utc)


def locked_save():
    raise sqlite3.OperationalError('database is locked')


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)

    def note(self, job):
        return json.loads((self.data / 'crashes' / (job + '.json')).read_text())

    def test_database_error_keeps_its_message_and_the_code_path(self):
        with self.assertRaises(sqlite3.OperationalError):
            with db.failures_noted(self.data, 'wo-research.service'):
                locked_save()
        note = self.note('wo-research.service')
        self.assertEqual(note['error'], 'OperationalError: database is locked')
        self.assertIn('test_failure_notes.py', note['where']); self.assertIn('locked_save', note['where'])
        self.assertEqual(note['recent'], [note['at']])

    def test_other_errors_keep_only_their_type(self):
        try: raise ValueError('https://example.test/private?token=abc')
        except ValueError as error: db.note_failure(self.data, 'wo-email.service', error)
        self.assertEqual(self.note('wo-email.service')['error'], 'ValueError')
        self.assertNotIn('example.test', (self.data / 'crashes' / 'wo-email.service.json').read_text())

    def test_a_note_names_the_run_and_process_and_drops_other_text(self):
        with patch.dict(os.environ, {'WO_RUN_ID': 'email:test-run'}):
            try: raise ValueError('https://example.test/private?token=abc')
            except ValueError as error: db.note_failure(self.data, 'wo-email.service', error, NOW)
        note = self.note('wo-email.service')
        self.assertEqual(note['error'], 'ValueError')
        self.assertEqual(note['run_id'], 'email:test-run')
        self.assertEqual(note['pid'], os.getpid())
        self.assertNotIn('token', json.dumps(note))
        text, count = db.failure_summary(self.data, 'wo-email.service', NOW + dt.timedelta(minutes=1))
        self.assertIn('run email:test-run', text)
        self.assertEqual(count, 1)

    def test_recent_failures_are_bounded(self):
        for i in range(12):
            db.note_failure(self.data, 'wo-email.service', RuntimeError(), NOW + dt.timedelta(minutes=i))
        self.assertEqual(len(self.note('wo-email.service')['recent']), 10)

    def test_an_unwritable_note_never_hides_the_original_failure(self):
        (self.data / 'crashes').write_text('not a folder')
        with self.assertRaises(KeyError):
            with db.failures_noted(self.data, 'wo-scan.service'):
                raise KeyError('x')

    def test_the_research_job_notes_a_crash_and_still_fails(self):
        argv = ['research_worker.py', '--db', str(self.data / 'w.db'), '--data', str(self.data)]
        with patch.object(sys, 'argv', argv), patch.object(research_worker, 'run', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                research_worker.main()
        self.assertEqual(self.note('wo-research.service')['error'], 'OperationalError: database is locked')


class CaretakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.conn = db.connect(self.data / 'test.db'); db.migrate(self.conn); self.addCleanup(self.conn.close)
        self.units = {u: {'ActiveState': 'active', 'Result': 'success'} for u in caretaker.UNITS}
        at = NOW.isoformat()
        for lane in ('email', 'research', 'ingest'):
            save_receipt(self.conn, lane + ':' + at, dict(status='ok', started_at=at, finished_at=at))
        (self.data / 'backups').mkdir()
        (self.data / 'backups' / 'last-backup.json').write_text(json.dumps({'at': at}))

    def check(self, id, now=NOW):
        checks = caretaker.inspect(self.conn, self.data, now, units=self.units, health=True)
        return next(c for c in checks if c['id'] == id)

    def crash(self, job, at):
        try: locked_save()
        except sqlite3.OperationalError as error: db.note_failure(self.data, job, error, at)

    def test_a_failed_unit_card_says_why(self):
        self.crash('wo-research.service', NOW - dt.timedelta(minutes=10))
        self.units['wo-research.service'] = {'ActiveState': 'failed', 'Result': 'exit-code'}
        c = self.check('unit:wo-research.service')
        self.assertEqual(c['status'], 'failed')
        self.assertIn('OperationalError: database is locked at ', c['detail'])
        self.assertIn('locked_save', c['detail'])

    def test_repeated_failures_raise_even_when_the_next_run_recovered(self):
        for minutes in (5, 20, 35):
            self.crash('wo-email.service', NOW - dt.timedelta(minutes=minutes))
        c = self.check('unit:wo-email.service')
        self.assertEqual(c['status'], 'failed'); self.assertIn('3 in 24 h', c['detail'])
        self.assertEqual(self.check('unit:wo-email.service', NOW + dt.timedelta(hours=25))['status'], 'ok')

    def test_one_recovered_failure_is_not_a_card(self):
        self.crash('wo-email.service', NOW - dt.timedelta(minutes=5))
        self.assertEqual(self.check('unit:wo-email.service')['status'], 'ok')

    def test_a_failed_receipt_card_carries_its_recorded_cause(self):
        at = (NOW + dt.timedelta(minutes=1)).isoformat()
        save_receipt(self.conn, 'email:' + at, dict(status='failed', error_kind='TimeoutError', started_at=at, finished_at=at))
        c = self.check('email', NOW + dt.timedelta(minutes=2))
        self.assertEqual(c['status'], 'failed'); self.assertIn('TimeoutError', c['detail'])
        save_receipt(self.conn, 'research:' + at, dict(status='blocked', reason='access', started_at=at, finished_at=at))
        c = self.check('research', NOW + dt.timedelta(minutes=2))
        self.assertEqual(c['status'], 'failed'); self.assertIn('Latest run blocked: access', c['detail'])

    def test_a_stuck_receipt_card_carries_the_crash(self):
        at = (NOW + dt.timedelta(minutes=1)).isoformat()
        save_receipt(self.conn, 'research:' + at, dict(status='running', started_at=at))
        self.crash('wo-research.service', NOW + dt.timedelta(minutes=30))
        c = self.check('research', NOW + dt.timedelta(hours=2))
        self.assertEqual(c['status'], 'failed'); self.assertIn('database is locked', c['detail'])

    def test_the_raised_card_shows_the_cause(self):
        self.crash('wo-research.service', NOW - dt.timedelta(minutes=10))
        self.units['wo-research.service'] = {'ActiveState': 'failed', 'Result': 'exit-code'}
        caretaker.record(self.conn, caretaker.inspect(self.conn, self.data, NOW, units=self.units, health=True), NOW)
        context = self.conn.execute("SELECT context FROM decisions WHERE subject='workspace:caretaker-unit-wo-research-service'").fetchone()[0]
        self.assertIn('database is locked', context)


class CaretakerWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.conn = db.connect(self.data / 'test.db'); db.migrate(self.conn); self.addCleanup(self.conn.close)

    def open_subjects(self):
        return [r[0] for r in self.conn.execute("SELECT subject FROM decisions WHERE kind='failure' AND status='open'")]

    def test_a_caretaker_that_never_ran_is_not_raised(self):
        failure_tasks.watch(self.conn, self.data, NOW)
        self.assertNotIn('workspace:site-caretaker', self.open_subjects())

    def test_a_silent_caretaker_is_raised_with_its_crash_and_cleared_when_it_runs(self):
        old = (NOW - dt.timedelta(hours=3)).isoformat()
        save_receipt(self.conn, 'caretaker:' + old, dict(status='ok', started_at=old, finished_at=old))
        try: locked_save()
        except sqlite3.OperationalError as error: db.note_failure(self.data, 'wo-caretaker.service', error, NOW - dt.timedelta(minutes=30))
        self.assertIn('site-caretaker', failure_tasks.watch(self.conn, self.data, NOW)['raised'])
        context = self.conn.execute("SELECT context FROM decisions WHERE subject='workspace:site-caretaker'").fetchone()[0]
        self.assertIn('database is locked', context)
        fresh = NOW.isoformat()
        save_receipt(self.conn, 'caretaker:' + fresh, dict(status='ok', started_at=fresh, finished_at=fresh))
        self.assertIn('site-caretaker', failure_tasks.watch(self.conn, self.data, NOW + dt.timedelta(minutes=5))['cleared'])


if __name__ == '__main__':
    unittest.main()
