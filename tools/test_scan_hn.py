import json
import tempfile
import unittest
from pathlib import Path

from tools import scan_hn


class HNTests(unittest.TestCase):
    def test_apply_links_and_urls_are_not_titles(self):
        job=scan_hn.normalise('99',dict(objectID='1',parent_id=99,comment_text='Axmed | AI Engineer | REMOTE | apply: axmed.teamtailor.com/jobs/8175859-ai-engineer<p>Text'))
        self.assertEqual(job['title'],'AI Engineer')
        job=scan_hn.normalise('99',dict(objectID='2',parent_id=99,comment_text='Search Atlas | Product Engineer | Full-time contractor | https://careers.searchatlas.com/jobs/599248-product-engineer<p>Text'))
        self.assertEqual(job['title'],'Product Engineer')

    def test_offline_thread_is_archived_and_observed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            portals = root / "portals.yml"
            portals.write_text("""title_filter:\n  positive: [engineering lead]\n  negative: []
location_filter:\n  accept_any_of: [london, remote]\n  reject_any_of: [us only]
engagement_filter:\n  text_markers: [contract]\n  prefer: [contract]
hn_who_is_hiring:\n  enabled: true\n""")
            fixture = root / "hn.json"
            fixture.write_text(json.dumps({
                "thread": {"objectID": "99", "title": "Ask HN: Who is hiring?"},
                "comments": {"_complete": True, "_pages": 1, "hits": [{
                    "objectID": "101", "parent_id": 99, "created_at": "2026-09-20T10:00:00Z",
                    "comment_text": "Acme | London | Remote | contract engineering lead"}]}}))
            status = scan_hn.run(portals, root / "out", "2026-09-20", fixture)
            self.assertTrue(status["complete"])
            self.assertIn("101", (root / "out/pending/hn/2026-09-20.tsv").read_text())
            obs = json.loads((root / "out/pending/observations.json").read_text())
            self.assertIn("hn:who-is-hiring:99\x1f101", obs["items"])

    def test_early_in_a_month_last_months_thread_is_read_until_the_new_one_is_posted(self):
        from unittest.mock import patch
        september = {"objectID": "49522897", "title": "Ask HN: Who is hiring? (September 2026)"}
        with patch.object(scan_hn, "fetch_json", lambda url: {"hits": [september] if "September" in url else []}):
            self.assertEqual(scan_hn.current_thread("2026-10-01"), (september, "September 2026"))
            self.assertEqual(scan_hn.current_thread("2026-09-20"), (september, "September 2026"))
            with self.assertRaises(scan_hn.ThreadNotFound):
                scan_hn.current_thread("2026-10-12")  # a week in, a missing thread is a real failure
        with patch.object(scan_hn, "fetch_json", lambda url: {"hits": [september]}):
            with self.assertRaises(scan_hn.ThreadNotFound):
                scan_hn.find_thread("October 2026")  # September's title never stands in for October's


class EligibilityTests(unittest.TestCase):
    def test_header_preserves_location_and_decodes_entities(self):
        job=scan_hn.normalise('99',dict(objectID='1',parent_id=99,comment_text='Example | Founding Engineer | Remote (US-Based Only)<p>We&#x27;re hiring.'))
        self.assertEqual(job['company'],'Example')
        self.assertNotIn('hiring',job['title'])
        self.assertIn('US-Based Only',job['location'])
        self.assertIn("We're",job['summary'])
    def test_us_only_and_overseas_onsite_never_count_as_uk_eligible(self):
        for header in ['Remote (US-Based Only)', 'San Francisco, CA | Onsite', 'Hybrid NYC', 'Remote (US)', 'Hybrid (Leuven, Belgium; remote 3 days/week)']:
            job=scan_hn.normalise('99',dict(objectID='1',parent_id=99,comment_text='Example | Founding Engineer | '+header))
            self.assertFalse(scan_hn.uk_route(job)[0],header)
        for header in ['London, UK | HYBRID','REMOTE (worldwide)','Remote (EMEA)']:
            job=scan_hn.normalise('99',dict(objectID='1',parent_id=99,comment_text='Example | Founding Engineer | '+header))
            self.assertTrue(scan_hn.uk_route(job)[0],header)

if __name__ == "__main__":
    unittest.main()
