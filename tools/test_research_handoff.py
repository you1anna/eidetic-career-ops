"""Synthetic research handoffs: identity, receipts, history and protected workflow."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent / 'workspace')]
import db
import records
import record_imports


class ResearchHandoffTests(unittest.TestCase):
    def test_recovered_alert_preserves_email_clock_without_public_verification(self):
        import record_views
        import research_intake
        url = 'https://www.linkedin.com/jobs/view/123456/'
        entry = self.finding(source_urls=[url], source_date='2026-10-05T08:00:00Z',
                            create=dict(type='job', name='Technical Lead', organisation='Example Co', url=url),
                            alert_evidence=dict(source='alert:linkedin', received_at='2026-10-05T08:00:00Z', url=url))
        first = self.run_batch(entry)
        rid = first['items'][0]['record_id']
        row = record_views.load_records(self.conn, rid)[0]
        self.assertEqual(row['source_context'], dict(source='alert:linkedin', url=url,
                         at='2026-10-05T08:00:00Z', basis='email'))
        self.assertFalse(row.get('date'))
        self.assertNotEqual(row['availability']['status'], 'open')
        self.assertEqual(self.run_batch(entry)['accepted'], 0)
        for changes in ({'source':'board:example'}, {'received_at':'not a timestamp'},
                        {'url':'https://www.linkedin.com/jobs/view/999/'}, {'extra':'bad'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                research_intake.validate([dict(entry, alert_evidence={**entry['alert_evidence'], **changes})])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'researcher.json'
        self.conn = db.connect(Path(self.tmp.name) / 'test.db')
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def finding(self, **changes):
        entry = dict(finding_id='vacancy-1', reviewed_at='2026-10-06T10:00:00Z',
                     source_date='2026-10-05', source_urls=['https://example.com/jobs/1'],
                     create=dict(type='job', name='Head of Quality', organisation='Example Co',
                                 url='https://example.com/jobs/1', location='London'),
                     summary='An advertised quality leadership role.',
                     facts=['The advert names London.'], assumptions=['Scope may include team leadership.'],
                     decisive_unknowns=['Confirm team size.'], suggested_task='Research the team and requirements')
        entry.update(changes)
        return entry

    def run_batch(self, *entries):
        self.path.write_text(json.dumps(entries))
        return record_imports.import_research(self.conn, self.path)

    def freshness_check(self, **changes):
        check = dict(scope='vacancy', checked_at='2026-10-06T09:15:00+01:00', status='confirmed',
                     source_url='https://example.com/jobs/1', basis='The current advert accepts applications.')
        check.update(changes)
        return check

    def test_typed_checks_preserve_evidence_without_inventing_links_or_commercial_state(self):
        person = records.create(self.conn, dict(type='person', name='Example Contact'))['record']
        company = records.create(self.conn, dict(type='company', name='Example Employer'))['record']
        contact = self.freshness_check(scope='contact_route', person_id=person['id'],
            organisation_id=company['id'], status='unverified', basis='The profile could not be read.')
        finding = self.finding(freshness_checks=[self.freshness_check(), contact])
        result = self.run_batch(finding)
        job = records.get(self.conn, result['items'][0]['record_id'])
        self.assertEqual(job['evidence_checks'], dict(vacancy=finding['freshness_checks'][0], contacts=[contact]))
        self.assertEqual(job['status'], 'candidate')
        self.assertEqual(job['summary'], finding['summary'])
        self.assertNotEqual(job['verification_level'], 'primary-read')
        self.assertIsNone(self.conn.execute('SELECT 1 FROM links WHERE from_id=? OR to_id=?',
                                            (person['id'], person['id'])).fetchone())
        before = list(self.conn.iterdump())
        self.assertEqual(self.run_batch(finding)['accepted'], 0)
        self.assertEqual(before, list(self.conn.iterdump()))

    def test_invalid_typed_checks_roll_back_the_entire_batch(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)).isoformat()
        invalids = [None, {}, [42], [self.freshness_check(scope='email')],
            [self.freshness_check(status='fresh')], [self.freshness_check(basis='  ')],
            [self.freshness_check(checked_at='2026-10-06')],
            [self.freshness_check(checked_at='2026-10-06T09:15:00')],
            [self.freshness_check(checked_at='2026-10-06T09:15:00+01:60')],
            [self.freshness_check(checked_at='2026-02-30T09:15:00Z')],
            [self.freshness_check(checked_at=future)],
            [self.freshness_check(source_url='https://unread.example')],
            [self.freshness_check(source_url='javascript:alert(1)')],
            [self.freshness_check(source_url='https://user:pass@example.com')],
            [self.freshness_check(person_id='CP-extra')],
            [self.freshness_check(organisation_id='CO-extra')],
            [self.freshness_check(scope='contact_route')],
            [self.freshness_check(scope='contact_route', person_id='CP-absent', status='closed')],
            [self.freshness_check(invented_field=True)]]
        for checks in invalids:
            with self.subTest(checks=checks):
                before = list(self.conn.iterdump())
                with self.assertRaises(ValueError):
                    self.run_batch(self.finding(), self.finding(freshness_checks=checks))
                self.assertEqual(before, list(self.conn.iterdump()))

    def test_typed_contact_checks_require_active_person_and_optional_organisation(self):
        person = records.create(self.conn, dict(type='person', name='Example Person'))['record']
        company = records.create(self.conn, dict(type='company', name='Example Company'))['record']
        archived = records.create(self.conn, dict(type='person', name='Archived Person'))['record']
        records.archive(self.conn, archived['id'], dict(revision=archived['revision'], reason='Retired'))
        archived_org = records.create(self.conn, dict(type='agency', name='Archived Agency'))['record']
        records.archive(self.conn, archived_org['id'], dict(revision=archived_org['revision'], reason='Retired'))
        contact = self.freshness_check(scope='contact_route', person_id=person['id'])
        invalids = [dict(contact, person_id='missing'), dict(contact, person_id=company['id']),
                    dict(contact, person_id=archived['id']), dict(contact, organisation_id='missing'),
                    dict(contact, organisation_id=person['id']), dict(contact, organisation_id=archived_org['id'])]
        for check in invalids:
            with self.subTest(check=check), self.assertRaises(ValueError):
                self.run_batch(self.finding(freshness_checks=[check]))
        for target in (person, company):
            with self.subTest(target=target['type']), self.assertRaises(ValueError):
                self.run_batch(dict(id=target['id'], reviewed_at='2026-10-06',
                    source_urls=['https://example.com/jobs/1'], freshness_checks=[self.freshness_check()]))
        self.assertEqual(self.run_batch(self.finding(freshness_checks=[contact]))['accepted'], 1)

    def test_five_minute_clock_skew_is_explicit_and_bounded(self):
        import evidence_checks
        now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
        with patch.object(evidence_checks, 'utc_now', return_value=now):
            self.assertEqual(self.run_batch(self.finding(freshness_checks=[self.freshness_check(
                checked_at='2026-10-17T12:05:00Z')]))['accepted'], 1)
            with self.assertRaises(ValueError):
                self.run_batch(self.finding(freshness_checks=[self.freshness_check(
                    checked_at='2026-10-17T12:05:00.001Z')]))

    def route_finding(self):
        c = records.create(self.conn, dict(type='company', name='Example Route Company'))['record']
        p = records.create(self.conn, dict(type='person', name='Example Leader', role='CTO',
            verification_level='primary-read', evidence_urls='https://route.example/team', date=dt.date.today().isoformat()))['record']
        records.link(self.conn, dict(from_id=p['id'], to_id=c['id'], relation='works_at', revision=p['revision']))
        return dict(id=c['id'], reviewed_at=dt.date.today().isoformat(), source_date=dt.date.today().isoformat(),
                    verification='primary-read', source_urls=['https://route.example/team'], outreach_signals=[dict(
                        kind='leadership_change', person_id=p['id'], started_on=dt.date.today().isoformat(),
                        text='The CTO leads software engineering and release readiness.', url='https://route.example/team')])

    def test_a_prepared_approach_lands_on_a_person_and_malformed_ones_are_refused(self):
        p = records.create(self.conn, dict(type='person', name='Example Friend'))['record']
        c = records.create(self.conn, dict(type='company', name='Example Approach Co'))['record']
        approach = dict(role='connector', points=['Congratulate them on the award.', 'Ask how hiring is going.'],
                        ask='Who leads quality engineering there?', cv='QE', goal='Two named introductions', wave=1,
                        last='Mar 2026, email: they offered to meet in April.')
        good = dict(id=p['id'], reviewed_at='2026-10-15', source_urls=['https://example.com/about'],
                    summary='Head of QA at Example (per LinkedIn, Sep 2026).', verification='unconfirmed', approach=approach)
        self.assertEqual(self.run_batch(good)['accepted'], 1)
        self.assertEqual(records.get(self.conn, p['id'])['summary'], good['summary'])
        for bad in (dict(good, id=c['id']),                                   # prepared approaches are for people
                    dict(good, approach=dict(approach, role='friend')),
                    dict(good, approach=dict(approach, cv='Nope')),
                    dict(good, approach=dict(approach, points=['One.', 'Two.', 'Three.'])),
                    dict(good, approach=dict(approach, points=['x' * 241])),
                    dict(good, approach=dict(approach, draft='Hello Sam, …')),   # talking points, never a draft
                    dict(good, approach=dict(approach, wave=12)),
                    dict(good, approach=dict(approach, last='x' * 241)),        # one line, not a thread
                    dict(good, approach='connector')):
            with self.subTest(bad=bad.get('approach') if bad['id'] == p['id'] else 'company'):
                with self.assertRaises(ValueError):
                    self.run_batch(dict(bad, reviewed_at='2026-10-16'))

    def test_outreach_evidence_import_replay_projection_and_original_outcome_context(self):
        finding = self.route_finding()
        self.assertEqual(self.run_batch(finding)['accepted'], 1)
        self.assertEqual(self.run_batch(finding)['accepted'], 0)
        projected = records.state(self.conn)
        self.assertNotIn('intelligence_error', projected)
        action = next(a for a in projected['intelligence']['actions'] if a['company_id'] == finding['id'])
        self.assertEqual(action['brief']['approach_route']['kind'], 'new_leader')
        rid = action['record_id']
        outcome = records.outcome(self.conn, rid, dict(kind='sent', action_id=action['id'],
                                  revision=records.get(self.conn, rid)['revision']))['outcome']
        self.assertEqual(outcome['snapshot']['brief']['approach_route']['kind'], 'new_leader')
        withdrawal = dict(finding, reviewed_at=dt.date.today().isoformat() + 'T23:59:00Z', outreach_signals=[])
        self.run_batch(withdrawal)
        updated = records.state(self.conn)
        tracked = next(a for a in updated['intelligence']['actions'] if a['id'] == action['id'])
        self.assertEqual(tracked['progress']['status'], 'waiting')
        self.assertEqual(tracked['brief']['approach_route']['kind'], 'new_leader')

    def test_invalid_outreach_evidence_rolls_back_whole_batch(self):
        finding = self.route_finding()
        invalids = [dict(finding, verification='search-verified'), dict(finding, outreach_signals={}),
                    dict(finding, outreach_signals=[dict(finding['outreach_signals'][0], started_on='2026-02-31')]),
                    dict(finding, outreach_signals=[dict(finding['outreach_signals'][0], person_id='missing')]),
                    dict(finding, outreach_signals=[dict(finding['outreach_signals'][0], url='https://unread.example')]),
                    dict(finding, outreach_signals=[dict(kind='delivery_partner', accepts_specialists='yes',
                        text='A supplier route', url='https://route.example/team')])]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                before = list(self.conn.iterdump())
                with self.assertRaises(ValueError):
                    self.run_batch(self.finding(), invalid)
                self.assertEqual(before, list(self.conn.iterdump()))

    def test_signal_cannot_reassign_a_person_to_another_company(self):
        finding = self.route_finding()
        other = records.create(self.conn, dict(type='company', name='Unrelated Example'))['record']
        with self.assertRaisesRegex(ValueError, 'linked to this company'):
            self.run_batch(dict(finding, id=other['id']))

    def test_new_job_returns_record_receipt_and_replay_changes_nothing(self):
        result = self.run_batch(self.finding())
        item = result['items'][0]
        self.assertEqual(item['finding_id'], 'vacancy-1')
        self.assertEqual((item['outcome'], item['task']), ('created', 'suggested'))
        job = records.get(self.conn, item['record_id'])
        self.assertEqual((job['type'], job['status'], job['owner'], job['working_state']),
                         ('job', 'candidate', 'agent', 'needs_review'))
        self.assertEqual(job['research'][0]['facts'], self.finding()['facts'])
        before = list(self.conn.iterdump())
        again = self.run_batch(self.finding())
        self.assertEqual((again['items'][0]['record_id'], again['items'][0]['outcome']), (job['id'], 'unchanged'))
        self.assertEqual(before, list(self.conn.iterdump()))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM links WHERE relation='hiring_for'").fetchone()[0], 1)

    def test_existing_exact_url_matches_and_owner_edits_and_task_win(self):
        job = records.create(self.conn, dict(type='job', name='My name', summary='My assessment',
                              url='https://example.com/jobs/1'))['record']
        records.followup(self.conn, dict(id=job['id'], revision=job['revision'], action='My next step',
                                        owner='owner', status='paused'))
        result = self.run_batch(self.finding())
        self.assertEqual((result['items'][0]['record_id'], result['items'][0]['task']), (job['id'], 'preserved'))
        after = records.get(self.conn, job['id'])
        self.assertEqual((after['name'], after['summary'], after['next_action'], after['working_state']),
                         ('My name', 'My assessment', 'My next step', 'paused'))
        self.assertTrue(result['items'][0]['conflicts'])

    def test_historical_research_summary_cannot_borrow_a_recent_job_date(self):
        today = dt.date.today().isoformat()
        summary = 'Engineering leadership of a software platform &amp;  release readiness.'
        for field in ('summary', 'desk_summary'):
            with self.subTest(field=field):
                job = records.create(self.conn, dict(type='job', name='Head of Engineering',
                    date=today, location='London', engagement='contract',
                    url=f'https://example.com/jobs/{field}'))['record']
                self.run_batch(dict(id=job['id'], reviewed_at=today, source_date='2024-05-10',
                    source_urls=['https://example.com/historical-thread'], **{field: summary}))
                projected = records.state(self.conn)
                self.assertNotIn('intelligence_error', projected)
                record = next(r for r in projected['records'] if r['id'] == job['id'])
                self.assertEqual(record['summary'], summary.replace('&amp;', '&'))
                self.assertEqual(record['date'], today)
                self.assertEqual(record['research'][0]['observed_at'], today)
                advice = projected['intelligence']
                self.assertEqual([a['stage'] for a in advice['actions'] if a['record_id'] == job['id']], [])
                item = next(a for a in advice['research'] if a['record_id'] == job['id'])
                self.assertEqual([(f['url'], f['date']) for f in item['brief']['facts']],
                                 [('https://example.com/historical-thread', '2024-05-10')])

    def test_current_scanner_body_independently_supports_a_historical_research_job(self):
        today = dt.date.today().isoformat()
        summary = 'Engineering leadership of a software platform and release readiness.'
        url = 'https://example.com/jobs/current'
        job = records.create(self.conn, dict(type='job', name='Head of Engineering',
            date=today, location='London', engagement='contract', url=url))['record']
        self.run_batch(dict(id=job['id'], reviewed_at=today, source_date='2024-05-10',
            source_urls=['https://example.com/historical-thread'], summary=summary))
        records.add_observation(self.conn, job['id'], 'board:greenhouse:example', today,
                                json.dumps(dict(url=url, title='Head of Engineering', text=summary)))
        projected = records.state(self.conn)
        self.assertNotIn('intelligence_error', projected)
        item = next(a for a in projected['intelligence']['actions'] if a['record_id'] == job['id'])
        self.assertEqual(item['stage'], 'ready')
        self.assertEqual([(f['url'], f['date']) for f in item['brief']['facts']], [(url, today)])

    def test_historical_summary_stays_unfresh_after_later_findings_hide_its_source(self):
        today = dt.date.today().isoformat()
        summary = 'Engineering leadership of a software platform and release readiness.'
        url = 'https://example.com/jobs/research-history'
        job = records.create(self.conn, dict(type='job', name='Head of Engineering',
            date=today, location='London', engagement='contract', url=url))['record']
        self.run_batch(dict(id=job['id'], reviewed_at=today, source_date='2024-05-10',
            source_urls=['https://example.com/historical-thread'], summary=summary))
        for hour in range(1, 4):
            self.run_batch(dict(id=job['id'], reviewed_at=f'{today}T0{hour}:00:00Z',
                source_date='2024-05-10', source_urls=[f'https://example.com/history/{hour}'],
                facts=[f'Historical supporting fact {hour}.']))
        projected = records.state(self.conn)
        self.assertNotIn('intelligence_error', projected)
        record = next(r for r in projected['records'] if r['id'] == job['id'])
        self.assertEqual(record['summary'], summary)
        self.assertEqual(len(record['research']), 3)
        self.assertTrue(all('summary' not in finding for finding in record['research']))
        advice = projected['intelligence']
        self.assertEqual([a['stage'] for a in advice['actions'] if a['record_id'] == job['id']], [])
        item = next(a for a in advice['research'] if a['record_id'] == job['id'])
        self.assertEqual([(f['url'], f['date']) for f in item['brief']['facts']], [(url, '')])

    def test_manual_summary_without_research_keeps_its_advert_date(self):
        today = dt.date.today().isoformat()
        url = 'https://example.com/jobs/manual'
        job = records.create(self.conn, dict(type='job', name='Head of Engineering',
            date=today, location='London', engagement='contract', url=url,
            summary='Engineering leadership of a software platform and release readiness.'))['record']
        projected = records.state(self.conn)
        self.assertNotIn('intelligence_error', projected)
        item = next(a for a in projected['intelligence']['actions'] if a['record_id'] == job['id'])
        self.assertEqual([(f['url'], f['date']) for f in item['brief']['facts']], [(url, today)])

    def test_current_research_summary_uses_its_source_date_instead_of_job_date(self):
        today = dt.date.today()
        source_date = (today - dt.timedelta(days=1)).isoformat()
        job = records.create(self.conn, dict(type='job', name='Head of Engineering',
            date=today.isoformat(), location='London', engagement='contract',
            url='https://example.com/jobs/current-research'))['record']
        source = 'https://example.com/current-research'
        self.run_batch(dict(id=job['id'], reviewed_at=today.isoformat(), source_date=source_date,
            source_urls=[source], summary='Engineering leadership of a software platform and release readiness.'))
        projected = records.state(self.conn)
        self.assertNotIn('intelligence_error', projected)
        item = next(a for a in projected['intelligence']['actions'] if a['record_id'] == job['id'])
        self.assertEqual([(f['url'], f['date']) for f in item['brief']['facts']], [(source, source_date)])

    def test_new_vacancy_links_history_without_inheriting_status(self):
        old = records.create(self.conn, dict(type='job', name='Head of Quality', organisation='Example Co',
                             url='https://example.com/jobs/old', status='rejected'))['record']
        result = self.run_batch(self.finding(related_ids=[old['id']]))
        new_id = result['items'][0]['record_id']
        self.assertNotEqual(new_id, old['id'])
        self.assertEqual(records.get(self.conn, old['id'])['status'], 'rejected')
        self.assertEqual(records.get(self.conn, new_id)['status'], 'candidate')
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM links WHERE from_id=? AND to_id=? AND relation='related'",
                                               (new_id, old['id'])).fetchone())

    def test_linkedin_tracking_variants_match_one_vacancy(self):
        first = self.finding()
        first['create']['url'] = 'https://www.linkedin.com/comm/jobs/view/12345/?trackingId=one'
        second = copy.deepcopy(first)
        second['create']['url'] = 'https://uk.linkedin.com/jobs/view/12345/?trackingId=two'
        a = self.run_batch(first)['items'][0]['record_id']
        b = self.run_batch(second)['items'][0]['record_id']
        self.assertEqual(a, b)
        self.assertEqual(records.get(self.conn, a)['url'], 'https://www.linkedin.com/jobs/view/12345/')

    def test_archived_url_is_reported_without_recreating_job(self):
        rid = self.run_batch(self.finding())['items'][0]['record_id']
        job = records.get(self.conn, rid)
        records.archive(self.conn, rid, dict(revision=job['revision'], reason='Closed'))
        before = list(self.conn.iterdump())
        result = self.run_batch(self.finding())
        self.assertEqual(result['items'][0]['outcome'], 'archived')
        self.assertEqual(before, list(self.conn.iterdump()))

    def test_ambiguous_exact_urls_do_not_choose_or_create(self):
        ids = [records.create(self.conn, dict(type='job', name='Role', url='https://example.com/jobs/1'))['record']['id']
               for _ in range(2)]
        result = self.run_batch(self.finding())
        self.assertEqual(result['items'][0]['outcome'], 'ambiguous')
        self.assertCountEqual(result['items'][0]['matches'], ids)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)

    def test_stale_research_cannot_install_task_or_link(self):
        rid = self.run_batch(self.finding(suggested_task=''))['items'][0]['record_id']
        other = records.create(self.conn, dict(type='company', name='Historical'))['record']['id']
        stale = self.finding(reviewed_at='2026-10-01', related_ids=[other])
        result = self.run_batch(stale)
        self.assertEqual(result['items'][0]['outcome'], 'stale')
        self.assertEqual(records.get(self.conn, rid)['next_action'], '')
        self.assertIsNone(self.conn.execute("SELECT 1 FROM links WHERE relation='related'").fetchone())

    def test_invalid_batch_is_atomic_and_workflow_in_create_is_refused(self):
        invalid = self.finding()
        invalid['create']['url'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            self.run_batch(self.finding(), invalid)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        invalid = self.finding()
        invalid['create']['status'] = 'applied'
        with self.assertRaises(ValueError):
            self.run_batch(invalid)

    def test_missing_link_target_rolls_back_instead_of_silently_dropping_history(self):
        with self.assertRaises(ValueError):
            self.run_batch(self.finding(related_ids=['JOB-absent']))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_receipts_cover_legacy_company_missing_and_archived_ids(self):
        org = dict(create=dict(type='company', name='Another Co'), reviewed_at='2026-10-06',
                   source_urls=['https://example.com'])
        result = self.run_batch(org, dict(id='CO-missing', reviewed_at='2026-10-06', source_urls=['https://example.com']))
        self.assertEqual([i['outcome'] for i in result['items']], ['created', 'unknown_id'])
        self.assertEqual(result['unknown_ids'], ['CO-missing'])

    def test_closed_record_and_open_decision_keep_control_of_workflow(self):
        job = records.create(self.conn, dict(type='job', name='Role', status='rejected',
                             url='https://example.com/jobs/1'))['record']
        result = self.run_batch(self.finding())
        self.assertEqual(result['items'][0]['task'], 'preserved')
        self.assertEqual(records.get(self.conn, job['id'])['status'], 'rejected')
        self.assertEqual(records.get(self.conn, job['id'])['next_action'], '')
        company = records.create(self.conn, dict(type='company', name='Considering'))['record']
        records.create_decision(self.conn, dict(subject=company['id'], kind='approve', question='Research this?',
                                               recommendation='approve', reason='Synthetic question'))
        entry = self.finding(id=company['id'])
        del entry['create']
        self.assertEqual(self.run_batch(entry)['items'][0]['task'], 'preserved')

    def test_bad_sources_and_fact_shapes_are_rejected_before_any_write(self):
        for changes in (dict(source_urls=[]), dict(source_urls=['javascript:bad']),
                        dict(reviewed_at='yesterday'), dict(facts='a claim'), dict(assumptions=[42]),
                        dict(suggested_task='x' * 241)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_batch(self.finding(), self.finding(**changes))
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_late_failure_rolls_back_prior_valid_entries(self):
        other = self.finding(related_ids=['JOB-absent'])
        other['create']['url'] = 'https://example.com/jobs/2'
        with self.assertRaises(ValueError):
            self.run_batch(self.finding(), other)
        for table in ('records', 'observations', 'followups', 'links', 'state_events'):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_updated_existing_id_returns_current_revision_without_resurrecting_completed_task(self):
        rid = self.run_batch(self.finding())['items'][0]['record_id']
        before = records.get(self.conn, rid)
        records.followup(self.conn, dict(id=rid, revision=before['revision'], action='Research completed',
                                        owner='agent', status='done'), actor='agent')
        update = self.finding(id=rid, reviewed_at='2026-10-07', summary='Later evidence', finding_id='reply-2')
        del update['create']
        item = self.run_batch(update)['items'][0]
        self.assertEqual((item['record_id'], item['outcome'], item['task']), (rid, 'updated', 'preserved'))
        after = records.get(self.conn, rid)
        self.assertEqual((after['summary'], after['working_state']), ('Later evidence', 'done'))
        self.assertEqual(item['revision'], after['revision'])

    def test_archived_employer_is_visible_in_receipt_and_not_recreated(self):
        org = records.create(self.conn, dict(type='company', name='Example Co'))['record']
        records.archive(self.conn, org['id'], dict(revision=org['revision'], reason='Archived intentionally'))
        result = self.run_batch(self.finding())
        self.assertEqual(result['archived_organisations'], ['Example Co'])
        self.assertEqual(result['items'][0]['archived_organisation'], 'Example Co')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='company'").fetchone()[0], 1)

    def test_imported_historical_status_is_protected_without_edit_markers(self):
        record_imports.upsert_record(self.conn, record_imports.ed.row(id='JOB-history', type='job',
                                     name='Role', url='https://example.com/jobs/1'),
                                     dict(status='rejected'), '2022-09-27')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM record_edits').fetchone()[0], 0)
        item = self.run_batch(self.finding())['items'][0]
        self.assertEqual(item['task'], 'preserved')
        self.assertEqual(records.get(self.conn, 'JOB-history')['status'], 'rejected')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM followups').fetchone()[0], 0)

    def test_malformed_skipped_target_rejects_entire_batch(self):
        for field, bad in [('summary', ['bad']), ('desk_summary', ['bad']),
                           ('public_contact_route', ['bad']), ('verification', 'fabricated'),
                           ('named_consultant', {'name': ['bad']})]:
            invalid = dict(id='JOB-absent', reviewed_at='2026-10-06', source_urls=['https://example.com'], **{field: bad})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_batch(self.finding(), invalid)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_shared_identity_matches_secondary_advert_urls_and_merged_survivors(self):
        old = records.create(self.conn, dict(type='job', name='Earlier copy',
                             detail_url='https://example.com/jobs/1'))['record']
        keep = records.create(self.conn, dict(type='job', name='Kept copy',
                              url='https://example.com/canonical'))['record']
        records.merge(self.conn, old['id'], keep['id'], dict(revision=old['revision'],
                                               into_revision=keep['revision'], reason='Same vacancy'), actor='agent')
        result = self.run_batch(self.finding())
        self.assertEqual(result['items'][0]['record_id'], keep['id'])
        self.assertEqual(result['created'], 0)

    def test_excluded_employer_retains_evidence_without_suggesting_work(self):
        from unittest.mock import patch
        import search_config
        original = search_config._read
        def config(name):
            value = original(name)
            return {**value, 'exclusions': [dict(company='Example Co', reason='Do not pursue')]} if name == 'relevance.yml' else value
        with patch.object(search_config, '_read', side_effect=config):
            result = self.run_batch(self.finding())
            rid = result['items'][0]['record_id']
            self.assertEqual(result['items'][0]['task'], 'preserved')
            self.assertEqual(records.get(self.conn, rid)['next_action'], '')
            self.assertEqual(len(records.get(self.conn, rid)['research']), 1)


if __name__ == '__main__':
    unittest.main()
