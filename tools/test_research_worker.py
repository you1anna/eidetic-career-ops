"""Unattended research must preserve evidence, coverage and a durable spending ceiling."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
import db
import records
import research_worker as worker

class ResearchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / 'work.db'
        self.conn = db.connect(self.path); db.migrate(self.conn); self.addCleanup(self.conn.close)
        self.now = '2026-10-17T12:00:00Z'

class ResearchTests(ResearchCase):
    def test_successful_http_login_redirect_is_unreadable_for_research(self):
        page=dict(url='https://example.com/sign-in?redirect_url=%2Fjobs%2F1',headers={},charset='utf-8',
                  body=b'<html><body>Welcome to the hiring platform. Find your next role. Log in with email. New to the platform? Sign up. Read about our service and the many companies using it.</body></html>')
        robots=dict(body=b'User-agent: *\nAllow: /',headers={},charset='utf-8',url='https://example.com/robots.txt')
        with patch.object(worker.sources,'public_url'),patch.object(worker.sources.web_read,'request',side_effect=[robots,page]):
            result=worker.sources.read('https://example.com/jobs/1')
        self.assertEqual((result['ok'],result['reason']),(False,'login_or_challenge'))
        self.assertEqual(result['text'],'')

    def test_cli_expected_wait_is_success_but_operational_failure_is_not(self):
        for status,reason,expected in (
            ('blocked','usage_limit',0), ('partial','usage_limit',0),
            ('blocked','disabled',0), ('ok',None,0), ('partial',None,0),
            ('blocked','access',1), ('failed','TimeoutError',1),
            ('failed','usage_limit',1),
        ):
            receipt={'status':status,'reason':reason}
            with self.subTest(status=status,reason=reason), \
                 patch.object(sys,'argv',['research_worker','--db',str(self.path),'--data',str(self.root)]), \
                 patch.object(worker,'run',return_value=receipt), patch('builtins.print'):
                self.assertEqual(worker.main(),expected)

    def test_unknown_call_cost_is_reserved_across_restarts_and_months(self):
        limits = {'daily_usd': 2, 'monthly_usd': 3}
        a = worker.reserve(self.conn, 'r', 1_000_000, limits, self.now)
        worker.reserve(self.conn, 'r', 1_000_000, limits, self.now)
        with self.assertRaises(worker.BudgetStopped): worker.reserve(self.conn, 'r', 1, limits, self.now)
        worker.settle(self.conn, a, {'input_tokens': 1000, 'output_tokens': 100}, tools=0)
        self.assertLess(worker.spending(self.conn, self.now)['day_usd'], 2)
        worker.reserve(self.conn, 'next', 1_000_000, limits, '2026-10-18T12:00:00Z')
        with self.assertRaises(worker.BudgetStopped):
            worker.reserve(self.conn, 'next', 1_000_000, limits, '2026-10-18T12:00:00Z')

    def test_reservation_survives_database_backup_and_reopen(self):
        worker.reserve(self.conn,'run',1_000_000,{'daily_usd':2,'monthly_usd':30,'enabled':True},self.now)
        target=db.connect(self.root/'restored.db')
        try:
            self.conn.backup(target)
            self.assertEqual(worker.spending(target,self.now)['reserved_usd'],1)
        finally: target.close()

    def test_missing_or_impossible_usage_does_not_release_reservation(self):
        rid = worker.reserve(self.conn, 'r', 80_000, {'daily_usd': 2, 'monthly_usd': 30}, self.now)
        with self.assertRaises(ValueError): worker.settle(self.conn, rid, {})
        self.assertEqual(worker.spending(self.conn, self.now)['day_usd'], .08)
        with self.assertRaises(ValueError): worker.settle(self.conn, rid, {'input_tokens': -1, 'output_tokens': 1})

    def test_sources_cannot_target_private_hosts_or_redirect_to_them(self):
        for url in ['http://127.0.0.1/a','http://[::1]/','http://169.254.169.254/','file:///etc/passwd','https://u:p@example.com','https://example.com:1234']:
            with self.subTest(url=url), self.assertRaises(ValueError): worker.public_url(url)
        with patch.object(worker.socket, 'getaddrinfo', return_value=[(2,1,6,'',('10.0.0.1',80))]):
            with self.assertRaises(ValueError): worker.public_connection(('example.com',80))

    def test_fabricated_quote_or_wrong_job_cannot_refresh_a_check(self):
        job = {'id':'JOB-one','name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/jobs/1'}
        source = {'url':job['url'],'checked_at':self.now,'text':'AI Engineer at Example Labs. Apply now for this position.','ok':True}
        claim = {'status':'confirmed','source':0,'quote':'Apply now for this position.','basis':'Exact advert open'}
        good = worker.check_claim(job, claim, [source])
        self.assertEqual(good['status'],'confirmed'); self.assertEqual(good['checked_at'],self.now)
        self.assertEqual(worker.check_claim(job, claim|{'quote':'fabricated evidence'}, [source])['status'],'unverified')
        self.assertEqual(worker.check_claim(job, claim, [source|{'text':'Browse all jobs. Apply now for this position.'}])['status'],'unverified')
        self.assertEqual(worker.check_claim(job, claim, [source|{'ok':False}])['status'],'unverified')

    def test_historical_contact_and_hiring_authority_are_not_inferred(self):
        job={'name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/job'}
        person={'id':'CP-one','name':'Alex Example','organisation':'Example Labs','url':'https://example.com/team'}
        claim={'status':'confirmed','source':0,'quote':'Alex Example','basis':'Former colleague'}
        source={'url':person['url'],'checked_at':self.now,'text':'Alex Example previously worked at Example Labs.','ok':True}
        self.assertEqual(worker.check_claim(job,claim,[source],person)['status'],'unverified')

    def test_disabled_worker_records_block_without_model_requests(self):
        with patch.object(worker,'select_work',return_value=[]), patch.object(worker.model,'exchange') as api:
            receipt=worker.run(self.path,self.root,None,{'enabled':False})
        self.assertEqual(receipt['status'],'blocked');api.assert_not_called()
        self.assertEqual(receipt['reason'],'disabled')

    def test_a_partial_run_does_not_replace_last_complete_run(self):
        import record_views
        worker.save_receipt(self.conn,'research:2026-10-17T09:00:00Z',{'status':'ok','started_at':'2026-10-17T09:00:00Z','finished_at':'2026-10-17T09:10:00Z','checked':3,'total':3})
        worker.save_receipt(self.conn,'research:2026-10-17T19:00:00Z',{'status':'partial','started_at':'2026-10-17T19:00:00Z','finished_at':'2026-10-17T19:10:00Z','checked':1,'total':3,'backlog':2})
        status=record_views.research_status(self.conn)
        self.assertEqual(status['last_success'],'2026-10-17T09:10:00Z'); self.assertEqual(status['backlog'],2)

    def test_status_counts_completed_passes_that_confirmed_nothing_and_names_a_capped_pass(self):
        import record_views
        def research(day,hour,status='partial',**receipt):
            at=f'2026-10-{day}T{hour}:00:00Z'
            worker.save_receipt(self.conn,'research:'+at,dict(status=status,started_at=at,finished_at=f'2026-10-{day}T{hour}:10:00Z',**receipt))
            return record_views.research_status(self.conn)
        nothing=dict(checked=2,items=[{'id':'A','outcome':'unverified'},{'id':'B','outcome':'missing_context'}])
        research(20,'08',checked=2,contacts_verified=1,items=[{'id':'P','outcome':'unverified'}])
        research(20,'18',**nothing)
        research(21,'08',status='blocked',checked=0,reason='usage_limit',usage={'calls':0},limits={'max_calls':6})
        status=research(21,'18',**nothing,reason='usage_limit',usage={'calls':6},limits={'max_calls':6})
        self.assertEqual((status['quiet_passes'],status['last_confirmed'],status['quiet_attention'],status['pass_cap']),
                         (2,'2026-10-20T08:10:00Z',False,True))
        research(22,'08',**nothing,reason='usage_limit',usage={'calls':2},limits={'max_calls':6})
        status=research(22,'18',**nothing)
        self.assertEqual((status['quiet_passes'],status['quiet_attention'],status['pass_cap']),(4,True,False))
        self.assertFalse(record_views.research_pass_capped(dict(nothing,reason='usage_limit',usage={'calls':2},limits={'max_calls':6})))
        for confirmed in ({'outcome':'closed'},{'outcome':'current'},{'outcome':'confirmed'}):
            self.assertTrue(record_views.research_confirmed({'items':[confirmed]}),confirmed)
        self.assertTrue(record_views.research_confirmed({'verified':1}))
        status=research(23,'08',checked=1,items=[{'id':'J','outcome':'closed'}])
        self.assertEqual((status['quiet_passes'],status['last_confirmed'],status['quiet_attention']),(0,'2026-10-23T08:10:00Z',False))

class RecoveryAndImportTests(ResearchCase):
    def job(self):
        job=records.create(self.conn,{'type':'job','name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/jobs/1','status':'pursue'},actor='agent')['record']
        self.job_id=job['id'];return job

    def finding(self):
        return {'id':self.job_id,'reviewed_at':self.now,'source_urls':['https://example.com/jobs/1'],
                'freshness_checks':[{'scope':'vacancy','status':'confirmed','checked_at':self.now,'source_url':'https://example.com/jobs/1','basis':'AI Engineer at Example Labs; Apply now.'}],
                'facts':['Synthetic job evidence.']}

    def test_crash_after_import_replays_without_changing_dates_or_drafts(self):
        job=self.job();finding=self.finding();name='a'*32+'.json'
        digest=worker.private_json(self.root/'research-server'/name,{'finding':finding,'sources':[],'note':None})
        job['context_snapshot']=worker.brief_research.snapshot(self.conn,dict(records.get(self.conn,job['id']),contacts=job.get('contacts',[])))
        worker.apply_finding(self.conn,job,finding,'research:vps:'+name,None)
        before=self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        worker.save_receipt(self.conn,'research:old',{'started_at':self.now,'status':'running','items':[{'id':job['id'],'batch':name,'sha256':digest}]})
        self.assertEqual(worker.recover_batches(self.conn,self.root),1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0],before)
        self.assertEqual(worker.recover_batches(self.conn,self.root),0)

    def test_research_preserves_owner_draft_and_workflow_and_prior_urls(self):
        job=self.job()
        records.draft(self.conn,{'record_id':job['id'],'revision':job['revision'],'purpose':'message','text':'My own message'},actor='owner')
        job['context_snapshot']=worker.brief_research.snapshot(self.conn,dict(records.get(self.conn,job['id']),contacts=job.get('contacts',[])))
        result=worker.apply_finding(self.conn,job,self.finding(),'research:vps:test',{'text':'Generated replacement'})
        self.assertEqual(result['items'][0]['outcome'],'updated')
        self.assertEqual(self.conn.execute('SELECT text FROM drafts WHERE record_id=?',(job['id'],)).fetchone()[0],'My own message')
        self.assertEqual(self.conn.execute('SELECT status FROM records WHERE id=?',(job['id'],)).fetchone()[0],'pursue')

    def test_failed_recheck_supersedes_the_company_specific_contact_confirmation(self):
        job=self.job()
        company=records.create(self.conn,{'type':'company','name':'Example Labs'},actor='agent')['record']
        person=records.create(self.conn,{'type':'person','name':'Alex Example','url':'https://example.com/team'},actor='agent')['record']
        records.link(self.conn,dict(from_id=job['id'],to_id=company['id'],relation='hiring_for',revision=job['revision']),actor='agent')
        records.link(self.conn,dict(from_id=person['id'],to_id=company['id'],relation='works_at',revision=person['revision']),actor='agent')
        finding=self.finding();finding['source_urls'].append(person['url'])
        finding['freshness_checks'].append(dict(scope='contact_route',person_id=person['id'],organisation_id=company['id'],status='confirmed',source_url=person['url'],checked_at=self.now,basis='Alex Example currently works at Example Labs.'))
        job['context_snapshot']=worker.brief_research.snapshot(self.conn,dict(records.get(self.conn,job['id']),contacts=job.get('contacts',[])))
        worker.apply_finding(self.conn,job,finding,'research:old',None)
        selected=next(j for j in worker.select_work(self.conn) if j['id']==job['id'])
        self.assertEqual(selected['contacts'][0]['organisation_id'],company['id'])
        with patch.object(worker,'select_work',return_value=[selected]),patch.object(worker,'research_one',side_effect=TimeoutError):
            worker.run(self.path,self.root,None,{'enabled':True})
        checks=records.get(self.conn,job['id'])['evidence_checks']['contacts']
        matching=[c for c in checks if c.get('organisation_id')==company['id']]
        self.assertEqual(len(matching),1)
        self.assertEqual(matching[0]['status'],'unverified')

    def test_complete_import_and_partial_coverage_are_accounted_separately(self):
        job=self.job();job['contacts']=[]
        later=records.create(self.conn,dict(type='job',name='Software Lead',status='pursue',url='https://example.com/jobs/2'),actor='agent')['record']
        with patch.object(worker,'select_work',return_value=[job,dict(later,contacts=[])]), patch.object(worker,'research_one',return_value=(self.finding(),[],None)):
            result=worker.run(self.path,self.root,'synthetic-key',{'daily_usd':2,'monthly_usd':30,'enabled':True},max_jobs=1)
        self.assertEqual(result['checked'],1);self.assertEqual(result['verified'],1);self.assertEqual(result['backlog'],1);self.assertEqual(result['status'],'partial')

    def test_collection_deferral_preserves_evidence_and_waiting_without_codex_exhaustion(self):
        job=self.job();job['contacts']=[]
        before=records.get(self.conn,job['id'])
        with patch.object(worker,'select_work',return_value=[job]),patch.object(worker,'research_one',side_effect=worker.collection_budget.Deferred('ashby','daily-limit')):
            result=worker.run(self.path,self.root,None,{'enabled':True})
        self.assertEqual((result['status'],result['reason'],result['checked']),('blocked','collection_limit',0))
        self.assertEqual(result['waiting_ids'],[job['id']])
        self.assertEqual(records.get(self.conn,job['id'])['revision'],before['revision'])
        self.assertEqual(records.get(self.conn,job['id'])['evidence_checks'],before['evidence_checks'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM codex_invocations').fetchone()[0],0)

    def test_budget_stop_keeps_remaining_jobs_visible(self):
        job=self.job();job['contacts']=[]
        with patch.object(worker,'select_work',return_value=[job]),patch.object(worker,'research_one',side_effect=worker.model.UsageLimited):
            result=worker.run(self.path,self.root,'synthetic-key',{'daily_usd':2,'monthly_usd':30,'enabled':True})
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['backlog'],1);self.assertEqual(result['checked'],0)

class ModelBoundaryTests(ResearchCase):
    def test_codex_usage_and_no_api_fallback(self):
        response={'ok':True,'result':{'urls':[]},'usage':{'input_tokens':300,'output_tokens':100}}
        limits={'max_calls':1}
        with patch.object(worker.model,'exchange',return_value=response) as exchange:
            result=worker.model.call(self.conn,'run',None,limits,self.now,{'job':'synthetic'},worker.model.DISCOVERY,search=True)
            self.assertEqual(result,{'urls':[]})
            self.assertEqual(exchange.call_args.args[0]['operation'],'discovery')
            with self.assertRaises(worker.model.UsageLimited):worker.model.call(self.conn,'run',None,limits,self.now,{},worker.model.DISCOVERY,search=True)
        self.assertEqual(limits['_usage']['input_tokens'],300)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM research_requests').fetchone()[0],0)

    def test_timeout_does_not_retry_or_fall_back_to_api(self):
        with patch.object(worker.model,'exchange',side_effect=TimeoutError) as exchange:
            with self.assertRaises(TimeoutError):
                worker.model.call(self.conn,'run',None,{},self.now,{},worker.model.FINDING)
        self.assertEqual(exchange.call_count,1)

    def test_failed_new_verification_supersedes_old_confirmation(self):
        job=records.create(self.conn,{'type':'job','name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/job','status':'pursue'},actor='agent')['record'];job['contacts']=[]
        with patch.object(worker,'select_work',return_value=[job]),patch.object(worker,'research_one',side_effect=TimeoutError):
            result=worker.run(self.path,self.root,'synthetic',{'daily_usd':2,'monthly_usd':30,'enabled':True})
        self.assertEqual(result['status'],'partial');self.assertEqual(result['verified'],0)
        actual=records.get(self.conn,job['id'])['evidence_checks']['vacancy']
        self.assertEqual(actual['status'],'unverified')

    def test_a_failed_item_records_its_type_and_code_location_but_never_its_message(self):
        job=records.create(self.conn,{'type':'job','name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/job','status':'pursue'},actor='agent')['record'];job['contacts']=[]
        def raises(*args,**kwargs):
            raise ValueError('SENTINEL page text that must not be stored')
        with patch.object(worker,'select_work',return_value=[job]),patch.object(worker,'research_one',side_effect=raises):
            result=worker.run(self.path,self.root,'synthetic',{'daily_usd':2,'monthly_usd':30,'enabled':True})
        item=result['items'][0]
        self.assertEqual((item['outcome'],item['reason']),('failed','ValueError'))
        self.assertRegex(item['where'],r'^test_research_worker\.py:\d+ raises <- research_worker\.py:\d+ ')
        stored=self.conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'research:%'").fetchone()[0]
        self.assertNotIn('SENTINEL',stored)
        status=worker.record_views.research_status(self.conn)
        self.assertEqual(status['item_failures'],[dict(reason='ValueError',where=item['where'],count=1)])
        self.assertNotIn('SENTINEL',json.dumps(status))

    def test_alternate_listing_or_redirect_cannot_confirm_an_exact_vacancy(self):
        job={'name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/job'}
        source={'url':job['url'],'final_url':'https://example.com/careers','checked_at':self.now,'ok':True,'text':'Example Labs AI Engineer and hundreds of jobs. Apply now for this position.'}
        claim={'status':'confirmed','source':0,'quote':'Apply now for this position.','basis':'Open'}
        self.assertEqual(worker.check_claim(job,claim,[source])['status'],'unverified')

    def test_public_reader_obeys_robots_and_retains_failed_reason(self):
        with patch.object(worker.sources.web_read,'request',return_value={'body':b'User-agent: *\nDisallow: /','charset':'utf-8'}):
            result=worker.sources.read('https://example.com/job')
        self.assertFalse(result['ok']);self.assertEqual(result['reason'],'robots_disallowed')
