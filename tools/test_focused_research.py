import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workspace'))
import db
import records
import research_worker as worker
import research_cache as memo

class FocusTests(unittest.TestCase):
    def test_chosen_and_qualification_lanes_share_a_six_call_pass_and_rotate(self):
        ids = [f'Q{i}' for i in range(10)] + [f'C{i}' for i in range(6)]
        jobs = [dict(id=i, type='job', contacts=[]) for i in ids]
        state = dict(opportunities=dict(research_ids=ids,
            active=[dict(record_id=f'C{i}') for i in range(3)],
            shortlisted=[dict(record_id=f'C{i}') for i in range(3,6)]),
            research_attempts={f'C{i}': dict(attempted_at='2026-11-05T09:00:00Z') for i in range(6)})
        selected = worker.focus_work(self.conn, jobs, state)
        self.assertEqual([j['id'] for j in selected[:6]], ['C0','Q0','C1','Q1','C2','Q2'])
        self.assertEqual({j['id'] for j in selected}, set(ids))
        # Three two-call targets also include both lanes. The next pass rotates both.
        for rid in ('C0','Q0','C1'):
            state['research_attempts'][rid] = dict(attempted_at='2026-11-06T09:00:00Z')
        self.assertEqual([j['id'] for j in worker.focus_work(self.conn,jobs,state)[:3]], ['C2','Q1','C3'])

    def test_allowance_stop_retains_latest_attempt_but_not_a_rotation_turn(self):
        import research_scope
        for run, items in (
            ('earlier', [dict(id='A',attempted_at='2026-11-01T09:00:00Z',outcome='unverified'),
                         dict(id='B',attempted_at='2026-11-02T09:00:00Z',outcome='failed')]),
            ('latest', [dict(id='A',attempted_at='2026-11-06T09:00:00Z',outcome='usage_limited'),
                        dict(id='C',attempted_at='2026-11-06T09:00:00Z',outcome='access_blocked')])):
            self.conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                ('research:'+run,'2026-11-06','partial',json.dumps(dict(items=items))))
        attempts = research_scope.latest_attempts(self.conn)
        self.assertEqual((attempts['A']['attempted_at'],attempts['A']['outcome']),
                         ('2026-11-06T09:00:00Z','usage_limited'))
        self.assertEqual(attempts['A']['rotation_at'], '2026-11-01T09:00:00Z')
        self.assertEqual(attempts['C']['rotation_at'], '')
        jobs = [dict(id=i,type='job',contacts=[]) for i in ('B','A','C')]
        state = dict(opportunities=dict(research_ids=['B','A','C'],active=[]),research_attempts=attempts)
        self.assertEqual([j['id'] for j in worker.focus_work(self.conn,jobs,state)], ['C','A','B'])

    def test_two_call_targets_use_six_call_cap_without_starving_either_lane(self):
        jobs = [dict(id=i,type='job',name='Engineering Manager',organisation='Example Labs',
                     url='https://example.org/jobs/'+i) for i in ('Q0','Q1','Q2','C0','C1','C2')]
        state = dict(records=jobs,links=[],opportunities=dict(research_ids=[j['id'] for j in jobs],
                     active=[dict(record_id=i) for i in ('C0','C1','C2')]),
                     research_attempts={i:dict(attempted_at='2026-11-05T09:00:00Z') for i in ('C0','C1','C2')})
        completed=[]
        def maintain(conn,run_id,data,limits,job,cache,receipt,item,**kwargs):
            for _ in range(2):
                worker.model.call(conn,run_id,None,limits,worker.sources.stamp(),{},worker.model.DISCOVERY,search=True)
            completed.append(job['id']);item['outcome']='unverified'
        with patch.object(worker.records,'state_context',return_value=state), \
             patch.object(worker,'maintain_brief',side_effect=maintain), \
             patch.object(worker.model,'exchange',return_value=dict(ok=True,result={'urls':[]},usage={})) as exchange:
            receipt=worker.run(self.path,self.path.parent,None,dict(enabled=True,briefs=True,max_calls=6))
        self.assertEqual(completed,['C0','Q0','C1'])
        self.assertEqual((exchange.call_count,receipt['usage']['calls'],receipt['checked']),(6,6,3))
        self.assertEqual(receipt['waiting_ids'],['Q1','C2','Q2'])
        self.assertEqual(receipt['items'][-1]['outcome'],'usage_limited')

    def test_worker_keeps_displayed_priority_within_attempt_rotation(self):
        jobs = [dict(id=i, type='job', contacts=[]) for i in ('A', 'Z', 'M')]
        state = dict(opportunities=dict(research_ids=['Z', 'A', 'M'], active=[], shortlisted=[]),
                     research_attempts={'M': {'attempted_at': '2026-10-28T09:00:00Z'}})
        self.assertEqual([j['id'] for j in worker.focus_work(self.conn, jobs, state)], ['Z', 'A', 'M'])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.db';self.conn=db.connect(self.path);db.migrate(self.conn);self.addCleanup(self.conn.close)

    def test_only_shared_opportunities_are_maintained_without_personal_targets(self):
        jobs=[dict(id=i,type='job',status='candidate',contacts=[]) for i in ('J1','J2','J3')]
        state=dict(records=jobs,links=[],opportunities=dict(research_ids=['J2'],active=[],shortlisted=[]))
        self.assertEqual([j['id'] for j in worker.focus_work(self.conn,jobs,state)],['J2'])
        state['opportunities']['research_ids']=[]
        self.assertEqual(worker.focus_work(self.conn,jobs,state),[])

    def test_deferred_focus_is_not_reintroduced_by_research(self):
        state=dict(records=[dict(id='P1',type='person')],links=[],intelligence={'week':{'focus_ids':['a1'],'items':[dict(action_id='a1',record_id='P1',state='deferred')]}})
        with patch.object(worker.records,'state_context',return_value=state):self.assertEqual(worker.focus_work(self.conn,[]),[])

    def test_source_cooldown_never_changes_failed_read_time(self):
        at='2026-10-18T10:00:00Z';url='https://example.com/job'
        source=dict(url=url,checked_at=at,ok=False,text='',reason='robots_disallowed')
        with patch.object(memo.sources,'read',return_value=source) as read:
            first=memo.read_source(self.conn,url,{},at)
            second=memo.read_source(self.conn,url,{},'2026-10-18T19:00:00Z')
            self.assertEqual(read.call_count,1);self.assertEqual(first,second)
            memo.read_source(self.conn,url,{},'2026-10-19T10:01:00Z')
            self.assertEqual(read.call_count,2)

    def test_reuse_requires_same_text_context_and_policy_but_new_read_time_is_allowed(self):
        prompt=dict(job={'revision':1},sources=[dict(text='Exact current source',checked_at='2026-10-18T10:00:00Z')])
        with patch.object(memo.model,'call',return_value={'urls':[]}) as call:
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            prompt['sources'][0]['checked_at']='2026-10-18T19:00:00Z'
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            self.assertEqual(call.call_count,1)
            prompt['sources'][0]['text']='Changed source'
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            prompt['job']['revision']=2
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            self.assertEqual(call.call_count,3)

    def test_person_check_cannot_invent_current_employer_and_preserves_draft(self):
        p=records.create(self.conn,dict(type='person',name='Alex Example',organisation='Example Labs',url='https://example.com/team'),actor='agent')['record']
        records.draft(self.conn,dict(record_id=p['id'],revision=p['revision'],purpose='message',text='My note'),actor='owner')
        p=records.get(self.conn,p['id']);p['contacts']=[dict(p)]
        claim=dict(source=0,status='confirmed',quote='Alex Example currently works at Example Labs.',basis='Current team')
        result={'contacts':[dict(person_id=p['id'],claim=claim)],'facts':[],'gaps':[]}
        source=dict(url=p['url'],ok=True,text=claim['quote'],checked_at=worker.sources.stamp(),reason='read')
        with patch.object(memo,'read_source',return_value=source),patch.object(memo,'result',return_value=result):
            finding,evidence,note=worker.research_person(self.conn,'r',{},p,{})
        self.assertEqual(finding['freshness_checks'][0]['status'],'confirmed');self.assertIsNone(note)
        p['context_snapshot']=worker.brief_research.snapshot(self.conn,dict(records.get(self.conn,p['id']),contacts=p.get('contacts',[])))
        worker.apply_finding(self.conn,p,finding,'research:vps:person',note)
        self.assertEqual(self.conn.execute('SELECT text FROM drafts WHERE record_id=?',(p['id'],)).fetchone()[0],'My note')

    def test_self_checks_reject_another_person_or_vacancy(self):
        from research_intake import validate_signal_targets
        p=records.create(self.conn,dict(type='person',name='Test Person'),actor='agent')['record']
        for check in [dict(scope='vacancy'),dict(scope='contact_route',person_id='other')]:
            with self.assertRaises(ValueError):validate_signal_targets(self.conn,p['id'],{'freshness_checks':[check]})

    def test_revision_rebase_preserves_cache_but_a_human_edit_invalidates_it(self):
        prompt=dict(job={'revision':1},sources=[dict(text='Same read',checked_at='2026-10-18T10:00:00Z')])
        with patch.object(memo.model,'call',return_value={'urls':[]}) as call:
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            memo.rebase_revision(self.conn,'J1',1,2)
            prompt['job']['revision']=2
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            self.assertEqual(call.call_count,1)
            prompt['job']['revision']=3
            memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            self.assertEqual(call.call_count,2)
            with patch.object(memo,'policy',return_value='changed-template'):
                memo.result(self.conn,'r',{},'J1','finding',prompt,worker.model.DISCOVERY)
            self.assertEqual(call.call_count,3)

    def test_pause_prevents_batch_import_and_calls(self):
        (self.path.parent/'research.pause').touch()
        with patch.object(worker,'recover_batches') as replay,patch.object(worker,'research_one') as research:
            receipt=worker.run(self.path,self.path.parent,None,{'enabled':True,'focused':True,'max_calls':6})
        self.assertEqual(receipt['reason'],'disabled');replay.assert_not_called();research.assert_not_called()

    def test_fresh_source_read_is_still_required_for_cached_confirmation(self):
        url='https://example.com/job';at=worker.sources.stamp()
        source=dict(url=url,text='Current',ok=True,checked_at=at)
        with patch.object(memo.sources,'read',return_value=source) as read:
            memo.read_source(self.conn,url,{},at);memo.read_source(self.conn,url,{},at)
        self.assertEqual(read.call_count,2)
        job={'type':'job','contacts':[],'evidence_checks':{'vacancy':dict(status='confirmed',checked_at='2026-10-18T00:00:00Z')}}
        self.assertTrue(worker.current_checks(job,'2026-10-18T07:00:00Z'))
        self.assertFalse(worker.current_checks(job,'2026-10-18T08:00:00Z'))

    def test_worker_consumes_real_projection_including_qualification_slots(self):
        import make_fixture
        fixture=self.path.parent/'fixture.db';make_fixture.build(fixture)
        conn=db.connect(fixture)
        try:
            state=records.state_context(conn)
            projection=state['opportunities']
            expected=set(projection['research_ids'])
            selected=worker.focus_work(conn,worker.select_work(conn,state),state)
            self.assertTrue(expected)
            self.assertEqual({r['id'] for r in selected},expected)
            self.assertTrue({r['record_id'] for r in projection['needs_checking']} <= expected)
            self.assertLessEqual(len(expected), len(projection['active']) + len(projection['shortlisted']) + 10)
            self.assertFalse(any(r['type']=='person' for r in selected))
        finally:conn.close()

    def test_context_change_during_research_prevents_import(self):
        p=records.create(self.conn,dict(type='person',name='Test Person',url='https://example.com/team'),actor='agent')['record']
        target=dict(p,context_revision=p['revision'])
        records.draft(self.conn,dict(record_id=p['id'],revision=p['revision'],purpose='message',text='Human edit'),actor='owner')
        finding=dict(id=p['id'],reviewed_at=worker.sources.stamp(),source_urls=[p['url']],freshness_checks=[dict(scope='contact_route',person_id=p['id'],status='unverified',source_url=p['url'],checked_at=worker.sources.stamp(),basis='Cannot confirm')])
        self.assertEqual(worker.apply_finding(self.conn,target,finding,'research:vps:concurrent',None)['outcome'],'context_changed')


class SourceListAndRouteScopeTests(unittest.TestCase):
    """Stored source lists, person-only versus employer routes, and route provenance."""
    LIVE_SHAPED=('https://mail.google.com/mail/u/0/#all/18a0b1c2d3e4f5a6; https://www.linkedin.com/in/example-person; '
                 'https://example.com/about;jsessionid=AB12; https://example.com/team;')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.db';self.conn=db.connect(self.path);db.migrate(self.conn);self.addCleanup(self.conn.close)

    def source(self,url,ok=True,text='',reason='read'):
        return dict(url=url,ok=ok,text=text,checked_at=worker.sources.stamp(),reason=reason if not ok else 'read')

    def test_stored_semicolon_list_keeps_legitimate_url_punctuation(self):
        self.assertEqual(worker.urls_in(self.LIVE_SHAPED),[
            'https://mail.google.com/mail/u/0/#all/18a0b1c2d3e4f5a6','https://www.linkedin.com/in/example-person',
            'https://example.com/about;jsessionid=AB12','https://example.com/team'])
        self.assertEqual(worker.urls_in('https://example.com/about; https://example.com/x'),
                         ['https://example.com/about','https://example.com/x'])
        self.assertEqual(worker.urls_in('see (https://example.com/a), then https://example.com/b.'),
                         ['https://example.com/a','https://example.com/b'])
        self.assertEqual(worker.urls_in(''),[]);self.assertEqual(worker.urls_in(None),[])

    def test_read_targets_skip_private_mail_and_try_primary_sources_before_linkedin(self):
        person=dict(url='https://www.linkedin.com/in/example-person',evidence_urls=self.LIVE_SHAPED)
        self.assertEqual(worker.person_sources(person),[
            'https://example.com/about;jsessionid=AB12','https://example.com/team','https://www.linkedin.com/in/example-person'])

    def person(self,**extra):
        p=records.create(self.conn,dict(type='person',name='Alex Example',url='https://www.linkedin.com/in/alex-example',
                                        evidence_urls='https://mail.google.com/mail/u/0/#all/abc; https://alex.example.com/about',**extra),actor='agent')['record']
        return records.get(self.conn,p['id'])

    def test_person_without_employer_is_checked_on_a_personal_route_only(self):
        p=self.person();expected=[dict(p,route_organisation='')]
        p['contacts']=worker.person_contacts(p,[])
        self.assertEqual(p['contacts'],expected)
        read=[]
        def fake(conn,url,cache,at=None):
            read.append(url)
            return self.source(url,ok=url.startswith('https://alex.'),text='Alex Example writes about engineering teams. Contact Alex Example at https://www.linkedin.com/in/alex-example',reason='robots_disallowed')
        claim=dict(source=0,status='confirmed',quote='Contact Alex Example at https://www.linkedin.com/in/alex-example',basis='Own site links profile')
        with patch.object(memo,'read_source',side_effect=fake),patch.object(memo,'result',return_value={'contacts':[dict(person_id=p['id'],claim=claim)]}):
            finding,_,note=worker.research_person(self.conn,'r',{},p,{})
        self.assertNotIn('https://mail.google.com/mail/u/0/#all/abc',read)
        self.assertEqual(read,['https://alex.example.com/about','https://www.linkedin.com/in/alex-example'])
        check=finding['freshness_checks'][0]
        self.assertEqual((check['status'],check['provenance'],check['source_url']),('confirmed','public_read','https://alex.example.com/about'))
        self.assertNotIn('organisation_id',check);self.assertIsNone(note)

    def test_personal_route_confirmation_never_confirms_an_employer_route(self):
        p=self.person(organisation='Example Labs')
        org=records.create(self.conn,dict(type='company',name='Example Labs'),actor='agent')['record']
        contacts=worker.person_contacts(p,[dict(p,organisation_id=org['id'],route_organisation='Example Labs')])
        self.assertEqual([c.get('organisation_id') for c in contacts],[None,org['id']])
        p['contacts']=contacts
        text='Alex Example shares notes on engineering. Contact Alex Example at https://www.linkedin.com/in/alex-example'
        claim=dict(source=0,status='confirmed',quote='Contact Alex Example at https://www.linkedin.com/in/alex-example',basis='Own site')
        with patch.object(memo,'read_source',return_value=self.source('https://alex.example.com/about',text=text)), \
             patch.object(memo,'result',return_value={'contacts':[dict(person_id=p['id'],claim=claim)]}):
            finding,_,_=worker.research_person(self.conn,'r',{},p,{})
        by_scope={c.get('organisation_id'):c['status'] for c in finding['freshness_checks']}
        self.assertEqual(by_scope,{None:'confirmed',org['id']:'unverified'})

    def test_missing_claim_cites_the_source_actually_read_not_a_blocked_profile(self):
        p=self.person();p['contacts']=worker.person_contacts(p,[])
        def fake(conn,url,cache,at=None):
            if 'linkedin' in url:return self.source(url,ok=False,reason='robots_disallowed')
            return self.source(url,text='Alex Example writes about delivery.')
        with patch.object(memo,'read_source',side_effect=fake),patch.object(memo,'result',return_value={'contacts':[]}):
            finding,_,_=worker.research_person(self.conn,'r',{},p,{})
        check=finding['freshness_checks'][0]
        self.assertEqual((check['status'],check['source_url']),('unverified','https://alex.example.com/about'))
        self.assertNotIn('robots',check['basis'])
        # Nothing readable: cite the first route actually attempted with its real reason, never an unattempted one.
        with patch.object(memo,'read_source',side_effect=lambda c,u,k,at=None:self.source(u,ok=False,reason='robots_disallowed')) as read, \
             patch.object(memo,'result') as model:
            finding,_,_=worker.research_person(self.conn,'r',{},p,{})
        model.assert_not_called()
        self.assertEqual(finding['freshness_checks'][0]['source_url'],'https://alex.example.com/about')
        self.assertIn(finding['freshness_checks'][0]['source_url'],[c.args[1] for c in read.call_args_list])

    def test_a_failed_public_read_does_not_hide_a_confirmation_from_private_records(self):
        p=self.person()
        legacy=dict(scope='contact_route',person_id=p['id'],status='confirmed',checked_at='2026-10-18T17:38:00+00:00',
                    source_url='https://mail.google.com/mail/u/0/#all/abc',basis='The owner confirmed this personal email.')
        self.conn.execute('BEGIN')
        records.research_observation(self.conn,p['id'],'research:attended',dict(reviewed_at='2026-10-18T17:38:00Z',
            source_urls=[legacy['source_url']],freshness_checks=[legacy]))
        self.conn.execute('COMMIT')
        newer=dict(scope='contact_route',person_id=p['id'],status='unverified',checked_at=worker.sources.stamp(),
                   source_url='https://www.linkedin.com/in/alex-example',basis='robots_disallowed',provenance='public_read')
        self.conn.execute('BEGIN')
        records.research_observation(self.conn,p['id'],'research:vps:x',dict(reviewed_at=worker.sources.stamp(),
            source_urls=[newer['source_url']],freshness_checks=[newer]))
        self.conn.execute('COMMIT')
        shown=records.get(self.conn,p['id'])['evidence_checks']['contacts']
        self.assertEqual(sorted(c['status'] for c in shown),['confirmed','unverified'])
        # A newer public read supersedes an older public read of the same route.
        again=dict(newer,status='unverified',checked_at=worker.sources.stamp(),basis='still blocked')
        self.conn.execute('BEGIN')
        records.research_observation(self.conn,p['id'],'research:vps:y',dict(reviewed_at=worker.sources.stamp(),
            source_urls=[again['source_url']],freshness_checks=[again]))
        self.conn.execute('COMMIT')
        self.assertEqual(len(records.get(self.conn,p['id'])['evidence_checks']['contacts']),2)

    def test_provenance_is_a_closed_vocabulary_and_public_reads_need_public_sources(self):
        import evidence_checks
        base=dict(scope='contact_route',person_id='P1',status='confirmed',checked_at='2026-10-18T10:00:00Z',basis='b')
        for provenance in ('public_read','owner_confirmed','prior_correspondence'):
            url='https://example.com/a' if provenance=='public_read' else 'https://mail.google.com/mail/u/0/#all/x'
            evidence_checks.validate_check(dict(base,provenance=provenance,source_url=url),[url])
        for bad in (dict(base,provenance='guess',source_url='https://example.com/a'),
                    dict(base,provenance='public_read',source_url='https://mail.google.com/mail/u/0/#all/x')):
            with self.assertRaises(ValueError):evidence_checks.validate_check(bad,[bad['source_url']])
        self.assertEqual(evidence_checks.route_class(dict(base,source_url='https://mail.google.com/x')),'private')
        self.assertEqual(evidence_checks.route_class(dict(base,source_url='https://example.com/x')),'public')
        self.assertEqual(evidence_checks.route_class(dict(base,provenance='owner_confirmed',source_url='https://example.com/x')),'owner_confirmed')


class JobRouteAttributionTests(unittest.TestCase):
    def test_unreadable_advert_does_not_write_a_check_for_an_unattempted_contact_route(self):
        job=dict(id='J1',type='job',name='AI Engineer',organisation='Example Labs',url='https://example.com/job',
                 contacts=[dict(id='P1',name='Alex',url='https://example.com/team-not-read')])
        blocked=lambda conn,url,cache,at=None:dict(url=url,ok=False,text='',checked_at=worker.sources.stamp(),reason='robots_disallowed')
        with patch.object(memo,'read_source',side_effect=blocked),patch.object(memo,'result',return_value={'urls':[]}):
            finding,_,_=worker.research_one(None,'r',None,{},job,{})
        self.assertEqual([c['scope'] for c in finding['freshness_checks']],['vacancy','contact_route'])
        job['contacts'][0]['url']='https://elsewhere.example.org/alex'
        job['url']='https://example.com/job2'
        def partial(conn,url,cache,at=None):
            return blocked(conn,url,cache)
        with patch.object(memo,'read_source',side_effect=partial),patch.object(memo,'result',return_value={'urls':[]}), \
             patch.object(worker,'urls_in',return_value=[]):
            job['contacts'][0].pop('url')
            finding,_,_=worker.research_one(None,'r',None,{},job,{})
        self.assertEqual([c['scope'] for c in finding['freshness_checks']],['vacancy'])


class JobNoteLinkTests(unittest.TestCase):
    def test_a_job_note_needs_the_site_link_and_a_domain_named_employer_is_not_a_second_link(self):
        import note_context
        job=dict(id='J1',organisation='northwind.io',contacts=[dict(id='P1',route_organisation='brightpath.ai')])
        kept="Hi Sam, northwind.io's Forward Deployed Engineer role caught my eye, as did brightpath.ai's. More at portfolio.example.com. Cheers, Morgan"
        self.assertEqual(note_context.lint_note(worker.without_employers(kept,job)),[])
        self.assertIn('3 links',' '.join(note_context.lint_note(kept)))  # the shared check alone would drop it
        linkless=kept.replace(' More at portfolio.example.com.','')
        self.assertIn('No link to portfolio.example.com: every first approach includes it.',
                      note_context.lint_note(worker.without_employers(linkless,job)))
        other=kept.replace('portfolio.example.com','example.org/me and portfolio.example.com')
        self.assertIn('2 links',' '.join(note_context.lint_note(worker.without_employers(other,job))))

class DecisionFocusTests(unittest.TestCase):
    def test_application_blocker_precedes_routine_recheck_and_discovery_keeps_its_turn(self):
        import research_scope
        jobs=[dict(id='routine',type='job',status='applied',evidence_brief={'gaps':[]}),
              dict(id='apply',type='job',status='pursue',availability={'status':'open'},
                   relevance={'fit':{'status':'supported'}},evidence_brief={'gaps':[
                       {'code':'remit','text':'Establish the essential requirements.','owner':'research','blocking':True}]}),
              dict(id='discovery',type='job',status='candidate',evidence_brief={'gaps':[
                  {'code':'vacancy','text':'Check the exact vacancy.','owner':'research','blocking':True}]})]
        state=dict(records=jobs,opportunities=dict(research_ids=['routine','apply','discovery'],
                   active=[{'record_id':'routine'}],shortlisted=[{'record_id':'apply'}]),research_attempts={})
        plan=research_scope.decision_plan(state)
        self.assertEqual([p['record_id'] for p in plan],['apply','discovery','routine'])
        self.assertEqual(plan[0]['question'],'Establish the essential requirements.')
        self.assertIn('application',plan[0]['consequence'])
        self.assertEqual([j['id'] for j in worker.focus_work(None,jobs,state)],['apply','discovery','routine'])
        self.assertEqual(worker.focus_work(None,jobs,state)[0]['research_question'],plan[0]['question'])

    def test_research_cannot_resolve_a_personal_evidence_gap_or_invent_a_blocker(self):
        import research_scope
        jobs=[dict(id='personal',type='job',status='pursue',evidence_brief={'gaps':[
              {'code':'experience','text':'Confirm your experience.','owner':'owner','blocking':True}]}),
              dict(id='complete',type='job',status='candidate',evidence_brief={'gaps':[]},
                   relevance={'fit':{'status':'supported'}})]
        state=dict(records=jobs,opportunities={'research_ids':['personal','complete']},research_attempts={})
        plan=research_scope.decision_plan(state)
        self.assertEqual({p['record_id']:p['kind'] for p in plan},{'personal':'your_choice','complete':'maintenance'})
        self.assertIn('cannot establish',plan[0]['consequence'])

    def test_personal_only_blocker_never_uses_a_research_invocation(self):
        job=dict(id='personal',type='job',status='pursue',evidence_brief={'gaps':[
            {'code':'experience','text':'Confirm your experience.','owner':'owner','blocking':True}]})
        state=dict(records=[job],opportunities={'research_ids':['personal']},research_attempts={})
        self.assertEqual(worker.focus_work(None,[job],state),[])

    def test_one_decisive_public_question_precedes_multiple_unknowns(self):
        one=dict(id='one',type='job',status='pursue',evidence_brief={'gaps':[
            {'code':'vacancy','text':'Is this vacancy still open?','owner':'research','blocking':True}]})
        two=dict(id='two',type='job',status='pursue',evidence_brief={'gaps':[
            {'code':'vacancy','text':'Is this vacancy still open?','owner':'research','blocking':True},
            {'code':'remit','text':'What is the work?','owner':'research','blocking':True}]})
        state=dict(records=[two,one],opportunities={'research_ids':['two','one']},research_attempts={})
        self.assertEqual([r['id'] for r in worker.focus_work(None,[two,one],state)],['one','two'])

    def test_known_personal_fit_gap_is_a_choice_not_another_generic_research_question(self):
        import research_scope
        job=dict(id='fit',type='job',status='pursue',evidence_brief={'gaps':[]},
                 relevance={'fit':{'status':'supported','gaps':['Essential specialist experience is not established.']}})
        state=dict(records=[job],opportunities={'research_ids':['fit']},research_attempts={})
        plan=research_scope.decision_plan(state)
        self.assertEqual((plan[0]['kind'],plan[0]['question']),('your_choice','Essential specialist experience is not established.'))
        self.assertEqual(worker.focus_work(None,[job],state),[])

    def test_conflicting_source_requirement_is_still_researchable(self):
        import research_scope
        job=dict(id='conflict',type='job',status='pursue',evidence_brief={'gaps':[]},
                 relevance={'fit':{'status':'supported','gaps':['Requirement wording conflicts.'],
                                   'requirements':[{'status':'ambiguous','importance':'essential'}]}})
        state=dict(records=[job],opportunities={'research_ids':['conflict']},research_attempts={})
        self.assertEqual(research_scope.decision_plan(state)[0]['kind'],'decision')
        self.assertEqual([r['id'] for r in worker.focus_work(None,[job],state)],['conflict'])

    def test_a_public_blocker_still_receives_research_when_personal_evidence_is_also_missing(self):
        job=dict(id='mixed',type='job',status='pursue',evidence_brief={'gaps':[
            {'code':'vacancy','text':'Is the vacancy open?','owner':'research','blocking':True},
            {'code':'experience','text':'Confirm your experience.','owner':'owner','blocking':True}]})
        state=dict(records=[job],opportunities={'research_ids':['mixed']},research_attempts={})
        self.assertEqual([r['id'] for r in worker.focus_work(None,[job],state)],['mixed'])

    def test_personal_choice_disposition_does_not_claim_scheduled_research(self):
        import research_scope
        job=dict(id='personal',type='job',status='pursue',evidence_brief={'gaps':[
            {'code':'experience','text':'Confirm your experience.','owner':'owner','blocking':True}]})
        state=dict(records=[job],opportunities={'research_ids':['personal']},research_attempts={})
        research_scope.attach(None,state,{})
        self.assertEqual(job['research_disposition']['scope'],'needs_input')
        self.assertIn('waiting for your evidence',job['research_disposition']['label'])

    def test_dashboard_plan_is_bounded_without_truncating_worker_targets(self):
        import research_scope
        jobs=[dict(id=str(i),type='job',status='candidate') for i in range(20)]
        state=dict(records=jobs,opportunities={'research_ids':[r['id'] for r in jobs]},research_attempts={})
        research_scope.attach(None,state,{})
        self.assertEqual(len(state['research_plan']),6)
        self.assertEqual(len(worker.focus_work(None,jobs,state)),20)
