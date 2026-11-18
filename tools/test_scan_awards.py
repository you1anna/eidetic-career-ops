"""Commercial award signals stay factual, bounded and separate from open opportunities."""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_awards as sa

FIXTURES = Path(__file__).parent / "fixtures" / "awards"
FILTER = {"cpv_prefixes": ["722"], "keywords": ["test automation", "AI assurance"],
          "negative": ["construction"], "minimum_value_gbp": 50000}
PORTALS = """
tenders:
  filter:
    cpv_prefixes: ['722']
    keywords: [test automation, AI assurance]
    negative: [construction]
    minimum_value_gbp: 50000
commercial_awards:
  sources:
    - {source: contracts_finder, name: Contracts Finder awards, enabled: true, params: {max_pages: 99}}
    - {source: find_a_tender, name: Find a Tender awards, enabled: true}
"""


def release(provider="contracts_finder"):
    return json.loads((FIXTURES / f"award-{provider}.json").read_text())["releases"][0]


class NormaliseTests(unittest.TestCase):
    def items(self, r=None, provider="contracts_finder", today="2026-10-04"):
        return sa.normalise(provider, r or release(provider), today, FILTER)

    def test_winner_is_company_not_buyer_and_tender_complete_is_not_contract_closed(self):
        item = self.items()[0]
        row = item["current"]
        self.assertEqual(row["signal_type"], "contract_award")
        self.assertEqual(row["company"], "Example Software Partner")
        self.assertEqual(row["buyer"], "Example Council")
        self.assertEqual(row["amount"], 240000)
        self.assertEqual(row["award_date"], "2026-10-01")
        self.assertEqual(row["published"], "2026-10-03")
        self.assertTrue(row["eligible"])
        self.assertIn("award", row["headline"].lower())
        self.assertNotIn("funding", row["headline"].lower())
        self.assertIn("/Notice/00000000", row["url"])

    def test_updated_old_award_never_takes_the_notice_date_as_award_date(self):
        r = release()
        r["awards"][0]["date"] = "2022-05-11T00:00:00+01:00"
        row = self.items(r)[0]["current"]
        self.assertEqual(row["award_date"], "2022-05-11")
        self.assertEqual(row["published"], "2026-10-03")
        self.assertIn("Award dated 2022-05-11", row["summary"])
        self.assertNotIn("new", row["headline"].lower())

    def test_supplier_parties_and_contract_award_ids_are_joined_exactly(self):
        items = self.items(provider="find_a_tender")
        rows = {i["current"]["company"]: i["current"] for i in items}
        self.assertEqual(len(items), 3)
        self.assertEqual(len({i["source_id"] for i in items}), 3)
        for name in ("Example Evaluation Company", "Example Integration Company"):
            self.assertEqual(rows[name]["buyer"], "Example Public Service")
            self.assertEqual(rows[name]["amount"], 180000)
            self.assertEqual(rows[name]["contract_end"], "2027-10-14")
            self.assertEqual(rows[name]["award_date"], "")
            self.assertIn("Award date not stated", rows[name]["summary"])
        self.assertEqual(rows["Example Small Partner"]["amount"], 70000)
        self.assertEqual(rows["Example Small Partner"]["currency"], "EUR")
        self.assertEqual(rows["Example Small Partner"]["contract_start"], "2026-10-20")

    def test_identity_does_not_use_release_id_or_supplier_display_spelling(self):
        r = release()
        before = self.items(r)[0]["source_id"]
        r["id"] = "a-new-release-id"
        r["awards"][0]["suppliers"][0]["name"] = "Changed display name"
        self.assertEqual(before, self.items(r)[0]["source_id"])
        self.assertIn("ocds-test-software-recovery", before)
        r["awards"][0]["suppliers"] = [{"name": "  Example   SMALL partner "}]
        name_id = self.items(r)[0]["source_id"]
        r["awards"][0]["suppliers"][0]["name"] = "example small PARTNER"
        self.assertEqual(name_id, self.items(r)[0]["source_id"])

    def test_no_borrowing_tender_budget_missing_facts_remain_visible(self):
        r = release()
        for key in ("value", "date", "contractPeriod"):
            r["awards"][0].pop(key)
        row = self.items(r)[0]["current"]
        self.assertEqual(row["amount"], "")
        self.assertEqual(row["currency"], "")
        self.assertEqual(row["contract_end"], "")
        self.assertEqual(row["award_date"], "")
        self.assertTrue(row["eligible"])
        self.assertIn("award date", row["summary"].lower())

    def test_expired_and_cancelled_awards_are_explicitly_ineligible(self):
        r = release()
        r["awards"][0]["contractPeriod"]["endDate"] = "2026-10-03"
        item = self.items(r)[0]
        self.assertFalse(item["current"]["eligible"])
        self.assertIn("expired", item["current"]["eligibility_reason"])
        self.assertIsNotNone(item["explicit_closure"])
        r = release()
        r["awards"][0]["status"] = "cancelled"
        self.assertFalse(self.items(r)[0]["current"]["eligible"])
        r = release("find_a_tender")
        r["contracts"][0]["status"] = "terminated"
        self.assertFalse(self.items(r, "find_a_tender")[0]["current"]["eligible"])

    def test_nonmatching_missing_identity_and_invalid_dates(self):
        r = release()
        r["tender"] = {"title": "Office furniture", "description": "Desks"}
        self.assertFalse(self.items(r)[0]["current"]["eligible"])
        r["ocid"] = ""
        self.assertEqual(self.items(r), [])
        r = release()
        r["awards"][0]["date"] = "not-a-date"
        r["awards"][0]["contractPeriod"]["endDate"] = "2026-99-99"
        self.assertEqual(self.items(r)[0]["current"]["award_date"], "")
        self.assertEqual(self.items(r)[0]["current"]["contract_end"], "")

    def test_broad_management_classification_needs_software_text_evidence(self):
        r = release()
        r["tender"] = {"title": "Financial consultancy", "description": "Business management advice",
                       "classification": {"id": "79400000"}}
        filters = {**FILTER, "cpv_prefixes": ["722", "7940"]}
        self.assertFalse(sa.normalise("contracts_finder", r, "2026-10-04", filters)[0]["current"]["eligible"])
        r["tender"]["description"] = "AI assurance for an existing software workflow"
        self.assertTrue(sa.normalise("contracts_finder", r, "2026-10-04", filters)[0]["current"]["eligible"])

    def test_multiple_contract_values_are_not_summed_into_an_invented_award_value(self):
        r = release("find_a_tender")
        r["contracts"].append({"id": "contract-extra", "awardID": "award-2", "status": "active",
                               "value": {"amount": 150000, "currency": "GBP"}})
        row = self.items(r, "find_a_tender")[0]["current"]
        self.assertEqual(row["amount"], "")
        self.assertEqual(row["contract_end"], "")

    def test_generic_testing_and_wrong_cpv_labels_do_not_create_software_signals(self):
        for title, description in [
            ("Portable Appliance Testing", "Electrical equipment testing and repair"),
            ("Dental and sight testing", "Clinical testing in educational settings"),
            ("Medical sterilisers", "Supply, validation and testing of sterilising equipment"),
            ("Victim support service", "Suppliers provide the support service deliverables"),
            ("Temporary project manager", "Organisational development and change"),
            ("Fire protection", "Delivery partner for building fire protection works"),
        ]:
            with self.subTest(title=title):
                r = release()
                r["tender"].update(title=title, description=description)
                row = self.items(r)[0]["current"]
                self.assertFalse(row["eligible"])
                self.assertEqual(row["eligibility_reason"], "no-software-delivery-context")

    def test_licence_and_hardware_supply_need_an_actual_delivery_workstream(self):
        for title, description in [
            ("Case management software", "Provision of a cloud-hosted legal case management system"),
            ("Enterprise SaaS", "Provision of software as a service via an approved framework"),
            ("Enterprise SaaS", "Software as a service via an Integrated Health Economy Framework"),
            ("Cluster replacement", "Replacement and support services for an existing HPC cluster"),
            ("Firewall support", "Support and maintenance for an existing firewall"),
            ("Building Management System maintenance", "Controllers, field devices, software support and system recovery"),
            ("Learning management software", "Renew existing platform for workforce development, avoiding data migration and implementation risks"),
        ]:
            with self.subTest(title=title):
                r = release()
                r["tender"].update(title=title, description=description)
                self.assertFalse(self.items(r)[0]["current"]["eligible"])

    def test_current_software_delivery_evidence_survives_narrower_gate(self):
        for title, description in [
            ("CRM improvements", "Integrate the existing Salesforce and finance systems"),
            ("Digital delivery and test support", "Review the digital test strategy and delivery plan"),
            ("Finance platform", "Implementation and integration of a modern cloud-based finance solution"),
            ("FP&A consultancy", "Configuration, implementation, testing and deployment of the existing Unit4 solution"),
            ("Application support", "Business-critical applications with Development, Test, Disaster Recovery and Production services"),
        ]:
            with self.subTest(title=title):
                r = release()
                r["tender"].update(title=title, description=description)
                self.assertTrue(self.items(r)[0]["current"]["eligible"])

    def test_ageing_without_source_change_does_not_change_stored_evidence(self):
        self.assertEqual(self.items(today="2026-12-29"), self.items(today="2027-01-01"))

    def test_mixed_lot_awards_do_not_borrow_another_suppliers_software_scope(self):
        r = release("find_a_tender")
        r["tender"]["lots"] = [
            {"id": "software", "title": "Software integration", "description": "Implementation and integration of an existing digital application"},
            {"id": "hardware", "title": "Computer equipment", "description": "Provision of computers and peripherals"},
        ]
        r["awards"][0]["relatedLots"] = ["software"]
        r["awards"][1]["relatedLots"] = ["hardware"]
        rows = {i["current"]["company"]: i["current"] for i in self.items(r, "find_a_tender")}
        self.assertTrue(rows["Example Evaluation Company"]["eligible"])
        self.assertFalse(rows["Example Small Partner"]["eligible"])
        self.assertEqual(rows["Example Small Partner"]["title"], "Computer equipment")
        self.assertNotIn("AI workflow", rows["Example Small Partner"]["summary"])

    def test_single_lot_can_keep_unambiguous_main_scope_and_title(self):
        r = release("find_a_tender")
        r["tender"].update(title="CRM delivery", description="Digital data capture and software integration",
                           lots=[{"id": "1", "description": "Direct award via an existing framework"}])
        r["awards"][0]["relatedLots"] = ["1"]
        row = self.items(r, "find_a_tender")[0]["current"]
        self.assertTrue(row["eligible"])
        self.assertEqual(row["title"], "CRM delivery")
        self.assertIn("Digital data capture", row["summary"])


class FetchTests(unittest.TestCase):
    def test_hard_page_limit_and_presence_of_next_link_mean_partial(self):
        payload = {"releases": [release()], "links": {"next": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=next"}}
        with patch.object(sa, "fetch_json", return_value=payload) as fetch:
            result = sa.fetch_source("contracts_finder", {"max_pages": 999, "stages": "tender", "page_size": 999}, "2026-09-27", "2026-10-04")
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(result["_complete"])
        self.assertIn("stages=award", fetch.call_args_list[0].args[0])
        self.assertIn("limit=100", fetch.call_args_list[0].args[0])

    def test_second_page_error_retains_first_page_as_partial(self):
        payload = {"releases": [release()], "links": {"next": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=next"}}
        with patch.object(sa, "fetch_json", side_effect=[payload, urllib.error.HTTPError("https://example.test", 503, "unavailable", {}, None)]):
            result = sa.fetch_source("contracts_finder", {}, "2026-09-27", "2026-10-04")
        self.assertEqual(len(result["releases"]), 1)
        self.assertFalse(result["_complete"])
        self.assertEqual(result["_error"]["status"], 503)

    def test_untrusted_cursor_is_not_followed(self):
        payload = {"releases": [release()], "links": {"next": "https://example.test/wrong-source"}}
        with patch.object(sa, "fetch_json", return_value=payload) as fetch:
            result = sa.fetch_source("contracts_finder", {}, "2026-09-27", "2026-10-04")
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(result["_complete"])
        self.assertIn("cursor", result["_error"]["error"])


class RunTests(unittest.TestCase):
    def setUp(self):
        import collection_schedule
        session=collection_schedule.Session
        schedule_patch=patch.object(collection_schedule,'Session',side_effect=lambda data,offline=False:session(data,offline=True))
        schedule_patch.start();self.addCleanup(schedule_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "portals.yml"
        self.cfg.write_text(PORTALS)

    def obs(self):
        return json.loads((self.root / "pending/observations.json").read_text())

    def run_scan(self, today="2026-10-04"):
        return sa.run(self.cfg, self.root, today, FIXTURES)

    def test_repeat_is_idempotent_and_only_observations_are_written(self):
        first, again = self.run_scan(), self.run_scan()
        self.assertEqual(first["totals"]["new"], 4)
        self.assertEqual(again["totals"]["new"], 0)
        obs = self.obs()
        self.assertEqual(len(obs["items"]), 4)
        self.assertEqual({i["kind"] for i in obs["items"].values()}, {"commercial_signal"})
        self.assertEqual(set(obs["sources"]), {"award:contracts_finder", "award:find_a_tender"})
        self.assertTrue((self.root / "pending/awards/last-run.json").exists())
        self.assertEqual(list(self.root.rglob("*.tsv")), [])
        self.assertEqual(first["window"], ["2026-09-27", "2026-10-04"])

    def test_two_day_rehearsal_import_keeps_unchanged_award_evidence_once(self):
        import record_imports
        conn = record_imports.wodb.connect(self.root / "test.db")
        self.addCleanup(conn.close)
        record_imports.wodb.migrate(conn)
        self.run_scan("2026-10-04")
        record_imports.cmd_import_scans(conn, self.root / "pending")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 4)
        first_sources = {i["source_id"]: i["current"]["source_url"] for i in self.obs()["items"].values()}
        self.run_scan("2026-10-05")
        record_imports.cmd_import_scans(conn, self.root / "pending")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 4)
        self.assertEqual(first_sources, {i["source_id"]: i["current"]["source_url"] for i in self.obs()["items"].values()})

    def test_duplicate_releases_keep_latest_facts_even_when_feed_is_newest_first(self):
        old, new = release(), release()
        new["id"] = "cf-release-2"
        new["date"] = "2026-10-04T09:00:00Z"
        new["awards"][0]["value"]["amount"] = 250000
        with patch.object(sa, "fetch_source", return_value={"releases": [new, old], "_pages": 1, "_complete": True}):
            sa.run(self.cfg, self.root, "2026-10-04")
        cf = [i for i in self.obs()["items"].values() if i["provider"] == "contracts_finder"]
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["current"]["amount"], 250000)

    def test_release_order_uses_utc_instead_of_offset_spelling(self):
        older, newer = release(), release()
        older["date"] = "2026-10-04T10:00:00+02:00"
        newer["date"] = "2026-10-04T09:00:00Z"
        newer["awards"][0]["value"]["amount"] = 250000
        with patch.object(sa, "fetch_source", return_value={"releases": [newer, older], "_pages": 1, "_complete": True}):
            sa.run(self.cfg, self.root, "2026-10-04")
        self.assertEqual(next(iter(self.obs()["items"].values()))["current"]["amount"], 250000)

    def test_empty_window_never_closes_previously_seen_award(self):
        self.run_scan()
        with patch.object(sa, "fetch_source", return_value={"releases": [], "_pages": 1, "_complete": True}):
            sa.run(self.cfg, self.root, "2026-10-05")
        for i in self.obs()["items"].values():
            self.assertEqual(i["source_lifecycle"], "active")
            self.assertEqual(i["absence_observations"], [])

    def test_errors_are_isolated_and_partial_results_importable_but_not_complete(self):
        partial = {"releases": [release("find_a_tender")], "_pages": 1, "_complete": False, "_error": {"kind": "timeout", "error": "timeout"}}
        with patch.object(sa, "fetch_source", side_effect=[TimeoutError("timeout"), partial]):
            status = sa.run(self.cfg, self.root, "2026-10-04")
        self.assertEqual(status["sources"]["Contracts Finder awards"]["state"], "error")
        self.assertEqual(status["sources"]["Find a Tender awards"]["state"], "partial")
        self.assertEqual(len(self.obs()["items"]), 3)
        self.assertFalse(self.obs()["sources"]["award:find_a_tender"]["complete"])

    def test_nonmatching_noise_is_not_persisted_but_known_closure_is(self):
        self.run_scan()
        irrelevant = release()
        irrelevant["ocid"] = "noise"
        irrelevant["tender"] = {"title": "Office chairs", "description": "Furniture"}
        closed = release()
        closed["awards"][0]["status"] = "cancelled"
        with patch.object(sa, "fetch_source", return_value={"releases": [irrelevant, closed], "_pages": 1, "_complete": True}):
            sa.run(self.cfg, self.root, "2026-10-05")
        self.assertEqual(len(self.obs()["items"]), 4)
        cf = [i for i in self.obs()["items"].values() if i["provider"] == "contracts_finder"]
        self.assertEqual(cf[0]["source_lifecycle"], "closed")

    def test_missing_or_disabled_configuration_cannot_fetch(self):
        for text in ("{}", PORTALS.replace("commercial_awards:\n", "commercial_awards:\n  enabled: false\n")):
            self.cfg.write_text(text)
            with patch.object(sa, "fetch_source") as fetch:
                sa.run(self.cfg, self.root, "2026-10-04")
            fetch.assert_not_called()

    def test_cli_page_cap_does_not_raise_a_daily_failure_but_partial_error_does(self):
        import record_imports
        import failure_tasks
        conn = record_imports.wodb.connect(self.root / "failures.db")
        self.addCleanup(conn.close)
        record_imports.wodb.migrate(conn)
        for error in (None, {"error": "later page unavailable", "kind": "server", "status": 503}):
            partial = {"state": "partial", "complete": False, "source": "contracts_finder", **(error or {})}
            report = {"sources": {"Contracts Finder awards": partial}}
            with patch.object(sa, "run", return_value=report), contextlib.redirect_stdout(io.StringIO()):
                code = sa.main([])
            self.assertEqual(code, int(error is not None))
            receipt = {"started_at": "2026-10-04T00:00:00Z", "sources": {"award:contracts_finder": partial},
                       "adapters": {"contract awards": {"returncode": code}}}
            result = failure_tasks.for_ingest(conn, receipt, [])
            if error:
                self.assertTrue(result["raised"])
            else:
                self.assertEqual(result["raised"], [])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='failure'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
