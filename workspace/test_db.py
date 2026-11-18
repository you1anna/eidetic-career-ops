"""Database-backed workspace state; HTTP behavior lives in test_api.py."""
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db
import records
import server


class DatabaseModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "work-ops.db"
        self.conn = db.connect(self.path)
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_migration_009_retains_old_records_with_null_cv_and_enforces_values(self):
        old = Path(self.temp.name) / 'old'
        old.mkdir()
        for migration in db.MIGRATIONS.glob('*.sql'):
            if int(migration.name[:3]) < 9:
                (old / migration.name).write_text(migration.read_text())
        conn = db.connect(Path(self.temp.name) / 'v8.db')
        try:
            db.migrate(conn, old)
            conn.execute("INSERT INTO records(id,type,name,created_at,updated_at,status) VALUES('J1','job','Old job','x','x','applied')")
            self.assertEqual(db.migrate(conn), ['009_cv_override.sql', '010_research_receipts.sql',
                                                 '011_outreach_lifecycle.sql', '012_person_reach.sql', '013_submission_company.sql',
                                                 '014_draft_purpose.sql', '015_questions.sql', '016_research_budget.sql', '017_automation.sql', '018_incoming_roles.sql', '019_approach_next_steps.sql', '020_outcomes_at.sql'])
            row = conn.execute('SELECT * FROM records').fetchone()
            self.assertEqual((row['name'], row['status'], row['revision'], row['cv_override']), ('Old job', 'applied', 1, None))
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE records SET cv_override='HeadQE'")
            conn.execute("UPDATE records SET cv_override='Manager'")
            self.assertEqual(db.migrate(conn), [])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            conn.close()

    def test_draft_migration_labels_legacy_snapshot_without_claiming_old_approval(self):
        old = Path(self.temp.name) / 'old'
        old.mkdir()
        for path in db.MIGRATIONS.glob('*.sql'):
            if int(path.name[:3]) < 7:
                (old / path.name).write_text(path.read_text())
        conn = db.connect(Path(self.temp.name) / 'legacy.db')
        try:
            db.migrate(conn,old)
            conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES('P1','person','Example','x','x')")
            conn.execute("INSERT INTO drafts(record_id,title,text) VALUES('P1','Note','Only the currently saved text')")
            conn.execute("INSERT INTO decisions(subject,record_id,kind,question,status,answer,created_at,updated_at,revision) "
                         "VALUES('P1','P1','draft','Approve?','answered','approve','x','x',2)")
            db.migrate(conn)
            d = records.get_decision(conn,1)
            self.assertEqual((d['draft_binding'],d['draft']['legacy_snapshot']),('legacy',1))
            self.assertEqual(d['draft']['text'],'Only the currently saved text')
            with self.assertRaises(records.Conflict):
                records.process_decision(conn,1,'done','Pretend earlier approval was exact',revision=d['revision'])
            approved=records.answer_decision(conn,1,dict(choice='approve',revision=d['revision']))['decision']
            self.assertEqual(approved['draft_binding'],'exact')
            records.process_decision(conn,1,'done','Approved this saved snapshot',revision=approved['revision'])
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE draft_versions SET text='Tampered'")
            self.assertEqual(db.migrate(conn),[])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:
            conn.close()

    def test_state_comes_only_from_the_database_and_exposes_current_contract(self):
        person = records.create(self.conn, {
            "type": "person", "name": "Test person", "relationship": "none",
            "url": "https://example.test/person",
        })["record"]
        state = server.load_state_db(self.conn)
        self.assertEqual(state["mode"], "database")
        self.assertEqual(state["records"][0]["id"], person["id"])
        self.assertEqual(state["records"][0]["revision"], 1)
        self.assertEqual(state["records"][0]["url"], "https://example.test/person")
        self.assertEqual(state["links"], [])
        self.assertEqual(state["interactions"], [])
        self.assertEqual(state["review_queue"], [])
        self.assertIn("vocabularies", state)
        self.assertIn("working_states", state)

    def test_followup_uses_revision_and_never_changes_record_stage(self):
        opportunity = records.create(self.conn, {
            "type": "opportunity", "name": "Example", "stage": "lead",
        })["record"]
        saved = server.save_followup_db(self.conn, {
            "id": opportunity["id"], "revision": opportunity["revision"],
            "action": "Review evidence", "status": "done", "owner": "owner",
        })["record"]
        self.assertEqual((saved["working_state"], saved["stage"]), ("done", "lead"))
        self.assertEqual(saved["revision"], 2)
        with self.assertRaises(records.Conflict):
            server.save_followup_db(self.conn, {
                "id": opportunity["id"], "revision": 1, "action": "Overwrite",
                "status": "ready", "owner": "owner",
            })

    def test_migration_005_keeps_one_row_per_scanner_content_and_every_other_observation(self):
        import json, shutil
        old = Path(self.temp.name) / "old"
        old.mkdir()
        for f in sorted(db.MIGRATIONS.glob("00[1-4]_*.sql")):
            shutil.copy(f, old / f.name)
        conn = db.connect(Path(self.temp.name) / "v4.db")
        db.migrate(conn, old)
        conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES('J1','job','QA Lead','x','x')")
        seen = lambda when, title="QA Lead": json.dumps(dict(kind="job", source_scope="board:ashby:x", last_seen_at=when,
                                                            last_complete_seen_at=when, current=dict(title=title)), sort_keys=True)
        rows = [("board:ashby:x", "2026-09-25", seen("2026-09-25")), ("board:ashby:x", "2026-09-27", seen("2026-09-27")),
                ("board:ashby:x", "2026-09-26", seen("2026-09-26")), ("board:ashby:x", "2026-09-26", seen("2026-09-26", "QA Lead II")),
                ("research:a.json", "2026-09-24", '{"summary": "one"}'), ("research:b.json", "2026-09-24", '{"summary": "one"}')]
        for source, at, payload in rows:
            conn.execute("INSERT INTO observations(record_id,source,observed_at,payload_json) VALUES('J1',?,?,?)", (source, at, payload))
        db.migrate(conn)
        kept = conn.execute("SELECT source,observed_at,payload_sha FROM observations ORDER BY source,observed_at").fetchall()
        self.assertEqual([(r[0], r[1]) for r in kept], [("board:ashby:x", "2026-09-26"), ("board:ashby:x", "2026-09-27"),
                                                        ("research:a.json", "2026-09-24"), ("research:b.json", "2026-09-24")])
        self.assertTrue(all(len(r[2]) == 64 for r in kept))
        conn.close()

    def test_retired_columns_are_gone_and_the_csv_still_derives_related_records(self):
        cols = lambda t: {r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")}
        self.assertFalse({"related_ids", "related_names"} & cols("records"))
        self.assertNotIn("due", cols("followups"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_followup_dates'").fetchone())
        a = records.create(self.conn, {"type": "person", "name": "Ann"})["record"]
        b = records.create(self.conn, {"type": "agency", "name": "Desk"})["record"]
        records.link(self.conn, {"from_id": a["id"], "to_id": b["id"], "relation": "consultant_at", "revision": a["revision"]})
        rows = {r["id"]: r for r in records.csv_rows(self.conn)}
        self.assertEqual((rows[a["id"]]["related_ids"], rows[a["id"]]["related_names"]), (b["id"], "Desk"))

    def test_backup_pruning_keeps_recent_and_counted_copies(self):
        import os, time
        folder = Path(self.temp.name) / "backups"
        folder.mkdir()
        now = time.time()
        def make(name, days_old):
            path = folder / name
            path.write_bytes(b"x")
            os.utime(path, (now - days_old * 86400, now - days_old * 86400))
            return path
        ingest = [make(f"work-ops-pre-ingest-202609{d:02d}-063000.db", 30 - d) for d in range(1, 11)]
        phone = [make(f"work-ops-pre-phone-202609{d:02d}-090000.db", 30 - d) for d in range(20, 26)]
        old_manual = make("work-ops-pre-quality-20260801-2253.db", 57)
        (folder / "work-ops-pre-quality-20260801-2253.db-wal").write_bytes(b"w")
        recent_manual = make("local-final-2026-09-22.db", 5)
        other = make("notes.txt", 90)
        plan = db.prune_backups(folder, now=now, apply=False)
        self.assertTrue(all(p.exists() for p in plan))  # a dry run deletes nothing
        removed = set(db.prune_backups(folder, now=now))
        self.assertEqual(removed, set(ingest[:3] + phone[:3] + [old_manual, folder / "work-ops-pre-quality-20260801-2253.db-wal"]))
        self.assertTrue(all(p.exists() for p in ingest[3:] + phone[3:] + [recent_manual, other]))
        self.assertEqual(db.prune_backups(folder, now=now), [])

    def test_source_health_reads_as_sentences_and_a_capped_read_is_not_a_failure(self):
        rows = [("feed:adzuna:Adzuna UK — remote", "adzuna", "partial", 104, '{"name": "Adzuna UK \\u2014 remote"}'),
                ("board:ashby:x", "ashby", "error", 0, '{"name": "X", "slug": "x", "error": "HTTP 404"}'),
                ("tender:cf", "cf", "ok", 12, '{"name": "Contracts Finder"}')]
        for scope, provider, state, count, detail in rows:
            self.conn.execute("INSERT INTO source_health(scope,provider,state,observed_at,item_count,detail) VALUES(?,?,?,?,?,?)",
                              (scope, provider, state, "2026-09-24T11:22:49Z", count, detail))
        by = {s["scope"]: s for s in records.state(self.conn)["coverage"]["sources"]}
        feed = by["feed:adzuna:Adzuna UK — remote"]
        self.assertEqual((feed["label"], feed["problem"]), ("Adzuna UK — remote", False))
        self.assertIn("104", feed["summary"]); self.assertIn("page limit", feed["summary"])
        self.assertTrue(by["board:ashby:x"]["problem"]); self.assertIn("HTTP 404", by["board:ashby:x"]["summary"])
        self.assertFalse(by["tender:cf"]["problem"])
        self.assertFalse(any("{" in s["summary"] for s in by.values()))

    def test_a_classified_fetch_failure_says_what_follows(self):
        rows = [("news:tfn", "tfn", "error", '{"error": "HTTP Error 403: Forbidden", "kind": "challenged", "status": 403, "attempts": 1}'),
                ("tender:cf", "cf", "error", '{"name": "Contracts Finder", "error": "timed out", "kind": "timeout", "attempts": 3}'),
                ("board:lever:x", "lever", "bad-slug", '{"name": "X", "slug": "x", "kind": "not-found", "status": 404}')]
        for scope, provider, state, detail in rows:
            self.conn.execute("INSERT INTO source_health(scope,provider,state,observed_at,item_count,detail) VALUES(?,?,?,?,?,?)",
                              (scope, provider, state, "2026-09-28T05:30:00Z", 0, detail))
        by = {s["scope"]: s for s in records.state(self.conn)["coverage"]["sources"]}
        self.assertIn("bot challenge (HTTP 403)", by["news:tfn"]["summary"])
        self.assertIn("another channel", by["news:tfn"]["summary"])
        self.assertIn("timed out after 3 attempts", by["tender:cf"]["summary"])
        self.assertIn("not found (HTTP 404)", by["board:lever:x"]["summary"])
        self.assertTrue(all(s["problem"] for s in by.values()))

    def test_backup_is_a_consistent_independent_database(self):
        records.create(self.conn, {"type": "company", "name": "Example"})
        destination = Path(self.temp.name) / "backup.db"
        db.backup(self.conn, destination)
        copy = db.connect(destination)
        try:
            self.assertEqual(copy.execute("SELECT name FROM records").fetchone()[0], "Example")
            self.assertEqual(db.version(copy), db.version(self.conn))
        finally:
            copy.close()


if __name__ == "__main__":
    unittest.main()
