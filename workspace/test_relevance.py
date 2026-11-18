"""Derived relevance over synthetic records; the live config, no database."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relevance
import profile_rules
from test_profile_rules import EXAMPLE

TODAY = dt.date(2026, 9, 23)


def rec(id, type, name, **values):
    return dict(id=id, type=type, name=name, **values)


def pay_floor(day, year):
    """Score with a synthetic pay floor instead of the configured one."""
    config = copy.deepcopy(relevance.load_config())
    config['pay'].update(floor_day_gbp=day, floor_year_gbp=year)
    return patch.object(relevance, 'load_config', return_value=config)


def scored(records, links=(), interactions=(), drafts=(), sightings=None):
    state = dict(records=list(records), interactions=list(interactions), drafts=list(drafts),
                 links=[dict(from_id=a, to_id=b, relation=rel) for a, b, rel in links], sightings=sightings or {})
    return relevance.score(state, relevance.load_config(), TODAY)


class RelevanceTests(unittest.TestCase):
    def test_absent_literal_terms_skip_regex_search_but_present_terms_keep_boundaries(self):
        from unittest.mock import patch
        searches = []
        import job_rules
        pattern = job_rules._word_pattern

        class CountedPattern:
            def __init__(self, term):
                self.term, self.compiled = term, pattern(term)

            def search(self, text):
                searches.append((self.term, text))
                return self.compiled.search(text)

        with patch.object(job_rules, '_word_pattern', side_effect=CountedPattern):
            for term, text in (('engineering manager', 'software developer'), ('AI', 'software engineering'),
                               ('a.b', 'axb'), ('C++', 'c# engineer')):
                self.assertFalse(relevance.word(term, text))
            self.assertEqual(searches, [])
            self.assertFalse(relevance.word('ai', 'said'))
            self.assertTrue(relevance.word('AI', 'ai engineer'))
        self.assertEqual(searches, [('ai', 'said'), ('AI', 'ai engineer')])

    def test_an_advertiser_is_compared_with_the_agencies_once_however_many_adverts_it_has(self):
        compared = []
        real = relevance.same_key

        def counted(a, b):
            compared.append((a, b))
            return real(a, b)

        records = [rec('G1', 'agency', 'Sanderson'), rec('G2', 'agency', 'Harvey Nash')]
        records += [rec(f'J{n}', 'job', 'Engineering Manager', organisation='Sanderson Recruitment') for n in range(5)]
        records += [rec(f'D{n}', 'job', 'Engineering Manager', organisation='Direct Employer Ltd') for n in range(5)]
        scorer = relevance.Scorer(dict(records=records, links=[], interactions=[], drafts=[], sightings={}),
                                  relevance.load_config(), TODAY)
        with patch.object(relevance, 'same_key', counted):
            for record in records[2:]:
                scorer.route(record, None)
        self.assertTrue(all(scorer.routes[f'J{n}']['via_agency'] for n in range(5)))
        self.assertFalse(any(scorer.routes[f'D{n}']['via_agency'] for n in range(5)))
        # One advertiser matches its first agency; the other is compared with both, once.
        self.assertLessEqual(len(compared), 4)
        self.assertEqual({a for a, _ in compared}, {'sanderson recruitment', 'direct employer ltd'})

    def test_literal_term_matching_preserves_case_punctuation_and_ascii_boundaries(self):
        cases = [('AI', 'ai', True), ('ai', 'AI', False), ('ai', 'said', False),
                 ('qa', 'qa_engineer', True), ('qa', 'qa2', False), ('qa', 'éqaé', True),
                 ('c++', 'c++ engineer', True), ('c++', 'abc++', False), ('c#', 'c# engineer', True),
                 ('a.b', 'a.b', True), ('a.b', 'axb', False), ('[qa]', '[qa]', True),
                 ('a b', 'a  b', False), ('A\\B', 'a\\b', True), ('İ', 'i\u0307', True),
                 ('İ', 'i', False), ('é', 'café', False), ('é', ' é ', True), ('ss', 'ß', False),
                 ('😀', '😀', True), ('', '', True), ('', 'abc', False)]
        for term, text, expected in cases:
            with self.subTest(term=term, text=text):
                self.assertEqual(relevance.word(term, text), expected)

    def test_cv_suggestion_retains_actual_title_match_and_rule_precedence(self):
        cases = [
            ('QA Engineering Manager', '', 'QELead', 'title_rule', 'QA Engineering'),
            ('Engineering Team Lead', '', 'Manager', 'title_rule', 'Engineering Lead'),
            ('Staff Engineer', '', 'FDE', 'title_family', 'Staff Engineer'),
            ('Unmatched specialist', 'Engineering Manager', 'Director', 'title_family', 'Engineering Manager'),
        ]
        for title, role, cv, kind, term in cases:
            with self.subTest(title=title, role=role):
                row = rec('J', 'job', title, role=role)
                result = scored([row])['J']
                self.assertEqual(result['route']['cv'], cv)
                self.assertEqual(result['route']['suggested_cv'], cv)
                self.assertEqual(result['route']['suggestion_basis'], {'kind': kind, 'term': term})
                self.assertEqual(row, rec('J', 'job', title, role=role))

    def test_manual_cv_preserves_dynamic_suggestion_without_changing_ranking(self):
        record = rec('J', 'job', 'Engineering Manager', summary='Lead software engineering teams.')
        suggested = scored([record])['J']
        manual = scored([dict(record, cv_override='Founding')])['J']
        alias = scored([dict(record, cv_override='HeadQE')])['J']
        for result, cv in ((manual, 'Founding'), (alias, 'QE')):
            with self.subTest(cv=cv):
                self.assertEqual(result['route']['cv'], cv)
                self.assertEqual(result['route']['selection'], 'manual')
                self.assertEqual(result['route']['suggested_cv'], 'Manager')
                self.assertEqual(result['route']['suggestion_basis'],
                                 {'kind': 'title_rule', 'term': 'Engineering Manager'})
                self.assertEqual({k: v for k, v in result.items() if k != 'route'},
                                 {k: v for k, v in suggested.items() if k != 'route'})
        changed_title = scored([dict(record, name='Head of Engineering', cv_override='Founding')])['J']
        self.assertEqual(changed_title['route']['cv'], 'Founding')
        self.assertEqual(changed_title['route']['suggested_cv'], 'Director')
        self.assertEqual(changed_title['route']['suggestion_basis'],
                         {'kind': 'title_rule', 'term': 'Head of Engineering'})
        reset = scored([dict(record, name='Head of Engineering', cv_override=None)])['J']
        self.assertEqual(reset['route']['cv'], 'Director')
        self.assertEqual(reset['route']['selection'], 'suggested')

    def test_unmatched_manual_cv_does_not_invent_a_suggestion(self):
        row = rec('J', 'job', 'Unmatched specialist')
        before = scored([row])['J']
        self.assertNotIn('route', before)
        after = scored([dict(row, cv_override='Founding')])['J']
        self.assertEqual(after['route']['cv'], 'Founding')
        self.assertIsNone(after['route']['suggested_cv'])
        self.assertIsNone(after['route']['suggestion_basis'])
        self.assertEqual({k: v for k, v in after.items() if k != 'route'}, before)

    def test_cv_readiness_does_not_select_or_explain_a_suggestion(self):
        row = rec('J', 'job', 'Head of Engineering')
        registry = {
            'Director': {'title': 'Leadership CV', 'url': 'https://example.test/director', 'readiness': 'draft'},
            'Manager': {'title': 'Management CV', 'url': 'https://example.test/manager', 'readiness': 'ready'},
        }
        first = relevance.score(dict(records=[row], cvs=registry), relevance.load_config(), TODAY)['J']
        flipped = {key: dict(value, readiness='ready' if key == 'Director' else 'draft')
                   for key, value in registry.items()}
        second = relevance.score(dict(records=[row], cvs=flipped), relevance.load_config(), TODAY)['J']
        for result, readiness in ((first, 'draft'), (second, 'ready')):
            with self.subTest(readiness=readiness):
                self.assertEqual(result['route']['cv'], 'Director')
                self.assertEqual(result['route']['suggested_cv'], 'Director')
                self.assertEqual(result['route']['suggestion_basis'],
                                 {'kind': 'title_rule', 'term': 'Head of Engineering'})
                self.assertEqual(result['route']['readiness'], readiness)
        self.assertEqual({k: v for k, v in first.items() if k != 'route'},
                         {k: v for k, v in second.items() if k != 'route'})

    def test_cv_projection_preserves_outside_domain_gate_and_manual_choice(self):
        row = rec('J', 'job', 'Head of Quality Engineering',
                  summary='Lead quality engineering at a manufacturing plant, responsible for electrical engineering.')
        before = scored([row])['J']
        self.assertFalse(before['qualification']['eligible'])
        self.assertNotIn('route', before)
        after = scored([dict(row, cv_override='Manager')])['J']
        self.assertEqual(after['route']['cv'], 'Manager')
        self.assertEqual(after['route']['selection'], 'manual')
        self.assertIsNone(after['route']['suggested_cv'])
        self.assertIsNone(after['route']['suggestion_basis'])
        self.assertEqual({k: v for k, v in after.items() if k != 'route'}, before)

    def test_intake_role_equivalents_also_receive_the_correct_cv_route(self):
        for title, cv in (
            ('Principal QA Engineer', 'QELead'), ('Staff QA Engineer', 'QELead'),
            ('Senior QA Engineer – (Security)', 'QELead'),
            ('Senior Quality Assurance Engineer', 'QELead'),
            ('Software Dev. Engineer in Test', 'QELead'),
            ('Test Automation Manager', 'QELead'), ('Founding Senior Engineer', 'Founding'),
            ('Engineering Team Lead', 'Manager'), ('Director, Software Engineering', 'Director'),
            ('Founding Staff Software Engineer', 'Founding'),
            ('Test Automation Lead / Engineering Manager', 'QELead'),
        ):
            with self.subTest(title=title):
                row = rec('J', 'job', title)
                result = scored([row])['J']
                self.assertEqual(result['route']['cv'], cv)
                self.assertEqual(row['name'], title)

    def test_concrete_delivery_and_model_evaluation_remits_have_bounded_overlap(self):
        for text in ('Lead engineering delivery and coaching across product teams.',
                     'Build and lead the engineering organisation.',
                     'Build evaluation hooks and fallbacks for a model integration.'):
            with self.subTest(text=text):
                result = scored([rec('J', 'job', 'Engineering Manager', summary=text)])['J']
                self.assertEqual(result['fit']['status'], 'adjacent')
                self.assertFalse(result['fit']['gaps'])
        for text in ('Engineering delivery and coaching.', 'Our customers build evaluation hooks.',
                     'Do not lead the engineering organisation.'):
            with self.subTest(text=text):
                self.assertEqual(scored([rec('J', 'job', 'Engineering Manager', summary=text)])['J']['fit']['status'], 'unconfirmed')

    def test_pay_text_is_compared_when_no_structured_scanner_pay_exists(self):
        self.enterContext(pay_floor(450, 68000))
        row = rec('J', 'job', 'QA Manager', pay='GBP 60,000–65,000/year',
                  summary='Lead quality engineering teams.')
        result = scored([row])['J']
        self.assertEqual(result['band'], 'possible')
        self.assertTrue(any(r.get('below_pay_floor') for r in result['reasons']))

    def test_below_floor_pay_cannot_be_a_strong_recommendation(self):
        self.enterContext(pay_floor(450, 68000))
        row = rec('J', 'job', 'QA Manager', location='London', engagement='permanent',
                  date=TODAY.isoformat(), pay_currency='GBP', pay_period='year', pay_max='65000',
                  summary='Lead software quality engineering and improve release readiness.')
        result = scored([row])['J']
        self.assertGreater(result['score'], 55)
        self.assertEqual(result['band'], 'possible')
        self.assertTrue(any(r.get('below_pay_floor') for r in result['reasons']))
        for currency, pay in [('GBP', '68000'), ('EUR', '65000')]:
            result = scored([dict(row, pay_currency=currency, pay_max=pay)])['J']
            self.assertFalse(any(r.get('below_pay_floor') for r in result['reasons']))

    def test_a_working_message_is_not_a_draft_waiting_for_review(self):
        out = scored([rec('K', 'person', 'Someone')],
                     drafts=[dict(record_id='K', version_id=1, purpose='message', text='Hello')])
        self.assertFalse(any('waiting for your review' in x['text'] for x in out['K']['reasons']))
        self.assertNotIn('readiness', out['K'])
        waiting = scored([rec('K', 'person', 'Someone')], drafts=[dict(record_id='K', version_id=1, text='Hello')])
        self.assertFalse(any('waiting for your review' in x['text'] for x in waiting['K']['reasons']))
        self.assertNotIn('readiness', waiting['K'])

    def test_target_role_in_london_outranks_foreign_onsite_presales(self):
        out = scored([
            rec('J1', 'job', 'Interim Head of Engineering', engagement='interim', location='London', date='2026-09-18'),
            rec('J2', 'job', 'Enterprise Solutions Engineer - France', engagement='permanent', location='France',
                workplace='onsite', date='2026-03-11'),
        ])
        self.assertGreater(out['J1']['score'], out['J2']['score'])
        self.assertEqual(out['J2']['band'], 'low')
        self.assertTrue(any('France' in x['text'] and x['weight'] < 0 for x in out['J2']['reasons']))

    def test_work_is_laned_contract_permanent_unstated_or_pipeline(self):
        out = scored([
            rec('C', 'job', 'Test Manager', engagement='contract'),
            rec('I', 'job', 'Interim Head of QA', engagement='interim'),
            rec('F', 'job', 'QA Lead', engagement='ftc'),
            rec('T', 'tender', 'Software testing services', engagement='procurement'),
            rec('P', 'job', 'Engineering Manager', engagement='permanent'),
            rec('U', 'job', 'Engineering Manager', engagement='unknown'),
            rec('B', 'job', 'Engineering Manager'),
            rec('O', 'opportunity', 'Agency desk'),
            rec('OC', 'opportunity', 'Fractional QA lead', engagement='fractional'),
            rec('E', 'event', 'Meetup'),
            rec('X', 'person', 'Someone'),
        ])
        lanes = {k: v.get('lane') for k, v in out.items()}
        self.assertEqual(lanes, dict(C='contract', I='contract', F='contract', T='contract', P='permanent',
                                     U='unstated', B='unstated', O='pipeline', OC='contract', E='event', X=None))

    def test_a_recent_raise_lifts_an_organisation_and_an_old_one_does_not(self):
        story = lambda d: dict(company='X', amount='£16.1m', published=d, region='UK', feed='uktn', headline='X secures £16.1m')
        out = scored([rec('A', 'company', 'Prevalent AI', signals=[story('2026-08-24')]),
                      rec('B', 'company', 'Old Raise', signals=[story('2025-12-01')]),
                      rec('C', 'company', 'No News')])
        reason = [x for x in out['A']['reasons'] if 'Raised' in x['text']]
        self.assertEqual(len(reason), 1)
        self.assertGreater(reason[0]['weight'], 0)
        self.assertIn('£16.1m', reason[0]['text'])
        self.assertIn('24 Aug 2026', reason[0]['text'])
        self.assertFalse(any('Raised' in x['text'] and x['weight'] for x in out['B']['reasons']))
        self.assertGreater(out['A']['score'], out['C']['score'])
        self.assertEqual(out['A'].get('funded'), '2026-08-24')

    def test_qe_and_ai_assurance_firms_rank_as_organisations_to_watch(self):
        out = scored([rec('Q', 'company', 'Resillion', subtype='QE consultancy'),
                      rec('A', 'company', 'Advai', subtype='AI assurance'),
                      rec('N', 'company', 'Plain Co')])
        self.assertTrue(any('Quality engineering consultancy' in x['text'] for x in out['Q']['reasons']))
        self.assertTrue(any('AI assurance' in x['text'] for x in out['A']['reasons']))
        self.assertEqual(out['Q']['band'], 'possible')
        self.assertEqual(out['N']['band'], 'low')

    def test_name_variants_and_agency_boilerplate_are_only_possible_matches(self):
        body = 'Contract: Tech Lead / Software Engineering Manager - Backend (TypeScript / Java, AWS) Location: London - 1x Day Per Week Onsite'
        ai = 'AI Evaluation Engineer Rate / Duration / Location £700-£750/day Inside IR35 | Duration: 4 months | Clearance: BPSS'
        out = scored([
            rec('S1', 'job', 'Software Engineering Manager', organisation='Sanderson', location='London, UK', summary=body),
            rec('S2', 'job', 'Software Engineering Manager', organisation='Sanderson Recruitment', location='South East London', summary=body),
            rec('T', 'job', 'AI Evaluation Engineer', organisation='TXP', location='Farringdon', summary=ai),
            rec('C', 'job', 'AI Evaluation Engineer', organisation='Concept Resourcing', location='London, UK', summary=ai),
            rec('M', 'job', 'AI Evaluation Engineer', organisation='Metaswitch', location='London', summary='Different advert text entirely, about something else.'),
            rec('P1', 'job', 'Forward Deployed Engineer', organisation='Palantir', location='London', summary=body + ' one text'),
            rec('P2', 'job', 'Forward Deployed Engineer', organisation='Palantir', location='New York', summary=body + ' one text'),
        ])
        self.assertFalse(any('also listed as' in x['text'] for x in out['P1']['reasons'] + out['P2']['reasons']))
        copies = [k for k in ('S1', 'S2') if any('also listed as' in x['text'] for x in out[k]['reasons'])]
        self.assertEqual(len(copies), 0)
        self.assertTrue(any('Concept Resourcing' in x['text'] and 'unconfirmed' in x['text'] and x.get('note') for x in out['T']['reasons']))
        self.assertTrue(any('TXP' in x['text'] for x in out['C']['reasons']))
        self.assertFalse(any('also' in x['text'].lower() for x in out['M']['reasons']))
        self.assertFalse(any('also listed as' in x['text'] for x in out['T']['reasons'] + out['C']['reasons']))

    def test_matching_headline_or_boilerplate_never_confirms_same_job(self):
        body = 'Lead software engineering for our product and our customers. ' * 4
        rows = [rec('A', 'job', 'Engineering Manager', organisation='Example', location='London',
                    summary=body, url='https://example.com/jobs/one'),
                rec('B', 'job', 'Engineering Manager', organisation='Example', location='London',
                    summary=body, url='https://example.com/jobs/two'),
                rec('C', 'job', 'Engineering Manager', organisation='Example Recruitment', location='London', summary=body)]
        out = scored(rows)
        self.assertFalse(any(r.get('duplicate_of') for r in out.values()))
        self.assertFalse(any('one end role' in x['text'] for r in out.values() for x in r['reasons']))

    def test_confirmed_identity_is_transitive_and_source_ids_are_scoped(self):
        rows = [rec('A', 'job', 'First title', url='https://www.linkedin.com/jobs/view/123/'),
                rec('B', 'job', 'Changed title', url='https://www.linkedin.com/comm/jobs/view/title-123?tracking=1',
                    advert={'source_scope': 'board:example', 'source_id': 'abc'}),
                rec('C', 'job', 'Another title', advert={'source_scope': 'board:example', 'source_id': 'abc'}),
                rec('D', 'job', 'Another title', advert={'source_scope': 'board:other', 'source_id': 'abc'})]
        out = scored(rows)
        self.assertEqual(out['B']['duplicate_of'], 'A')
        self.assertEqual(out['C']['duplicate_of'], 'A')
        self.assertNotIn('duplicate_of', out['D'])

    def test_decisions_and_assigned_steps_do_not_manufacture_fit(self):
        import states
        out = scored([rec('W', 'agency', 'Contra', owner='owner', working_state='waiting', waiting_on=states.DECISION_WAITING,
                          next_action='Join Contra?'),
                      rec('T', 'agency', 'Other', owner='owner', working_state='ready', next_action='Call them')])
        self.assertEqual(out['W']['band'], 'low')
        self.assertIn(('Waiting on your answer to a decision', 0), [(x['text'], x['weight']) for x in out['W']['reasons']])
        self.assertTrue(any(x['text'].startswith('Your next step') and x['weight'] == 0 and x.get('readiness')
                            for x in out['T']['reasons']))
        self.assertEqual(out['W']['score'], out['T']['score'])

    def test_unstated_facts_are_neutral(self):
        out = scored([rec('J', 'job', 'Engineering Manager')])
        texts = {x['text']: x['weight'] for x in out['J']['reasons']}
        self.assertEqual(texts['Pay not stated'], 0)
        self.assertEqual(texts['Location not stated'], 0)
        self.assertEqual(texts['Engagement not stated'], 0)

    def test_pay_below_floor_is_named_and_foreign_currency_not_compared(self):
        self.enterContext(pay_floor(450, 68000))
        out = scored([
            rec('LOW', 'job', 'Engineering Manager', pay_min='300', pay_max='400', pay_currency='GBP', pay_period='day'),
            rec('EUR', 'job', 'Engineering Manager', pay_max='110000', pay_currency='EUR', pay_period='year'),
        ])
        self.assertTrue(any('below your £450/day floor' in x['text'] for x in out['LOW']['reasons']))
        self.assertTrue(any('not compared' in x['text'] and x['weight'] == 0 for x in out['EUR']['reasons']))

    def test_remote_tied_to_abroad_is_penalised_but_open_remote_is_not(self):
        out = scored([
            rec('A', 'job', 'Engineering Manager', location='Germany / Remote'),
            rec('B', 'job', 'Engineering Manager', location='Remote (Europe)'),
        ])
        self.assertLess(out['A']['score'], out['B']['score'])

    def test_person_contact_needs_own_evidence_not_a_pipeline_date(self):
        out = scored([
            rec('P1', 'person', 'Known', role='Coach', relationship='warm'),
            rec('P2', 'person', 'Named in pipeline', role='CTO'),
            rec('O2', 'opportunity', 'Row', stage='lead', last_touch='2026-09-03'),
        ], links=[('O2', 'P2', 'related')], interactions=[dict(record_id='P1', at='2026-08-27')])
        self.assertTrue(out['P1']['known'])
        self.assertFalse(out['P2']['known'])
        self.assertGreater(out['P1']['score'], out['P2']['score'])
        self.assertTrue(any('27 Aug 2026' in x['text'] for x in out['P1']['reasons']))
        self.assertTrue(any(x['text'] == 'No recorded contact with you yet' for x in out['P2']['reasons']))

    def test_former_employer_warning_in_context_becomes_a_reason(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        warning = 'Example Corp: check confidentiality and non-solicitation obligations before contact.'
        out = scored([rec('P1', 'person', 'Flagged', known_context='Imported. ' + warning),
                      rec('P2', 'person', 'Plain', known_context='Example Corp mentioned without the warning')])
        expected = 'Example Corp contact: check confidentiality and non-solicitation obligations before contact'
        self.assertIn(expected, [x['text'] for x in out['P1']['reasons']])
        self.assertNotIn(expected, [x['text'] for x in out['P2']['reasons']])

    def test_pipeline_date_is_not_contact_and_watch_only_ranks_low(self):
        out = scored([
            rec('W', 'opportunity', 'Board watch', stage='lead', last_touch='2026-09-03', next_action='Watch the Lever board'),
            rec('R', 'opportunity', 'Warm ask', stage='lead', last_touch='2026-09-03', next_action='The owner sends the draft'),
            rec('H', 'opportunity', 'Parked', stage='lead', last_touch='2026-07-05', next_action='On hold — deprioritised'),
            rec('P', 'person', 'Known'),
        ], links=[('R', 'P', 'related')], interactions=[dict(record_id='P', at='2026-07-01')])
        self.assertFalse(any('touch point' in x['text'] for x in out['W']['reasons']))
        self.assertTrue(any('Watch only' in x['text'] for x in out['W']['reasons']))
        self.assertGreater(out['R']['score'], out['W']['score'])
        self.assertGreater(out['R']['score'], out['H']['score'])

    def test_draft_changes_readiness_without_lifting_commercial_fit(self):
        out = scored([
            rec('J', 'job', 'Programme Test Lead', engagement='contract'),
            rec('K', 'job', 'Programme Test Lead', engagement='contract', organisation='Other'),
        ], drafts=[dict(record_id='J')])
        self.assertFalse(any('draft is waiting' in x['text'] for x in out['J']['reasons']))
        self.assertEqual(out['J']['score'], out['K']['score'])
        self.assertNotIn('readiness', out['J'])

    def test_organisation_ranks_on_relevant_roles_and_people(self):
        out = scored([
            rec('C1', 'company', 'Good Co'),
            rec('C2', 'company', 'Quiet Co'),
            rec('J1', 'job', 'Head of Engineering', engagement='contract', location='London'),
            rec('P1', 'person', 'Known'),
        ], links=[('J1', 'C1', 'hiring_for'), ('P1', 'C1', 'related')],
            interactions=[dict(record_id='P1', at='2026-09-01')])
        self.assertGreater(out['C1']['score'], out['C2']['score'])
        self.assertTrue(any('relevant role' in x['text'] for x in out['C1']['reasons']))

    def test_closed_or_past_deadline_adverts_drop(self):
        out = scored([
            rec('T', 'tender', 'Head of Quality', published_deadline='2026-09-01'),
            rec('G', 'job', 'Head of Quality', source_lifecycle='closed'),
            rec('L', 'job', 'Head of Quality'),
        ])
        self.assertLess(out['T']['score'], out['L']['score'])
        self.assertLess(out['G']['score'], out['L']['score'])

    def test_an_advert_no_source_has_listed_for_two_weeks_drops(self):
        roles = {k: rec(k, 'job', 'Head of Quality', organisation=f'Org {k}') for k in 'ABMG'}
        roles['G']['source_lifecycle'] = 'closed'
        roles['T'] = rec('T', 'tender', 'Head of Quality', organisation='Org T', published_deadline='2026-10-30')
        out = scored(roles.values(), sightings={'A': '2026-09-01T05:30:00Z', 'B': '2026-09-20T05:30:00Z',
                                                'G': '2026-09-01T05:30:00Z', 'T': '2026-09-01T05:30:00Z'})
        unseen = lambda k: [x['text'] for x in out[k]['reasons'] if x.get('unseen')]
        self.assertEqual(unseen('A'), ['No source has listed it since 1 Sep 2026; it may have closed'])
        self.assertLess(out['A']['score'], out['B']['score'])
        self.assertEqual([unseen(k) for k in 'BMGT'], [[]] * 4)  # recent, never scanned, already gone, closes later

    def test_duplicate_advert_points_to_the_copy_with_owners_work(self):
        out = scored([
            rec('Job6', 'job', 'Interim Senior Engineering Manager', organisation='Trainline', location='London / Remote', next_action='refresh form', url='https://example.com/role/1'),
            rec('SCAN-1', 'job', 'Interim Senior Engineering Manager', organisation='Trainline', location='London / Remote', url='https://example.com/role/1'),
            rec('SCAN-2', 'job', 'Interim Senior Engineering Manager', organisation='Trainline', location='Berlin'),
        ])
        self.assertTrue(any('Same role also listed as Job6' in x['text'] for x in out['SCAN-1']['reasons']))
        self.assertFalse(any('Same role also listed' in x['text'] for x in out['Job6']['reasons'] + out['SCAN-2']['reasons']))
        self.assertGreater(out['Job6']['score'], out['SCAN-1']['score'])

    def test_escaped_duplicate_organisation_points_to_the_clean_record(self):
        out = scored([rec('CO-a', 'company', 'Newton&#x27;s Tree'), rec('CO-b', 'company', "Newton's Tree")])
        self.assertTrue(any('Same organisation as CO-b' in x['text'] for x in out['CO-a']['reasons']))
        self.assertFalse(any('Same organisation' in x['text'] for x in out['CO-b']['reasons']))

    def test_agency_with_a_company_record_is_not_a_duplicate(self):
        out = scored([rec('AG-1', 'agency', 'Client Server'), rec('CO-1', 'company', 'Client Server')])
        self.assertFalse(any('Same organisation' in x['text'] for r in out.values() for x in r['reasons']))

    def test_linked_names_are_listed_once(self):
        out = scored([rec('CO', 'company', 'Org'), rec('P', 'person', 'Known', relationship='warm'),
                      rec('J1', 'job', 'Head of Engineering', organisation='Org', location='London', engagement='contract', url='https://example.com/role/1'),
                      rec('J2', 'job', 'Head of Engineering', organisation='Org', location='London', engagement='contract', url='https://example.com/role/1')],
                     links=[('P', 'CO', 'related'), ('P', 'CO', 'consultant_at'), ('J1', 'CO', 'hiring_for'), ('J2', 'CO', 'hiring_for')],
                     interactions=[dict(record_id='P', at='2026-09-01')])
        texts = [x['text'] for x in out['CO']['reasons']]
        self.assertIn('Someone you know is here: Known', texts)
        self.assertIn('1 relevant role: Head of Engineering', texts)

    def test_scoring_never_changes_records(self):
        r = rec('J', 'job', 'Engineering Manager', working_state='needs_review')
        before = dict(r)
        scored([r])
        self.assertEqual(r, before)

    def test_reasons_are_ordered_by_weight(self):
        out = scored([rec('J', 'job', 'Head of Engineering', location='Paris', engagement='contract')])
        weights = [abs(x['weight']) for x in out['J']['reasons']]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_roles_route_to_the_cv_in_rates_md_and_qe_ranks_below_leadership(self):
        out = scored([
            rec('Q', 'job', 'Test Manager', engagement='contract', location='London'),
            rec('L', 'job', 'Interim Head of Engineering', engagement='contract', location='London'),
            rec('A', 'job', 'AI Evaluation Engineer', engagement='contract', location='London'),
            rec('F', 'job', 'Forward Deployed Engineer', engagement='contract', location='London'),
            rec('B', 'job', 'Founding Engineer', engagement='contract', location='London'),
            rec('M', 'job', 'QA Engineering Manager', engagement='contract', location='London'),
            rec('W', 'job', 'Solutions Engineer', engagement='contract', location='London'),
        ])
        route = {k: (v.get('route') or {}).get('cv') for k, v in out.items()}
        self.assertEqual(route, dict(Q='QELead', L='Director', A='QELead', F='FDE', B='Founding', M='QELead', W=None))
        self.assertEqual(out['M']['route']['family'], 'quality')
        self.assertGreater(out['L']['score'], out['Q']['score'])
        self.assertTrue(any('cash channel' in x['text'] for x in out['Q']['reasons']))

    def test_title_location_and_pay_leave_unestablished_remit_possible(self):
        out = scored([rec('Q', 'job', 'Test Lead', engagement='contract', location='London',
                          pay_min='500', pay_max='500', pay_currency='GBP', pay_period='day')])
        self.assertEqual(out['Q']['band'], 'possible')
        self.assertEqual(out['Q']['fit']['status'], 'unconfirmed')

    def test_adverts_a_scan_found_in_the_last_week_are_marked_found(self):
        out = scored([
            rec('F', 'job', 'Engineering Manager', source_file='source:feed:reed:Reed UK — London', created_at='2026-09-22T06:31:40+01:00'),
            rec('B', 'job', 'Test Lead', source_file='source:board:ashby:acme', created_at='2026-09-17T06:31:40+01:00'),
            rec('T', 'tender', 'Testing services', source_file='source:tender:cf:123', created_at='2026-09-23T06:31:40+01:00'),
            rec('R', 'job', 'Head of Engineering', source_file='source:research:2026-09-22-x.json', created_at='2026-09-22T10:00:00+01:00'),
            rec('C', 'company', 'Acme', source_file='source:feed:reed:Reed UK — London', created_at='2026-09-22T06:31:40+01:00'),
            rec('O', 'job', 'Engineering Manager', source_file='source:feed:reed:Reed UK — London', created_at='2026-09-16T06:31:40+01:00'),
        ])
        self.assertEqual({k: v.get('found') for k, v in out.items() if v.get('found')},
                         dict(F='2026-09-22', B='2026-09-17', T='2026-09-23'))
        reason = next(x for x in out['F']['reasons'] if x['text'].startswith('Found'))
        self.assertEqual((reason['weight'], reason['text']), (0, 'Found on 22 Sep 2026 via Reed'))
        self.assertIn('via a public tender notice', ' '.join(x['text'] for x in out['T']['reasons']))

    def test_a_funded_or_hiring_company_is_one_to_approach_with_who_and_why(self):
        story = dict(company='X', amount='£5m', published='2026-09-10', region='UK', feed='uktn', headline='X raises £5m')
        lead = dict(engagement='interim', location='London', date='2026-09-20')
        out = scored([
            rec('A', 'company', 'Acme AI', signals=[story], research=[dict(signal='Shipped an AI assistant in August.')]),
            rec('P', 'person', 'Jane Example', role='CTO'),
            rec('B', 'company', 'Beta', signals=[story]),
            rec('C', 'company', 'Gamma'),
            rec('Q', 'person', 'Sam Example', role='CTO'),
            rec('H', 'company', 'Hiring Co'),
            rec('HJ', 'job', 'Interim Head of Engineering', organisation='Hiring Co', **lead),
            rec('K', 'person', 'Kim Known', role='Head of Engineering', last_touch='2026-06-01', relationship='warm'),
            rec('G', 'agency', 'Sanderson'),
            rec('V', 'company', 'Via Agency Client'),
            rec('VJ', 'job', 'Interim Head of Engineering', organisation='Sanderson', **lead),
        ], links=[('P', 'A', 'works_at'), ('Q', 'C', 'works_at'), ('HJ', 'H', 'hiring_for'), ('K', 'H', 'works_at'),
                  ('VJ', 'G', 'hiring_for'), ('VJ', 'V', 'related')])
        self.assertEqual(out['A']['approach'], dict(trigger='funded', named=True, warm=False))
        self.assertEqual(out['B']['approach'], dict(trigger='funded', named=False, warm=False))
        self.assertNotIn('approach', out['C'])  # a named CTO alone is not a reason to approach
        self.assertEqual(out['H']['approach'], dict(trigger='hiring', named=True, warm=True))
        self.assertNotIn('approach', out['V'])  # the advert is an agency's, not the company's own
        self.assertNotIn('approach', out['G'])  # an agency is never a company to approach
        texts = lambda k: [x['text'] for x in out[k]['reasons']]
        self.assertIn('Named decision-maker: Jane Example, CTO', texts('A'))
        self.assertTrue(any(x['text'] == 'Why approach: Shipped an AI assistant in August.' and x.get('note')
                            for x in out['A']['reasons']))
        self.assertIn('Hiring now: Interim Head of Engineering', texts('H'))
        self.assertGreater(out['A']['score'], out['B']['score'])

    def test_historical_or_unrelated_contacts_are_not_current_decision_makers(self):
        for relation in ('formerly_at', 'former_colleague_at', 'introducer', 'related', 'possible_match'):
            with self.subTest(relation=relation):
                out = scored([
                    rec('C', 'company', 'Funded Co', signals=[dict(published='2026-09-20', amount='£5m')]),
                    rec('P', 'person', 'Past CTO', role='CTO', last_touch='2026-09-01'),
                ], links=[('P', 'C', relation)])
                self.assertEqual(out['C']['approach'], dict(trigger='funded', named=False, warm=False))
                self.assertFalse(any('is here' in x['text'] or 'Named decision-maker' in x['text']
                                     for x in out['C']['reasons']))

    def test_do_not_contact_person_does_not_become_an_approach_route(self):
        out = scored([
            rec('C', 'company', 'Funded Co', signals=[dict(published='2026-09-20', amount='£5m')]),
            rec('P', 'person', 'CTO', role='CTO', last_touch='2026-09-01', relationship='do_not_contact'),
        ], links=[('P', 'C', 'works_at')])
        self.assertEqual(out['C']['approach'], dict(trigger='funded', named=False, warm=False))

    def test_related_advert_does_not_claim_the_company_is_hiring(self):
        out = scored([
            rec('C', 'company', 'Co'),
            rec('J', 'job', 'Interim Head of Engineering', organisation='Elsewhere', engagement='contract', location='London'),
        ], links=[('J', 'C', 'related')])
        self.assertNotIn('approach', out['C'])

    def test_companies_house_facts_are_notes_on_a_company_to_approach_and_never_move_its_score(self):
        raise_ = dict(published='2026-09-20', amount='£5m')
        registry = dict(company_status='liquidation', officers=[dict(name='QUILL, Ada', role='director')],
                        allotments=[dict(filed_on='2026-09-09', allotted_on='2026-08-28', form='SH01'),
                                    dict(filed_on='2025-01-10', form='SH01')])
        out = scored([rec('A', 'company', 'Acme AI', signals=[raise_], registry=[registry]),
                      rec('B', 'company', 'Acme AI copy', signals=[raise_]),
                      rec('Q', 'company', 'Quiet', registry=[registry])])
        notes = [x['text'] for x in out['A']['reasons'] if x.get('note')]
        self.assertIn('Shares allotted 9 Sep 2026 (Companies House)', notes)
        self.assertIn('Companies House status: liquidation', notes)
        self.assertEqual(out['A']['score'], out['B']['score'])
        self.assertFalse(any('Companies House' in x['text'] for x in out['Q']['reasons']))
        self.assertFalse(any('QUILL' in x['text'] for x in out['A']['reasons']))  # a director is not a decision-maker

    def test_a_company_to_approach_says_who_is_named_why_now_and_whether_someone_known_is_there(self):
        # The outreach brief itself is derived by decision_intelligence; relevance keeps the reasons and the queue flags.
        story = dict(company='X', amount='£5m', published='2026-09-10', region='UK', headline='X raises £5m', url='https://news.test/x')
        lead = dict(engagement='interim', location='London', date='2026-09-20')
        research = dict(signal='Shipped an AI assistant in August.', source_urls=['https://acme.test/blog'],
                        decisive_unknowns=['who owns quality'])
        out = scored([
            rec('A', 'company', 'Acme AI', signals=[story], research=[research]),
            rec('P', 'person', 'Jane Example', role='CTO'),
            rec('B', 'company', 'Beta', signals=[story]),
            rec('H', 'company', 'Hiring Co'),
            rec('HJ', 'job', 'Interim Head of Engineering', organisation='Hiring Co', **lead),
            rec('K', 'person', 'Kim Known', role='Engineer', last_touch='2026-06-01', relationship='warm'),
        ], links=[('P', 'A', 'works_at'), ('HJ', 'H', 'hiring_for'), ('K', 'H', 'works_at')])
        notes = lambda k: [x['text'] for x in out[k]['reasons'] if x.get('note')]
        self.assertEqual(out['A']['approach'], dict(trigger='funded', named=True, warm=False))
        self.assertEqual(out['H']['approach'], dict(trigger='hiring', named=False, warm=True))
        self.assertIn('Why approach: Shipped an AI assistant in August.', notes('A'))
        self.assertIn('Hiring now: Interim Head of Engineering', notes('H'))
        self.assertFalse(any(x.startswith('Why approach') for x in notes('B')))
        self.assertNotIn('approach', out['P'])
        self.assertTrue(all('brief' not in out[k] for k in out))

    def test_a_sentence_shared_by_three_companies_is_not_a_reason_to_approach(self):
        story = dict(published='2026-09-10', amount='£5m')
        generic = dict(signal='What the company does, from its own job advert.')
        out = scored([rec(k, 'company', f'Co {k}', signals=[story], research=[generic]) for k in 'ABC']
                     + [rec('S', 'company', 'Specific', signals=[story], research=[dict(signal='Launched an eval harness.')])])
        for k in 'ABC':
            self.assertFalse(any(x['text'].startswith('Why approach') for x in out[k]['reasons']))
        self.assertIn('Why approach: Launched an eval harness.', [x['text'] for x in out['S']['reasons']])
        records = [rec(k, 'company', f'Co {k}', research=[generic]) for k in 'ABC']
        self.assertEqual(relevance.generic_signals(records), {generic['signal']})

    def test_a_vice_president_of_engineering_is_a_named_decision_maker(self):
        story = dict(published='2026-09-10', amount='£5m')
        out = scored([rec('W', 'company', 'Wayfarer', signals=[story]),
                      rec('P', 'person', 'Sam Example', role='Sr. Vice President, Engineering'),
                      rec('X', 'company', 'Other', signals=[story]),
                      rec('Q', 'person', 'Pat Example', role='Engineer')],
                     links=[('P', 'W', 'works_at'), ('Q', 'X', 'works_at')])
        self.assertTrue(out['W']['approach']['named'])
        self.assertFalse(out['X']['approach']['named'])

    def test_test_architect_titles_route_to_the_qe_cv(self):
        out = scored([rec('J', 'job', 'Test Architect - Non Functional & Performance', organisation='Acme', engagement='contract'),
                      rec('K', 'job', 'Head of Testing', organisation='Acme', engagement='contract')])
        self.assertEqual([out[k]['route']['family'] for k in 'JK'], ['quality', 'quality'])

    def test_agency_adverts_keep_the_same_remit_route_as_direct_adverts(self):
        out = scored([
            rec('AG', 'agency', 'Sanderson'),
            rec('J1', 'job', 'AI Evaluation Engineer', organisation='Sanderson Recruitment', engagement='contract'),
            rec('J2', 'job', 'Interim Head of Engineering', organisation='Sanderson', engagement='contract'),
            rec('J3', 'job', 'Forward Deployed Engineer', organisation='Northwind'),
            rec('J4', 'job', 'Forward Deployed Engineer', organisation='Sanderson'),
        ])
        import search_config
        cvs = search_config.cvs()
        for key, cv, family, via, term in [('J1', 'QELead', 'ai', True, 'AI Evaluation'),
                                          ('J2', 'Director', 'leadership', True, 'Head of Engineering'),
                                          ('J3', 'FDE', 'ai', False, 'Forward Deployed'),
                                          ('J4', 'FDE', 'ai', True, 'Forward Deployed')]:
            self.assertEqual(out[key]['route'], dict(cv=cv, family=family, via_agency=via, **cvs[cv],
                             selection='suggested', suggested_cv=cv,
                             suggestion_basis={'kind': 'title_rule', 'term': term}))


class ReadTimeRequirementFitTests(unittest.TestCase):
    def test_form_administration_does_not_create_candidate_fit_gaps(self):
        text = ('Improve release readiness.\nApplication form:\n'
                'If you have a preferred name, please enter it here.\n'
                'Your full legal name is required to complete this form.\n'
                'Email address is required.\nYou must upload your CV.')
        row = rec('FORM', 'job', 'Engineering Manager', summary=text,
                  url='https://example.test/jobs/manager', date='2026-09-20')
        result = scored([row])['FORM']
        self.assertEqual(result['fit']['status'], 'supported')
        self.assertEqual(result['fit']['requirements'], [])
        self.assertEqual(result['fit']['gaps'], [])
        self.assertEqual(result['fit']['optional_unknowns'], [])
        self.assertEqual(row['summary'], text)

    def test_real_eligibility_remains_unestablished_inside_application_form(self):
        text = ('Improve release readiness.\nApplication form:\nEmail address is required.\n'
                'Do you require visa sponsorship?\nYou must have the right to work in the UK.')
        result = scored([rec('FORM', 'job', 'Engineering Manager', summary=text,
                             url='https://example.test/jobs/manager', date='2026-09-20')])['FORM']
        self.assertEqual(result['band'], 'possible')
        self.assertEqual([(item['quote'], item['importance'], item['status'], item['url'])
                          for item in result['fit']['requirements']], [
            ('You must have the right to work in the UK.', 'essential', 'not_established', 'https://example.test/jobs/manager'),
            ('Do you require visa sponsorship?', 'ambiguous', 'not_established', 'https://example.test/jobs/manager'),
        ])
        self.assertEqual(result['fit']['gaps'],
                         ['Essential experience not established: You must have the right to work in the UK.'])

    def test_fit_evidence_distinguishes_advertised_duty_from_candidate_requirement(self):
        for text, expected in (('Improve release readiness.', 'remit'),
                               ('Release readiness is essential.', 'requirement'),
                               ('Leading quality engineering teams is required.', 'requirement')):
            with self.subTest(text=text):
                result = scored([rec('EVIDENCE-KIND', 'job', 'Engineering Manager', summary=text,
                                     url='https://example.com/kind', date='2026-09-20')])['EVIDENCE-KIND']
                evidence = result['fit']['evidence']
                self.assertEqual([(item['kind'], item['quote'], item['url'], item['date']) for item in evidence],
                                 [(expected, text, 'https://example.com/kind', '2026-09-20')])

    def test_bulleted_essential_experience_is_not_advertised_duty_evidence(self):
        for marker in ('-', '*', '•'):
            with self.subTest(marker=marker):
                text = f'Essential requirements:\n{marker} Leading quality engineering teams'
                result = scored([rec('BULLET-REQUIREMENT', 'job', 'Engineering Manager', summary=text,
                                     url='https://example.com/bullet', date='2026-09-20')])['BULLET-REQUIREMENT']
                self.assertEqual([(item['kind'], item['quote'], item['url'], item['date'])
                                  for item in result['fit']['evidence']],
                                 [('requirement', 'Leading quality engineering teams',
                                   'https://example.com/bullet', '2026-09-20')])

    def test_equivalent_owned_duties_have_equal_fit_without_changing_source_quotes(self):
        for text in ('Lead quality engineering teams and improve release readiness.',
                     'Drive quality engineering teams and strengthen release readiness.',
                     'You will drive quality engineering teams and strengthen release readiness.',
                     'Responsible for strengthening release readiness.'):
            with self.subTest(text=text):
                result = scored([rec('DUTY', 'job', 'Head of Quality Engineering', summary=text,
                                     location='London', date=TODAY.isoformat(), engagement='permanent',
                                     pay_max='100000', pay_currency='GBP', pay_period='year',
                                     url='https://example.com/duty')])['DUTY']
                self.assertEqual((result['fit']['status'], result['band'], result['score']), ('supported', 'strong', 140))
                self.assertEqual(result['fit']['evidence'][0]['quote'], text)
                self.assertEqual(result['fit']['evidence'][0]['url'], 'https://example.com/duty')
        for text in ('Our customers drive quality engineering teams.',
                     'You will not strengthen release readiness.',
                     'Drive sales while our engineers improve release readiness.'):
            with self.subTest(text=text):
                self.assertEqual(scored([rec('OTHER', 'job', 'Engineering Manager', summary=text)])['OTHER']['fit']['status'], 'unconfirmed')

    def test_physical_quality_duties_cannot_claim_software_fit_from_incidental_tools(self):
        text = ('Lead quality engineering at a manufacturing plant, responsible for electrical engineering. '
                'Use software testing tools for equipment diagnostics.')
        for title, summary in (('Head of Quality Engineering', text),
                               ('Head of Quality Engineering — AI-enabled manufacturing', text),
                               ('Head of Quality Engineering', text.replace('Use software', 'Implement software'))):
            with self.subTest(title=title, summary=summary):
                result = scored([rec('PHYSICAL', 'job', title, summary=summary,
                                     location='London', date=TODAY.isoformat(), engagement='permanent',
                                     pay_max='100000', pay_currency='GBP', pay_period='year')])['PHYSICAL']
                self.assertEqual(result['qualification']['domain'], 'outside')
                self.assertEqual(result['band'], 'low')
                self.assertEqual(result['fit']['status'], 'unconfirmed')
                self.assertFalse(result['fit']['evidence'])

    def test_general_communication_essential_stays_unknown_without_vetoing_supported_remit(self):
        requirement = 'Excellent communication skills are essential.'
        result = scored([rec('GENERAL', 'job', 'Head of Quality Engineering',
                             summary='Lead quality engineering teams. ' + requirement)])['GENERAL']
        self.assertEqual(result['band'], 'strong')
        self.assertFalse(result['fit']['gaps'])
        comparison = result['fit']['requirements'][0]
        self.assertEqual((comparison['quote'], comparison['importance'], comparison['status']),
                         (requirement, 'essential', 'not_established'))
        self.assertNotIn('proof_id', comparison)
        self.assertTrue(any(requirement in item for item in result['fit']['optional_unknowns']))
        unsupported = scored([rec('GENERAL-ONLY', 'job', 'Head of Quality Engineering', summary=requirement)])['GENERAL-ONLY']
        self.assertEqual(unsupported['fit']['status'], 'unconfirmed')
        self.assertFalse(unsupported['fit']['evidence'])
        for specialist in ('Communication skills and police-system experience are essential.',
                           'German communication skills are required.',
                           'Excellent communication skills in banking are essential.'):
            with self.subTest(specialist=specialist):
                result = scored([rec('SPECIALIST', 'job', 'Head of Quality Engineering',
                                     summary='Lead quality engineering teams. ' + specialist)])['SPECIALIST']
                self.assertEqual(result['band'], 'possible')
                self.assertTrue(result['fit']['gaps'])

    def test_fit_cache_distinguishes_software_title_from_physical_duties_with_identical_text(self):
        text = 'Lead quality engineering at a manufacturing plant.'
        rows = [rec('SOFTWARE-CACHE', 'job', 'Software Engineering Manager', summary=text),
                rec('PHYSICAL-CACHE', 'job', 'Head of Quality Engineering', summary=text)]
        for ordered in (rows, list(reversed(rows))):
            relevance._FIT_CACHE.clear()
            result = scored(ordered)
            self.assertEqual(result['SOFTWARE-CACHE']['fit']['status'], 'supported')
            self.assertEqual(result['PHYSICAL-CACHE']['fit']['status'], 'unconfirmed')
            self.assertFalse(result['PHYSICAL-CACHE']['fit']['evidence'])

    def test_flattened_advert_heading_and_contracted_conflicts_remain_uncertain(self):
        for text in ('Improve release readiness. Minimum requirements Police-system experience Benefits Flexible working.',
                     "Data integration is essential. Data integration isn't required.",
                     'Data integration is essential. Data integration isn’t required.'):
            with self.subTest(text=text):
                result = scored([rec('CONFLICT', 'job', 'Engineering Manager', summary=text)])['CONFLICT']
                self.assertNotEqual(result['band'], 'strong')
                self.assertTrue(result['fit']['gaps'])


    def test_waived_skill_cannot_swallow_an_independent_essential(self):
        for wording in ("You don't need data integration and police-system experience is essential.",
                        'You need not know Java and police-system experience is mandatory.'):
            with self.subTest(wording=wording):
                result = scored([rec('WAIVER', 'job', 'Engineering Manager',
                    summary='Improve release readiness. ' + wording)])['WAIVER']
                self.assertEqual(result['band'], 'possible')
                self.assertTrue(any('police-system' in gap for gap in result['fit']['gaps']))

    def test_mixed_requirement_importance_keeps_essential_gap(self):
        text = 'Improve release readiness. Police-system experience is essential and Python is preferred.'
        result = scored([rec('MIXED', 'job', 'Engineering Manager', summary=text)])['MIXED']
        self.assertEqual(result['band'], 'possible')
        self.assertTrue(any('Police-system' in gap for gap in result['fit']['gaps']))
        self.assertTrue(any(item['importance'] == 'desirable' for item in result['fit']['requirements']))

    def test_conjunction_cannot_be_satisfied_by_a_later_or_branch(self):
        for requirement in ('Police systems experience is required and either data integration or release readiness is required.',
                            'Police systems and data integration or release readiness are required.'):
            with self.subTest(requirement=requirement):
                result = scored([rec('AND-OR', 'job', 'Engineering Manager',
                    summary='Improve release readiness. ' + requirement)])['AND-OR']
                self.assertEqual(result['band'], 'possible')
                self.assertTrue(result['fit']['gaps'])

    def test_negated_role_actions_and_other_teams_work_do_not_establish_fit(self):
        for text in ("You don't need to build data integrations.", "You won't build AI evaluation systems; another team owns that.",
                     'You can’t lead quality engineering teams.', 'You will not improve release readiness.',
                     'Our QA team manages test automation. You will develop payroll reporting.',
                     'You will develop payroll reporting while our QA team manages test automation.',
                     'You will develop payroll reporting and our QA team manages test automation.',
                     'The engineering team builds data integrations. You will develop payroll reporting.'):
            with self.subTest(text=text):
                result = scored([rec('ACTOR', 'job', 'Engineering Manager', summary=text)])['ACTOR']
                self.assertNotEqual(result['band'], 'strong')
                self.assertEqual(result['fit']['status'], 'unconfirmed')
                self.assertFalse(result['fit']['evidence'])

    def test_explicit_role_owned_work_remains_supported(self):
        for text in ('Improve release readiness.', 'You will improve release readiness.',
                     'You’ll build data integrations.', 'This role will lead quality engineering teams.',
                     'Responsibilities: build AI evaluation systems.', 'Responsible for improving release readiness.'):
            with self.subTest(text=text):
                result = scored([rec('OWNER', 'job', 'Engineering Manager', summary=text)])['OWNER']
                self.assertEqual(result['band'], 'strong')
                self.assertEqual(result['fit']['status'], 'supported')

    def test_fit_reason_names_your_experience_rather_than_the_roles_terms(self):
        # A proof label such as "Interim engineering leadership" describes the owner's experience. Placed
        # first, it read as the advert's terms on a permanent or unstated role.
        for summary, expected in (
                ('Improve release readiness.',
                 'Advertised work overlaps your sourced experience in release reliability and quality.'),
                ('Lead software engineering teams.',
                 'Advertised work is adjacent to your sourced experience in interim engineering leadership.')):
            with self.subTest(summary=summary):
                result = scored([rec('S', 'job', 'Engineering Manager', engagement='permanent', summary=summary)])['S']
                self.assertEqual([x['text'] for x in result['reasons'] if x.get('fit')], [expected])
        self.assertEqual(relevance.experience_phrase('Existing-system AI evaluation'), 'existing-system AI evaluation')
        self.assertEqual(relevance.experience_phrase('AI assurance'), 'AI assurance')

    def test_evidenced_remit_beats_title_terms_freshness_and_network(self):
        rows = [rec('P', 'job', 'Engineering Manager', engagement='permanent',
                    summary='Lead software engineering teams and improve release readiness.', url='https://example.com/permanent'),
                rec('C', 'job', 'Interim Head of Engineering', engagement='contract', location='London',
                    date='2026-09-22', pay_max='850', pay_currency='GBP', pay_period='day'),
                rec('PERSON', 'person', 'Existing contact', relationship='warm')]
        result = scored(rows, links=[('C', 'PERSON', 'related')])
        self.assertGreater(result['P']['score'], result['C']['score'])
        self.assertEqual(result['P']['fit']['status'], 'supported')
        self.assertNotEqual(result['C']['band'], 'strong')
        reason = next(x for x in result['P']['reasons'] if x.get('fit'))
        self.assertIn('quote', reason['evidence'])
        self.assertEqual(reason['evidence']['url'], 'https://example.com/permanent')
        self.assertTrue(reason['evidence']['proof_id'])
        self.assertEqual(sum(x['weight'] for x in result['P']['reasons']), result['P']['score'])

    def test_unverified_essential_caps_fit_but_desirable_does_not(self):
        common = dict(engagement='contract', location='London', summary='Lead software engineering teams.\n')
        rows = [rec('E', 'job', 'Engineering Manager', **dict(common, summary=common['summary'] +
                        'Police-system experience is essential.')),
                rec('D', 'job', 'Engineering Manager', **dict(common, summary=common['summary'] +
                        'Police-system experience is desirable.'))]
        result = scored(rows)
        self.assertEqual(result['E']['band'], 'possible')
        self.assertEqual(result['D']['band'], 'strong')
        self.assertIn('Police-system', result['E']['fit']['gaps'][0])
        self.assertIn('not established', result['E']['fit']['gaps'][0])
        self.assertFalse(result['D']['fit']['gaps'])

    def test_partial_match_does_not_confirm_specialist_or_numeric_requirements(self):
        for requirement in ('Must have leadership and police-system experience.',
                            'Must have experience leading engineering teams in banking.',
                            'Must have 10 years of API integration experience.'):
            with self.subTest(requirement=requirement):
                result = scored([rec('J', 'job', 'Engineering Manager',
                    summary='Lead software engineering teams.\n' + requirement)])['J']
                self.assertEqual(result['band'], 'possible')
                self.assertTrue(result['fit']['gaps'])

    def test_supported_alternative_and_negated_requirement_keep_unknowns_honest(self):
        text = ('Build data integrations.\nExperience with data integration or police systems is required.\n'
                'Kubernetes experience is not required.')
        result = scored([rec('J', 'job', 'Software Engineer', summary=text)])['J']
        self.assertFalse(result['fit']['gaps'])
        self.assertEqual(result['band'], 'strong')

    def test_repeated_text_is_not_more_fit_and_proof_changes_recompute(self):
        from unittest.mock import patch
        text = 'Improve release readiness and test automation.'
        result = scored([rec('A', 'job', 'Engineering Manager', summary=text),
                         rec('B', 'job', 'Engineering Manager', summary='\n'.join([text] * 50))])
        self.assertEqual(result['A']['score'], result['B']['score'])
        with patch.object(relevance, '_proofs', return_value=[]):
            unsupported = scored([rec('A', 'job', 'Engineering Manager', summary=text)])['A']
        self.assertNotEqual(unsupported['band'], 'strong')
        self.assertEqual(unsupported['fit']['status'], 'unconfirmed')

    def test_quoted_research_evidence_can_supply_requirements_with_its_own_provenance(self):
        result = scored([rec('J', 'job', 'Engineering Manager', briefing=dict(
            facts=['Advert states: "Lead software engineering teams."',
                   'Advert states: "Police-system experience is essential."'],
            provenance={'facts': {'source_urls': ['https://example.com/role'], 'source_date': '2026-09-20'}}))])['J']
        self.assertEqual(result['band'], 'possible')
        self.assertEqual(result['fit']['evidence'][0]['url'], 'https://example.com/role')
        self.assertEqual(result['fit']['evidence'][0]['date'], '2026-09-20')
        essential = next(x for x in result['fit']['requirements'] if x['importance'] == 'essential')
        self.assertEqual(essential['status'], 'not_established')
        self.assertEqual(essential['url'], 'https://example.com/role')

    def test_proof_scope_does_not_establish_broader_essential_skills(self):
        for requirement in ('Software engineering leadership is required.', 'API integration is required.'):
            result = scored([rec('J', 'job', 'Engineering Manager',
                summary='Lead quality engineering teams.\n' + requirement)])['J']
            self.assertEqual(result['band'], 'possible')
            self.assertTrue(result['fit']['gaps'])

    def test_visible_fit_factor_uses_the_strongest_actual_evidence(self):
        for text in ('Lead software engineering teams. Improve release readiness.',
                     'Lead software engineering teams. Lead quality engineering teams.'):
            result = scored([rec('J', 'job', 'Engineering Manager', summary=text)])['J']
            self.assertEqual(result['fit']['status'], 'supported')
            reason = next(x for x in result['reasons'] if x.get('evidence'))
            self.assertIn(reason['evidence']['quote'], ('Improve release readiness.', 'Lead quality engineering teams.'))

    def test_conflicting_required_and_waived_statements_remain_uncertain(self):
        result = scored([rec('J', 'job', 'Engineering Manager', summary=
            'Build data integrations. Data integration is required. Data integration is not required.')])['J']
        self.assertEqual(result['band'], 'possible')
        self.assertTrue(any('conflict' in gap.lower() for gap in result['fit']['gaps']))

    def test_comparison_cache_reuses_pure_result_and_invalidates_source_evidence(self):
        from unittest.mock import patch
        base = rec('CACHE', 'job', 'Engineering Manager', summary='Improve release readiness.',
                   url='https://example.com/cache-one', date='2026-09-20')
        with patch.object(relevance, '_compare_sources', wraps=relevance._compare_sources) as compare:
            first = scored([base])['CACHE']
            count = compare.call_count
            first['fit']['evidence'][0]['quote'] = 'Mutated client copy'
            again = scored([base])['CACHE']
            self.assertEqual(compare.call_count, count)
            self.assertEqual(again['fit']['evidence'][0]['quote'], base['summary'])
            changed = scored([dict(base, date='2026-09-21')])['CACHE']
            self.assertEqual(compare.call_count, count + 1)
            self.assertEqual(changed['fit']['evidence'][0]['date'], '2026-09-21')

    def test_comparison_cache_is_bounded_by_entries_and_serialized_bytes(self):
        from unittest.mock import patch
        relevance._FIT_CACHE.clear()
        relevance._FIT_BYTES = 0
        with patch.object(relevance, '_FIT_MAX_ENTRIES', 2), patch.object(relevance, '_FIT_MAX_BYTES', 2048):
            scored([rec(f'BOUNDED-{i}', 'job', 'Engineering Manager', summary='Build data integrations.',
                        url=f'https://example.com/bounded-{i}') for i in range(6)])
            self.assertLessEqual(len(relevance._FIT_CACHE), 2)
            self.assertLessEqual(relevance._FIT_BYTES, 2048)

    def test_first_research_quote_is_compared_when_no_advert_text_exists(self):
        result = scored([rec('FIRST-QUOTE', 'job', 'Engineering Manager', briefing=dict(
            facts=['Advert says: "Police-system experience is essential."'],
            provenance={'facts': {'source_urls': ['https://example.com/first-quote']}}))])['FIRST-QUOTE']
        self.assertTrue(result['fit']['gaps'])
        self.assertEqual(result['fit']['requirements'][0]['status'], 'not_established')

    def test_unquoted_model_praise_is_not_source_proof(self):
        result = scored([rec('J', 'job', 'Engineering Manager', briefing=dict(
            facts=['Excellent fit for engineering leadership.'],
            provenance={'facts': {'source_urls': ['https://example.com/role']}}))])['J']
        self.assertEqual(result['fit']['status'], 'unconfirmed')
        self.assertNotEqual(result['band'], 'strong')


class PermanentSignalTests(unittest.TestCase):
    """Engagement preference is a selection tie-break, never a suitability bonus."""

    def test_equal_evidence_keeps_permanent_and_contract_suitability_equal(self):
        lead = dict(engagement='permanent', location='London', date='2026-09-20')
        out = scored([rec('P', 'job', 'Head of Engineering', organisation='Hiring Co', **lead),
                      rec('C', 'job', 'Interim Head of Engineering', organisation='Hiring Co',
                          **dict(lead, engagement='interim'))])
        self.assertIn(out['P']['band'], ('strong', 'possible'))
        self.assertNotIn('signal_band', out['P'])
        self.assertEqual(out['P']['score'], out['C']['score'])
        self.assertIn(out['C']['band'], ('strong', 'possible'))
        self.assertNotIn('signal_band', out['C'])

    def test_a_permanent_leadership_advert_still_makes_its_company_one_to_approach(self):
        out = scored([rec('H', 'company', 'Hiring Co'),
                      rec('HJ', 'job', 'Head of Engineering', organisation='Hiring Co', engagement='permanent',
                          location='London', date='2026-09-20')],
                     links=[('HJ', 'H', 'hiring_for')])
        self.assertEqual(out['H']['approach']['trigger'], 'hiring')


if __name__ == '__main__':
    unittest.main()
