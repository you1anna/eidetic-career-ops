"""Independent cap/clock/transport regressions; no provider sockets."""
import concurrent.futures
import datetime as dt
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'workspace'), str(Path(__file__).resolve().parent)]
import collection_policy as policy
import collection_budget as budget
import web_read

AT = '2026-11-08T12:00:00+00:00'
class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'collection.db'
        self.policy=policy.load();self.policy['providers']['adzuna']['min_interval_seconds']=0
        budget.initialize(self.path, at=AT, hold_today=False)
        self.b=budget.Budget(self.path, self.policy, now=lambda:dt.datetime.fromisoformat(AT))
    def test_concurrent_callers_share_ten_and_restart_does_not_refund(self):
        def call(i):
            try:return self.b.reserve('adzuna', caller='attended', task=str(i))
            except budget.Deferred:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:ids=list(pool.map(call,range(32)))
        self.assertEqual(sum(x is not None for x in ids),10)
        with self.assertRaises(budget.Deferred):budget.Budget(self.path,self.policy,now=self.b.now).reserve('adzuna')
        self.assertEqual(self.b.summary()['providers']['adzuna']['used'],10)
    def test_scheduled_target_and_independent_provider(self):
        for _ in range(6):self.b.reserve('adzuna',caller='scan')
        with self.assertRaises(budget.Deferred):self.b.reserve('adzuna',caller='scan')
        for _ in range(4):self.b.reserve('adzuna',caller='research')
        with self.assertRaises(budget.Deferred):self.b.reserve('adzuna')
        self.b.reserve('reed');self.assertEqual(self.b.summary()['providers']['reed']['used'],1)
    def test_cutover_missing_corrupt_and_restored_store_fail_closed(self):
        other=Path(self.tmp.name)/'new.db';budget.initialize(other,at=AT)
        with self.assertRaises(budget.Deferred):budget.Budget(other,self.policy,now=self.b.now).reserve('adzuna')
        with self.assertRaises(budget.Deferred):budget.Budget(other.with_name('absent'),self.policy,now=self.b.now).reserve('adzuna')
        other.write_bytes(b'broken')
        with self.assertRaises(budget.Deferred):budget.Budget(other,self.policy,now=self.b.now).reserve('adzuna')
    def test_london_midnight_dst_and_clock_rewind(self):
        self.b.now=lambda:dt.datetime.fromisoformat('2027-03-28T22:59:59+00:00')
        for _ in range(10):self.b.reserve('adzuna')
        self.b.now=lambda:dt.datetime.fromisoformat('2027-03-28T23:00:00+00:00')
        self.b.reserve('adzuna');self.assertEqual(self.b.summary()['providers']['adzuna']['used'],1)
        self.b.now=lambda:dt.datetime.fromisoformat(AT)
        with self.assertRaises(budget.Deferred):self.b.reserve('adzuna')
        self.assertEqual(self.b.summary()['blocked_reason'],'clock-rewind')
    def test_transport_eleventh_never_opens_and_no_lock_during_io(self):
        calls=[]
        def open_(req,timeout):
            with sqlite3.connect(self.path,timeout=.1) as c:c.execute('BEGIN IMMEDIATE');c.rollback()
            calls.append(req.full_url)
            raise TimeoutError()
        with budget.using(self.b),web_read.single_attempt():
            for _ in range(10):
                with self.assertRaises(TimeoutError):web_read.request('https://api.adzuna.com/v1/api/jobs/gb/search/1',opener=open_)
            with self.assertRaises(budget.Deferred):web_read.request('https://api.adzuna.com/v1/api/jobs/gb/search/1',opener=open_)
        self.assertEqual(len(calls),10)
    def test_diagnostic_is_read_only_and_uses_the_same_effective_limits(self):
        import json,subprocess
        self.b.reserve('adzuna')
        before=self.path.read_bytes()
        run=subprocess.run([sys.executable,'tools/wo.py','--db',str(self.path.with_name('records.db')),'collection-policy','--json'],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        value=json.loads(run.stdout)
        self.assertEqual(value['effective_policy']['providers']['adzuna']['daily_limit'],10)
        self.assertEqual(value['production_authority'],'/srv/work-ops/data/collection.db')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.path.with_name('records.db').exists())
        with patch.object(policy,'load',side_effect=ValueError('unsafe setting')):
            self.assertEqual(self.b.summary()['state'],'ok') # this explicit fixture carries its validated snapshot
            import collection_reads
            self.assertEqual(collection_reads.summary(self.path)['reason'],'invalid-policy')

    def test_default_authority_refuses_local_live_reads(self):
        with patch.object(budget,'AUTHORITY',Path(self.tmp.name)/'absent'):
            with self.assertRaises(budget.Deferred):budget.begin('https://api.adzuna.com/v1/api/jobs/gb/search/1')
    def test_policy_defaults_and_invalid_configuration(self):
        self.assertEqual(self.policy['providers']['adzuna']['daily_limit'],10)
        self.assertEqual(self.policy['providers']['reed']['detail_limit'],2)
        self.assertEqual(self.policy['providers']['companies-house']['refresh_days'],30)
        from copy import deepcopy
        for alter in (lambda p:p.update(typo=True),lambda p:p['providers']['adzuna'].update(daily_limit=11)):
            p=policy.load();alter(p)
            with self.assertRaises(ValueError):policy.validate(p)
    def test_provider_mapping_groups_boards_and_procurement_but_not_mail(self):
        for u,p in [('https://api.ashbyhq.com/posting-api/job-board/a','ashby'),
                    ('https://api.ashbyhq.com/posting-api/job-board/b','ashby'),
                    ('https://www.reed.co.uk/api/1.0/jobs/123','reed'),
                    ('https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages','find_a_tender')]:
            self.assertEqual(policy.provider_for(u),p)
        self.assertEqual(policy.provider_for('https://reed.co.uk/%61pi/1.0/jobs/1'),'reed')
        self.assertEqual(policy.provider_for('https://www.reed.co.uk/path/../api/1.0/search'),'reed')
        self.assertIsNone(policy.provider_for('https://mail.google.com/'))
        self.assertIsNone(policy.provider_for('https://www.reed.co.uk/jobs/example/123'))
        self.assertEqual(policy.provider_for('https://www.uktech.news/funding/feed'),'uktn')
        self.assertIsNone(policy.provider_for('https://www.uktech.news/funding/example-company'))

    def test_retries_and_redirects_each_spend_an_attempt_without_fallback(self):
        from urllib.request import Request
        from urllib.error import URLError
        attempts=[]
        def broken(req,timeout):
            attempts.append(req.full_url);raise URLError('synthetic offline')
        with budget.using(self.b),patch.object(web_read.POLICY,'wait'),patch.object(web_read.POLICY,'may_retry',return_value=True):
            for _ in range(3):
                with self.assertRaises(URLError):web_read.request('https://api.adzuna.com/test',opener=broken)
            self.assertEqual(len(attempts),9)
            redirect=web_read.SafeRedirectHandler()
            redirect.redirect_request(Request('https://api.adzuna.com/first'),io.BytesIO(),302,'',{},'https://api.adzuna.com/second')
            with self.assertRaises(budget.Deferred):redirect.redirect_request(Request('https://api.adzuna.com/first'),io.BytesIO(),302,'',{},'https://api.adzuna.com/second')
        self.assertEqual(self.b.summary()['providers']['adzuna']['used'],10)
        import fetch
        with budget.using(self.b),patch.object(fetch,'remote_read') as remote,patch.object(fetch,'browser_read') as browser:
            code,body,notes=fetch.run('https://api.adzuna.com/test',fmt='json',remote='synthetic')
        self.assertEqual(code,1);self.assertIn('collection-deferred',' '.join(notes))
        remote.assert_not_called();browser.assert_not_called()

    def test_reed_allocations_and_registry_bundles_are_daily_not_per_instance(self):
        self.policy['providers']['reed']['min_interval_seconds']=0
        for _ in range(2):self.b.reserve('reed','scan',kind='detail')
        with self.assertRaisesRegex(budget.Deferred,'detail-limit'):
            budget.Budget(self.path,self.policy,now=self.b.now).reserve('reed','scan',kind='detail')
        for _ in range(4):self.b.reserve('reed','scan',kind='search')
        with self.assertRaises(budget.Deferred):self.b.reserve('reed','scan',kind='search')
        self.policy['providers']['companies-house']['min_interval_seconds']=0
        for n in range(3):self.b.reserve('companies-house','scan',task='registry:'+str(n))
        with self.assertRaisesRegex(budget.Deferred,'company-limit'):self.b.reserve('companies-house','scan',task='registry:next')
        self.b.reserve('companies-house','scan',task='registry:0')

    def test_redirect_to_another_public_host_retains_original_provider_budget(self):
        from urllib.request import Request
        request=Request('https://api.ashbyhq.com/board')
        with budget.using(self.b):
            request._collection_reservations=[budget.begin(request.full_url)]
            redirected=web_read.SafeRedirectHandler().redirect_request(request,io.BytesIO(),302,'',{},'https://cdn.example.test/board')
            self.assertEqual(self.b.summary()['providers']['ashby']['used'],2)
            for _ in range(8):self.b.reserve('ashby')
            with self.assertRaises(budget.Deferred):
                web_read.SafeRedirectHandler().redirect_request(redirected,io.BytesIO(),302,'',{},'https://cdn.example.test/next')

    def test_lowered_limits_and_autumn_dst_never_refund(self):
        for _ in range(6):self.b.reserve('adzuna')
        self.policy['providers']['adzuna']['daily_limit']=4
        with self.assertRaises(budget.Deferred):self.b.reserve('adzuna')
        self.assertEqual(self.b.summary()['providers']['adzuna']['used'],6)
        self.policy['providers']['adzuna']['daily_limit']=10
        self.b.now=lambda:dt.datetime.fromisoformat('2027-10-30T23:00:00+00:00')
        for _ in range(10):self.b.reserve('adzuna')
        for when in ('2027-10-31T00:30:00+00:00','2027-10-31T01:30:00+00:00','2027-10-31T23:59:59+00:00'):
            self.b.now=lambda:dt.datetime.fromisoformat(when)
            with self.assertRaises(budget.Deferred):self.b.reserve('adzuna')
        self.b.now=lambda:dt.datetime.fromisoformat('2027-11-01T00:00:00+00:00')
        self.b.reserve('adzuna')

    def test_pacing_wait_holds_no_sqlite_transaction(self):
        self.b.reserve('adzuna')
        self.policy['providers']['adzuna']['min_interval_seconds']=4.1
        waits=[]
        def wait(seconds):
            with sqlite3.connect(self.path,timeout=.1) as c:c.execute('BEGIN IMMEDIATE');c.rollback()
            waits.append(seconds)
        self.b.sleep=wait;self.b.reserve('adzuna')
        self.assertEqual(waits,[4.1])

    def test_pacing_continues_across_london_midnight(self):
        self.b.now=lambda:dt.datetime.fromisoformat('2026-11-08T23:59:59+00:00')
        self.b.reserve('adzuna')
        self.policy['providers']['adzuna']['min_interval_seconds']=4.1
        self.b.now=lambda:dt.datetime.fromisoformat('2026-11-09T00:00:00+00:00')
        waits=[];self.b.sleep=waits.append
        self.b.reserve('adzuna')
        self.assertAlmostEqual(waits[0],3.1)

    def test_real_processes_and_crashed_reservations_share_the_cap(self):
        import multiprocessing
        with concurrent.futures.ProcessPoolExecutor(max_workers=4,mp_context=multiprocessing.get_context('spawn')) as pool:
            results=list(pool.map(_process_reserve,[str(self.path)]*24))
        self.assertEqual(sum(results),10)
        self.assertEqual(self.b.summary()['providers']['adzuna']['reserved'],10)
        with self.assertRaises(budget.Deferred):self.b.reserve('adzuna')

    def test_indexed_budget_queries_do_not_scan_old_attempts(self):
        def measure():
            conn=self.b.connection();steps=[0]
            conn.set_progress_handler(lambda:steps.__setitem__(0,steps[0]+1) or 0,1)
            with patch.object(self.b,'connection',return_value=conn):self.b.reserve('ashby')
            return steps[0]
        first=measure()
        with sqlite3.connect(self.path) as c:
            c.executemany('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',[(str(i),'ashby','2025-01-01','2025-01-01T00:00:00+00:00','scan','','200','read') for i in range(10000)])
        second=measure()
        self.assertLess(second,first*1.10+20)

    def test_invalid_policy_and_missing_accounting_are_explicit_in_diagnostics(self):
        import collection_reads
        self.assertEqual(collection_reads.summary(self.path.with_name('missing'),self.policy)['providers']['adzuna']['used'],None)
        invalid=Path(self.tmp.name)/'invalid.yml';invalid.write_text('version: [broken')
        with self.assertRaises(ValueError):policy.load(invalid)
        from copy import deepcopy
        alterations=(lambda p:p['providers']['reed'].update(search_limit=5),
                     lambda p:p['providers']['ashby'].update(refresh_days=30),
                     lambda p:p['providers']['ashby'].update(daily_limit=0),
                     lambda p:p['providers']['adzuna']['account'].update(day='many'))
        for change in alterations:
            p=policy.load();change(p)
            with self.assertRaises(ValueError):policy.validate(p)


def _process_reserve(path):
    p=policy.load();p['providers']['adzuna']['min_interval_seconds']=0
    try:budget.Budget(path,p,now=lambda:dt.datetime.fromisoformat(AT)).reserve('adzuna');return 1
    except budget.Deferred:return 0
