"""Failures become tasks for the owner, once each, and clear themselves when the thing works again."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "workspace")]
import db  # noqa: E402
import failure_tasks as ft  # noqa: E402
import records  # noqa: E402

OK = {"returncode": 0}


def receipt(sources, adapters=None, at="2026-09-29T05:30:00Z"):
    return {"started_at": at, "sources": sources, "adapters": adapters or {"boards": OK}}


class FailureTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def live(self):
        return {r["subject"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM decisions WHERE kind='failure' AND status IN ('open','answered')")}

    def test_a_blocked_source_is_raised_at_once_with_what_follows(self):
        r = ft.for_ingest(self.conn, receipt({"news:tfn": {"state": "error", "kind": "challenged", "status": 403, "label": "Tech Funding News"}}), [])
        self.assertEqual(r["raised"], ["news:tfn"])
        task = self.live()["workspace:source-news-tfn"]
        self.assertIn("Tech Funding News", task["question"])
        self.assertIn("bot challenge", task["question"])
        self.assertEqual(task["recommendation"], "fix")

    def test_a_transient_failure_waits_for_a_second_run(self):
        timeout = {"tender:cf": {"state": "error", "kind": "timeout", "attempts": 3}}
        self.assertEqual(ft.for_ingest(self.conn, receipt(timeout), [])["raised"], [])
        self.assertEqual(ft.for_ingest(self.conn, receipt(timeout), ["tender:cf"])["raised"], ["tender:cf"])
        self.assertEqual(self.live()["workspace:source-tender-cf"]["recommendation"], "wait")

    def test_asking_again_keeps_one_task_and_recovery_withdraws_it(self):
        failing = {"board:lever:x": {"state": "bad-slug", "kind": "not-found", "status": 404}}
        ft.for_ingest(self.conn, receipt(failing), [])
        self.assertEqual(ft.for_ingest(self.conn, receipt(failing), ["board:lever:x"])["raised"], [])
        self.assertEqual(len(self.live()), 1)
        r = ft.for_ingest(self.conn, receipt({"board:lever:x": {"state": "ok", "complete": True}}), [])
        self.assertEqual((r["cleared"], self.live()), (["board:lever:x"], {}))

    def test_partial_or_reused_reads_do_not_clear_an_existing_failure(self):
        scope = 'board:lever:x'
        ft.for_ingest(self.conn, receipt({scope: {'state':'http-403'}}), [])
        for source in ({'state':'partial','complete':False}, {'state':'ok','complete':False}):
            self.assertEqual(ft.for_ingest(self.conn, receipt({scope:source}), [scope])['cleared'], [])
            self.assertTrue(self.live())
        reused = receipt({scope:{'state':'ok','complete':True}})
        reused['reused_pending'] = True
        self.assertEqual(ft.for_ingest(self.conn, reused, [scope])['cleared'], [])
        self.assertTrue(self.live())
        self.assertEqual(ft.for_ingest(self.conn, receipt({scope:{'state':'ok','complete':True}}), [scope])['cleared'], [scope])

    def test_leave_it_is_not_raised_again_until_it_has_worked_in_between(self):
        failing = {"board:lever:x": {"state": "http-403"}}
        ft.for_ingest(self.conn, receipt(failing), [])
        did = self.live()["workspace:source-board-lever-x"]["id"]
        records.answer_decision(self.conn, did, {"choice": "wait", "revision": 1}, actor="owner")
        records.process_decision(self.conn, did, "done", "Left as the owner chose.", revision=2)
        self.assertEqual(ft.for_ingest(self.conn, receipt(failing), ["board:lever:x"])["raised"], [])
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.conn.execute("INSERT INTO source_health(scope,provider,state,observed_at,last_success) VALUES(?,?,?,?,?)",
                          ("board:lever:x", "lever", "ok", later, later))
        self.assertEqual(ft.for_ingest(self.conn, receipt(failing), ["board:lever:x"])["raised"], ["board:lever:x"])

    def test_a_page_cap_or_local_budget_is_not_a_failure(self):
        r = ft.for_ingest(self.conn, receipt({"feed:reed:a": {"state": "partial"}, "feed:adzuna:b": {"state": "budget"}}), [])
        self.assertEqual((r["raised"], self.live()), ([], {}))

    def test_a_crashed_scanner_without_source_receipts_is_raised(self):
        crashed = {"hacker news": {"returncode": 1, "output_tail": "Traceback…\nKeyError: 'hits'"}}
        r = ft.for_ingest(self.conn, receipt({}, crashed), [])
        self.assertEqual(r["raised"], ["hacker news"])
        self.assertIn("exit 1", self.live()["workspace:scanner-hacker-news"]["question"])
        self.assertNotIn("KeyError", self.live()["workspace:scanner-hacker-news"]["question"])
        self.assertEqual(ft.for_ingest(self.conn, receipt({}, {"hacker news": OK}), [])["cleared"], ["hacker news"])

    def test_a_source_failure_does_not_hide_an_unrelated_adapter_crash(self):
        sources = {'board:lever:missing': {'state': 'bad-slug'}}
        adapters = {'boards': {'returncode': 1}, 'hacker news': {'returncode': 1, 'output_tail': 'KeyError: hits'}}
        result = ft.for_ingest(self.conn, receipt(sources, adapters), [])
        self.assertEqual(result['raised'], ['board:lever:missing', 'hacker news'])
        self.assertEqual(ft.for_ingest(self.conn, receipt(sources, adapters), [])['raised'], [])
        self.assertEqual(len(self.live()), 2)
        recovered = ft.for_ingest(self.conn, receipt(sources, {'boards': adapters['boards'], 'hacker news': OK}), [])
        self.assertEqual(recovered['cleared'], ['hacker news'])

    def test_an_unknown_adapter_crash_is_not_assumed_to_be_explained_by_a_source(self):
        result = ft.for_ingest(self.conn, receipt({'news:test': {'state': 'timeout'}},
                              {'custom reader': {'returncode': 1}}), [])
        self.assertEqual(result['raised'], ['custom reader'])

    def test_a_matching_source_failure_does_not_create_a_duplicate_scanner_task(self):
        result = ft.for_ingest(self.conn, receipt({'feed:reed:uk': {'state': 'http-403'}},
                              {'boards and feeds': {'returncode': 1}}), [])
        self.assertEqual(result['raised'], ['feed:reed:uk'])

    def test_an_award_failure_explains_only_the_award_adapter(self):
        result = ft.for_ingest(self.conn, receipt({'award:contracts_finder': {'state': 'http-403'}},
                              {'contract awards': {'returncode': 1}, 'tenders': {'returncode': 1}}), [])
        self.assertEqual(result['raised'], ['award:contracts_finder', 'tenders'])

    def test_verified_scanner_recovery_records_the_complete_read(self):
        from unittest.mock import patch
        with patch('db.now', return_value='2026-09-28T06:00:00Z'):
            ft.for_ingest(self.conn, receipt({}, {'boards':{'returncode':1}}, at='2026-09-28T05:30:00Z'), [])
        ft.for_ingest(self.conn, receipt({}, {'boards':OK}), [])
        row = self.conn.execute("SELECT status, thread_json FROM decisions WHERE kind='failure'").fetchone()
        verification = json.loads(row['thread_json'])[-1]['verification']
        self.assertEqual(row['status'], 'withdrawn')
        self.assertEqual((verification['at'], verification['complete']), ('2026-09-29T05:30:00Z', True))

    def test_a_recent_failed_ingest_does_not_clear_the_job_issue(self):
        data=Path(self.tmp.name)
        ft.watch(self.conn, data, dt.datetime(2026,9,29,6,tzinfo=dt.timezone.utc))
        at='2026-09-29T07:00:00Z'
        self.conn.execute('INSERT INTO scan_runs VALUES(?,?,?,?)',('ingest:'+at,at[:10],json.dumps(dict(started_at=at,status='failed')),'failed'))
        result=ft.watch(self.conn, data, dt.datetime(2026,9,29,8,tzinfo=dt.timezone.utc))
        self.assertNotIn('daily-ingest', result['cleared'])
        self.assertIn('workspace:daily-ingest', self.live())

    def test_overdue_jobs_are_raised_and_cleared(self):
        data = Path(self.tmp.name)
        now = dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc)
        self.conn.execute("INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?)",
                          ("ingest:2026-09-28T05:30:00Z", "2026-09-28", json.dumps({"started_at": "2026-09-28T05:30:00Z"}), "ok"))
        r = ft.watch(self.conn, data, now)
        self.assertEqual(sorted(r["raised"]), ["daily-ingest", "nightly-backup"])
        self.assertIn("overdue", self.live()["workspace:daily-ingest"]["question"])
        (data / "backups").mkdir()
        (data / "backups" / "last-backup.json").write_text(json.dumps({"at": "2026-09-30T02:15:00Z"}))
        self.conn.execute("INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?)",
                          ("ingest:2026-09-30T05:30:00Z", "2026-09-30", json.dumps({"started_at": "2026-09-30T05:30:00Z"}), "ok"))
        self.assertEqual(sorted(ft.watch(self.conn, data, now)["cleared"]), ["daily-ingest", "nightly-backup"])
        self.assertEqual(self.live(), {})

    def test_subjects_are_valid_workspace_slugs(self):
        self.assertEqual(ft.subject_for("feed:adzuna:Adzuna UK — remote"), "workspace:source-feed-adzuna-adzuna-uk-remote")


if __name__ == "__main__":
    unittest.main()
