import json
import datetime as dt
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "workspace")]
import db  # noqa: E402
import ingest  # noqa: E402

ROOT = HERE.parent


class EnvFileTests(unittest.TestCase):
    def test_env_file_forms_and_missing_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# keys\nADZUNA_APP_ID=abc\nexport ADZUNA_APP_KEY='k=1'\n\nREED_API_KEY = \"r\"\nnot a line\n")
            env = ingest.load_env(path)
        self.assertEqual(env, {"ADZUNA_APP_ID": "abc", "ADZUNA_APP_KEY": "k=1", "REED_API_KEY": "r"})
        self.assertEqual(ingest.missing(env, ["ADZUNA_APP_ID", "WO_ALERTS_IMAP_USER"]), ["WO_ALERTS_IMAP_USER"])
        self.assertEqual(ingest.load_env(Path(tmp) / "absent.env"), {})

    def test_the_local_env_file_is_never_committed(self):
        result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
        self.assertEqual(result.returncode, 0, ".env must stay ignored by git")


class IngestTests(unittest.TestCase):
    def test_collection_snapshot_is_released_before_adapter_subprocesses(self):
        import records,weakref
        original=records.state_context
        snapshots=[]
        class Snapshot(dict):pass
        def projection(conn):
            value=Snapshot(original(conn));snapshots.append(weakref.ref(value));return value
        def adapter(argv,env):
            self.assertEqual(len(snapshots),1)
            self.assertIsNone(snapshots[0](),'The full projection must not overlap scanner/import memory')
            self.assertTrue(Path(env['WO_COLLECTION_CONTEXT']).exists())
            return dict(returncode=0,seconds=0,output_tail='synthetic',state='deferred')
        with patch.object(records,'state_context',side_effect=projection),patch.object(ingest,'_run_adapter',side_effect=adapter):
            ingest.run(self.db,self.data,env={},adapters=self.adapters(0))

    def test_request_cap_reason_survives_source_receipt_projection(self):
        from pending_store import ObservationStore
        store = ObservationStore(self.data / 'pending' / 'observations.json')
        store.observe(scope='feed:adzuna:test', source_type='job', provider='adzuna',
            observed_at='2026-11-05T05:30:00Z', state='partial', complete=False,
            items=[{'source_id': 'one'}], absence_authoritative=False,
            detail={'name': 'Example feed', 'stop_reason': 'budget'})
        sources = ingest._source_states(self.data, '2026-11-05T05:29:00Z')
        self.assertEqual(sources['feed:adzuna:test'], dict(state='partial', complete=False, items=1,
            observed_at='2026-11-05T05:30:00Z', stop_reason='budget'))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "work-ops.db"
        conn = db.connect(self.db)
        db.migrate(conn)
        conn.close()
        self.data = root / "data"
        (self.data / "pending").mkdir(parents=True)

    def adapters(self, *codes):
        """Stand-in adapters: each exits with the given code, as a scanner subprocess would."""
        return [(f"adapter{i}", [sys.executable, "-c", f"import sys; print('adapter {i}'); sys.exit({code})"])
                for i, code in enumerate(codes)]

    def test_a_failing_adapter_is_isolated_and_the_run_is_recorded(self):
        receipt = ingest.run(self.db, self.data, env={}, adapters=self.adapters(0, 3))
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual({k: v["returncode"] for k, v in receipt["adapters"].items()}, {"adapter0": 0, "adapter1": 3})
        self.assertIn("defects", receipt["quality"])
        conn = db.connect(self.db)
        row = conn.execute("SELECT run_id, status, receipt_json FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "partial")
        self.assertEqual(json.loads(row["receipt_json"])["adapters"]["adapter1"]["returncode"], 3)
        # The crash and the missing backup receipt wait on the owner as tasks, recorded in the receipt.
        self.assertIn("adapter1", receipt["tasks"]["raised"])
        self.assertIn("nightly-backup", json.loads(row["receipt_json"])["tasks"]["raised"])

    def test_malformed_pending_is_preserved_and_failure_task_clears_after_recovery(self):
        path = self.data / 'pending' / 'observations.json'
        path.write_bytes(b'{"schema_version":1,broken')
        before = path.read_bytes()
        failed = ingest.run(self.db, self.data, env={}, adapters=[])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('JSONDecodeError', failed['import']['error'])
        self.assertIn('import', failed['tasks']['raised'])
        self.assertIn('import failed:', ingest.format_receipt(failed))
        conn = db.connect(self.db)
        saved = json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()[0])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        conn.close()
        path.write_text(json.dumps(dict(schema_version=1, sources={}, items={})))
        recovered = ingest.run(self.db, self.data, env={}, adapters=[])
        self.assertEqual(recovered['status'], 'ok')
        self.assertIn('import', recovered['tasks']['cleared'])
        conn = db.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT status FROM decisions WHERE subject='workspace:ingest-import'").fetchone()[0], 'withdrawn')
        conn.close()

    def test_import_exception_rolls_back_rows_and_still_reports_source_and_adapter_failures(self):
        path = self.data / 'pending' / 'observations.json'
        when = (ingest.dt.datetime.now(ingest.dt.timezone.utc) + ingest.dt.timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(dict(sources={
            'board:lever:missing': dict(state='bad-slug', observed_at=when, provider='lever')}, items={
            'broken': dict(kind='tender', source_scope='tender:test', source_id='1', last_seen_at=when, current='bad shape')})))
        (self.data / 'pending' / '2026-10-04.tsv').write_text('company\ttitle\turl\nExample\tHead of Quality\thttps://example.test/job\n')
        before = path.read_bytes()
        failed = ingest.run(self.db, self.data, env={}, adapters=self.adapters(1))
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue({'import', 'adapter0', 'board:lever:missing'} <= set(failed['tasks']['raised']))
        self.assertEqual(path.read_bytes(), before)
        conn = db.connect(self.db)
        for table in ('records', 'links', 'import_runs', 'source_health'):
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0, table)
        conn.close()

    def test_failed_import_returns_nonzero_from_cli_while_partial_reads_keep_existing_exit(self):
        import wo
        for status, expected in [('failed', 1), ('partial', 0)]:
            value = dict(status=status, adapters={}, quality=dict(defects=0, gaps=0),
                         **({'import': {'error': 'ValueError: invalid evidence'}} if status == 'failed' else {}))
            with self.subTest(status=status), patch.object(ingest, 'run', return_value=value), \
                 patch.object(ingest, 'load_env', return_value={}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wo.main(['--db', str(self.db), 'ingest']), expected)

    def test_the_receipt_counts_adverts_no_scanner_has_seen_lately(self):
        import records
        conn = db.connect(self.db)
        with records.transaction(conn):
            for k, seen in (("A", "2020-01-01T05:30:00Z"), ("B", db.now())):
                rid = records.create(conn, dict(type="job", name=f"Head of Quality {k}"))["record"]["id"]
                records.add_observation(conn, rid, "board:ashby:test", seen, json.dumps({"title": k}))
        conn.close()
        receipt = ingest.run(self.db, self.data, env={}, adapters=self.adapters(0))
        self.assertEqual(receipt["stale"], {"unseen_adverts": 1, "days": 14})

    def test_after_a_successful_import_old_closed_advert_text_is_dropped_and_counted(self):
        path = self.data / "pending" / "observations.json"
        item = dict(source_scope="board:ashby:test", source_id="1", provider="ashby", kind="job",
                    first_seen_at="2026-01-01T05:30:00Z", last_seen_at="2026-01-01T05:30:00Z",
                    source_lifecycle="closed", closed_at="2026-01-03T05:30:00Z", absence_observations=[],
                    current=dict(title="Head of QA", url="https://jobs.example/1", eligible=False, text="Long"))
        path.write_text(json.dumps(dict(schema_version=1, generated_at="2026-01-03T05:30:00Z", sources={},
                                        items={"board:ashby:test\x1f1": item})))
        receipt = ingest.run(self.db, self.data, env={}, adapters=[])
        self.assertEqual((receipt["status"], receipt["import"]["compacted"]), ("ok", 1))
        self.assertNotIn("text", json.loads(path.read_text())["items"]["board:ashby:test\x1f1"]["current"])
        again = ingest.run(self.db, self.data, env={}, adapters=[])
        self.assertEqual(again["import"]["compacted"], 0)

    def test_the_receipt_times_each_step_and_the_import_write_lock(self):
        receipt = ingest.run(self.db, self.data, env={}, adapters=[])
        self.assertEqual(set(receipt["phase_seconds"]), {"backup", "adapters", "import", "checks"})
        self.assertIsInstance(receipt["import"]["write_lock_seconds"], float)
        self.assertLessEqual(receipt["import"]["write_lock_seconds"], receipt["phase_seconds"]["import"] + 0.1)

    def cgroup(self, unit="wo-scan.service", **files):
        root = Path(self.tmp.name) / "cgroup"
        group = root / "system.slice" / unit
        group.mkdir(parents=True)
        for name, text in files.items():
            (group / name.replace("_", ".", 1)).write_text(text)
        proc = Path(self.tmp.name) / "proc-cgroup"
        proc.write_text(f"0::/system.slice/{unit}\n")
        return proc, root

    def test_memory_evidence_separates_file_cache_from_process_memory(self):
        proc, root = self.cgroup(memory_peak="532705280\n", memory_max="536870912\n",
                                 memory_stat="anon 120000000\nfile 400000000\nkernel 9000\nshmem 0\n",
                                 memory_events="low 0\nhigh 0\nmax 3\noom 0\noom_kill 0\noom_group_kill 0\n")
        evidence = ingest.memory_evidence(proc_cgroup=proc, cgroup_root=root)
        self.assertEqual(evidence["cgroup"], {
            "state": "observed", "peak_bytes": 532705280, "limit_bytes": 536870912,
            "anon_bytes": 120000000, "file_bytes": 400000000,
            "events": {"high": 0, "max": 3, "oom": 0, "oom_kill": 0}})
        self.assertGreater(evidence["process_peak_rss_bytes"], 1_000_000)
        self.assertIsInstance(evidence["largest_child_peak_rss_bytes"], int)

    def test_memory_evidence_keeps_unknowns_unknown(self):
        proc, root = self.cgroup(memory_peak="not a number\n", memory_max="max\n", memory_stat="anon x\n")
        evidence = ingest.memory_evidence(proc_cgroup=proc, cgroup_root=root)
        self.assertEqual(evidence["cgroup"], {"state": "observed", "peak_bytes": None, "limit_bytes": None,
                                              "anon_bytes": None, "file_bytes": None, "events": {}})
        missing = ingest.memory_evidence(proc_cgroup=Path(self.tmp.name) / "absent", cgroup_root=root)
        self.assertEqual(missing["cgroup"], {"state": "unknown"})
        escape = Path(self.tmp.name) / "escape"
        escape.write_text("0::/../../etc\n")
        self.assertEqual(ingest.memory_evidence(proc_cgroup=escape, cgroup_root=root)["cgroup"], {"state": "unknown"})

    def test_the_receipt_records_this_runs_memory(self):
        receipt = ingest.run(self.db, self.data, env={}, adapters=self.adapters(0))
        self.assertIn("cgroup", receipt["memory"])
        self.assertGreater(receipt["memory"]["process_peak_rss_bytes"], 0)
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        stored = json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()[0])
        self.assertEqual(stored["memory"]["process_peak_rss_bytes"], receipt["memory"]["process_peak_rss_bytes"])

    def test_the_board_scan_alone_receives_the_evidence_records_hold(self):
        import records
        from pending_store import PROTECTION
        conn = db.connect(self.db)
        held = records.create(conn, dict(type="job", name="Director of Marketing", organisation="GH Co",
                                         url="https://gh/6#apply"))["record"]
        records.create(conn, dict(type="job", name="Old Role", organisation="Gone Ltd", url="https://gh/7"))
        conn.execute("UPDATE records SET archived_at='2026-10-01T00:00:00Z' WHERE name='Old Role'")
        conn.execute("INSERT INTO source_records VALUES('board:greenhouse:ghco','https://gh/2',?)", (held["id"],))
        conn.execute("INSERT INTO source_records VALUES('hn:who-is-hiring:1','c1',?)", (held["id"],))
        conn.commit()
        conn.close()
        path = self.data / "pending" / PROTECTION
        seen = {}
        def adapter(argv, env):
            seen[argv[0]] = json.loads(path.read_text()) if path.exists() else None
            return {"returncode": 0, "seconds": 0, "output_tail": ""}
        with patch.object(ingest, "_run_adapter", side_effect=adapter):
            ingest.run(self.db, self.data, env={}, adapters=[("boards and feeds", ["boards"]), ("tenders", ["tenders"])])
        manifest = seen["boards"]
        self.assertEqual(manifest["identities"], [["board:greenhouse:ghco", "https://gh/2"]])
        # Archived adverts still own their URL; only active jobs can attach a sighting by role.
        self.assertEqual(manifest["urls"], ["https://gh/6", "https://gh/7"])
        self.assertEqual(manifest["roles"], [["gh co", "director of marketing"]])
        self.assertFalse(path.exists(), "a later standalone scan must not trust this run's manifest")
        seen.clear()
        with patch.object(ingest, "_run_adapter", side_effect=adapter):
            ingest.run(self.db, self.data, env={}, adapters=[("tenders", ["tenders"])])
        self.assertEqual(seen, {"tenders": None})

    def test_records_keep_their_evidence_when_unmatched_advert_text_is_dropped(self):
        import records
        import test_scan_boards as fixtures
        conn = db.connect(self.db)
        held = records.create(conn, dict(type="job", name="Director of Marketing", organisation="GH Co",
                                         url="https://gh/6", summary="Saved by hand"))["record"]
        conn.close()
        root = Path(self.tmp.name)
        (root / "portals.yml").write_text(fixtures.PORTALS)
        (root / "fx").mkdir()
        for name, payload in (("greenhouse-ghco", fixtures.GH), ("lever-leverco", fixtures.LEVER),
                              ("ashby-ashbyco", fixtures.ASHBY), ("smartrecruiters-SRCo", fixtures.SMARTRECRUITERS)):
            (root / "fx" / f"{name}.json").write_text(json.dumps(payload))
        scan = [("boards", [sys.executable, str(HERE / "scan_boards.py"), "--portals", str(root / "portals.yml"),
                            "--out", "{data}", "--offline-fixture", str(root / "fx"), "--no-feeds"])]
        import record_imports
        listed = record_imports.protected_sources
        def before_late_save(conn):
            sources = listed(conn)
            # Saved while the scan runs: after the list was written, before the import.
            records.create(conn, dict(type="job", name="Junior Test Manager", organisation="GH Co",
                                      url="https://gh/2", summary="Keep me"))
            return sources
        with patch.object(record_imports, "protected_sources", side_effect=before_late_save):
            receipt = ingest.run(self.db, self.data, env={}, adapters=scan)
        self.assertEqual(receipt["adapters"]["boards"]["returncode"], 0, receipt["adapters"]["boards"]["output_tail"])
        self.assertEqual((receipt["retention"]["manifest"], receipt["retention"]["held"]), ("ok", 1))
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        summary = lambda url: conn.execute("SELECT summary FROM records WHERE url=?", (url,)).fetchone()[0]
        self.assertEqual(summary("https://gh/6"), "Saved by hand")
        payload = json.loads(conn.execute("SELECT payload_json FROM observations WHERE record_id=? AND source LIKE 'board:%'",
                                          (held["id"],)).fetchone()[0])
        self.assertEqual(payload["current"]["text"], "B2B SaaS, employment contract")
        self.assertNotIn("compacted_at", payload)
        # The late save is missing from this run's list: its advert waits for tomorrow's full sighting
        # instead of replacing the saved summary with nothing.
        self.assertEqual(summary("https://gh/2"), "Keep me")
        late = conn.execute("SELECT id FROM records WHERE url='https://gh/2'").fetchone()[0]
        self.assertIsNone(conn.execute("SELECT 1 FROM observations WHERE record_id=?", (late,)).fetchone())

    def test_a_failed_list_or_scan_never_blocks_collection_or_reports_old_counts(self):
        import record_imports
        import sqlite3
        from pending_store import PROTECTION
        (self.data / "pending" / "last-run.json").write_text(json.dumps(
            dict(date="2026-11-03", boards={}, retention=dict(manifest="ok", text_dropped=9, held=0))))
        ran = []
        def adapter(argv, env):
            ran.append((self.data / "pending" / PROTECTION).exists())
            return {"returncode": 0, "seconds": 0, "output_tail": ""}
        with patch.object(record_imports, "protected_sources", side_effect=sqlite3.OperationalError("locked")), \
             patch.object(ingest, "_run_adapter", side_effect=adapter):
            receipt = ingest.run(self.db, self.data, env={}, adapters=[("boards", ["boards"])])
        self.assertEqual(ran, [False], "the scan still runs, keeping every advert whole")
        self.assertEqual(receipt["retention"]["list_error"], "OperationalError")
        crashed = ingest.run(self.db, self.data, env={}, adapters=[("boards", [sys.executable, "-c", "raise SystemExit(3)"])])
        self.assertEqual(crashed["retention"], {"manifest": "unreported"})

    def test_the_receipt_records_store_size_retention_and_memory_per_phase(self):
        path = self.data / "pending" / "observations.json"
        path.write_text(json.dumps(dict(schema_version=1, generated_at=None, sources={}, items={})))
        (self.data / "pending" / "last-run.json").write_text(json.dumps(
            dict(date="2026-11-04", boards={}, retention=dict(manifest="ok", text_dropped=4, held=1))))
        receipt = ingest.run(self.db, self.data, env={}, adapters=[("boards", [sys.executable, "-c", "pass"])])
        self.assertEqual(receipt["store"], {"bytes": path.stat().st_size})
        self.assertEqual(receipt["retention"], {"manifest": "ok", "text_dropped": 4, "held": 1})
        self.assertEqual(set(receipt["phase_peak_rss_bytes"]), {"adapters", "backup", "import", "checks"})
        peaks = [receipt["phase_peak_rss_bytes"][p] for p in ("adapters", "backup", "import", "checks")]
        self.assertEqual(peaks, sorted(peaks), "each figure is the process high-water mark so far")
        self.assertIsInstance(receipt["adapters"]["boards"]["children_peak_rss_bytes"], int)
        quiet = ingest.run(self.db, self.data, env={}, adapters=[("tenders", [sys.executable, "-c", "pass"])])
        self.assertNotIn("retention", quiet, "only a run that scanned the boards reports their retention")

    def test_import_failure_leaves_a_safe_failed_receipt(self):
        import record_imports
        with patch.object(record_imports, 'cmd_import_scans', side_effect=ValueError('token=private-secret')):
            result = ingest.run(self.db, self.data, env={}, adapters=[])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('memory', result)
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT * FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'failed')
        self.assertNotIn('private-secret', row['receipt_json'])
        self.assertEqual(json.loads(row['receipt_json'])['error_kind'], 'ValueError')
        self.assertIn('import', json.loads(row['receipt_json'])['failures'])

    def test_quality_failure_leaves_a_safe_failed_receipt_and_propagates(self):
        import quality
        with patch.object(quality, 'check', side_effect=ValueError('token=private-secret')):
            with self.assertRaises(ValueError):
                ingest.run(self.db, self.data, env={}, adapters=[])
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT * FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertNotIn('private-secret', row['receipt_json'])
        self.assertIn('daily-ingest', json.loads(row['receipt_json'])['failures'])
        self.assertIn('memory', json.loads(row['receipt_json']))

    def test_adapter_requests_are_logged_under_the_run_and_summarised_in_the_receipt(self):
        writer = ("import sys; sys.path.insert(0, sys.argv[1]); import web_read; "
                  "web_read.log({'url': 'https://a.test/x', 'ok': True, 'attempts': 2}); "
                  "web_read.log({'url': 'https://b.test/y', 'ok': False, 'kind': 'challenged', 'attempts': 1})")
        log = self.data / "logs" / "fetch.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"run": "fetch:earlier", "ok": False, "kind": "timeout"}) + "\n")
        receipt = ingest.run(self.db, self.data, env={}, adapters=[("logger", [sys.executable, "-c", writer, str(HERE)])])
        self.assertEqual(receipt["fetch"], {"requests": 2, "recovered": 1, "failed": {"challenged": 1}})
        self.assertIn("requests: 2, 1 recovered on retry, 1 challenged", ingest.format_receipt(receipt))

    def test_a_second_run_is_refused_while_one_holds_the_lock(self):
        with ingest.writer_lock(self.data):
            with self.assertRaises(ingest.Busy):
                ingest.run(self.db, self.data, env={}, adapters=self.adapters(0))

    def test_backups_rotate(self):
        backups = self.data / "backups"
        for i in range(9):
            ingest.backup(self.db, backups, keep=7, stamp=f"20260924-0000{i:02d}")
        kept = sorted(p.name for p in backups.glob("work-ops-pre-ingest-*.db"))
        self.assertEqual(len(kept), 7)
        self.assertEqual(kept[0], "work-ops-pre-ingest-20260924-000002.db")

    def test_dry_run_leaves_the_database_and_pending_store_untouched(self):
        (self.data / "pending" / "observations.json").write_text("{}")
        before = self.db.read_bytes()
        receipt = ingest.run(self.db, self.data, env={}, adapters=[
            ("writer", [sys.executable, "-c", "import sys, pathlib; pathlib.Path(sys.argv[1], 'pending', 'touched').write_text('x')",
                        "{data}"])], dry_run=True)
        self.addCleanup(shutil.rmtree, receipt["dry_run"], True)  # kept on purpose for --reuse-pending; tests remove theirs
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.data / "pending" / "touched").exists())
        self.assertTrue(Path(receipt["dry_run"], "data", "pending", "touched").exists())

    def test_a_dry_run_reads_the_real_backup_receipt(self):
        (self.data / "backups").mkdir(exist_ok=True)
        (self.data / "backups" / "last-backup.json").write_text(json.dumps({"at": ingest.dt.datetime.now(ingest.dt.timezone.utc).isoformat()}))
        receipt = ingest.run(self.db, self.data, env={}, adapters=[], dry_run=True)
        self.addCleanup(shutil.rmtree, receipt["dry_run"], True)
        self.assertNotIn("nightly-backup", receipt["tasks"]["raised"])

    def test_reuse_pending_imports_without_requests_and_keeps_original_source_times(self):
        saved = Path(self.tmp.name) / "saved-pending"
        saved.mkdir()
        (saved / "observations.json").write_text(json.dumps({
            "generated_at": "2026-09-24T12:00:00Z", "items": {},
            "sources": {"feed:reed:test": {"provider": "reed", "state": "partial", "complete": False,
                                          "observed_at": "2026-09-24T12:00:00Z", "item_ids": []}}}))
        (saved / "last-run.json").write_text(json.dumps({"feeds": {"test": {"calls": 25}}}))
        with patch.object(ingest, "_run_adapter", side_effect=AssertionError("no network during reuse")):
            receipt = ingest.run(self.db, self.data, env={}, reuse_pending=saved)
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual((receipt["failures"], receipt["capped"]), ([], ["feed:reed:test"]))
        self.assertEqual(receipt["feed_calls"], {})
        self.assertEqual(receipt["sources"]["feed:reed:test"]["observed_at"], "2026-09-24T12:00:00Z")
        self.assertTrue(receipt["reused_pending"])
        self.assertTrue((self.data / "pending" / "observations.json").exists())

    def test_a_source_read_in_the_first_second_of_the_run_is_in_the_receipt(self):
        """The run starts at whole seconds; adapters stamp microseconds. `.` sorts before `Z`."""
        (self.data / "pending" / "observations.json").write_text(json.dumps({"sources": {
            "board:lever:early": {"state": "error", "observed_at": "2026-09-25T18:07:19.954646Z"},
            "board:lever:before": {"state": "ok", "observed_at": "2026-09-25T18:07:18.999999Z"}}}))
        states = ingest._source_states(self.data, "2026-09-25T18:07:19Z")
        self.assertEqual(sorted(states), ["board:lever:early"])

    def test_a_keyless_run_calls_only_the_sources_that_need_no_key(self):
        """Free sources only: boards without the Adzuna and Reed feeds, tenders, awards, HN and funding news;
        no mailbox."""
        full = dict(ingest.default_adapters())
        keyless = dict(ingest.default_adapters(keyless=True))
        self.assertIn("job alerts", full)
        self.assertNotIn("--no-feeds", full["boards and feeds"])
        self.assertEqual(sorted(keyless), ["boards", "contract awards", "funding news", "hacker news", "tenders"])
        self.assertIn("--no-feeds", keyless["boards"])
        ran = []
        with patch.object(ingest, "_run_adapter", side_effect=lambda argv, env: ran.append(argv) or
                          {"returncode": 0, "seconds": 0, "output_tail": ""}):
            receipt = ingest.run(self.db, self.data, env={}, keyless=True)
        self.assertTrue(receipt["keyless"])
        self.assertEqual(len(ran), 5)
        self.assertFalse(any("scan_alerts.py" in " ".join(argv) for argv in ran))
        self.assertIn("free sources only", ingest.format_receipt(receipt))

    def test_an_approved_keyed_source_runs_by_name_with_its_targets(self):
        """#29: a keyless run may call Companies House when asked by name, and nothing else keyed."""
        self.assertIn("companies house", dict(ingest.default_adapters()))
        keyed = dict(ingest.default_adapters(keyless=True, keyed=("companies-house",)))
        self.assertEqual(sorted(keyed), ["boards", "companies house", "contract awards", "funding news", "hacker news", "tenders"])
        with self.assertRaisesRegex(ValueError, "not an approved keyed source"):
            ingest.default_adapters(keyless=True, keyed=("mailbox",))
        feeds = dict(ingest.default_adapters(keyless=True, keyed=("feeds",)))  # #39: Adzuna and Reed, still no mailbox
        self.assertEqual(sorted(feeds), ["boards and feeds", "contract awards", "funding news", "hacker news", "tenders"])
        self.assertNotIn("--no-feeds", feeds["boards and feeds"])
        conn = db.connect(self.db)
        import records
        funded = records.create(conn, dict(type="company", name="Zephyr Labs"))["record"]
        records.create(conn, dict(type="company", name="Quiet Ltd"))
        today = __import__("datetime").date.today().isoformat()
        records.add_observation(conn, funded["id"], "news:uktn", today, json.dumps(
            {"company": "Zephyr Labs", "amount": "£3m", "published": today, "headline": "Zephyr Labs raises £3m"}))
        conn.execute("INSERT INTO source_records VALUES('companies-house','00000001',?)", (funded["id"],))
        conn.commit()
        conn.close()
        ran = []
        with patch.object(ingest, "_run_adapter", side_effect=lambda argv, env: ran.append(argv) or
                          {"returncode": 0, "seconds": 0, "output_tail": ""}):
            receipt = ingest.run(self.db, self.data, env={}, keyless=True, keyed=("companies-house",))
        self.assertTrue(any("scan_companies_house.py" in " ".join(argv) for argv in ran))
        targets = json.loads((self.data / "pending" / ingest.CH_TARGETS).read_text())
        self.assertEqual(targets, [{"id": funded["id"], "name": "Zephyr Labs", "company_number": "00000001"}])
        self.assertEqual(receipt["keyed"], ["companies-house"])
        self.assertIn("approved keyed: companies-house", ingest.format_receipt(receipt))

    def test_alerts_are_explicitly_keyed_and_cli_accepts_the_group(self):
        import wo
        selected = dict(ingest.default_adapters(keyless=True, keyed=("alerts",)))
        self.assertIn("job alerts", selected)
        self.assertNotIn("job alerts", dict(ingest.default_adapters(keyless=True)))
        with patch.object(ingest, "run", return_value=dict(status="ok", adapters={}, quality=dict(defects=0, gaps=0))) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wo.main(["--db", str(self.db), "ingest", "--keyless", "--keyed", "alerts"]), 0)
        self.assertEqual(run.call_args.kwargs["keyed"], ["alerts"])
        receipt = dict(status="ok", adapters={}, quality=dict(defects=0, gaps=0), keyless=True, keyed=["alerts"])
        self.assertNotIn("no mailbox", ingest.format_receipt(receipt))

    def test_enabled_alerts_without_credentials_report_source_health_and_failure_task(self):
        portals = Path(self.tmp.name) / "portals.yml"
        portals.write_text("alerts:\n  enabled: true\n")
        missing_env = {key: "" for key in ingest.ALERT_KEYS} | {"CREDENTIALS_DIRECTORY": str(Path(self.tmp.name) / "absent")}
        adapter = [sys.executable, str(HERE / "scan_alerts.py"), "--portals", str(portals), "--out", "{data}"]
        receipt = ingest.run(self.db, self.data, env=missing_env, adapters=[("job alerts", adapter), *self.adapters(0)])
        self.assertEqual(receipt["status"], "partial")
        self.assertNotEqual(receipt["adapters"]["job alerts"]["returncode"], 0)
        self.assertRegex(ingest.format_receipt(receipt), r"FAIL\s+job alerts")
        self.assertEqual(receipt["adapters"]["adapter0"]["returncode"], 0)
        self.assertEqual(receipt["sources"]["alert:linkedin"]["state"], "config")
        self.assertIn("alert:linkedin", receipt["tasks"]["raised"])
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT state FROM source_health WHERE scope='alert:linkedin'").fetchone()[0], "config")

    def test_selected_disabled_alerts_are_skipped_without_claiming_a_source_read(self):
        portals = Path(self.tmp.name) / "portals.yml"
        portals.write_text("alerts:\n  enabled: false\n")
        missing_env = {key: "" for key in ingest.ALERT_KEYS} | {"CREDENTIALS_DIRECTORY": str(Path(self.tmp.name) / "absent")}
        adapter = dict(ingest.default_adapters(keyless=True, keyed=("alerts",)))["job alerts"]
        with patch.object(ingest, "PORTALS", portals):
            receipt = ingest.run(self.db, self.data, env=missing_env, adapters=[("job alerts", adapter)],
                                 keyless=True, keyed=("alerts",))
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["adapters"]["job alerts"]["returncode"], 0)
        self.assertEqual(receipt["adapters"]["job alerts"].get("state"), "disabled")
        self.assertEqual(receipt["sources"], {})
        self.assertEqual(receipt["failures"], [])
        self.assertNotIn("job alerts", receipt["tasks"]["raised"])
        self.assertNotIn("alert:linkedin", receipt["tasks"]["raised"])
        log = ingest.format_receipt(receipt)
        self.assertRegex(log, r"SKIP\s+job alerts.*disabled.*no source read")
        self.assertNotRegex(log, r"\bok\s+job alerts")
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0], 0)
        saved = json.loads(conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'ingest:%'").fetchone()[0])
        self.assertEqual(saved["adapters"]["job alerts"].get("state"), "disabled")
        self.assertFalse((self.data / "pending" / "observations.json").exists())

    def test_skipped_adapter_does_not_clear_an_existing_scanner_failure(self):
        failed_adapter = [("job alerts", [sys.executable, "-c", "raise SystemExit(1)"])]
        first = ingest.run(self.db, self.data, env={}, adapters=failed_adapter)
        self.assertIn("job alerts", first["tasks"]["raised"])
        for state in ("disabled", "skipped", "deferred"):
            with self.subTest(state=state):
                command = [sys.executable, "-c", f"print({json.dumps({'state': state})!r})"]
                receipt = ingest.run(self.db, self.data, env={}, adapters=[("job alerts", command)])
                self.assertNotIn("job alerts", receipt["tasks"]["cleared"])
                self.assertRegex(ingest.format_receipt(receipt), r"SKIP\s+job alerts")
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT status FROM decisions WHERE subject='workspace:scanner-job-alerts'").fetchone()[0], "open")

    def test_a_nonzero_adapter_exit_cannot_be_disguised_by_a_disabled_status(self):
        command = [sys.executable, "-c", 'print(\'{"state": "disabled"}\'); raise SystemExit(1)']
        receipt = ingest.run(self.db, self.data, env={}, adapters=[("job alerts", command)])
        self.assertIn("job alerts", receipt["failures"])
        self.assertIn("job alerts", receipt["tasks"]["raised"])
        self.assertRegex(ingest.format_receipt(receipt), r"FAIL\s+job alerts")
        self.assertNotRegex(ingest.format_receipt(receipt), r"SKIP\s+job alerts")

    def test_alert_layout_failure_retries_raise_then_clear_failure_task(self):
        from test_scan_alerts import alert_email, PORTALS
        portals = Path(self.tmp.name) / "portals.yml"
        portals.write_text(PORTALS)
        inbox = Path(self.tmp.name) / "inbox"
        inbox.mkdir()
        message = inbox / "1.eml"
        message.write_bytes(alert_email("<p>unsupported layout</p>"))
        adapter = [("job alerts", [sys.executable, str(HERE / "scan_alerts.py"), "--portals", str(portals), "--out", "{data}", "--offline-fixture", str(inbox)])]
        first = ingest.run(self.db, self.data, env={}, adapters=adapter)
        self.assertEqual(first["sources"]["alert:linkedin"]["state"], "error")
        second = ingest.run(self.db, self.data, env={}, adapters=adapter)
        self.assertIn("alert:linkedin", second["tasks"]["raised"])
        message.write_bytes(alert_email())
        third = ingest.run(self.db, self.data, env={}, adapters=adapter)
        self.assertEqual(third["sources"]["alert:linkedin"]["state"], "ok")
        self.assertIn("alert:linkedin", third["tasks"]["cleared"])

    def test_reuse_pending_requires_observation_evidence(self):
        with self.assertRaisesRegex(ValueError, "observations"):
            ingest.run(self.db, self.data, env={}, reuse_pending=Path(self.tmp.name) / "missing")

    @patch('db.now', return_value='2026-09-24T12:00:00Z')
    @patch('evidence_checks.utc_now', return_value=dt.datetime(2026,9,24,12,tzinfo=dt.timezone.utc))
    def test_spool_dry_run_reads_original_source_but_changes_only_scratch_state(self, *_clocks):
        from mail_intake import MailSpool
        import scan_alerts
        from test_scan_alerts import HTML, PORTALS, alert_email
        spool_path = Path(self.tmp.name) / 'private-spool.db'
        spool = MailSpool(spool_path)
        def receive(html, when):
            spool.receive(alert_email(html), envelope_from='sender@example.test',
                          envelope_to='alerts@example.com', received_at=when)
        receive(HTML, '2026-09-23T06:00:00Z')
        portals = Path(self.tmp.name) / 'spool-portals.yml'
        portals.write_text(PORTALS.replace('enabled: true', 'enabled: true\n  transport: spool'))
        env = {'WO_ALERTS_SPOOL': str(spool_path)}
        scan_alerts.run(portals, self.data, '2026-10-07', env=env)
        before = {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        receive(HTML.replace('4012345678', '4022222222'), '2026-09-24T06:00:00Z')
        spool_before = spool_path.read_bytes()
        adapter = [('job alerts', [sys.executable, str(HERE / 'scan_alerts.py'), '--portals', str(portals), '--out', '{data}'])]
        result = ingest.run(self.db, self.data, env=env, adapters=adapter, dry_run=True)
        scratch = Path(result['dry_run'])
        self.addCleanup(shutil.rmtree, scratch)
        self.assertEqual(result['adapters']['job alerts']['returncode'], 0)
        self.assertEqual(json.loads((scratch / 'data/pending/alerts/spool-state.json').read_text())['last_sequence'], 2)
        self.assertEqual({str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob('*') if p.is_file()}, before)
        self.assertEqual(spool_path.read_bytes(), spool_before)
        preview = db.connect(scratch / 'work-ops.db')
        try:
            dates = {row[0] for row in preview.execute("SELECT observed_at FROM observations WHERE source='alert:linkedin'")}
            self.assertTrue(dates)
            self.assertTrue(dates <= {'2026-09-23T06:00:00Z', '2026-09-24T06:00:00Z'})
        finally:
            preview.close()
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_ingest_probe_delegates_spool_without_imap_credentials(self):
        portals = Path(self.tmp.name) / 'spool-portals.yml'
        portals.write_text('alerts:\n  enabled: false\n  transport: spool\n')
        with patch.object(ingest, 'PORTALS', portals), \
             patch.object(ingest.subprocess, 'run', return_value=type('Result', (), {'returncode': 0})()) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ingest.probe({'WO_ALERTS_SPOOL': '/synthetic/spool.db'}), 0)
        command = run.call_args_list[-1].args[0]
        self.assertIn(str(HERE / 'scan_alerts.py'), command)
        self.assertIn('--probe', command)
        self.assertIn(str(portals), command)

    def test_reuse_pending_refuses_to_replace_newer_source_observations(self):
        source = {"sources": {"feed:reed:test": {"observed_at": "2026-09-24T12:00:00Z"}}, "items": {}}
        path = self.data / "pending" / "observations.json"
        path.write_text(json.dumps(source))
        saved = Path(self.tmp.name) / "older"
        saved.mkdir()
        source["sources"]["feed:reed:test"]["observed_at"] = "2026-09-23T12:00:00Z"
        (saved / "observations.json").write_text(json.dumps(source))
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "newer"):
            ingest.run(self.db, self.data, env={}, reuse_pending=saved)
        self.assertEqual(path.read_bytes(), before)


# A presence-only read, observed at READ_AT, with argv: tools dir, data dir, item count, state.
READ = ("import os, sys; sys.path.insert(0, sys.argv[1]); from pending_store import ObservationStore; "
        "items = [dict(source_id=str(i), current=dict(title='Head of Quality', company='Example', eligible=True, "
        "url=f'https://jobs.example.test/{i}')) for i in range(int(sys.argv[3]))]; "
        "ObservationStore(os.path.join(sys.argv[2], 'pending', 'observations.json')).observe(scope='alert:test', "
        "source_type='job', provider='test', observed_at=os.environ['READ_AT'], state=sys.argv[4], complete=True, "
        "items=items, absence_authoritative=False)")


class IncrementalIngestTests(unittest.TestCase):
    """The 15-minute email run: a read that staged nothing is recorded without a backup or replay."""
    setUp = IngestTests.setUp

    def read(self, items=0, state="empty", code=0):
        argv = [sys.executable, "-c", READ + (f"; sys.exit({code})" if code else ""), str(HERE), "{data}", str(items), state]
        return [("reader", argv)]

    def at(self, minutes):
        """A read time after the run starts, so the receipt counts it as read in this run."""
        return (ingest.dt.datetime.now(ingest.dt.timezone.utc) + ingest.dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def run_read(self, db_path=None, data=None, *, minutes=5, incremental=True, **read):
        return ingest.run(db_path or self.db, data or self.data, env={"READ_AT": self.at(minutes)},
                          adapters=self.read(**read), incremental=incremental)

    def backups(self, data=None):
        return len(list(((data or self.data) / "backups").glob("work-ops-pre-ingest-*.db")))

    def count(self, table, db_path=None):
        conn = db.connect(db_path or self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_a_read_that_staged_nothing_matches_a_full_replay_without_a_backup_or_report(self):
        first = self.run_read(minutes=1, items=1, state="ok")
        self.assertEqual((first["status"], self.count("records WHERE type='job'")), ("ok", 1), first)
        self.assertNotIn("import_skipped", first)
        twin = Path(self.tmp.name) / "twin"
        twin.mkdir()
        shutil.copy(self.db, twin / "work-ops.db")
        shutil.copytree(self.data, twin / "data")
        at = self.at(20)
        shutil.rmtree(twin / "data" / "backups")
        fast = ingest.run(self.db, self.data, env={"READ_AT": at}, adapters=self.read(), incremental=True)
        full = ingest.run(twin / "work-ops.db", twin / "data", env={"READ_AT": at}, adapters=self.read())
        self.assertEqual(fast["import_skipped"], "no new items")
        self.assertNotIn("import", fast)
        self.assertNotIn("backup", fast)
        self.assertIn("import", full)
        self.assertEqual((fast["status"], fast["sources"], fast["tasks"]), (full["status"], full["sources"], full["tasks"]))
        self.assertEqual((self.backups(), self.backups(twin / "data")), (1, 1))
        self.assertEqual((self.count("import_runs"), self.count("import_runs", twin / "work-ops.db")), (1, 2))
        # Apart from the import report and run receipts, the database ends exactly as a replay leaves it.
        left, right = db.connect(self.db), db.connect(twin / "work-ops.db")
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        tables = [r[0] for r in left.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table in ("import_runs", "scan_runs"):
                continue
            rows = lambda conn: sorted(map(tuple, conn.execute(f'SELECT * FROM "{table}"')), key=repr)
            self.assertEqual(rows(left), rows(right), table)
        self.assertEqual(left.execute("SELECT observed_at, last_success FROM source_health WHERE scope='alert:test'").fetchone()[:], (at, at))

    def test_only_a_clean_empty_read_skips_the_replay(self):
        self.run_read(minutes=1, items=1, state="ok")
        for minutes, read in ((5, dict(items=1, state="ok")), (6, dict(state="partial")), (7, dict(code=1))):
            with self.subTest(**read):
                receipt = self.run_read(minutes=minutes, **read)
                self.assertNotIn("import_skipped", receipt)
                self.assertIn("import", receipt)
        self.assertNotIn("import_skipped", ingest.run(self.db, self.data, env={}, adapters=[], incremental=True))
        self.assertNotIn("import_skipped", self.run_read(minutes=9, incremental=False))

    def test_a_failed_import_is_retried_by_the_next_read_even_when_it_staged_nothing(self):
        import record_imports
        with patch.object(record_imports, "cmd_import_scans", side_effect=ValueError("fixture")):
            failed = self.run_read(minutes=1, items=1, state="ok")
        self.assertEqual((failed["status"], self.count("records WHERE type='job'")), ("failed", 0))
        retry = self.run_read(minutes=5)
        self.assertNotIn("import_skipped", retry)
        self.assertIn("import", retry["tasks"]["cleared"])
        self.assertEqual(self.count("records WHERE type='job'"), 1)
        self.assertEqual(self.run_read(minutes=9)["import_skipped"], "no new items")

    def test_evidence_staged_by_a_daily_run_that_did_not_finish_is_imported_by_the_next_read(self):
        self.run_read(minutes=1)
        with patch.object(ingest, "backup", side_effect=OSError("disk full")), self.assertRaises(OSError):
            ingest.run(self.db, self.data, env={"READ_AT": self.at(2)}, adapters=self.read(items=1, state="ok"))
        self.assertEqual(self.count("records WHERE type='job'"), 0)
        retry = self.run_read(minutes=5)
        self.assertNotIn("import_skipped", retry)
        self.assertEqual(self.count("records WHERE type='job'"), 1)

    def test_a_skipped_import_still_watches_the_other_jobs(self):
        import failure_tasks
        self.run_read(minutes=1)
        with patch.object(failure_tasks, "watch", wraps=failure_tasks.watch) as watch:
            skipped = self.run_read(minutes=5)
        self.assertEqual(skipped["import_skipped"], "no new items")
        watch.assert_called_once()
        self.assertIn("import: skipped (no new items)", ingest.format_receipt(skipped))


if __name__ == "__main__":
    unittest.main()
