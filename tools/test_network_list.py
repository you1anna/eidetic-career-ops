"""The owner's list: the sheet, checking a returned sheet and the import, on synthetic people only."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import network_list as nl  # noqa: E402
import wo  # noqa: E402
import profile_rules  # noqa: E402
from test_profile_rules import EXAMPLE  # noqa: E402

store = wo.store


def u(slug):
    return f"https://www.linkedin.com/in/{slug}"


def person(slug, name, company="", position="", two_way=0, last="", endorsed=0, flags=(), connected="2020-01-01"):
    first, _, last_name = name.partition(" ")
    threads = dict(count=two_way, two_way=two_way, last_two_way=last, conversation_ids=["c-secret"]) if two_way else {}
    return dict(url=u(slug), name=name, first_name=first, last_name=last_name, email="", company=company, position=position,
                connected_on="01 Jan 2020", connected_date=connected, flags=list(flags),
                evidence=dict(endorsed_you=endorsed, you_endorsed=0, recommendations=[], invitation=None, threads=threads))


SNAPSHOT = dict(export=dict(folder="2026-09-21"), people={p["url"]: p for p in [
    person("ann", "Ann Endorser", "Northwind Fintech", "Head of Engineering", 3, "2025-05-01", endorsed=2),
    person("bob", "Bob Desk", "Acme Recruitment", "Senior Recruitment Consultant, QA and Testing", connected="2024-01-01"),
    person("cat", "Cat Current", "Example Corp", "Engineering Manager", 5, "2026-01-01", flags=["former_employer"]),
    person("dan", "Dan Declined", "Elsewhere", "CTO", 1, "2023-01-01"),
    person("eve", "Eve Tagged", "Brightpath", "Director of Engineering", 2, "2024-03-01"),
    person("fay", "Fay Quiet", "Somewhere", "Inspector", connected="2026-02-01"),
    person("gus", "Gus Listed", "Harbour", "VP Product", 1, "2022-01-01"),
    person("hal", "Hal Sales", "Shop", "Director of Sales", connected="2021-01-01"),
    person("jon", "Jon Notes", "Workshop", "Engineer", connected="2019-01-01"),
]})


class Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = wo.wodb.connect(self.dir / "t.db")
        wo.wodb.migrate(self.conn)
        self.eve = store.create(self.conn, dict(type="person", name="Eve Tagged", url=u("eve")))["record"]
        self.eve = store.transition(self.conn, self.eve["id"], dict(field="relationship", to="active", revision=self.eve["revision"]))["record"]
        self.gus = store.create(self.conn, dict(type="person", name="Gus Listed", url=u("gus"), priority="first",
                                                preferred_channel="call"))["record"]
        self.ann = store.create(self.conn, dict(type="person", name="Ann Endorser", url=u("ann")))["record"]
        store.create(self.conn, dict(type="company", name="Northwind Fintech"))
        # Turned down in the 24 September review, through the same card an export import queues.
        card, extra = nl.linkedin_export.card(self.conn, SNAPSHOT["people"][u("dan")], [], "2026-09-21")
        qid, _ = store.linkedin_card(self.conn, card, source="LinkedIn export 2026-09-21", extra=extra)
        store.resolve_review(self.conn, qid, dict(resolution="reject", revision=1))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def site(self):
        self.conn.row_factory = wo.sqlite3.Row
        return nl._read_site(self.conn)

    def sheet(self, edits=None, drop=()):
        rows, key = nl.sheet_rows(SNAPSHOT, self.site())
        path = self.dir / "2026-10-14-network-review.csv"
        nl.write_sheet(rows, key, path, "2026-09-21")
        if edits or drop:
            with path.open(encoding="utf-8-sig", newline="") as f:
                lines = list(csv.DictReader(f))
            for line in lines:
                line.update((edits or {}).get(line["Name"], {}))
            columns = [c for c in nl.COLUMNS if c not in drop]
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
                w.writeheader()
                w.writerows(lines)
        return path, rows


class SheetTests(Base):
    def test_titles_match_on_word_boundaries(self):
        self.assertEqual(nl.group_of("Director of Sales", False), "senior other")  # "cto" is not inside Director
        self.assertIsNone(nl.group_of("Inspector", False))
        self.assertEqual(nl.group_of("Head of Testing", False), "senior tech")
        self.assertEqual(nl.group_of("Talent Partner", False), "recruiter")
        self.assertEqual(nl.group_of("QA Lead", True), "history")
        self.assertIsNone(nl.group_of("QA Lead", False))

    def test_sheet_puts_the_list_first_and_leaves_out_former_employer_turned_down_and_tagged(self):
        path, rows = self.sheet()
        names = [r["Name"] for r in rows]
        self.assertEqual(rows[0]["Group"], "Your list")
        self.assertEqual((rows[0]["Name"], rows[0]["Priority (1/2)"], rows[0]["Channel"]), ("Gus Listed", "1", "call"))
        self.assertNotIn("Cat Current", names)   # currently at the former employer
        self.assertNotIn("Dan Declined", names)  # turned down on 24 September
        self.assertNotIn("Eve Tagged", names)    # already tagged
        self.assertEqual(names[1], "Ann Endorser")  # endorsement and recent history rank first
        self.assertEqual(rows[1]["On site"], "yes")
        self.assertEqual({r["Name"]: r["Group"] for r in rows}["Fay Quiet"], "Others")
        key = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual([k["n"] for k in key["rows"]], list(range(1, len(rows) + 1)))
        self.assertEqual(key["rows"][1], dict(n=2, url=u("ann"), record_id=self.ann["id"]))
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertNotIn("c-secret", path.read_text(encoding="utf-8-sig") + path.with_suffix(".json").read_text())

    def test_a_recorded_former_colleague_ranks_without_any_linkedin_messages(self):
        # 10 November: a colleague who worked with the owner but never messaged him on LinkedIn ranked 322nd.
        fay = store.create(self.conn, dict(type="person", name="Fay Quiet", url=u("fay")))["record"]
        store.transition(self.conn, fay["id"], dict(field="kind", to="former_colleague", revision=fay["revision"]))
        self.conn.commit()
        _, rows = self.sheet()
        names = [r["Name"] for r in rows]
        self.assertEqual(names[1:3], ["Ann Endorser", "Fay Quiet"])
        self.assertEqual(rows[2]["Group"], "Top 30")
        self.assertLess(names.index("Fay Quiet"), names.index("Hal Sales"))


class CheckTests(Base):
    def check(self, path):
        return nl.check(path, path.with_suffix(".json"), SNAPSHOT)

    def test_values_notes_and_never(self):
        path, _ = self.sheet({
            "Ann Endorser": {"How well (W/T/K/N)": "w", "Priority (1/2)": "1", "Who": "Colleague", "Channel": "WhatsApp",
                             "Ask": "intro", "Notes": "Ex-XC, worked together 2016"},
            "Bob Desk": {"How well (W/T/K/N)": "T", "Priority (1/2)": "2.0", "Who": "recruiter", "Channel": "email or call",
                         "Ask": "availability"},
            "Hal Sales": {"How well (W/T/K/N)": "K", "Notes": "private: owes me a favour"},
            "Fay Quiet": {"How well (W/T/K/N)": "N"},
            "Gus Listed": {"Notes": "Ex-XC; mentioned the medical results"},
            "Jon Notes": {"Notes": "met at a meetup"},
        })
        doc, report = self.check(path)
        self.assertEqual(report["stopped"], "")
        rows = {r["name"]: r for r in doc["rows"]}
        ann = rows["Ann Endorser"]
        self.assertEqual((ann["relationship"], ann["priority"], ann["kind"], ann["preferred_channel"], ann["preferred_ask"]),
                         ("warm", "first", "former_colleague", "whatsapp", "introduction"))
        self.assertTrue(ann["former_employer"])
        self.assertEqual(ann["record_id"], self.ann["id"])
        self.assertIsNone(ann["linkedin"])  # already on the site
        bob = rows["Bob Desk"]
        self.assertEqual((bob["relationship"], bob["priority"], bob["preferred_channel"]), ("active", "next", "email"))
        self.assertNotIn("conversation_ids", json.dumps(bob["linkedin"]))
        self.assertEqual(bob["linkedin"]["url"], u("bob"))
        self.assertEqual(len(report["several_values"]), 1)
        self.assertTrue(report["several_values"][0].endswith("channel"))
        self.assertEqual(rows["Hal Sales"]["note"], "")  # private notes stay local
        self.assertIsNone(rows["Hal Sales"]["priority"])
        self.assertEqual(report["private_notes"], 1)
        self.assertEqual(len(report["withheld_notes"]), 1)
        self.assertEqual(rows["Gus Listed"]["note"], "")  # withheld: only its row number is reported
        self.assertTrue(rows["Gus Listed"]["former_employer"])  # but the obligations warning still applies
        self.assertTrue(any("a note but no tags" in p for p in report["problems"]))
        self.assertNotIn("Jon Notes", rows)
        self.assertNotIn("medical", json.dumps(doc) + json.dumps(report))
        self.assertEqual(doc["never"], [u("fay")])
        self.assertNotIn("Fay Quiet", rows)

    def test_the_earlier_ask_words_still_read_as_the_three_asks(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "W", "Ask": "referral"},
                              "Hal Sales": {"How well (W/T/K/N)": "K", "Ask": "view"}})
        rows = {r["name"]: r for r in self.check(path)[0]["rows"]}
        self.assertEqual((rows["Ann Endorser"]["preferred_ask"], rows["Hal Sales"]["preferred_ask"]), ("introduction", "catch_up"))

    def test_unrecognised_values_are_reported_not_guessed(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "X", "Who": "Colleague"}})
        doc, report = self.check(path)
        self.assertEqual([r["name"] for r in doc["rows"]], ["Gus Listed"])  # his current list, prefilled, re-applies unchanged
        self.assertEqual(len(report["problems"]), 1)
        self.assertTrue(report["problems"][0].endswith("unrecognised relationship"))

    def test_a_moved_row_stops_the_check(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "W", "LinkedIn": u("someone-else")}})
        doc, report = self.check(path)
        self.assertIn("does not match the key", report["stopped"])
        self.assertEqual(doc["rows"], [])

    def test_the_earlier_copy_without_a_priority_column_puts_every_tagged_row_on_the_list(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "W"}, "Hal Sales": {"Who": "peer"}},
                             drop=("Priority (1/2)",))
        doc, report = self.check(path)
        rows = {r["name"]: r for r in doc["rows"]}
        self.assertFalse(report["priority_column"])
        self.assertEqual(rows["Ann Endorser"]["priority"], "next")
        self.assertNotIn("priority", rows["Hal Sales"])  # a kind alone is not a reason to approach

    def saved_by_sheets(self, path, blank=False):
        """The owner's 15 October copy as Google Sheets saved it: no byte-order mark, CRLF, his own column
        order, no Priority column, and a heading row that lost its `#` cell and moved one column left
        (or, with `blank`, kept an empty `#` heading)."""
        order = ["#", "Group", "Name", "Role", "Who", "Channel", "Ask", "Notes", "How well (W/T/K/N)", "Company",
                 "Connected", "Last two-way", "Two-way threads", "Endorsement", "On site", "Live adverts at company", "LinkedIn"]
        with path.open(encoding="utf-8-sig", newline="") as f:
            lines = list(csv.DictReader(f))
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\r\n")
            w.writerow([""] + order[1:] if blank else [" " + order[1]] + order[2:])
            w.writerows([line[c] for c in order] for line in lines)

    def test_a_heading_row_that_lost_its_hash_cell_is_realigned(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "W", "Who": "Colleague", "Channel": "WhatsApp", "Ask": "referral"},
                              "Bob Desk": {"How well (W/T/K/N)": "T", "Who": "recruiter", "Ask": "availability"}})
        self.saved_by_sheets(path)
        doc, report = self.check(path)
        self.assertEqual(report["stopped"], "")
        self.assertEqual(report["header"], "missing")
        self.assertFalse(report["priority_column"])
        rows = {r["name"]: r for r in doc["rows"]}
        self.assertEqual((rows["Ann Endorser"]["relationship"], rows["Ann Endorser"]["kind"], rows["Ann Endorser"]["preferred_channel"],
                          rows["Ann Endorser"]["preferred_ask"], rows["Ann Endorser"]["record_id"]),
                         ("warm", "former_colleague", "whatsapp", "introduction", self.ann["id"]))
        self.assertEqual((rows["Bob Desk"]["kind"], rows["Bob Desk"]["priority"]), ("recruiter", "next"))

    def test_a_blank_hash_heading_is_restored(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "K"}})
        self.saved_by_sheets(path, blank=True)
        doc, report = self.check(path)
        self.assertEqual((report["stopped"], report["header"]), ("", "blank"))
        self.assertIn("Ann Endorser", {r["name"] for r in doc["rows"]})

    def test_a_shifted_heading_without_row_numbers_still_stops(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "W"}})
        self.saved_by_sheets(path)
        text = path.read_text(encoding="utf-8").splitlines()
        body = [line.split(",", 1)[1] if "," in line else line for line in text[1:]]  # the row numbers deleted too
        path.write_text("\r\n".join([text[0], *body]) + "\r\n", encoding="utf-8")
        doc, report = self.check(path)
        self.assertIn("# column", report["stopped"])
        self.assertEqual(doc["rows"], [])

    def test_obvious_misspellings_are_read_and_listed_but_short_or_distant_words_are_not_guessed(self):
        path, _ = self.sheet({"Ann Endorser": {"How well (W/T/K/N)": "W", "Who": "colleauge", "Ask": "cacatch-up"},
                              "Bob Desk": {"How well (W/T/K/N)": "T", "Who": "peer", "Channel": "wa", "Ask": "intro"},
                              "Jon Notes": {"How well (W/T/K/N)": "T", "Who": "colleague", "Channel": "text"},
                              "Hal Sales": {"How well (W/T/K/N)": "K", "Channel": "emial"}})
        doc, report = self.check(path)
        rows = {r["name"]: r for r in doc["rows"]}
        self.assertEqual((rows["Ann Endorser"]["kind"], rows["Ann Endorser"]["preferred_ask"]), ("former_colleague", "catch_up"))
        self.assertEqual(sorted(c.split(": ", 1)[1] for c in report["corrections"]),
                         ["ask 'cacatch-up' read as 'catch-up'", "who 'colleauge' read as 'colleague'"])
        self.assertTrue(all(c.startswith("row ") for c in report["corrections"]))
        self.assertEqual((rows["Bob Desk"]["kind"], rows["Bob Desk"]["preferred_channel"], rows["Bob Desk"]["preferred_ask"]),
                         ("peer", "whatsapp", "introduction"))
        self.assertEqual((rows["Jon Notes"]["kind"], rows["Jon Notes"]["preferred_channel"]), ("former_colleague", "text"))
        self.assertNotIn("Hal Sales", rows)  # "emial" is too far from any channel to guess
        self.assertTrue(any(p.endswith("unrecognised channel") for p in report["problems"]))
        self.assertNotIn("Ann", json.dumps(report))

    def test_added_rows_need_a_name(self):
        path, _ = self.sheet()
        with path.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["", "Added", "Ivy Offline", "", "", "", "", "K", "1", "friend", "call", "catch-up", "", "", "", "", "", ""])
            w.writerow(["", "Added", "", "", "", "", "", "T", "", "", "", "", "", "", "", "", "", ""])
        doc, report = self.check(path)
        added = [r for r in doc["rows"] if r["n"] is None]
        self.assertEqual([(r["name"], r["relationship"], r["priority"]) for r in added], [("Ivy Offline", "dormant", "first")])
        self.assertTrue(any("needs a name" in p for p in report["problems"]))


class ImportTests(Base):
    def doc(self, *rows):
        return dict(source="The owner's list (test)", export="2026-09-21", made="2026-10-14", rows=list(rows))

    def row(self, slug, **values):
        p = SNAPSHOT["people"][u(slug)]
        return dict(dict(n=1, line=2, name=p["name"], url=p["url"], record_id=None, note="", former_employer=False,
                         linkedin=nl._linkedin(p)), **values)

    def test_tags_land_on_new_and_existing_people_as_agent_edits(self):
        doc = self.doc(
            dict(self.row("ann", relationship="warm", priority="first", kind="former_colleague", preferred_channel="whatsapp",
                          preferred_ask="introduction", note="Worked together 2016", former_employer=True),
                 record_id=self.ann["id"], linkedin=None),
            self.row("bob", relationship="active", priority="next", kind="recruiter", preferred_ask="availability"),
            dict(n=None, line=40, name="Ivy Offline", url="", record_id=None, note="Neighbour", former_employer=False, linkedin=None,
                 relationship="dormant", priority="first", kind="friend", preferred_channel="call"),
            dict(n=3, line=5, name="Eve Tagged", url=u("eve"), record_id=self.eve["id"], note="", former_employer=False, linkedin=None,
                 relationship="do_not_contact", priority=None))
        activity = {r["id"]: r["last_activity"] for r in store.state(self.conn)["records"]}
        last_event = self.conn.execute("SELECT MAX(id) FROM state_events").fetchone()[0]
        report = nl.import_list(self.conn, doc)
        self.assertTrue(report["saved"], report["problems"])
        self.assertEqual((report["matched"], report["accepted"], report["created"], report["listed"], report["never"]), (2, 1, 1, 3, 1))
        people = {r["name"]: r for r in store.state(self.conn)["records"] if r["type"] == "person"}
        ann = people["Ann Endorser"]
        self.assertEqual((ann["relationship"], ann["kind"], ann["priority"], ann["preferred_channel"], ann["preferred_ask"]),
                         ("warm", "former_colleague", "first", "whatsapp", "introduction"))
        self.assertIn("Your note (14 Oct 2026): Worked together 2016", ann["known_context"])
        self.assertIn("Example Corp: check confidentiality and non-solicitation obligations before contact.", ann["known_context"])
        bob = people["Bob Desk"]
        self.assertEqual((bob["relationship"], bob["kind"], bob["priority"], bob["preferred_ask"]), ("active", "recruiter", "next", "availability"))
        self.assertEqual(bob["linkedin"]["observed_company"], "Acme Recruitment")
        card = self.conn.execute("SELECT resolution FROM review_queue WHERE resolution LIKE ?", (f"%{bob['id']}",)).fetchone()
        self.assertEqual(card[0], f"accept:{bob['id']}")
        self.assertEqual(people["Ivy Offline"]["relationship"], "dormant")
        self.assertEqual(people["Eve Tagged"]["relationship"], "do_not_contact")
        # Applied by the agent from the owner's list: never shown as contact activity, and provenance is kept.
        self.assertEqual(ann["last_activity"], activity[ann["id"]])
        self.assertEqual(bob["last_activity"], "")
        actors = {r[0] for r in self.conn.execute("SELECT DISTINCT actor FROM state_events WHERE id>? AND entity_id IN (?,?)",
                                                  (last_event, ann["id"], bob["id"]))}
        self.assertEqual(actors - {"source"}, {"agent"})
        self.assertTrue(self.conn.execute("SELECT 1 FROM state_events WHERE entity_id=? AND field='relationship' AND note=?",
                                          (ann["id"], "The owner's list, 14 Oct 2026")).fetchone())
        again = nl.import_list(self.conn, dict(doc, rows=[r for r in doc["rows"] if r.get("n")]))
        self.assertTrue(again["saved"])
        self.assertEqual(again["changed"], 0)  # a repeated list changes nothing
        readded = nl.import_list(self.conn, doc)  # an added person, once on the site, needs a merge-or-new ruling
        self.assertEqual((readded["saved"], len(readded["problems"])), (False, 1))
        self.assertEqual(people["Ann Endorser"]["known_context"], store.get(self.conn, ann["id"])["known_context"])

    def test_a_placeholder_employer_creates_no_company(self):
        store.create(self.conn, dict(type="company", name="Stealth (Fintech)"))  # an earlier import made one
        self.conn.commit()
        quiet = nl._linkedin(person("ivy", "Ivy Founding", "Stealth (Fintech)", "Co-Founder"))
        doc = dict(source="t", export="2026-09-21", made="2026-11-10", rows=[dict(
            n=None, line=None, name="Ivy Founding", url=u("ivy"), record_id=None, note="", former_employer=False,
            linkedin=quiet, kind="former_colleague")])
        self.assertTrue(nl.import_list(self.conn, doc, actor="agent")["saved"])
        names = [r[0] for r in self.conn.execute("SELECT name FROM records WHERE type IN ('company','agency')")]
        self.assertEqual(names.count("Stealth (Fintech)"), 1)
        ivy = self.conn.execute("SELECT id FROM records WHERE url=?", (u("ivy"),)).fetchone()[0]
        self.assertFalse(self.conn.execute("SELECT 1 FROM links WHERE from_id=? OR to_id=?", (ivy, ivy)).fetchone())
        self.assertEqual(self.conn.execute("SELECT kind FROM records WHERE url=?", (u("ivy"),)).fetchone()[0], "former_colleague")

    def test_one_bad_row_leaves_everything_as_it_was(self):
        before = self.conn.execute("SELECT COUNT(*),SUM(revision) FROM records").fetchone()[:]
        queued = self.conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        doc = self.doc(self.row("bob", relationship="active", priority="next"),
                       self.row("dan", relationship="warm", priority="first"),  # turned down on 24 September
                       dict(n=None, line=9, name="Gus Listed", url="", record_id=None, note="", former_employer=False, linkedin=None,
                            relationship="warm"))  # a name already on the site, with no ruling
        report = nl.import_list(self.conn, doc)
        self.assertFalse(report["saved"])
        self.assertEqual(len(report["problems"]), 2)
        self.assertTrue(report["problems"][0].startswith("row 1: turned down"))
        self.assertIn("merge into them, or add as new", report["problems"][1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*),SUM(revision) FROM records").fetchone()[:], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0], queued)
        self.assertNotIn("Gus", json.dumps(report))

    def test_a_dry_run_reports_and_changes_nothing(self):
        report = nl.import_list(self.conn, self.doc(self.row("bob", relationship="active", priority="next")), dry_run=True)
        self.assertEqual((report["saved"], report["dry_run"], report["accepted"], report["problems"]), (False, True, 1, []))
        self.assertFalse(self.conn.execute("SELECT 1 FROM records WHERE name='Bob Desk'").fetchone())

    def test_a_ruling_merges_a_name_match_or_adds_as_new(self):
        doc = self.doc(dict(n=None, line=9, name="Gus Listed", url="", record_id=None, note="", former_employer=False, linkedin=None,
                            relationship="warm", merge_into=self.gus["id"]))
        self.assertTrue(nl.import_list(self.conn, doc)["saved"])
        self.assertEqual(store.get(self.conn, self.gus["id"])["relationship"], "warm")
        doc["rows"][0].pop("merge_into")
        doc["rows"][0]["as_new"] = True
        self.assertEqual(nl.import_list(self.conn, doc)["created"], 1)

    def test_files_with_message_text_or_undeclared_values_are_refused(self):
        bad = self.row("bob", relationship="active")
        bad["linkedin"]["evidence"]["threads"]["conversation_ids"] = ["c1"]
        for doc in (self.doc(bad), self.doc(self.row("bob", relationship="best friend")),
                    self.doc(dict(self.row("bob"), body="hello")), dict(rows="x")):
            with self.assertRaises(ValueError):
                nl.import_list(self.conn, doc)

    def test_the_cli_reports_counts_and_fails_on_problems(self):
        path = self.dir / "list.json"
        path.write_text(json.dumps(self.doc(self.row("dan", relationship="warm"))))
        db = self.dir / "t.db"
        self.conn.commit()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wo.main(["--db", str(db), "--actor", "agent", "import-list", str(path)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["saved"])


if __name__ == "__main__":
    unittest.main()
