"""The store's exports, backups and state rules, over a small fixture built the way the importers write."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "workspace"))

import db as wodb  # noqa: E402
import server  # noqa: E402
import states  # noqa: E402
import wo  # noqa: E402

SCAN_COLS = ["date_seen", "company", "title", "location", "url", "posted", "engagement", "rate", "why"]
FIXTURE = [  # id, type, name, organisation, status_raw, status, stage, sub_state, kind
    ("CP-001", "person", "Test person", "Example Ltd", "", None, None, None, None),
    ("AG-003", "agency", "Example Agency", "", "not contacted", "not_contacted", None, None, "agency"),
    ("CO-example", "company", "Example Ltd", "", "", None, None, None, "company"),
    ("CA-20260913-001", "opportunity", "Example platform work", "Example Ltd", "lead — first call booked", None, "lead",
     "first call booked", None),
]


def seed(conn: sqlite3.Connection) -> None:
    """Rows as the importers write them: plain inserts, so no state events exist yet."""
    for rid, typ, name, org, raw, status, stage, sub, kind in FIXTURE:
        conn.execute("INSERT INTO records(id,type,name,organisation,status_raw,status,stage,sub_state,kind,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid, typ, name, org, raw, status, stage, sub, kind,
                                                       "2026-09-21T10:00:00+01:00", "2026-09-21T10:00:00+01:00"))
    conn.execute("INSERT INTO links(from_id,to_id,relation) VALUES('CP-001','AG-003','consultant_at')")
    conn.execute("INSERT INTO review_queue(kind,payload_json,source,created_at) VALUES('person_candidate',?,'fixture',?)",
                 (json.dumps({"name": "Candidate", "agency_id": "AG-003"}), "2026-09-21T10:00:00+01:00"))
    conn.commit()


def write_scan(folder: Path, day: str, rows: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(SCAN_COLS)] + ["\t".join(r.get(c, "") for c in SCAN_COLS) for r in rows]
    (folder / f"{day}.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


SCANS = [dict(date_seen="2026-09-14", company="Example Ltd", title="Head of Quality Engineering", url="https://jobs.example/1",
              posted="2026-09-12", engagement="contract", rate="£700/day", why="title: head of quality"),
         dict(date_seen="2026-09-14", company="Other Co", title="QA Lead", url="https://jobs.example/2", posted="Unknown")]


def build(path: Path, pending: Path) -> sqlite3.Connection:
    conn = wodb.connect(path)
    wodb.migrate(conn)
    seed(conn)
    wo.cmd_import_scans(conn, write_scan(pending, "2026-09-14", SCANS))
    return conn


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.pending = cls.dir / "pending"
        cls.conn = build(cls.dir / "wo.db", cls.pending)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def export(self, name: str) -> Path:
        out = self.dir / name
        wo.store.write_csv(self.conn, out)
        return out

    def test_reexport_is_deterministic(self):
        self.assertEqual(self.export("b.csv").read_bytes(), self.export("c.csv").read_bytes())

    def test_export_uses_declared_state_and_retains_source_status(self):
        row = self.conn.execute("SELECT status_raw,status FROM records WHERE id='AG-003'").fetchone()
        self.assertEqual(tuple(row), ('not contacted', 'not_contacted'))
        exported = {r['id']: r for r in wo.csv_rows(self.conn)}
        self.assertEqual(exported['AG-003']['status'], 'not_contacted')
        self.assertNotIn('action_due', exported['AG-003'])

    def test_scan_import_creates_candidates_and_a_reimport_changes_nothing(self):
        jobs = {r["name"]: r for r in self.conn.execute("SELECT * FROM records WHERE type='job'")}
        self.assertEqual(set(jobs), {"Head of Quality Engineering", "QA Lead"})
        self.assertEqual(jobs["Head of Quality Engineering"]["status"], "candidate")
        self.assertEqual(jobs["Head of Quality Engineering"]["triage_reason"], "title: head of quality")
        self.assertEqual(jobs["QA Lead"]["date"], "")  # 'Unknown' is not a date
        before = self.export('before.csv').read_bytes()
        report = wo.cmd_import_scans(self.conn, self.pending)
        self.assertEqual(report['new'], 0)
        self.assertEqual(self.export('after.csv').read_bytes(), before)

    def test_backup_restore_exports_the_same_file(self):
        first = self.export("e.csv").read_bytes()
        wodb.backup(self.conn, self.dir / "backup.db")
        wodb.restore(self.dir / "backup.db", self.dir / "restored.db")
        restored = wodb.connect(self.dir / "restored.db")
        try:
            out = self.dir / "f.csv"
            wo.store.write_csv(restored, out)
            self.assertEqual(out.read_bytes(), first)
        finally:
            restored.close()

    def test_json_round_trip_keeps_counts_and_ids(self):
        payload = wo.dump(self.conn)
        before = [r["id"] for r in self.conn.execute("SELECT id FROM records ORDER BY id")]
        target = wodb.connect(self.dir / "json.db")
        try:
            wodb.migrate(target)
            counts = wo.load_dump(target, payload)
            self.assertEqual(counts["records"], len(before))
            self.assertEqual([r["id"] for r in target.execute("SELECT id FROM records ORDER BY id")], before)
            self.assertEqual(counts["links"], len(payload["tables"]["links"]))
        finally:
            target.close()

    def test_backup_is_allowed_before_a_pending_migration(self):
        path = self.dir / "old.db"
        old = wodb.connect(path)
        old.executescript((wodb.MIGRATIONS / "001_init.sql").read_text())
        old.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
        old.execute("INSERT INTO schema_version VALUES(1,'001_init','2026-09-01')")
        old.commit(); old.close()
        self.assertEqual(wo.main(["--db", str(path), "backup", str(self.dir / "copy.db")]), 0)
        self.assertEqual(wo.main(["--db", str(path), "state"]), 2)

    def test_migrate_on_a_migrated_database_is_a_no_op(self):
        self.assertEqual(wodb.migrate(self.conn), [])
        self.assertEqual(wodb.version(self.conn), len(list(wodb.MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pending = self.dir / "pending"
        self.conn = build(self.dir / "wo.db", self.pending)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def stage(self, record_id: str) -> str:
        return self.conn.execute("SELECT stage FROM records WHERE id = ?", (record_id,)).fetchone()[0]

    def test_a_disallowed_transition_raises_and_writes_nothing(self):
        before = self.stage("CA-20260913-001")
        self.assertEqual(before, "lead")
        with self.assertRaises(states.StateError):
            states.transition(self.conn, "CA-20260913-001", "stage", "won", "test")
        with self.assertRaises(states.StateError):
            states.transition(self.conn, "CA-20260913-001", "stage", "prospect", "test")
        self.assertEqual(self.stage("CA-20260913-001"), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM state_events WHERE entity_id='CA-20260913-001'").fetchone()[0], 0)

    def test_an_allowed_transition_records_what_moved(self):
        states.transition(self.conn, "CA-20260913-001", "stage", "contacted", "owner", "sent")
        event = self.conn.execute("SELECT * FROM state_events WHERE entity_id='CA-20260913-001'").fetchone()
        self.assertEqual((event["field"], event["from_value"], event["to_value"], event["actor"]),
                         ("stage", "lead", "contacted", "owner"))
        self.assertEqual(self.stage("CA-20260913-001"), "contacted")

    def test_a_follow_up_reaching_done_never_moves_a_stage(self):
        before = self.stage("CA-20260913-001")
        result = server.save_followup_db(self.conn, dict(id="CA-20260913-001", action="Chase", owner="owner", revision=1, status="done", notes=""))
        self.assertTrue(result["saved"])
        self.assertEqual(result["destination"], "work-ops database")
        self.assertEqual(self.stage("CA-20260913-001"), before)
        row = self.conn.execute("SELECT * FROM followups WHERE record_id = 'CA-20260913-001'").fetchone()
        self.assertEqual((row["status"], row["owner"], row["status_raw"], row["source"]),
                         ("done", "owner", "done", "local"))
        fields = [r["field"] for r in self.conn.execute("SELECT field FROM state_events WHERE entity_id='CA-20260913-001'")]
        self.assertEqual(fields, ["working_state"])

    def test_a_rescan_does_not_untriage_a_job(self):
        scan_id = self.conn.execute("SELECT id FROM records WHERE type = 'job' AND id LIKE 'SCAN-%'").fetchone()[0]
        states.transition(self.conn, scan_id, "status", "pursue", "owner", "worth a look")
        wo.cmd_import_scans(self.conn, self.pending)
        self.assertEqual(self.conn.execute("SELECT status FROM records WHERE id = ?", (scan_id,)).fetchone()[0],
                         "pursue")

    def test_an_interaction_is_append_only_and_validated(self):
        wo.log_interaction(self.conn, "CP-001", "call", "out", "2026-09-18", "Rehearsal")
        with self.assertRaises(states.StateError):
            wo.log_interaction(self.conn, "CP-001", "carrier-pigeon", "out", "2026-09-18", "No")
        rows = self.conn.execute("SELECT * FROM interactions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["person_id"], rows[0]["record_id"], rows[0]["source"]), ("CP-001", "CP-001", "manual"))

    def test_brief_counts_records_and_the_review_queue(self):
        text = wo.cmd_brief(self.conn, markdown=False)
        self.assertIn("person: 1", text)
        self.assertIn("Import review: 1", text)
        self.assertTrue(wo.cmd_brief(self.conn, markdown=True).startswith("# Career brief"))

    def test_brief_says_when_sources_were_last_read_and_which(self):
        self.assertIn("no daily run recorded", wo.cmd_brief(self.conn, markdown=False))
        self.conn.execute("INSERT INTO scan_runs(run_id, date, receipt_json, status) VALUES (?,?,?,?)",
                          ("ingest:2026-09-25T18:10:00Z", "2026-09-25", '{"keyless": true}', "ok"))
        self.assertIn("sources last read 2026-09-25 (free sources only)", wo.cmd_brief(self.conn, markdown=False))
        self.conn.execute("INSERT INTO scan_runs(run_id, date, receipt_json, status) VALUES (?,?,?,?)",
                          ("ingest:2026-10-02T05:30:00Z", "2026-10-02", '{"keyless": true, "keyed": ["companies-house", "feeds"]}', "ok"))
        self.assertIn("sources last read 2026-10-02 (free sources plus companies-house, feeds)", wo.cmd_brief(self.conn, markdown=False))


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.conn=wodb.connect(self.path/'wo.db');wodb.migrate(self.conn)
    def tearDown(self):
        self.conn.close();self.tmp.cleanup()
    def write_observation(self, title='First title', lifecycle='active', when='2026-09-20T12:00:00Z'):
        data=dict(schema_version=1,generated_at=when,sources={},items={'key':dict(source_scope='tender:contracts_finder',source_id='notice-1',kind='tender',last_seen_at=when,source_lifecycle=lifecycle,current=dict(title=title,buyer='Buyer organisation',url='https://example.org/notice-1',eligible=True,deadline='2026-10-01'))})
        (self.path/'observations.json').write_text(json.dumps(data))
        with wo.store.transaction(self.conn): return wo.import_observations(self.conn,self.path)
    def test_tenders_are_visible_linked_and_refresh_without_changing_decisions(self):
        report=self.write_observation();self.assertEqual(report['new_observed'],1)
        r=next(r for r in wo.store.state(self.conn)['records'] if r['type']=='tender')
        wo.store.edit(self.conn,r['id'],dict(revision=r['revision'],name='Human title'))
        r=wo.store.get(self.conn,r['id'])
        wo.store.followup(self.conn,dict(id=r['id'],revision=r['revision'],action='Read specification',status='paused'))
        report=self.write_observation('Changed advert','closed','2026-09-21T12:00:00Z')
        r=wo.store.get(self.conn,r['id'])
        self.assertEqual((r['name'],r['working_state'],r['source_lifecycle']),('Human title','paused','closed'))
        self.assertEqual(report['conflicts'][0]['record_id'],r['id'])
        self.assertEqual(len(wo.store.state(self.conn)['links']),1)
        self.assertEqual(r['published_deadline'],'2026-10-01')
        self.write_observation('Reopened','active','2026-09-22T12:00:00Z')
        self.assertEqual(wo.store.get(self.conn,r['id'])['source_lifecycle'],'active')
    def rows(self, source='tender:contracts_finder'):
        return self.conn.execute("SELECT observed_at,payload_json,payload_sha FROM observations WHERE source=? ORDER BY id",(source,)).fetchall()
    def test_an_unchanged_sighting_updates_one_row_and_a_change_adds_one(self):
        self.write_observation(when='2026-09-20T12:00:00Z')
        self.write_observation(when='2026-09-21T12:00:00Z')
        rows=self.rows()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['observed_at'],'2026-09-21T12:00:00Z')
        self.assertEqual(json.loads(rows[0]['payload_json'])['last_seen_at'],'2026-09-21T12:00:00Z')
        self.write_observation('Changed title',when='2026-09-22T12:00:00Z')
        self.assertEqual(len(self.rows()),2)
        self.write_observation(when='2026-09-20T06:00:00Z')  # an older replay never moves the clock back
        self.assertEqual(self.rows()[0]['observed_at'],'2026-09-21T12:00:00Z')
    def test_a_closed_advert_stays_closed_when_the_closure_is_seen_again(self):
        self.write_observation(when='2026-09-20T12:00:00Z')
        self.write_observation(lifecycle='closed',when='2026-09-21T12:00:00Z')
        self.write_observation(lifecycle='closed',when='2026-09-22T12:00:00Z')
        rid=self.conn.execute("SELECT record_id FROM source_records WHERE source_id='notice-1'").fetchone()[0]
        self.assertEqual(wo.store.get(self.conn,rid)['source_lifecycle'],'closed')
        self.assertEqual(len(self.rows()),2)
    def test_a_research_rerun_still_adds_nothing(self):
        r=wo.store.create(self.conn,dict(type='company',name='Acme'))['record']
        batch=self.path/'batch.json'
        batch.write_text(json.dumps([dict(id=r['id'],name='Acme',reviewed_at='2026-09-27',summary='Makes things.',source_urls=['https://acme.example/'])]))
        self.assertEqual(wo.import_research(self.conn,batch)['accepted'],1)
        count=lambda: self.conn.execute("SELECT count(*) FROM observations WHERE source LIKE 'research:%'").fetchone()[0]
        self.assertEqual(count(),1)
        wo.import_research(self.conn,batch)
        self.assertEqual(count(),1)
    def test_a_new_source_identity_for_a_merged_advert_updates_the_kept_record(self):
        dup=wo.store.create(self.conn,dict(type='tender',name='Copy',url='https://example.org/notice-1'))['record']
        keep=wo.store.create(self.conn,dict(type='tender',name='Kept notice',url='https://example.org/notice-1'))['record']
        wo.store.merge(self.conn,dup['id'],keep['id'],dict(revision=dup['revision'],reason='Same URL'),actor='agent')
        self.write_observation('Refreshed title')
        mapped=self.conn.execute("SELECT record_id FROM source_records WHERE source_id='notice-1'").fetchone()[0]
        self.assertEqual(mapped,keep['id'])
        self.assertEqual(wo.store.get(self.conn,dup['id'])['name'],'Copy')
    def test_invalid_json_restore_is_atomic_and_missing_backup_does_not_erase(self):
        r=wo.store.create(self.conn,dict(type='person',name='Retain me'))['record']
        payload=wo.dump(self.conn);payload['tables']['links']=[dict(from_id='missing',to_id=r['id'],relation='related',notes='')]
        with self.assertRaises(ValueError): wo.load_dump(self.conn,payload)
        self.assertEqual(wo.store.get(self.conn,r['id'])['name'],'Retain me')
        payload=wo.dump(self.conn);payload['tables']['records; DROP TABLE records']=[]
        with self.assertRaises(ValueError): wo.load_dump(self.conn,payload)
        with self.assertRaises(ValueError): wodb.restore(self.path/'missing.db',self.path/'wo.db')
        self.assertEqual(wo.store.get(self.conn,r['id'])['name'],'Retain me')

if __name__ == "__main__":
    unittest.main()


class DecisionCommandTests(unittest.TestCase):
    """The agent's side of a decision: ask, read the owner's answers, process them."""

    def setUp(self):
        import records
        self.records = records
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        conn = wodb.connect(self.db)
        wodb.migrate(conn)
        self.agency = records.create(conn, dict(type="agency", name="YunoJuno"))["record"]
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def wo(self, *args):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wo.main(["--db", str(self.db), "--actor", "agent", *args])
        return code, out.getvalue() + err.getvalue()

    def answer(self, did, choice, note=""):
        conn = wodb.connect(self.db)
        d = self.records.get_decision(conn, did)
        self.records.answer_decision(conn, did, dict(choice=choice, note=note, revision=d["revision"]))
        conn.close()

    def test_card_questions_are_listed_and_answered_from_a_file_or_stdin(self):
        conn = wodb.connect(self.db)
        qid = self.records.ask_question(conn, self.agency["id"], dict(question="Is this desk worth a call?"))["question"]["id"]
        conn.close()
        code, out = self.wo("question", "list")
        self.assertEqual(code, 0)
        self.assertIn(f"question {qid} · YunoJuno ({self.agency['id']})", out)
        self.assertIn("Is this desk worth a call?", out)
        answer = Path(self.tmp.name) / "answer.md"
        answer.write_text("Yes: two live QA briefs this week.\n", encoding="utf-8")
        self.assertEqual(self.wo("question", "answer", str(qid), "--text-file", str(answer)), (0, f"question {qid} answered on YunoJuno\n"))
        self.assertEqual(self.wo("question", "list"), (0, "No unanswered questions.\n"))
        code, out = self.wo("question", "list", "--all")
        self.assertIn("answered", out)
        self.assertIn("two live QA briefs", out)
        code, out = self.wo("question", "answer", str(qid), "--text-file", str(answer))
        self.assertEqual(code, 2)
        self.assertIn("already answered", out)

    def test_asking_twice_says_the_question_is_already_live(self):
        args = ("decision", "add", self.agency["id"], "--kind", "join", "--question", "Join YunoJuno?", "--recommend", "help")
        self.assertEqual(self.wo(*args), (0, "asked #1: Join YunoJuno?\n"))
        self.assertEqual(self.wo(*args), (0, "already live #1: Join YunoJuno?\n"))
        code, out = self.wo("decision", "add", self.agency["id"], "--kind", "join", "--question", "x", "--recommend", "approve")
        self.assertEqual(code, 2)
        self.assertIn("Recommend one of the join choices", out)

    def test_the_inbox_shows_owners_answer_note_and_what_happens_next(self):
        self.wo("decision", "add", self.agency["id"], "--kind", "join", "--question", "Join YunoJuno?")
        self.assertEqual(self.wo("decision", "list"), (0, "No answered decisions.\n"))
        self.answer(1, "help", "Profile first")
        code, out = self.wo("decision", "list")
        self.assertEqual(code, 0)
        self.assertIn("The owner: Help me register — Profile first", out)
        self.assertIn("Next: Claude prepares the profile text", out)

    def test_processing_needs_an_answer_and_closes_the_records_waiting_step(self):
        self.wo("decision", "add", self.agency["id"], "--kind", "join", "--question", "Join YunoJuno?")
        code, out = self.wo("decision", "done", "1", "Nothing to do yet")
        self.assertEqual(code, 2)
        self.assertIn("Cannot apply 'done' to a decision in 'open'", out)
        self.assertIn("answer", out)
        self.answer(1, "no")
        self.assertEqual(self.wo("decision", "done", "1", "Closed and ranked down.", "--revision", "2"), (0, "#1 done\n"))
        conn = wodb.connect(self.db)
        r = self.records.get(conn, self.agency["id"])
        conn.close()
        self.assertEqual((r["owner"], r["working_state"]), ("agent", "done"))
        self.assertIn("Outcome: Closed and ranked down.", self.wo("decision", "list", "--status", "all")[1])

    def test_claim_and_block_require_the_read_revision_and_preserve_answer(self):
        self.wo('decision','add',self.agency['id'],'--kind','join','--question','Join?')
        self.answer(1,'help')
        self.assertIn('Revision: 2',self.wo('decision','show','1')[1])
        self.assertEqual(self.wo('decision','claim','1','Preparing profile','--revision','1')[0],2)
        self.assertEqual(self.wo('decision','claim','1','Preparing profile','--revision','2'),(0,'#1 working\n'))
        self.assertEqual(self.wo('decision','block','1','Needs access','--revision','3'),(0,'#1 blocked\n'))
        self.assertEqual(self.wo('decision','reply','1','Please provide access','--revision','4'),(0,'#1 open\n'))

    def test_outcome_cli_uses_the_shared_service_and_actor(self):
        code,out=self.wo('outcome',self.agency['id'],'conversation','--note','Discussed relevant work','--revision','1')
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out)['outcome']['actor'],'agent')
        self.assertEqual(self.wo('outcome',self.agency['id'],'sent','--revision','1')[0],2)

    def test_correct_cli_undoes_a_mistaken_send_as_the_agent(self):
        code,out=self.wo('outcome',self.agency['id'],'sent','--revision','1')
        self.assertEqual(code,0)
        sent=json.loads(out)
        code,out=self.wo('correct',self.agency['id'],'--outcome',str(sent['outcome']['id']),
                         '--revision',str(sent['record']['revision']),'--note','The owner: tapped by mistake')
        self.assertEqual(code,0)
        conn=wodb.connect(self.db)
        try:
            row=conn.execute('SELECT corrected_by,correction_note FROM outcomes WHERE id=?',(sent['outcome']['id'],)).fetchone()
            self.assertEqual(tuple(row),('agent','The owner: tapped by mistake'))
        finally:
            conn.close()
        self.assertEqual(self.wo('correct',self.agency['id'],'--outcome',str(sent['outcome']['id']),'--revision','1')[0],2)

    def test_full_and_partial_exports_keep_draft_versions_and_outcome_links(self):
        conn=wodb.connect(self.db)
        try:
            saved=self.records.draft(conn,dict(record_id=self.agency['id'],revision=1,text='Exact draft'),actor='agent')
            self.records.outcome(conn,self.agency['id'],dict(kind='sent',revision=saved['record']['revision'],draft_version_id=saved['version_id']))
            for partial in (False,True):
                target=wodb.connect(Path(self.tmp.name)/f'roundtrip-{partial}.db')
                try:
                    wodb.migrate(target)
                    payload=wo.committed_dump(conn) if partial else wo.dump(conn)
                    wo.load_dump(target,payload,partial=partial)
                    state=self.records.state(target)
                    self.assertEqual(state['drafts'][0]['version_id'],state['outcomes'][0]['draft_version_id'])
                    self.assertEqual(target.execute('SELECT text FROM draft_versions').fetchone()[0],'Exact draft')
                    self.assertEqual(target.execute('PRAGMA foreign_key_check').fetchall(),[])
                finally:
                    target.close()
        finally:
            conn.close()

    def test_committed_export_of_v6_database_keeps_its_original_schema(self):
        old=Path(self.tmp.name)/'v6-migrations';old.mkdir()
        for migration in wodb.MIGRATIONS.glob('*.sql'):
            if int(migration.name[:3])<7:
                (old/migration.name).write_text(migration.read_text())
        conn=wodb.connect(Path(self.tmp.name)/'v6.db')
        try:
            wodb.migrate(conn,old)
            payload=wo.committed_dump(conn)
            self.assertEqual(payload['schema_version'],6)
            self.assertNotIn('draft_versions',payload['tables'])
            self.assertNotIn('outcomes',payload['tables'])
            self.assertEqual(wodb.version(conn),6)
            self.assertEqual([n for n,_ in wodb.pending(conn)],[7,8,9,10,11,12,13,14,15,16,17,18,19,20])
        finally:
            conn.close()

    def test_draft_creation_and_clearing_are_retired_without_consuming_stdin(self):
        import io
        from unittest.mock import patch
        for args in (('--title','Old','--text-file','-'),('--clear','--reason','Old')):
            stream=io.StringIO('Historical content')
            with patch('sys.stdin',stream):code,out=self.wo('draft',self.agency['id'],*args)
            self.assertEqual(code,2)
            self.assertIn('retired',out)
            self.assertEqual(stream.tell(),0)
        self.assertNotIn('draft',json.loads(self.wo('show',self.agency['id'])[1]))

    def test_one_decision_is_shown_with_its_context_and_recommendation(self):
        self.wo("decision", "add", self.agency["id"], "--kind", "join", "--question", "Join YunoJuno?",
                "--context", "Vetted network; a profile is needed.", "--recommend", "help", "--reason", "Leadership demand")
        code, out = self.wo("decision", "show", "1")
        self.assertEqual(code, 0)
        for text in ("#1 [open]", "Q: Join YunoJuno?", "Context: Vetted network; a profile is needed.",
                     "Recommended: Help me register", "Why: Leadership demand"):
            self.assertIn(text, out)
        code, out = self.wo("decision", "show", "9")
        self.assertEqual(code, 2)
        self.assertIn("Unknown decision", out)

    def test_a_record_is_created_from_json_on_stdin_and_found_by_name(self):
        import io
        from unittest.mock import patch
        with patch("sys.stdin", io.StringIO('{"type": "person", "name": "Alex O\'Example", "role": "CTO", "organisation": "YunoJuno"}')):
            code, out = self.wo("create", "--json", "-")
        self.assertEqual(code, 0)
        rid = json.loads(out)["record"]["id"]
        code, out = self.wo("find", "o'example")
        self.assertEqual((code, out.split()[0]), (0, rid))
        self.assertIn("Alex O'Example · CTO · YunoJuno", out)
        code, out = self.wo("find", "YUNOJUNO", "--type", "agency")
        self.assertEqual(out.splitlines(), [f"{self.agency['id']}  agency  YunoJuno"])
        self.assertEqual(self.wo("find", "nobody"), (0, "No records match 'nobody'.\n"))

    def test_an_empty_draft_or_no_text_is_refused(self):
        import io
        from unittest.mock import patch
        with patch("sys.stdin", io.StringIO("  \n")):
            code, out = self.wo("draft", self.agency["id"], "--text-file", "-")
        self.assertEqual(code, 2)
        self.assertIn("retired", out)
        code, out = self.wo("draft", self.agency["id"])
        self.assertEqual(code, 2)
        self.assertIn("retired", out)


class ApproachQueueTests(unittest.TestCase):
    def test_the_queue_shows_best_opportunities_as_the_page_does_and_says_why_others_wait_in_jobs(self):
        state=dict(records=[dict(id='C',name='Contract'),dict(id='L',name='Lead'),
                            dict(id='P',name='Permanent',availability=dict(status='unconfirmed'))],
                   opportunities=dict(active=[],shortlisted=[],discovery=[
                       dict(record_id='C',lane='contract',reason='Contract remit',worth_a_look=True),
                       dict(record_id='P',lane='permanent',reason='Suitable role',worth_a_look=False),
                       dict(record_id='L',lane='direct',reason='Sourced remit',worth_a_look=True)]))
        self.assertEqual(wo.approach_queue(state).splitlines(),[
            'Opportunities: 3 active, shortlisted or recommended work records',
            'Best opportunities: 2',
            '  C  Contract  [contract]  Contract remit',
            '  L  Lead  [direct]  Sourced remit',
            'Jobs → Discovery only, advert not open now: 1',
            '  P  Permanent  [permanent · availability unconfirmed]  Suitable role'])
        self.assertEqual(wo.approach_queue(state,1).splitlines()[1:4],
                         ['Best opportunities: 2','  C  Contract  [contract]  Contract remit','  … 1 more (--limit)'])

    def test_the_queue_follows_the_page_order_and_traces_rules_checking_and_research_targets(self):
        def ref(rid,attention,rule='shortlisted.job'):
            return dict(record_id=rid,lane='unstated',rule=rule,attention=attention,reason=f'{rid} step.')
        act,wait,work=ref('S','shortlisted'),ref('W','their_step','active.process'),ref('I','incoming','active.process')
        state=dict(records=[dict(id=x,name=x) for x in 'SWIL'],opportunities=dict(
            active=[wait,work],shortlisted=[act],discovery=[],act_now=[act],waiting=[wait],in_progress=[work],
            needs_checking=[dict(record_id='L',lane='contract',rule='qualify.advert',uncertainty='Remit unclear',
                                 research_status=dict(status='unverified',attempted_at='2026-10-29T09:01:00Z'))],
            research_ids=['S','W','I','L']))
        self.assertEqual(wo.approach_queue(state).splitlines(),[
            'Opportunities: 3 active, shortlisted or recommended work records',
            'Act now: 1',
            '  S  S  [unstated · shortlisted.job · shortlisted]  S step.',
            'Needs checking: 1',
            '  L  L  [contract · qualify.advert]  Remit unclear · research: unverified 2026-10-29T09:01:00Z',
            'In progress: 1',
            '  I  I  [unstated · active.process · incoming]  I step.',
            'Waiting: 1',
            '  W  W  [unstated · active.process · their_step]  W step.',
            'Research targets: 4  S W I L'])
        # The session hook reads only this first line.
        self.assertEqual(wo.approach_queue(state,0),'Opportunities: 3 active, shortlisted or recommended work records')
