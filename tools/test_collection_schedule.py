"""Policy-selected task fairness, saturation and recovery using a durable synthetic ledger."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collection_budget as budget
import collection_schedule as schedule
import collection_policy
from backup_collection import embed,restore,verify
import sqlite3

class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.at=dt.datetime(2026,11,8,12,tzinfo=dt.timezone.utc)
        p=collection_policy.load()
        for v in p['providers'].values():v['min_interval_seconds']=0
        budget.initialize(self.root/'collection.db',at=self.at,hold_today=False)
        self.b=budget.Budget(self.root/'collection.db',p,now=lambda:self.at)
        self.use=budget.using(self.b);self.use.__enter__();self.addCleanup(self.use.__exit__,None,None,None)
        self.s=schedule.Session(self.root)
    def attempt(self,provider,key,fail=False):
        self.b.reserve(provider,'scan',key)
        if not fail:self.b.succeeded(provider,key)
    def test_all_twenty_one_queries_rotate_across_four_days_and_failures_get_a_turn(self):
        keys=[str(i) for i in range(21)];seen=[]
        for day in range(4):
            picked=self.s.choose('adzuna',keys)
            self.assertEqual(len(picked),6)
            for key in picked:self.attempt('adzuna',key,fail=key=='0')
            seen.extend(picked);self.at+=dt.timedelta(days=1)
        self.assertEqual(set(seen),set(keys))
        self.assertEqual(seen[:6],['0','1','2','3','4','5'])
    def test_current_work_gets_four_slots_and_discovery_two_when_full(self):
        keys=[str(i) for i in range(21)]
        self.s.context=dict(full=True,priority=['9','10','11','12','13'])
        self.assertEqual(self.s.choose('adzuna',keys),['9','10','11','12','0','1'])
        self.s.context=dict(full=True,priority=[])
        self.assertEqual(self.s.choose('adzuna',keys),['0','1'])
    def test_reed_reserves_discovery_within_its_search_allocation(self):
        self.s.context=dict(full=True,priority=['chosen1','chosen2','chosen3','chosen4'])
        keys=['chosen1','chosen2','chosen3','chosen4','new1','new2']
        self.assertEqual(self.s.choose('reed',keys),['chosen1','chosen2','new1','new2'])
        for key in ('chosen1','chosen2'):
            self.b.reserve('reed','scan',key,kind='search')
        self.assertEqual(self.s.choose('reed',keys),['new1','new2'])

    def test_denied_tasks_do_not_advance_and_shared_procurement_does_not_starve(self):
        self.attempt('contracts_finder','tender:contracts_finder')
        self.at+=dt.timedelta(days=1)
        self.assertEqual(self.s.choose('contracts_finder',['tender:contracts_finder'],background=True),[])
        self.assertEqual(self.s.choose('contracts_finder',['award:contracts_finder'],background=True),['award:contracts_finder'])
        for _ in range(9):self.b.reserve('adzuna')
        self.b.reserve('adzuna')
        self.assertEqual(self.s.choose('adzuna',['waiting']),[])
        self.assertNotIn('waiting',self.b.history('adzuna'))
    def test_chosen_background_work_can_refresh_between_discovery_days(self):
        self.s.context={'full':True,'priority':['tender:contracts_finder'],
                        'observed':{'tender:contracts_finder':(self.at-dt.timedelta(hours=25)).isoformat()}}
        self.assertEqual(self.s.choose('contracts_finder',['tender:contracts_finder'],background=True),['tender:contracts_finder'])
        self.assertEqual(self.s.choose('contracts_finder',['award:contracts_finder'],background=True),[])

    def test_background_days_and_repeated_run(self):
        self.s.context={'full':True}
        self.assertEqual(self.s.choose('hn_algolia',['hn:current'],background=True),[]) # Sunday
        self.at+=dt.timedelta(days=2)
        self.assertEqual(self.s.choose('hn_algolia',['hn:current'],background=True),['hn:current'])
        self.attempt('hn_algolia','hn:current')
        self.assertEqual(self.s.choose('hn_algolia',['hn:current'],background=True),[])
    def test_existing_evidence_untouched_when_source_is_deferred(self):
        import scan_boards
        cfg=self.root/'portals.yml';cfg.write_text('tracked_companies:\n  - {name: Example, careers_url: https://jobs.ashbyhq.com/example}\n')
        pending=self.root/'pending';pending.mkdir();obs=pending/'observations.json'
        from pending_store import ObservationStore
        ObservationStore(obs).observe(scope='board:ashby:example',source_type='job',provider='ashby',observed_at=self.at.isoformat(),
            state='ok',complete=True,items=[dict(source_id='1',current=dict(title='Example',url='https://example.test/1'))])
        before=obs.read_bytes()
        for _ in range(10):self.b.reserve('ashby')
        with patch.object(scan_boards,'fetch_board') as read:
            result=scan_boards.run(cfg,self.root,'2026-11-08',feeds=False)
        read.assert_not_called();self.assertEqual(obs.read_bytes(),before)
        self.assertEqual(result['boards']['Example']['state'],'deferred')
    def test_backup_round_trip_retains_all_reservations_and_restore_holds_today(self):
        for _ in range(10):self.b.reserve('adzuna')
        backup=self.root/'backup.db'
        with sqlite3.connect(backup) as c:c.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        embed(self.b.path,backup);self.assertTrue(verify(backup))
        path=self.root/'restored.db';restore(backup,path,at=self.at)
        other=budget.Budget(path,self.b.policy,now=lambda:self.at)
        self.assertEqual(other.summary()['providers']['adzuna']['used'],10)
        with self.assertRaises(budget.Deferred):other.reserve('reed')

    def test_news_budget_denial_is_a_deferral_without_an_observation(self):
        import scan_news
        cfg=self.root/'portals.yml'
        cfg.write_text('signals:\n  funding_news:\n    - id: uktn\n      enabled: true\n      url: https://www.uktech.news/funding/feed\n')
        with patch.object(scan_news,'fetch_text',side_effect=budget.Deferred('uktn','daily-limit')):
            result=scan_news.run(cfg,self.root,'2026-11-08')
        self.assertEqual(result['feeds']['uktn']['state'],'deferred')
        self.assertFalse((self.root/'pending/observations.json').exists())
    def test_canonical_board_reuse_preserves_clock_and_expires_before_freshness_window(self):
        import research_sources
        url='https://jobs.ashbyhq.com/example/11111111-2222-4333-8444-555555555555'
        old=(self.at-dt.timedelta(hours=12)).isoformat()
        job=dict(url=url,type='job',advert=dict(url=url,title='Example role',text='Apply for the Example role.',observed_at=old,source='board:ashby:example'))
        with patch.object(research_sources,'stamp',return_value=self.at.isoformat()),patch.object(research_sources.evidence_checks,'utc_now',return_value=self.at):
            result=research_sources.observed_posting(job,{})
            self.assertIsNotNone(result);self.assertEqual(result[0]['checked_at'],old)
            job['advert']['source']='feed:adzuna:london'
            self.assertIsNone(research_sources.observed_posting(job,{}))
            job['advert']['source']='board:ashby:example'
            job['advert']['observed_at']=(self.at-dt.timedelta(hours=25)).isoformat()
            self.assertIsNone(research_sources.observed_posting(job,{}))

    def test_source_cadence_follows_configured_discovery_days(self):
        import source_coverage
        p=collection_policy.load()
        with patch.object(collection_policy,'load',return_value=p):
            self.assertEqual(source_coverage._expected_age([{'provider':'hn_algolia'}]),5)
            p['background_days']=['Tuesday']
            self.assertEqual(source_coverage._expected_age([{'provider':'hn_algolia'}]),8)
            p['background_days']=list(collection_policy.DAYS)
            self.assertEqual(source_coverage._expected_age([{'provider':'hn_algolia'}]),3)

    def test_current_evidence_waits_twenty_four_hours_and_failure_does_not_repeat_today(self):
        self.s.context={'priority':['chosen'],'observed':{'chosen':(self.at-dt.timedelta(hours=23)).isoformat()}}
        self.assertEqual(self.s.choose('ashby',['chosen','new']),['new'])
        self.at+=dt.timedelta(hours=1)
        self.assertEqual(self.s.choose('ashby',['chosen','new']),['chosen','new'])
        self.attempt('ashby','chosen');self.at+=dt.timedelta(days=1)
        self.attempt('ashby','chosen',fail=True)
        self.assertNotIn('chosen',self.s.choose('ashby',['chosen','new']))

    def test_adapter_executes_current_work_before_configured_discovery(self):
        import scan_boards
        cfg=self.root/'portals.yml'
        cfg.write_text('tracked_companies:\n'+''.join(f'  - {{name: Board{i}, careers_url: https://jobs.ashbyhq.com/board{i}}}\n' for i in range(8)))
        self.s.context=dict(priority=['board:ashby:board7'],full=True)
        calls=[]
        def read(provider,slug):
            calls.append(slug);self.b.reserve(provider,'scan',budget._task.get());return {'jobs':[]}
        with patch.object(scan_boards.collection_schedule,'Session',return_value=self.s),patch.object(scan_boards,'fetch_board',side_effect=read):
            scan_boards.run(cfg,self.root,'2026-11-08',feeds=False)
        self.assertEqual(calls,['board7','board0','board1'])

    def test_registry_due_date_new_choice_number_change_and_failed_bundle(self):
        import scan_companies_house as ch
        from test_scan_companies_house import FakeRegister,ZEPHYR
        register=FakeRegister({'00000001':ZEPHYR,'00000002':ZEPHYR})
        def read(url):
            self.b.reserve('companies-house','scan',budget._task.get())
            return register(url)
        target={'id':'CO-example','name':'Example','company_number':'00000001'}
        def run():
            client=ch.Client('fixture',get=read,pause=0);client.synthetic=False
            with patch.object(ch.collection_schedule,'Session',return_value=self.s):
                return ch.run([target],self.root,client,self.at.date(),self.at.isoformat())
        self.assertEqual(run()['read'],1)
        self.at+=dt.timedelta(days=29)
        self.assertEqual(run()['state'],'deferred');self.assertEqual(len(register.urls),3)
        self.s.context={'companies':{'CO-example':self.at.isoformat()}}
        self.assertEqual(run()['read'],1) # newly chosen, before the monthly date
        self.at+=dt.timedelta(days=1);target['company_number']='00000002'
        self.assertEqual(run()['read'],1)
        path=self.root/'pending/observations.json';before=path.read_bytes()
        self.at+=dt.timedelta(days=30)
        with patch.object(ch,'read_company',side_effect=budget.Deferred('companies-house','daily-limit')):
            self.assertEqual(run()['state'],'deferred')
        self.assertEqual(path.read_bytes(),before)

    def test_context_uses_existing_pool_admissions_links_and_observation_provenance(self):
        import db,allowances
        c=db.connect(self.root/'work.db');db.migrate(c)
        self.addCleanup(c.close)
        c.execute('INSERT INTO records(id,type,name,created_at,updated_at) VALUES(?,?,?,?,?)',('JOB-x','job','Example',self.at.isoformat(),self.at.isoformat()))
        c.execute("INSERT INTO observations(record_id,source,observed_at,payload_json,payload_sha) VALUES(?,?,?,?,?)",('JOB-x','feed:adzuna:Example',self.at.isoformat(),json.dumps({'current':{'query_keys':['feed:q']}}),'sha'))
        c.execute("INSERT INTO state_events(entity_id,field,to_value,at) VALUES(?,?,?,?)",('JOB-x','working_state','shortlisted',self.at.isoformat()))
        state=dict(discovery_capacity={'research_pool_limit':30,'additions_per_seven_days':60},
            opportunities={'research_pool':list(range(30)),'shortlisted':[{'record_id':'JOB-x'}]},
            records=[{'id':'JOB-x','type':'job'}],links=[{'from_id':'JOB-x','to_id':'CO-x','relation':'hiring_for'}])
        out=schedule.context(c,state,self.at.isoformat())
        self.assertTrue(out['full']);self.assertEqual(out['priority'],['feed:q']);self.assertEqual(out['companies'],{'CO-x':self.at.isoformat()})
        state['links']=[{'from_id':'JOB-x','to_id':'CO-buyer','relation':'buyer'}]
        self.assertEqual(schedule.context(c,state,self.at.isoformat())['companies'],{'CO-buyer':self.at.isoformat()})
        state['opportunities']['research_pool']=[]
        self.assertFalse(schedule.context(c,state,self.at.isoformat())['full'])
        c.execute('INSERT INTO meta VALUES(?,?)',(allowances.KEY,json.dumps([{'id':str(n),'at':self.at.isoformat()} for n in range(60)])))
        self.assertTrue(schedule.context(c,state,self.at.isoformat())['full'])
