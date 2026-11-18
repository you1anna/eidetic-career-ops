import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_tenders as st  # noqa: E402

PORTALS = """
tenders:
  filter:
    cpv_prefixes: ["722", "7254"]
    keywords: ["test", "quality assurance", "delivery partner"]
    negative: ["cleaning", "catering"]
    minimum_value_gbp: 50000
  sources:
    - {name: Contracts Finder, source: contracts_finder, enabled: true, params: {}}
    - {name: Find a Tender, source: find_a_tender, enabled: false, params: {}}
    - {name: Nonsense, source: ojeu, enabled: true, params: {}}
"""


def release(ocid, title, *, cpv="72200000", amount=250000, deadline="2026-10-30",
            desc="Software delivery and assurance.", buyer="Leeds City Council",
            date="2026-09-15T00:00:00Z", status="active"):
    return {"ocid": ocid, "date": date, "tag": ["tender"],
            "buyer": {"name": buyer},
            "tender": {"title": title, "description": desc, "status": status,
                       "value": {"amount": amount, "currency": "GBP"},
                       "tenderPeriod": {"endDate": f"{deadline}T17:00:00Z"},
                       "classification": {"scheme": "CPV", "id": cpv, "description": "IT services"},
                       "contactPoint": {"name": "Procurement", "email": "proc@leeds.gov.uk"}}}


# Contracts Finder has shipped both wrappers; the reader must not depend on one of them.
PACKAGE_SHAPE = {"hitCount": 2, "results": [
    {"releases": [release("ocds-1", "Test automation and quality assurance services")]},
    {"releases": [release("ocds-2", "Digital delivery partner framework", cpv="72240000")]},
]}
BARE_SHAPE = {"releases": [release("ocds-3", "Software testing services")]}


def read_rows(path):
    lines = path.read_text().splitlines()
    cols = lines[0].split("\t")
    return [dict(zip(cols, line.split("\t"))) for line in lines[1:]]


class TenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.portals = root / "portals.yml"
        self.portals.write_text(PORTALS)
        self.fx = root / "fx"
        self.fx.mkdir()
        self.fx_file = self.fx / "tender-contracts_finder.json"
        self.fx_file.write_text(json.dumps(PACKAGE_SHAPE))
        self.out = root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_ocds_wrappers_flatten(self):
        self.assertEqual([r["ocid"] for r in st.releases_of(PACKAGE_SHAPE)], ["ocds-1", "ocds-2"])
        self.assertEqual([r["ocid"] for r in st.releases_of(BARE_SHAPE)], ["ocds-3"])
        self.assertEqual(st.releases_of({}), [])
        self.assertEqual(st.releases_of({"results": [None, "junk"]}), [])

    def test_normalise_keeps_the_buyer_budget_and_deadline(self):
        row = st.normalise("contracts_finder", release("ocds-9", "Test management services"))
        self.assertEqual(row["buyer"], "Leeds City Council")
        self.assertEqual((row["value_amount"], row["value_currency"]), ("250000", "GBP"))
        self.assertEqual(row["deadline"], "2026-10-30")
        self.assertEqual(row["published"], "2026-09-15")
        self.assertEqual(row["cpv"], "72200000")
        self.assertEqual(row["contact_email"], "proc@leeds.gov.uk")
        self.assertEqual(row["url"], "https://www.contractsfinder.service.gov.uk/Notice/ocds-9")

    def test_an_unstated_value_is_an_unknown_not_a_rejection(self):
        """A notice with no published budget is still a buying signal; it must not be filtered out
        by a floor it cannot be compared against."""
        bare = release("ocds-10", "Software testing services", amount=None)
        bare["tender"]["value"] = {}
        row = st.normalise("contracts_finder", bare)
        self.assertEqual((row["value_amount"], row["value_currency"]), ("", ""))
        ok, why = st.relevant(row, {"cpv_prefixes": ["722"], "minimum_value_gbp": 50000})
        self.assertTrue(ok)
        self.assertIn("cpv:722", why)

    def test_relevance_reasons_and_rejections(self):
        f = {"cpv_prefixes": ["722"], "keywords": ["quality assurance"],
             "negative": ["cleaning"], "minimum_value_gbp": 50000}
        ok, why = st.relevant(st.normalise("cf", release("a", "Quality assurance services", cpv="79400000")), f)
        self.assertTrue(ok)
        self.assertIn("keyword:quality assurance", why)
        ok, why = st.relevant(st.normalise("cf", release("b", "Window cleaning", cpv="90000000")), f)
        self.assertFalse(ok)
        self.assertEqual(why, "negative:cleaning")
        ok, why = st.relevant(st.normalise("cf", release("c", "Grounds maintenance", cpv="77000000")), f)
        self.assertFalse(ok)
        self.assertEqual(why, "no-match")
        ok, why = st.relevant(st.normalise("cf", release("d", "Software testing", amount=1000)), f)
        self.assertFalse(ok)
        self.assertIn("below-value-floor", why)

    def test_a_single_word_keyword_matches_as_a_word(self):
        """'test' must not fire on 'latest' or 'contest'; a phrase stays a substring match."""
        f = {"keywords": ["test", "delivery partner"], "cpv_prefixes": []}
        hit = st.relevant(st.normalise("cf", release("a", "Test management services", cpv="90000000")), f)
        self.assertEqual(hit, (True, "keyword:test"))
        miss = st.relevant(st.normalise("cf", release("b", "Latest greatest contest", cpv="90000000",
                                                      desc="Nothing relevant here.")), f)
        self.assertEqual(miss, (False, "no-match"))
        phrase = st.relevant(st.normalise("cf", release("c", "Digital delivery partnership", cpv="90000000",
                                                        desc="Seeking a delivery partner.")), f)
        self.assertEqual(phrase[0], True)

    def test_expired_notices_are_counted_not_surfaced(self):
        self.fx_file.write_text(json.dumps({"releases": [
            release("ocds-old", "Test management services", deadline="2026-09-01"),
            release("ocds-live", "Test management services", deadline="2026-12-01"),
        ]}))
        status = st.run(self.portals, self.out, "2026-09-16", 7, self.fx)
        ocids = [r["ocid"] for r in read_rows(self.out / "pending" / "tenders" / "2026-09-16.tsv")]
        self.assertEqual(ocids, ["ocds-live"])
        self.assertEqual(status["totals"]["expired_skipped"], 1)
        self.assertEqual(status["totals"]["matched"], 2)
        observations = json.loads((self.out / "pending" / "observations.json").read_text())
        old = observations["items"]["tender:contracts_finder\x1focds-old"]
        self.assertEqual(old["source_lifecycle"], "active")
        self.assertIsNone(old["explicit_closure"])
        self.assertFalse(old["current"]["eligible"])

    def test_run_writes_rows_dedupes_and_isolates_a_bad_source(self):
        status = st.run(self.portals, self.out, "2026-09-16", 7, self.fx)
        rows = read_rows(self.out / "pending" / "tenders" / "2026-09-16.tsv")
        self.assertEqual({r["ocid"] for r in rows}, {"ocds-1", "ocds-2"})
        self.assertEqual(status["sources"]["Nonsense"]["state"], "unsupported-source")
        self.assertNotIn("Find a Tender", status["sources"])  # disabled
        self.assertEqual(status["totals"]["new"], 2)
        # A second run the next day surfaces nothing already seen.
        again = st.run(self.portals, self.out, "2026-09-17", 7, self.fx)
        self.assertEqual(again["totals"]["new"], 0)
        self.assertEqual(read_rows(self.out / "pending" / "tenders" / "2026-09-17.tsv"), [])

    def test_a_retry_preserves_what_the_first_attempt_surfaced(self):
        st.run(self.portals, self.out, "2026-09-16", 7, self.fx)
        (self.out / "pending" / "tenders" / "seen.json").write_text("[]")  # seen index lost
        status = st.run(self.portals, self.out, "2026-09-16", 7, self.fx)
        rows = read_rows(self.out / "pending" / "tenders" / "2026-09-16.tsv")
        self.assertEqual({r["ocid"] for r in rows}, {"ocds-1", "ocds-2"})
        self.assertEqual(status["totals"]["new"], 0)  # recovered from the saved TSV, not re-added

    def test_soonest_deadline_first_and_undated_last(self):
        undated = release("ocds-none", "Software testing services")
        undated["tender"]["tenderPeriod"] = {}
        self.fx_file.write_text(json.dumps({"releases": [
            undated,
            release("ocds-late", "Software testing services", deadline="2026-12-01"),
            release("ocds-soon", "Software testing services", deadline="2026-09-25"),
        ]}))
        st.run(self.portals, self.out, "2026-09-16", 7, self.fx)
        rows = read_rows(self.out / "pending" / "tenders" / "2026-09-16.tsv")
        self.assertEqual([r["ocid"] for r in rows], ["ocds-soon", "ocds-late", "ocds-none"])

    def test_truncated_paging_is_reported_partial(self):
        page = {"results": [{"releases": [release("ocds-p", "Software testing")]}],
                "links": {"next": "https://next"}}
        with patch.object(st, "fetch_json", return_value=page):
            payload = st.fetch_source("contracts_finder", {"max_pages": 2}, "2026-09-09", "2026-09-16")
        self.assertFalse(payload["_complete"])
        self.assertEqual(payload["_pages"], 2)

    def test_source_urls_carry_the_window(self):
        cf = st.source_url("contracts_finder", {}, 1, "2026-09-09", "2026-09-16")
        self.assertIn("publishedFrom=2026-09-09T00%3A00%3A00", cf)
        self.assertIn("publishedTo=2026-09-16T23%3A59%3A59", cf)
        ft = st.source_url("find_a_tender", {}, 1, "2026-09-09", "2026-09-16")
        self.assertIn("updatedFrom=2026-09-09T00%3A00%3A00", ft)
        with self.assertRaises(ValueError):
            st.source_url("nope", {}, 1, "a", "b")

    def test_probe_names_the_fields_that_moved(self):
        thin = {"results": [{"releases": [{"ocid": "x", "date": "2026-09-15",
                                           "tender": {"title": "T", "status": "active"}}]}]}
        with patch.object(st, "fetch_json", return_value=thin):
            r = st.probe("contracts_finder", {}, "2026-09-09", "2026-09-16")
        self.assertTrue(r["ok"])
        self.assertIn("buyer.name", r["optional_missing"])
        self.assertIn("tender.value.amount", r["optional_missing"])
        self.assertEqual(r["present"]["tender.title"], 1)
        self.assertEqual(r["shape"], "release-package")


if __name__ == "__main__":
    unittest.main()
