"""Automatic intake spends bounded capacity; saved work and evidence survive."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parent.parent/'workspace')]
import db
import records
import record_imports
import evidence_checks
import discovery_intake


class IntakeCapacityCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.conn=db.connect(self.folder/'test.db');db.migrate(self.conn);self.addCleanup(self.conn.close)
        self.now=dt.datetime(2026,10,29,12,tzinfo=dt.timezone.utc)
        legacy=patch.object(discovery_intake.work_selection,'capacity_policy',return_value={'working_limit':10,'additions_per_seven_days':10,'qualification_days':14})
        legacy.start();self.addCleanup(legacy.stop)
        for owner,name,value in [(db,'now',lambda:self.now.isoformat()),(evidence_checks,'utc_now',lambda:self.now)]:
            p=patch.object(owner,name,value);p.start();self.addCleanup(p.stop)

    def stage(self, count=20):
        items={}
        for i in range(count):
            scope='board:example'
            items[scope+'\x1f'+str(i)]=dict(kind='job',source_scope=scope,source_id=str(i),
                first_seen_at='2026-10-29T12:00:00+00:00',last_seen_at=self.now.isoformat(),source_lifecycle='active',
                current=dict(title=f'Engineering Manager {i:02}',company=f'Example {i}',eligible=True,
                    location='London',url=f'https://example.test/{i}',
                    text='Lead software engineering teams and improve release readiness.' if i>=10 else 'Role details to follow.'))
        (self.folder/'observations.json').write_text(json.dumps(dict(sources={},items=items)))

    def jobs(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM records WHERE type='job' ORDER BY name")]


class IntakeCapacityTests(IntakeCapacityCase):
    def test_twenty_candidates_create_only_the_ten_stronger_jobs_and_their_employers(self):
        self.stage();report=record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual([r['name'] for r in self.jobs()],[f'Engineering Manager {i:02}' for i in range(10,20)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='company'").fetchone()[0],10)
        self.assertEqual(report['new_observed'],10)
        self.assertEqual(record_imports.cmd_import_scans(self.conn,self.folder)['new_observed'],0)
        self.assertEqual(len(self.jobs()),10)

    def test_saving_frees_a_place_but_cannot_bypass_the_seven_day_allowance(self):
        self.stage();record_imports.cmd_import_scans(self.conn,self.folder)
        chosen=self.jobs()[0]
        saved=records.transition(self.conn,chosen['id'],dict(revision=chosen['revision'],field='status',to='pursue'))['record']
        records.edit(self.conn,chosen['id'],dict(revision=saved['revision'],notes='Keep this process'))
        self.stage(21)
        record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(len(self.jobs()),10)
        self.now+=dt.timedelta(days=7,seconds=1);self.stage(21)
        report=record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertGreater(report['new_observed'],0)
        self.assertLessEqual(report['new_observed'],10)
        saved=records.get(self.conn,chosen['id'])
        self.assertEqual((saved['status'],saved['notes']),('pursue','Keep this process'))

    def test_existing_low_priority_records_refresh_without_spending_capacity(self):
        self.stage();record_imports.cmd_import_scans(self.conn,self.folder)
        chosen=self.jobs()[0];before=self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0]
        path=self.folder/'observations.json';data=json.loads(path.read_text())
        item=next(i for i in data['items'].values() if i['current']['url']==chosen['url'])
        item['current']['title']='Junior Engineer';item['source_lifecycle']='closed';item['last_seen_at']='2026-10-30T12:00:00Z'
        path.write_text(json.dumps(data));self.now+=dt.timedelta(days=1)
        report=record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(report['new_observed'],0)
        self.assertEqual(len(self.jobs()),10)
        self.assertEqual(records.get(self.conn,chosen['id'])['source_lifecycle'],'closed')
        self.assertGreater(self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0],before)

    def test_ledger_rejects_duplicate_entries_without_writing(self):
        self.stage()
        entry=dict(id='J1',at=self.now.isoformat())
        self.conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',(discovery_intake.KEY,json.dumps([entry,entry])))
        with self.assertRaises(ValueError):
            record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(self.jobs(),[])

    def test_concurrent_manual_save_retries_and_preserves_the_saved_job(self):
        self.stage()
        prepare=discovery_intake.prepare
        attempts=[]
        def intervening(conn,candidates,now):
            self.assertFalse(conn.in_transaction)
            budget=prepare(conn,candidates,now);attempts.append(budget)
            if len(attempts)==1:
                other=db.connect(self.folder/'test.db')
                try:
                    records.save_job(other,dict(name='Personally chosen',url='https://example.test/19',notes='Keep my notes'))
                finally:
                    other.close()
            return budget
        with patch.object(discovery_intake,'prepare',side_effect=intervening):
            report=record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(len(attempts),2)
        self.assertEqual(report['new_observed'],10)
        saved=next(r for r in self.jobs() if r['url']=='https://example.test/19')
        self.assertEqual(saved['notes'],'Keep my notes')
        self.assertEqual(saved['status'],'pursue')
        self.assertEqual(len(self.jobs()),11)

    def test_failure_rolls_back_records_history_and_allowance_then_retry_works(self):
        self.stage();source=(self.folder/'observations.json').read_bytes()
        finish=discovery_intake.Budget.finish
        def fail(budget,conn):
            finish(budget,conn)
            raise RuntimeError('interrupt before commit')
        with patch.object(discovery_intake.Budget,'finish',fail):
            with self.assertRaisesRegex(RuntimeError,'interrupt'):
                record_imports.cmd_import_scans(self.conn,self.folder)
        for table in ('records','state_events','import_runs'):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(discovery_intake.ledger(self.conn,self.now),[])
        self.assertEqual((self.folder/'observations.json').read_bytes(),source)
        self.assertEqual(record_imports.cmd_import_scans(self.conn,self.folder)['new_observed'],10)

    def test_allowance_uses_admission_time_and_expires_at_exactly_seven_days(self):
        self.stage();record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(len(discovery_intake.ledger(self.conn,self.now+dt.timedelta(days=7,microseconds=-1))),10)
        self.assertEqual(discovery_intake.ledger(self.conn,self.now+dt.timedelta(days=7)),[])

    def test_invalid_policy_keeps_records_readable_but_stops_admission(self):
        records.save_job(self.conn,dict(name='My saved role',url='https://example.test/saved'))
        self.stage()
        with patch('relevance.load_config',return_value={}), patch.object(discovery_intake.work_selection,'capacity_policy',side_effect=KeyError('discovery_capacity')):
            state=records.state_context(self.conn)
            self.assertEqual(len(state['records']),1)
            self.assertIsNone(state['opportunities'])
            self.assertIn('discovery_capacity',state['intelligence_error'])
            with self.assertRaises(KeyError):
                record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(len(self.jobs()),1)

    def test_invalid_or_oversized_ledger_stops_before_any_write(self):
        self.stage()
        for value in ({}, [dict(id=str(i),at=self.now.isoformat()) for i in range(1001)],
                      [dict(id='one',at='not a timestamp')], [dict(id=1,at=self.now.isoformat())]):
            with self.subTest(value_type=type(value).__name__,size=len(value)):
                self.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                                  (discovery_intake.KEY,json.dumps(value)))
                with self.assertRaises(ValueError):
                    record_imports.cmd_import_scans(self.conn,self.folder)
                self.assertEqual(self.jobs(),[])

    def test_repeated_concurrent_writes_fail_cleanly_after_three_attempts(self):
        self.stage();prepare=discovery_intake.prepare;attempts=[]
        def competing(conn,candidates,now):
            budget=prepare(conn,candidates,now);attempts.append(budget)
            other=db.connect(self.folder/'test.db')
            try:
                other.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('test_competing_writer',str(len(attempts))))
            finally:
                other.close()
            return budget
        with patch.object(discovery_intake,'prepare',side_effect=competing):
            with self.assertRaisesRegex(records.Conflict,'retry the import'):
                record_imports.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(len(attempts),3)
        self.assertEqual(self.jobs(),[])
        self.assertEqual(discovery_intake.ledger(self.conn,self.now),[])

    def test_manual_candidates_and_old_edits_do_not_create_an_unlimited_review_queue(self):
        for i in range(25):
            records.create(self.conn,dict(type='job',name=f'Engineering Manager {i}',status='candidate',
                organisation='Example',location='London',date=self.now.date().isoformat(),
                summary='Lead software engineering teams and improve release readiness.',
                url=f'https://example.test/manual/{i}',notes='Preserve my notes'),actor='owner')
        before=list(self.conn.iterdump())
        state=records.state_context(self.conn)
        refs=state['opportunities']['discovery']+[r for r in state['opportunities']['other'] if r['rule'] in ('qualify.advert','qualify.lead')]
        self.assertEqual(len(refs),10)
        self.assertEqual(len(state['records']),25)
        self.assertEqual(list(self.conn.iterdump()),before)
        self.assertTrue(all(r['notes']=='Preserve my notes' for r in state['records']))


class ExpandedPoolTests(IntakeCapacityCase):
    def test_expanded_pool_retains_unqualified_roles_without_expanding_visible_work(self):
        self.stage(50)
        policy={'working_limit':10,'research_pool_limit':30,'additions_per_seven_days':60,'qualification_days':14}
        with patch.object(discovery_intake.work_selection,'capacity_policy',return_value=policy):
            report=record_imports.cmd_import_scans(self.conn,self.folder)
            self.assertEqual(report['new_observed'],20)
            self.assertEqual(record_imports.cmd_import_scans(self.conn,self.folder)['new_observed'],0)
            state=records.state_context(self.conn)
        self.assertLessEqual(len(state['opportunities']['discovery'])+len(state['opportunities']['needs_checking']),10)
        self.assertEqual(report['capacity']['allowance_remaining'],40)

class ComparisonTests(IntakeCapacityCase):
    def test_comparison_uses_admission_reasons_and_protects_chosen_processes(self):
        self.stage(); record_imports.cmd_import_scans(self.conn,self.folder)
        chosen=self.jobs()[0]
        records.transition(self.conn,chosen['id'],dict(revision=chosen['revision'],field='status',to='pursue'))
        self.stage(25)
        import intake_review
        before=list(self.conn.iterdump())
        report=intake_review.compare(self.conn,self.folder,pending=self.folder)
        self.assertEqual(report['state'],'ok')
        self.assertEqual(report['allowance_remaining'],0)
        self.assertTrue(report['excluded'])
        self.assertTrue(all(row['deferral'] in ('working_limit','seven_day_limit','not_recommended','unresolved_stale') for row in report['excluded']))
        self.assertNotIn(chosen['id'],[r['record_id'] for r in report['maintained']])
        self.assertLessEqual(len(report['excluded']),6)
        self.assertLessEqual(len(report['maintained']),3)
        self.assertTrue(all('rule' in r and 'uncertainty' in r and 'score' in r for r in report['excluded']))
        self.assertEqual(list(self.conn.iterdump()),before)

    def test_read_error_is_explicit_and_no_comparison_is_invented(self):
        import intake_review
        with self.assertRaisesRegex(ValueError,'could not be read'):
            intake_review.compare(self.conn,self.folder)

    def test_busy_comparison_refuses_to_queue_and_releases_after_failure(self):
        import intake_review
        with intake_review._LOCK:
            with self.assertRaisesRegex(ValueError,'already running'):
                intake_review.compare(self.conn,self.folder)
        with self.assertRaises(ValueError):intake_review.compare(self.conn,self.folder)
        self.assertFalse(intake_review._LOCK.locked())

    def test_input_cap_is_explicit_and_keeps_a_bounded_candidate_set(self):
        import intake_review
        self.stage(20)
        with patch.object(intake_review,'MAX_CANDIDATES',3):
            report=intake_review.compare(self.conn,self.folder,pending=self.folder)
        self.assertEqual((report['complete'],report['eligible_observations'],report['candidate_limit']),(False,20,3))
        self.assertEqual(report['assessed_new'],3)

    def test_comparison_does_not_overlap_a_summary_rebuild(self):
        import intake_review
        import threading
        held,release=threading.Event(),threading.Event()
        def hold():
            with intake_review.workspace_state._BUILD_LOCK:
                held.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start()
        try:
            self.assertTrue(held.wait(2))
            with self.assertRaisesRegex(ValueError,'workspace is rebuilding'):
                intake_review.compare(self.conn,self.folder)
        finally:release.set();thread.join()
        self.assertFalse(intake_review._LOCK.locked())

    def test_saved_sources_do_not_use_the_comparison_input_allowance(self):
        import intake_review
        self.stage();record_imports.cmd_import_scans(self.conn,self.folder)
        # The first ten rich roles are already saved; only unsaved rows consume the input bound.
        path=self.folder/'observations.json';data=json.loads(path.read_text())
        data['items']=dict(sorted(data['items'].items(),key=lambda pair:int(pair[1]['source_id'])<10))
        path.write_text(json.dumps(data))
        with patch.object(intake_review,'MAX_CANDIDATES',3):
            report=intake_review.compare(self.conn,self.folder,pending=self.folder)
        self.assertEqual((report['assessed_new'],report['eligible_observations']),(3,10))

    def test_personal_evidence_wait_is_not_claimed_as_consuming_research(self):
        rows=[dict(id=rid,name=rid,type='job',research_disposition={'scope':scope})
              for rid,scope in [('held','needs_input'),('working','maintained')]]
        refs=[dict(record_id=rid,rule='qualify.advert',reason='Needs public evidence.') for rid in ('held','working')]
        state=dict(records=rows,opportunities=dict(research_ids=['held','working'],other=refs))
        projection=dict(discovery=[],other=refs,active=[],shortlisted=[],closed=[],ranked_ids=['working','held'])
        result=discovery_intake._comparison(state,state,projection,{}, {}, 5)
        self.assertEqual([r['record_id'] for r in result['maintained']],['working'])
