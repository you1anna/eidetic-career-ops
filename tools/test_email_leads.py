"""Deferred alert review stays bounded, unassessed and explicitly manual."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import concurrent.futures

import db
import records


def item(number=1, **fields):
    current = dict(title='QA Lead', company='Example Labs', url=f'https://www.linkedin.com/jobs/view/{number}/',
                   eligible=True, received_at='2026-11-05T10:00:00Z', email_summary='A strong match. Lead a team.',
                   pay_text='£90,000–£110,000', location='London')
    current.update(fields)
    return dict(kind='job', source_scope='alert:linkedin', source_id=str(number), current=current,
                first_seen_at='2026-11-01T10:00:00Z', last_seen_at=current['received_at'], source_lifecycle='active')


class EmailLeadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = db.connect(self.root / 'site.db'); db.migrate(self.conn); self.addCleanup(self.conn.close)
        (self.root / 'pending').mkdir()
        self.path = self.root / 'pending/observations.json'
        self.write([item()])

    def write(self, items):
        self.path.write_text(json.dumps(dict(items={str(i): row for i, row in enumerate(items)}, sources={})))

    def test_recent_bounded_search_and_source_identity(self):
        import email_leads
        self.write([item(i, title=f'QA Lead {i}', received_at=f'2026-11-{i:02}T10:00:00Z') for i in range(1, 26)])
        result = email_leads.search(self.conn, self.root)
        self.assertEqual(len(result['items']), 20)
        self.assertEqual(result['items'][0]['title'], 'QA Lead 25')
        self.assertNotIn('email_summary', result['items'][0])
        self.assertNotIn('strong match', json.dumps(result))
        self.assertEqual(len(email_leads.search(self.conn, self.root, query='Lead 2')['items']), 7)
        self.assertEqual(email_leads.search(self.conn, self.root, source='jackandjill')['items'], [])
        self.assertEqual(result['items'][0]['id'], hashlib.sha256(b'alert:linkedin\x1f25').hexdigest())

    def test_detail_preserves_unverified_context_and_reading_writes_nothing(self):
        import email_leads
        before = self.conn.total_changes
        lead = email_leads.search(self.conn, self.root)['items'][0]
        detail = email_leads.detail(self.conn, self.root, lead['id'])
        self.assertEqual(detail['email_summary'], 'A strong match. Lead a team.')
        self.assertEqual(detail['pay_text'], '£90,000–£110,000')
        self.assertEqual(detail['assessment'], 'Unassessed')
        self.assertEqual(self.conn.total_changes, before)

    def test_search_treats_punctuation_consistently_and_keeps_language_names_distinct(self):
        import email_leads
        self.write([item(1, title='Senior QA-Lead', company='Marks & Spencer'),
                    item(2, title='C++ Engineer'), item(3, title='C# Engineer')])
        for query, titles in [('QA-Lead', ['Senior QA-Lead']), ('QA Lead', ['Senior QA-Lead']),
                              ('Marks & Spencer', ['Senior QA-Lead']), ('C++', ['C++ Engineer']),
                              ('C#', ['C# Engineer'])]:
            with self.subTest(query=query):
                self.assertEqual([r['title'] for r in email_leads.search(self.conn, self.root, query=query)['items']], titles)

    def test_recent_search_compares_utc_instants_before_capping_or_deduplicating(self):
        import email_leads
        self.write([item(i, received_at='2026-11-05T10:00:00Z') for i in range(1, 21)] +
                   [item(21, title='Actually older', received_at='2026-11-05T11:00:00+02:00')])
        rows = email_leads.search(self.conn, self.root)['items']
        self.assertEqual(len(rows), 20)
        self.assertNotIn('Actually older', [r['title'] for r in rows])
        self.write([item(1, title='Later', received_at='2026-11-05T10:00:00Z'),
                    item(2, title='Earlier', url='https://www.linkedin.com/jobs/view/1/', received_at='2026-11-05T11:00:00+02:00')])
        row = email_leads.search(self.conn, self.root)['items'][0]
        self.assertEqual((row['title'], row['received_at']), ('Later', '2026-11-05T10:00:00Z'))

    def test_search_deduplicates_urls_and_hides_saved_closed_or_ineligible_leads(self):
        import email_leads
        duplicate = item(2, title='Latest sighting', url='https://www.linkedin.com/jobs/view/1/?trackingId=repeat',
                         received_at='2026-11-06T10:00:00Z')
        closed = item(3); closed['source_lifecycle'] = 'closed'
        jack = item(5, title='Principal Engineer', url='https://app.jackandjill.ai/jobs/11111111-1111-1111-1111-111111111111/post')
        jack.update(source_scope='alert:jackandjill', source_id='11111111-1111-1111-1111-111111111111')
        self.write([item(), duplicate, closed, item(4, eligible=False), jack])
        found = email_leads.search(self.conn, self.root)['items']
        self.assertEqual([r['title'] for r in found], ['Latest sighting', 'Principal Engineer'])
        self.assertEqual([r['title'] for r in email_leads.search(self.conn, self.root, source='jackandjill')['items']], ['Principal Engineer'])
        records.save_job(self.conn, dict(name='Already saved', url='https://www.linkedin.com/jobs/view/1/'))
        self.assertEqual([r['title'] for r in email_leads.search(self.conn, self.root)['items']], ['Principal Engineer'])

    def test_conflicting_record_identity_refuses_shortlist_without_mutation(self):
        import email_leads
        first = records.create(self.conn, dict(type='job', name='First', url='https://www.linkedin.com/jobs/view/1/'))['record']
        second = records.create(self.conn, dict(type='job', name='Second', url='https://www.linkedin.com/jobs/view/2/'))['record']
        with self.conn:
            self.conn.execute('INSERT INTO source_records(source_scope,source_id,record_id) VALUES(?,?,?)',
                              ('alert:linkedin', '1', second['id']))
        before = self.conn.total_changes
        with self.assertRaises(records.Conflict): email_leads.shortlist(self.conn, self.root, email_leads.lead_id(item()))
        self.assertEqual(self.conn.total_changes, before)
        self.assertIsNone(records.get(self.conn, first['id'])['status'])
        self.assertIsNone(records.get(self.conn, second['id'])['status'])

    def test_shortlist_bypasses_allowance_without_promoting_email_claims(self):
        import email_leads
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('discovery_capacity_admissions_v1','invalid-but-untouched')")
        lead = email_leads.search(self.conn, self.root)['items'][0]
        saved = email_leads.shortlist(self.conn, self.root, lead['id'], actor='owner')
        row = records.get(self.conn, saved['record']['id'])
        self.assertEqual(row['status'], 'pursue')
        self.assertEqual((row['pay'], row['summary']), ('', ''))
        self.assertNotEqual(row['availability']['status'], 'open')
        self.assertEqual(json.loads(self.conn.execute('SELECT payload_json FROM observations').fetchone()[0])['current']['email_summary'], 'A strong match. Lead a team.')
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='discovery_capacity_admissions_v1'").fetchone()[0], 'invalid-but-untouched')
        self.assertEqual(email_leads.search(self.conn, self.root)['items'], [])
        self.assertEqual(email_leads.detail(self.conn, self.root, lead['id'])['record_id'], row['id'])
        self.assertEqual(email_leads.record_review(self.conn, row['id']), {'id':lead['id']})
        again = email_leads.shortlist(self.conn, self.root, lead['id'], actor='owner')
        self.assertEqual(again['record']['id'], row['id'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)

    def test_existing_candidate_transitions_but_application_and_human_facts_survive(self):
        import email_leads
        lead = email_leads.search(self.conn, self.root)['items'][0]
        record = records.create(self.conn, dict(type='job', name='Edited title', organisation='Example Labs',
                               url='https://www.linkedin.com/jobs/view/1/', pay='Human pay'))['record']
        saved = email_leads.shortlist(self.conn, self.root, lead['id'], actor='owner')['record']
        self.assertEqual((saved['id'], saved['status'], saved['name'], saved['pay']), (record['id'], 'pursue', 'Edited title', 'Human pay'))
        records.transition(self.conn, record['id'], dict(field='status', to='applied', revision=saved['revision']))
        saved = email_leads.shortlist(self.conn, self.root, lead['id'], actor='owner')['record']
        self.assertEqual(saved['status'], 'applied')

    def test_later_source_terms_follow_shared_import_policy_without_confirming_email_claims(self):
        import email_leads, record_imports, scan_alerts, yaml
        lead = item(title='Principal Engineer')
        scan_alerts._eligibility([lead['current']], yaml.safe_load((Path(__file__).resolve().parent.parent/'config/portals.yml').read_text()))
        self.write([lead])
        saved = email_leads.shortlist(self.conn, self.root, email_leads.lead_id(lead))['record']
        self.assertEqual((saved['pay'], saved['summary']), ('', ''))
        lead['current']['received_at'] = lead['last_seen_at'] = '2026-11-06T10:00:00Z'
        with records.transaction(self.conn):
            record_imports.import_observations(self.conn, self.root/'pending', data=dict(items={'role': lead}, sources={}))
        row = records.get(self.conn, saved['id'])
        self.assertEqual((row['pay'], row['pay_min'], row['pay_max']), ('GBP 90000–110000/year', '90000', '110000'))
        self.assertEqual((row['status'], row['summary']), ('pursue', ''))
        self.assertNotEqual(row['availability']['status'], 'open')
        self.assertFalse(row['advert'])

    def test_missing_corrupt_and_invalid_id_fail_closed(self):
        import email_leads
        for text in ('{broken', '{"items":{"a":1,"a":2}}', '{}', '{"items":[],"sources":{}}'):
            self.path.write_text(text)
            with self.assertRaises(ValueError): email_leads.search(self.conn, self.root)
        self.path.unlink()
        with self.assertRaises(ValueError): email_leads.search(self.conn, self.root)
        with self.assertRaises(ValueError): email_leads.detail(self.conn, self.root, '../mail.db')

    def test_concurrent_shortlist_creates_one_role_and_one_source_observation(self):
        import email_leads
        identity = email_leads.search(self.conn, self.root)['items'][0]['id']
        def save():
            conn = db.connect(self.root / 'site.db')
            try: return email_leads.shortlist(conn, self.root, identity)['record']['id']
            finally: conn.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            saved = list(pool.map(lambda _: save(), range(4)))
        self.assertEqual(len(set(saved)), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_original_email_is_hash_checked_and_never_rendered_as_html(self):
        import email_leads
        from mail_intake import MailSpool
        raw = b'From: source@example.test\r\nMessage-ID: <fixture@example.test>\r\nSubject: Original alert\r\n\r\n<script>untrusted()</script>'
        spool = MailSpool(self.root / 'email/mail.db')
        receipt = spool.receive(raw, envelope_from='source@example.test', envelope_to='alerts@example.com', received_at='2026-11-05T10:00:00Z')
        self.write([item(receipt_id=receipt['receipt_id'])])
        identity = email_leads.search(self.conn, self.root)['items'][0]['id']
        detail = email_leads.detail(self.conn, self.root, identity)
        self.assertTrue(detail['email_available'])
        self.assertEqual(detail['original_email'], 'https://mail.google.com/mail/u/0/#search/rfc822msgid%3Afixture%40example.test')
        self.assertEqual(email_leads.original_email(self.root, identity), raw)
        with spool._connection(write=True) as conn:
            conn.execute('UPDATE messages SET raw=?', (raw + b'changed',))
        with self.assertRaises(ValueError): email_leads.original_email(self.root, identity)

    def test_partial_save_rolls_back_and_closed_or_archived_leads_do_not_reopen(self):
        import email_leads
        identity = email_leads.search(self.conn, self.root)['items'][0]['id']
        with patch('records.add_observation', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError): email_leads.shortlist(self.conn, self.root, identity)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        closed = item(); closed['source_lifecycle'] = 'closed'; self.write([closed])
        with self.assertRaises(ValueError): email_leads.shortlist(self.conn, self.root, identity)
        self.write([item()])
        saved = email_leads.shortlist(self.conn, self.root, identity)['record']
        records.archive(self.conn, saved['id'], dict(revision=saved['revision'], reason='Not pursuing'))
        with self.assertRaises(records.Conflict): email_leads.shortlist(self.conn, self.root, identity)


if __name__ == '__main__': unittest.main()
