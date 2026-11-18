"""Synthetic digest contracts; original inbox contents never enter fixtures."""
from email.message import EmailMessage
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_alerts as sa
import test_alerts_spool
from test_scan_alerts import alert_email

ONE = '11111111-1111-4111-8111-111111111111'
TWO = '22222222-2222-4222-8222-222222222222'
BASE = 'https://app.jackandjill.ai/jobs/'
SENDERS = {'jackandjill': ['jack@jackandjill.ai'], 'linkedin': ['jobalerts-noreply@linkedin.com']}
HTML = f'''<p>These are strong matches for you.</p>
<p><strong><a href="{BASE}{ONE}/post?source=email&amp;campaign=jack_agent">Engineering Lead at Example Labs</a></strong>:
Hybrid London, with three days in the office. Own the platform and release readiness.
The listed range is £90,000–£130,000 plus options.</p>
<p><a href="{BASE}{TWO}/post?source=email">QA Lead at Second Example</a>:
The first hire building reliable software.</p>
<p><a href="https://app.jackandjill.ai/jack/dashboard">Go to Dashboard</a></p>'''


def digest(html=HTML, plain=None, date='Thu, 05 Nov 2026 09:12:35 +0000'):
    message = EmailMessage()
    message['From'] = 'Jack from Jack and Jill <jack@jackandjill.ai>'
    message['Date'] = date
    message['Subject'] = 'Synthetic role suggestions'
    message.set_content(plain or 'See the role digest.')
    if html is not None:
        message.add_alternative(html, subtype='html')
    return message.as_bytes()


def radar_card(identity=ONE, title='Engineering Lead (£50k–£70k + Equity) at Example Labs',
               company='Example Labs', location='London', summary='Build the platform. A strong match for you.'):
    return f'''<table class="jj-card"><tbody><tr><td>
      <table><tbody><tr><td><a class="jj-title" href="{BASE}{identity}/post?source=email">{title}</a>
      <p>{company} · {location}</p></td></tr></tbody></table>
      <p>{summary}</p><p><a href="{BASE}{identity}/post">See the full role</a></p>
      <table class="jj-action-row"><tr><td><a href="https://app.jackandjill.ai/jack/radar?a=intro">Request an intro</a></td></tr></table>
      </td></tr></tbody></table>'''


class DigestParserTests(unittest.TestCase):
    def test_radar_cards_keep_title_metadata_pay_and_summary_separate(self):
        html = radar_card() + radar_card(TWO, 'QA Lead', 'Second Example', 'Singapore', 'A £90,000 grant funds our team.')
        kind, jobs, day = sa.read_message(digest(html), SENDERS)
        self.assertEqual((kind, day), ('jackandjill', '2026-11-05'))
        self.assertEqual([j['source_id'] for j in jobs], [ONE, TWO])
        self.assertEqual([j['company'] for j in jobs], ['Example Labs', 'Second Example'])
        self.assertEqual([j['title'] for j in jobs], ['Engineering Lead (£50k–£70k + Equity)', 'QA Lead'])
        self.assertEqual([j['location'] for j in jobs], ['London', 'Singapore'])
        self.assertEqual(jobs[0]['pay_text'], '£50k–£70k + Equity')
        self.assertEqual(jobs[1]['pay_text'], '')
        self.assertEqual(jobs[0]['email_summary'], 'Build the platform. A strong match for you.')
        self.assertTrue(all(not j.get('text') and not j.get('summary') for j in jobs))
        import yaml
        sa._eligibility(jobs, yaml.safe_load((Path(__file__).resolve().parent.parent/'config/portals.yml').read_text()))
        self.assertEqual([j['eligible'] for j in jobs], [True, False])
        self.assertEqual((jobs[0]['pay_min'], jobs[0]['pay_max']), ('50000', '70000'))

    def test_radar_and_digest_layouts_coexist_without_quoted_or_action_cards(self):
        html = HTML + '<blockquote>' + radar_card() + '</blockquote>' + radar_card(TWO, 'QA Lead at Second Example', 'Second Example', '', 'The first hire building reliable software.')
        jobs = sa.read_message(digest(html), SENDERS)[1]
        self.assertEqual([j['source_id'] for j in jobs], [ONE, TWO, TWO])
        self.assertNotIn('Request an intro', json.dumps(jobs))

    def test_radar_ambiguous_missing_or_truncated_cards_fail_closed(self):
        malformed = [(radar_card(company='Different Employer'), 'Conflicting .* employer'),
                     (radar_card(title=''), 'Incomplete or ambiguous'),
                     (radar_card().replace('class="jj-title"', 'class="unknown-title"'), 'Incomplete or ambiguous'),
                     (radar_card().replace('<p>Example Labs', '<p><p>Example Labs'), 'Malformed .* paragraph'),
                     (radar_card().replace('<p>Example Labs', radar_card(TWO) + '<p>Example Labs'), 'Nested .* cards'),
                     (radar_card().replace('<p>Example Labs', f'<a class="jj-title" href="{BASE}{ONE}/post">Second title</a><p>Example Labs'), 'Ambiguous .* title'),
                     (radar_card().replace('Example Labs · London', 'Location unavailable'), 'employer missing'),
                     (radar_card().replace(f'{BASE}{ONE}/post\">See', f'{BASE}{TWO}/post\">See'), 'Incomplete or ambiguous'),
                     (radar_card().replace('app.jackandjill.ai/jobs/', 'app.jackandjill.ai.evil.test/jobs/'), 'Unsupported .* URL'),
                     (radar_card().rsplit('</table>', 1)[0], 'Truncated .* card')]
        for html, reason in malformed:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                other_cards = HTML.replace(ONE, '33333333-3333-4333-8333-333333333333').replace(TWO, '44444444-4444-4444-8444-444444444444')
                sa.read_message(digest(other_cards + html), SENDERS)

    def test_conflicting_duplicate_identity_is_quarantined(self):
        with self.assertRaises(ValueError):
            sa.read_message(digest(HTML+f'<p><a href="{BASE}{ONE}/post">QA Lead at Conflicting Example</a>: London.</p>'),SENDERS)
        self.assertEqual(len(sa.read_message(digest(HTML+HTML),SENDERS)[1]),4)
    def test_explicit_foreign_location_is_kept_for_the_shared_filter(self):
        html=HTML.replace('Hybrid London, with three days in the office.', 'Remote United States, with occasional office visits.')
        jobs=sa.read_message(digest(html), SENDERS)[1]
        self.assertEqual(jobs[0]['location'],'Remote United States')
        import yaml
        sa._eligibility(jobs,yaml.safe_load((Path(__file__).resolve().parent.parent/'config/portals.yml').read_text()))
        self.assertFalse(jobs[0]['eligible'])

    def test_simple_location_prefixes_are_not_replaced_with_unknown(self):
        import yaml
        config=yaml.safe_load((Path(__file__).resolve().parent.parent/'config/portals.yml').read_text())
        # New York is explicitly provisional in the shared timezone policy; Singapore is excluded.
        for location,eligible in [('Singapore',False),('New York',True),('London',True)]:
            with self.subTest(location=location):
                html=HTML.replace('Hybrid London, with three days in the office.', location+', with three days in the office.')
                jobs=sa.read_message(digest(html),SENDERS)[1]
                self.assertEqual(jobs[0]['location'],location)
                sa._eligibility(jobs,config)
                self.assertEqual(jobs[0]['eligible'],eligible)

    def test_company_money_is_not_salary_and_mixed_bad_job_links_fail_the_message(self):
        html=HTML.replace('The listed range is £90,000–£130,000 plus options.', 'The company received a £60,000 grant for software research.')
        self.assertFalse(sa.read_message(digest(html),SENDERS)[1][0]['pay_text'])
        for url in (BASE.replace('https:','http:')+TWO+'/post', BASE.replace('.ai/','.ai.evil.test/')+TWO+'/post'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                sa.read_message(digest(HTML+f'<p><a href="{url}">QA Lead at Another Example</a>: London.</p>'),SENDERS)

    def test_pay_keyword_cannot_turn_company_funding_into_salary(self):
        for text,expected in [('The company received a £60,000 grant and offers competitive pay.', ''),
                              ('A £60,000 grant funds the team, and the salary is £90,000–£130,000.', 'salary is £90,000–£130,000.')]:
            with self.subTest(text=text):
                html=HTML.replace('The listed range is £90,000–£130,000 plus options.',text)
                self.assertEqual(sa.read_message(digest(html),SENDERS)[1][0]['pay_text'],expected)

    def test_wrapped_plain_reply_header_cannot_import_old_quoted_roles(self):
        text=f'[Engineering Lead at Example]({BASE}{ONE}/post): London.\n\nOn Thursday 5 November 2026 at 09:12,\nJack wrote:\n> [QA Lead at Quoted Example]({BASE}{TWO}/post): London.'
        self.assertEqual([j['source_id'] for j in sa.read_message(digest(None,text),SENDERS)[1]],[ONE])
    def test_html_keeps_per_role_identity_and_terms_without_promoting_generated_prose(self):
        kind, jobs, day = sa.read_message(digest(), SENDERS)
        self.assertEqual((kind, day), ('jackandjill', '2026-11-05'))
        self.assertEqual([j['source_id'] for j in jobs], [ONE, TWO])
        self.assertEqual([j['company'] for j in jobs], ['Example Labs', 'Second Example'])
        self.assertEqual([j['title'] for j in jobs], ['Engineering Lead', 'QA Lead'])
        self.assertEqual(jobs[0]['url'], BASE + ONE + '/post')
        self.assertEqual(jobs[0]['location'], 'Hybrid London')
        self.assertIn('£90,000–£130,000', jobs[0]['pay_text'])
        self.assertEqual(jobs[1]['location'], '')
        self.assertFalse(jobs[1].get('pay_text'))
        self.assertIn('Own the platform', jobs[0]['email_summary'])
        self.assertTrue(all(not j.get('text') and not j.get('summary') for j in jobs))

    def test_plain_text_and_html_have_the_same_card_facts(self):
        plain = f'''[Engineering Lead at Example Labs]({BASE}{ONE}/post?source=email): Hybrid London, with three days in the office. Own the platform and release readiness. The listed range is £90,000–£130,000 plus options.

[QA Lead at Second Example]({BASE}{TWO}/post?source=email): The first hire building reliable software.

[Go to Dashboard](https://app.jackandjill.ai/jack/dashboard)'''
        self.assertEqual(sa.read_message(digest(None, plain), SENDERS)[1], sa.read_message(digest(), SENDERS)[1])

    def test_quoted_reply_and_tracking_variants_do_not_add_cards(self):
        quoted = HTML + f'<blockquote><p><a href="{BASE}33333333-3333-4333-8333-333333333333/post">Old Lead at Old Example</a>: London.</p></blockquote>'
        self.assertEqual([j['source_id'] for j in sa.read_message(digest(quoted), SENDERS)[1]], [ONE, TWO])
        from normalise import job_url
        self.assertEqual(job_url(BASE + ONE + '/post?source=email#top'), BASE + ONE + '/post')
        self.assertEqual(job_url(BASE + ONE.upper() + '/post/'), BASE + ONE + '/post')

    def test_malformed_job_card_or_spoofed_host_never_silently_yields_partial_success(self):
        for html in (HTML + f'<p><a href="{BASE}33333333-3333-4333-8333-333333333333/post">View role</a></p>',
                     HTML.replace('app.jackandjill.ai', 'app.jackandjill.ai.evil.test')):
            with self.subTest(html=html[:30]), self.assertRaises(ValueError):
                sa.read_message(digest(html), SENDERS)

    def test_multiple_from_headers_cannot_choose_a_trusted_sender(self):
        raw = b'From: jack@jackandjill.ai\r\n' + alert_email()
        self.assertIsNone(sa.read_message(raw, SENDERS)[0])


class MixedSpoolTests(test_alerts_spool.SpoolReaderTests):
    def setUp(self):
        super().setUp()
        self.portals.write_text(self.portals.read_text() + '    jackandjill: [jack@jackandjill.ai]\n')

    def test_radar_quarantine_recovery_keeps_original_receipt_and_replay(self):
        self.spool.add(digest(radar_card()), received_at='2026-11-05T15:22:37Z')
        with patch.dict(sa.PARSERS, jackandjill=lambda html: (_ for _ in ()).throw(ValueError('old parser'))):
            self.assertEqual(self.run_scan()['quarantined'], 1)
        checkpoint = self.checkpoint.read_bytes()
        self.assertEqual(self.run_scan()['quarantined'], 1)
        result = self.run_scan(retry_quarantine=True)
        self.assertEqual((result['state'], result['messages'], result['jobs'], result['quarantined']), ('ok', 1, 1, 0))
        row = self.observations()['items']['alert:jackandjill\x1f' + ONE]
        self.assertEqual((row['first_seen_at'], row['last_seen_at']), ('2026-11-05T15:22:37Z', '2026-11-05T15:22:37Z'))
        self.assertEqual(json.loads(self.checkpoint.read_bytes())['last_sequence'], json.loads(checkpoint)['last_sequence'])
        self.assertEqual(self.run_scan(retry_quarantine=True)['new'], 0)
        self.assertEqual(self.observations()['items']['alert:jackandjill\x1f' + ONE], row)

    def test_two_sources_keep_independent_counts_dates_and_unchanged_replay(self):
        self.spool.add()
        self.spool.add(digest(), received_at='2026-11-05T09:13:00Z')
        result = self.run_scan()
        self.assertEqual((result['state'], result['messages'], result['jobs']), ('ok', 2, 5))
        sources = self.observations()['sources']
        self.assertEqual(sources['alert:linkedin']['detail']['jobs'], 3)
        self.assertEqual(sources['alert:jackandjill']['detail']['jobs'], 2)
        before = self.observations()['items']
        self.assertEqual(before['alert:jackandjill\x1f' + ONE]['last_seen_at'], '2026-11-05T09:13:00Z')
        self.checkpoint.unlink()
        self.assertEqual(self.run_scan()['new'], 0)
        self.assertEqual(self.observations()['items'], before)

    def test_two_receipts_in_one_batch_keep_first_sighting_and_newest_terms(self):
        self.spool.add(digest(date='Sun, 01 Nov 2026 09:00:00 +0000'),received_at='2026-11-01T09:00:00Z')
        self.spool.add(digest(HTML.replace('£90,000–£130,000','£100,000–£140,000')),received_at='2026-11-05T09:00:00Z')
        self.assertEqual(self.run_scan()['jobs'],2)
        row=self.observations()['items']['alert:jackandjill\x1f'+ONE]
        self.assertEqual(row['first_seen_at'],'2026-11-01T09:00:00Z')
        self.assertEqual(row['last_seen_at'],'2026-11-05T09:00:00Z')
        self.assertIn('£100,000–£140,000',row['current']['pay_text'])

    def test_equal_time_quarantine_recovery_cannot_replace_later_receipt_terms(self):
        self.spool.add(digest(), received_at='2026-11-05T09:00:00Z')
        with patch.dict(sa.PARSERS, jackandjill=lambda html: (_ for _ in ()).throw(ValueError('old parser'))):
            self.assertEqual(self.run_scan()['quarantined'], 1)
        self.spool.add(digest(HTML.replace('£90,000–£130,000', '£100,000–£140,000')), received_at='2026-11-05T09:00:00Z')
        self.assertEqual(self.run_scan()['jobs'], 2)
        before = self.observations()['items']
        self.assertEqual(before['alert:jackandjill\x1f' + ONE]['current']['receipt_sequence'], 2)
        result = self.run_scan(retry_quarantine=True)
        self.assertEqual((result['state'], result['messages'], result['quarantined']), ('ok', 1, 0))
        self.assertEqual(self.observations()['items'], before)

    def test_interrupted_stage_and_older_recovery_preserve_first_sighting(self):
        self.spool.add(digest(date='Sun, 01 Nov 2026 09:00:00 +0000'),received_at='2026-11-01T09:00:00Z')
        self.spool.add(digest(HTML.replace('£90,000–£130,000','£100,000–£140,000')),received_at='2026-11-05T09:00:00Z')
        with patch.object(sa,'atomic_write_json',side_effect=OSError('interrupted staging')):
            self.assertEqual(self.run_scan()['state'],'error')
        self.assertEqual(self.run_scan()['state'],'ok')
        row=self.observations()['items']['alert:jackandjill\x1f'+ONE]
        self.assertEqual(row['first_seen_at'],'2026-11-01T09:00:00Z')
        self.assertIn('£100,000–£140,000',row['current']['pay_text'])
        self.spool.add(digest(date='Sat, 31 Oct 2026 09:00:00 +0000'),received_at='2026-10-31T09:00:00Z')
        self.assertEqual(self.run_scan()['state'],'ok')
        row=self.observations()['items']['alert:jackandjill\x1f'+ONE]
        # Recovery leaves an already retained later observation byte-for-byte unchanged.
        self.assertEqual(row['first_seen_at'],'2026-11-01T09:00:00Z')
        self.assertEqual(row['last_seen_at'],'2026-11-05T09:00:00Z')
        self.assertIn('£100,000–£140,000',row['current']['pay_text'])

    def test_one_source_failure_cannot_poison_or_be_hidden_by_other_source(self):
        self.spool.add(digest('<p>Changed format</p>'))
        self.spool.add()
        self.assertEqual(self.run_scan()['state'], 'error')
        sources = self.observations()['sources']
        self.assertEqual(sources['alert:linkedin']['state'], 'ok')
        self.assertEqual(sources['alert:jackandjill']['state'], 'error')
        self.assertEqual(self.run_scan()['quarantined'], 1)
        self.assertEqual(self.observations()['sources']['alert:jackandjill']['state'], 'error')


if __name__ == '__main__':
    unittest.main()
