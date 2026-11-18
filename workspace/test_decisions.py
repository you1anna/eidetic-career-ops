"""Decisions the owner answers on the site: fixed choices, the owner-only answers, agent processing."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import records  # noqa: E402
import states  # noqa: E402


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.migrate(self.conn)
        self.agency = records.create(self.conn, dict(type="agency", name="YunoJuno"))["record"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ask(self, **extra):
        payload = dict(subject=self.agency["id"], kind="join", question="Join YunoJuno?",
                       context="UK-first marketplace.", recommendation="help", reason="QA is a category.")
        payload.update(extra)
        return records.create_decision(self.conn, payload)

    def live(self):
        return [d for d in records.state(self.conn)["decisions"] if d["status"] in ("open", "answered")]

    def save_draft(self, body='First exact text'):
        r = records.get(self.conn, self.agency['id'])
        records.draft(self.conn, dict(record_id=r['id'], title='Note', text=body, revision=r['revision']), actor='agent')

    def test_decision_history_and_state_queries_are_bounded_as_history_grows(self):
        def add(start, stop):
            with records.transaction(self.conn):
                self.conn.executemany('INSERT INTO decisions(subject,record_id,kind,question,status,created_at,updated_at,processed_at) '
                    'VALUES(?,?,?,?,?,?,?,?)', [(self.agency['id'], self.agency['id'], 'join', f'Question {i}',
                                               'done', db.now(), db.now(), db.now()) for i in range(start, stop)])
        def count(call):
            queries = []
            self.conn.set_trace_callback(queries.append)
            try:
                result = call()
            finally:
                self.conn.set_trace_callback(None)
            return result, len(queries)
        add(0, 100)
        _, before_history = count(lambda: records.decisions_history(self.conn))
        _, before_state = count(lambda: records.state(self.conn))
        add(100, 1000)
        history, after_history = count(lambda: records.decisions_history(self.conn))
        state, after_state = count(lambda: records.state(self.conn))
        self.assertEqual(len(history), 1000)
        self.assertEqual(len(state['decisions']), 1000)
        self.assertLess(after_history, 10)
        self.assertLess(after_state, 100)
        self.assertEqual(after_history, before_history)
        self.assertEqual(after_state, before_state)

    def test_batch_history_matches_single_views_for_exact_and_legacy_drafts(self):
        self.save_draft('Original approved text')
        first = self.ask(kind='draft', recommendation='approve')['decision']
        first = records.answer_decision(self.conn, first['id'], dict(choice='approve', revision=first['revision']))['decision']
        records.process_decision(self.conn, first['id'], 'done', 'Approved', revision=first['revision'])
        self.save_draft('Current legacy snapshot')
        current = self.ask(kind='draft', recommendation='approve')['decision']
        current = records.answer_decision(self.conn, current['id'], dict(choice='approve', revision=current['revision']))['decision']
        records.process_decision(self.conn, current['id'], 'done', 'Historical completion', revision=current['revision'])
        self.conn.execute("UPDATE decisions SET draft_binding='legacy',processed_at='2000-01-01' WHERE id=?", (current['id'],))
        workspace = records.create_decision(self.conn, dict(subject='workspace:test-history', kind='approve', question='Proceed?'))['decision']
        expected = [records.get_decision(self.conn, did) for did in (first['id'], current['id'], workspace['id'])]
        history = records.decisions_history(self.conn)
        self.assertEqual(history, expected)
        self.assertEqual([d['draft']['text'] for d in history[:2]], ['Original approved text', 'Current legacy snapshot'])
        self.assertEqual([d['draft_is_current'] for d in history[:2]], [False, True])
        self.assertEqual([d['can_reconfirm'] for d in history[:2]], [False, True])
        self.assertEqual(records.decisions_history(self.conn, self.agency['id']), expected[:2])
        live = {d['id']: d for d in records.decisions_state(self.conn)}
        self.assertEqual(live[current['id']], expected[1])
        self.assertEqual(live[workspace['id']], expected[2])

    def test_draft_replacement_invalidates_approval_and_keeps_exact_history(self):
        self.save_draft()
        d = self.ask(kind='draft', recommendation='approve')['decision']
        self.assertEqual(d['draft']['text'], 'First exact text')
        approved = records.answer_decision(self.conn, d['id'], dict(choice='approve', revision=d['revision']))['decision']
        records.process_decision(self.conn, d['id'], 'done', 'Approved for the owner to send.', revision=approved['revision'])
        self.save_draft('Replacement needs new approval')
        old = records.get_decision(self.conn, d['id'])
        self.assertEqual(old['status'], 'withdrawn')
        self.assertEqual(old['draft']['text'], 'First exact text')
        self.assertFalse(old['draft_is_current'])
        new = self.ask(kind='draft', recommendation='approve')['decision']
        self.assertNotEqual(new['draft_version_id'], old['draft_version_id'])
        self.assertEqual(new['draft']['text'], 'Replacement needs new approval')
        self.assertEqual(len(records.decisions_history(self.conn, self.agency['id'])), 2)

    def test_replacement_and_removal_refuse_stale_answers(self):
        for replace in (True, False):
            with self.subTest(replace=replace):
                self.save_draft()
                d = self.ask(kind='draft', recommendation='approve')['decision']
                if replace:
                    self.save_draft('Changed')
                else:
                    r = records.get(self.conn, self.agency['id'])
                    records.clear_draft(self.conn, dict(record_id=r['id'], revision=r['revision']))
                with self.assertRaises(records.Conflict):
                    records.answer_decision(self.conn, d['id'], dict(choice='approve', revision=d['revision']))

    def test_processing_checks_revision_and_claim_freezes_the_exact_answer(self):
        d = self.ask()['decision']
        d = records.answer_decision(self.conn, d['id'], dict(choice='help', revision=d['revision']))['decision']
        first = d
        d = records.answer_decision(self.conn, d['id'], dict(choice='no', revision=d['revision']))['decision']
        with self.assertRaises(records.Conflict):
            records.process_decision(self.conn, d['id'], 'done', 'Obsolete work', revision=first['revision'])
        with self.assertRaises(records.Conflict):
            records.process_decision(self.conn, d['id'], 'done', 'Missing read revision')
        claimed = records.process_decision(self.conn, d['id'], 'claim', 'Reviewing the request', revision=d['revision'])['decision']
        self.assertEqual((claimed['execution_status'], claimed['claimed_answer_revision']), ('working', d['answer_revision']))
        with self.assertRaises(records.Conflict):
            records.answer_decision(self.conn, d['id'], dict(choice='later', revision=claimed['revision']))
        blocked = records.process_decision(self.conn, d['id'], 'block', 'Need account access', revision=claimed['revision'])['decision']
        self.assertEqual(blocked['execution_status'], 'blocked')
        done = records.process_decision(self.conn, d['id'], 'done', 'Closed as requested', revision=blocked['revision'])['decision']
        self.assertEqual(done['execution_status'], 'completed')

    def test_draft_decision_needs_saved_text_and_old_decisions_remain_accessible(self):
        with self.assertRaises(ValueError):
            self.ask(kind='draft', recommendation='approve')
        d = self.ask()['decision']
        d = records.answer_decision(self.conn, d['id'], dict(choice='no', revision=d['revision']))['decision']
        records.process_decision(self.conn, d['id'], 'done', 'Closed', revision=d['revision'])
        self.conn.execute("UPDATE decisions SET processed_at='2000-01-01' WHERE id=?", (d['id'],))
        self.assertEqual(records.decisions_state(self.conn), [])
        self.assertEqual(records.decisions_history(self.conn, self.agency['id'])[0]['answer'], 'no')

    def test_old_current_legacy_approval_is_visible_and_can_confirm_current_text(self):
        self.save_draft()
        d=self.ask(kind='draft',recommendation='approve')['decision']
        d=records.answer_decision(self.conn,d['id'],dict(choice='approve',revision=d['revision']))['decision']
        d=records.process_decision(self.conn,d['id'],'done','Original historical completion',revision=d['revision'])['decision']
        self.conn.execute("UPDATE decisions SET draft_binding='legacy',processed_at='2000-01-01' WHERE id=?",(d['id'],))
        current=records.decisions_state(self.conn)[0]
        self.assertEqual(current['status'],'done')
        self.assertTrue(current['can_reconfirm'])
        confirmed=records.answer_decision(self.conn,d['id'],dict(choice='approve',revision=current['revision']))['decision']
        self.assertEqual((confirmed['status'],confirmed['draft_binding'],confirmed['execution_status']),('answered','exact','queued'))
        self.assertEqual(confirmed['outcome'],'')
        self.assertTrue(any(t.get('action')=='legacy_reconfirmation' and 'Original historical completion' in t['text'] for t in confirmed['thread']))
        self.assertFalse(confirmed['can_reconfirm'])

    def test_legacy_reconfirmation_never_revives_a_superseded_decision(self):
        self.save_draft()
        d=self.ask(kind='draft',recommendation='approve')['decision']
        d=records.answer_decision(self.conn,d['id'],dict(choice='approve',revision=d['revision']))['decision']
        d=records.process_decision(self.conn,d['id'],'done','Old approval',revision=d['revision'])['decision']
        self.conn.execute("UPDATE decisions SET draft_binding='legacy',processed_at='2000-01-01' WHERE id=?",(d['id'],))
        newer=self.ask(kind='draft',recommendation='hold')['decision']
        self.assertFalse(records.get_decision(self.conn,d['id'])['can_reconfirm'])
        self.assertEqual([v['id'] for v in records.decisions_state(self.conn)],[newer['id']])
        with self.assertRaises(records.Conflict):
            records.answer_decision(self.conn,d['id'],dict(choice='approve',revision=d['revision']))

    def test_a_decision_declares_its_choices_with_what_happens_next(self):
        self.ask()
        d = self.live()[0]
        ids = [c["id"] for c in d["choices"]]
        self.assertEqual(ids, [c[0] for c in states.DECISION_KINDS["join"]["choices"]] + ["ask"])
        self.assertTrue(all(c["label"] and c["next"] for c in d["choices"]))
        self.assertEqual(d["choices"][-1]["note"], "Your question")
        self.assertEqual((d["record_name"], d["recommendation"]), ("YunoJuno", "help"))

    def test_each_decision_says_whether_it_is_about_the_job_search_or_the_site(self):
        # 27 Sep: "Waiting on you" should separate site and operational questions from job hunting.
        source = records.create(self.conn, dict(type="source", name="Job search APIs that need keys"))["record"]
        self.ask()
        records.create_decision(self.conn, dict(subject="workspace:hosting", kind="approve", question="Host it?"))
        records.create_decision(self.conn, dict(subject=source["id"], kind="join", question="Request the API keys?"))
        areas = {d["question"]: (d["area"], d["area_label"]) for d in self.live()}
        self.assertEqual(areas["Join YunoJuno?"], ("search", states.DECISION_AREAS["search"]))
        self.assertEqual(areas["Host it?"][0], "operations")
        self.assertEqual(areas["Request the API keys?"][0], "operations")
        self.assertEqual(list(states.DECISION_AREAS), ["operations", "search"])  # the order the page shows them

    def test_asking_twice_returns_the_live_decision(self):
        first = self.ask()
        again = self.ask(question="Join YunoJuno now?")
        self.assertTrue(first["saved"])
        self.assertFalse(again["saved"])
        self.assertEqual(first["decision"]["id"], again["decision"]["id"])

    def test_workspace_decisions_are_unique_per_subject(self):
        a = records.create_decision(self.conn, dict(subject="workspace:local-acceptance", kind="approve", question="Accept?"))
        b = records.create_decision(self.conn, dict(subject="workspace:local-acceptance", kind="approve", question="Accept?"))
        self.assertEqual(a["decision"]["id"], b["decision"]["id"])
        self.assertIsNone(a["decision"]["record_id"])

    def test_bad_kind_subject_or_recommendation_is_refused(self):
        with self.assertRaises(ValueError):
            self.ask(kind="whatever")
        with self.assertRaises(ValueError):
            self.ask(subject="CO-missing")
        with self.assertRaises(ValueError):
            self.ask(recommendation="approve")
        with self.assertRaises(ValueError):
            records.create_decision(self.conn, dict(subject="workspace:Bad Slug", kind="approve", question="x"))

    def test_owner_answers_and_can_change_it_until_processed(self):
        d = self.ask()["decision"]
        d = records.answer_decision(self.conn, d["id"], dict(choice="later", revision=d["revision"]))["decision"]
        self.assertEqual((d["status"], d["answer"]), ("answered", "later"))
        d = records.answer_decision(self.conn, d["id"], dict(choice="help", note="Profile first", revision=d["revision"]))["decision"]
        self.assertEqual((d["answer"], d["note"]), ("help", "Profile first"))
        self.assertEqual([t["choice"] for t in d["thread"]], ["later", "help"])

    def test_answers_are_validated(self):
        d = self.ask()["decision"]
        with self.assertRaises(ValueError):
            records.answer_decision(self.conn, d["id"], dict(choice="approve", revision=d["revision"]))
        with self.assertRaises(ValueError):
            records.answer_decision(self.conn, d["id"], dict(choice="ask", revision=d["revision"]))  # needs a note
        with self.assertRaises(records.Conflict):
            records.answer_decision(self.conn, d["id"], dict(choice="no", revision=d["revision"] + 1))
        with self.assertRaises(ValueError):
            records.answer_decision(self.conn, d["id"], dict(choice="no", revision=d["revision"]), actor="agent")

    def test_an_answer_changes_no_record(self):
        d = self.ask()["decision"]
        before = records.get(self.conn, self.agency["id"])
        records.answer_decision(self.conn, d["id"], dict(choice="no", revision=d["revision"]))
        after = records.get(self.conn, self.agency["id"])
        self.assertEqual({k: before[k] for k in ("summary", "status", "working_state")},
                         {k: after[k] for k in ("summary", "status", "working_state")})

    def test_the_record_waits_on_the_decision_until_it_is_processed(self):
        d = self.ask()["decision"]
        r = records.get(self.conn, self.agency["id"])
        self.assertEqual((r["owner"], r["working_state"], r["waiting_on"]), ("owner", "waiting", records.DECISION_WAITING))
        d = records.answer_decision(self.conn, d["id"], dict(choice="ask", note="Fees?", revision=d["revision"]))["decision"]
        d = records.process_decision(self.conn, d["id"], "reply", "None for freelancers.", revision=d["revision"])["decision"]
        self.assertEqual(records.get(self.conn, self.agency["id"])["working_state"], "waiting")
        d = records.answer_decision(self.conn, d["id"], dict(choice="no", revision=d["revision"]))["decision"]
        records.process_decision(self.conn, d["id"], "done", "Closed; ranked down.", revision=d["revision"])
        r = records.get(self.conn, self.agency["id"])
        self.assertEqual((r["owner"], r["working_state"]), ("agent", "done"))
        self.assertIn("Closed; ranked down.", r["next_action"])

    def test_processing_leaves_a_next_step_the_agent_has_since_changed(self):
        d = self.ask()["decision"]
        r = records.get(self.conn, self.agency["id"])
        records.followup(self.conn, dict(id=r["id"], action="Prepare the profile", owner="agent", status="ready", revision=r["revision"]), actor="agent")
        d = records.answer_decision(self.conn, d["id"], dict(choice="help", revision=d["revision"]))["decision"]
        records.process_decision(self.conn, d["id"], "done", "Profile text drafted.", revision=d["revision"])
        self.assertEqual(records.get(self.conn, self.agency["id"])["next_action"], "Prepare the profile")

    def test_a_reworded_question_replaces_its_withdrawal_but_a_real_withdrawal_shows(self):
        first = self.ask()["decision"]
        records.process_decision(self.conn, first["id"], "withdraw", "Re-asked in plainer terms.", revision=first["revision"])
        second = self.ask(question="Register with YunoJuno?")["decision"]
        job = records.create(self.conn, dict(type="job", name="Test Lead"))["record"]
        records.draft(self.conn, dict(record_id=job["id"], title="Note", text="Saved text", revision=job["revision"]))
        gone = records.create_decision(self.conn, dict(subject=job["id"], kind="draft", question="Approve the draft?"))["decision"]
        records.process_decision(self.conn, gone["id"], "withdraw", "The listing has expired.", revision=gone["revision"])
        shown = {d["id"]: d["status"] for d in records.state(self.conn)["decisions"]}
        self.assertEqual(shown, {second["id"]: "open", gone["id"]: "withdrawn"})

    def test_decisions_come_in_order_of_urgency_then_recommendation(self):
        job = records.create(self.conn, dict(type="job", name="Test Lead"))["record"]
        records.draft(self.conn, dict(record_id=job["id"], title="Note", text="Saved text", revision=job["revision"]))
        other = records.create(self.conn, dict(type="agency", name="Contra"))["record"]
        records.create_decision(self.conn, dict(subject=other["id"], kind="join", question="Join Contra?", recommendation="no"))
        self.ask()  # recommends help
        records.create_decision(self.conn, dict(subject="workspace:daily-run", kind="approve", question="Run daily?"))
        records.create_decision(self.conn, dict(subject=job["id"], kind="draft", question="Approve the draft?"))
        order = [d["question"] for d in records.state(self.conn)["decisions"]]
        self.assertEqual(order, ["Approve the draft?", "Run daily?", "Join YunoJuno?", "Join Contra?"])

    def test_a_kind_no_longer_declared_neither_breaks_the_workspace_nor_takes_answers(self):
        d = self.ask()["decision"]
        self.conn.execute("UPDATE decisions SET kind='retired' WHERE id=?", (d["id"],))
        shown = records.state(self.conn)["decisions"][0]
        self.assertEqual((shown["choices"], shown["kind_label"]), ([], "Kind no longer declared"))
        with self.assertRaises(ValueError):
            records.answer_decision(self.conn, d["id"], dict(choice="no", revision=d["revision"]))
        records.process_decision(self.conn, d["id"], "withdraw", "Kind retired.", revision=d["revision"])

    def test_the_agent_processes_replies_or_withdraws(self):
        d = self.ask()["decision"]
        with self.assertRaises(ValueError):
            records.process_decision(self.conn, d["id"], "done", "too early", revision=d["revision"])
        d = records.answer_decision(self.conn, d["id"], dict(choice="ask", note="What does it cost?", revision=d["revision"]))["decision"]
        d = records.process_decision(self.conn, d["id"], "reply", "No fee for freelancers.", revision=d["revision"])["decision"]
        self.assertEqual((d["status"], d["answer"]), ("open", ""))
        self.assertEqual([t["by"] for t in d["thread"]], ["owner", "agent"])
        d = records.answer_decision(self.conn, d["id"], dict(choice="registered", revision=d["revision"]))["decision"]
        d = records.process_decision(self.conn, d["id"], "done", "Recorded as your channel.", revision=d["revision"])["decision"]
        self.assertEqual((d["status"], d["outcome"]), ("done", "Recorded as your channel."))
        self.assertTrue(self.ask()["saved"], "a processed decision no longer blocks a new one")
        w = records.process_decision(self.conn, self.live()[0]["id"], "withdraw", "Advert closed.", revision=self.live()[0]["revision"])["decision"]
        self.assertEqual(w["status"], "withdrawn")


class QuestionTests(unittest.TestCase):
    """"Ask Claude…" on a card: the owner asks on the site, an agent answers once, the record is untouched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.migrate(self.conn)
        self.person = records.create(self.conn, dict(type="person", name="Ada Example"))["record"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_owner_asks_and_the_agent_answers_once_without_touching_the_record(self):
        q = records.ask_question(self.conn, self.person["id"], dict(question="  Prep me for a call?  "))["question"]
        self.assertEqual((q["question"], q["asked_by"], q["answer"], q["record_name"]), ("Prep me for a call?", "owner", "", "Ada Example"))
        self.assertEqual(records.get(self.conn, self.person["id"])["revision"], self.person["revision"])
        self.assertEqual([x["id"] for x in records.questions(self.conn, unanswered=True)], [q["id"]])
        a = records.answer_question(self.conn, q["id"], "Ask about their platform team.", actor="agent")["question"]
        self.assertEqual((a["answer"], a["answered_by"]), ("Ask about their platform team.", "agent"))
        self.assertTrue(a["answered_at"])
        self.assertEqual(records.questions(self.conn, unanswered=True), [])
        self.assertEqual([x["answer"] for x in records.state(self.conn)["questions"]], ["Ask about their platform team."])
        self.assertEqual([x["id"] for x in records.state(self.conn, compact=True)["questions"]], [q["id"]])
        with self.assertRaises(records.Conflict):
            records.answer_question(self.conn, q["id"], "Again", actor="agent")
        self.assertEqual(records.get(self.conn, self.person["id"])["revision"], self.person["revision"])

    def test_only_owner_asks_only_an_agent_answers_and_only_on_an_active_record(self):
        with self.assertRaises(ValueError):
            records.ask_question(self.conn, self.person["id"], dict(question="x"), actor="agent")
        with self.assertRaises(ValueError):
            records.ask_question(self.conn, self.person["id"], dict(question="   "))
        q = records.ask_question(self.conn, self.person["id"], dict(question="What should I say back?"))["question"]
        with self.assertRaises(ValueError):
            records.answer_question(self.conn, q["id"], "My own answer", actor="owner")
        with self.assertRaises(ValueError):
            records.answer_question(self.conn, q["id"], "  ", actor="agent")
        with self.assertRaises(ValueError):
            records.answer_question(self.conn, 999, "Nobody asked", actor="agent")
        records.archive(self.conn, self.person["id"], dict(reason="Duplicate", revision=self.person["revision"]))
        with self.assertRaises(ValueError):
            records.ask_question(self.conn, self.person["id"], dict(question="Still there?"))

    def test_answered_questions_leave_the_site_after_two_weeks_but_unanswered_ones_stay(self):
        old, waiting = (records.ask_question(self.conn, self.person["id"], dict(question=t))["question"]["id"] for t in ("Old", "Waiting"))
        records.answer_question(self.conn, old, "Done", actor="agent")
        self.conn.execute("UPDATE questions SET asked_at='2026-01-01T09:00:00+00:00'")
        self.conn.commit()
        self.assertEqual([q["id"] for q in records.state(self.conn)["questions"]], [waiting])


if __name__ == "__main__":
    unittest.main()
