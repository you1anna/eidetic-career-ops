"""Former colleagues from mail headers, on synthetic people and addresses only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_colleagues as mc  # noqa: E402
import network_list as nl  # noqa: E402
import wo  # noqa: E402
import profile_rules  # noqa: E402
from test_profile_rules import EXAMPLE  # noqa: E402
from test_network_list import person, u  # noqa: E402

store = wo.store
PROFILE = dict(EXAMPLE, employers=[
    {"name": "Northwind", "domains": ["northwind.example"], "from": "2012-01", "to": "2013-12"},
    {"name": "Tailspin", "domains": ["tailspin.example", "tailspin.test"], "from": "2015-01", "to": "2015-12"}])
ME = "morgan.sample@mail.example"
THREAD = "https://mail.example/#all/thread-f:1945555039024054529"


def message(mid, when, sender, to, cc=(), names=None, thread="t1"):
    return dict(id=mid, threadId=thread, date=when, labelIds=[], sender=sender, toRecipients=list(to),
                ccRecipients=list(cc), addressDisplayNames=[dict(emailAddress=a, displayName=n) for a, n in (names or {}).items()])


def page(*messages, url=THREAD):
    threads = {}
    for m in messages:
        threads.setdefault(m["threadId"], dict(id=m["threadId"], messageCount=0, viewUrl=url, messages=[]))["messages"].append(m)
    return dict(employer="northwind", query="", page=1, threads=list(threads.values()))


SNAPSHOT = dict(export=dict(folder="2026-09-21"), people={p["url"]: p for p in [
    person("pat", "Pat Quiet", "Stealth", "Co-Founder", connected="2012-04-28"),
    person("lee", "Lee Group", "Elsewhere", "Engineer"),
    person("sam-a", "Sam Twin", "One", "Engineer"),
    person("sam-b", "Sam Twin", "Two", "Engineer"),
    person("ola", "Ola Late", "Later", "Engineer"),
    person("cat", "Cat Current", "Example Corp", "Engineering Manager", flags=["former_employer"]),
    person("dan", "Dan Declined", "Elsewhere", "CTO"),
    person("kim", "Kim Known", "Harbour", "Recruiter"),
]})

STAFF = [f"{x}@northwind.example" for x in ("pat.quiet", "lee.group", "sam.twin", "cat.current", "dan.declined",
                                              "kim.known", "ian", "morgan.sample")]
MAIL = [
    # Pat wrote to the owner directly before he joined and he replied: a direct exchange both ways.
    message("1b00000000000101", "2011-11-17T09:37:36Z", "pat.quiet@northwind.example", [ME], names={"pat.quiet@northwind.example": "Pat Quiet"}),
    message("1b00000000000102", "2011-11-17T17:22:20Z", ME, ["pat.quiet@northwind.example"]),
    # The all-staff list during his months there, with the owner's own work address on it.
    message("1b00000000000103", "2012-07-03T12:53:36Z", "1hr.team@northwind.example", STAFF, cc=[ME], thread="t2",
            names={"1hr.team@northwind.example": "Northwind People"}),
    # Seen only after the owner left: not evidence they overlapped.
    message("1a00000000000001", "2016-05-01T10:00:00Z", "ola.late@northwind.example", [ME], thread="t3"),
    # An unrelated address.
    message("1a00000000000002", "2012-08-01T10:00:00Z", "friend@other.example", [ME], thread="t4"),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.rules = self.enterContext(profile_rules.override(PROFILE))
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "raw").mkdir()
        (self.dir / "raw/northwind-1.json").write_text(json.dumps(page(*MAIL)))
        # A page saved twice must not count its messages twice.
        (self.dir / "raw/northwind-2.json").write_text(json.dumps(page(MAIL[2])))

    def tearDown(self):
        self.tmp.cleanup()

    def doc(self):
        return mc.evidence([self.dir / "raw"], SNAPSHOT, self.rules)


class NameTests(Base):
    def test_person_keys_fold_titles_accents_hyphens_and_credentials(self):
        self.assertEqual(mc.person_key("Dr Andie", "Carver-Lowe, MBA"), "andiecarverlowe")
        self.assertEqual(mc.person_key("Zoë", "Ó Briain"), "zoebriain")  # the surname's last word, on both sides
        self.assertEqual(mc.person_key("", "Solo"), "")

    def test_address_keys_use_display_names_and_first_last_local_parts_only(self):
        self.assertEqual(mc.address_keys("pat.quiet@x.example"), {"patquiet"})
        self.assertEqual(mc.address_keys("1remy.fevre@x.example"), {"remyfevre"})
        self.assertEqual(mc.address_keys("ian@x.example"), set())
        self.assertEqual(mc.address_keys("ian@x.example", "Ian Cooper"), {"iancooper"})
        self.assertEqual(mc.address_keys("p@x.example", "Quiet, Pat"), {"patquiet"})
        self.assertEqual(mc.address_keys("p@x.example", "p@x.example"), set())


class EvidenceTests(Base):
    def test_only_unique_matches_within_his_months_there_count(self):
        doc = self.doc()
        by = {p["name"]: p for p in doc["people"]}
        self.assertEqual(sorted(by), ["Cat Current", "Dan Declined", "Kim Known", "Lee Group", "Pat Quiet"])
        self.assertEqual(doc["report"]["ambiguous"], 1)       # two connections called Sam Twin
        self.assertEqual(doc["report"]["outside_months"], 1)  # Ola, seen only after he left
        facts = mc.addresses(mc.read_pages(self.dir / "raw"), self.rules)
        self.assertNotIn("morgan.sample@northwind.example", facts)  # his own work address is not a colleague
        self.assertNotIn("friend@other.example", facts)  # not at a declared employer
        pat = by["Pat Quiet"]["employers"]["Northwind"]
        self.assertEqual(pat["addresses"], ["pat.quiet@northwind.example"])
        self.assertEqual((pat["first"][:10], pat["last"][:10], pat["threads"]), ("2011-11-17", "2012-07-03", 2))
        self.assertEqual([(d["direction"], d["at"][:10]) for d in pat["direct"]], [("in", "2011-11-17"), ("out", "2011-11-17")])
        self.assertEqual(pat["direct"][0]["link"], THREAD + "|msg-f:1945555039024054529")
        self.assertEqual(pat["direct"][1]["link"], THREAD + "|msg-f:1945555039024054530")
        self.assertEqual(by["Lee Group"]["employers"]["Northwind"]["direct"], [])  # group mail is not direct

    def test_threads_held_by_the_legal_screen_are_not_evidence_of_anything(self):
        doc = mc.evidence([self.dir / "raw"], SNAPSHOT, self.rules, held={"t1"})
        pat = next(p for p in doc["people"] if p["name"] == "Pat Quiet")["employers"]["Northwind"]
        self.assertEqual((pat["direct"], pat["threads"]), ([], 1))  # only the all-staff thread is left
        self.assertEqual(doc["report"]["held_messages"], 2)

    def test_a_message_to_owner_and_someone_else_is_not_direct(self):
        (self.dir / "raw/northwind-3.json").write_text(json.dumps(page(message(
            "1b00000000000001", "2013-01-01T00:00:00Z", "lee.group@northwind.example", [ME, "pat.quiet@northwind.example"], thread="t9"))))
        by = {p["name"]: p for p in self.doc()["people"]}
        self.assertEqual(by["Lee Group"]["employers"]["Northwind"]["direct"], [])

    def test_partial_copies_of_one_message_merge_their_recipients_and_names(self):
        # A later page may hold only some recipients of a message an earlier page saved.
        part = message("1b00000000000103", "2012-07-03T12:53:36Z", "1hr.team@northwind.example", ["sam@northwind.example"],
                       thread="t2", names={"sam@northwind.example": "Sam Twin"})
        (self.dir / "raw/a-first.json").write_text(json.dumps(page(part)))
        merged = next(m for m in mc.read_pages(self.dir / "raw") if m["id"] == "1b00000000000103")
        self.assertIn("pat.quiet@northwind.example", merged["toRecipients"])
        self.assertIn("sam@northwind.example", merged["toRecipients"])
        self.assertIn("Sam Twin", [x["displayName"] for x in merged["addressDisplayNames"]])

    def test_older_pages_with_only_internal_dates_read_the_same(self):
        old = dict(MAIL[2], internalDate="1341320016000")
        del old["date"]
        (self.dir / "raw/northwind-2.json").write_text(json.dumps(page(old)))
        (self.dir / "raw/northwind-1.json").write_text(json.dumps(page(*MAIL[:2], *MAIL[3:])))
        by = {p["name"]: p for p in self.doc()["people"]}
        self.assertEqual(by["Lee Group"]["employers"]["Northwind"]["first"], "2012-07-03T12:53:36Z")

    def test_the_cli_writes_evidence_and_prints_counts_not_names(self):
        snap = self.dir / "snap.json"
        snap.write_text(json.dumps(SNAPSHOT))
        out = self.dir / "colleagues.json"
        with unittest.mock.patch("builtins.print") as printed:
            self.assertEqual(mc.main(["evidence", str(self.dir / "raw"), "--snapshot", str(snap), "--out", str(out)]), 0)
        self.assertNotIn("Pat", str(printed.call_args))
        self.assertEqual(len(json.loads(out.read_text())["people"]), 5)


class VariantTests(Base):
    def snapshot(self, *people):
        return dict(export=dict(folder="2026-09-21"), people={p["url"]: p for p in people})

    def test_familiar_first_names_and_a_later_double_barrelled_surname_still_match_uniquely(self):
        facts = {a: dict(address=a, employer="Northwind", names={n} if n else set(), first="2012-07-01", last="2012-07-01",
                         threads={"t"}, during=True, direct=[])
                 for a, n in (("philip.oakden@northwind.example", ""), ("yiannis@northwind.example", "Ioannis Vell"),
                              ("andie.carver@northwind.example", "Andie Carver"), ("perry.wold@northwind.example", ""))}
        snap = self.snapshot(person("phil", "Phil Oakden"), person("ioa", "Yiannis Vell"),
                             dict(person("andie", "Andie X"), first_name="Dr Andie", last_name="Carver-Lowe"),
                             person("per", "Peregrine Wold"))
        found, report = mc.match(facts, snap)
        self.assertEqual(sorted(e["name"] for e in found.values()),
                         ["Andie X", "Peregrine Wold", "Phil Oakden", "Yiannis Vell"])
        self.assertEqual(report["ambiguous"], 0)

    def test_a_familiar_form_that_fits_two_connections_is_left_out(self):
        facts = {"chris.lee@northwind.example": dict(address="chris.lee@northwind.example", employer="Northwind", names=set(),
                                                   first="2012-07-01", last="2012-07-01", threads={"t"}, during=True, direct=[])}
        found, report = mc.match(facts, self.snapshot(person("christopher", "Christopher Lee"), person("christine", "Christine Lee")))
        self.assertEqual((found, report["ambiguous"]), ({}, 1))

    def test_two_full_names_sharing_a_short_form_are_different_people(self):
        self.assertNotIn("christinelee", mc.person_keys("Christopher", "Lee"))
        self.assertNotIn("rajeshbadiani", mc.person_keys("Rajiv", "Badiani"))
        self.assertIn("rajbadiani", mc.person_keys("Rajiv", "Badiani"))

    def test_unrelated_first_names_with_one_surname_do_not_match(self):
        facts = {"dan.taylor@northwind.example": dict(address="dan.taylor@northwind.example", employer="Northwind", names=set(),
                                                      first="2012-07-01", last="2012-07-01", threads={"t"}, during=True, direct=[])}
        found, _ = mc.match(facts, self.snapshot(person("ellie", "Ellie Taylor")))
        self.assertEqual(found, {})


class ListTests(Base):
    def setUp(self):
        super().setUp()
        self.conn = wo.wodb.connect(self.dir / "t.db")
        wo.wodb.migrate(self.conn)
        self.kim = store.create(self.conn, dict(type="person", name="Kim Known", url=u("kim")))["record"]
        self.kim = store.transition(self.conn, self.kim["id"], dict(field="kind", to="recruiter", revision=self.kim["revision"]))["record"]
        self.lee = store.create(self.conn, dict(type="person", name="Lee Group", url=u("lee")))["record"]
        card, extra = nl.linkedin_export.card(self.conn, SNAPSHOT["people"][u("dan")], [], "2026-09-21")
        qid, _ = store.linkedin_card(self.conn, card, source="LinkedIn export 2026-09-21", extra=extra)
        store.resolve_review(self.conn, qid, dict(resolution="reject", revision=1))
        self.conn.commit()
        self.conn.row_factory = wo.sqlite3.Row

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_rows_add_new_colleagues_keep_his_kinds_and_leave_out_former_employer_turned_down_and_never(self):
        out, report = mc.list_rows(self.doc(), SNAPSHOT, self.conn, never={nl.canonical(u("pat"))})
        rows = {r["name"]: r for r in out["rows"]}
        self.assertEqual(sorted(rows), ["Kim Known", "Lee Group"])
        self.assertNotIn("kind", rows["Kim Known"])  # the owner's recruiter tag stays
        self.assertEqual((rows["Lee Group"]["kind"], rows["Lee Group"]["record_id"]), ("former_colleague", self.lee["id"]))
        self.assertEqual((report["former_employer"], report["turned_down"], report["never"]), (1, 1, 1))

        out, report = mc.list_rows(self.doc(), SNAPSHOT, self.conn)
        pat = next(r for r in out["rows"] if r["name"] == "Pat Quiet")
        self.assertEqual((pat["kind"], pat["record_id"], pat["linkedin"]["url"]), ("former_colleague", None, u("pat")))
        self.assertNotIn("priority", pat)  # on the site, not on his list
        self.assertEqual(pat["linkedin"]["company"], "")  # "Stealth" is a placeholder: no company record from it
        self.assertEqual(report["placeholder_employer"], 1)
        result = nl.import_list(self.conn, out, actor="agent")
        self.assertTrue(result["saved"], result)
        added = self.conn.execute("SELECT kind,priority,relationship FROM records WHERE url=?", (u("pat"),)).fetchone()
        self.assertEqual(tuple(added), ("former_colleague", None, None))
        kinds = dict(self.conn.execute("SELECT name,kind FROM records WHERE type='person'").fetchall())
        self.assertEqual((kinds["Kim Known"], kinds["Lee Group"]), ("recruiter", "former_colleague"))


class ContactTests(Base):
    def test_old_work_addresses_are_labelled_context_never_the_email_route_and_steps_repeat_safely(self):
        pat = next(p for p in self.doc()["people"] if p["name"] == "Pat Quiet")
        steps = mc.contact_steps(pat, dict(known_context="Imported LinkedIn observation.", interactions=[]))
        self.assertNotIn("email", steps["edit"])
        self.assertEqual(steps["edit"]["known_context"].splitlines(), [
            "Imported LinkedIn observation.",
            "Former colleague at Northwind (your mail): 2 threads, Nov 2011–Jul 2012; 2 direct emails.",
            "Old work address (Northwind, former employer, likely inactive; not used as a route): pat.quiet@northwind.example"])
        self.assertEqual([(i["direction"], i["summary"]) for i in steps["interactions"]], [
            ("in", "Direct email from Pat (Northwind address); header read only"),
            ("out", "You emailed Pat directly (Northwind address); header read only")])
        again = mc.contact_steps(pat, dict(known_context=steps["edit"]["known_context"],
                                           interactions=[dict(evidence_ref=i["evidence"]) for i in steps["interactions"]]))
        self.assertEqual(again, dict(edit={}, interactions=[]))

    def test_an_email_already_recorded_that_day_under_another_link_is_not_added_again(self):
        pat = next(p for p in self.doc()["people"] if p["name"] == "Pat Quiet")
        earlier = [dict(channel="email", direction="in", at="2011-11-17T09:40:00+00:00", evidence_ref="https://other/thread"),
                   dict(channel="linkedin", direction="out", at="2011-11-17T10:00:00+00:00", evidence_ref="")]
        steps = mc.contact_steps(pat, dict(known_context="", interactions=earlier))
        self.assertEqual([i["direction"] for i in steps["interactions"]], ["out"])

    def test_a_rerun_replaces_its_own_earlier_lines_instead_of_adding_new_ones(self):
        pat = next(p for p in self.doc()["people"] if p["name"] == "Pat Quiet")
        before = ("Imported LinkedIn observation.\nFormer colleague at Northwind (your mail): 9 threads, Nov 2011–Jul 2012; 2 direct emails.\n"
                  "Your note (14 Oct 2026): worked together.")
        steps = mc.contact_steps(pat, dict(known_context=before, interactions=[]))
        lines = steps["edit"]["known_context"].splitlines()
        self.assertEqual([l for l in lines if l.startswith("Former colleague at Northwind")],
                         ["Former colleague at Northwind (your mail): 2 threads, Nov 2011–Jul 2012; 2 direct emails."])
        self.assertIn("Your note (14 Oct 2026): worked together.", lines)  # his own lines stay

    def test_a_rerun_redates_an_address_line_instead_of_repeating_it(self):
        entry = dict(name="Pat Quiet", employers={}, personal=[dict(address="pat@quiet.example", last="2011-02-05T00:00:00Z", direct=True)])
        before = "Other address from your mail (direct email, Jul 2011): pat@quiet.example\nYour note: keep."
        steps = mc.contact_steps(entry, dict(known_context=before, interactions=[], email="pat@quiet.example"))
        self.assertEqual(steps["edit"]["known_context"].splitlines(),
                         ["Your note: keep.", "Other address from your mail (direct email, Feb 2011): pat@quiet.example"])

    def test_group_mail_only_says_so(self):
        lee = next(p for p in self.doc()["people"] if p["name"] == "Lee Group")
        self.assertIn("1 thread, Jul 2012; group mail only.", mc.context_lines(lee)[0])


class PersonalTests(Base):
    def setUp(self):
        super().setUp()
        (self.dir / "raw/personal-1.json").write_text(json.dumps(page(
            message("2a01", "2015-10-06T10:00:00Z", "pat@quiet.example", [ME], names={"pat@quiet.example": "Pat Quiet"}, thread="p1"),
            message("2a02", "2013-03-06T10:00:00Z", "patq@mail.example", [ME], names={"patq@mail.example": "Pat Quiet"}, thread="p2"),
            message("2a03", "2015-06-18T10:00:00Z", "leeg1@mail.example", [ME, "x@else.example"], names={"leeg1@mail.example": "Lee Group"}, thread="p3"),
            message("2a04", "2017-01-01T10:00:00Z", "notifications@code.example", [ME], names={"notifications@code.example": "Lee Group"}, thread="p4"),
            message("2a05", "2016-01-01T10:00:00Z", "lee@tailspin.example", [ME], names={"lee@tailspin.example": "Lee Group"}, thread="p5"),
            message("2a06", "2016-01-01T10:00:00Z", "someone@law.example", [ME], names={"someone@law.example": "Unrelated Person"}, thread="p6"))))

    def test_personal_addresses_come_only_from_mail_the_colleague_sent(self):
        by = {p["name"]: p for p in self.doc()["people"]}
        self.assertEqual([(x["address"], x["direct"]) for x in by["Pat Quiet"]["personal"]],
                         [("pat@quiet.example", True), ("patq@mail.example", True)])  # newest first
        self.assertEqual([(x["address"], x["direct"]) for x in by["Lee Group"]["personal"]], [("leeg1@mail.example", False)])
        self.assertNotIn("law.example", json.dumps(self.doc()))

    def test_the_newest_direct_personal_address_is_the_route_and_his_saved_email_stays(self):
        by = {p["name"]: p for p in self.doc()["people"]}
        steps = mc.contact_steps(by["Pat Quiet"], dict(known_context="", interactions=[]))
        self.assertEqual(steps["edit"]["email"], "pat@quiet.example")
        self.assertIn("Other address from your mail (direct email, Oct 2015): pat@quiet.example", steps["edit"]["known_context"])
        self.assertIn("Other address from your mail (direct email, Mar 2013): patq@mail.example", steps["edit"]["known_context"])
        kept = mc.contact_steps(by["Pat Quiet"], dict(email="saved@owner.example", known_context="", interactions=[]))
        self.assertNotIn("email", kept["edit"])
        lee = mc.contact_steps(by["Lee Group"], dict(known_context="", interactions=[]))
        self.assertNotIn("email", lee["edit"])  # group mail only: kept as context, not a route
        self.assertIn("Other address from your mail (group mail, Jun 2015): leeg1@mail.example", lee["edit"]["known_context"])


class PhoneTests(Base):
    PHONES = [dict(url=u("pat"), phone="07700 900123", kind="mobile", at="2011-11-17T09:37:36Z", link=THREAD + "|msg-f:1"),
              dict(url=u("pat"), phone="020 7946 0000", kind="office", at="2011-11-17T09:37:36Z", link=THREAD + "|msg-f:1"),
              dict(url=u("lee"), phone="+44 20 7946 0001", kind="unspecified", at="2012-07-03T12:00:00Z", link=THREAD + "|msg-f:2")]

    def entries(self):
        doc = self.doc()
        mc.attach_phones(doc, self.PHONES)
        return {p["name"]: p for p in doc["people"]}

    def test_a_signature_mobile_is_the_phone_route_dated_and_unconfirmed_and_office_lines_are_left_out(self):
        by = self.entries()
        steps = mc.contact_steps(by["Pat Quiet"], dict(known_context="", interactions=[]))
        self.assertEqual(steps["edit"]["phone"], "07700 900123")
        self.assertIn("Phone from Pat's email signature (Nov 2011, mobile; current reachability unconfirmed): 07700 900123",
                      steps["edit"]["known_context"])
        self.assertNotIn("7946 0000", steps["edit"]["known_context"])
        lee = mc.contact_steps(by["Lee Group"], dict(known_context="", interactions=[]))
        self.assertNotIn("phone", lee["edit"])  # not known to be a mobile: context only
        self.assertIn("(Jul 2012, unspecified; current reachability unconfirmed): +44 20 7946 0001", lee["edit"]["known_context"])
        kept = mc.contact_steps(by["Pat Quiet"], dict(phone="07700 900999", known_context="", interactions=[]))
        self.assertNotIn("phone", kept["edit"])  # a saved number stays

    def test_phones_for_someone_not_matched_are_refused(self):
        with self.assertRaises(ValueError):
            mc.attach_phones(self.doc(), [dict(self.PHONES[0], url=u("nobody"))])


class ProfileTests(unittest.TestCase):
    def test_an_employer_needs_domains_and_months(self):
        for bad in ({"name": "X", "domains": [], "from": "2012-01", "to": "2013-01"},
                    {"name": "X", "domains": ["x.example"], "from": "2013-01", "to": "2012-01"},
                    {"name": "X", "domains": ["x.example"], "from": "2012", "to": "2013"}):
            with self.assertRaises(ValueError):
                profile_rules.rules_from(dict(employers=[bad]))
        e = profile_rules.rules_from(dict(employers=[PROFILE["employers"][1]])).employers[0]
        self.assertTrue(e.owns("A.B@Tailspin.TEST") and e.owns("a@mail.tailspin.example"))
        self.assertFalse(e.owns("a@nottailspin.example"))
        self.assertEqual((e.during("2015-01-01"), e.during("2015-12-31"), e.during("2016-01-01")), (True, True, False))


if __name__ == "__main__":
    unittest.main()
