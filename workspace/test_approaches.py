"""Each approach has one status, next step and set of permitted actions, derived from recorded facts."""
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approaches
import decision_intelligence as intelligence
import states

TODAY = dt.date(2026, 10, 14)  # a Wednesday


def company(id='C1', **kwargs):
    return dict(id=id, type='company', name=f'Company {id}', research=[dict(
        signal='Building a Quality Engineering department to improve release readiness and incident rate.',
        reviewed_at='2026-10-10', source_urls=[f'https://{id.lower()}.example/careers'],
        verification='primary-read', decisive_unknowns=['Whether an interim start is possible'])],
        relevance={'approach': {'trigger': 'hiring'}}, **kwargs)


def person(id='P1', **kwargs):
    values = dict(id=id, type='person', name=f'Person {id}', role='CTO', verification_level='primary-read',
                  evidence_urls='https://c1.example/about', created_at='2026-10-01T10:00:00+01:00')
    values.update(kwargs)
    return values


def known(id, tier='in_touch', **kwargs):
    return person(id, role='Engineer', verification_level='', evidence_urls='',
                  relevance={'reach': {'tier': tier, 'reason': 'Two-way contact'}}, **kwargs)


def job(id='J1', **kwargs):
    values = dict(id=id, type='job', name='Head of Quality Engineering', status='candidate', engagement='contract',
                  date='2026-10-10', created_at='2026-10-10T06:00:00+01:00', url=f'https://jobs.example/{id}',
                  summary='Own software release readiness and incident prevention for the platform.',
                  relevance={'band': 'strong', 'lane': 'contract', 'last_seen': '2026-10-14', 'qualification': {
                      'eligible': True, 'domain': 'software', 'engagement': 'contract', 'conflicts': []}})
    values.update(kwargs)
    return values


def build(records, links=(), outcomes=(), today=TODAY, **extra):
    state = dict(records=list(records), links=[dict(from_id=a, to_id=b, relation=r) for a, b, r in links],
                 outcomes=list(outcomes), interactions=[], week_items=[], status_since={})
    state.update(extra)
    intel = intelligence.build(state, today)
    return approaches.annotate(intel, state, today)


def historical_build(records, links=(), outcomes=(), today=TODAY, **extra):
    """Compatibility-only exercise of legacy week helpers, never the public state path."""
    state = dict(records=list(records), links=[dict(from_id=a, to_id=b, relation=r) for a,b,r in links],
                 outcomes=list(outcomes), interactions=[], week_items=[], status_since={})
    state.update(extra)
    intel = build(records, links, outcomes, today, **extra)
    ctx = approaches._Context(intel, state, today, approaches.config())
    intel['week'] = approaches.compose_week(intel, state.get('week_items') or [], ctx)
    people = [i for i in intel['week']['items'] if (ctx.records.get(i['record_id']) or {}).get('type') == 'person'
              or (i['state'] == 'invalid' and i['slot'] == 'known')]
    intel['week']['focus_ids'] = [i['action_id'] for i in sorted(people, key=lambda i: bool(i.get('done')))][:5]
    return intel


def item(result, record_id):
    return next(a for a in result['actions'] + result['research'] if a['record_id'] == record_id)


def selected(result, records):
    """Selection assertions exercise its sole owner, not a second attention projection."""
    import work_selection
    return work_selection.build(dict(records=records, intelligence=result))


def sent(record_id, at, action_id, kind='sent', **kwargs):
    values = dict(id=abs(hash((record_id, at, kind))) % 10**6, record_id=record_id, kind=kind, at=at,
                  snapshot={'action_id': action_id}, corrected_at=None)
    values.update(kwargs)
    return values


class KindTests(unittest.TestCase):
    def test_each_route_has_the_kind_owner_recognises(self):
        agency = dict(id='A1', type='agency', name='Quality Desk', subtype='Quality engineering desk',
                      summary='A specialist quality engineering recruitment desk', relationship='warm',
                      relevance={'band': 'strong'})
        opportunity = dict(id='O1', type='opportunity', name='Inbound enquiry', stage='lead',
                           summary='An inbound enquiry for interim engineering leadership')
        result = build([company(), person(), known('K1', tier='close', relationship='warm'), agency, opportunity, job()],
                       links=[('P1', 'C1', 'works_at'), ('J1', 'C1', 'hiring_for')])
        self.assertEqual(item(result, 'P1')['kind'], 'note')
        self.assertEqual(item(result, 'K1')['kind'], 'ask')
        self.assertEqual(item(result, 'A1')['kind'], 'desk')
        self.assertEqual(item(result, 'O1')['kind'], 'opportunity')
        self.assertEqual(item(result, 'J1')['kind'], 'application')

    def test_someone_known_at_a_company_with_a_fresh_reason_gets_a_catch_up(self):
        result = build([company(), known('K1'), job()], links=[('K1', 'C1', 'works_at'), ('J1', 'C1', 'hiring_for')])
        catch_up = item(result, 'K1')
        self.assertEqual(catch_up['kind'], 'catch_up')
        self.assertIn('Head of Quality Engineering', catch_up['progress']['why'])

    def test_someone_known_without_a_reason_or_an_ask_gets_the_catch_up(self):
        # 15 October: nobody is asked for a referral by default; the ladder starts at a catch-up.
        result = build([known('K1', tier='close', relationship='warm')])
        ask = item(result, 'K1')
        self.assertEqual(ask['kind'], 'ask')
        self.assertEqual(ask['progress']['say'], approaches.config()['asks']['catch_up'])
        self.assertEqual(ask['progress']['section'], 'people')


class LifecycleTests(unittest.TestCase):
    def note_after(self, outcomes, today):
        result = build([company(), person()], links=[('P1', 'C1', 'works_at')], outcomes=outcomes, today=today)
        return item(result, 'P1')['progress']

    def test_personal_followups_and_no_response_require_reported_facts(self):
        aid = 'action:P1:C1'
        first = [sent('P1', '2026-10-01', aid)]
        for date in (dt.date(2026,10,5), dt.date(2026,10,11), dt.date(2027,1,1)):
            self.assertEqual(self.note_after(first,date)['status'], 'waiting')
        both = first + [sent('P1', '2026-10-11', aid, kind='followed_up')]
        self.assertEqual(self.note_after(both,dt.date(2027,1,1))['status'], 'waiting')
        parked = both + [sent('P1','2026-10-22',aid,kind='no_response')]
        self.assertEqual(self.note_after(parked,dt.date(2027,1,1))['status'], 'no_response')
        late = parked + [sent('P1','2027-01-02',aid,kind='replied')]
        self.assertEqual(self.note_after(late,dt.date(2027,1,3))['status'], 'replied')

    def test_work_terminal_authority_requires_a_current_uncorrected_outcome(self):
        records = [company()]
        aid = 'action:C1:'
        elapsed = [sent('C1','2026-09-01',aid), sent('C1','2026-09-10',aid,kind='followed_up')]
        result = build(records, outcomes=elapsed)
        progress = item(result,'C1')['progress']
        self.assertEqual(progress['status'], 'no_response')
        self.assertFalse(progress['terminal_recorded'])
        self.assertNotIn('You recorded no response', progress['next'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['active']], ['C1'])

        ending = sent('C1','2026-10-13',aid,kind='no_response')
        result = build(records, outcomes=[*elapsed,ending])
        self.assertTrue(item(result,'C1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['closed']], ['C1'])
        self.assertFalse(selected(result,records)['active'])

        result = build(records, outcomes=[*elapsed,dict(ending,corrected_at='2026-10-14')])
        self.assertFalse(item(result,'C1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['active']], ['C1'])
        result = build(records, outcomes=[*elapsed,ending,sent('C1','2026-10-14',aid,kind='replied')])
        self.assertEqual(item(result,'C1')['progress']['status'], 'replied')
        self.assertFalse(item(result,'C1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['act_now']], ['C1'])

    def test_work_commitment_survives_missing_recommendation_but_not_recorded_ending(self):
        records = [dict(id='C1',type='company',name='Recorded company')]
        aid = 'action:C1:'
        for kind, section in (('mine','act_now'),('waiting','waiting')):
            with self.subTest(kind=kind):
                steps = [dict(record_id='C1',action_id=aid,value={'kind':kind,'text':'Recorded commitment'},
                              context={'route':'direct','company_id':'C1','person_id':None})]
                result = build(records, approach_next_steps=steps)
                self.assertEqual(item(result,'C1')['progress']['status'], 'ready')
                self.assertEqual([r['record_id'] for r in selected(result,records)[section]], ['C1'])
                ending = sent('C1','2026-10-13',aid,kind='no_fit')
                result = build(records, approach_next_steps=steps, outcomes=[ending])
                self.assertTrue(item(result,'C1')['progress']['terminal_recorded'])
                self.assertFalse(selected(result,records)[section])
                self.assertEqual([r['record_id'] for r in selected(result,records)['closed']], ['C1'])

    def test_source_hold_does_not_close_application_or_old_outcome_close_later_interview(self):
        records = [job(status='applied',summary='Lead software release readiness. Security clearance required.')]
        result = build(records, status_since={'J1':'2026-10-13'})
        self.assertEqual(item(result,'J1')['stage'], 'held')
        self.assertFalse(item(result,'J1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['active']], ['J1'])
        records = [job(status='applied')]
        ending = sent('J1','2026-10-13','action:J1:',kind='no_response')
        result = build(records, outcomes=[ending], status_since={'J1':'2026-10-12'})
        self.assertTrue(item(result,'J1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['closed']], ['J1'])
        result = build(records, outcomes=[dict(ending,corrected_at='2026-10-14')], status_since={'J1':'2026-10-12'})
        self.assertFalse(item(result,'J1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['active']], ['J1'])
        records = [job(status='interview', incoming={'current':True,'status':'interview',
                       'label':'Screening completed','next_step':'Await their response.'})]
        result = build(records, outcomes=[sent('J1','2026-10-01','action:J1:',kind='no_response')],
                       status_since={'J1':'2026-10-13'})
        self.assertEqual(item(result,'J1')['progress']['status'], 'interview')
        self.assertFalse(item(result,'J1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['in_progress']], ['J1'])

    def test_an_explicit_historical_hold_is_recorded_terminal_authority(self):
        records = [company()]
        result = build(records, decisions=[dict(id=1,kind='draft',record_id='C1',status='done',answer='hold')])
        self.assertEqual(item(result,'C1')['progress']['status'], 'held')
        self.assertTrue(item(result,'C1')['progress']['terminal_recorded'])
        self.assertEqual([r['record_id'] for r in selected(result,records)['closed']], ['C1'])

    def test_a_desk_follow_up_counts_working_days(self):
        agency = dict(id='A1', type='agency', name='Quality Desk', subtype='Quality engineering desk',
                      summary='A specialist quality engineering recruitment desk', relationship='warm',
                      relevance={'band': 'strong'})
        outcomes = [sent('A1', '2026-10-09T09:00:00+01:00', 'action:A1:')]  # a Friday
        status = lambda day: item(build([agency], outcomes=outcomes, today=day), 'A1')['progress']['status']
        self.assertEqual(status(dt.date(2026, 10, 13)), 'waiting')    # Tuesday: two working days
        self.assertEqual(status(dt.date(2026, 10, 14)), 'follow_up')  # Wednesday: three

    def test_a_corrected_entry_no_longer_counts(self):
        wrong = sent('P1', '2026-10-01T09:00:00+01:00', 'action:P1:C1', corrected_at='2026-10-01T09:05:00+01:00')
        progress = self.note_after([wrong], dt.date(2026, 10, 14))
        self.assertEqual(progress['status'], 'ready')
        self.assertEqual(progress['section'], 'decision_makers')

    def test_a_late_reply_after_parking_is_still_recorded_against_the_approach(self):
        aid = 'action:P1:C1'
        outcomes = [sent('P1', '2026-09-01T09:00:00+01:00', aid), sent('P1', '2026-09-11T09:00:00+01:00', aid, kind='followed_up'),
                    sent('P1', '2026-10-13T09:00:00+01:00', aid, kind='replied')]
        progress = self.note_after(outcomes, TODAY)
        self.assertEqual((progress['status'], progress['section']), ('replied', 'in_progress'))
        self.assertEqual([h['kind'] for h in progress['history']], ['sent', 'followed_up', 'replied'])

    def test_a_conversation_offers_handover_and_handover_points_to_work(self):
        aid = 'action:P1:C1'
        talking = self.note_after([sent('P1', '2026-10-10T09:00:00+01:00', aid, kind='conversation')], TODAY)
        self.assertEqual(talking['primary']['id'], 'handover')
        done = self.note_after([sent('P1', '2026-10-10T09:00:00+01:00', aid, kind='conversation'),
                                sent('P1', '2026-10-12T09:00:00+01:00', aid, kind='handed_over')], TODAY)
        self.assertEqual((done['status'], done['section']), ('handed_over', 'done'))

    def test_an_approach_whose_recommendation_has_gone_keeps_its_history(self):
        outcomes = [sent('P1', '2026-10-10T09:00:00+01:00', 'action:P1:GONE')]
        result = build([person()], outcomes=outcomes)
        tracked = next(a for a in result['actions'] if a['id'] == 'action:P1:GONE')
        self.assertEqual(tracked['progress']['section'], 'in_progress')
        self.assertTrue(tracked['progress']['note'])

    def test_every_progress_has_one_primary_action_and_plain_labels(self):
        result = build([company(), person(), known('K1', tier='close', relationship='warm'), job()],
                       links=[('P1', 'C1', 'works_at'), ('J1', 'C1', 'hiring_for')],
                       outcomes=[sent('P1', '2026-10-01T09:00:00+01:00', 'action:P1:C1')])
        for a in result['actions']:
            with self.subTest(item=a['id']):
                progress = a['progress']
                self.assertTrue(progress['next'])
                self.assertEqual(sum(1 for x in progress['actions'] if x.get('primary')), 1 if progress['actions'] else 0)
                for x in progress['actions']:
                    self.assertIn(x['method'], approaches.METHODS)
                    self.assertNotIn('_', x['label'])


class ApplicationTests(unittest.TestCase):
    def test_default_application_step_is_provenance_not_points_or_matching_human_text(self):
        from unittest.mock import patch
        role = job(status='pursue', summary='')
        with patch.object(approaches, '_points', return_value=[]):
            default = item(build([role]), 'J1')
        self.assertEqual(default['progress']['next'], 'Apply through the advert.')
        self.assertTrue(default['progress']['next_is_default'])
        self.assertTrue(item(build([role]), 'J1')['progress']['next_is_default'])
        contact = known('P1', relationship='warm', list_priority='first')
        contact_action = item(build([contact]), 'P1')
        for kind in ('mine', 'waiting'):
            steps = [dict(record_id='P1', action_id=contact_action['id'],
                          value=dict(kind=kind,text='Send your ask.',agreed_on=None), revision=1)]
            saved = item(build([contact], approach_next_steps=steps), 'P1')['progress']
            self.assertFalse(saved['next_is_default'])
            self.assertEqual(saved['next'], ('Waiting for them: ' if kind == 'waiting' else '') + 'Send your ask.')
            self.assertEqual(saved['actions'], contact_action['progress']['actions'])
        incoming = job(status='pursue', incoming=dict(current=True,status='pursue',label='Saved incoming role',next_step='Apply through the advert.'))
        self.assertFalse(item(build([incoming]), 'J1')['progress']['next_is_default'])

    def test_an_application_is_tracked_from_job_status_even_after_the_advert_goes_stale(self):
        stale = job(status='applied', relevance={'band': 'strong', 'lane': 'contract', 'last_seen': '2026-08-01',
                                                  'qualification': {'eligible': True, 'domain': 'software',
                                                                    'engagement': 'contract', 'conflicts': []}})
        result = build([company(), stale], links=[('J1', 'C1', 'hiring_for')],
                       status_since={'J1': '2026-09-20T10:00:00+01:00'})
        application = item(result, 'J1')
        self.assertEqual(application['kind'], 'application')
        self.assertEqual((application['progress']['status'], application['progress']['section']), ('follow_up', 'in_progress'))

    def test_application_steps_use_the_job_status_transitions(self):
        result = build([company(), job(status='responded')], links=[('J1', 'C1', 'hiring_for')],
                       status_since={'J1': '2026-10-12T10:00:00+01:00'})
        primary = item(result, 'J1')['progress']['primary']
        self.assertEqual((primary['method'], primary['to']), ('transition', 'interview'))

    def test_an_application_waiting_for_a_reply_can_be_closed(self):
        for since in ('2026-10-13T10:00:00+01:00', '2026-09-20T10:00:00+01:00'):
            result = build([company(), job(status='applied')], links=[('J1', 'C1', 'hiring_for')],
                           status_since={'J1': since})
            progress = item(result, 'J1')['progress']
            self.assertIn(progress['status'], ('waiting', 'follow_up'))
            self.assertIn(('transition', 'closed'), [(a['method'], a.get('to')) for a in progress['actions']])

    def test_an_application_never_offers_a_status_move_the_job_refuses(self):
        # A legacy "sent" entry on a job left at candidate made the card offer moves the API refused.
        result = build([company(), job()], links=[('J1', 'C1', 'hiring_for')],
                       outcomes=[sent('J1', '2026-10-06T04:35:06+01:00', 'action:J1:C1')])
        progress = item(result, 'J1')['progress']
        allowed = states.allowed_from('job', 'status', 'candidate')
        moves = [a['to'] for a in progress['actions'] if a['method'] == 'transition']
        self.assertTrue(all(to in allowed for to in moves), moves)

    def test_an_advert_not_yet_applied_for_stays_out_of_outreach_lists(self):
        result = build([company(), job()], links=[('J1', 'C1', 'hiring_for')])
        progress = item(result, 'J1')['progress']
        self.assertEqual(progress['section'], 'adverts')
        self.assertEqual(progress['primary']['to'], 'applied')


class WeekTests(unittest.TestCase):
    def setUp(self):
        # More people he knows than the week has places, so a replacement always exists.
        self.people = [known(f'K{i}', tier='close' if i < 2 else 'in_touch', relationship='warm' if i < 2 else 'active', priority='first')
                       for i in range(approaches.config()['plan']['size'] + 2)]
        desk = dict(id='A1', type='agency', name='Quality Desk', subtype='Quality engineering desk',
                    summary='A specialist quality engineering recruitment desk', relationship='warm',
                    relevance={'band': 'strong'})
        self.records = [*self.people, desk, company(), job('J1'), job('J2', url='https://jobs.example/J2'), person()]
        self.links = [('J1', 'C1', 'hiring_for'), ('J2', 'C1', 'hiring_for'), ('P1', 'C1', 'works_at')]

    def test_week_boundary_is_monday_in_london(self):
        self.assertEqual(approaches.week_of(dt.date(2026, 10, 18)), '2026-10-12')
        self.assertEqual(approaches.week_of(dt.date(2026, 10, 19)), '2026-10-19')

    def test_five_listed_people_lead_without_agency_or_job_fillers(self):
        week = historical_build(self.records, self.links)['week']
        self.assertFalse(week['stored'])
        slots = [x['slot'] for x in week['items']]
        size = approaches.config()['plan']['size']
        self.assertEqual(len(slots), size)
        self.assertEqual(slots, ['known'] * size)
        ids = [x['record_id'] for x in week['items']]
        self.assertEqual(ids[:2], ['K0', 'K1'])  # close contacts first
        self.assertEqual(historical_build(self.records, self.links)['week']['items'], week['items'])

    def test_someone_approached_recently_is_not_suggested_again(self):
        outcomes = [sent('K0', '2026-09-30T09:00:00+01:00', 'action:K0:', kind='replied')]
        week = historical_build(self.records, self.links, outcomes=outcomes)['week']
        self.assertNotIn('K0', [x['record_id'] for x in week['items']])

    def test_a_stored_week_keeps_its_items_and_explains_a_no_longer_valid_one(self):
        first = historical_build(self.records, self.links)['week']
        rows = [dict(week=first['week'], action_id=x['action_id'], record_id=x['record_id'], slot=x['slot'],
                     position=n, state='planned', reason='') for n, x in enumerate(first['items'])]
        rows.append(dict(week=first['week'], action_id='action:GONE:', record_id='GONE', slot='known',
                         position=9, state='planned', reason=''))
        rows[1]['state'], rows[1]['reason'] = 'deferred', 'Not this week'
        outcomes = [sent('K0', '2026-10-13T09:00:00+01:00', first['items'][0]['action_id'])]
        week = historical_build(self.records, self.links, outcomes=outcomes, week_items=rows)['week']
        self.assertTrue(week['stored'])
        by_id = {x['action_id']: x for x in week['items']}
        self.assertTrue(by_id[first['items'][0]['action_id']]['done'])
        self.assertEqual(by_id['action:GONE:']['state'], 'invalid')
        self.assertTrue(by_id['action:GONE:']['reason'])
        self.assertEqual([x['action_id'] for x in week['deferred']], [first['items'][1]['action_id']])

    def test_a_step_recorded_this_week_can_be_undone_from_the_done_card(self):
        first = historical_build(self.records, self.links)['week']
        rows = [dict(week=first['week'], action_id=x['action_id'], record_id=x['record_id'], slot=x['slot'],
                     position=n, state='planned', reason='') for n, x in enumerate(first['items'])]
        a, b = first['items'][0], first['items'][1]
        mistake = sent(a['record_id'], '2026-10-13T22:38:00+01:00', a['action_id'])
        earlier = sent(b['record_id'], '2026-10-02T09:00:00+01:00', b['action_id'])
        week = historical_build(self.records, self.links, outcomes=[mistake, earlier], week_items=rows)['week']
        by_id = {x['action_id']: x for x in week['items']}
        done = by_id[a['action_id']]
        self.assertTrue(done['done'])
        undo = done['actions']
        self.assertEqual(len(undo), 1)
        self.assertEqual((undo[0]['method'], undo[0]['record_id'], undo[0]['outcome_id'], undo[0]['undo']),
                         ('correct', a['record_id'], mistake['id'], True))
        self.assertTrue(undo[0]['label'].startswith('Undo'))
        # A step from before this week is history: the week card does not offer to undo it.
        self.assertTrue(by_id[b['action_id']]['done'])
        self.assertEqual(by_id[b['action_id']]['actions'], [])
        corrected = dict(mistake, corrected_at='2026-10-13T22:40:00+01:00')
        again = {x['action_id']: x for x in historical_build(self.records, self.links, outcomes=[corrected, earlier],
                                                    week_items=rows)['week']['items']}
        self.assertFalse(again[a['action_id']]['done'])
        self.assertEqual([x['method'] for x in again[a['action_id']]['actions']], ['week'])

    def test_a_set_aside_item_can_be_put_back_while_it_still_qualifies(self):
        first = historical_build(self.records, self.links)['week']
        rows = [dict(week=first['week'], action_id=x['action_id'], record_id=x['record_id'], slot=x['slot'],
                     position=n, state='planned', reason='') for n, x in enumerate(first['items'])]
        rows[1]['state'], rows[1]['reason'] = 'deferred', 'Not this week'
        aside = first['items'][1]
        week = historical_build(self.records, self.links, week_items=rows)['week']
        back = week['deferred'][0]['actions']
        self.assertEqual([(a['method'], a['action_id']) for a in back], [('week_restore', aside['action_id'])])
        self.assertEqual(back[0]['label'], 'Put back')
        # The "Not this week" that set it aside declares that its notice may offer Undo.
        planned = next(x for x in week['items'] if x['state'] == 'planned' and not x['done'])
        self.assertTrue(planned['actions'][0]['undoable'])
        # An approach that no longer exists, or no longer qualifies, is not offered: it would come back unusable.
        gone = [dict(r) for r in rows] + [dict(week=first['week'], action_id='action:GONE:', record_id='GONE', slot='known',
                                                position=9, state='deferred', reason='Not this week')]
        self.assertEqual(next(d for d in historical_build(self.records, self.links, week_items=gone)['week']['deferred']
                              if d['action_id'] == 'action:GONE:')['actions'], [])
        excluded = [dict(r, exclusion='Not for now') if r['id'] == aside['record_id'] else r for r in self.records]
        self.assertEqual(historical_build(excluded, self.links, week_items=rows)['week']['deferred'][0]['actions'], [])

    def test_a_set_aside_item_he_acts_on_anyway_comes_back_as_done(self):
        first = historical_build(self.records, self.links)['week']
        rows = [dict(week=first['week'], action_id=x['action_id'], record_id=x['record_id'], slot=x['slot'],
                     position=n, state='planned', reason='') for n, x in enumerate(first['items'])]
        rows[1]['state'], rows[1]['reason'] = 'deferred', 'Not this week'
        aside = first['items'][1]
        contact = sent(aside['record_id'], '2026-10-14T09:00:00+01:00', aside['action_id'])
        week = historical_build(self.records, self.links, outcomes=[contact], week_items=rows)['week']
        self.assertEqual(week['deferred'], [])
        back = next(x for x in week['items'] if x['action_id'] == aside['action_id'])
        self.assertTrue(back['done'])
        self.assertEqual(back['actions'][0]['outcome_id'], contact['id'])

    def test_a_done_card_names_the_step_it_undoes(self):
        for kind, label in (('no_fit', 'Not a fit'), ('no_budget', 'No current budget'), ('proposal', 'Proposal sent'),
                            ('won', 'Engagement won'), ('permanent_only', 'Permanent only'), ('handed_over', 'Hand over to Work')):
            self.assertEqual(approaches.STEP_LABELS[kind], label)

    def test_a_handover_is_undone_from_the_card_only_while_its_opportunity_is_unchanged(self):
        first = historical_build(self.records, self.links)['week']
        rows = [dict(week=first['week'], action_id=x['action_id'], record_id=x['record_id'], slot=x['slot'],
                     position=n, state='planned', reason='') for n, x in enumerate(first['items'])]
        a = first['items'][0]
        steps = [sent(a['record_id'], '2026-10-12T09:00:00+01:00', a['action_id']),
                 sent(a['record_id'], '2026-10-13T09:00:00+01:00', a['action_id'], kind='conversation'),
                 sent(a['record_id'], '2026-10-14T09:00:00+01:00', a['action_id'], kind='handed_over',
                      snapshot={'action_id': a['action_id'], 'opportunity_id': 'O9', 'opportunity_revision': 3})]

        def offered(revision):
            opp = dict(id='O9', type='opportunity', name='Handed over', stage='conversation', revision=revision)
            result = historical_build(self.records + [opp], self.links, outcomes=steps, week_items=rows)
            card = next(x for x in result['week']['items'] if x['action_id'] == a['action_id'])
            sheet = next(i for i in result['actions'] if i['id'] == a['action_id'])['progress']['actions']
            return [x['label'] for x in card['actions']], [x['label'] for x in sheet if x['method'] == 'correct']

        self.assertEqual(offered(3), (['Undo: Hand over to Work'], ['Correct the last entry']))
        self.assertEqual(offered(4), ([], []))

    def test_refresh_keeps_what_he_acted_on_and_leaves_deferred_items_out(self):
        first = historical_build(self.records, self.links)['week']
        ids = [x['action_id'] for x in first['items']]
        rows = [dict(week=first['week'], action_id=x['action_id'], record_id=x['record_id'], slot=x['slot'],
                     position=n, state='planned', reason='') for n, x in enumerate(first['items'])]
        rows[1]['state'] = 'deferred'
        state = dict(records=self.records, links=[dict(from_id=a, to_id=b, relation=r) for a, b, r in self.links],
                     outcomes=[sent('K0', '2026-10-13T09:00:00+01:00', ids[0])], interactions=[], week_items=rows,
                     status_since={})
        intel = historical_build(self.records, self.links, outcomes=state['outcomes'], week_items=rows)
        chosen = approaches.refreshed_week(intel, state, TODAY)
        got = [c['action_id'] for c in chosen]
        self.assertEqual(got[0], ids[0])  # acted on, so it keeps its place although K0 was contacted recently
        self.assertNotIn(ids[1], got)
        self.assertEqual(len(chosen), approaches.config()['plan']['size'])
        self.assertEqual(len({c['record_id'] for c in chosen}), len(chosen))
        self.assertEqual(sum(c['record_id'] in ('J1', 'J2') for c in chosen), 0)  # jobs stay in Jobs

    def test_a_replacement_is_the_next_eligible_item_for_the_same_slot(self):
        result = historical_build(self.records, self.links)
        chosen = {x['action_id'] for x in result['week']['items']}
        replacement = approaches.replacement(result, 'known', chosen, today=TODAY,
                                             state=dict(records=self.records, outcomes=[], interactions=[]))
        self.assertEqual(replacement['slot'], 'known')
        self.assertNotIn(replacement['action_id'], chosen)


class FivePersonPilotTests(unittest.TestCase):
    def people(self, count=7):
        return [known(f'L{i}', relationship='warm', priority='first', research=[dict(
            approach=dict(wave=i + 1, role='connector', goal='Discuss a possible introduction'),
            decisive_unknowns=['Their current remit', 'Whether they can introduce you'])]) for i in range(count)]

    def test_pilot_selects_five_listed_people_in_wave_order_without_company_deduplication(self):
        people = self.people()
        result = historical_build([company(), *reversed(people), job()],
                       links=[(p['id'], 'C1', 'works_at') for p in people])
        self.assertEqual([x['record_id'] for x in result['week']['items']], ['L0', 'L1', 'L2', 'L3', 'L4'])
        self.assertEqual(result['week']['focus_ids'], [x['action_id'] for x in result['week']['items']])
        self.assertEqual(item(result, 'L0')['progress']['plan']['gaps'],
                         ['Their current remit', 'Whether they can introduce you'])

    def test_short_list_is_not_padded_and_listed_recruiters_share_the_priority_order(self):
        people = self.people(1)
        recruiter = known('R1', relationship='active', priority='first', kind='recruiter',
                          research=[dict(approach=dict(wave=2))])
        other = known('U1', relationship='warm')
        result = historical_build([*people, recruiter, other, job(), company()])
        self.assertEqual([x['record_id'] for x in result['week']['items']], ['L0', 'R1'])
        self.assertEqual(historical_build([other, job()])['week']['items'], [])

    def test_stored_overflow_is_preserved_and_readiness_changes_do_not_reorder_the_pilot(self):
        people = self.people()
        rows = [dict(week='2026-10-12', action_id=f'action:{p["id"]}:', record_id=p['id'],
                     position=i, slot='known', state='planned') for i, p in enumerate(people)]
        sent_step = sent('L6', '2026-10-13', 'action:L6:')
        result = historical_build(people, week_items=rows, outcomes=[sent_step])
        self.assertEqual(len(result['week']['items']), 7)
        self.assertEqual(result['week']['focus_ids'], [x['action_id'] for x in rows[:5]])
        self.assertTrue(result['week']['items'][6]['done'])
        changed = historical_build(people, week_items=rows, outcomes=[sent_step], drafts=[
            dict(record_id='L4', purpose='message', text='A prepared note', version_id=1)])
        self.assertEqual(changed['week']['focus_ids'], result['week']['focus_ids'])

    def test_a_finished_approach_gives_its_focus_place_to_the_next_saved_person(self):
        people = self.people()
        rows = [dict(week='2026-10-12', action_id=f'action:{p["id"]}:', record_id=p['id'],
                     position=i, slot='known', state='planned') for i, p in enumerate(people)]
        result = historical_build(people, week_items=rows, outcomes=[sent('L0', '2026-10-13', 'action:L0:')])
        self.assertEqual(result['week']['focus_ids'], [x['action_id'] for x in rows[1:6]])
        self.assertTrue(result['week']['items'][0]['done'])
        everyone_done = [sent(p['id'], '2026-10-13', f'action:{p["id"]}:') for p in people[:6]]
        result = historical_build(people, week_items=rows, outcomes=everyone_done)
        self.assertEqual(result['week']['focus_ids'], [rows[6]['action_id']] + [x['action_id'] for x in rows[:4]])

    def test_refresh_keeps_all_recorded_progress_even_when_it_exceeds_five(self):
        people = self.people(8)
        rows = [dict(week='2026-10-12', action_id=f'action:{p["id"]}:', record_id=p['id'],
                     position=i, slot='known', state='planned') for i, p in enumerate(people)]
        outcomes = [sent(p['id'], '2026-10-13', f'action:{p["id"]}:') for p in people[:6]]
        result = historical_build(people, week_items=rows, outcomes=outcomes)
        chosen = approaches.refreshed_week(result, dict(records=people, outcomes=outcomes, interactions=[]), today=TODAY)
        self.assertEqual([x['record_id'] for x in chosen], [p['id'] for p in people[:6]])
        self.assertEqual(len(result['week']['focus_ids']), 5)

    def test_replacement_respects_the_pilot_list_and_exclusions(self):
        people = self.people()
        people[5]['exclusion'] = 'Do not approach'
        result = historical_build(people)
        chosen = {x['action_id'] for x in result['week']['items']}
        state = dict(records=people, outcomes=[], interactions=[])
        replacement = approaches.replacement(result, 'known', chosen, today=TODAY, state=state)
        self.assertEqual(replacement['record_id'], 'L6')


class ListTests(unittest.TestCase):
    """The owner's list (Release 2): his priority sets the order, and once he has a list only it is suggested."""

    def test_listed_people_keep_his_order_and_spare_places_are_not_filled(self):
        people = [known('K0', tier='close', relationship='warm'), known('K1', relationship='active', priority='next'),
                  known('K2', tier='known', relationship='dormant', priority='first')]
        ids = [x['record_id'] for x in historical_build(people)['week']['items']]
        self.assertEqual(ids, ['K2', 'K1'])  # first, then next; unlisted contacts stay in People
        self.assertEqual(historical_build([known('K0', tier='close', relationship='warm')])['week']['items'], [])

    def test_a_list_that_fills_the_week_is_all_that_is_suggested(self):
        size = approaches.config()['plan']['size']
        records = [*(known(f'L{i}', relationship='active', priority='first') for i in range(size)),
                   known('K0', tier='close', relationship='warm')]
        result = historical_build(records)
        ids = [x['record_id'] for x in result['week']['items']]
        self.assertEqual(len(ids), size)
        self.assertNotIn('K0', ids)  # someone he knows but did not choose waits while his list fills the week
        state = dict(records=records, outcomes=[], interactions=[])
        chosen = {x['action_id'] for x in result['week']['items']}
        self.assertIsNone(approaches.replacement(result, 'known', chosen, today=TODAY, state=state))
        short = chosen - {'action:L0:', 'action:L1:', 'action:L2:'}  # three of the list are contacted elsewhere
        self.assertEqual(approaches.replacement(result, 'known', short, today=TODAY, state=state)['record_id'], 'L0')

    def test_a_recruiter_he_knows_or_chose_is_approached_like_a_desk(self):
        chosen = person('R1', role='Senior Recruitment Consultant', kind='recruiter', priority='next',
                        verification_level='', evidence_urls='')
        known_recruiter = known('R2', relationship='active', kind='recruiter')
        stranger = person('R3', role='Recruiter', kind='recruiter', verification_level='', evidence_urls='')
        result = historical_build([chosen, known_recruiter, stranger, known('K1', relationship='active', priority='first')])
        recruiter = item(result, 'R1')
        self.assertEqual((recruiter['kind'], recruiter['route'], recruiter['progress']['status']), ('recruiter', 'recruiter', 'ready'))
        self.assertEqual(recruiter['progress']['kind_label'], 'Recruiter')
        self.assertNotIn('R3', [a['record_id'] for a in result['actions'] + result['research']])
        week = {x['record_id']: x for x in result['week']['items']}
        self.assertEqual((week['R1']['slot'], week['R1']['reason']), ('contract', 'Your list · a recruiter'))
        order = [x['record_id'] for x in result['week']['items'] if x['slot'] == 'contract']
        self.assertEqual(order, ['R1'])  # only the recruiter he chose enters the pilot
        outcomes = [sent('R1', '2026-10-09T09:00:00+01:00', recruiter['id'])]  # Friday; three working days by Wednesday
        later = item(historical_build([chosen], outcomes=outcomes), 'R1')['progress']
        self.assertEqual(later['status'], 'waiting')
        replied = item(historical_build([chosen], outcomes=[*outcomes, sent('R1', '2026-10-13T09:00:00+01:00', recruiter['id'], kind='replied')]), 'R1')
        self.assertIn('submitted', [a['kind'] for a in replied['progress']['actions'] if a['method'] == 'outcome'])

    def test_someone_he_chose_but_does_not_know_waits_for_research(self):
        stranger = person('S1', role='CTO', priority='first', verification_level='', evidence_urls='')
        result = historical_build([stranger])
        self.assertIn('S1', [a['record_id'] for a in result['research']])
        self.assertEqual(result['week']['items'], [])


class MessageNoteTests(unittest.TestCase):
    """A working message is the note on the card. An approval draft still needs its decision."""

    def test_a_message_stays_ready_and_does_not_change_the_week(self):
        person = known('K1', relationship='warm', priority='first')
        without = build([person])
        with_note = build([person], drafts=[dict(record_id='K1', version_id=4, purpose='message',
                                                 title='WhatsApp', text='Hello from the note.')])
        base, noted = item(without, 'K1'), item(with_note, 'K1')
        self.assertEqual(base['progress']['status'], 'ready')
        self.assertEqual((noted['progress']['status'], noted['stage']), ('ready', base['stage']))
        self.assertIsNone(noted.get('draft_version_id'))
        self.assertNotIn('message_version_id', noted)
        self.assertIsNone(noted['progress']['message'])
        self.assertNotIn('version_id', noted['progress']['primary'])
        self.assertEqual(noted['progress']['primary']['method'], 'outcome')
        self.assertEqual([x['record_id'] for x in without['week']['items']],
                         [x['record_id'] for x in with_note['week']['items']])

    def test_an_approval_draft_still_needs_a_decision_before_it_can_be_copied(self):
        waiting = build([known('K1', relationship='warm', priority='first')],
                        drafts=[dict(record_id='K1', version_id=4, purpose='approval', text='Approve this')])
        review = item(waiting, 'K1')
        self.assertEqual(review['progress']['status'], 'ready')
        self.assertIsNone(review['progress']['message'])
        self.assertNotEqual(review['progress']['primary']['method'], 'copy')
        approved = build([company(), person()], links=[('P1', 'C1', 'works_at')],
                         drafts=[dict(record_id='P1', version_id=8, purpose='approval', text='Exact note')],
                         decisions=[dict(id=1, kind='draft', record_id='P1', status='done', answer='approve',
                                         draft_version_id=8, draft_binding='exact', draft_is_current=True)])
        note = item(approved, 'P1')
        self.assertEqual(note['progress']['status'], 'ready')
        self.assertEqual(note['progress']['primary']['method'], 'outcome')
        self.assertIsNone(note['progress']['message'])

    def test_sent_words_stay_when_the_current_note_changes(self):
        person = known('K1', relationship='warm', priority='first')
        aid = item(build([person]), 'K1')['id']
        outcome = sent('K1', '2026-10-14T09:00:00+01:00', aid,
                       snapshot={'action_id': aid, 'message': 'The words that went.'})
        progress = item(build([person], outcomes=[outcome], drafts=[dict(
            record_id='K1', version_id=9, purpose='message', text='A later edit.')]), 'K1')['progress']
        self.assertEqual(progress['status'], 'waiting')
        self.assertIsNone(progress['sent'])
        self.assertIsNone(progress['message'])

    def test_a_follow_up_shows_the_new_note_and_keeps_what_was_sent(self):
        person = known('K1', relationship='warm', priority='first')
        aid = item(build([person]), 'K1')['id']
        outcome = sent('K1', '2026-10-01T09:00:00+01:00', aid,
                       snapshot={'action_id': aid, 'message': 'The first note.'})
        progress = item(build([person], outcomes=[outcome], drafts=[dict(
            record_id='K1', version_id=6, purpose='message', title='WhatsApp', text='Just checking in.')]), 'K1')['progress']
        self.assertEqual(progress['status'], 'waiting')
        self.assertIsNone(progress['message'])
        self.assertIsNone(progress['sent'])
        self.assertNotIn('version_id', progress['primary'])


class TalkingPointTests(unittest.TestCase):
    """Two or three talking points per move before contact (Release 2): never a draft, never a price."""

    def test_someone_he_knows_gets_his_note_then_his_kind_of_ask(self):
        noted = known('K1', relationship='warm', priority='first', preferred_ask='catch_up', preferred_channel='whatsapp',
                      known_context='Imported LinkedIn observation: Northwind.\nYour note (14 Oct 2026): Ran the payments team with you.')
        progress = item(build([noted]), 'K1')['progress']
        cfg = approaches.config()
        self.assertEqual(progress['points'], ['How you know them: Ran the payments team with you.', cfg['asks']['catch_up']])
        self.assertEqual((progress['channel'], progress['ask']), ('whatsapp', 'catch_up'))
        sent = next(a for a in progress['actions'] if a['kind'] == 'sent')
        self.assertEqual(sent['channel'], 'whatsapp')

    def test_an_old_conversation_is_the_reason_and_the_catch_up_follows(self):
        old = known('K2', tier='known', relationship='dormant',
                    linkedin={'evidence': {'threads': {'two_way': 3, 'last_two_way': '2019-05-01'}}})
        points = item(build([old]), 'K2')['progress']['points']
        self.assertEqual(points, ['You last spoke in 2019: reason enough to get back in touch.', approaches.config()['asks']['catch_up']])

    def test_a_recruiter_gets_where_they_work_the_question_and_availability(self):
        r = person('R1', role='Recruitment Consultant', kind='recruiter', priority='next', verification_level='', evidence_urls='',
                   linkedin={'observed_company': 'Acme Recruitment'})
        points = item(build([r]), 'R1')['progress']['points']
        self.assertEqual(points[0], 'Recruits at Acme Recruitment (per LinkedIn, Sep 2026).')
        self.assertTrue(points[1].startswith('Ask: Which current contract or interim briefs'))
        self.assertEqual(points[2], approaches.config()['asks']['availability'])

    def test_a_note_to_a_decision_maker_has_its_reason_and_question_but_no_generic_ask(self):
        result = build([company(), person(), job()], links=[('P1', 'C1', 'works_at'), ('J1', 'C1', 'hiring_for')])
        points = item(result, 'P1')['progress']['points']
        self.assertEqual(len(points), 2)
        self.assertTrue(points[1].startswith('Ask: '))
        self.assertNotIn(approaches.config()['asks']['introduction'], points)

    def test_no_points_once_contact_is_made(self):
        k = known('K1', relationship='warm')
        action_id = item(build([k]), 'K1')['id']
        progress = item(build([k], outcomes=[sent('K1', '2026-10-13T09:00:00+01:00', action_id)]), 'K1')['progress']
        self.assertEqual((progress['status'], progress['points']), ('waiting', []))

    def test_the_tally_counts_this_weeks_contacts_replies_and_conversations(self):
        k = known('K1', relationship='warm')
        aid = item(build([k]), 'K1')['id']
        outcomes = [sent('K1', '2026-10-09T09:00:00+01:00', aid),  # last week
                    sent('K1', '2026-10-12T09:00:00+01:00', aid, id=2), sent('K1', '2026-10-13T09:00:00+01:00', aid, kind='replied', id=3),
                    sent('K1', '2026-10-14T09:00:00+01:00', aid, kind='conversation', id=4)]
        self.assertEqual(build([k], outcomes=outcomes)['tally'], dict(contacted=1, replies=1, conversations=1))


class SuggestionScopeTests(unittest.TestCase):
    def test_people_at_an_excluded_company_are_not_suggested(self):
        excluded = known('KX', tier='close', relationship='warm',
                         exclusion_contexts=[{'company': 'Excluded Co', 'reason': 'The owner excluded it'}])
        result = build([excluded, known('K1', tier='close', relationship='warm')])
        self.assertNotIn('KX', [x['record_id'] for x in result['week']['items']])
        self.assertNotIn(item(result, 'KX')['id'], result['sections'].get('people', []))
        self.assertIn(item(result, 'K1')['id'], result['sections']['people'])

    def test_someone_you_know_at_two_companies_has_one_card(self):
        records = [company('C1'), company('C2'), known('K1'), job('J1'), job('J2', url='https://jobs.example/J2')]
        links = [('K1', 'C1', 'works_at'), ('K1', 'C2', 'works_at'), ('J1', 'C1', 'hiring_for'), ('J2', 'C2', 'hiring_for')]
        result = build(records, links)
        mine = [a for a in result['actions'] if a['record_id'] == 'K1']
        self.assertEqual(len(mine), 2)
        self.assertEqual(sum(1 for a in mine if a['id'] in result['sections']['people']), 1)



def prepared(**values):
    """The agent's prepared approach for one person, as a research batch records it."""
    approach = dict(role='connector', points=['Congratulate them on the award.', 'Ask how hiring is going.'],
                    ask='Who leads engineering at Northwind?', cv='QE', goal='Two named introductions', wave=1)
    approach.update(values)
    return [dict(approach=approach, reviewed_at='2026-10-15', source_urls=['https://northwind.example/about'],
                 summary='Head of QA at Northwind.')]


class PreparedApproachTests(unittest.TestCase):
    """The first batch (15 October): each person's role in the search, fitted ask, CV, aim and wave."""

    def test_a_prepared_approach_supplies_the_points_the_ask_and_the_plan(self):
        progress = item(build([known('K1', relationship='warm', priority='first', research=prepared())]), 'K1')['progress']
        self.assertEqual(progress['points'], ['Congratulate them on the award.', 'Ask how hiring is going.',
                                              'Who leads engineering at Northwind?'])
        self.assertEqual(progress['plan'], dict(role='Connector', cv='Head of Quality Engineering',
                                                cv_url=approaches.search_config.cvs()['QE']['url'],
                                                goal='Two named introductions', wave=1, last='', checked='2026-10-15',
                                                source='https://northwind.example/about', check='', gaps=[]))
        self.assertTrue(progress['next_is_default'])  # the button says it; the card leaves the line out
        plain = item(build([known('K2', relationship='warm', priority='first')]), 'K2')['progress']
        self.assertIsNone(plain['plan'])
        self.assertEqual(plain['points'][-1], approaches.config()['asks']['catch_up'])

    def test_an_unsettled_question_on_the_preparation_shows_as_check_first(self):
        research = prepared()
        research[0]['decisive_unknowns'] = ['Whether they still lead the platform team']
        plan = item(build([known('K1', relationship='warm', research=research)]), 'K1')['progress']['plan']
        self.assertEqual(plan['check'], 'Whether they still lead the platform team')

    def test_the_last_conversation_shows_on_the_plan_before_and_after_contact(self):
        last = 'Mar 2026, email: they offered to meet in April.'
        k = known('K1', relationship='warm', research=prepared(last=last))
        before = item(build([k]), 'K1')
        after = item(build([k], outcomes=[sent('K1', '2026-10-13T09:00:00+01:00', before['id'])]), 'K1')
        self.assertEqual((before['progress']['plan']['last'], after['progress']['plan']['last']), (last, last))

    def test_a_draft_cv_says_so_on_the_plan(self):
        from unittest import mock
        cvs = {'Director': dict(title='Director of Engineering / CTO', url='https://example.com/director-cv', readiness='draft'),
               'QE': dict(title='Head of Quality Engineering', url='', readiness='ready')}
        with mock.patch.object(approaches.search_config, 'cvs', return_value=cvs):
            draft = item(build([known('K1', relationship='warm', research=prepared(cv='Director'))]), 'K1')['progress']['plan']
            ready = item(build([known('K2', relationship='warm', research=prepared(cv='QE'))]), 'K2')['progress']['plan']
        self.assertEqual(draft['cv'], 'Director of Engineering / CTO (still a draft)')
        self.assertEqual(draft['cv_url'], 'https://example.com/director-cv')
        self.assertEqual(ready['cv_url'], '')
        self.assertEqual(ready['cv'], 'Head of Quality Engineering')

    def test_a_prepared_approach_without_an_ask_ends_with_the_persons_kind_of_ask(self):
        k = known('K1', relationship='warm', preferred_ask='introduction', research=prepared(ask=''))
        points = item(build([k]), 'K1')['progress']['points']
        self.assertEqual(points[-1], approaches.config()['asks']['introduction'])

    def test_prepared_points_end_once_contact_is_made(self):
        k = known('K1', relationship='warm', research=prepared())
        action_id = item(build([k]), 'K1')['id']
        progress = item(build([k], outcomes=[sent('K1', '2026-10-13T09:00:00+01:00', action_id)]), 'K1')['progress']
        self.assertEqual((progress['status'], progress['points']), ('waiting', []))

    def test_waves_set_the_order_within_his_own_order(self):
        people = [known('W2', tier='close', relationship='warm', priority='first', research=prepared(wave=2)),
                  known('W1', tier='known', relationship='dormant', priority='first', research=prepared(wave=1)),
                  known('NW', tier='close', relationship='warm', priority='first')]
        self.assertEqual([x['record_id'] for x in historical_build(people)['week']['items']], ['W1', 'W2', 'NW'])
        mixed = [known('A', relationship='active', priority='next', research=prepared(wave=1)),
                 known('B', relationship='active', priority='first', research=prepared(wave=3))]
        self.assertEqual([x['record_id'] for x in historical_build(mixed)['week']['items']], ['B', 'A'])  # his priority wins


class ClashTests(unittest.TestCase):
    """A recruiter's submission and a referral to the same company can clash (the owner's rule, 15 October)."""

    def setUp(self):
        self.friend = known('F1', relationship='warm', priority='first', organisation='ClearBank Ltd', preferred_ask='introduction')
        self.recruiter = person('R1', role='Recruiter', kind='recruiter', priority='first', verification_level='', evidence_urls='')
        base = build([self.friend, self.recruiter])
        self.f, self.r = item(base, 'F1')['id'], item(base, 'R1')['id']

    def submitted(self, at, company='ClearBank', **kwargs):
        return sent('R1', at, self.r, kind='submitted', snapshot={'action_id': self.r, 'target': {'name': company}}, **kwargs)

    def warnings(self, outcomes):
        result = build([self.friend, self.recruiter], outcomes=outcomes)
        return item(result, 'R1')['progress']['warning'], item(result, 'F1')['progress']['warning']

    def test_a_submission_while_a_referral_is_under_way_warns_on_both(self):
        recruiter, friend = self.warnings([sent('F1', '2026-10-12T09:00:00+01:00', self.f),
                                           sent('R1', '2026-10-12T10:00:00+01:00', self.r, kind='replied'),
                                           self.submitted('2026-10-13T10:00:00+01:00')])
        self.assertIn('ClearBank', recruiter)
        self.assertIn('Person F1', recruiter)
        self.assertIn('Person R1', friend)
        self.assertIn('13 Oct', friend)

    def test_a_referral_after_a_submission_warns_and_a_corrected_submission_does_not(self):
        outcomes = [self.submitted('2026-10-09T10:00:00+01:00'), sent('F1', '2026-10-12T09:00:00+01:00', self.f),
                    sent('F1', '2026-10-13T09:00:00+01:00', self.f, kind='referral')]
        recruiter, friend = self.warnings(outcomes)
        self.assertTrue(recruiter and friend)
        corrected = [self.submitted('2026-10-09T10:00:00+01:00', corrected_at='2026-10-10T09:00:00+01:00'), *outcomes[1:]]
        self.assertEqual(self.warnings(corrected), ('', ''))

    def test_no_warning_for_another_company_or_before_the_referral_is_asked(self):
        other = [sent('F1', '2026-10-12T09:00:00+01:00', self.f), self.submitted('2026-10-13T10:00:00+01:00', company='Monzo')]
        self.assertEqual(self.warnings(other), ('', ''))
        self.assertEqual(self.warnings([self.submitted('2026-10-13T10:00:00+01:00')]), ('', ''))  # nothing sent to the friend yet

    def test_a_recruiter_is_reminded_of_the_consent_rule_before_any_submission(self):
        progress = item(build([self.recruiter], outcomes=[sent('R1', '2026-10-13T10:00:00+01:00', self.r, kind='replied')]), 'R1')['progress']
        self.assertIn('your yes', progress['next'])
        submit = next(a for a in progress['actions'] if a.get('kind') == 'submitted')
        self.assertEqual(submit['asks'], 'company')

    def test_warning_uses_the_approachs_company_not_another_employer(self):
        friend = dict(self.friend, organisation='Company C1')
        people = [friend, self.recruiter, company('C1'), company('C2')]
        links = [('F1', 'C1', 'works_at'), ('F1', 'C2', 'related')]
        actions = build(people, links=links)['actions']
        referral = next(a for a in actions if a['person_id'] == 'F1' and a['company_id'] == 'C2')
        outcomes = [sent('F1', '2026-10-12', referral['id'])]
        other = build(people, links=links, outcomes=[*outcomes, self.submitted('2026-10-13', company='Company C1')])
        self.assertFalse(next(a for a in other['actions'] if a['id'] == referral['id'])['progress']['warning'])
        same = build(people, links=links, outcomes=[*outcomes, self.submitted('2026-10-13', company='Company C2')])
        self.assertTrue(next(a for a in same['actions'] if a['id'] == referral['id'])['progress']['warning'])


class IntroducerTests(unittest.TestCase):
    """A prompt to thank and update whoever made an introduction, derived from outcomes and links."""

    def test_an_introduction_prompts_thanks_and_a_conversation_prompts_an_update(self):
        intro, met = known('I1', relationship='warm', priority='first'), known('M1', relationship='active', priority='first')
        links = [('I1', 'M1', 'introducer')]
        base = build([intro, met], links=links)
        i, m = item(base, 'I1')['id'], item(base, 'M1')['id']
        offered = build([intro, met], links=links, outcomes=[sent('I1', '2026-10-12T09:00:00+01:00', i),
                                                             sent('I1', '2026-10-13T09:00:00+01:00', i, kind='introduction')])
        self.assertIn('thank them', item(offered, 'I1')['progress']['next'].lower())
        replied = build([intro, met], links=links, outcomes=[sent('M1', '2026-10-13', m, kind='replied')])
        self.assertNotIn('how it went', item(replied, 'M1')['progress']['next'])
        spoke = build([intro, met], links=links, outcomes=[sent('M1', '2026-10-12T09:00:00+01:00', m),
                                                           sent('M1', '2026-10-13T09:00:00+01:00', m, kind='conversation')])
        self.assertIn('Person I1', item(spoke, 'M1')['progress']['next'])
        alone = build([met], outcomes=[sent('M1', '2026-10-12T09:00:00+01:00', m), sent('M1', '2026-10-13T09:00:00+01:00', m, kind='conversation')])
        self.assertNotIn('introduced', item(alone, 'M1')['progress']['next'])


class ReconnectAndShortlistTests(unittest.TestCase):
    def test_raw_interactions_do_not_silently_drop_a_contact_from_this_week(self):
        k = known('K1', tier='close', relationship='warm', priority='first')
        result = historical_build([k], interactions=[dict(record_id='K1', channel='email', direction='out',
                                               at='2026-10-10', summary='Past email import')])
        self.assertIn('K1', [x['record_id'] for x in result['week']['items']])

    def test_recorded_outcome_note_leads_the_next_step_and_an_agent_follow_up_never_does(self):
        k = known('K1', tier='close', relationship='warm', priority='first',
                  next_action='Follow up on November board advisory brief', owner='owner', working_state='ready')
        aid = item(build([k]), 'K1')['id']
        self.assertNotIn('November board', item(build([k]), 'K1')['progress']['next'])
        with_note = build([k], outcomes=[dict(id=1, record_id='K1', approach_id=aid, kind='conversation',
                                              channel='phone', note='Send CV and call back Friday',
                                              occurred_at='2026-10-14T10:00:00+01:00')])
        self.assertTrue(item(with_note, 'K1')['progress']['next'].startswith('Send CV and call back Friday.'))

    def test_shortlisted_job_is_tracked_even_when_permanent_or_unconfirmed_terms(self):
        perm = job('JP', status='pursue', relevance={
            'band': 'low', 'lane': 'permanent', 'last_seen': '2026-10-10',
            'qualification': {'eligible': True, 'domain': 'software', 'engagement': 'permanent', 'conflicts': []}})
        result = build([company(), perm], links=[('JP', 'C1', 'hiring_for')])
        tracked = item(result, 'JP')
        self.assertEqual((tracked['kind'], tracked['progress']['section']), ('application', 'adverts'))
        self.assertEqual(tracked['progress']['primary']['to'], 'applied')
        self.assertNotIn('JP', [x['record_id'] for x in result['week']['items']])  # still tracked in Jobs, outside the people pilot


class HandoverTests(unittest.TestCase):
    def test_an_opportunity_handed_over_shows_the_step_agreed_and_no_handover(self):
        opp = dict(id='O1', type='opportunity', name='Scoped engagement', stage='lead', next_action='Send the scoped proposal by Friday',
                   summary='An inbound enquiry for interim engineering leadership')
        aid = item(build([opp]), 'O1')['id']
        p = item(build([opp], outcomes=[sent('O1', '2026-10-13T09:00:00+01:00', aid, kind='conversation')]), 'O1')['progress']
        self.assertTrue(p['next'].startswith('Send the scoped proposal by Friday. '), p['next'])
        self.assertNotIn('handover', [a['id'] for a in p['actions']])


class CorrespondenceTests(unittest.TestCase):
    """Email logged as interactions keeps the card current: the latest direct email shows, and a reply
    newer than his last step comes first."""

    def setUp(self):
        self.k = known('K1', tier='close', relationship='warm', priority='first')
        self.aid = item(build([self.k]), 'K1')['id']
        self.sent = [sent('K1', '2026-09-20T09:00:00+01:00', self.aid)]

    def mail(self, at, direction='in'):
        return dict(id=1, record_id='K1', person_id='K1', channel='email', direction=direction, at=at,
                    summary='Direct email', evidence_ref='https://mail.google.com/mail/u/0/#all/abc')

    def test_an_email_after_the_send_makes_recording_the_reply_the_next_step(self):
        p = item(build([self.k], outcomes=self.sent, interactions=[self.mail('2026-10-08T10:00:00+01:00')]), 'K1')['progress']
        self.assertEqual(p['status'], 'waiting')
        self.assertEqual(p['next'], 'They wrote on 8 Oct: record their reply.')
        self.assertEqual(p['primary']['kind'], 'replied')
        self.assertEqual(p['mail'], dict(at='2026-10-08', direction='in', url='https://mail.google.com/mail/u/0/#all/abc'))

    def test_an_email_before_the_send_changes_nothing(self):
        p = item(build([self.k], outcomes=self.sent, interactions=[self.mail('2026-09-19T10:00:00+01:00')]), 'K1')['progress']
        self.assertEqual(p['primary']['kind'], 'replied')
        self.assertIn('Waiting for a reply', p['next'])

    def test_next_uses_his_own_note_not_the_history_line(self):
        reply = sent('K1', '2026-10-13T09:00:00+01:00', self.aid, kind='replied', note='Call on Friday',
                     snapshot={'action_id': self.aid, 'target': {'name': 'Someone Else'}})
        p = item(build([self.k], outcomes=[*self.sent, reply]), 'K1')['progress']
        self.assertTrue(p['next'].startswith('Call on Friday. '), p['next'])


if __name__ == '__main__':
    unittest.main()


class AttentionTests(unittest.TestCase):
    def project(self, status='sent', at='2026-10-13', **kwargs):
        people = [known('W', relationship='warm', list_priority='first'), known('N', relationship='warm', list_priority='next')]
        self.people = people
        rows = [dict(week='2026-10-12', action_id=f'action:{p["id"]}:', record_id=p['id'], position=n, slot='known', state='planned') for n,p in enumerate(people)]
        return build(people, week_items=rows, outcomes=[sent('W', at, 'action:W:', kind=status)], **kwargs)

    def test_person_history_stays_off_work_attention_without_rewriting_saved_membership(self):
        result = self.project()
        work = selected(result, self.people)
        self.assertFalse(work['waiting'])
        self.assertFalse(work['act_now'])
        self.assertEqual([i['record_id'] for i in result['week']['items']], ['W','N'])
        self.assertNotIn('attention', result)
        self.assertTrue(all(not i['actions'] for i in result['week']['items']))

    def test_personal_reply_is_kept_in_people_without_becoming_work_attention(self):
        for event in ('sent','replied'):
            result = self.project(event,'2026-10-01')
            self.assertEqual(item(result,'W')['progress']['status'], 'waiting' if event == 'sent' else 'replied')
            self.assertFalse(selected(result, self.people)['act_now'])

    def test_incoming_mail_prompts_recording_without_changing_status(self):
        result=self.project(interactions=[dict(record_id='W',channel='email',direction='in',at='2026-10-14T10:00:00Z')])
        self.assertFalse(selected(result, self.people)['act_now'])
        self.assertEqual(item(result,'W')['progress']['status'],'waiting')
        self.assertTrue(item(result,'W')['progress']['reply_to_record'])

    def test_job_stages_and_incoming_label_are_kept(self):
        roles=[job('O',status='offer'),job('I',status='interview',incoming=dict(current=True,status='interview',label='Screening completed',next_step='Await the recruiter outcome.')),job('A',status='applied')]
        result=build(roles,status_since={'A':'2026-10-13'})
        work=selected(result,roles)
        self.assertEqual([i['record_id'] for i in work['act_now']],['O'])
        self.assertEqual([i['record_id'] for i in work['in_progress']],['I'])
        self.assertEqual(item(result,'I')['progress']['label'],'Screening completed')
        self.assertEqual(item(result,'I')['progress']['next'],'Await the recruiter outcome.')
        self.assertEqual([i['record_id'] for i in work['waiting']],['A'])

    def test_projection_is_pure_and_small_even_with_many_approaches(self):
        from copy import deepcopy
        intel=self.project(); before=deepcopy(intel)
        projected=selected(intel,self.people)
        self.assertEqual(intel,before)
        self.assertNotIn('attention',intel)
        for group,refs in projected.items():
            if isinstance(refs,list):
                self.assertEqual(refs,[])


class AttentionBoundaryTests(unittest.TestCase):
    def test_corrected_contact_returns_to_outreach_and_late_mail_prompts_review(self):
        p=known('L',relationship='warm',list_priority='first')
        rows=[dict(week='2026-10-12',action_id='action:L:',record_id='L',position=0,slot='known',state='planned')]
        corrected=sent('L','2026-10-13','action:L:',corrected_at='2026-10-14')
        result=build([p],week_items=rows,outcomes=[corrected])
        self.assertFalse(selected(result,[p])['act_now'])
        self.assertEqual(item(result,'L')['progress']['status'], 'ready')
        result=build([p],week_items=rows,outcomes=[sent('L','2026-09-01','action:L:'),sent('L','2026-09-09','action:L:',kind='followed_up')],interactions=[dict(record_id='L',channel='email',direction='in',at='2026-10-14T10:00:00Z')])
        self.assertEqual(item(result,'L')['progress']['status'],'waiting')
        self.assertTrue(item(result,'L')['progress']['reply_to_record'])
        self.assertFalse(selected(result,[p])['act_now'])

    def test_shortlisted_job_is_actionable_but_discovery_does_not_join_the_week(self):
        roles=[job('S',status='pursue'),job('D')]
        result=build(roles)
        self.assertEqual([i['record_id'] for i in selected(result,roles)['act_now']],['S'])
        self.assertFalse(result['week']['items'])

    def test_demoted_person_is_excluded_from_approaches_and_selection_until_promoted(self):
        co = company('C1')
        dm = person('P1', relationship='warm', priority='first')
        aid = 'action:P1:C1'
        steps = [dict(record_id='P1', action_id=aid, value={'kind': 'mine', 'text': 'Follow up with CTO'},
                      context={'route': 'direct', 'company_id': 'C1', 'person_id': 'P1'})]
        active_res = build([co, dm], links=[('P1', 'C1', 'works_at')],
                           outcomes=[sent('P1', '2026-10-01', aid)], approach_next_steps=steps)
        self.assertIn(aid, active_res['sections'].get('in_progress', []))
        demoted = dict(dm, relationship='do_not_contact', priority=None)
        demoted_res = build([co, demoted], links=[('P1', 'C1', 'works_at')],
                            outcomes=[sent('P1', '2026-10-01', aid)], approach_next_steps=steps)
        for sec, ids in demoted_res['sections'].items():
            self.assertNotIn(aid, ids, f'demoted approach leaked into section {sec}')
        self.assertNotIn(aid, demoted_res['followups'])
        promoted = dict(dm, relationship='dormant', priority=None)
        promoted_res = build([co, promoted], links=[('P1', 'C1', 'works_at')],
                             outcomes=[sent('P1', '2026-10-01', aid)], approach_next_steps=steps)
        self.assertIn(aid, promoted_res['sections'].get('in_progress', []))


class ProgressProjectionCapacityTests(unittest.TestCase):
    def context(self, records, links=(), **extra):
        return approaches._Context({}, dict(records=records, links=links, **extra), TODAY, approaches.config())

    def test_link_lookups_inspect_edges_linearly(self):
        class CountedLinks(list):
            visits = 0
            def __iter__(self):
                for link in super().__iter__():
                    self.visits += 1
                    yield link
        for size in (40, 160):
            with self.subTest(size=size):
                records = [dict(id='I', type='person', name='Introducer')]
                links = CountedLinks()
                for n in range(size):
                    records += [dict(id=f'P{n}', type='person', name=f'Person {n}'),
                                dict(id=f'C{n}', type='company', name=f'Employer {n}'),
                                dict(id=f'J{n}', type='job', name=f'Job {n}')]
                    links.extend([dict(from_id=f'P{n}', to_id=f'C{n}', relation='works_at'),
                                  dict(from_id='I', to_id=f'P{n}', relation='introducer'),
                                  dict(from_id=f'J{n}', to_id=f'C{n}', relation='hiring_for')])
                ctx = self.context(records, links)
                for n in range(size):
                    self.assertEqual(approaches._introducers({'person_id': f'P{n}'}, ctx), ['Introducer'])
                    self.assertEqual(ctx.org_of(f'J{n}')['id'], f'C{n}')
                    self.assertEqual(approaches._companies({'id': f'P{n}'}, ctx), {f'employer {n}': f'Employer {n}'})
                self.assertLessEqual(links.visits, 3 * len(links))

    def test_link_lookups_keep_direction_first_match_order_duplicates_and_types(self):
        records = [dict(id=rid, type=kind, name=name) for rid, kind, name in [
            ('P', 'person', 'Recipient'), ('I1', 'person', 'First'), ('I2', 'person', 'Second'),
            ('C1', 'company', 'Example Ltd'), ('C2', 'agency', 'Example Limited'), ('J', 'job', 'Job')]]
        links = [dict(from_id=a, to_id=b, relation=relation) for a, b, relation in [
            ('I2', 'P', 'introducer'), ('I1', 'P', 'introducer'), ('I2', 'P', 'introducer'),
            ('P', 'I1', 'introducer'), ('C1', 'P', 'introducer'), ('missing', 'P', 'introducer'),
            ('C1', 'J', 'hiring_for'), ('J', 'missing', 'hiring_for'), ('J', 'C2', 'advertised_by'),
            ('J', 'C1', 'hiring_for'), ('P', 'C1', 'works_at'), ('C2', 'P', 'works_at'),
            ('P', 'I1', 'works_at'), ('missing', 'P', 'works_at')]]
        ctx = self.context(records, links)
        self.assertEqual(approaches._introducers({'person_id': 'P'}, ctx), ['Second', 'First', 'Second'])
        self.assertEqual(approaches._introducers({'person_id': 'I1'}, ctx), ['Recipient'])
        self.assertEqual(approaches._introducers({}, ctx), [])
        self.assertEqual(ctx.org_of('J')['id'], 'C2')
        self.assertIsNone(ctx.org_of('P'))
        self.assertEqual(approaches._companies(dict(id='P', organisation='Own Employer',
            linkedin={'observed_company': 'LinkedIn Employer'}), ctx),
            {'own employer': 'Own Employer', 'linkedin employer': 'LinkedIn Employer', 'example': 'Example Limited'})
        # A separate read cannot retain a former snapshot's links.
        empty = self.context(records)
        self.assertEqual(approaches._introducers({'person_id': 'P'}, empty), [])
        self.assertIsNone(empty.org_of('J'))

    def test_ready_progress_does_not_request_unused_introducers(self):
        from unittest.mock import patch
        ctx = self.context([job('J', status='pursue')])
        with patch.object(approaches, '_introducers', side_effect=AssertionError('Unused graph lookup')):
            result = approaches._texts(dict(kind='application', record_id='J', brief={}), ctx, 'ready', None)
        self.assertEqual(result, ('A relevant advertised role.', 'Apply through the advert with the suggested CV.',
                                  'Apply through the advert.'))

    def test_ready_job_constructs_only_its_visible_action(self):
        from unittest.mock import patch
        ctx = self.context([job('J', status='pursue')])
        with patch.object(approaches, '_act', wraps=approaches._act) as make:
            actual = approaches._actions(dict(id='A', kind='application', record_id='J'), ctx, 'ready', [])
        self.assertEqual(actual, [dict(id='applied', label='I applied', method='transition', record_id='J',
                                       field='status', to='applied', primary=True)])
        self.assertEqual(make.call_count, 1)

    def test_job_action_order_tracks_status_and_allowed_transitions(self):
        cases = [
            ('ready', 'pursue', ['applied']),
            ('waiting', 'applied', ['responded', 'followed_up', 'rejected', 'closed', 'correct']),
            ('follow_up', 'applied', ['followed_up', 'responded', 'rejected', 'closed', 'correct']),
            ('no_response', 'applied', ['responded', 'closed', 'correct']),
            ('replied', 'responded', ['interview', 'rejected', 'correct']),
            ('interview', 'interview', ['offer', 'rejected', 'correct']),
            ('offer', 'offer', ['closed', 'rejected', 'correct']),
            ('closed', 'closed', ['correct']), ('held', 'pursue', []),
            ('ready', 'closed', []),  # Closed applications cannot jump straight to applied.
        ]
        methods = {'followed_up': 'outcome', 'correct': 'correct'}
        for progress_status, record_status, ids in cases:
            with self.subTest(progress=progress_status, record=record_status):
                ctx = self.context([job('J', status=record_status)])
                actions = approaches._actions(dict(id='A', kind='application', record_id='J'), ctx, progress_status, [])
                self.assertEqual([a['id'] for a in actions], ids)
                self.assertEqual([a['method'] for a in actions], [methods.get(id, 'transition') for id in ids])
                self.assertEqual([a['primary'] for a in actions], [i == 0 for i in range(len(ids))])
                self.assertTrue(all(a['record_id'] == 'J' for a in actions))

    def test_person_action_order_and_explicit_handover_routes(self):
        cases = [
            ('ask', 'ready', ['sent', 'next_step']), ('ask', 'review', ['sent', 'next_step']),
            ('ask', 'approved', ['sent', 'next_step']),
            ('ask', 'waiting', ['replied', 'followed_up', 'no_response', 'next_step']),
            ('ask', 'follow_up', ['followed_up', 'replied', 'no_response', 'next_step']),
            ('ask', 'no_response', ['replied']),
            ('ask', 'replied', ['conversation', 'referral', 'introduction', 'no_fit', 'no_budget', 'next_step']),
            ('desk', 'replied', ['conversation', 'submitted', 'no_fit', 'no_budget', 'next_step']),
            ('ask', 'submitted', ['conversation', 'no_fit', 'next_step']),
            ('ask', 'conversation', ['handover', 'proposal', 'no_fit', 'no_budget', 'permanent_only', 'next_step']),
            ('ask', 'proposal', ['handover', 'won', 'no_budget', 'next_step']),
            ('ask', 'won', ['handover']), ('ask', 'held', []),
            ('opportunity', 'conversation', ['proposal', 'no_fit', 'no_budget', 'permanent_only']),
            ('opportunity', 'won', ['open']), ('tender', 'ready', ['open']),
        ]
        for kind, status, ids in cases:
            with self.subTest(kind=kind, status=status):
                record = dict(id='P', type='opportunity' if kind == 'opportunity' else 'person', name='Person')
                ctx = self.context([record])
                actions = approaches._actions(dict(id='A', kind=kind, record_id='P', person_id='P'), ctx, status, [])
                self.assertEqual([a['id'] for a in actions], ids)
                self.assertEqual([a['primary'] for a in actions], [i == 0 for i in range(len(ids))])
                self.assertTrue(all(a['record_id'] == 'P' for a in actions))
        ctx = self.context([dict(id='P', type='person')], outcomes=[dict(id=1, record_id='P', kind='handed_over',
            at='2026-10-01', snapshot={'action_id': 'A', 'opportunity_id': 'O'})])
        item = dict(id='A', kind='ask', record_id='P', person_id='P')
        self.assertEqual(approaches._actions(item, ctx, 'won', [{'kind': 'handed_over'}]), [])
        self.assertEqual(approaches._actions(item, ctx, 'handed_over', []),
                         [dict(id='open', label='Open in Work', method='open', record_id='O', primary=True)])

    def test_action_metadata_preserves_channel_version_targets_and_filtering(self):
        p = dict(id='P', type='person', preferred_channel='email')
        item = dict(id='A', kind='ask', record_id='P', person_id='P', company_id='C')
        ctx = self.context([p])
        actions = approaches._actions(item, ctx, 'ready', [], message={'version_id': 17})
        self.assertEqual(actions, [
            dict(id='sent', label='I sent my ask', method='outcome', kind='sent', record_id='P', action_id='A',
                 channel='email', version_id=17, primary=True),
            dict(id='next_step', label='Update next step', method='next_step', record_id='P', action_id='A', primary=False)])
        followed = approaches._actions(item, ctx, 'follow_up', [], message={'version_id': 18})[0]
        self.assertEqual(followed, dict(id='followed_up', label='I followed up', method='outcome', kind='followed_up',
                                       record_id='P', action_id='A', channel='email', version_id=18, primary=True))
        submitted = approaches._actions(dict(item, kind='desk'), ctx, 'replied', [])[1]
        self.assertEqual(submitted, dict(id='submitted', label='CV submitted', method='outcome', kind='submitted',
                                        record_id='P', action_id='A', asks='company', primary=False))
        self.assertEqual(approaches._actions(item, ctx, 'conversation', [])[0],
                         dict(id='handover', label='Hand over to Work', method='handover', record_id='P',
                              action_id='A', company_id='C', primary=True))
        ctx = self.context([dict(p, outcome_actions=[{'kind': 'replied'}])])
        self.assertEqual(approaches._actions(item, ctx, 'waiting', []), [
            dict(id='replied', label='They replied', method='outcome', kind='replied', record_id='P', action_id='A', primary=True),
            dict(id='next_step', label='Update next step', method='next_step', record_id='P', action_id='A', primary=False)])
