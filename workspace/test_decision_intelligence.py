"""Decision advice uses evidence without promoting hypotheses or historical approvals to fact."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_intelligence as intelligence
import profile_rules
from test_profile_rules import EXAMPLE

TODAY = dt.date(2026, 10, 4)


def company(id='C1', **kwargs):
    return dict(id=id, type='company', name='Example Systems', research=[dict(
        signal='Building a Quality Engineering department to improve release readiness and incident rate.',
        reviewed_at='2026-10-01', source_urls=['https://example.com/careers'],
        verification='primary-read', decisive_unknowns=['Whether an interim start is possible'])],
        relevance={'approach': {'trigger': 'hiring'}}, **kwargs)


def person(id='P1', **kwargs):
    # A named decision-maker read from the company's own page (the contact gate, 14 October).
    values = dict(id=id, type='person', name='Example Person', role='CTO', verification_level='primary-read',
                  evidence_urls='https://example.com/team', created_at='2026-10-01T10:00:00+01:00')
    values.update(kwargs)
    return values


def advert(id, name, text, **kwargs):
    values = dict(id=id, type='job', name=name, date='2026-10-02', url=f'https://jobs.example.com/{id}',
                  advert={'text': text, 'url': f'https://jobs.example.com/{id}', 'observed_at': '2026-10-03'},
                  relevance={'band': 'strong', 'last_seen': '2026-10-03', 'qualification': {
                      'eligible': True, 'domain': 'software', 'engagement': 'contract', 'conflicts': []}})
    values.update(kwargs)
    return values


def state(records=None, **kwargs):
    return dict(records=records or [company(), person()],
                links=[dict(from_id='P1', to_id='C1', relation='works_at')], **kwargs)


class IntelligenceTests(unittest.TestCase):
    def award_company(self, **changes):
        c = company()
        c['research'], c['relevance'] = [], {}
        c['commercial_signals'] = [dict(signal_type='contract_award', eligible=True,
            title='Software integration and release readiness', summary='Delivery of a new software service',
            buyer='Example Council', award_date='2026-09-20', contract_end='2027-10-01',
            published='2026-10-03', observed_at='2026-10-04', url='https://example.com/award',
            amount=1000000, currency='GBP', **changes)]
        return c

    def test_award_is_supplier_qualification_with_no_assumed_subcontract_budget(self):
        result = intelligence.build(state([self.award_company(), person()]), TODAY)
        item = result['actions'][0]
        self.assertIn('subcontract', item['brief']['question'].lower())
        self.assertIn('2026-09-20', item['brief']['why_now'])
        self.assertIn('unconfirmed', item['brief']['why_now'])
        self.assertIn('delivery owner', ' '.join(item['brief']['unknowns']).lower())
        self.assertNotIn('£1', item['brief']['help'])

    def test_award_without_recipient_or_specific_playbook_stays_in_research(self):
        c = self.award_company()
        c['commercial_signals'][0]['title'] = 'Digital service delivery'
        c['commercial_signals'][0]['summary'] = 'Contract awarded to supplier'
        result = intelligence.build(state([c]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(len(result['research']), 1)
        self.assertIn('delivery', result['research'][0]['brief']['recommendation'].lower())

    def test_award_update_date_does_not_make_historical_award_a_fresh_trigger(self):
        c = self.award_company()
        c['commercial_signals'][0]['award_date'] = '2022-01-01'
        result = intelligence.build(state([c, person()]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertIn('2022-01-01', result['research'][0]['brief']['why_now'])
        self.assertIn('current delivery phase', ' '.join(result['research'][0]['brief']['unknowns']))

    def test_ineligible_expired_and_missing_dates_never_create_ready_award_approach(self):
        for change in ({'eligible': False}, {'contract_end': '2026-09-01'}, {'award_date': ''}):
            c = self.award_company()
            c['commercial_signals'][0].update(change)
            result = intelligence.build(state([c, person()]), TODAY)
            self.assertEqual(result['actions'], [])
            if change.get('award_date') != '':
                self.assertEqual(result['research'], [])

    def test_sourced_remit_yields_a_bounded_hypothesis_and_useful_advice(self):
        item = intelligence.build(state(), TODAY)['actions'][0]
        self.assertEqual(item['person_id'], 'P1')
        self.assertIn('may', item['brief']['hypothesis'].lower())
        self.assertIn('release', item['brief']['help'].lower())
        self.assertEqual(item['brief']['facts'][0]['date'], '2026-10-01')
        self.assertTrue(item['brief']['question'])
        self.assertTrue(item['brief']['alternatives'])
        self.assertTrue(item['brief']['expected_result'])
        self.assertNotIn('£', item['brief']['help'])

    def test_funding_is_timing_not_evidence_of_a_problem(self):
        c = company()
        c['research'] = []
        c['signals'] = [dict(headline='Example raises investment', published='2026-10-01', url='https://example.com/news')]
        c['relevance'] = {'approach': {'trigger': 'funded'}, 'funded': '2026-10-01'}
        result = intelligence.build(state([c, person()]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(len(result['research']), 1)
        self.assertIn('funding', result['research'][0]['brief']['hypothesis'].lower())

    def test_company_activity_does_not_claim_a_current_requested_remit(self):
        for signal in (
            'Raised funding to build AI integrations and improve release readiness.',
            'Our product builds AI integrations and improves release readiness for customers.',
            'We provide engineering leadership and deliver data integrations for customers.',
            'We are hiring a Head of Engineering to improve release readiness.',
            'We do not need help to improve release readiness or incident rate.',
        ):
            with self.subTest(signal=signal):
                c = company()
                c['research'][0]['signal'] = signal
                result = intelligence.build(state([c]), TODAY)
                self.assertEqual(result['actions'], [])
                item = result['research'][0]
                self.assertFalse(item['work_evidence']['specific'])
                self.assertEqual(item['confidence']['label'], 'Needs verification')
                self.assertEqual(item['brief']['unknowns'][0],
                                 'No current source establishes requested work or a specific delivery remit; company activity and hiring alone are context.')
                self.assertNotIn('specific remit was recorded', item['brief']['why_now'])

    def test_qualified_vacancy_does_not_ask_for_a_separate_consulting_problem(self):
        job = advert('J', 'Engineering Manager', 'Lead software teams maintaining payment services.')
        item = intelligence.build(state([job]), TODAY)['actions'][0]
        self.assertNotIn('Find a specific existing engineering remit or problem before proposing help',
                         item['brief']['unknowns'])
        self.assertIn('advertised', item['brief']['recommendation'])

    def test_vacancy_briefing_unknowns_precede_company_context(self):
        c = company()
        job = advert('J', 'Engineering Manager', 'Lead software teams maintaining payment services.')
        job['briefing'] = {'decisive_unknowns': ['Confirm recent production LLM delivery depth.']}
        s = state([c, job])
        s['links'] = [dict(from_id='J', to_id='C1', relation='hiring_for')]
        item = next(a for a in intelligence.build(s, TODAY)['actions'] if a['record_id'] == 'J')
        self.assertEqual(item['brief']['unknowns'][0], 'Confirm recent production LLM delivery depth.')
        self.assertNotIn('Whether an interim start is possible', item['brief']['unknowns'])

    def test_speculative_or_generic_customer_need_is_not_requested_company_work(self):
        for signal in ('They may need help to improve release readiness.',
                       'Our customers need help to improve release readiness.',
                       'No external help is needed. Improve release readiness and incident rate.',
                       'Improve release readiness with our product.',
                       'Improve release readiness using our platform.',
                       'Improve release readiness using our AI platform.',
                       'Deliver quality engineering improvements with our managed service.',
                       'Current remit: Improve release readiness through our advanced AI platform.',
                       'Requested remit: Improve release readiness using our advanced AI platform.',
                       'Improve release readiness and incident rate.',
                       'Our platform supports teams that need help to improve release readiness.',
                       'Improve release readiness is not planned.',
                       'Current remit: Improve release readiness is not currently planned.',
                       'The team needs help to improve release readiness, but this work is not yet requested.',
                       'The requested work to improve release readiness has been postponed.',
                       'Our AI product supports release readiness. We need help delivering electrical engineering.',
                       'Our software supports release readiness. We need help delivering electrical engineering and release readiness.'):
            with self.subTest(signal=signal):
                c = company()
                c['research'][0]['signal'] = signal
                result = intelligence.build(state([c]), TODAY)
                self.assertEqual(result['actions'], [])
                self.assertFalse(result['research'][0]['work_evidence']['specific'])

    def test_explicit_requested_remit_survives_funding_context_and_unknown_budget(self):
        c = company()
        c['research'][0]['signal'] = ('Raised funding for growth. The engineering team requests help '
                                     'to improve release readiness for its existing payments service.')
        c['research'][0]['decisive_unknowns'] = ['Budget and engagement terms are unknown.']
        item = intelligence.build(state([c]), TODAY)['actions'][0]
        self.assertTrue(item['work_evidence']['specific'])
        self.assertEqual(item['work_evidence']['date'], '2026-10-01')
        self.assertIn('Budget and engagement terms are unknown.', item['brief']['unknowns'])

    def test_team_requested_work_can_name_existing_platform_without_known_budget(self):
        c = company()
        c['research'][0]['signal'] = ('Our engineering team requests help to improve release readiness '
                                     'using our existing AI platform. Budget is not yet confirmed.')
        item = intelligence.build(state([c]), TODAY)['actions'][0]
        self.assertTrue(item['work_evidence']['specific'])

    def test_repeated_generic_research_is_not_problem_evidence(self):
        records = [company(id=f'C{i}') for i in range(1, 4)]
        result = intelligence.build(state(records + [person()]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(len(result['research']), 3)

    def test_refreshing_one_company_does_not_make_its_signal_generic(self):
        c = company()
        before = intelligence.build(state([c, person()]), TODAY)['actions'][0]
        c['research'] *= 4
        after = intelligence.build(state([c, person()]), TODAY)['actions'][0]
        self.assertEqual(after['confidence'], before['confidence'])
        self.assertEqual(after['priority'], before['priority'])
        self.assertEqual(after['brief']['facts'], before['brief']['facts'])

    def test_generic_research_ignores_case_and_spacing_across_companies(self):
        companies = [company(id=f'C{i}') for i in range(1, 4)]
        companies[1]['research'][0]['signal'] = companies[1]['research'][0]['signal'].upper()
        companies[2]['research'][0]['signal'] = '  ' + companies[2]['research'][0]['signal'].replace(' ', '\n  ')
        result = intelligence.build(state(companies + [person()]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(len(result['research']), 3)

    def test_undated_or_stale_evidence_goes_to_research(self):
        for date in ('', '2025-02-01'):
            with self.subTest(date=date):
                c = company()
                c['research'][0]['reviewed_at'] = date
                result = intelligence.build(state([c, person()]), TODAY)
                self.assertEqual(result['actions'], [])
                self.assertEqual(result['research'][0]['stage'], 'research')

    def test_historical_source_reviewed_today_keeps_its_date_and_needs_research(self):
        c = company()
        c['research'][0].update(source_date='2024-05-10', reviewed_at=TODAY.isoformat(),
                                observed_at=TODAY.isoformat())
        result = intelligence.build(state([c, person()]), TODAY)
        self.assertEqual(result['actions'], [])
        item = result['research'][0]
        self.assertEqual(item['brief']['facts'][0]['date'], '2024-05-10')
        self.assertIn('Refresh and verify', ' '.join(item['brief']['unknowns']))
        self.assertNotIn('evidence', [reason['factor'] for reason in item['priority']['reasons']])

    def test_explicit_unknown_inexact_or_invalid_source_date_never_uses_review_time(self):
        for date in ('', None, 'unknown', '2026', '2026-10', '2026-02-30',
                     '2026-10-01not-a-date'):
            with self.subTest(source_date=date):
                c = company()
                c['research'][0].update(source_date=date, reviewed_at=TODAY.isoformat(),
                                        observed_at=TODAY.isoformat())
                result = intelligence.build(state([c, person()]), TODAY)
                self.assertEqual(result['actions'], [])
                self.assertEqual(result['research'][0]['brief']['facts'][0]['date'], '')

    def test_future_source_date_is_visible_but_not_current_evidence(self):
        c = company()
        c['research'][0]['source_date'] = '2026-10-05'
        result = intelligence.build(state([c, person()]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['research'][0]['brief']['facts'][0]['date'], '2026-10-05')

    def test_legacy_research_without_source_date_retains_its_page_review_clock(self):
        # Omission is the existing page-research contract, not an explicit unknown source date.
        for clocks, expected in (({'reviewed_at': '2026-10-01', 'observed_at': '2026-10-03'}, '2026-10-01'),
                                 ({'reviewed_at': '', 'observed_at': '2026-10-02'}, '2026-10-02')):
            with self.subTest(clocks=clocks):
                c = company()
                c['research'][0].update(clocks)
                item = intelligence.build(state([c, person()]), TODAY)['actions'][0]
                self.assertEqual(item['brief']['facts'][0]['date'], expected)

    def test_current_dated_finding_can_support_a_remit_beside_historical_evidence(self):
        c = company()
        c['research'][0].update(source_date='2024-05-10', reviewed_at=TODAY.isoformat())
        c['research'].append(dict(source_date='2026-10-02T10:30:00Z', reviewed_at=TODAY.isoformat(),
            # Hiring alone now establishes vacancy context, not direct work. Keep this date
            # regression about a genuine requested remit beside its historical evidence.
            signal='The current team needs help to improve release readiness and quality engineering.',
            source_urls=['https://example.com/current-role'], verification='primary-read'))
        item = intelligence.build(state([c, person()]), TODAY)['actions'][0]
        dates = {fact['url']: fact['date'] for fact in item['brief']['facts']}
        self.assertEqual(dates, {'https://example.com/current-role': '2026-10-02',
                                 'https://example.com/careers': '2024-05-10'})
        self.assertIn('2026-10-02', item['brief']['why_now'])
        self.assertIn('evidence', [reason['factor'] for reason in item['priority']['reasons']])

    def test_rereview_and_repeated_projection_do_not_refresh_historical_evidence(self):
        c = company()
        c['research'][0].update(source_date='2024-05-10', reviewed_at='2024-05-10')
        initial = intelligence.build(state([c, person()]), TODAY)
        c['research'][0]['reviewed_at'] = TODAY.isoformat()
        c['research'].append(copy.deepcopy(c['research'][0]))
        s = state([c, person()])
        original = copy.deepcopy(s)
        self.assertEqual(intelligence.build(s, TODAY), initial)
        self.assertEqual(intelligence.build(s, TODAY), initial)
        self.assertEqual(s, original)

    def test_unsupported_source_url_is_not_actionable(self):
        c = company()
        c['research'][0]['source_urls'] = ['javascript:alert(1)']
        self.assertEqual(intelligence.build(state([c, person()]), TODAY)['actions'], [])

    def test_wrong_domain_advert_does_not_create_an_action_or_research(self):
        c = company()
        c['research'] = []
        j = dict(id='J1', type='job', name='Mechanical Engineering Manager', summary='Engineering leadership',
                 relevance={'qualification': {'domain': 'outside', 'eligible': False, 'conflicts': []}})
        s = state([c, person(), j])
        s['links'].append(dict(from_id='C1', to_id='J1', relation='hiring_for'))
        result = intelligence.build(s, TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['research'], [])

    def test_closed_advert_is_not_a_live_trigger(self):
        c = company()
        c['research'] = []
        j = dict(id='J1', type='job', name='Head of Engineering', status='closed',
                 summary='Interim engineering leadership', relevance={'qualification': {'eligible': True}})
        s = state([c, person(), j])
        s['links'].append(dict(from_id='C1', to_id='J1', relation='hiring_for'))
        self.assertEqual(intelligence.build(s, TODAY)['actions'], [])

    def test_conflicting_engagement_evidence_requires_research(self):
        c = company()
        j = dict(id='J1', type='job', name='Head of Engineering', relevance={'qualification': {
            'eligible': True, 'domain': 'software', 'conflicts': ['Contract in title, permanent in advert']}})
        s = state([c, person(), j])
        s['links'].append(dict(from_id='C1', to_id='J1', relation='hiring_for'))
        result = intelligence.build(s, TODAY)
        self.assertEqual(result['actions'], [])
        self.assertIn('Contract', ' '.join(result['research'][0]['brief']['unknowns']))

    def test_counterevidence_is_not_used_as_positive_keyword_evidence(self):
        c = company()
        c['research'][0]['signal'] = 'VP Engineering is a partner-engineering role, not a product-engineering leadership vacancy.'
        result = intelligence.build(state([c, person()]), TODAY)
        self.assertEqual(result['actions'], [])

    def test_one_adverse_finding_is_not_overwhelmed_by_positive_keywords(self):
        c = company()
        c['research'].append(dict(signal='VP Engineering is a partner-engineering role, not a product-engineering leadership vacancy.',
                                 reviewed_at='2026-10-02', source_urls=['https://example.com/role']))
        result = intelligence.build(state([c, person()]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertIn('Contrary source finding', ' '.join(result['research'][0]['brief']['unknowns']))

    def test_no_named_person_keeps_company_in_agent_research(self):
        actions = intelligence.build(state([company()]), TODAY)['actions']
        self.assertEqual([a['record_id'] for a in actions], ['C1'])
        self.assertTrue(actions[0]['work_evidence']['specific'])
        self.assertIsNone(actions[0]['person_id'])

    def test_one_way_contact_never_becomes_a_warm_route(self):
        p = person(last_touch='2026-10-03')
        s = state([company(), p], interactions=[dict(record_id='P1', direction='out', at='2026-10-03')])
        item = intelligence.build(s, TODAY)['actions'][0]
        self.assertEqual(item['route'], 'direct')
        self.assertEqual(item['access']['label'], 'Cold')

    def test_current_former_employer_people_excluded_but_explicit_leaver_allowed(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        p = person(organisation='Example Corp', linkedin={'former_employer': True})
        s = state([company(), p])
        self.assertFalse(any(a.get('person_id') == 'P1' for a in intelligence.build(s, TODAY)['actions']))
        p['known_context'] = 'Former Example Corp colleague; confirmed by the user'
        self.assertEqual(len([a for a in intelligence.build(s, TODAY)['actions'] if a.get('person_id') == 'P1']), 1)
        p['relationship'] = 'do_not_contact'
        self.assertFalse(any(a.get('person_id') == 'P1' for a in intelligence.build(s, TODAY)['actions']))

    def test_projected_linkedin_flags_exclude_current_employer(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        p = person(linkedin={'flags': ['former_employer'], 'observed_company': 'Example Employer'})
        self.assertFalse(any(a.get('person_id') == 'P1' for a in intelligence.build(state([company(),p]), TODAY)['actions']))

    def test_observed_linkedin_role_keeps_employment_uncertainty_visible(self):
        p = person(linkedin={'observed_role': 'CTO', 'observed_company': 'Example Systems'})
        p['role'] = ''
        item = intelligence.build(state([company(), p]), TODAY)['actions'][0]
        self.assertEqual(item['role'], 'CTO')
        self.assertIn('dated observation', ' '.join(item['brief']['unknowns']))

    def test_current_approved_draft_and_hold_have_distinct_states(self):
        s = state(drafts=[dict(record_id='P1', version_id=8, text='A note')], decisions=[dict(
            id=1, kind='draft', record_id='P1', status='done', answer='approve', draft_version_id=8, draft_binding='exact')])
        self.assertEqual(intelligence.build(s, TODAY)['actions'][0]['stage'], 'ready')
        s['decisions'][0]['answer'] = 'hold'
        item = intelligence.build(s, TODAY)['actions'][0]
        self.assertEqual(item['stage'], 'held')
        self.assertIn('hold', item['brief']['recommendation'].lower())

    def test_changed_draft_does_not_inherit_prior_approval(self):
        s = state(drafts=[dict(record_id='P1', version_id=9, text='Changed note')], decisions=[dict(
            id=1, kind='draft', record_id='P1', status='done', answer='approve', draft_version_id=8, draft_binding='exact')])
        self.assertEqual(intelligence.build(s, TODAY)['actions'][0]['stage'], 'ready')

    def test_legacy_approval_is_labelled_as_historical(self):
        s = state(drafts=[dict(record_id='C1', version_id=8, text='Old note')], decisions=[dict(
            id=1, kind='draft', record_id='C1', status='done', answer='approve', draft_binding='legacy')])
        item = intelligence.build(s, TODAY)['actions'][0]
        self.assertEqual(item['stage'], 'ready')
        self.assertNotIn('draft_version_id', item)
        self.assertEqual(item['readiness']['label'], 'Qualify first')

    def test_owner_approval_does_not_wait_for_agent_acknowledgement(self):
        s = state(drafts=[dict(record_id='P1', version_id=8, text='A note')], decisions=[dict(
            id=1, kind='draft', record_id='P1', status='answered', answer='approve', draft_version_id=8, draft_binding='exact')])
        self.assertEqual(intelligence.build(s, TODAY)['actions'][0]['stage'], 'ready')

    def test_approved_note_cannot_override_stale_evidence(self):
        c = company()
        c['research'][0]['reviewed_at'] = '2025-02-01'
        s = state([c, person()], drafts=[dict(record_id='P1', version_id=8, text='A note')], decisions=[dict(
            id=1, kind='draft', record_id='P1', status='done', answer='approve', draft_version_id=8, draft_binding='exact')])
        result = intelligence.build(s, TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['research'][0]['stage'], 'research')

    def test_company_note_attaches_only_to_its_named_recipient(self):
        s = state([company(), person(), person(id='P2')], drafts=[dict(record_id='C1', version_id=3,
                  title='Note to Example Person', text='Hello')])
        s['records'][2]['name'] = 'Other Person'
        s['links'].append(dict(from_id='P2', to_id='C1', relation='works_at'))
        result = intelligence.build(s, TODAY)
        has_draft = [a for a in result['actions'] if a.get('draft_record_id')]
        self.assertEqual(has_draft, [])
        self.assertEqual({a['record_id'] for a in result['actions']}, {'C1', 'P1', 'P2'})

    def test_an_ineligible_company_still_prevents_misattributing_its_personal_note(self):
        alpha, beta = company(), company(id='C2')
        alpha['name'], beta['name'] = 'Alpha Systems', 'Beta Systems'
        beta['research'], beta['relevance'] = [], {}
        s = state([alpha, beta, person()], drafts=[dict(record_id='P1', version_id=8,
            title='Beta Systems introduction', text='Can we discuss Beta Systems?')], decisions=[dict(
            id=1, record_id='P1', kind='draft', status='answered', answer='approve',
            draft_binding='exact', draft_version_id=8)])
        s['links'].append(dict(from_id='P1', to_id='C2', relation='works_at'))
        item = intelligence.build(s, TODAY)['actions'][0]
        self.assertEqual(item['company_id'], 'C1')
        self.assertNotIn('draft_version_id', item)
        self.assertNotEqual(item['stage'], 'approved')
        self.assertEqual(item['stage'], 'ready')

    def test_old_company_draft_questions_do_not_supply_a_recipient_for_new_copy(self):
        s = state(drafts=[dict(record_id='C1', version_id=9, title='New approach', text='Hello')],
                  decisions=[dict(id=1, record_id='C1', kind='draft', status='withdrawn',
                                  question='Approve a note to Example Person?',
                                  draft_binding='exact', draft_version_id=8)])
        item = next(a for a in intelligence.build(s, TODAY)['actions'] if a['record_id'] == 'C1')
        self.assertEqual(item['record_id'], 'C1')
        self.assertIsNone(item['person_id'])

    def test_unattributable_company_note_stays_on_a_company_action(self):
        s = state([company(), person(), person(id='P2')], drafts=[dict(record_id='C1', version_id=3, text='Hello')])
        s['links'].append(dict(from_id='P2', to_id='C1', relation='works_at'))
        result = intelligence.build(s, TODAY)
        company_actions = [a for a in result['actions'] if a['record_id'] == 'C1']
        self.assertEqual(len(company_actions), 1)
        self.assertIsNone(company_actions[0]['person_id'])
        self.assertTrue(all('draft_record_id' not in a for a in result['actions']))

    def test_personal_draft_only_attaches_to_its_named_company_context(self):
        for where in ('title', 'opening', 'decision'):
            with self.subTest(where=where):
                c1, c2 = company(id='C1'), company(id='C2')
                c1['name'], c2['name'] = 'Alpha Systems', 'Beta Systems'
                draft = dict(record_id='P1', version_id=8, title='A note', text='Hello, can we discuss the engineering remit?')
                decision = dict(id=1, record_id='P1', kind='draft', status='done', answer='approve',
                                draft_binding='exact', draft_version_id=8, context='Review this note')
                if where == 'title':
                    draft['title'] = 'Alpha Systems introduction'
                elif where == 'opening':
                    draft['text'] = 'Hello, I would like to discuss the remit at Alpha Systems.'
                else:
                    decision['context'] = 'This note is for Alpha Systems.'
                s = state([c1, c2, person()], drafts=[draft], decisions=[decision])
                s['links'].append(dict(from_id='P1', to_id='C2', relation='works_at'))
                actions = {a['id']: a for a in intelligence.build(s, TODAY)['actions']}
                self.assertEqual(actions['action:P1:C1']['stage'], 'ready')
                self.assertNotIn('draft_record_id', actions['action:P1:C1'])
                other = actions['action:P1:C2']
                self.assertEqual(other['stage'], 'ready')
                self.assertNotIn('draft_record_id', other)
                self.assertNotIn('draft_version_id', other)

    def test_ambiguous_personal_draft_does_not_copy_approval_to_company_actions(self):
        c1, c2 = company(id='C1'), company(id='C2')
        c1['name'], c2['name'] = 'Alpha Systems', 'Beta Systems'
        for text in ('Hello, can we discuss engineering?', 'A note about Alpha Systems and Beta Systems.'):
            with self.subTest(text=text):
                s = state([c1, c2, person()], drafts=[dict(record_id='P1', version_id=8, text=text)], decisions=[dict(
                    id=1, record_id='P1', kind='draft', status='done', answer='approve', draft_binding='exact', draft_version_id=8)])
                s['links'].append(dict(from_id='P1', to_id='C2', relation='works_at'))
                before = copy.deepcopy(s)
                actions = intelligence.build(s, TODAY)['actions']
                self.assertEqual(len(actions), 4)
                self.assertTrue(all(a['stage'] == 'ready' for a in actions))
                self.assertTrue(all('draft_record_id' not in a for a in actions))
                self.assertTrue(all('draft_version_id' not in a for a in actions))
                self.assertEqual(s, before)

    def test_outcomes_stop_completed_or_disqualified_moves_being_reoffered(self):
        for kind, stage in [('sent', 'sent'), ('replied', 'replied'), ('conversation', 'conversation'),
                            ('no_budget', 'held'), ('no_fit', 'held'), ('won', 'won')]:
            with self.subTest(kind=kind):
                s = state(outcomes=[dict(id=1, record_id='P1', kind=kind, at='2026-10-03', note='Recorded by the owner')])
                item = intelligence.build(s, TODAY)['actions'][0]
                self.assertEqual(item['stage'], stage)
                self.assertEqual(item['last_outcome']['kind'], kind)
                self.assertNotEqual(item['brief']['recommendation'], intelligence.build(state(), TODAY)['actions'][0]['brief']['recommendation'])

    def test_action_scoped_outcome_does_not_change_another_company_approach(self):
        for kind, expected in [('no_budget', 'held'), ('sent', 'sent')]:
            with self.subTest(kind=kind):
                s = state([company(id='C1'), company(id='C2'), person()], outcomes=[dict(
                    id=1, record_id='P1', kind=kind, at='2026-10-03', snapshot={'action_id': 'action:P1:C2'})])
                s['links'].append(dict(from_id='P1', to_id='C2', relation='works_at'))
                actions = {a['id']: a for a in intelligence.build(s, TODAY)['actions']}
                self.assertEqual(actions['action:P1:C1']['stage'], 'ready')
                self.assertNotIn('last_outcome', actions['action:P1:C1'])
                self.assertEqual(actions['action:P1:C2']['stage'], expected)
                self.assertEqual(actions['action:P1:C2']['last_outcome']['id'], 1)

    def test_legacy_record_outcome_remains_conservative_and_explains_its_scope(self):
        s = state([company(id='C1'), company(id='C2'), person()], outcomes=[dict(
            id=1, record_id='P1', kind='no_budget', at='2026-10-03')])
        s['links'].append(dict(from_id='P1', to_id='C2', relation='works_at'))
        actions = [a for a in intelligence.build(s, TODAY)['actions'] if a.get('person_id')]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a['stage'] == 'held' for a in actions))
        self.assertTrue(all('record-level' in a['readiness']['reason'] for a in actions))

    def test_warm_referral_without_company_trigger_has_no_invented_company_pain(self):
        p = person(relevance={'reach': {'tier': 'close', 'reason': 'The owner named a friend'}})
        item = intelligence.build(state([p]), TODAY)['actions'][0]
        self.assertEqual(item['route'], 'close_network')
        self.assertIn('referral', item['brief']['hypothesis'].lower())
        self.assertEqual(item['brief']['facts'], [])

    def test_source_qualified_advert_body_supports_company_hypothesis(self):
        c = company()
        c['research'] = []
        j = dict(id='J1', type='job', name='AI Platform Lead', advert=dict(
            text='Build model evaluation and guardrails for deployed AI agents.',
            url='https://example.com/job', observed_at='2026-10-03'),
            relevance={'qualification': {'domain': 'software', 'eligible': True, 'conflicts': []}})
        s = state([c, person(), j])
        s['links'].append(dict(from_id='C1', to_id='J1', relation='hiring_for'))
        item = intelligence.build(s, TODAY)['actions'][0]
        self.assertIn('evaluation', item['brief']['help'].lower())

    def test_valid_advert_has_its_own_decision_brief(self):
        job = dict(id='J1', type='job', name='Contract Head of Quality', advert=dict(
            text='Own software release readiness and incident prevention.', url='https://example.com/job', observed_at='2026-10-03'),
            relevance={'band': 'strong', 'qualification': {'eligible': True, 'domain': 'software', 'engagement': 'contract', 'conflicts': []}})
        item = intelligence.build(state([job]), TODAY)['actions'][0]
        self.assertEqual(item['record_id'], 'J1')
        self.assertEqual(item['route'], 'opportunity')
        self.assertEqual(item['stage'], 'ready')
        self.assertIn('contract', item['brief']['recommendation'])
        job['relevance']['qualification']['domain'] = 'unknown'
        item = intelligence.build(state([job]), TODAY)['actions'][0]
        self.assertEqual(item['confidence']['label'], 'Confirm the remit')

    def test_adverts_missing_body_or_with_conflicting_terms_are_research(self):
        job = dict(id='J1', type='job', name='Head of Engineering', url='https://example.com/job', date='2026-10-03',
                   relevance={'band': 'strong', 'qualification': {'eligible': True, 'domain': 'software', 'conflicts': []}})
        result = intelligence.build(state([job]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['research'][0]['record_id'], 'J1')
        job['summary'] = 'Engineering leadership for platform modernisation'
        job['relevance']['qualification']['conflicts'] = ['Permanent-only source contradicts contract title']
        result = intelligence.build(state([job]), TODAY)
        self.assertEqual(result['actions'], [])
        self.assertIn('contradicts', ' '.join(result['research'][0]['brief']['unknowns']))

    def test_closed_stale_low_and_duplicate_adverts_never_enter_actions(self):
        base = dict(id='J1', type='job', name='Head of Engineering', url='https://example.com/job', date='2026-10-03',
                    summary='Engineering leadership for platform modernisation', relevance={'band': 'strong', 'qualification': {'eligible': True}})
        cases = [dict(source_lifecycle='closed'), dict(source_lifecycle='expired'), dict(published_deadline='2026-10-03'),
                 dict(relevance={'band': 'low'}), dict(relevance={'band': 'strong', 'duplicate_of': 'J2'}),
                 dict(relevance={'band': 'strong', 'last_seen': '2026-08-01'})]
        for extra in cases:
            with self.subTest(extra=extra):
                result = intelligence.build(state([{**base, **extra}]), TODAY)
                self.assertEqual(result['actions'], [])
                self.assertEqual(result['research'], [])

    def test_expired_linked_advert_does_not_supply_company_problem_evidence(self):
        c = company()
        c['research'] = []
        job = dict(id='J1', type='job', name='Head of Engineering', published_deadline='2026-10-03',
                   summary='Engineering leadership for platform modernisation', url='https://example.com/job',
                   date='2026-10-03', relevance={'band': 'strong', 'qualification': {'eligible': True}})
        s = state([c, person(), job])
        s['links'].append(dict(from_id='J1', to_id='C1', relation='hiring_for'))
        result = intelligence.build(s, TODAY)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['research'], [])

    def test_sourced_named_prospect_outranks_generic_historical_referrals(self):
        historical = [person(id=f'H{i}', relevance={'reach': {'tier': 'known', 'reason': 'Historical contact'}})
                      for i in range(8)]
        result = intelligence.build(state([company(), person(), *historical]), TODAY)
        self.assertEqual(result['actions'][0]['person_id'], 'P1')
        prospect = result['actions'][0]
        referral = next(a for a in result['actions'] if a['person_id'] == 'H0')
        self.assertGreater(prospect['priority']['score'], referral['priority']['score'])
        self.assertTrue(any('sourced remit' in r['text'] for r in prospect['priority']['reasons']))
        self.assertEqual(prospect['priority']['score'], sum(r['points'] for r in prospect['priority']['reasons']))

    def test_explicit_close_relationship_remains_a_valued_referral_route(self):
        close = person(id='FRIEND', relationship='warm')
        result = intelligence.build(state([company(), person(), close]), TODAY)
        referral = next(a for a in result['actions'] if a['person_id'] == 'FRIEND')
        self.assertEqual(referral['route'], 'close_network')
        self.assertEqual(referral['access']['label'], 'Close network')
        self.assertEqual(result['actions'][0]['person_id'], 'FRIEND')

    def test_corrected_terms_determine_priority_instead_of_stale_contract_labels(self):
        rows = [dict(id=term, type='job', name='Head of Quality', engagement='contract',
                     summary='Own software release readiness and incident prevention.', date='2026-10-03',
                     url=f'https://example.com/{term}', relevance={'band': 'strong', 'qualification': {
                         'eligible': True, 'domain': 'software', 'engagement': term, 'conflicts': []}})
                for term in ('permanent', 'ftc', 'contract')]
        actions = intelligence.build(state(rows), TODAY)['actions']
        self.assertEqual([a['record_id'] for a in actions], ['contract', 'ftc', 'permanent'])
        for item in actions:
            term = item['record_id']
            self.assertIn(f'advertised {term} role', item['brief']['recommendation'])
            term_reasons = [r for r in item['priority']['reasons'] if r.get('factor') == 'engagement']
            self.assertEqual(term_reasons[0]['text'], f'Qualified engagement: {term}')

    def test_approval_neither_adds_priority_nor_breaks_a_tie(self):
        s = state([company(), person(id='A'), person(id='B')], drafts=[dict(record_id='B', version_id=1, text='Note')],
                  decisions=[dict(id=1, record_id='B', kind='draft', status='done', answer='approve',
                                  draft_binding='exact', draft_version_id=1)])
        s['links'] = [dict(from_id=p, to_id='C1', relation='works_at') for p in ('A', 'B')]
        actions = intelligence.build(s, TODAY)['actions']
        self.assertEqual([a['person_id'] for a in actions if a.get('person_id')], ['A', 'B'])
        self.assertEqual(actions[0]['priority']['score'], actions[1]['priority']['score'])

    def test_stale_linked_demand_does_not_make_an_agency_ready(self):
        agency = dict(id='C1', type='agency', name='Desk', subtype='Quality engineering desk', relevance={'band': 'strong'})
        job = dict(id='J1', type='job', name='Head of Quality', source_lifecycle='closed',
                   summary='Quality engineering department', relevance={'band': 'strong'})
        s = state([agency, job])
        s['links'] = [dict(from_id='C1', to_id='J1', relation='advertised_by')]
        self.assertEqual(intelligence.build(s, TODAY)['actions'], [])

    def test_manual_opportunity_and_agency_desk_are_not_lost(self):
        opportunity = dict(id='O1', type='opportunity', name='Interim leadership enquiry', stage='lead',
                           summary='A recorded inbound enquiry for interim engineering leadership', next_action='Qualify the remit')
        agency = dict(id='A1', type='agency', name='Example Desk', subtype='Quality engineering desk',
                      summary='A specialist quality engineering recruitment desk', relationship='warm',
                      relevance={'band': 'strong'})
        result = intelligence.build(state([opportunity, agency]), TODAY)
        self.assertEqual({a['route'] for a in result['actions']}, {'opportunity', 'agency'})

    def test_manual_commercial_progress_is_not_reset_to_a_first_approach(self):
        opportunity = dict(id='O1', type='opportunity', name='Leadership enquiry', stage='proposal',
                           summary='Engineering leadership engagement', next_action='Review proposal')
        item = intelligence.build(state([opportunity]), TODAY)['actions'][0]
        self.assertEqual(item['stage'], 'proposal')
        self.assertIn('do not repeat', item['brief']['recommendation'])

    def test_large_advert_is_excerpted_around_the_actual_problem(self):
        c = company()
        c['research'][0]['signal'] = 'General company description. ' * 40 + 'Release readiness needs engineering quality evidence. ' + 'Benefits and location. ' * 40
        item = intelligence.build(state([c, person()]), TODAY)['actions'][0]
        fact = item['brief']['facts'][0]['text']
        self.assertLess(len(fact), 480)
        self.assertIn('Release readiness', fact)

    def test_brief_leads_with_the_supporting_advert_and_deduplicates_its_prose(self):
        c = company()
        c['research'] = []
        body = 'Build agentic workflows and model evaluation for deployed AI agents.'
        jobs = [dict(id='SECURITY', type='job', name='Security Lead', summary='Set governance guardrails for secure services.',
                     url='https://example.com/security', date='2026-10-03'),
                dict(id='AI', type='job', name='AI Platform Lead', summary=body,
                     url='https://example.com/ai', date='2026-10-03'),
                dict(id='AI-COPY', type='job', name='AI Platform Lead', summary=body,
                     url='https://example.com/copied-ai', date='2026-10-02')]
        s = state([c, person(), *jobs])
        s['links'] += [dict(from_id=j['id'], to_id='C1', relation='hiring_for') for j in jobs]
        item = next(a for a in intelligence.build(s, TODAY)['actions'] if a['person_id'] == 'P1')
        self.assertIn('AI Platform Lead', item['brief']['why_now'])
        self.assertNotIn('Security Lead', item['brief']['why_now'])
        self.assertEqual(item['brief']['facts'][0]['text'], body)
        self.assertEqual(item['brief']['facts'][0]['url'], 'https://example.com/ai')
        self.assertEqual(sum(f['text'] == body for f in item['brief']['facts']), 1)

    def test_generic_guardrails_without_ai_context_do_not_support_an_ai_hypothesis(self):
        for text, actionable in [('Current remit: Set governance guardrails for secure services.', False),
                                 ('Current remit: Set guardrails for deployed AI agents.', True)]:
            with self.subTest(text=text):
                c = company()
                c['research'][0]['signal'] = text
                result = intelligence.build(state([c, person()]), TODAY)
                self.assertEqual(bool(result['actions']), actionable)
                if not actionable:
                    self.assertEqual(result['research'][0]['fit']['label'], 'Needs qualification')

    def test_draft_readiness_does_not_outrank_a_stronger_access_route(self):
        s = state([company(), person(), person(id='P2', relevance={'reach': {'tier': 'known', 'reason': 'Two-way evidence'}})],
                  drafts=[dict(record_id='P1', version_id=1, text='Note')], decisions=[dict(id=1, kind='draft',
                  record_id='P1', status='done', answer='approve', draft_binding='exact', draft_version_id=1)])
        s['links'].append(dict(from_id='P2', to_id='C1', relation='works_at'))
        self.assertEqual(intelligence.build(s, TODAY)['actions'][0]['person_id'], 'P2')

    def test_projection_is_pure_and_contract_is_complete(self):
        s = state()
        before = copy.deepcopy(s)
        result = intelligence.build(s, TODAY)
        self.assertEqual(s, before)
        self.assertEqual(set(result), {'actions', 'research', 'outcomes_summary', 'eligible_advert_ids'})
        keys = {'id', 'record_id', 'company_id', 'person_id', 'title', 'person', 'company', 'role',
                'route', 'stage', 'fit', 'access', 'confidence', 'readiness', 'brief'}
        self.assertTrue(keys <= result['actions'][0].keys())

    def test_multiple_relationship_edges_do_not_duplicate_a_persons_action(self):
        s = state()
        s['links'].extend([dict(from_id='P1', to_id='C1', relation='related'),
                           dict(from_id='C1', to_id='P1', relation='related')])
        result = intelligence.build(s, TODAY)
        self.assertEqual(len([a for a in result['actions'] if a.get('person_id') == 'P1']), 1)
        self.assertEqual(len([a for a in result['actions'] if a['record_id'] == 'C1']), 1)


class CueCacheTests(unittest.TestCase):
    """A rebuild after a save matches every document again; unchanged text must not be re-read."""

    def test_a_rebuild_re_reads_no_unchanged_text_beyond_the_old_per_playbook_limit(self):
        from unittest import mock
        config = intelligence._config()
        self.assertGreater(3000 * len(config['playbooks']), 8192, 'the fixture must exceed one entry per playbook')
        docs = [dict(kind='company_fact', url='', date='2026-11-01',
                     text=f'Synthetic company {i} releases its platform weekly.') for i in range(3000)]
        intelligence._playbook_hits.cache_clear()
        intelligence._cue_hits.cache_clear()
        counted = mock.Mock(side_effect=intelligence._count_cues)
        with mock.patch.object(intelligence, '_count_cues', counted):
            first = [intelligence._match([d], config)[0] for d in docs]
            reads = counted.call_count
            second = [intelligence._match([d], config)[0] for d in docs]
        self.assertEqual(reads, 3000 * len(config['playbooks']))  # each text read once per playbook
        self.assertEqual(counted.call_count, reads, 'a second identical pass re-read cached text')
        self.assertEqual(first, second)


class QualificationFixTests(unittest.TestCase):
    """The five repeated defects found by the 10 October value gate, plus the contact gate."""

    def hiring(self, *adverts, people=None):
        c = company()
        c['research'] = []
        records = [c, *adverts, *(people if people is not None else [person()])]
        s = state(records)
        s['links'] = [dict(from_id=a['id'], to_id='C1', relation='hiring_for') for a in adverts]
        s['links'] += [dict(from_id=p['id'], to_id='C1', relation='works_at') for p in records if p['type'] == 'person']
        return intelligence.build(s, TODAY)

    def test_stated_contract_terms_are_shown_and_not_asked_again(self):
        ad = advert('J1', 'Programme Test Manager', 'Six month contract, up to £750 per day via umbrella, inside IR35. '
                    'Lead release readiness and test automation for the platform. London two days a week.')
        item = self.hiring(ad)['actions'][0]
        terms = ' | '.join(item['brief']['terms'][0]['terms'])
        self.assertIn('£750/day umbrella', terms)
        self.assertIn('6 months', terms)
        self.assertIn('inside IR35', terms)
        self.assertNotIn('still need confirmation', item['brief']['why_now'])
        self.assertNotIn('Whether an interim or bounded engagement is possible', item['brief']['unknowns'])

    def test_different_payment_bases_are_kept_apart(self):
        ad = advert('J1', 'Test Lead', 'Contract role: £463 per day PAYE inclusive of holiday pay, or £537 per day umbrella. '
                    'System integration testing and UAT for release readiness.')
        terms = self.hiring(ad)['actions'][0]['brief']['terms'][0]['terms']
        self.assertIn('£463/day PAYE', terms)
        self.assertIn('£537/day umbrella', terms)
        self.assertFalse(any('463–537' in x or '463-537' in x for x in terms))

    def test_clearance_requirement_parks_the_approach_and_says_why(self):
        ad = advert('J1', 'Senior Engineering Manager', 'DV clearable role, five days on-site. Release readiness for AI systems.')
        result = self.hiring(ad)
        item = next(a for a in result['actions'] + result['research'] if a['record_id'] in ('P1', 'J1'))
        self.assertEqual(item['stage'], 'held')
        self.assertIn('clearance', item['readiness']['reason'].lower())
        self.assertTrue(any('clearance' in c.lower() for c in item['brief']['constraints']))

    def test_essential_specialist_experience_is_shown_as_a_constraint(self):
        ad = advert('J1', 'Test Manager', 'Six month contract. Relevant IFS programme experience is essential. '
                    'Lead release readiness and regression testing.')
        constraints = self.hiring(ad)['actions'][0]['brief']['constraints']
        self.assertTrue(any('IFS programme experience' in c for c in constraints))

    def test_separate_remits_keep_separate_hypotheses(self):
        first = advert('J1', 'Engineering Manager: Agentic Integrations', 'Take agentic products to reliable, production-grade '
                       'customer deployments with integration reliability across a growing number of them.')
        second = advert('J2', 'Engineering Manager: Personalization', 'Lead the personalization team building ranking features.')
        item = self.hiring(first, second)['actions'][0]
        facts = ' '.join(f['text'] for f in item['brief']['facts'])
        self.assertNotIn('personalization', facts.lower())
        self.assertEqual([r['name'] for r in item['brief']['other_remits']], ['Engineering Manager: Personalization'])

    def test_an_unconfirmed_or_old_named_person_is_not_a_route(self):
        for change in ({'verification_level': ''}, {'created_at': '2026-05-01T10:00:00+01:00'}):
            with self.subTest(change=change):
                result = intelligence.build(state([company(), person(**change)]), TODAY)
                self.assertEqual([a['record_id'] for a in result['actions']], ['C1'])
                self.assertIn("Confirm their current role on the company's own site",
                              result['research'][0]['brief']['unknowns'])

    def test_a_chief_executive_is_not_the_route_for_an_advertised_engineering_remit(self):
        ad = advert('J1', 'Engineering Manager: Integrations', 'Integration reliability for customer deployments.')
        result = self.hiring(ad, people=[person(role='Chief Executive Officer')])
        ceo = next(a for a in result['actions'] + result['research'] if a['record_id'] == 'P1')
        self.assertEqual(ceo['stage'], 'research')
        self.assertIn('owns this remit', ' '.join(ceo['brief']['unknowns']))

    def test_a_founder_is_the_route_at_a_recently_funded_company(self):
        c = company(signals=[dict(published='2026-09-20', amount='£4m', headline='Example raises £4m')])
        item = intelligence.build(state([c, person(role='Founder and CEO')]), TODAY)['actions'][0]
        self.assertEqual(item['stage'], 'ready')


class PermanentAdvertTests(unittest.TestCase):
    def test_an_advert_with_no_stated_terms_waits_for_the_assistant_to_confirm_them(self):
        ad = advert('J1', 'Head of Quality Engineering', 'Lead release readiness and test automation.',
                    relevance={'band': 'strong', 'last_seen': '2026-10-03', 'qualification': {
                        'eligible': True, 'domain': 'software', 'engagement': 'unknown', 'conflicts': []}})
        result = intelligence.build(state([ad]), TODAY)
        self.assertEqual([a['record_id'] for a in result['actions']], ['J1'])
        self.assertEqual(intelligence._engagement(ad), 'unknown')
        self.assertIn('terms still need confirmation', result['actions'][0]['brief']['why_now'])

    def test_a_permanent_advert_is_a_reason_to_approach_but_never_an_application(self):
        c = company()
        c['research'] = []
        ad = advert('J1', 'Head of Quality Engineering', 'Permanent role. Lead release readiness and test automation.',
                    relevance={'band': 'strong', 'last_seen': '2026-10-03', 'qualification': {
                        'eligible': True, 'domain': 'software', 'engagement': 'permanent', 'conflicts': []}})
        s = state([c, ad, person()])
        s['links'].append(dict(from_id='J1', to_id='C1', relation='hiring_for'))
        result = intelligence.build(s, TODAY)
        self.assertTrue(any(a['record_id'] == 'J1' for a in result['actions'] + result['research']))
        item = next(a for a in result['actions'] if a['record_id'] == 'P1')
        self.assertEqual(item['trigger']['kind'], 'advert')


class AgencyIntelligenceTests(unittest.TestCase):
    def test_primary_company_research_does_not_hide_later_unverified_gaps(self):
        co=company()
        co['research'].append(dict(reviewed_at='2026-10-04', verification='unconfirmed',
            decisive_unknowns=['Confirm whether the newly advertised remit remains funded']))
        item=intelligence.build(state([co,person()]),TODAY)['actions'][0]
        self.assertIn('Confirm whether the newly advertised remit remains funded',item['brief']['unknowns'])

    def test_conflicted_or_subthreshold_agency_adverts_do_not_demote_verified_consultant(self):
        ag = dict(
            id='AG1', type='agency', kind='agency', name='Example Specialist Agency',
            subtype='Tier 1 — Engineering & AI contracts', summary='Specialist technology recruitment agency.',
            research=[
                dict(reviewed_at='2026-10-03', verification='primary-read',
                     desk_summary='Dedicated Data & AI contract and interim practice in London.',
                     public_contact_route='https://agency.example.com/contact',
                     source_urls=['https://agency.example.com/team'],
                     decisive_unknowns=['Live outside-IR35 brief on the contract desk']),
                dict(reviewed_at='2026-09-21', verification='unconfirmed',
                     summary='Initial inventory row.', source_urls=['https://agency.example.com'],
                     decisive_unknowns=['Named relevant consultant', 'source evidence date']),
            ],
            relevance={'score': 48, 'band': 'possible'},
        )
        consultant = person('P1', name='Casey Consultant', role='VP, Data & AI Contracts', kind='recruiter')
        conflicted = advert(
            'J_CONFLICT', 'Forward Deployed AI Engineer',
            'Contract 12 months outside IR35.',
            relevance={'score': 40, 'band': 'possible', 'last_seen': '2026-10-03', 'qualification': {
                'eligible': True, 'domain': 'software', 'engagement': 'interim',
                'conflicts': ['Advert text states interim; employment_type says permanent.']}},
        )
        subthreshold = advert(
            'J_LOW', 'Oracle Test Manager',
            'Fixed term 24 months, 2 days a week.',
            relevance={'score': 32, 'band': 'possible', 'last_seen': '2026-10-03', 'qualification': {
                'eligible': True, 'domain': 'software', 'engagement': 'ftc', 'conflicts': []}},
        )
        relevant = advert(
            'J_GOOD', 'Head of AI',
            'Contract £850/day outside IR35 leading AI engineering delivery.',
            pay='£850/day', engagement='contract',
            relevance={'score': 40, 'band': 'possible', 'last_seen': '2026-10-03', 'qualification': {
                'eligible': True, 'domain': 'software', 'engagement': 'contract', 'conflicts': []}},
        )
        s = dict(
            records=[ag, consultant, conflicted, subthreshold, relevant],
            links=[
                dict(from_id='P1', to_id='AG1', relation='consultant_at'),
                dict(from_id='J_CONFLICT', to_id='AG1', relation='hiring_for'),
                dict(from_id='J_LOW', to_id='AG1', relation='hiring_for'),
                dict(from_id='J_GOOD', to_id='AG1', relation='hiring_for'),
            ],
        )
        result = intelligence.build(s, TODAY)
        item = next((a for a in result['actions'] if a['record_id'] == 'P1'), None)
        self.assertIsNotNone(item, 'verified agency consultant should remain in ready actions')
        self.assertEqual(item['stage'], 'ready')
        self.assertEqual(item['company_id'], 'AG1')
        self.assertEqual(item['trigger']['record_id'], 'J_GOOD')
        self.assertIn('Head of AI', item['brief']['why_now'])
        self.assertNotIn('Oracle Test Manager', item['brief']['why_now'])
        self.assertIn('Live outside-IR35 brief on the contract desk', item['brief']['unknowns'])
        self.assertNotIn('Named relevant consultant', item['brief']['unknowns'])
        self.assertNotIn('source evidence date', item['brief']['unknowns'])
        self.assertFalse(any('employment_type says permanent' in u for u in item['brief']['unknowns']))

    def test_agency_with_only_subthreshold_adverts_leads_with_verified_desk_remit(self):
        ag = dict(
            id='AG2', type='agency', kind='agency', name='Example Leadership Search',
            subtype='Tier 1 — Engineering leadership', summary='Technology leadership recruitment.',
            research=[dict(
                reviewed_at='2026-10-03', verification='primary-read',
                desk_summary='Places interim and fractional CTO, VP Engineering and AI leadership roles.',
                public_contact_route='https://search.example.com/contact',
                source_urls=['https://search.example.com/team'],
                decisive_unknowns=['Current interim CTO or VP Engineering mandates'])],
            relevance={'score': 22, 'band': 'possible'},
        )
        consultant = person('P2', name='The owner Recruiter', role='Senior Director, Engineering', kind='recruiter')
        ic_advert = advert(
            'J_IC', 'Founding Software Engineer',
            'Hands-on React and Python product engineering role.',
            relevance={'score': 22, 'band': 'possible', 'last_seen': '2026-10-03', 'qualification': {
                'eligible': True, 'domain': 'software', 'engagement': 'unknown', 'conflicts': []}},
        )
        s = dict(
            records=[ag, consultant, ic_advert],
            links=[
                dict(from_id='P2', to_id='AG2', relation='consultant_at'),
                dict(from_id='J_IC', to_id='AG2', relation='hiring_for'),
            ],
        )
        result = intelligence.build(s, TODAY)
        item = next(a for a in result['actions'] if a['record_id'] == 'P2')
        self.assertEqual(item['stage'], 'ready')
        self.assertNotIn('Founding Software Engineer', item['brief']['why_now'])
        self.assertIn('Places interim and fractional CTO', item['brief']['why_now'])


if __name__ == '__main__':
    unittest.main()
