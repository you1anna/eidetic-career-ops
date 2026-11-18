import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workspace'))
import db
import automation_usage as quota


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.db'
        self.conn = db.connect(self.path); db.migrate(self.conn); self.addCleanup(self.conn.close)
        self.at = '2026-10-19T09:00:00Z'

    def test_restarts_and_unknown_outcomes_do_not_reset_daily_allowance(self):
        for i in range(12): quota.reserve(self.conn, str(i), 'research', self.at)
        other = db.connect(self.path)
        try:
            with self.assertRaises(quota.Limited): quota.reserve(other, 'restart', 'research', self.at)
            self.assertEqual(quota.summary(other, self.at)['research_day'], 12)
        finally: other.close()

    def test_weekly_and_combined_caps_and_disabled_lanes(self):
        for day in range(7):
            at = f'2026-10-{19+day}T09:00:00Z'
            for i in range(12): quota.reserve(self.conn, str(i), 'research', at)
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'more', 'research', at)
        for lane, count in [('incident',4), ('question',6), ('review',1), ('evaluation',5)]:
            for i in range(count):
                # Spread ancillary calls across the week, observing the combined daily limit.
                at = f'2026-10-{19+i}T10:00:00Z' if lane == 'evaluation' else '2026-10-25T10:00:00Z'
                if lane == 'question': at = f'2026-10-{19+i}T11:00:00Z'
                if lane == 'review': at = '2026-10-24T11:00:00Z'
                quota.reserve(self.conn, str(i), lane, at)
        self.assertEqual(quota.summary(self.conn, self.at)['week'], 100)
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'more', 'evaluation', self.at)
        with self.assertRaises(ValueError): quota.reserve(self.conn, 'bad', 'unbounded', self.at)
        self.assertEqual(quota.periods('2026-10-18T23:30:00Z'), ('2026-10-19','2026-10-19'))

    def test_legacy_receipts_are_charged_once_before_first_new_dispatch(self):
        receipt = dict(started_at=self.at, status='partial', usage={'calls':12})
        self.conn.execute('INSERT INTO scan_runs VALUES(?,?,?,?)', ('research:legacy',self.at[:10],json.dumps(receipt),'partial'))
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'new', 'research', self.at)
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'retry', 'research', self.at)
        self.assertEqual(quota.summary(self.conn,self.at)['day'],12)

    def test_lock_prevents_two_model_exchanges_and_backup_preserves_reservations(self):
        with quota.serial(self.conn):
            with self.assertRaises(quota.Limited):
                with quota.serial(self.conn): pass
        rid=quota.reserve(self.conn,'r','research',self.at)
        quota.finish(self.conn,rid,{'input_tokens':30,'cached_input_tokens':20,'output_tokens':2},True)
        other=db.connect(Path(self.tmp.name)/'restored.db')
        try:
            self.conn.backup(other)
            self.assertEqual(quota.summary(other,self.at)['day'],1)
            self.assertEqual(json.loads(other.execute('SELECT usage_json FROM codex_invocations').fetchone()[0])['cached_input_tokens'],20)
        finally: other.close()

    def test_global_daily_limit_and_failed_transport_are_not_refunded(self):
        import research_model as model
        from research_schemas import FINDING
        with patch.object(model,'exchange',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):model.call(self.conn,'failed',None,{},self.at,{},FINDING)
        self.assertEqual(quota.summary(self.conn,self.at)['day'],1)
        for i in range(11):quota.reserve(self.conn,'r','research',self.at)
        for i in range(4):quota.reserve(self.conn,'i','incident',self.at)
        with self.assertRaises(quota.Limited):quota.reserve(self.conn,'q','question',self.at)
        self.assertEqual(quota.summary(self.conn,self.at)['day'],16)

    def test_interrupted_legacy_pass_reserves_one_uncertain_inflight_invocation(self):
        receipt=dict(started_at=self.at,status='running',usage={'calls':0})
        self.conn.execute('INSERT INTO scan_runs VALUES(?,?,?,?)',('research:interrupted',self.at[:10],json.dumps(receipt),'running'))
        quota.seed(self.conn)
        self.assertEqual(quota.summary(self.conn,self.at)['day'],1)
        quota.seed(self.conn)
        self.assertEqual(quota.summary(self.conn,self.at)['day'],1)


class PreparationSublimitTests(unittest.TestCase):
    """Weekly preparation calls are research calls with a durable weekly sublimit of 15."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.db'
        self.conn = db.connect(self.path); db.migrate(self.conn); self.addCleanup(self.conn.close)

    def test_sublimit_is_durable_inside_the_research_lane_and_survives_restart_and_restore(self):
        days = ['2026-10-19T08:00:00Z', '2026-10-20T08:00:00Z']
        for i in range(15):
            quota.reserve(self.conn, f'p{i}', 'research', days[i // 8], purpose='preparation')
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'more', 'research', days[1], purpose='preparation')
        # Ordinary research still has the rest of its own allowance that day (12 - 7).
        for i in range(5): quota.reserve(self.conn, f'r{i}', 'research', days[1])
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'r', 'research', days[1])
        total = quota.summary(self.conn, days[1])
        self.assertEqual((total['preparation_week'], total['preparation_week_limit'], total['research_week']), (15, 15, 20))
        other = db.connect(self.path)
        try:
            with self.assertRaises(quota.Limited): quota.reserve(other, 'restart', 'research', '2026-10-21T08:00:00Z', purpose='preparation')
        finally: other.close()
        restored = db.connect(Path(self.tmp.name) / 'restored.db')
        try:
            self.conn.backup(restored)
            with self.assertRaises(quota.Limited): quota.reserve(restored, 'restored', 'research', '2026-10-22T08:00:00Z', purpose='preparation')
            self.assertEqual(quota.summary(restored, days[1])['preparation_week'], 15)
        finally: restored.close()
        # A new London week starts a new sublimit; the research week cap is shared with it.
        quota.reserve(self.conn, 'next', 'research', '2026-10-26T08:00:00Z', purpose='preparation')

    def test_preparation_counts_against_daily_research_cap_and_unknown_purpose_is_refused(self):
        at = '2026-10-19T08:00:00Z'
        for i in range(12): quota.reserve(self.conn, str(i), 'research', at, purpose='preparation')
        with self.assertRaises(quota.Limited): quota.reserve(self.conn, 'x', 'research', at)
        with self.assertRaises(ValueError): quota.reserve(self.conn, 'x', 'research', at, purpose='improvement')
        with self.assertRaises(ValueError): quota.reserve(self.conn, 'x', 'question', at, purpose='preparation')

    def test_retired_preparation_is_refused_before_transport_or_reservation(self):
        import research_model as model
        from research_schemas import PREPARATION
        with patch.object(model, 'exchange', side_effect=TimeoutError):
            with self.assertRaisesRegex(ValueError,'schema'):
                model.call(self.conn, 'failed', None, {}, '2026-10-19T08:00:00Z', {}, PREPARATION, purpose='preparation')
        self.assertEqual(quota.summary(self.conn, '2026-10-19T08:00:00Z')['preparation_week'], 0)
