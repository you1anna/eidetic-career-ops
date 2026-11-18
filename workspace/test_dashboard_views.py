"""The Dashboard's server-only figures: correct, bounded, read-only and independent of state builds."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dashboard_views
import db
import records
import workspace_state
from test_api import ServerCase

NOW = dt.datetime(2026, 11, 3, 16, 0, tzinfo=dt.timezone.utc)
MIB = 1024 * 1024


def receipt(conn, run_id, **body):
    conn.execute('INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?)',
                 (run_id, run_id.split(':', 1)[1][:10], json.dumps(body), body.get('status', 'ok')))


def memory(rss_mib, peak_mib, anon_mib, events=None):
    return dict(process_peak_rss_bytes=rss_mib * MIB, largest_child_peak_rss_bytes=MIB,
                cgroup=dict(state='observed', peak_bytes=peak_mib * MIB, limit_bytes=512 * MIB,
                            anon_bytes=anon_mib * MIB, file_bytes=0, events=events or {}))


class DashboardViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.conn = db.connect(self.data / 'work-ops.db')
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_scan_memory_is_the_newest_seven_daily_runs_newest_first(self):
        with self.conn:
            for day in range(1, 10):  # nine runs: 25 October to 2 November
                at = f'2026-10-{24 + day:02}T05:30:00+00:00' if day < 8 else f'2026-11-{day - 7:02}T05:30:00+00:00'
                receipt(self.conn, f'ingest:{at}', started_at=at, finished_at=at, memory=memory(250 + day, 450 + day, 150 + day))
        capacity = dashboard_views.overview(self.conn, self.data, NOW)['capacity']
        self.assertEqual(capacity['state'], 'ok')
        self.assertEqual([s['process_peak_bytes'] // MIB for s in capacity['scans']], [259, 258, 257, 256, 255, 254, 253])
        self.assertEqual(capacity['scans'][0]['limit_bytes'], 512 * MIB)
        self.assertEqual(capacity['scans'][0]['anon_bytes'], 159 * MIB)
        self.assertEqual(capacity['headroom_target'], 0.2)

    def test_pressure_events_spool_database_and_disk(self):
        with self.conn:
            receipt(self.conn, 'ingest:2026-11-03T05:30:00+00:00', finished_at='2026-11-03T05:38:00+00:00',
                    memory=memory(300, 500, 260, dict(high=2, max=0, oom=1, oom_kill=0, low=9)))
            receipt(self.conn, 'email:2026-11-03T15:45:00+00:00', finished_at='2026-11-03T15:46:00+00:00',
                    memory=memory(90, 120, 60), spool=dict(bytes=5 * MIB, count=40))
        capacity = dashboard_views.overview(self.conn, self.data, NOW)['capacity']
        self.assertEqual(capacity['scans'][0]['pressure_events'], 3)  # high + oom; "low" is not pressure
        self.assertEqual((capacity['spool']['bytes'], capacity['spool']['count']), (5 * MIB, 40))
        self.assertEqual((capacity['spool']['warn_bytes'], capacity['spool']['warn_count']), (48 * MIB, 750))
        self.assertEqual(capacity['email']['process_peak_bytes'], 90 * MIB)
        self.assertGreater(capacity['database']['bytes'], 0)
        self.assertGreater(capacity['disk']['total_bytes'], capacity['disk']['free_bytes'] - 1)

    def test_feed_calls_by_london_day_seven_days_and_month_against_declared_limits(self):
        adzuna = 'Adzuna UK — London QA and engineering leadership'
        with self.conn:
            for at, calls in (('2026-11-03T05:30:00+00:00', 23), ('2026-11-02T05:30:00+00:00', 22),
                              ('2026-10-28T05:30:00+00:00', 21), ('2026-10-27T05:30:00+00:00', 20),
                              ('2026-10-01T05:30:00+00:00', 19)):  # outside the 31-day read
                receipt(self.conn, f'ingest:{at}', started_at=at, feed_calls={adzuna: calls, 'Not configured': 99})
        quotas = dashboard_views.overview(self.conn, self.data, NOW)['quotas']
        feed = quotas['feeds']['adzuna']
        # Today 23; the last 7 London days (28 Oct–3 Nov) 23+22+21; this month 23+22.
        self.assertEqual(feed['used'], dict(day=23, seven_days=66, month=45))
        self.assertEqual(feed['run_cap'], 10)
        self.assertEqual((feed['account']['day'], feed['account']['week'], feed['account']['month']), (150, 750, 1500))
        self.assertEqual(quotas['feeds']['reed']['used'], dict(day=0, seven_days=0, month=0))
        self.assertEqual(quotas['spend'], 'not recorded')

    def test_feed_totals_cover_every_run_in_the_period(self):
        adzuna = 'Adzuna UK — London QA and engineering leadership'
        with self.conn:
            for n in range(63):  # retries and reruns: more runs than days
                at = f'2026-11-03T{6 + n // 60:02}:{n % 60:02}:00+00:00'
                receipt(self.conn, f'ingest:{at}', started_at=at, feed_calls={adzuna: 1})
        used = dashboard_views.overview(self.conn, self.data, NOW)['quotas']['feeds']['adzuna']['used']
        self.assertEqual(used, dict(day=63, seven_days=63, month=63))

    def test_the_largest_process_includes_a_child_that_outgrew_its_parent(self):
        with self.conn:
            at = '2026-11-03T05:30:00+00:00'
            child = dict(memory(120, 450, 150), largest_child_peak_rss_bytes=340 * MIB)
            receipt(self.conn, f'ingest:{at}', finished_at=at, memory=child)
        scan = dashboard_views.overview(self.conn, self.data, NOW)['capacity']['scans'][0]
        self.assertEqual(scan['largest_process_bytes'], 340 * MIB)

    def test_codex_allowance_reads_the_live_ledger(self):
        with self.conn:
            for n, day in enumerate(('2026-11-03', '2026-11-03', '2026-11-02')):
                self.conn.execute('INSERT INTO codex_invocations(id,run_id,lane,started_at,day,week) VALUES(?,?,?,?,?,?)',
                                  (f'i{n}', 'research:x', 'research', f'{day}T09:00:00Z', day, '2026-11-02'))
        codex = dashboard_views.overview(self.conn, self.data, NOW)['quotas']['codex']
        self.assertEqual((codex['research_day'], codex['research_week'], codex['day'], codex['week']), (2, 3, 2, 3))
        self.assertEqual((codex['research_day_limit'], codex['research_week_limit'], codex['day_limit'], codex['week_limit']),
                         (12, 84, 16, 100))

    def test_recent_hunt_counts_exclude_corrections_and_older_events(self):
        person = records.create(self.conn, dict(type='person', name='Synthetic Contact'))['record']
        sent = records.outcome(self.conn, person['id'], dict(kind='sent', channel='email', revision=person['revision']))
        replied = records.outcome(self.conn, person['id'], dict(kind='replied', channel='email', revision=sent['record']['revision']))
        records.correct(self.conn, person['id'], dict(outcome_id=replied['outcome']['id'], note='Mistaken tap',
                                                      revision=replied['record']['revision']))
        job = records.create(self.conn, dict(type='job', name='Synthetic Engineering Lead', status='candidate'))['record']
        moved = records.transition(self.conn, job['id'], dict(field='status', to='pursue', revision=job['revision']))['record']
        moved = records.transition(self.conn, job['id'], dict(field='status', to='applied', revision=moved['revision']))['record']
        with self.conn:  # an application recorded 40 days ago stays outside the 30-day window
            self.conn.execute("UPDATE state_events SET recorded_at='2026-09-24T10:00:00+00:00' WHERE to_value='pursue'")
            self.conn.execute(
                "INSERT INTO state_events(entity_id,field,from_value,to_value,at,actor,recorded_at) "
                "VALUES(?, 'status', 'pursue', 'applied', '2026-09-24', 'owner', '2026-09-24T10:00:00+00:00')", (job['id'],))
            self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ('discovery_capacity_admissions_v1', json.dumps(
                [dict(id='a', at='2026-11-01T05:30:00+00:00'), dict(id='b', at='2026-10-20T05:30:00+00:00')])))
        now = dt.datetime.now(dt.timezone.utc)
        hunt = dashboard_views.overview(self.conn, self.data, now)['hunt']
        self.assertEqual(hunt['outcomes'], dict(sent=1))
        self.assertEqual(hunt['job_status'], dict(applied=1))
        self.assertEqual(hunt['days'], 30)
        fixed = dashboard_views.overview(self.conn, self.data, NOW)['hunt']
        self.assertEqual(fixed['admissions_seven_days'], 1)  # 1 November is inside; 20 October is not

    def test_an_unreadable_section_is_unknown_and_the_others_still_answer(self):
        with mock.patch.object(dashboard_views, '_quotas', side_effect=OSError('portals unreadable')):
            out = dashboard_views.overview(self.conn, self.data, NOW)
        self.assertEqual({k:out['quotas'][k] for k in ('state','reason')}, dict(state='unknown', reason='OSError'))
        self.assertEqual(out['quotas']['collection']['state'],'unknown')
        self.assertEqual(out['capacity']['state'], 'ok')
        self.assertEqual(out['hunt']['state'], 'ok')
        self.assertNotIn('portals unreadable', json.dumps(out))

    def test_reads_are_bounded_and_write_nothing_as_receipts_grow(self):
        def measured():
            queries, steps, before = [], [0], self.conn.total_changes
            self.conn.set_trace_callback(queries.append)
            # SQLite's own work, in units of 100 virtual-machine steps: an unbounded scan grows it.
            self.conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100)
            dashboard_views.overview(self.conn, self.data, NOW)
            self.conn.set_trace_callback(None)
            self.conn.set_progress_handler(None, 100)
            self.assertEqual(self.conn.total_changes, before, 'the Dashboard wrote data')
            return len(queries), steps[0]
        with self.conn:
            for n in range(10):
                receipt(self.conn, f'ingest:2026-10-{n + 10:02}T05:30:00+00:00', memory=memory(250, 450, 150))
        person = records.create(self.conn, dict(type='person', name='Synthetic History'))['record']['id']
        job = records.create(self.conn, dict(type='job', name='Synthetic Old Role', status='candidate'))['record']['id']
        small = measured()
        with self.conn:
            for n in range(10000):  # old history outside every window: outcomes, status moves, invocations
                self.conn.execute("INSERT INTO outcomes(record_id,kind,at,actor) VALUES(?,'sent',?,'owner')",
                                  (person, f'2025-{1 + n % 12:02}-{1 + n % 28:02}T10:00:00+00:00'))
                self.conn.execute("INSERT INTO state_events(entity_id,field,from_value,to_value,at,actor,recorded_at) "
                                  "VALUES(?,'status','pursue','applied','2025-03-01','owner',?)", (job, f'2025-03-01T10:{n % 60:02}:00+00:00'))
                self.conn.execute('INSERT INTO codex_invocations(id,run_id,lane,started_at,day,week) VALUES(?,?,?,?,?,?)',
                                  (f'old{n}', 'research:old', 'research', '2025-03-03T09:00:00Z', '2025-03-03', '2025-03-03'))
            for n in range(5000):
                receipt(self.conn, f'email:2026-{1 + n // 450:02}-01T{n % 24:02}:{n % 60:02}:{n % 59:02}.{n:06}+00:00',
                        memory=memory(90, 120, 60), spool=dict(bytes=MIB, count=1))
                receipt(self.conn, f'research:2026-09-01T00:00:{n:06}', status='ok')
        started = time.perf_counter()
        large = measured()
        self.assertEqual(large[0], small[0], 'query count changed with history')
        self.assertLessEqual(large[1], small[1] * 1.5 + 50, 'reads grew with older receipts, outcomes or events')
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_it_never_builds_workspace_state(self):
        with mock.patch.object(workspace_state, 'state', side_effect=AssertionError('built state')), \
                mock.patch.object(workspace_state, '_state', side_effect=AssertionError('built state')):
            self.assertEqual(dashboard_views.overview(self.conn, self.data, NOW)['hunt']['state'], 'ok')

    def test_research_item_failures_preserve_affected_record_ids(self):
        import record_views
        with self.conn:
            receipt(self.conn, 'research:2026-11-03T09:00:00+00:00', status='partial',
                    started_at='2026-11-03T09:00:00+00:00', finished_at='2026-11-03T09:05:00+00:00',
                    items=[dict(id='job-1', outcome='failed', reason='RuntimeError', where='worker.py:10', preparation=True),
                           dict(id='job-2', outcome='failed', reason='RuntimeError', where='worker.py:10')])
        status = record_views.research_status(self.conn)
        self.assertEqual(status['item_failures'], [dict(reason='RuntimeError', where='worker.py:10', count=2)])
        self.assertEqual([i['id'] for i in status['issues'] if i['outcome'] == 'failed'], ['job-1', 'job-2'])


class DashboardRouteTests(ServerCase):
    def test_the_route_answers_while_a_state_build_holds_the_lock(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with workspace_state._BUILD_LOCK:
                held.set()
                release.wait(5)
        holder = threading.Thread(target=hold)
        holder.start()
        try:
            held.wait(2)
            started = time.perf_counter()
            status, headers, body = self.request('GET', '/api/dashboard', origin=False)
            self.assertLess(time.perf_counter() - started, 1)
        finally:
            release.set()
            holder.join()
        self.assertEqual(status, 200)
        self.assertEqual({k for k in body}, {'capacity', 'quotas', 'hunt', 'responsiveness', 'generated_at'})
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_intake_comparison_is_a_separate_read_only_route(self):
        import intake_review
        with mock.patch.object(intake_review, 'compare', return_value={'state':'ok','excluded':[]}) as compare:
            status, headers, body = self.request('GET','/api/intake-review',origin=False)
        self.assertEqual((status,body), (200,{'state':'ok','excluded':[]}))
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(compare.call_count,1)
        code, _ = self.rejected('GET','/api/intake-review',origin=False,headers={'Host':'example.com'})
        self.assertEqual(code,403)

    def test_the_route_refuses_another_host(self):
        code, _ = self.rejected('GET', '/api/dashboard', origin=False, headers={'Host': 'example.com'})
        self.assertEqual(code, 403)


if __name__ == '__main__':
    unittest.main()
