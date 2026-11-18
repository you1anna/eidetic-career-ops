"""The opportunity projection is the sole work/research admission and ordering policy."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approaches
import decision_intelligence
import relevance

TODAY = dt.date(2026, 10, 26)
REMIT = [dict(signal='Current remit: Improve release readiness and incident rate for existing software.',
              reviewed_at='2026-10-25', source_urls=['https://example.com/need'])]


def job(rid, engagement='contract', **changes):
    item = dict(id=rid, type='job', name='Engineering Manager', status='candidate', date='2026-10-25',
                engagement=engagement, location='London', url=f'https://example.com/{rid}',
                summary='Lead software engineering teams and improve release readiness.',
                relevance=dict(score=80, band='strong', lane='contract', qualification=dict(
                    eligible=True, domain='software', engagement=engagement, conflicts=[])))
    item.update(changes)
    return item


def project(records, **changes):
    import work_selection
    state = dict(records=records, links=[], outcomes=[], interactions=[])
    state.update(changes)
    if 'intelligence' not in state:
        state['intelligence'] = approaches.annotate(decision_intelligence.build(state, TODAY), state, TODAY)
    previous = copy.deepcopy(state)
    result = work_selection.build(state)
    assert state == previous, 'selection must not mutate the shared coherent snapshot'
    return result


class OpportunitySelectionTests(unittest.TestCase):
    def test_score_precedes_engagement_and_the_reason_is_a_score_factor(self):
        contract, permanent = job('C'), job('P', 'permanent')
        contract['relevance'].update(score=65, reasons=[{'weight':35, 'text':'Adjacent remit', 'fit':True}])
        permanent['relevance'].update(score=90, reasons=[{'weight':60, 'text':'Advertised team leadership matches recorded experience', 'fit':True}])
        selected = project([contract, permanent], intelligence={'actions':[], 'research':[]})
        self.assertEqual([r['record_id'] for r in selected['discovery']], ['P','C'])
        self.assertEqual(selected['discovery'][0]['reason'], permanent['relevance']['reasons'][0]['text'])
        contract['relevance']['score'] = 90
        self.assertEqual([r['record_id'] for r in project([permanent,contract])['discovery']], ['C','P'])

    def test_section_owner_uses_commitments_not_contract_lane_or_free_text(self):
        rows = [job('I','permanent',status='interview'), job('A',status='applied'),
                job('W',status='interview'), job('M',status='responded'), job('U',status='interview',
                    incoming={'current':True,'status':'interview','next_step':'Await the recruiter outcome.'})]
        actions=[]
        for r in rows:
            p=dict(status=r['status'],section='in_progress',next='Recorded step',since='2026-10-25')
            if r['id']=='W': p['next_step']={'kind':'waiting','text':'Await their answer'}
            if r['id']=='M': p['next_step']={'kind':'mine','text':'Send the agreed example'}
            actions.append(dict(id='action:'+r['id'],record_id=r['id'],progress=p))
        selected=project(rows,intelligence={'actions':actions,'research':[]})
        self.assertEqual({r['record_id'] for r in selected['act_now']},{'I','M'})
        self.assertEqual({r['record_id'] for r in selected['waiting']},{'W','A'})
        self.assertEqual([r['record_id'] for r in selected['in_progress']],['U'])
        self.assertEqual({r['record_id'] for r in selected['active']},{'I','A','W','M','U'})

    def test_confirmed_copy_does_not_reappear_beside_active_shortlisted_or_skipped_work(self):
        for status in ('interview','pursue','skip'):
            with self.subTest(status=status):
                old,new=job('old',status=status),job('new')
                old['relevance']['duplicate_of']='new'
                selected=project([old,new],intelligence={'actions':[],'research':[]})
                self.assertFalse(selected['discovery'])
                self.assertEqual([r['record_id'] for r in selected['other']],['new'])
                target={'interview':'active','pursue':'shortlisted','skip':'closed'}[status]
                self.assertEqual([r['record_id'] for r in selected[target]],['old'])

    def test_precontact_work_commitments_keep_their_weekly_place(self):
        rows = [dict(id='C',type='company',name='Example company'), dict(id='A',type='agency',name='Example agency')]
        actions = [dict(id='action:'+r['id'], record_id=r['id'], progress=dict(status='ready',
            next_step={'kind':kind,'text':'Recorded commitment'})) for r,kind in zip(rows,('mine','waiting'))]
        result = project(rows, intelligence={'actions':actions,'research':[]})
        self.assertEqual([ref['record_id'] for ref in result['act_now']], ['C'])
        self.assertEqual([ref['record_id'] for ref in result['waiting']], ['A'])
        self.assertEqual(result['act_now'][0]['reason'], 'Recorded commitment')
        self.assertEqual(result['waiting'][0]['reason'], 'Waiting for them: Recorded commitment')
        self.assertFalse(result['other'])

    def test_recorded_terminal_outcome_precedes_older_active_job_status(self):
        rows = [job('ended',status='applied'), job('held',status='applied'), job('incoming',status='interview',incoming={'current':True})]
        actions = [dict(id='action:ended',record_id='ended',last_outcome={'kind':'no_response'},
                        progress={'status':'no_response','terminal_recorded':True,'next':'You recorded no response.'}),
                   dict(id='action:held',record_id='held',progress={'status':'held','next':'New source uncertainty.'}),
                   dict(id='action:incoming',record_id='incoming',last_outcome={'kind':'no_response'},progress={'status':'interview'})]
        result = project(rows,intelligence={'actions':actions,'research':[]})
        self.assertEqual([ref['record_id'] for ref in result['closed']], ['ended'])
        self.assertEqual({ref['record_id'] for ref in result['active']}, {'held','incoming'})
        self.assertNotIn('ended', result['research_ids'])

    def test_a_saved_work_commitment_suppresses_confirmed_discovery_copies(self):
        old = dict(id='old',type='company',name='Example company',relevance={'duplicate_of':'new'})
        new = dict(id='new',type='company',name='Example company')
        actions = [dict(id='action:old',record_id='old',progress={'status':'ready',
                       'next_step':{'kind':'mine','text':'Send the agreed example'}}),
                   dict(id='action:new',record_id='new',work_evidence={'specific':True})]
        result = project([old,new], intelligence={'actions':actions,'research':[]})
        self.assertEqual([ref['record_id'] for ref in result['act_now']], ['old'])
        self.assertFalse(result['discovery'])
        self.assertEqual([ref['record_id'] for ref in result['other']], ['new'])

    def test_needs_checking_shares_qualification_pool_and_retains_actual_attempt_and_source(self):
        role=job('L','unknown',source_file='source:alert:linkedin',summary='',
                 source_context={'source':'alert:linkedin','url':'https://www.linkedin.com/jobs/view/123/',
                                 'at':'2026-10-24T08:00:00Z','basis':'email'})
        role['relevance'].update(band='possible',score=40,
                                fit={'status':'unconfirmed','gaps':['Essential payments experience is not established.']})
        result=project([role],intelligence={'actions':[],'research':[]},
                       research_attempts={'L':{'attempted_at':'2026-10-25T09:00:00Z','outcome':'unverified','reason':'Source refused the read'}})
        self.assertEqual(result['research_ids'],['L'])
        ref=result['needs_checking'][0]
        self.assertEqual(ref['record_id'],'L')
        self.assertEqual(ref['uncertainty'],role['relevance']['fit']['gaps'][0])
        self.assertEqual(ref['research_status']['status'],'unverified')
        self.assertEqual(ref['source'],{'label':'LinkedIn email','url':role['source_context']['url'],
                                     'at':'2026-10-24T08:00:00Z','basis':'email'})

    def test_needs_checking_shows_only_candidates_research_will_read(self):
        # Reed and Adzuna listings decide their adverts' availability and their pages refuse reads,
        # so research checks them only once shortlisted. They held Needs checking at "No research
        # attempt recorded" indefinitely, ahead of the LinkedIn alerts research was reading.
        feed = dict(status='open', basis='listing', at='2026-10-29T05:30:00Z', source='feed:reed')
        listed = [job(f'F{i}', availability=feed) for i in range(3)]
        for row in listed:
            row['relevance'].update(band='possible', score=62,
                                    fit={'status': 'adjacent', 'gaps': ['Essential experience not established.']})
        alerts = [job(f'L{i}', 'unknown', source_file='source:alert:linkedin', summary='') for i in range(4)]
        for row in alerts:
            row['relevance'].update(band='possible', score=40)
        attempts = {'L0': {'attempted_at': '2026-10-29T09:01:00Z', 'outcome': 'unverified'}}
        result = project([*listed, *alerts], intelligence={'actions': [], 'research': []}, research_attempts=attempts)
        self.assertEqual([r['record_id'] for r in result['needs_checking']], ['L1', 'L2', 'L3', 'L0'])
        self.assertTrue({r['record_id'] for r in result['needs_checking']} <= set(result['research_ids']))
        held = {r['record_id']: r for r in result['other'] if r['record_id'].startswith('F')}
        self.assertEqual({r['rule'] for r in held.values()}, {'other.unchecked_advert'})
        self.assertIn('shortlist', held['F0']['reason'])
        self.assertNotIn('F0', result['research_ids'])
        # Shortlisting is the route: research then checks it like any shortlisted role.
        listed[0]['status'] = 'pursue'
        result = project([*listed, *alerts], intelligence={'actions': [], 'research': []}, research_attempts=attempts)
        self.assertIn('F0', result['research_ids'])

    def test_undated_evidence_ranks_after_dated_evidence_at_equal_score(self):
        # An empty date sorted ahead of every dated item, reversing "newest evidence first".
        rows = [dict(id=rid, type='company', name=rid, relevance={'score': 30, 'band': 'possible'}) for rid in 'ABC']
        actions = [dict(id='action:' + rid, record_id=rid, work_evidence={'specific': True, 'date': date})
                   for rid, date in (('A', ''), ('B', '2026-10-01'), ('C', '2026-10-20'))]
        result = project(rows, intelligence={'actions': actions, 'research': []})
        self.assertEqual([r['record_id'] for r in result['discovery']], ['C', 'B', 'A'])

    def test_an_alert_is_dated_by_its_receipt_but_a_listing_never_dates_an_advert(self):
        # LinkedIn alerts carry no publication date; undated they would queue behind every dated
        # advert. A daily listing must not make an old advert look new, so it never dates one.
        alert = job('alert', 'unknown', date='', source_file='source:alert:linkedin', summary='',
                    source_context={'source': 'alert:linkedin', 'at': '2026-10-28T08:00:00Z', 'basis': 'email'})
        board = job('board', 'unknown', date='2026-10-20')
        listed = job('listed', 'unknown', date='',
                     source_context={'source': 'feed:reed', 'at': '2026-10-29T05:30:00Z', 'basis': 'listing'})
        for row in (alert, board, listed):
            row['relevance'].update(band='possible', score=40, fit={'gaps': ['Essential experience not established.']})
        result = project([listed, board, alert], intelligence={'actions': [], 'research': []})
        self.assertEqual([r['record_id'] for r in result['needs_checking']], ['alert', 'board', 'listed'])

    def test_weekly_projection_excludes_personal_commitments_and_is_read_only(self):
        person=dict(id='P',type='person',name='Synthetic colleague')
        action=dict(id='action:P',record_id='P',progress={'status':'waiting','section':'in_progress',
                    'next_step':{'kind':'mine','text':'Call a friend'}})
        result=project([person],intelligence={'actions':[action],'research':[]})
        for group in ('act_now','waiting','in_progress','needs_checking'):
            self.assertEqual(result[group],[])

    def test_needs_checking_bounds_weekly_references_but_retains_the_catalogue(self):
        rows = [job(str(n), source_file='source:alert:linkedin') for n in range(40)]
        for row in rows:
            row['relevance'].update(band='possible', fit={'gaps':['Essential experience not established.']})
        result = project(rows, intelligence={'actions':[], 'research':[]})
        self.assertEqual(len(result['needs_checking']), 6)
        self.assertEqual(len(result['other']), 40)
        self.assertEqual(len(result['research_ids']), 10)

    def test_recent_equal_fit_alert_precedes_older_contract_and_captured_advert_is_queued(self):
        old = job('old', date='2026-10-20', source_file='source:alert:linkedin')
        new = job('new', 'unknown', date='', source_context={'source':'alert:linkedin',
                  'basis':'email', 'at':'2026-10-29T08:54:00Z'})
        for row in (old, new):
            row['relevance'].update(band='possible', score=40, fit={'status':'unconfirmed','gaps':[]})
        result = project([old, new], intelligence={'actions':[], 'research':[]})
        self.assertEqual([r['record_id'] for r in result['needs_checking']], ['new', 'old'])
        self.assertEqual(result['research_ids'], ['new', 'old'])
        self.assertFalse(result['discovery'])

    def test_daily_brief_facts_cannot_refresh_old_or_undated_listings(self):
        old = job('old', date='2026-09-08', source_context={'basis':'listing', 'at':'2026-10-29T05:30:00Z'})
        undated = job('undated', date='', source_context={'basis':'listing', 'at':'2026-10-29T05:30:00Z'})
        alert = job('alert', 'unknown', date='', source_file='source:alert:linkedin',
                    source_context={'basis':'email', 'at':'2026-10-28T08:54:00Z'})
        for row in (old, undated, alert):
            row['relevance'].update(band='possible', score=40, fit={'status':'unconfirmed','gaps':[]})
        actions = [dict(record_id=r['id'], brief={'facts':[{'date':'2026-10-29','url':r['url']}]}) for r in (old, undated)]
        result = project([old, undated, alert], intelligence={'actions':actions, 'research':[]})
        self.assertEqual([r['record_id'] for r in result['needs_checking']], ['alert','old','undated'])

    def test_waiting_qualification_is_queued_not_ineligible(self):
        import research_scope
        rows = [job(str(i), source_file='source:alert:linkedin') for i in range(12)]
        for row in rows:
            row['relevance'].update(band='possible', score=40)
        state = dict(records=rows, opportunities=project(rows, intelligence={'actions':[], 'research':[]}))
        research_scope.attach(None, state, attempts={})
        queued = [r for r in rows if r['id'] not in state['opportunities']['research_ids']]
        self.assertEqual(len(queued), 2)
        self.assertTrue(all(r['research_disposition']['scope'] == 'outside_focus' for r in queued))

    def test_direct_essential_gaps_use_the_shared_checking_pool_without_advert_gates(self):
        gap = 'Essential payments-system experience is not established.'
        for kind in ('company','agency','opportunity'):
            with self.subTest(kind=kind):
                record = dict(id='D',type=kind,name='Recorded direct work',summary='Improve software release reliability.',
                              relevance={'score':1,'band':'low','fit':{'gaps':[gap]}})
                action = dict(id='action:D:',record_id='D',work_evidence={'specific':True},
                              brief={'why_now':'A current sourced remit.'})
                intel = {'actions':[action],'research':[],'eligible_advert_ids':[]}
                result = project([record], intelligence=intel)
                self.assertFalse(result['discovery'])
                self.assertEqual([r['record_id'] for r in result['other']], ['D'])
                self.assertEqual([r['record_id'] for r in result['needs_checking']], ['D'])
                self.assertEqual(result['needs_checking'][0]['uncertainty'], gap)
                self.assertEqual(result['research_ids'], ['D'])
                # A low advert-like score alone must not withdraw an established direct remit.
                record['relevance']['fit']['gaps'] = []
                result = project([record], intelligence=intel)
                self.assertEqual([r['record_id'] for r in result['discovery']], ['D'])
                self.assertFalse(result['needs_checking'])

    def test_direct_uncertainty_cannot_admit_generic_excluded_duplicate_or_closed_work(self):
        def direct(rid, **changes):
            record = dict(id=rid,type='company',name=rid,relevance={'score':100,'band':'possible',
                          'fit':{'gaps':['An essential requirement needs evidence.']}})
            record.update(changes)
            return record
        records = [direct('generic'),direct('excluded',exclusion={'reason':'Excluded'}),
                   direct('copy'),direct('closed',type='opportunity',stage='lost')]
        records[2]['relevance']['duplicate_of'] = 'generic'
        actions = [dict(id='action:'+r['id'],record_id=r['id'],work_evidence={'specific':True})
                   for r in records if r['id'] != 'generic']
        result = project(records, intelligence={'actions':actions,'research':[],'eligible_advert_ids':[]})
        self.assertFalse(result['discovery'])
        self.assertFalse(result['needs_checking'])
        self.assertFalse(result['research_ids'])

    def test_direct_essential_gaps_share_six_reserved_research_places(self):
        direct = [dict(id=f'C{i}',type='company',name=f'Sourced company {i}',relevance={
                       'score':100,'band':'possible','fit':{'gaps':['Essential experience needs evidence.']}}) for i in range(8)]
        jobs = [job(f'J{i:02}') for i in range(12)]
        actions = [dict(id='action:'+r['id'],record_id=r['id'],work_evidence={'specific':True}) for r in direct]
        result = project([*direct,*jobs], intelligence={'actions':actions,'research':[],
                          'eligible_advert_ids':[j['id'] for j in jobs]})
        self.assertEqual([r['record_id'] for r in result['needs_checking']], [f'C{i}' for i in range(6)])
        self.assertEqual(len(result['research_ids']), 10)
        self.assertEqual(result['research_ids'], ['C0', 'C1', 'J00', 'C2', 'C3', 'J01', 'C4', 'C5', 'J02', 'J03'])
        self.assertEqual(set(result['research_ids']) & {r['id'] for r in jobs}, {f'J{i:02}' for i in range(4)})
        self.assertFalse({r['record_id'] for r in result['discovery']} & {r['id'] for r in direct})

    def test_uncertain_promising_adverts_enter_research_without_becoming_recommendations(self):
        uncertain = job('uncertain', 'unknown', summary='', availability={'status': 'unconfirmed'},
                        source_file='source:alert:linkedin')
        uncertain['relevance'].update(band='possible', score=40)
        result = project([uncertain])
        self.assertEqual(result['discovery'], [])
        self.assertEqual([r['record_id'] for r in result['other']], ['uncertain'])
        self.assertEqual(result['research_ids'], ['uncertain'])

    def test_qualification_research_rotates_within_ten_slots_and_respects_exclusions(self):
        pending = [job(f'Q{i:02}', 'unknown', summary='') for i in range(15)]
        for r in pending:
            r['relevance'].update(band='possible', score=40)
            r['source_file'] = 'source:alert:linkedin'
        blocked = [job('excluded', exclusion={'reason': 'Excluded'}), job('duplicate'),
                   job('outside'), job('low'), job('old'), job('closed', status='closed')]
        blocked[1]['relevance']['duplicate_of'] = 'Q00'
        blocked[2]['relevance']['qualification']['eligible'] = False
        blocked[3]['relevance']['band'] = 'low'
        for r in blocked:
            r['source_file'] = 'source:alert:linkedin'
        state = dict(intelligence={'actions': [], 'research': [],
                     'eligible_advert_ids': [r['id'] for r in pending]},
                     research_attempts={f'Q{i:02}': {'attempted_at': '2026-10-27T09:00:00Z'} for i in range(10)})
        result = project([*pending, *blocked, job('active', status='interview')], **state)
        self.assertEqual(result['research_ids'], ['active', *[f'Q{i:02}' for i in range(10,15)],
                                               *[f'Q{i:02}' for i in range(5)]])
        self.assertEqual(result['discovery'], [])

    def test_alert_backlog_shares_ten_slots_without_displacing_all_existing_opportunities(self):
        pending = [job(f'Q{i:02}', source_file='source:alert:linkedin') for i in range(20)]
        for r in pending:
            r['relevance'].update(band='possible', score=40)
        records = [*[job(f'D{i:02}') for i in range(12)], *pending]
        result = project(records, intelligence={'actions': [], 'research': []})
        self.assertEqual(result['research_ids'], ['Q00','Q01','D00','Q02','Q03','D01','Q04','Q05','D02','D03'])
        # Six calls can cover three two-call reads: both qualification and existing work get one.
        self.assertEqual(result['research_ids'][:3], ['Q00', 'Q01', 'D00'])

    def test_lanes_use_terms_without_inventing_a_contract_and_existing_relevance_breaks_ties(self):
        records = [job('P', 'permanent'), job('U', 'unknown'), job('C'), job('C2')]
        records[3]['relevance'] = {**records[3]['relevance'], 'score': 90}
        result = project(records)
        self.assertEqual([(r['record_id'], r['lane']) for r in result['discovery']],
                         [('C2', 'contract'), ('C', 'contract'), ('P', 'permanent'), ('U', 'unstated')])
        for group in ('discovery', 'shortlisted', 'active', 'closed', 'other'):
            for row in result[group]:
                extra = {'worth_a_look'} if group == 'discovery' else {'attention'} if group in ('active', 'shortlisted') else set()
                self.assertEqual(set(row)-{'source','uncertainty'}, {'record_id', 'action_id', 'lane', 'rule', 'reason'} | extra)

    def test_only_strong_eligible_current_adverts_are_discovery_but_catalogue_survives(self):
        records = [job('good'), job('possible'), job('outside'), job('gone', source_lifecycle='closed'),
                   job('skip', status='skip'), job('excluded', exclusion={'reason': 'Excluded'})]
        records[1]['relevance']['band'] = 'possible'
        records[2]['relevance']['qualification']['eligible'] = False
        result = project(records)
        self.assertEqual([r['record_id'] for r in result['discovery']], ['good'])
        self.assertEqual({r['record_id'] for r in result['other']}, {'possible', 'outside', 'excluded'})
        self.assertEqual({r['record_id'] for r in result['closed']}, {'gone', 'skip'})

    def test_closed_adverts_do_not_keep_unanswered_applications_or_shortlists_in_weekly_work(self):
        result = project([job('applied', status='applied', source_lifecycle='closed'),
                          job('chosen', status='pursue', source_lifecycle='closed'),
                          job('closed', status='closed')])
        self.assertFalse(result['active'])
        self.assertFalse(result['shortlisted'])
        self.assertEqual({r['record_id'] for r in result['closed']}, {'applied', 'chosen', 'closed'})
        self.assertEqual(result['research_ids'], [])

    def test_closed_advert_overrides_elapsed_followup_without_changing_application_history(self):
        for closure in ({'source_lifecycle': 'closed'},
                        {'evidence_checks': {'vacancy': {'status': 'closed'}}},
                        {'availability': {'status': 'closed', 'basis': 'deadline'}}):
            with self.subTest(closure=closure):
                role = job('application', status='applied', **closure)
                state = dict(records=[role], status_since={'application': '2026-10-01T12:00:00Z'})
                intel = approaches.annotate(decision_intelligence.build(state, TODAY), state, TODAY)
                self.assertEqual(intel['actions'][0]['progress']['status'], 'follow_up')
                result = project([role], intelligence=intel)
                self.assertEqual([r['record_id'] for r in result['closed']], ['application'])
                for section in ('act_now', 'waiting', 'in_progress', 'discovery', 'research_ids'):
                    self.assertEqual(result[section], [], section)
                self.assertEqual(role['status'], 'applied')

    def test_absent_or_unverified_advert_is_not_confirmed_closed(self):
        for evidence in ({'source_lifecycle': 'absent'}, {'availability': {'status': 'unknown'}},
                         {'evidence_checks': {'vacancy': {'status': 'unverified'}}}):
            with self.subTest(evidence=evidence):
                result = project([job('pending', status='applied', **evidence)])
                self.assertEqual([r['record_id'] for r in result['waiting']], ['pending'])
                self.assertFalse(result['closed'])

    def test_closed_adverts_preserve_real_processes_replies_and_explicit_commitments(self):
        for closure in ({'source_lifecycle': 'closed'},
                        {'evidence_checks': {'vacancy': {'status': 'closed'}}}):
            for status, progress, section in (
                ('responded', {'status': 'replied'}, 'act_now'),
                ('interview', {'status': 'interview'}, 'act_now'),
                ('offer', {'status': 'offer'}, 'act_now'),
                ('applied', {'status': 'follow_up', 'reply_to_record': True}, 'act_now'),
                ('applied', {'status': 'follow_up', 'next_step': {'kind': 'mine', 'text': 'Send agreed example'}}, 'act_now'),
                ('applied', {'status': 'waiting', 'next_step': {'kind': 'waiting', 'text': 'Await arranged call'}}, 'waiting'),
            ):
                with self.subTest(closure=closure, status=status, progress=progress):
                    result = project([job('ongoing', status=status, **closure)], intelligence={
                        'actions': [dict(id='action:ongoing', record_id='ongoing', progress=progress)], 'research': []})
                    self.assertEqual([r['record_id'] for r in result[section]], ['ongoing'])
                    self.assertEqual(result['research_ids'], ['ongoing'])
                    self.assertFalse(result['closed'])

    def test_current_specific_company_remit_needs_no_named_person(self):
        company = dict(id='C', type='company', name='Example', research=[dict(
            signal='Building a Quality Engineering department to improve release readiness and incident rate.',
            reviewed_at='2026-10-24', source_urls=['https://example.com/remit'], verification='primary-read')],
            relevance={'score': 1, 'approach': {'trigger': 'hiring'}})
        result = project([company])
        self.assertEqual([(r['record_id'], r['lane']) for r in result['discovery']], [('C', 'direct')])
        self.assertIsNotNone(result['discovery'][0]['action_id'])

    def test_person_variants_collapse_and_adverts_do_not_create_extra_contract_leads(self):
        company = dict(id='C', type='company', name='Example', relevance={'approach': {'trigger': 'hiring'}})
        people = [dict(id=f'P{i}', type='person', name=f'Person {i}', role='CTO', relationship='warm') for i in range(3)]
        links = [dict(from_id=p['id'], to_id='C', relation='works_at') for p in people]
        links.append(dict(from_id='J', to_id='C', relation='hiring_for'))
        result = project([company, *people, job('J')], links=links)
        self.assertEqual([r['record_id'] for r in result['discovery']], ['J'])
        company['research'] = [dict(signal='Current remit: Improve release readiness and incident rate for existing software.',
                                   source_urls=['https://example.com/need'], reviewed_at='2026-10-25')]
        result = project([company, *people], links=links[:-1])
        self.assertEqual([r['record_id'] for r in result['discovery']], ['C'])

    def test_research_of_the_same_advert_does_not_create_a_direct_company_lead(self):
        advert=job('J','permanent')
        company=dict(id='C',type='company',name='Example',research=[dict(
            signal='Building a Quality Engineering department to improve release readiness and incident rate.',
            reviewed_at='2026-10-25',source_urls=[advert['url']])])
        result=project([company,advert],links=[dict(from_id='J',to_id='C',relation='hiring_for')])
        self.assertEqual([r['record_id'] for r in result['discovery']],['J'])

    def test_nested_vacancy_source_does_not_create_a_second_company_recommendation(self):
        advert = job('J', 'permanent')
        source = advert.pop('url')
        advert['advert'] = {'url': source, 'text': advert['summary'], 'observed_at': '2026-10-25'}
        company = dict(id='C', type='company', name='Example', research=[dict(
            signal='Current remit: Improve release readiness and incident rate for existing software.',
            reviewed_at='2026-10-25', source_urls=[source])])
        result = project([company, advert], links=[dict(from_id='J', to_id='C', relation='hiring_for')])
        self.assertEqual([r['record_id'] for r in result['discovery']], ['J'])

    def test_linked_vacancy_research_at_another_url_stays_vacancy_owned(self):
        advert = job('J', 'permanent')
        advert['research'] = [dict(signal='Current remit: Improve release readiness and incident rate.',
                                  reviewed_at='2026-10-25',
                                  source_urls=['https://employer.example/careers/current-role'])]
        company = dict(id='C', type='company', name='Example')
        result = project([company, advert], links=[dict(from_id='J', to_id='C', relation='hiring_for')])
        self.assertEqual([r['record_id'] for r in result['discovery']], ['J'])
        self.assertEqual([r['record_id'] for r in result['other']], ['C'])

    def test_record_maintenance_date_cannot_refresh_qualifying_remit_evidence(self):
        def company(rid,date,read):
            return dict(id=rid,type='company',name=rid,date=date,research=[dict(
                signal='Current remit: Improve release readiness and incident rate for existing software.',
                reviewed_at=read,source_urls=['https://example.com/'+rid])])
        result=project([company('A','2026-10-26','2026-10-01'),company('B','2026-10-20','2026-10-20')])
        self.assertEqual([r['record_id'] for r in result['discovery']],['B','A'])

    def test_generic_titles_funding_partner_or_award_are_context_only(self):
        for signal in ('Head of Engineering', 'Raised funding to grow the business'):
            with self.subTest(signal=signal):
                company = dict(id='C', type='company', name='Example', relevance={'funded': True}, research=[dict(
                    signal=signal, reviewed_at='2026-10-25', source_urls=['https://example.com/news'])])
                self.assertEqual(project([company])['discovery'], [])
        company = dict(id='C', type='company', name='Example', relevance={'funded': True},
                       commercial_signals=[dict(signal_type='contract_award', eligible=True,
                           title='Software integration and release readiness', award_date='2026-10-25',
                           url='https://example.com/award')])
        self.assertEqual(project([company])['discovery'], [])

    def test_activity_and_hiring_cannot_promote_company_or_recorded_lead_into_best(self):
        signals = (
            'Raised funding to build AI integrations and improve release readiness.',
            'Our product builds AI integrations and improves release readiness for customers.',
            'We provide engineering leadership and deliver data integrations for customers.',
            'We are hiring a Head of Engineering to improve release readiness.',
        )
        for kind in ('company', 'opportunity'):
            for signal in signals:
                with self.subTest(kind=kind, signal=signal):
                    record = dict(id='C', type=kind, name='Example', summary=signal, stage='lead',
                                  relevance={'score': 1, 'band': 'low', 'funded': True}, research=[dict(
                                      signal=signal, reviewed_at='2026-10-25',
                                      source_urls=['https://example.com/news'])])
                    selected = project([record])
                    self.assertEqual(selected['discovery'], [])
                    self.assertEqual([r['record_id'] for r in selected['other']], ['C'])
                    self.assertIn('requested work', selected['other'][0]['uncertainty'])

    def test_hiring_quote_keeps_the_vacancy_without_a_second_company_recommendation(self):
        company = dict(id='C', type='company', name='Example', research=[dict(
            signal='We are hiring a Head of Engineering to improve release readiness.',
            reviewed_at='2026-10-25', source_urls=['https://example.com/news'])])
        advert = job('J', 'permanent', availability={'status': 'open'})
        result = project([company, advert], links=[dict(from_id='J', to_id='C', relation='hiring_for')])
        self.assertEqual([r['record_id'] for r in result['discovery']], ['J'])
        self.assertEqual([r['record_id'] for r in result['other']], ['C'])

    def test_warm_route_does_not_hide_the_missing_company_work_evidence(self):
        company = dict(id='C', type='company', name='Example', research=[dict(
            signal='We are hiring a Head of Engineering to improve release readiness.',
            reviewed_at='2026-10-25', source_urls=['https://example.com/news'])])
        person = dict(id='P', type='person', name='Known introducer', relationship='warm')
        result = project([company, person, job('J', availability={'status': 'open'})], links=[
            dict(from_id='P', to_id='C', relation='introducer'),
            dict(from_id='J', to_id='C', relation='hiring_for')])
        self.assertEqual(result['other'][0]['record_id'], 'C')
        self.assertEqual(result['other'][0]['uncertainty'],
                         'No current source establishes requested work or a specific delivery remit; company activity and hiring alone are context.')

    def test_closed_vacancy_evidence_cannot_reappear_as_a_company_lead(self):
        company = dict(id='C', type='company', name='Example', research=[dict(
            signal='Current remit: Improve release readiness and incident rate for existing software.',
            reviewed_at='2026-10-25', source_urls=['https://example.com/J'])])
        result = project([company, job('J', status='closed')],
                         links=[dict(from_id='J', to_id='C', relation='hiring_for')])
        self.assertEqual(result['discovery'], [])
        self.assertEqual([r['record_id'] for r in result['closed']], ['J'])
        self.assertEqual([r['record_id'] for r in result['other']], ['C'])

    def test_context_only_company_keeps_its_historical_hold_with_person_routes(self):
        company = dict(id='C', type='company', name='Example', research=[dict(
            signal='Our product builds AI integrations and improves release readiness for customers.',
            reviewed_at='2026-10-25', source_urls=['https://example.com/product'])])
        person = dict(id='P', type='person', name='Known person', role='CTO', relationship='warm')
        result = project([company, person], links=[dict(from_id='P', to_id='C', relation='works_at')],
                         decisions=[dict(id=1, kind='draft', record_id='C', status='done', answer='hold')])
        self.assertEqual([(r['record_id'], r['action_id'], r['rule']) for r in result['closed']],
                         [('C', 'action:C:', 'closed.recorded')])
        self.assertEqual(result['discovery'], [])
        self.assertEqual(result['other'], [])

    def test_advert_uncertainty_uses_its_decisive_briefing_before_generic_advice(self):
        advert = job('J', availability={'status': 'open'},
                     briefing={'decisive_unknowns': ['Confirm recent production LLM delivery depth.']})
        action = dict(id='action:J:', record_id='J', stage='ready', brief={'unknowns': [
            'Find a specific existing engineering remit or problem before proposing help']})
        result = project([advert], intelligence={'actions': [action], 'research': []})
        self.assertEqual(result['discovery'][0]['uncertainty'], 'Confirm recent production LLM delivery depth.')

    def test_ambiguous_advert_experience_is_an_unknown_not_an_essential_gate(self):
        advert = job('J', availability={'status': 'open'})
        advert['relevance']['fit'] = {'status': 'supported', 'gaps': [], 'optional_unknowns': [
            'General competency requirement not established: Communication skills',
            'Unspecified requirement not established: Experience with modern JavaScript and cloud platforms']}
        action = dict(id='action:J:', record_id='J', stage='ready', brief={'unknowns': [
            'Confirm that this is a software-engineering remit']})
        result = project([advert], intelligence={'actions': [action], 'research': []})
        self.assertEqual(result['discovery'][0]['uncertainty'],
                         'Unspecified requirement not established: Experience with modern JavaScript and cloud platforms')

    def test_recorded_opportunities_need_a_current_remit_to_be_recommended(self):
        # Contract change, 29 October: a recorded opportunity no longer becomes a Best opportunity
        # on its type alone. Fifteen September pipeline rows with no current need filled Discovery.
        # A promising one is researched for a remit; the rest stay in Other records. Recorded
        # company progress remains active work.
        import work_selection
        records = [dict(id='R', type='opportunity', name='Lead with a sourced remit', stage='lead',
                        summary='Fix release quality.', research=REMIT),
                   dict(id='K', type='opportunity', name='Lead through someone you know', stage='lead',
                        relevance={'score': 30, 'band': 'possible'}),
                   dict(id='N', type='opportunity', name='Lead with nothing current', stage='lead',
                        relevance={'score': 0, 'band': 'low'}),
                   dict(id='C', type='company', name='Example')]
        history = [dict(id=1, record_id='C', kind='conversation', at='2026-10-20',
                        snapshot={'action_id': 'action:C:', 'company_id': 'C', 'route': 'direct'})]
        result = project(records, outcomes=history)
        rules = {ref['record_id']: ref['rule'] for group in ('discovery', 'active', 'other') for ref in result[group]}
        self.assertEqual(rules, {'R': 'discovery.lead', 'K': 'qualify.lead', 'N': 'other.lead', 'C': 'active.process'})
        self.assertEqual([r['record_id'] for r in result['needs_checking']], ['K'])
        self.assertEqual(set(result['research_ids']), {'C', 'R', 'K'})
        self.assertEqual(next(r for r in result['other'] if r['record_id'] == 'N')['reason'],
                         work_selection.RULES['other.lead'].text)

    def test_pure_selection_100_and_1000_is_stable_with_ten_discovery_research_targets(self):
        for count in (100, 1000):
            records = [job(f'J{i:04}') for i in range(count)]
            result = project(records, intelligence={'actions': [], 'research': []})
            self.assertEqual(len(result['discovery']), count)
            self.assertEqual(result['research_ids'], [f'J{i:04}' for i in range(10)])
            self.assertEqual(result, project(list(reversed(records)), intelligence={'actions': [], 'research': []}))


    def test_worth_a_look_holds_open_adverts_and_leads_while_jobs_discovery_keeps_everything(self):
        opened = dict(status='open', basis='listing', at='2026-10-26T05:30:00Z', source='feed:adzuna')
        records = [job('listed', availability=opened),
                   job('stale', availability=dict(opened, status='unconfirmed')),
                   job('unknown', availability=dict(status='unconfirmed', basis=None, at=None, source=None)),
                   job('missing'),
                   dict(id='O', type='opportunity', name='Recorded lead', summary='Fix release quality.',
                        next_action='Qualify the remit.', research=REMIT)]
        result = project(records)
        flags = {r['record_id']: r['worth_a_look'] for r in result['discovery']}
        self.assertEqual(flags, {'listed': True, 'stale': False, 'unknown': False, 'missing': False, 'O': True})

    def test_discovery_research_skips_feed_only_adverts_whose_listing_decides_availability(self):
        def avail(status, source):
            return dict(status=status, basis='listing' if source else None, at=None, source=source)
        records = [job('feed-open', availability=avail('open', 'feed:adzuna')),
                   job('feed-stale', availability=avail('unconfirmed', 'feed:reed')),
                   job('board-open', availability=avail('open', 'board:greenhouse')),
                   job('board-stale', availability=avail('unconfirmed', 'board:ashby')),
                   job('unlisted', availability=avail('unconfirmed', None)),
                   job('research-open', availability=dict(status='open', basis='research', at=None, source=None)),
                   job('chosen', status='pursue', availability=avail('open', 'feed:adzuna')),
                   dict(id='O', type='opportunity', name='Recorded lead', summary='Fix release quality.',
                        next_action='Qualify the remit.', research=REMIT)]
        result = project(records)
        self.assertEqual(set(result['research_ids']), {'chosen', 'board-open', 'unlisted', 'research-open', 'O'})
        self.assertEqual(len(result['discovery']), 7)

    def test_feed_adverts_do_not_use_up_the_ten_discovery_research_slots(self):
        feed = dict(status='open', basis='listing', at=None, source='feed:adzuna')
        records = [job(f'F{i:02}', availability=feed) for i in range(12)]
        records += [job(f'P{i:02}', 'permanent') for i in range(12)]
        result = project(records, intelligence={'actions': [], 'research': []})
        self.assertEqual(result['research_ids'], [f'P{i:02}' for i in range(10)])

class PlacementTraceTests(unittest.TestCase):
    """Every reference names the declared rule that placed it, so a placement can be traced."""

    def mixed(self):
        rows = [job('strong'), job('gone', source_lifecycle='closed'), job('copy'), job('shut', status='closed'),
                job('chosen', status='pursue'), job('talking', status='interview'), job('applied', status='applied'),
                job('outside'), job('excluded', exclusion={'reason': 'Excluded'}),
                job('checked', evidence_checks={'vacancy': {'status': 'closed'}}),
                job('weak'), job('alert', 'unknown', source_file='source:alert:linkedin'),
                dict(id='context', type='company', name='Context only'),
                dict(id='lead', type='company', name='Sourced lead', relevance={'score': 1, 'band': 'low'}),
                dict(id='gaps', type='company', name='Sourced lead with gaps',
                     relevance={'score': 1, 'band': 'low', 'fit': {'gaps': ['Essential experience needs evidence.']}})]
        rows[2]['relevance']['duplicate_of'] = 'strong'
        rows[7]['relevance']['qualification']['eligible'] = False
        rows[10]['relevance']['band'] = 'low'
        rows[11]['relevance'].update(band='possible', score=40)
        actions = [dict(id='action:waiting', record_id='applied', progress={'status': 'applied',
                        'next_step': {'kind': 'waiting', 'text': 'Await their answer'}}),
                   *(dict(id='action:' + rid, record_id=rid, work_evidence={'specific': True}) for rid in ('lead', 'gaps'))]
        return project(rows, intelligence={'actions': actions, 'research': []})

    def test_each_reference_names_a_declared_rule_of_its_own_group(self):
        import work_selection
        result = self.mixed()
        placed = {}
        for group in ('discovery', 'shortlisted', 'active', 'closed', 'other'):
            for ref in result[group]:
                self.assertEqual(work_selection.RULES[ref['rule']].group, group, ref)
                placed[ref['record_id']] = group, ref['rule']
        self.assertEqual(placed, {
            'strong': ('discovery', 'discovery.advert'), 'gone': ('closed', 'closed.source'),
            'copy': ('other', 'other.duplicate'), 'shut': ('closed', 'closed.recorded'),
            'chosen': ('shortlisted', 'shortlisted.job'), 'talking': ('active', 'active.process'),
            'applied': ('active', 'active.process'), 'outside': ('other', 'other.unsuitable'),
            'excluded': ('other', 'other.excluded'), 'checked': ('closed', 'closed.check'),
            'weak': ('other', 'other.advert'), 'alert': ('other', 'qualify.advert'),
            'context': ('other', 'other.context'), 'lead': ('discovery', 'discovery.lead'),
            'gaps': ('other', 'qualify.lead')})
        for ref in result['needs_checking']:
            self.assertIn(ref['rule'], work_selection.QUALIFY)

    def test_weekly_references_name_the_attention_cause_of_their_section(self):
        import work_selection
        result = self.mixed()
        causes = {}
        for section in work_selection.SECTIONS:
            for ref in result[section]:
                self.assertEqual(work_selection.ATTENTION[ref['attention']].section, section, ref)
                causes[ref['record_id']] = section, ref['attention']
        self.assertEqual(causes, {'chosen': ('act_now', 'shortlisted'), 'talking': ('act_now', 'stage'),
                                  'applied': ('waiting', 'their_step')})

    def test_architecture_lists_the_rules_in_checking_order(self):
        import re
        import work_selection
        doc = (Path(__file__).resolve().parent.parent / 'docs/architecture.md').read_text()
        table = doc.split('### Placement rules', 1)[1].split('\n\n|', 1)[1].split('\n\n', 1)[0]
        codes = [code for row in table.splitlines()[2:] for code in re.findall(r'`([a-z_.]+)`', row.split('|')[2])]
        self.assertEqual(codes, list(work_selection.RULES))

    def test_declared_rules_explain_themselves(self):
        import work_selection
        groups = {'discovery', 'shortlisted', 'active', 'closed', 'other'}
        for code, rule in work_selection.RULES.items():
            self.assertIn(rule.group, groups, code)
            self.assertTrue(rule.text.strip(), code)
        self.assertTrue(set(work_selection.QUALIFY) <= set(work_selection.RULES))
        for code, attention in work_selection.ATTENTION.items():
            self.assertIn(attention.section, work_selection.SECTIONS, code)
            self.assertTrue(attention.text.strip(), code)

    def test_week_page_holds_open_discovery_and_the_sections_in_page_order(self):
        import work_selection
        ref = lambda rid, **extra: dict(record_id=rid, **extra)
        page = work_selection.week_page(dict(
            discovery=[ref('open', worth_a_look=True), ref('stale', worth_a_look=False), ref('lead', worth_a_look=True)],
            act_now=[ref('A')], waiting=[ref('W')], in_progress=[ref('I')], needs_checking=[ref('N')], other=[ref('O')]))
        self.assertEqual({key: [r['record_id'] for r in refs] for key, refs in page.items()},
                         {'act_now': ['A'], 'best': ['open', 'lead'], 'needs_checking': ['N'],
                          'in_progress': ['I'], 'waiting': ['W']})
        self.assertEqual(list(page), ['act_now', 'best', 'needs_checking', 'in_progress', 'waiting'])

    def test_the_page_renders_the_week_sections_in_the_same_order(self):
        import re
        import work_selection
        script = (Path(__file__).resolve().parent / 'js/week.js').read_text()
        titles = re.findall(r"\bgroup\('[\w-]+', '([^']+)'", script)
        self.assertEqual(titles, [title for _, title in work_selection.WEEK_PAGE])


class RetiredPersonalWorkflowTests(unittest.TestCase):
    def test_elapsed_time_cannot_create_a_followup_or_no_response(self):
        person = dict(id='P', type='person', name='Known person', relationship='warm')
        for kind in ('sent', 'followed_up'):
            state = dict(records=[person], links=[], outcomes=[dict(id=1, record_id='P', kind=kind,
                at='2025-01-01', snapshot={'action_id': 'action:P:'})])
            intel = approaches.annotate(decision_intelligence.build(state, TODAY), state, TODAY)
            self.assertEqual(intel['actions'][0]['progress']['status'], 'waiting')
            self.assertNotIn('suggested from', intel['actions'][0]['progress']['next'])
            selected=project([person],intelligence=intel)
            self.assertFalse(selected['act_now'])
            self.assertFalse(selected['waiting'])
            self.assertNotIn('attention',intel)
            self.assertFalse(intel['followups'])

    def test_drafts_and_approval_are_historical_and_contact_needs_neither(self):
        person = dict(id='P', type='person', name='Known person', relationship='warm')
        state = dict(records=[person], links=[], outcomes=[], drafts=[dict(record_id='P', version_id=4,
            purpose='approval', text='Historical outgoing text')], decisions=[dict(id=1, kind='draft', record_id='P',
            status='done', answer='approve', draft_version_id=4, draft_binding='exact')])
        intel = approaches.annotate(decision_intelligence.build(state, TODAY), state, TODAY)
        progress = intel['actions'][0]['progress']
        self.assertEqual(progress['status'], 'ready')
        self.assertIn('sent', [a['id'] for a in progress['actions']])
        self.assertFalse({a['method'] for a in progress['actions']} & {'copy', 'decision', 'week'})
        self.assertIsNone(progress['message'])
        self.assertTrue(all(not item['actions'] for item in intel['week']['items']))


class StateProjectionIntegrityTests(unittest.TestCase):
    def test_summary_omits_outgoing_wording_and_timeline_retains_all_versions(self):
        import json
        import tempfile
        import db
        import records
        import record_views
        import workspace_state
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(Path(directory) / 'synthetic.db')
            try:
                db.migrate(conn)
                rid = records.create(conn, dict(type='person', name='Synthetic colleague', relationship='warm'))['record']['id']
                for text in ('OLD_OUTGOING_WORDING', 'CURRENT_OUTGOING_WORDING'):
                    records.draft(conn, dict(record_id=rid, revision=records.get(conn,rid)['revision'], text=text))
                state = workspace_state.state(conn)
                aid = next(a['id'] for a in state['intelligence']['actions'] if a['record_id'] == rid)
                records.outcome(conn, rid, dict(revision=records.get(conn,rid)['revision'], kind='sent', action_id=aid,
                                                sent_message='ACTUAL_OUTGOING_WORDING'))
                summary = workspace_state.state(conn, compact=True)
                encoded = json.dumps(summary)
                for text in ('OLD_OUTGOING_WORDING', 'CURRENT_OUTGOING_WORDING', 'ACTUAL_OUTGOING_WORDING'):
                    self.assertNotIn(text, encoded)
                self.assertNotIn('templates', summary)
                self.assertNotIn('drafts', summary)
                self.assertNotIn('research_attempts', summary)
                timeline = record_views.timeline(conn, rid)
                self.assertEqual([d['text'] for d in timeline['drafts']], ['CURRENT_OUTGOING_WORDING','OLD_OUTGOING_WORDING'])
                self.assertEqual(timeline['outcomes'][0]['snapshot']['message'], 'ACTUAL_OUTGOING_WORDING')
                self.assertEqual(workspace_state.state(conn)['drafts'][0]['text'], 'CURRENT_OUTGOING_WORDING')
                # Retained wording grows in history, never in the page-load projection.
                with conn:
                    conn.executemany('INSERT INTO draft_versions(record_id,title,text,created_at,actor) VALUES(?,?,?,?,?)',
                        [(rid, 'Retained version', f'ARCHIVED_WORDING_{i}' + 'x' * 8000,
                          '2020-01-01T00:00:00Z', 'fixture') for i in range(1000)])
                expanded = workspace_state.state(conn, compact=True)
                # `loaded` is the build time and may cross a second boundary between the two builds.
                self.assertEqual({k: v for k, v in expanded.items() if k != 'loaded'},
                                 {k: v for k, v in summary.items() if k != 'loaded'})
                self.assertEqual(len(record_views.timeline(conn, rid)['drafts']), 1002)
            finally:
                conn.close()

    def test_sql_reads_have_fixed_query_count_and_do_not_write_at_100_and_1000_records(self):
        import tempfile
        from unittest.mock import patch
        import db
        import records
        import workspace_state
        counts = []
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(Path(directory) / 'synthetic.db')
            try:
                db.migrate(conn)
                for count in (100, 1000):
                    start = 0 if count == 100 else 100
                    for i in range(start,count):
                        records.create(conn, dict(type='job', name=f'Engineering Manager {i}', engagement='contract',
                            location='London', pay='£750/day', date='2026-10-25', url=f'https://example.com/{i}',
                            summary='Lead software engineering teams and improve delivery quality.'))
                    queries = []
                    before = conn.total_changes
                    conn.set_trace_callback(queries.append)
                    try:
                        with patch('approaches.london_today', return_value=TODAY):
                            result = workspace_state.state(conn, compact=True, receipt_history=False)
                    finally:
                        conn.set_trace_callback(None)
                    counts.append(len(queries))
                    self.assertEqual(conn.total_changes, before)
                    self.assertFalse(any(q.lstrip().split(' ',1)[0].upper() in ('INSERT','UPDATE','DELETE','REPLACE') for q in queries))
                    self.assertEqual(len(result['opportunities']['discovery']),10)
                    self.assertEqual(len(result['opportunities']['other']),count-10)
                self.assertEqual(counts[0], counts[1], 'State must batch SQL independently of record count')
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
