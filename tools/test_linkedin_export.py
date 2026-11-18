"""LinkedIn export snapshot and import on synthetic fixtures. The real export is never used here."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linkedin_export as lx  # noqa: E402
import wo  # noqa: E402
import profile_rules  # noqa: E402
from test_profile_rules import EXAMPLE  # noqa: E402

OWNER = "https://www.linkedin.com/in/owner"
BODY = "SENTINEL-BODY-do-not-store"
SUBJECT = "SENTINEL-SUBJECT-do-not-store"


def u(slug):
    return f"https://www.linkedin.com/in/{slug}"


def message(cid, sender, recipient, date, folder="INBOX", draft="No", subject="", body=BODY, sender_name="Someone"):
    return [cid, "", sender_name, sender, "Them", recipient, f"{date} 10:00:00 UTC", subject, body, folder, "", draft]


def write_csv(path, header, rows, preamble=""):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    path.write_text(preamble + buf.getvalue(), encoding="utf-8")


class LinkedInExportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "2026-09-21"
        self.dir.mkdir()
        write_csv(self.dir / "Connections.csv", ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"], [
            ["Ann", "Warm", u("ann"), "", "Client Server", "Consultant", "02 Sep 2026"],
            ["Bob", "Former", u("bob"), "", "Example Corp", "Engineer", "01 Jan 2016"],
            ["Cat", "Endorser", u("cat"), "cat@example.com", "Elsewhere", "QA Lead", "05 May 2015"],
            ["Dan", "Quiet", u("dan"), "", "Nowhere", "Engineer", "05 May 2015"],
            ["", "", "", "", "", "", "05 May 2015"],
        ], preamble='Notes:\n"Some emails are missing."\n\n')
        header = ["CONVERSATION ID", "CONVERSATION TITLE", "FROM", "SENDER PROFILE URL", "TO", "RECIPIENT PROFILE URLS",
                  "DATE", "SUBJECT", "CONTENT", "FOLDER", "ATTACHMENTS", "IS MESSAGE DRAFT"]
        write_csv(self.dir / "messages.csv", header, [
            message("c1", OWNER, u("ann"), "2026-03-01", subject=SUBJECT),
            message("c1", u("ann"), OWNER, "2026-03-02", sender_name="Ann Warm"),
            message("c2", u("bob"), OWNER, "2025-06-01", body="Example Corp mention in text only"),
            message("c3", u("dan"), OWNER, "2026-01-01", folder="SPAM"),
            message("c3", OWNER, u("dan"), "2026-01-02", folder="SPAM"),
            message("c4", OWNER, u("dan"), "2026-02-01", draft="Yes"),
            message("c5", u("zed"), OWNER, "2026-04-01", sender_name="Zed Recruiter"),
        ])
        write_csv(self.dir / "Endorsement_Received_Info.csv",
                  ["Endorsement Date", "Skill Name", "Endorser First Name", "Endorser Last Name", "Endorser Public Url", "Endorsement Status"],
                  [["2016/08/01 20:56:47 UTC", "QA", "Cat", "Endorser", "www.linkedin.com/in/cat", "ACCEPTED"]])
        write_csv(self.dir / "Endorsement_Given_Info.csv",
                  ["Endorsement Date", "Skill Name", "Endorsee First Name", "Endorsee Last Name", "Endorsee Public Url", "Endorsement Status"], [])
        for f in ("Recommendations_Received.csv", "Recommendations_Given.csv"):
            write_csv(self.dir / f, ["First Name", "Last Name", "Company", "Job Title", "Text", "Creation Date", "Status"], [])
        write_csv(self.dir / "Invitations.csv", ["From", "To", "Sent At", "Message", "Direction", "inviterProfileUrl", "inviteeProfileUrl"],
                  [["The owner", "Ann", "8/26/26, 10:05 AM", "SENTINEL-INVITE", "OUTGOING", OWNER, u("ann")]])
        excluded = self.dir / "Inferences_about_you.csv"
        excluded.write_text("SENTINEL-EXCLUDED")
        os.chmod(excluded, 0)  # reading it would raise; the reader must never open it
        self.conn = wo.wodb.connect(Path(self.tmp.name) / "t.db")
        wo.wodb.migrate(self.conn)
        wo.store.create(self.conn, dict(type="agency", name="Client Server"))

    def tearDown(self):
        os.chmod(self.dir / "Inferences_about_you.csv", 0o600)
        self.conn.close()
        self.tmp.cleanup()

    def snap(self):
        return lx.snapshot(self.dir, "linkedin.com/in/owner")

    def test_snapshot_is_deterministic_and_carries_no_message_text(self):
        first, second = self.snap(), self.snap()
        self.assertEqual(first, second)
        text = json.dumps(first)
        for sentinel in (BODY, SUBJECT, "SENTINEL-INVITE", "SENTINEL-EXCLUDED", "Example Corp mention"):
            self.assertNotIn(sentinel, text)
        self.assertIn("Inferences_about_you.csv", first["export"]["excluded_unread"])

    def test_counts_direction_and_evidence_rules(self):
        s = self.snap()
        c = s["export"]["counts"]
        self.assertEqual((c["connections"], c["connections_with_name_and_url"], c["connections_blank"], c["emails"]), (5, 4, 1, 1))
        self.assertEqual(c["skipped_messages"], {"draft": 1, "spam": 2})
        ann = s["people"][u("ann")]["evidence"]
        self.assertEqual((ann["threads"]["two_way"], ann["threads"]["last_sent"], ann["threads"]["subject"]), (1, "2026-03-02", True))
        self.assertEqual(ann["invitation"], {"direction": "outgoing", "date": "2026-08-26"})
        self.assertEqual(s["people"][u("dan")]["evidence"]["threads"], {})  # spam and drafts are not evidence
        self.assertEqual(s["people"][u("bob")]["flags"], ["former_employer"])  # from company only
        self.assertEqual(s["people"][u("cat")]["evidence"]["endorsed_you"], 1)
        self.assertIn(u("zed"), s["counterparts"])

    def test_first_tranche_queues_evidence_only_and_rerun_is_a_no_op(self):
        report = lx.import_snapshot(self.conn, self.snap(), "first")
        self.assertEqual(report["queued"], 2)  # Ann (two-way, agency match), Cat (endorsement); not Bob or Dan
        self.assertEqual(report["outside_selection"], 2)
        cards = [json.loads(r[0]) for r in self.conn.execute("SELECT payload_json FROM review_queue ORDER BY id")]
        self.assertEqual([c["name"] for c in cards], ["Ann Warm", "Cat Endorser"])  # strongest first
        self.assertEqual(cards[0]["org_match"]["relation"], "consultant_at")
        dump = json.dumps(wo.dump(self.conn))
        for kept_out in (BODY, SUBJECT, "conversation_ids"):
            self.assertNotIn(kept_out, dump)
        again = lx.import_snapshot(self.conn, self.snap(), "first")
        self.assertEqual((again["queued"], again["unchanged"]), (0, 2))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM interactions").fetchone()[0], 0)

    def test_accept_links_the_agency_and_sets_last_touch_from_sent_threads_only(self):
        lx.import_snapshot(self.conn, self.snap(), "first")
        qid, rev = self.conn.execute("SELECT id, revision FROM review_queue ORDER BY id LIMIT 1").fetchone()
        person = wo.store.resolve_review(self.conn, qid, dict(resolution="accept", revision=rev))["record"]
        self.assertEqual(person["last_touch"], "2026-03-02")
        self.assertIsNone(person["relationship"])
        links = [tuple(r) for r in self.conn.execute("SELECT relation FROM links WHERE from_id=?", (person["id"],))]
        self.assertEqual(links, [("consultant_at",)])

    def test_former_employer_card_carries_the_warning_and_the_accepted_record_keeps_it(self):
        lx.import_snapshot(self.conn, self.snap(), "all")
        row = next(r for r in self.conn.execute("SELECT id, revision, payload_json FROM review_queue")
                   if json.loads(r[2])["name"] == "Bob Former")
        self.assertEqual(json.loads(row[2])["warning"], "Example Corp: check confidentiality and non-solicitation obligations before contact.")
        person = wo.store.resolve_review(self.conn, row[0], dict(resolution="accept", revision=row[1]))["record"]
        self.assertIn("Example Corp: check confidentiality", person["known_context"])
        self.assertEqual(person["last_touch"], "")  # received-only thread is not evidence of contact

    def test_a_later_export_refreshes_accepted_people_and_only_moves_last_touch_forward(self):
        lx.import_snapshot(self.conn, self.snap(), "first")
        qid, rev = self.conn.execute("SELECT id, revision FROM review_queue ORDER BY id LIMIT 1").fetchone()
        pid = wo.store.resolve_review(self.conn, qid, dict(resolution="accept", revision=rev))["record"]["id"]
        later = self.snap()
        later["export"]["folder"] = "2026-12-01"
        later["people"][u("ann")]["evidence"]["threads"]["last_sent"] = "2026-11-30"
        report = lx.import_snapshot(self.conn, later, "none")
        self.assertEqual((report["queued"], report["refreshed"], report["last_touch_advanced"]), (0, 1, 1))
        self.assertEqual(wo.store.get(self.conn, pid)["last_touch"], "2026-11-30")
        older = self.snap()
        older["export"]["folder"] = "2026-06-01"
        older["people"][u("ann")]["evidence"]["threads"]["last_sent"] = "2025-01-01"
        lx.import_snapshot(self.conn, older, "none")
        self.assertEqual(wo.store.get(self.conn, pid)["last_touch"], "2026-11-30")

    def test_changed_employment_raises_a_new_card(self):
        lx.import_snapshot(self.conn, self.snap(), "first")
        moved = self.snap()
        moved["people"][u("ann")]["company"] = "New Agency"
        self.assertEqual(lx.import_snapshot(self.conn, moved, "first")["queued"], 1)

    def test_a_name_match_cannot_be_accepted_as_new_by_accident(self):
        wo.store.create(self.conn, dict(type="person", name="Ann Warm"))
        lx.import_snapshot(self.conn, self.snap(), "first")
        qid, rev, payload = self.conn.execute("SELECT id, revision, payload_json FROM review_queue ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(json.loads(payload)["matches"][0]["match"], "name only")
        with self.assertRaises(ValueError):
            wo.store.resolve_review(self.conn, qid, dict(resolution="accept", revision=rev))
        person = wo.store.resolve_review(self.conn, qid, dict(resolution="accept", accept_as_new=True, revision=rev))["record"]
        self.assertEqual(self.conn.execute("SELECT count(*) FROM records WHERE name='Ann Warm'").fetchone()[0], 2)
        self.assertTrue(person["id"])

    def test_merge_adds_linkedin_evidence_to_the_existing_person(self):
        existing = wo.store.create(self.conn, dict(type="person", name="Ann Warm"))["record"]
        lx.import_snapshot(self.conn, self.snap(), "first")
        qid, rev = self.conn.execute("SELECT id, revision FROM review_queue ORDER BY id LIMIT 1").fetchone()
        merged = wo.store.resolve_review(self.conn, qid, dict(resolution="merge", target_id=existing["id"], revision=rev))["record"]
        self.assertEqual((merged["id"], merged["url"], merged["last_touch"]), (existing["id"], u("ann"), "2026-03-02"))

    def test_a_logged_interaction_does_not_freeze_later_linkedin_dates(self):
        lx.import_snapshot(self.conn, self.snap(), "first")
        qid, rev = self.conn.execute("SELECT id, revision FROM review_queue ORDER BY id LIMIT 1").fetchone()
        pid = wo.store.resolve_review(self.conn, qid, dict(resolution="accept", revision=rev))["record"]["id"]
        wo.log_interaction(self.conn, pid, "call", "out", "2026-04-01", "Call")
        later = self.snap()
        later["export"]["folder"] = "2026-12-01"
        later["people"][u("ann")]["evidence"]["threads"]["last_sent"] = "2026-11-30"
        lx.import_snapshot(self.conn, later, "none")
        self.assertEqual(wo.store.get(self.conn, pid)["last_touch"], "2026-11-30")

    def test_default_json_export_is_committed_safe(self):
        lx.import_snapshot(self.conn, self.snap(), "first")
        out = Path(self.tmp.name) / "export.json"
        wo.main(["--db", str(Path(self.tmp.name) / "t.db"), "export-json", str(out)])
        self.assertNotIn("Ann Warm", out.read_text())
        wo.main(["--db", str(Path(self.tmp.name) / "t.db"), "export-json", str(out), "--full"])
        self.assertIn("Ann Warm", out.read_text())

    def test_connections_csv_upload_and_export_share_one_dedupe_key(self):
        csv_text = (self.dir / "Connections.csv").read_text()
        wo.store.import_linkedin(self.conn, csv_text)
        report = lx.import_snapshot(self.conn, self.snap(), "all")
        self.assertEqual(report["queued"], 0)


if __name__ == "__main__":
    unittest.main()
