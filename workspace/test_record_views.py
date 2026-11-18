"""Evidence projections retain decision inputs without exposing private message content."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import db
import records
import record_views


class EvidenceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.temp.name) / 'projection.db')
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def observe(self, rid, source, at, value):
        records.add_observation(self.conn, rid, source, at, json.dumps(value))

    def logo(self, rid, company, at, marker='NEW'):
        url = f'https://media.licdn.com/dms/image/v2/{marker}/company-logo_100_100/0/1?t=signed'
        self.observe(rid, 'alert:linkedin', at, dict(current=dict(company=company, company_logo_url=url,
                     received_at=at, url=f'https://www.linkedin.com/jobs/view/{marker}/')))
        return url

    def test_loading_more_records_than_sqlite_binds_parameters_still_projects_their_evidence(self):
        """One bound value per record once made the whole workspace fail to load past SQLite's
        variable limit (32,766 on the server's build), reached at half that many records."""
        import sqlite3
        people = [records.create(self.conn, dict(type='person', name=f'Example Person {i}'), actor='agent')['record']
                  for i in range(12)]
        records.interaction(self.conn, dict(record_id=people[11]['id'], revision=people[11]['revision'],
                                            at='2026-10-20T10:00:00+00:00', channel='email', direction='in',
                                            summary='Synthetic reply'), actor='agent')
        self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
        loaded = {r['id']: r for r in record_views.load_records(self.conn)}
        self.assertEqual(len(loaded), 12)
        self.assertTrue(all(r['evidence_brief']['version'] for r in loaded.values()))
        last = [item for item in loaded[people[11]['id']]['evidence_brief']['items'] if item['kind'] == 'last_contact']
        self.assertEqual([item['value'] for item in last], ['2026-10-20T10:00:00+00:00'])

    def test_latest_company_logo_is_shared_with_older_jobs_without_copying_facts(self):
        company = records.create(self.conn, dict(type='company', name='Example Company'))['record']
        jobs = [records.create(self.conn, dict(type='job', name=f'Vacancy {i}', organisation='Example Company'))['record']
                for i in range(3)]
        self.logo(jobs[0]['id'], 'Example Company', '2026-10-27T10:00:00Z', 'OLD')
        newest = self.logo(jobs[1]['id'], ' example company ', '2026-10-28T09:00:00Z')
        self.observe(jobs[1]['id'], 'alert:linkedin', '2026-10-28T10:00:00Z', dict(current=dict(
            company='Example Company', company_logo_url='https://evil.test/company-logo')))
        self.observe(jobs[1]['id'], 'alert:linkedin', '2026-10-28T11:00:00Z', dict(current=dict(company='Example Company')))
        views = record_views.flat_records(self.conn)
        changes = self.conn.total_changes
        projected = record_views.company_logos(self.conn, views)
        self.assertEqual(projected, {company['id']: dict(url=newest, company_name='Example Company',
                         observed_at='2026-10-28T09:00:00Z', source='alert:linkedin')})
        self.assertTrue(all(r.get('logo_company_id') == company['id'] for r in views))
        self.assertEqual(self.conn.total_changes, changes, 'Logo reads must not write or copy source facts')
        self.assertTrue(all('company_logo_url' not in r for r in record_views.flat_records(self.conn)))

    def test_company_logo_respects_merge_survivors_ambiguity_and_agency_labels(self):
        agency = records.create(self.conn, dict(type='agency', name='Example Agency'))['record']
        client = records.create(self.conn, dict(type='company', name='Example Client'))['record']
        job = records.create(self.conn, dict(type='job', name='Client role', organisation=client['name']))['record']
        self.logo(job['id'], agency['name'], '2026-10-28T09:00:00Z')
        views = record_views.flat_records(self.conn)
        logos = record_views.company_logos(self.conn, views)
        self.assertEqual(set(logos), {agency['id']})
        by_id = {r['id']: r for r in views}
        self.assertNotIn('logo_company_id', by_id[job['id']])
        self.assertEqual(by_id[agency['id']]['logo_company_id'], agency['id'])
        survivor = records.create(self.conn, dict(type='agency', name='Renamed Agency'))['record']
        records.merge(self.conn, agency['id'], survivor['id'], dict(revision=agency['revision']))
        self.assertEqual(set(record_views.company_logos(self.conn, record_views.flat_records(self.conn))), {survivor['id']})
        records.create(self.conn, dict(type='company', name='Renamed Agency'))
        self.assertEqual(record_views.company_logos(self.conn, record_views.flat_records(self.conn)), {},
                         'An ambiguous current company name must not receive borrowed branding')

    def test_logo_projection_query_count_and_result_are_bounded_with_history(self):
        company = records.create(self.conn, dict(type='company', name='Example'))['record']
        job = records.create(self.conn, dict(type='job', name='Role', organisation='Example'))['record']
        latest = self.logo(job['id'], 'Example', '2026-10-28T09:00:00Z')
        def project():
            queries = []
            views = record_views.flat_records(self.conn)
            self.conn.set_trace_callback(queries.append)
            try:
                result = record_views.company_logos(self.conn, views)
            finally:
                self.conn.set_trace_callback(None)
            return result, len(queries)
        first, count = project()
        for i in range(30):
            extra = records.create(self.conn, dict(type='job', name=f'Old role {i}', organisation='Example'))['record']
            self.logo(extra['id'], 'Example', '2026-10-01T09:00:00Z', str(i))
        result, later_count = project()
        self.assertEqual(later_count, count)
        self.assertEqual(result, first)
        self.assertEqual(result[company['id']]['url'], latest)

    def test_already_supplied_feed_logo_uses_the_same_host_and_identity_rules(self):
        company = records.create(self.conn, dict(type='company', name='Example'))['record']
        job = records.create(self.conn, dict(type='job', name='Role', organisation='Example'))['record']
        url = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1'
        self.observe(job['id'], 'feed:example', '2026-10-28T09:00:00Z', dict(current=dict(
            company='Example', company_logo_url=url)))
        self.observe(job['id'], 'feed:example', '2026-10-28T10:00:00Z', dict(current=dict(
            company='Example', company_logo_url='https://another-cdn.test/logo.png')))
        records.add_observation(self.conn, job['id'], 'alert:linkedin', '2026-10-28T11:00:00Z', '{bad json')
        result = record_views.company_logos(self.conn, record_views.flat_records(self.conn))
        self.assertEqual(result[company['id']]['url'], url)
        self.assertEqual(result[company['id']]['source'], 'feed:example')

    def test_source_context_preserves_receipt_date_without_erasing_richer_advert(self):
        job = records.create(self.conn, dict(type='job', name='Example role'))['record']
        self.observe(job['id'], 'board:example', '2026-10-27T09:00:00Z', dict(current=dict(
            company='Example', text='Lead a software engineering team.', url='https://example.test/role')))
        self.observe(job['id'], 'alert:linkedin', '2026-10-29T09:00:00Z', dict(current=dict(
            company='Example', received_at='2026-10-28T09:00:00Z', url='https://www.linkedin.com/jobs/view/901/')))
        view = record_views.load_records(self.conn, job['id'])[0]
        self.assertEqual(view['source_context'], dict(source='alert:linkedin',
            url='https://www.linkedin.com/jobs/view/901/', at='2026-10-28T09:00:00Z', basis='email'))
        self.assertEqual(view['advert']['text'], 'Lead a software engineering team.')
        self.assertNotEqual(view['availability'].get('source'), 'alert:linkedin')

    def test_bounded_advert_preserves_truncation_for_requirement_uncertainty(self):
        job = records.create(self.conn, dict(type='job', name='Head of Quality Engineering',
            url='https://example.com/jobs/long', engagement='contract', location='London'))['record']
        body = 'Improve release readiness. ' + 'Ordinary context. ' * 1000 + 'Police-system experience is essential.'
        self.observe(job['id'], 'board:example', db.now(), dict(current=dict(
            text=body, url=job['url'], employment_type='contract')))
        view = records.state(self.conn)
        role = next(r for r in view['records'] if r['id'] == job['id'])
        self.assertEqual(len(role['advert']['text']), 16001)
        self.assertTrue(role['relevance']['qualification']['requirements_truncated'])
        self.assertEqual(role['relevance']['band'], 'possible')
        self.assertTrue(any('bounded comparison' in gap for gap in role['relevance']['fit']['gaps']))
        self.assertFalse(view['opportunities']['discovery'])

    def test_job_checks_do_not_borrow_generic_review_email_or_record_dates(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy', date='2026-10-17'))['record']
        self.observe(job['id'], 'research:old-advert', '2026-10-17T11:00:00Z', dict(
            reviewed_at='2026-10-17T11:00:00Z', source_date='2026-10-17', verification='primary-read',
            summary='Reviewed the old email', source_urls=['https://example.com/jobs/1']))
        self.assertEqual(record_views.load_records(self.conn, job['id'])[0]['evidence_checks'],
                         dict(vacancy=None, contacts=[]))

    def test_job_checks_use_all_observations_and_latest_checked_instant_per_subject(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        person = records.create(self.conn, dict(type='person', name='Example Contact'))['record']
        other = records.create(self.conn, dict(type='person', name='Another Contact'))['record']
        company = records.create(self.conn, dict(type='company', name='Example Company'))['record']
        def check(at, **changes):
            value = dict(scope='vacancy', checked_at=at, status='confirmed',
                         source_url='https://example.com/jobs/1', basis='Read the current advert.')
            value.update(changes)
            return value
        early = check('2026-10-06T09:00:00+07:00')
        closed = check('2026-10-06T03:00:00Z', status='closed', basis='The page says closed.')
        contact = check('2026-10-06T02:00:00Z', scope='contact_route', person_id=person['id'])
        uncertain = dict(contact, checked_at='2026-10-06T03:00:00Z', status='unverified')
        employer = dict(contact, organisation_id=company['id'])
        another = dict(contact, person_id=other['id'])
        self.observe(job['id'], 'research:checks', '2026-10-06', dict(source_urls=[early['source_url']],
                     freshness_checks=[early, contact, employer, another]))
        self.observe(job['id'], 'research:checks', '2026-10-07', dict(source_urls=[early['source_url']],
                     freshness_checks=[closed, uncertain]))
        # Later reviews and imports of older checks cannot re-confirm or hide the newer failure.
        for day in range(8, 13):
            self.observe(job['id'], f'research:history-{day}', f'2026-10-{day:02}',
                         dict(source_urls=[early['source_url']], freshness_checks=[early]))
        view = record_views.load_records(self.conn, job['id'])[0]
        self.assertEqual(len(view['research']), 3)
        self.assertEqual(view['evidence_checks']['vacancy'], closed)
        contacts = view['evidence_checks']['contacts']
        self.assertCountEqual(contacts, [uncertain, employer, another])
        self.assertEqual(view['status'], job['status'])
        self.assertNotIn('fresh', view['evidence_checks']['vacancy'])

    def test_equal_time_cannot_reconfirm_a_failed_check_and_later_success_can(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        check = dict(scope='vacancy', checked_at='2026-10-06T11:00:00Z', status='unverified',
                     source_url='https://example.com/jobs/1', basis='The page could not be read.')
        for day, status in ((6, 'unverified'), (7, 'confirmed')):
            self.observe(job['id'], 'research:checks', f'2026-10-0{day}',
                         dict(source_urls=[check['source_url']], freshness_checks=[dict(check, status=status)]))
        self.assertEqual(record_views.load_records(self.conn, job['id'])[0]['evidence_checks']['vacancy'], check)
        later = dict(check, checked_at='2026-10-06T11:00:00.001Z', status='confirmed',
                     basis='The advert was read successfully and is accepting applications.')
        self.observe(job['id'], 'research:checks', '2026-10-08',
                     dict(source_urls=[check['source_url']], freshness_checks=[later]))
        self.assertEqual(record_views.load_records(self.conn, job['id'])[0]['evidence_checks']['vacancy'], later)

    def test_invalid_legacy_checks_and_archived_targets_never_support_a_current_claim(self):
        import evidence_checks
        now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        person = records.create(self.conn, dict(type='person', name='Retired Contact'))['record']
        records.archive(self.conn, person['id'], dict(revision=person['revision'], reason='Retired'))
        check = dict(scope='vacancy', checked_at='2026-10-17T11:00:00Z', status='confirmed',
                     source_url='https://example.com/jobs/1', basis='Read the current advert.')
        invalids = [None, 'bad', 42, dict(check, checked_at='2026-10-17'),
                    dict(check, checked_at='2026-10-17T12:06:00Z'),
                    dict(check, source_url='javascript:alert(1)'),
                    dict(check, source_url='https://unread.example'),
                    dict(check, scope='contact_route', person_id=person['id'])]
        self.observe(job['id'], 'research:legacy', '2026-10-17',
                     dict(source_urls=[check['source_url']], freshness_checks=invalids))
        with patch.object(evidence_checks, 'utc_now', return_value=now):
            self.assertEqual(record_views.load_records(self.conn, job['id'])[0]['evidence_checks'],
                             dict(vacancy=None, contacts=[]))

    def test_check_projection_queries_stay_batched_as_jobs_grow(self):
        def query_count():
            queries = []
            self.conn.set_trace_callback(queries.append)
            try:
                record_views.load_records(self.conn)
            finally:
                self.conn.set_trace_callback(None)
            return len(queries)
        records.create(self.conn, dict(type='job', name='First Vacancy'))
        before = query_count()
        for i in range(12):
            r = records.create(self.conn, dict(type='job', name=f'Vacancy {i}'))['record']
            self.observe(r['id'], 'research:checks', '2026-10-06', dict(
                source_urls=['https://example.com/jobs/1'], freshness_checks=[dict(scope='vacancy',
                    checked_at='2026-10-06T11:00:00Z', status='unverified',
                    source_url='https://example.com/jobs/1', basis='Source unavailable.')]))
        self.assertEqual(query_count(), before)

    def test_old_malformed_research_cannot_break_the_full_history_check_projection(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        records.add_observation(self.conn, job['id'], 'research:legacy', '2020-01-01', '{bad json')
        for day in range(1, 4):
            self.observe(job['id'], 'research:valid', f'2026-10-0{day}', dict(summary='Valid historical review.'))
        self.assertEqual(record_views.load_records(self.conn, job['id'])[0]['evidence_checks'],
                         dict(vacancy=None, contacts=[]))

    def test_agency_desk_survives_unrelated_reviews_within_existing_three_review_bound(self):
        agency = records.create(self.conn, dict(type='agency', kind='agency', name='Example Desk'))['record']
        self.observe(agency['id'], 'research:desk', '2026-10-01', dict(
            reviewed_at='2026-10-01', desk_summary='Engineering leadership contracts.',
            verification='primary-read', source_urls=['https://example.com/desk'],
            public_contact_route='https://example.com/contact'))
        for day in range(2, 7):
            self.observe(agency['id'], 'research:other', f'2026-10-{day:02}', dict(summary='Unrelated update.'))
        changes = self.conn.total_changes
        view = record_views.load_records(self.conn, agency['id'])[0]
        self.assertEqual([r['observed_at'] for r in view['research']], ['2026-10-06','2026-10-05','2026-10-01'])
        self.assertEqual(view['research'][-1]['public_contact_route'], 'https://example.com/contact')
        self.assertEqual(self.conn.total_changes, changes)
        # A newer explicit withdrawal must replace the positive desk, even after more unrelated reads.
        self.observe(agency['id'], 'research:desk', '2026-10-07', dict(
            reviewed_at='2026-10-07', desk_summary='', verification='unconfirmed'))
        for day in range(8, 12):
            self.observe(agency['id'], 'research:other', f'2026-10-{day:02}', dict(summary='Another update.'))
        reviews = record_views.load_records(self.conn, agency['id'])[0]['research']
        self.assertEqual([r['observed_at'] for r in reviews], ['2026-10-11','2026-10-10','2026-10-07'])
        self.assertEqual(reviews[-1]['desk_summary'], '')

    def test_desk_projection_is_agency_only_and_query_count_does_not_grow_per_record(self):
        def add(kind, name):
            row = records.create(self.conn, dict(type='agency', kind=kind, name=name))['record']
            self.observe(row['id'], 'research:desk', '2026-10-01', dict(desk_summary='Earlier desk.'))
            for day in range(2, 8):
                self.observe(row['id'], 'research:other', f'2026-10-{day:02}', dict(summary='Later update.'))
            return row['id']
        community = add('community', 'Example Community')
        first = add('agency', 'Example Agency')
        def project():
            queries = []
            self.conn.set_trace_callback(queries.append)
            try:
                result = record_views.load_records(self.conn)
            finally:
                self.conn.set_trace_callback(None)
            return result, len(queries)
        views, before = project()
        for i in range(12): add('agency', f'Example Agency {i}')
        views, after = project()
        self.assertEqual(after, before)
        self.assertTrue(all(len(r['research']) == 3 for r in views))
        indexed = {r['id']: r for r in views}
        self.assertEqual([r['observed_at'] for r in indexed[community]['research']], ['2026-10-07','2026-10-06','2026-10-05'])
        self.assertEqual(indexed[first]['research'][-1]['desk_summary'], 'Earlier desk.')

    def test_job_briefing_survives_check_only_reviews_with_original_provenance(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        at = '2026-10-01T09:00:00+01:00'
        self.observe(job['id'], 'research:requirements', at, dict(
            source_date='2026-09-29', source_urls=['https://example.com/jobs/1'],
            facts=['The role leads an integration team.'],
            decisive_unknowns=['Confirm required platform experience.'],
            public_contact_route='The advertised engineering recruitment desk.'))
        for day in range(2, 7):
            self.observe(job['id'], 'research:availability', f'2026-10-{day:02}', dict(
                source_urls=['https://example.com/jobs/1'], freshness_checks=[dict(
                    scope='vacancy', checked_at=f'2026-10-{day:02}T09:00:00Z',
                    status='confirmed', source_url='https://example.com/jobs/1', basis='Apply is visible.')]))
        view = record_views.load_records(self.conn, job['id'])[0]
        brief = view['briefing']
        self.assertEqual(brief['facts'], ['The role leads an integration team.'])
        self.assertEqual(brief['decisive_unknowns'], ['Confirm required platform experience.'])
        self.assertEqual(brief['public_contact_route'], 'The advertised engineering recruitment desk.')
        for field in ('facts', 'decisive_unknowns', 'public_contact_route'):
            self.assertEqual(brief['provenance'][field], dict(source='research:requirements',
                reviewed_at=at, source_date='2026-09-29', source_urls=['https://example.com/jobs/1']))
        self.assertEqual(len(view['research']), 3)
        self.assertTrue(all('facts' not in review for review in view['research']))
        self.assertEqual(view['evidence_checks']['vacancy']['checked_at'], '2026-10-06T09:00:00Z')
        self.assertNotIn('fresh', brief)

    def test_company_contact_route_retains_its_own_review_and_explicit_withdrawal(self):
        company = records.create(self.conn, dict(type='company', name='Example Employer'))['record']
        self.observe(company['id'], 'research:careers', '2026-10-01T09:00:00Z', dict(
            public_contact_route='careers@example.test — general employer route.',
            verification='primary-read', source_urls=['https://example.test/careers']))
        for day in range(2, 7):
            self.observe(company['id'], 'research:background', f'2026-10-{day:02}', dict(summary='Background.'))
        view = record_views.load_records(self.conn, company['id'])[0]
        brief = view['briefing']
        self.assertEqual(brief['public_contact_route'], 'careers@example.test — general employer route.')
        self.assertEqual(brief['provenance']['public_contact_route'], dict(source='research:careers',
            reviewed_at='2026-10-01T09:00:00Z', source_date='',
            source_urls=['https://example.test/careers'], verification='primary-read'))
        self.assertTrue(all('public_contact_route' not in review for review in view['research']))
        self.observe(company['id'], 'research:withdrawn', '2026-10-07', dict(public_contact_route='',
            verification='unverified', source_urls=['https://example.test/careers']))
        brief = record_views.load_records(self.conn, company['id'])[0]['briefing']
        self.assertEqual(brief['public_contact_route'], '')
        self.assertEqual(brief['provenance']['public_contact_route']['verification'], 'unverified')
        self.assertEqual(brief['provenance']['public_contact_route']['reviewed_at'], '2026-10-07')

    def test_job_briefing_fields_refresh_independently_by_actual_observation_instant(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        self.observe(job['id'], 'research:original', '2026-10-01', dict(
            facts=['Original remit.'], decisive_unknowns=['Original unanswered question.'],
            public_contact_route='Original desk.'))
        self.observe(job['id'], 'research:later-facts', '2026-10-02T03:00:00Z', dict(
            facts=['Updated remit.'], source_date='2026-10-01', source_urls=['https://example.com/remit']))
        self.observe(job['id'], 'research:older-import', '2026-10-02T09:00:00+07:00', dict(
            facts=['Older remit imported later.']))
        self.observe(job['id'], 'research:route', '2026-10-03', dict(public_contact_route='New advertised desk.'))
        brief = record_views.load_records(self.conn, job['id'])[0]['briefing']
        self.assertEqual(brief['facts'], ['Updated remit.'])
        self.assertEqual(brief['decisive_unknowns'], ['Original unanswered question.'])
        self.assertEqual(brief['public_contact_route'], 'New advertised desk.')
        self.assertEqual(brief['provenance']['facts']['reviewed_at'], '2026-10-02T03:00:00Z')
        self.assertEqual(brief['provenance']['decisive_unknowns']['source'], 'research:original')
        self.assertEqual(brief['provenance']['public_contact_route']['source'], 'research:route')

    def test_job_briefing_explicit_empty_fields_clear_without_reviving_older_content(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        self.observe(job['id'], 'research:old', '2026-10-01', dict(
            facts=['Old fact.'], decisive_unknowns=['Resolved question.'], public_contact_route='Old desk.'))
        self.observe(job['id'], 'research:clear', '2026-10-02', dict(
            facts=[], decisive_unknowns=[], public_contact_route='', source_urls=[]))
        for day in range(3, 8):
            self.observe(job['id'], 'research:check-only', f'2026-10-{day:02}', dict(freshness_checks=[]))
        brief = record_views.load_records(self.conn, job['id'])[0]['briefing']
        self.assertEqual(brief['facts'], [])
        self.assertEqual(brief['decisive_unknowns'], [])
        self.assertEqual(brief['public_contact_route'], '')
        self.assertEqual(set(brief['provenance']), {'facts', 'decisive_unknowns', 'public_contact_route'})
        self.assertTrue(all(p['source'] == 'research:clear' for p in brief['provenance'].values()))

    def test_job_briefing_ignores_malformed_legacy_fields_and_payloads(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        self.observe(job['id'], 'research:valid', '2026-10-01', dict(
            facts=['Supported fact.'], decisive_unknowns=['Still unknown.'], public_contact_route='Valid desk.'))
        records.add_observation(self.conn, job['id'], 'research:broken-json', '2026-10-02', '{bad json')
        for index, payload in enumerate((None, [], 'legacy text', dict(
                facts=['Not a valid list', 7], decisive_unknowns=None, public_contact_route=42)), 3):
            self.observe(job['id'], f'research:malformed-{index}', f'2026-10-{index:02}', payload)
        # General research still has its existing three-item contract; malformed older rows must
        # not make the independent full-history briefing scan fail.
        for day in range(7, 11):
            self.observe(job['id'], 'research:check-only', f'2026-10-{day:02}', dict(freshness_checks=[]))
        brief = record_views.load_records(self.conn, job['id'])[0]['briefing']
        self.assertEqual(brief['facts'], ['Supported fact.'])
        self.assertEqual(brief['decisive_unknowns'], ['Still unknown.'])
        self.assertEqual(brief['public_contact_route'], 'Valid desk.')

    def test_job_briefing_is_bounded_and_source_links_are_safe(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        self.observe(job['id'], 'research:large', '2026-10-01', dict(
            facts=['x' * 1500] * 25, decisive_unknowns=['y' * 1500] * 25,
            public_contact_route='z' * 2500, source_date='not a date',
            source_urls=['javascript:alert(1)', 'https://user:password@example.com/private',
                         *[f'https://example.com/source/{i}' for i in range(20)]]))
        brief = record_views.load_records(self.conn, job['id'])[0]['briefing']
        for field in ('facts', 'decisive_unknowns'):
            self.assertEqual(len(brief[field]), 20)
            self.assertTrue(all(len(value) == 1000 for value in brief[field]))
        self.assertEqual(len(brief['public_contact_route']), 2000)
        for provenance in brief['provenance'].values():
            self.assertEqual(len(provenance['source_urls']), 10)
            self.assertTrue(all(url.startswith('https://example.com/source/') for url in provenance['source_urls']))
            self.assertEqual(provenance['source_date'], '')

    def test_job_briefing_empty_defaults_do_not_claim_supplied_fields_or_affect_people(self):
        job = records.create(self.conn, dict(type='job', name='Example Vacancy'))['record']
        person = records.create(self.conn, dict(type='person', name='Example Person'))['record']
        self.assertEqual(record_views.load_records(self.conn, job['id'])[0]['briefing'], dict(
            facts=[], decisive_unknowns=[], public_contact_route='', provenance={}))
        self.assertNotIn('briefing', record_views.load_records(self.conn, person['id'])[0])

    def test_observation_chronology_uses_utc_and_keeps_original_timestamp(self):
        cases = [
            ('offset', '2026-10-04T09:00:00+07:00', '2026-10-04T03:00:00Z', 1),
            ('equal', '2026-10-04T09:00:00+07:00', '2026-10-04T02:00:00Z', 1),
            ('fraction', '2026-10-04T03:00:00Z', '2026-10-04T03:00:00.001Z', 1),
            ('date', '2026-10-04', '2026-10-04T00:00:00Z', 1),
            ('invalid', '2026-10-04', 'not-a-time', 0),
            ('invalid-offset', '2026-10-04', '9999-12-31T23:59:59-01:00', 0),
            ('backward', '2026-10-04T03:00:00Z', '2026-10-04T09:00:00+07:00', 0),
        ]
        for name, first, second, expected in cases:
            with self.subTest(name=name):
                r = records.create(self.conn, dict(type='job', name=name))['record']
                stamps = [first, second]
                for i, at in enumerate(stamps):
                    for source in ('research:test', 'news:test', 'LinkedIn Connections CSV', 'board:test'):
                        self.observe(r['id'], source, at, dict(title=str(i), observed_company=str(i)))
                view = record_views.load_records(self.conn, r['id'])[0]
                self.assertEqual(view['research'][0]['title'], str(expected))
                self.assertEqual(view['signals'][0]['title'], str(expected))
                self.assertEqual(view['linkedin']['observed_company'], str(expected))
                self.assertEqual(view['advert']['title'], str(expected))
                self.assertEqual(view['advert']['observed_at'], stamps[expected])
                self.assertEqual(record_views.last_sightings(self.conn)[r['id']], stamps[expected])

    def test_repeated_scanner_content_advances_only_to_a_strictly_later_instant(self):
        cases = [
            ('offset', '2026-10-04T09:00:00+07:00', '2026-10-04T03:00:00Z', True),
            ('equal', '2026-10-04T09:00:00+07:00', '2026-10-04T02:00:00Z', False),
            ('fraction', '2026-10-04T03:00:00Z', '2026-10-04T03:00:00.001Z', True),
            ('date', '2026-10-04', '2026-10-04T00:00:00Z', False),
            ('invalid', '2026-10-04', 'not-a-time', False),
            ('invalid-offset', '2026-10-04', '9999-12-31T23:59:59-01:00', False),
            ('valid', 'not-a-time', '2026-10-04', True),
            ('backward', '2026-10-04T03:00:00Z', '2026-10-04T09:00:00+07:00', False),
        ]
        for name, first, second, advances in cases:
            with self.subTest(name=name):
                r = records.create(self.conn, dict(type='job', name=name))['record']
                payload = lambda at: json.dumps(dict(title='Same advert', last_seen_at=at))
                records.add_observation(self.conn, r['id'], 'board:test', first, payload(first))
                result = records.add_observation(self.conn, r['id'], 'board:test', second, payload(second))
                rows = self.conn.execute('SELECT observed_at,payload_json FROM observations WHERE record_id=?', (r['id'],)).fetchall()
                expected = second if advances else first
                self.assertEqual(result.rowcount, int(advances))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['observed_at'], expected)
                self.assertEqual(json.loads(rows[0]['payload_json'])['last_seen_at'], expected)

    def test_latest_linkedin_metadata_uses_both_import_shapes_and_excludes_private_content(self):
        p = records.create(self.conn, dict(type='person', name='Example Person'))['record']
        evidence = dict(threads=dict(two_way=1, last_two_way='2026-09-24', sent=2, received=1,
                                    subject='PRIVATE', body='PRIVATE', conversation_ids=['PRIVATE']),
                        endorsed_you=1, recommendations=['received'], email='PRIVATE')
        self.observe(p['id'], 'LinkedIn Connections CSV', '2026-09-01',
                     dict(observed_company='Earlier Co', observed_role='Engineer', email='PRIVATE', evidence=evidence))
        self.assertEqual(record_views.load_records(self.conn, p['id'])[0]['linkedin']['observed_company'], 'Earlier Co')
        self.observe(p['id'], 'linkedin-export:2026-10-01', '2026-10-01',
                     dict(company='Current Co', position='CTO', flags=['former_employer'], email='PRIVATE', evidence=evidence))
        metadata = record_views.load_records(self.conn, p['id'])[0]['linkedin']
        self.assertEqual(metadata['observed_company'], 'Current Co')
        self.assertEqual(metadata['observed_role'], 'CTO')
        self.assertEqual(metadata['flags'], ['former_employer'])
        self.assertEqual(metadata['evidence']['threads'], dict(two_way=1,last_two_way='2026-09-24',sent=2,received=1))
        self.assertNotIn('PRIVATE', json.dumps(metadata))

    def test_commercial_signals_keep_latest_native_identity_and_original_evidence(self):
        r = records.create(self.conn, dict(type='company', name='Supplier'))['record']
        self.assertEqual(record_views.load_records(self.conn, r['id'])[0]['commercial_signals'], [])
        def award(source_id, at, **fields):
            current = dict(signal_type='contract_award', title='Delivery award', supplier='Supplier',
                           buyer='Public buyer', value=125000, currency='GBP', eligible=True, **fields)
            self.observe(r['id'], 'award:contracts-finder', at, dict(kind='commercial_signal',
                source_scope='award:contracts-finder', source_id=source_id, last_seen_at=at, current=current))
        award('award-1', '2026-10-04T09:00:00+07:00', url='https://example.com/old')
        award('award-1', '2026-10-04T03:00:00Z', url='https://example.com/current', status='expired')
        award('award-1', 'not-a-date', url='https://example.com/invalid')
        award('award-2', '', url='javascript:alert(1)')
        signals = record_views.load_records(self.conn, r['id'])[0]['commercial_signals']
        self.assertEqual(len(signals), 2)
        current = signals[0]
        self.assertEqual((current['source_scope'], current['source_id'], current['observed_at']),
                         ('award:contracts-finder', 'award-1', '2026-10-04T03:00:00Z'))
        self.assertEqual((current['url'], current['buyer'], current['value'], current['status']),
                         ('https://example.com/current', 'Public buyer', 125000, 'expired'))
        self.assertEqual(signals[1]['url'], '')
        self.assertNotIn('current', current)
        self.assertNotIn('commercial_signals', records.csv_rows(self.conn)[0])

    def test_commercial_signal_limit_follows_identity_deduplication(self):
        r = records.create(self.conn, dict(type='company', name='Supplier'))['record']
        for i in range(12):
            self.observe(r['id'], 'award:test', f'2026-10-{i + 1:02d}', dict(
                source_scope='award:test', source_id=str(i), current=dict(title=f'Award {i}', eligible=True)))
        for i in range(12):
            self.observe(r['id'], 'award:test', f'2026-11-{i + 1:02d}', dict(
                source_scope='award:test', source_id='11', current=dict(title=f'Latest {i}', eligible=False)))
        signals = record_views.load_records(self.conn, r['id'])[0]['commercial_signals']
        self.assertEqual(len(signals), 10)
        self.assertEqual([s['source_id'] for s in signals], [str(i) for i in range(11, 1, -1)])
        self.assertEqual(signals[0]['title'], 'Latest 11')
        self.assertFalse(signals[0]['eligible'])

    def test_award_resightings_advance_freshness_without_losing_changes_or_identity(self):
        r = records.create(self.conn, dict(type='company', name='Supplier'))['record']
        def observe(identity, at, eligible=True):
            return records.add_observation(self.conn, r['id'], 'award:test', at, json.dumps(dict(
                source_scope='award:test', source_id=identity, last_seen_at=at, last_observation_at=at,
                current=dict(signal_type='contract_award', title='Delivery award', eligible=eligible))))
        observe('first', '2026-10-04T09:00:00+07:00')
        observe('first', '2026-10-04T03:00:00Z')
        rows = self.conn.execute('SELECT * FROM observations WHERE record_id=?', (r['id'],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['observed_at'], '2026-10-04T03:00:00Z')
        self.assertEqual(json.loads(rows[0]['payload_json'])['last_observation_at'], rows[0]['observed_at'])
        self.assertEqual(observe('first', '2026-10-04T04:00:00+01:00').rowcount, 0)
        self.assertEqual(observe('first', '2026-10-04T09:00:00+07:00').rowcount, 0)
        observe('first', '2026-10-05', eligible=False)
        observe('second', '2026-10-05')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations WHERE record_id=?', (r['id'],)).fetchone()[0], 3)
        signals = {s['source_id']: s for s in record_views.load_records(self.conn, r['id'])[0]['commercial_signals']}
        self.assertFalse(signals['first']['eligible'])
        self.assertTrue(signals['second']['eligible'])

    def test_latest_scanner_advert_supports_wrapped_and_direct_payloads_without_research_overwrite(self):
        r = records.create(self.conn, dict(type='job', name='Software Engineering Manager'))['record']
        self.observe(r['id'], 'board:example', '2026-09-29',
                     dict(title='Earlier', text='Earlier advert', employment_type='contract'))
        self.observe(r['id'], 'feed:example', '2026-10-01',
                     dict(current=dict(title='Latest', text='Permanent role', employment_type='contract',
                                       url='https://example.com/job', secret='PRIVATE'), previous={'text':'Old'}))
        self.observe(r['id'], 'research:example.json', '2026-10-02', dict(signal='Research is not an advert'))
        advert = record_views.load_records(self.conn, r['id'])[0]['advert']
        self.assertEqual(advert['text'], 'Permanent role')
        self.assertEqual(advert['source'], 'feed:example')
        self.assertEqual(advert['employment_type'], 'contract')
        self.assertNotIn('PRIVATE', json.dumps(advert))
        self.assertEqual(record_views.last_sightings(self.conn)[r['id']], '2026-10-01')

    def test_availability_comes_from_the_latest_listing_and_feeds_the_brief_the_same_answer(self):
        import evidence_checks
        r = records.create(self.conn, dict(type='job', name='QA Lead', summary='Lead QA.', engagement='contract'))['record']
        now = dt.datetime.now(dt.timezone.utc)
        seen = (now - dt.timedelta(hours=5)).isoformat(timespec='seconds')
        self.observe(r['id'], 'feed:adzuna:London', seen, dict(title='QA Lead', text='Contract QA lead'))
        self.observe(r['id'], 'research:refused.json', now.isoformat(timespec='seconds'), dict(signal='Not an advert'))
        view = record_views.load_records(self.conn, r['id'])[0]
        until = (dt.datetime.fromisoformat(seen) + dt.timedelta(hours=48)).isoformat()
        self.assertEqual(view['availability'], dict(status='open', basis='listing', at=seen, source='feed:adzuna', until=until))
        self.assertNotIn('vacancy', [g['code'] for g in view['evidence_brief']['gaps']])
        # A research-side consumer recomputing from the same record reaches the same answer.
        self.assertEqual(evidence_checks.advert_availability(view)['status'], view['availability']['status'])
        person = records.create(self.conn, dict(type='person', name='Alex Example'))['record']
        self.assertNotIn('availability', record_views.load_records(self.conn, person['id'])[0])

    def test_an_advert_no_source_lists_stays_unconfirmed_and_keeps_its_vacancy_gap(self):
        r = records.create(self.conn, dict(type='job', name='QA Lead', summary='Lead QA.'))['record']
        view = record_views.load_records(self.conn, r['id'])[0]
        self.assertEqual(view['availability']['status'], 'unconfirmed')
        self.assertIn('vacancy', [g['code'] for g in view['evidence_brief']['gaps']])

    def test_timeline_retains_closed_decision_beyond_summary_window(self):
        r = records.create(self.conn, dict(type='company',name='Example Co'))['record']
        d = records.create_decision(self.conn,dict(subject=r['id'],kind='approve',question='Investigate?'))['decision']
        records.process_decision(self.conn,d['id'],'withdraw','Superseded',revision=d['revision'])
        self.conn.execute("UPDATE decisions SET updated_at='2020-01-01' WHERE id=?",(d['id'],))
        timeline = record_views.timeline(self.conn,r['id'])
        self.assertEqual(timeline['decisions'][0]['id'], d['id'])
        self.assertEqual(timeline['decisions'][0]['outcome'], 'Superseded')


if __name__ == '__main__':
    unittest.main()

class EmailStatusTests(unittest.TestCase):
    setUp = EvidenceProjectionTests.setUp
    tearDown = EvidenceProjectionTests.tearDown
    def add_run(self, at, status, **extra):
        payload=dict(started_at=at, finished_at=at, status=status, **extra)
        self.conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
            ('email:'+at,at[:10],status,json.dumps(payload)))
        self.conn.commit()

    def test_never_run_is_explicit_and_failure_does_not_hide_last_success(self):
        self.assertEqual(record_views.email_status(self.conn)['status'],'not_started')
        self.add_run('2026-10-17T12:00:00Z','ok',collection={'pending':0,'last_received_at':None},processing={'messages':0})
        self.add_run('2026-10-17T12:15:00Z','failed',message='Retained for retry')
        value=record_views.email_status(self.conn)
        self.assertEqual(value['status'],'failed')
        self.assertEqual(value['last_success'],'2026-10-17T12:00:00Z')
        self.assertIsNone(value['last_alert_processed'])

    def test_empty_poll_preserves_last_real_alert_and_hides_private_payload(self):
        self.add_run('2026-10-17T12:00:00Z','ok',processing={'messages':1,'jobs':5},secret='must not leave')
        self.add_run('2026-10-17T12:15:00Z','ok',processing={'messages':0})
        value=record_views.email_status(self.conn)
        self.assertEqual(value['last_alert_processed'],'2026-10-17T12:00:00Z')
        self.assertNotIn('secret',json.dumps(value))

    def test_last_import_counts_survive_empty_polls_without_leaking_identity_lists(self):
        self.add_run('2026-10-17T12:00:00Z', 'ok', processing={'messages':1,'jobs':6,'eligible':6},
                     **{'import': {'capacity': {'admitted': [], 'selected':['private-job'],
                         'deferred': {'seven_day_limit':6, 'private-key':999}, 'allowance_remaining':0},
                         'private':'must not leave'}})
        self.add_run('2026-10-17T12:15:00Z', 'ok', processing={'messages':0},
                     **{'import': {'capacity': {'admitted': [], 'deferred': {}, 'allowance_remaining':0}}})
        value = record_views.email_status(self.conn)
        self.assertEqual(value['last_import'], dict(finished_at='2026-10-17T12:00:00Z',
            messages=1, jobs=6, eligible=6, admitted=0, allowance_remaining=0,
            deferred={'working_limit':0,'seven_day_limit':6,'unresolved_stale':0,'not_recommended':0}))
        self.assertFalse(value['capacity_warning'])
        self.assertNotIn('private',json.dumps(value))

    def test_latest_real_import_replaces_old_deferrals_even_when_collection_is_partial(self):
        self.add_run('2026-10-17T12:00:00Z','ok',processing={'messages':1},
                     **{'import':{'capacity':{'admitted':[], 'deferred':{'working_limit':6}, 'allowance_remaining':0}}})
        self.add_run('2026-10-17T12:15:00Z','partial',processing={'messages':1,'jobs':2,'eligible':2},
                     **{'import':{'capacity':{'admitted':['private-one','private-two'], 'deferred':{}, 'allowance_remaining':8}}})
        value=record_views.email_status(self.conn)['last_import']
        self.assertEqual((value['finished_at'],value['admitted'],value['allowance_remaining']),
                         ('2026-10-17T12:15:00Z',2,8))
        self.assertEqual(sum(value['deferred'].values()),0)

    def test_legacy_alert_import_leaves_capacity_unknown(self):
        self.add_run('2026-10-17T12:00:00Z','ok',processing={'messages':1}, **{'import':{'new':1}})
        value=record_views.email_status(self.conn)['last_import']
        self.assertIsNone(value['admitted'])
        self.assertIsNone(value['allowance_remaining'])
        self.assertIsNone(value['deferred'])

    def test_failed_or_skipped_import_does_not_replace_completed_admission_counts(self):
        self.add_run('2026-10-17T12:00:00Z','ok',processing={'messages':1},
                     **{'import':{'capacity':{'admitted':[], 'deferred':{'working_limit':3}, 'allowance_remaining':4}}})
        self.add_run('2026-10-17T12:15:00Z','failed',processing={'messages':1}, **{'import':{'error':'private error'}})
        self.add_run('2026-10-17T12:30:00Z','partial',processing={'messages':1}, **{'import':{},'import_skipped':'lock'})
        value=record_views.email_status(self.conn)['last_import']
        self.assertEqual(value['finished_at'],'2026-10-17T12:00:00Z')
        self.assertEqual(value['deferred']['working_limit'],3)
