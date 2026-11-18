"""Decision qualification and relationship evidence; all examples synthetic."""
import datetime as dt
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relevance

TODAY = dt.date(2026, 10, 4)


def score(rows, **rest):
    return relevance.score(dict(records=rows, **rest), today=TODAY)


class DecisionRelevanceTests(unittest.TestCase):
    def test_preparation_does_not_improve_commercial_fit(self):
        a = dict(id='A', type='job', name='Test Manager', engagement='contract', location='London')
        b = dict(a, id='B', working_state='ready')
        out = score([a, b], drafts=[dict(record_id='B', text='Draft')])
        self.assertEqual(out['A']['score'], out['B']['score'])

    def test_assigned_next_step_changes_readiness_not_commercial_fit(self):
        base = dict(id='A', type='job', name='Test Manager', engagement='contract', location='London')
        out = score([base, dict(base, id='B', owner='owner', next_action='Review and send the prepared note')])
        self.assertEqual(out['A']['score'], out['B']['score'])
        self.assertTrue(any(r['text'].startswith('Your next step:') and r.get('readiness')
                            for r in out['B']['reasons']))

    def test_observed_title_is_used_with_observed_description(self):
        advert = dict(title='Software Engineering Manager', text='Responsible for building management systems.',
                      employment_type='contract')
        out = score([dict(id='J', type='job', name='Engineering Manager', engagement='contract', advert=advert)])['J']
        self.assertEqual(out['qualification'], relevance.qualification(dict(name='Engineering Manager',
                                                                           engagement='contract', **advert)))
        self.assertTrue(out['qualification']['eligible'])

    def test_expired_deadline_and_stale_advert_never_boost_company_or_claim_hiring(self):
        base = dict(id='J', type='job', name='Head of Engineering', organisation='Example',
                    summary='Lead quality engineering teams', engagement='contract', location='London',
                    date='2026-10-03', status='interview', source_lifecycle='active')
        for fields, sightings in ((dict(published_deadline='2026-09-30'), {'J': '2026-10-03'}),
                                 ({}, {'J': '2026-09-01'})):
            with self.subTest(fields=fields, sightings=sightings):
                out = score([dict(id='C', type='company', name='Example'), dict(base, **fields)],
                            links=[dict(from_id='J', to_id='C', relation='hiring_for')], sightings=sightings)
                self.assertEqual(out['J']['band'], 'strong')  # workflow can remain relevant to the owner
                self.assertNotIn('approach', out['C'])
                self.assertEqual(out['C']['score'], 0)
                self.assertFalse(any('relevant role' in r['text'] for r in out['C']['reasons']))

    def test_current_duplicate_ranks_with_old_workflow_still_visible_and_unchanged(self):
        old = dict(id='OLD', type='job', name='Head of Engineering', organisation='Example', url='https://example.com/role/1',
                   summary='Lead software engineering', engagement='contract', location='London',
                   date='2026-10-03', status='interview', source_lifecycle='closed', next_action='Check previous application')
        active = dict(old, id='LIVE', status='candidate', source_lifecycle='active', next_action='')
        rows = [dict(id='C', type='company', name='Example'), old, active]
        original = copy.deepcopy(rows)
        out = score(rows, links=[dict(from_id=x, to_id='C', relation='hiring_for') for x in ('OLD', 'LIVE')],
                    drafts=[dict(record_id='OLD', version_id=1)], sightings={'OLD': '2026-09-01', 'LIVE': '2026-10-03'})
        self.assertEqual(out['OLD']['duplicate_of'], 'LIVE')
        self.assertNotIn('duplicate_of', out['LIVE'])
        self.assertEqual(out['C']['approach']['trigger'], 'hiring')
        self.assertNotIn('readiness', out['OLD'])
        self.assertTrue(any('OLD' in r['text'] and 'interview' in r['text'] for r in out['LIVE']['reasons']))
        self.assertEqual(rows, original)

    def test_current_source_replaces_stale_name_variant_duplicate_for_hiring(self):
        body = 'Lead software engineering for our cloud product and developer platform in London.'
        old = dict(id='OLD', type='job', name='Head of Engineering', organisation='Example', url='https://example.com/role/1',
                   summary=body, engagement='contract', location='London', date='2026-09-01', next_action='Review old advert')
        active = dict(old, id='LIVE', organisation='Example UK', location='London / Hybrid',
                      date='2026-10-03', next_action='')
        out = score([dict(id='C', type='company', name='Example'), old, active],
                    links=[dict(from_id=x, to_id='C', relation='hiring_for') for x in ('OLD', 'LIVE')],
                    sightings={'OLD': '2026-09-01', 'LIVE': '2026-10-03'})
        self.assertEqual(out['OLD']['duplicate_of'], 'LIVE')
        self.assertEqual(out['C']['approach']['trigger'], 'hiring')

    def test_one_outbound_and_last_touch_do_not_create_warmth(self):
        p = dict(id='P', type='person', name='Example', last_touch='2026-10-01')
        out = score([p], interactions=[dict(record_id='P', direction='out', at='2026-10-01')])['P']
        self.assertFalse(out['known'])
        self.assertEqual(out['reach']['tier'], 'connected')

    def test_reciprocal_export_is_known_without_promoting_unverified_employment(self):
        p = dict(id='P', type='person', name='Example', linkedin=dict(observed_company='Example Co',
                 evidence=dict(threads=dict(two_way=2, last_two_way='2026-09-01'))))
        out = score([p])['P']
        self.assertTrue(out['known'])
        self.assertEqual(out['reach']['tier'], 'in_touch')
        self.assertNotIn('organisation', p)

    def test_wrong_domain_never_lifts_company(self):
        out = score([dict(id='C', type='company', name='Buildings Ltd'),
                     dict(id='J', type='job', name='BMS Engineering Manager', summary='Building management systems and building services maintenance.',
                          engagement='contract', location='London', pay_max='85000',pay_currency='GBP',pay_period='year')],
                    links=[dict(from_id='J',to_id='C',relation='hiring_for')])
        self.assertEqual(out['J']['band'], 'low')
        self.assertFalse(out['J']['qualification']['eligible'])
        self.assertNotIn('approach', out['C'])

    def test_conflicting_permanent_advert_is_not_a_contract_recommendation(self):
        out = score([dict(id='J', type='job', name='AI Engineering Manager', engagement='contract',
                         summary='AI Engineering Manager - Permanent - £75-85k - London / Hybrid',location='London')])['J']
        self.assertEqual(out['lane'], 'permanent')
        self.assertNotEqual(out['band'], 'strong')
        self.assertTrue(out['qualification']['conflicts'])

    def test_approved_draft_not_waiting_for_review(self):
        out = score([dict(id='C',type='company',name='Example')],drafts=[dict(record_id='C',version_id=4)],
                    decisions=[dict(id=1,record_id='C',kind='draft',status='done',answer='approve',draft_version_id=4,draft_binding='exact')])['C']
        self.assertFalse(any('waiting for your review' in r['text'] for r in out['reasons']))
        self.assertNotIn('readiness', out)

    def test_new_outbound_does_not_refresh_old_two_way_history(self):
        p = dict(id='P',type='person',name='Example')
        out = score([p],interactions=[dict(record_id='P',direction='in',at='2019-01-01'),
                      dict(record_id='P',direction='out',at='2026-10-03')])['P']
        self.assertEqual(out['reach']['tier'], 'known')

    def test_relationship_scoring_visits_the_full_interaction_list_once(self):
        class CountedInteractions(list):
            visits = 0

            def __iter__(self):
                for item in super().__iter__():
                    self.visits += 1
                    yield item

        for count in (10, 100):
            with self.subTest(people=count):
                people = [dict(id=f'P{i}', type='person', name=f'Person {i}') for i in range(count)]
                interactions = CountedInteractions(
                    dict(record_id=p['id'], direction=direction, at='2026-10-03')
                    for p in people for direction in ('in', 'out'))
                scorer = relevance.Scorer(dict(records=people, interactions=interactions),
                                          relevance.load_config(), TODAY)
                self.assertEqual(interactions.visits, count * 2)
                self.assertEqual({item['tier'] for item in scorer.reach.values()}, {'in_touch'})

    def test_grouped_relationships_preserve_evidence_boundaries_without_mutation(self):
        rows = [dict(id=rid, type='person', name=rid) for rid in
                ('OUT', 'IN', 'RECENT', 'OLD', 'INVALID', 'MISSING', 'THREAD', 'DORMANT', 'BLOCKED', 'WARM', 'ACTIVE', 'NONE')]
        by_id = {row['id']: row for row in rows}
        by_id['THREAD']['linkedin'] = dict(evidence=dict(threads=dict(two_way=1, last_two_way='2026-09-01')))
        for rid, relationship in (('DORMANT', 'dormant'), ('BLOCKED', 'do_not_contact'), ('WARM', 'warm'), ('ACTIVE', 'active')):
            by_id[rid]['relationship'] = relationship
        interactions = [dict(record_id='OUT', direction='out', at='2026-10-03'),
                        dict(record_id='IN', direction='in', at='2026-10-03'),
                        dict(record_id='MISSING', direction='out'),
                        dict(record_id='THREAD', direction='out', at='2026-10-03')]
        for rid, inbound in (('RECENT', (TODAY - dt.timedelta(days=730)).isoformat()),
                             ('OLD', (TODAY - dt.timedelta(days=731)).isoformat()),
                             ('INVALID', 'not-a-date'), ('DORMANT', '2026-10-02'), ('BLOCKED', '2026-10-02')):
            interactions.extend([dict(record_id=rid, direction='in', at=inbound),
                                 dict(record_id=rid, direction='out', at='2026-10-03')])
        original = copy.deepcopy((rows, interactions))
        expected = {'OUT': ('connected', False), 'IN': ('connected', False), 'RECENT': ('in_touch', True),
                    'OLD': ('known', True), 'INVALID': ('known', True), 'MISSING': ('connected', False),
                    'THREAD': ('in_touch', True), 'DORMANT': ('known', True), 'BLOCKED': ('blocked', False),
                    'WARM': ('close', True), 'ACTIVE': ('in_touch', True), 'NONE': ('cold', False)}
        out = score(rows, interactions=interactions)
        self.assertEqual({rid: (item['reach']['tier'], item['known']) for rid, item in out.items()}, expected)
        self.assertEqual({row['id']: relevance.reach_of(row, interactions, TODAY)['tier'] for row in rows},
                         {rid: tier for rid, (tier, _) in expected.items()})
        self.assertEqual((rows, interactions), original)

    def test_relationship_index_is_rebuilt_from_each_snapshot(self):
        people = [dict(id=rid, type='person', name=rid) for rid in ('A', 'B')]
        interactions = [dict(record_id='A', direction='out', at='2026-10-03'),
                        dict(record_id='B', direction='in', at='2026-10-03')]
        first = score(people, interactions=interactions)
        self.assertEqual([first[rid]['reach']['tier'] for rid in ('A', 'B')], ['connected', 'connected'])
        interactions.append(dict(record_id='A', direction='in', at='2026-10-02'))
        second = score(people, interactions=interactions)
        self.assertEqual([second[rid]['reach']['tier'] for rid in ('A', 'B')], ['in_touch', 'connected'])
        self.assertEqual(first['A']['reach']['tier'], 'connected')

    def test_endorsement_is_relationship_evidence_not_a_dated_contact(self):
        p = dict(id='P',type='person',name='Example',linkedin=dict(evidence=dict(endorsed_you=1)))
        out = score([p])['P']
        self.assertTrue(out['known'])
        self.assertEqual(out['reach']['tier'], 'known')
        self.assertFalse(any('touch point' in x['text'] or '1 Jan 1' in x['text'] for x in out['reasons']))

    def test_historical_relationship_is_known_not_a_warm_company_route(self):
        rows = [dict(id='C', type='company', name='Example', signals=[dict(published='2026-10-01', amount='£5m')]),
                dict(id='P', type='person', name='Example Person', linkedin=dict(evidence=dict(endorsed_you=1)))]
        out = score(rows, links=[dict(from_id='P', to_id='C', relation='works_at')])
        self.assertFalse(out['C']['approach']['warm'])
        self.assertEqual(out['P']['reach']['tier'], 'known')


if __name__ == '__main__':
    unittest.main()
