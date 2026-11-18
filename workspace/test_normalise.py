"""The shared normalisation rules every import and the repair backfill use."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalise
import profile_rules
from test_profile_rules import EXAMPLE


class NormaliseTests(unittest.TestCase):
    def test_job_url_canonicalises_linkedin_identity_and_preserves_other_queries(self):
        for url in ('https://www.linkedin.com/jobs/view/123456/?trackingId=abc',
                    'https://uk.linkedin.com/jobs/view/engineering-manager-at-example-123456?refId=x',
                    'http://www.linkedin.com/comm/jobs/view/123456/?trk=email'):
            self.assertEqual(normalise.job_url(url), 'https://www.linkedin.com/jobs/view/123456/')
        self.assertEqual(normalise.job_url('https://Example.test/jobs?id=12&source=email#details'),
                         'https://example.test/jobs?id=12&source=email')
        self.assertEqual(normalise.job_url('https://linkedin.com.example.test/jobs/view/123456/?id=4'),
                         'https://linkedin.com.example.test/jobs/view/123456/?id=4')

    def test_job_url_rejects_missing_or_malformed_host_and_embedded_credentials(self):
        for url in ('', 'javascript:alert(1)', 'https:///jobs/123', 'https://user@linkedin.com/jobs/view/1',
                    'https://linkedin.com\\@evil.test/jobs/1', 'https://bad host.test/jobs/1',
                    'https://example.test:invalid/jobs/1', 'https://example.test\n/jobs/1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalise.job_url(url)

    def test_employer_key_skips_non_employers_and_the_former_employer(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        self.assertEqual(normalise.employer_key('  Acme   Ltd '), 'acme ltd')
        for name in ('Retired', 'Self-employed', 'Example Corp', 'Example Group UK', '', None):
            self.assertEqual(normalise.employer_key(name), '', name)
        # LinkedIn placeholders for "no organisation yet" (10 November import of former colleagues).
        for name in ('Stealth', 'Stealth Startup', 'Stealth (Hospitality SaaS & Other Experiments)',
                     'Freelance (Self employed)', 'Self Employed Consultant', 'none', 'N/A', 'Confidential'):
            self.assertEqual(normalise.employer_key(name), '', name)
        for name in ('Stealthy Labs', 'Freelancer.com', 'Home Occasionally', 'Nonesuch Ltd'):
            self.assertNotEqual(normalise.employer_key(name), '', name)

    def test_entities_decode_and_whitespace_collapses(self):
        self.assertEqual(normalise.clean("Newton&#x27;s  Tree "), "Newton's Tree")
        self.assertEqual(normalise.org_key("Newton&#x27;s Tree"), normalise.org_key("newton's tree"))

    def test_a_hex_entity_missing_its_hash_decodes(self):
        self.assertEqual(normalise.clean("Date Posted: 2026-09-21&xa;&xa; Country: UK &x27;s"), "Date Posted: 2026-09-21 Country: UK 's")
        self.assertEqual(normalise.clean("Tax & xa; Ltd"), "Tax & xa; Ltd")
        self.assertEqual(normalise.normalise_values({"summary": "Posted&xa;Country"})["summary"], "Posted\nCountry")

    def test_date_splits_keep_the_date_and_move_the_text(self):
        cases = {
            '2026-10-23': ('2026-10-23', ''),
            '2027-02': ('2027-02', ''),
            'Unknown': ('', ''),
            '2025-11-24 (interview)': ('2025-11-24', 'interview'),
            '2026-10-23 (CE-001)': ('2026-10-23', 'CE-001'),
            '2026-10-01, Brighton Dome': ('2026-10-01', 'Brighton Dome'),
            '2027-02; days TBA': ('2027-02', 'days TBA'),
            'Check meetup.com': ('', 'Check meetup.com'),
        }
        for raw, expected in cases.items():
            self.assertEqual(normalise.split_date(raw), expected, raw)

    def test_normalise_values_appends_displaced_text_to_notes_once(self):
        once = normalise.normalise_values({'date': 'Recurring; check site', 'notes': 'Meetup.'})
        self.assertEqual(once['date'], '')
        self.assertEqual(once['notes'], 'Meetup. Date note: Recurring; check site.')
        again = normalise.normalise_values({**once, 'date': 'Recurring; check site'})
        self.assertEqual(again['notes'], once['notes'])

    def test_person_key_ignores_case_hyphens_dots_and_spacing(self):
        self.assertEqual(normalise.person_key('Alex Sample-Harris'), normalise.person_key('alex  sample harris'))
        self.assertEqual(normalise.person_key('J.R. Smith'), normalise.person_key('J R Smith'))
        self.assertEqual(normalise.person_key("Seán O'Brien"), normalise.person_key('Seán OBrien'))
        self.assertNotEqual(normalise.person_key('Ann Lee'), normalise.person_key('Anne Lee'))

    def test_clean_values_are_unchanged(self):
        values = {'name': 'Head of QA', 'date': '2026-09-01', 'notes': 'Fine.'}
        self.assertEqual(normalise.normalise_values(values), values)



class BoardPostingTests(unittest.TestCase):
    def test_advert_pages_on_employer_boards_yield_provider_slug_and_posting(self):
        uid = '1c29ae8a-1234-494c-a13b-4d4e2b6f262b'
        cases = {
            'https://job-boards.greenhouse.io/gocardless/jobs/7212345?gh_src=x': ('greenhouse', 'gocardless', '7212345'),
            'https://boards.greenhouse.io/flohealth/jobs/55': ('greenhouse', 'flohealth', '55'),
            f'https://jobs.lever.co/palantir/{uid}/apply': ('lever', 'palantir', uid),
            f'https://jobs.ashbyhq.com/motorway/{uid}': ('ashby', 'motorway', uid),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(normalise.board_posting(url), expected)

    def test_board_home_pages_and_other_sites_are_not_postings(self):
        for url in ('https://jobs.ashbyhq.com/motorway', 'https://job-boards.greenhouse.io/gocardless',
                    'https://www.adzuna.co.uk/jobs/land/ad/5928343016', 'https://jobs.ashbyhq.com/motorway/not-an-id',
                    '', None):
            with self.subTest(url=url):
                self.assertIsNone(normalise.board_posting(url))

if __name__ == '__main__':
    unittest.main()
