import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coverage_report as cr  # noqa: E402

CHANNELS = """
channels:
  - id: employer-ats
    name: Employer boards
    market: [permanent]
    access: api
    status: automated
    runtime: scan_boards
    measure: {provider_in: [greenhouse, ashby]}
    notes: Employer headcount only.
  - id: aggregator-contract
    name: Aggregators
    market: [agency-day-rate]
    access: api
    status: adapter-unprobed
    measure: {provider_in: [adzuna, reed]}
    notes: Needs a key and a probe.
  - id: public-tenders
    name: Tenders
    market: [direct-outside-ir35]
    access: api
    status: adapter-unprobed
    notes: No key needed.
  - id: agency-desks
    name: Agency desks
    market: [agency-day-rate]
    access: relationship
    status: manual-only
    notes: Nobody has rung them.
"""
PORTALS = """
tracked_companies:
  - {name: A, careers_url: https://job-boards.greenhouse.io/a, enabled: true}
  - {name: B, careers_url: https://jobs.ashbyhq.com/b, enabled: false}
feeds:
  - {name: Adzuna, provider: adzuna, enabled: false, params: {}}
  - {name: Reed, provider: reed, enabled: true, params: {}}
tenders:
  sources:
    - {name: CF, source: contracts_finder, enabled: false, params: {}}
search_queries:
  - {name: q1, query: x, enabled: true}
  - {name: q2, query: y, enabled: true}
"""
JOB_COLS = ["date_seen", "company", "title", "engagement", "rate", "location", "posted", "url",
            "provider", "why", "pay_min", "pay_max", "pay_currency", "pay_period", "pay_source",
            "workplace", "employment_type", "department", "ir35"]
TENDER_COLS = ["date_seen", "source", "source_key", "release_id", "ocid", "buyer", "title", "value_amount", "value_currency",
               "published", "deadline", "cpv", "cpv_label", "status", "url", "why",
               "contact_name", "contact_email", "summary"]


def tsv(cols, rows):
    return "\t".join(cols) + "\n" + "".join("\t".join(r.get(c, "") for c in cols) + "\n" for r in rows)


class CoverageTests(unittest.TestCase):
    def test_awards_have_separate_measured_yield_without_invented_contracts_or_terms(self):
        (self.base / 'channels.yml').write_text(CHANNELS + '''
  - id: awarded-suppliers
    name: Awarded suppliers
    finds: approach
    access: free
    runs: daily
    measure: {scope_prefix: 'award:'}
''')
        (self.base / 'data/pending/observations.json').write_text(json.dumps({'items': {
            'award:cf\\u001f1': {'kind': 'commercial_signal', 'current': {'eligible': True, 'amount': 500000}},
            'award:cf\\u001f2': {'kind': 'commercial_signal', 'current': {'eligible': False}},
        }}))
        rep = cr.build(self.base, self.base / 'data')
        award = next(c for c in rep['channels'] if c['id'] == 'awarded-suppliers')
        self.assertEqual(award['yield'], {'surfaced': 2, 'eligible_signals': 1})
        self.assertEqual(rep['totals']['commercial_signals'], 2)
        self.assertEqual(rep['totals']['job_candidates'], 4)
        self.assertEqual(rep['totals']['contract_shaped'], 2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = base = Path(self.tmp.name) / "workspace"
        base.mkdir(parents=True)
        (base / "channels.yml").write_text(CHANNELS)
        (base / "portals.yml").write_text(PORTALS)
        pend = base / "data/pending"
        (pend / "tenders").mkdir(parents=True)
        (pend / "2026-09-14.tsv").write_text(tsv(JOB_COLS, [
            {"url": "https://a/1", "provider": "greenhouse", "engagement": "permanent"},
            {"url": "https://a/2", "provider": "greenhouse", "engagement": "unknown"},
            {"url": "https://a/3", "provider": "ashby", "engagement": "interim", "pay_period": "day"},
        ]))
        # The same URL again the next day must not be counted twice.
        (pend / "2026-09-15.tsv").write_text(tsv(JOB_COLS, [
            {"url": "https://a/1", "provider": "greenhouse", "engagement": "permanent"},
            {"url": "https://r/9", "provider": "reed", "engagement": "contract", "pay_period": "day"},
        ]))
        (pend / "tenders" / "2026-09-15.tsv").write_text(tsv(TENDER_COLS, [
            {"source": "contracts_finder", "release_id": "r1", "ocid": "ocds-1", "deadline": "2030-01-01", "value_amount": "250000"},
            {"source": "contracts_finder", "release_id": "r2", "ocid": "ocds-2", "deadline": "", "value_amount": ""},
        ]))
        (pend / "last-run.json").write_text(json.dumps({"date": "2026-09-15"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidates_are_deduplicated_across_days(self):
        rep = cr.build(self.base, self.base / "data")
        self.assertEqual(rep["totals"]["job_candidates"], 4)  # a/1 seen twice, counted once
        self.assertEqual(rep["totals"]["by_engagement"],
                         {"contract": 1, "interim": 1, "unknown": 1, "permanent": 1})
        self.assertEqual(rep["totals"]["contract_shaped"], 2)
        self.assertEqual(rep["totals"]["with_a_day_rate"], 2)

    def test_yield_is_attributed_to_the_channel_that_found_it(self):
        by_id = {c["id"]: c for c in cr.build(self.base, self.base / "data")["channels"]}
        self.assertEqual(by_id["employer-ats"]["yield"]["surfaced"], 3)
        self.assertEqual(by_id["employer-ats"]["yield"]["contract_shaped"], 1)
        self.assertEqual(by_id["aggregator-contract"]["yield"]["surfaced"], 1)
        self.assertEqual(by_id["aggregator-contract"]["yield"]["contract_shaped"], 1)

    def test_a_channel_with_no_runtime_reports_nothing_rather_than_zero(self):
        """An unread channel is unknown, not empty. Showing it as 0 would read as 'checked, nothing
        there', which is the mistake this report exists to stop."""
        by_id = {c["id"]: c for c in cr.build(self.base, self.base / "data")["channels"]}
        self.assertEqual(by_id["agency-desks"]["yield"], {})
        rendered = cr.render(cr.build(self.base, self.base / "data"))
        row = next(l for l in rendered.splitlines() if l.startswith("| Agency desks"))
        self.assertTrue(row.rstrip().endswith("| — | — |"))

    def test_tender_yield_counts_live_notices(self):
        by_id = {c["id"]: c for c in cr.build(self.base, self.base / "data")["channels"]}
        y = by_id["public-tenders"]["yield"]
        self.assertEqual((y["surfaced"], y["with_terms"], y["live"]), (2, 1, 2))

    def test_tenders_deduplicate_by_source_scoped_release_not_ocid(self):
        pend = self.base / "data/pending/tenders"
        (pend / "2026-09-16.tsv").write_text(tsv(TENDER_COLS, [
            # update of r1: one release, despite appearing on two days
            {"source": "contracts_finder", "release_id": "r1", "ocid": "ocds-1", "value_amount": "300000"},
            # a later release in the same procurement process must remain distinct
            {"source": "contracts_finder", "release_id": "r3", "ocid": "ocds-1"},
            # the same provider-local release id at another source must remain distinct
            {"source": "find_a_tender", "release_id": "r1", "ocid": "ocds-1"},
        ]))
        rows = cr.load_tender_rows(self.base / "data")
        self.assertEqual(len(rows), 4)
        self.assertEqual(next(r for r in rows if r.get("source") == "contracts_finder" and
                              r.get("release_id") == "r1")["value_amount"], "300000")

    def test_hn_measurement_includes_only_explicitly_eligible_observations(self):
        base = self.base
        (base / "channels.yml").write_text(CHANNELS.replace(
            "  - id: agency-desks", "  - id: hn-who-is-hiring\n    name: HN hiring\n    market: [permanent]\n    access: api\n    status: manual-only\n    measure: {provider_in: [hn_algolia]}\n    notes: Manual.\n  - id: agency-desks"))
        observations = {"schema_version": 1, "items": {
            "hn:thread\u001f1": {"kind": "job", "provider": "hn_algolia",
                                  "current": {"eligible": True, "engagement": "contract", "url": "https://hn/1"}},
            "hn:thread\u001f2": {"kind": "job", "provider": "hn_algolia",
                                  "current": {"eligible": False, "engagement": "contract", "url": "https://hn/2"}},
        }}
        (base / "data/pending/observations.json").write_text(json.dumps(observations))
        rep = cr.build(self.base, self.base / "data")
        hn = next(c for c in rep["channels"] if c["id"] == "hn-who-is-hiring")
        self.assertEqual(hn["yield"]["surfaced"], 1)
        self.assertEqual(rep["totals"]["job_candidates"], 5)

    def test_only_the_given_data_directory_is_read(self):
        external = Path(self.tmp.name) / "repo-data"
        (external / "pending").mkdir(parents=True)
        (external / "pending/2026-09-21.tsv").write_text(tsv(JOB_COLS, [
            {"url": "https://only/current", "provider": "greenhouse", "engagement": "permanent"}
        ]))
        rep = cr.build(self.base, external)
        self.assertEqual(rep["totals"]["job_candidates"], 1)

    def test_config_reach_counts_what_is_actually_switched_on(self):
        c = cr.build(self.base, self.base / "data")["config"]
        self.assertEqual((c["tracked_boards_enabled"], c["tracked_boards_total"]), (1, 2))
        self.assertEqual(c["feeds_enabled"], ["Reed"])
        self.assertEqual(c["tender_sources_enabled"], [])
        self.assertEqual(c["search_queries_never_read"], 2)

    def test_render_states_the_headline_numbers(self):
        text = cr.render(cr.build(self.base, self.base / "data"))
        self.assertIn("| Job candidates ever surfaced | 4 |", text)
        self.assertIn("| …contract, interim or FTC | **2** |", text)
        self.assertIn("Nobody has rung them.", text)      # gaps section carries the note
        self.assertNotIn("Employer headcount only.", text)  # automated channels are not gaps
        self.assertIn("scanner output measured", text)
        self.assertNotIn("read daily", text)


if __name__ == "__main__":
    unittest.main()
