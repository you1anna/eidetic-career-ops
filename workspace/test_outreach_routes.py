"""The four routes need evidence, retain one approach, and never imply buying intent."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_approaches import build, company, person, known, job, item, sent, TODAY


def review(kind, **extra):
    return dict(reviewed_at='2026-10-14', source_date='2026-10-10', verification='primary-read',
                source_urls=['https://c1.example/news'], outreach_signals=[dict(
                    kind=kind, text='Software engineering leadership and release readiness.',
                    url='https://c1.example/news', **extra)])


class RouteTests(unittest.TestCase):
    def route(self, result, rid='P1'):
        return item(result, rid)['brief'].get('approach_route', {})

    def test_permanent_hiring_is_an_application_without_inferred_cover(self):
        c, ad = company(), job(engagement='permanent')
        c['research'] = []
        ad['relevance']['qualification']['engagement'] = 'permanent'
        ad['summary'] = 'Permanent Head of Quality Engineering. Own release readiness and incident prevention.'
        result = build([c,person(),ad],[('P1','C1','works_at'),('J1','C1','hiring_for')])
        self.assertFalse(self.route(result))
        self.assertEqual(item(result,'J1')['kind'], 'application')
        self.assertNotIn('contract or temporary', item(result,'J1')['brief']['recommendation'])

    def test_cover_needs_current_leadership_remit_and_usable_contact(self):
        for change in ('closed', 'old', 'individual', 'clearance', 'cold_ceo'):
            with self.subTest(change=change):
                c, ad, p = company(), job(engagement='permanent'), person()
                c['research'] = []
                ad['relevance'].update(band='low', signal_band='strong')
                ad['relevance']['qualification']['engagement'] = 'permanent'
                if change == 'closed': ad['source_lifecycle'] = 'closed'
                if change == 'old': ad['relevance']['last_seen'] = '2026-08-01'
                if change == 'individual': ad['name'] = 'Software Engineer'
                if change == 'clearance': ad['summary'] += ' SC clearance required.'
                if change == 'cold_ceo': p['role'] = 'Chief Executive Officer'
                result = build([c, ad, p], [('P1', 'C1', 'works_at'), ('J1', 'C1', 'hiring_for')])
                ready = [a for a in result['actions'] if a['progress']['status'] == 'ready']
                self.assertFalse(any(a['brief'].get('approach_route', {}).get('kind') == 'interim_cover' for a in ready))

    def test_first_90_days_uses_actual_start_not_announcement_or_review(self):
        for started, eligible in [('2026-10-01', True), ('2026-07-16', True), ('2026-07-15', False),
                                  ('2026-10-15', False), ('', False)]:
            with self.subTest(started=started):
                c = company()
                c['research'] = [review('leadership_change', person_id='P1', started_on=started)]
                result = build([c, person()], [('P1', 'C1', 'works_at')])
                routes = [a['brief'].get('approach_route', {}).get('kind') for a in result['actions']]
                self.assertEqual('new_leader' in routes, eligible)

    def test_leader_route_requires_named_current_person_at_company_and_primary_source(self):
        for change in ('other_person', 'related_only', 'secondary', 'missing_url', 'future_review'):
            with self.subTest(change=change):
                c = company()
                r = review('leadership_change', person_id='P1', started_on='2026-10-01')
                c['research'] = [r]
                link = 'works_at'
                if change == 'other_person': r['outreach_signals'][0]['person_id'] = 'P2'
                if change == 'related_only': link = 'related'
                if change == 'secondary': r['verification'] = 'search-verified'
                if change == 'missing_url': r['outreach_signals'][0]['url'] = 'https://unread.example/news'
                if change == 'future_review': r['reviewed_at'] = '2027-01-01'
                result = build([c, person()], [('P1', 'C1', link)])
                self.assertFalse(any(a['brief'].get('approach_route', {}).get('kind') == 'new_leader'
                                     for a in result['actions'] + result['research']))

    def test_partner_route_needs_explicit_current_specialist_channel(self):
        for accepts, source_date, expected in [(True, '2026-10-10', True), (False, '2026-10-10', False),
                                              (True, '2025-10-10', False), (True, '', False)]:
            with self.subTest(accepts=accepts, date=source_date):
                c = company()
                r = review('delivery_partner', accepts_specialists=accepts)
                r['source_date'] = source_date
                c['research'] = [r]
                result = build([c, person()], [('P1', 'C1', 'works_at')])
                self.assertEqual(any(a['brief'].get('approach_route', {}).get('kind') == 'delivery_partner'
                                     for a in result['actions']), expected)
                if expected:
                    self.assertIn('current workstream', item(result, 'P1')['progress']['say'])
                    self.assertIn('not a live mandate', self.route(result)['why'])

    def test_latest_explicit_signal_set_can_withdraw_an_earlier_claim(self):
        c = company()
        c['research'] = [review('delivery_partner', accepts_specialists=True),
                         dict(reviewed_at='2026-10-14T12:00:00Z', outreach_signals=[])]
        result = build([c, person()], [('P1', 'C1', 'works_at')])
        self.assertFalse(any(a['brief'].get('approach_route') for a in result['actions'] + result['research']))

    def test_repeated_source_prose_keeps_typed_route_without_counting_two_facts(self):
        c = company()
        r = review('delivery_partner', accepts_specialists=True)
        r['signal'] = r['outreach_signals'][0]['text']
        c['research'] = [r]
        result = build([c, person()], [('P1', 'C1', 'works_at')])
        self.assertEqual(self.route(result)['kind'], 'delivery_partner')
        self.assertEqual(len(item(result, 'P1')['brief']['facts']), 1)

    def test_equal_instant_conflicting_route_claims_never_choose_import_order(self):
        positive = review('delivery_partner', accepts_specialists=True)
        positive['reviewed_at'] = '2026-10-14T10:00:00Z'
        negative = copy.deepcopy(positive)
        negative['reviewed_at'] = '2026-10-14T11:00:00+01:00'
        negative['outreach_signals'][0]['accepts_specialists'] = False
        for reviews in ([positive, negative], [negative, positive]):
            c = company()
            c['research'] = reviews
            result = build([c, person()], [('P1', 'C1', 'works_at')])
            self.assertFalse(any(a['brief'].get('approach_route') for a in result['actions'] + result['research']))

    def test_future_contact_verification_cannot_make_a_route_ready(self):
        c = company()
        # Establish the work independently so this test isolates the contact-date gate;
        # a leadership announcement alone now leaves the more basic work evidence unknown.
        c['research'] = [review('leadership_change', person_id='P1', started_on='2026-10-01')]
        c['research'][0]['signal'] = 'The engineering team requests help to improve release readiness.'
        result = build([c, person(date='2026-10-15')], [('P1', 'C1', 'works_at')])
        self.assertEqual(item(result, 'P1')['progress']['status'], 'research')
        self.assertIn('Confirm their current role', item(result, 'P1')['brief']['unknowns'][0])

    def test_warm_path_uses_explicit_directed_introducer_edge_not_shared_employment(self):
        for relation, direction, tier, expected in [('introducer', True, 'known', True),
                ('related', True, 'known', False), ('works_at', True, 'known', False),
                ('introducer', False, 'known', False), ('introducer', True, 'connected', False)]:
            with self.subTest(relation=relation, direction=direction, tier=tier):
                edge = ('K1', 'P1', relation) if direction else ('P1', 'K1', relation)
                result = build([company(), person(), known('K1', tier=tier)], [('P1', 'C1', 'works_at'), edge])
                found = [a for a in result['actions'] if a['record_id'] == 'K1'
                         and a['brief'].get('approach_route', {}).get('kind') == 'warm_introduction']
                self.assertEqual(bool(found), expected)
                if expected:
                    self.assertIn('Person P1', found[0]['progress']['say'])
                    self.assertEqual(found[0]['company_id'], 'C1')
                    self.assertEqual(found[0]['kind'], 'catch_up')

    def test_company_introducer_can_ask_for_owner_without_inventing_a_buyer(self):
        result = build([company(), known('K1')], [('K1', 'C1', 'introducer')])
        self.assertEqual(self.route(result, 'K1')['kind'], 'warm_introduction')
        self.assertIn('who owns', item(result, 'K1')['progress']['say'])

    def test_blocked_or_excluded_people_never_become_introducers(self):
        for changes in ({'relationship': 'do_not_contact'}, {'archived_at': '2026-10-01'},
                        {'exclusion': {'company': 'Example', 'reason': 'Excluded'}}):
            result = build([company(), person(), known('K1', **changes)],
                           [('P1', 'C1', 'works_at'), ('K1', 'P1', 'introducer')])
            self.assertFalse(any(a['record_id'] == 'K1' for a in result['actions']))

    def test_route_snapshot_survives_evidence_expiry_and_late_reply(self):
        c = company()
        c['research'] = [review('leadership_change', person_id='P1', started_on='2026-10-01')]
        result = build([c, person()], [('P1', 'C1', 'works_at')])
        a = item(result, 'P1')
        snapshot = {k: copy.deepcopy(a[k]) for k in ('brief', 'route', 'company_id', 'person_id')}
        snapshot['action_id'] = a['id']
        c['research'] = []
        rows = [sent('P1', '2026-10-14', a['id'], snapshot=snapshot),
                sent('P1', '2026-10-25', a['id'], kind='followed_up', snapshot=snapshot)]
        late = build([c, person()], [('P1', 'C1', 'works_at')], rows, today=dt.date(2027, 2, 1))
        self.assertEqual(self.route(late)['kind'], 'new_leader')
        self.assertEqual(item(late, 'P1')['progress']['status'], 'waiting')
        self.assertIn('replied', [x['kind'] for x in item(late, 'P1')['progress']['actions'] if x['method'] == 'outcome'])
        c['relevance'] = {}
        rows.append(sent('P1', '2027-02-02', a['id'], kind='handed_over',
                         snapshot=dict(action_id=a['id'], company_id='C1', opportunity_id='O1')))
        handed = build([c, person()], [('P1', 'C1', 'works_at')], rows, today=dt.date(2027, 2, 3))
        self.assertEqual(self.route(handed)['kind'], 'new_leader')
        self.assertEqual(item(handed, 'P1')['progress']['status'], 'handed_over')


if __name__ == '__main__':
    unittest.main()
