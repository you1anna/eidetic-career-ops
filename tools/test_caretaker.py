import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workspace'))
import db
import caretaker
import records
from email_worker import save_receipt

class CaretakerCase(unittest.TestCase):
    def setUp(self):

        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name);self.conn=db.connect(self.data/'test.db');db.migrate(self.conn);self.addCleanup(self.conn.close)
        self.now=dt.datetime(2026,10,19,10,tzinfo=dt.timezone.utc)
        self.units={u:{'ActiveState':'active','Result':'success'} for u in caretaker.UNITS}
        self.good()

    def good(self):
        at=self.now.isoformat()
        for lane in ('email','research','ingest'):
            save_receipt(self.conn,lane+':'+at,dict(status='ok',started_at=at,finished_at=at))
        (self.data/'backups').mkdir(exist_ok=True)
        (self.data/'backups'/'last-backup.json').write_text(json.dumps({'at':at}))

    def run_checks(self):
        return caretaker.inspect(self.conn,self.data,self.now,units=self.units,health=True)


class CaretakerTests(CaretakerCase):
    def test_request_capped_source_remains_a_gap_with_its_real_stop_reason(self):
        import source_coverage
        at = self.now.isoformat()
        save_receipt(self.conn, 'ingest:' + at, dict(status='partial', started_at=at, finished_at=at, sources={
            'feed:adzuna:test': dict(state='partial', complete=False, items=18, stop_reason='budget')}))
        check = next(c for c in self.run_checks() if c['id'] == 'source:feed:adzuna:test')
        self.assertEqual((check['status'], check['verified_at'], check['detail']),
                         ('partial', None, 'Read partly (request cap); not a failure.'))
        row = source_coverage.describe_source(dict(scope='feed:adzuna:test', state='partial', item_count=18,
            observed_at=at, detail=json.dumps({'name': 'Example feed', 'stop_reason': 'budget'})))
        self.assertFalse(row['problem'])
        self.assertEqual(row['summary'], 'read 18 items on 2026-10-19, then stopped at the request limit (a partial read, not a failure)')

    def test_healthy_is_zero_model_and_offhost_unknown_is_explicit(self):
        checks=self.run_checks()
        self.assertFalse([c for c in checks if c['status']=='failed'])
        off=next(c for c in checks if c['id']=='offhost')
        self.assertEqual(off['status'],'unknown')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM codex_invocations').fetchone()[0],0)

    def test_overdue_email_incident_deduplicates_and_requires_new_success(self):
        self.now+=dt.timedelta(hours=2)
        checks=self.run_checks()
        first=caretaker.record(self.conn,checks,self.now)
        again=caretaker.record(self.conn,checks,self.now)
        self.assertIn('email',first['raised']);self.assertNotIn('email',again['raised'])
        at=self.now.isoformat()
        save_receipt(self.conn,'email:'+at,dict(status='failed',started_at=at,finished_at=at))
        self.assertNotIn('email',caretaker.record(self.conn,self.run_checks(),self.now)['cleared'])
        self.now+=dt.timedelta(minutes=1);self.good()
        self.assertIn('email',caretaker.record(self.conn,self.run_checks(),self.now)['cleared'])

    def test_the_site_projection_names_the_same_incident_as_the_card_it_raised(self):
        import record_views
        self.now+=dt.timedelta(hours=2)
        checks=self.run_checks()
        self.assertIn('email',caretaker.record(self.conn,checks,self.now)['raised'])
        at=self.now.isoformat()
        save_receipt(self.conn,'caretaker:'+at,dict(status='failed',started_at=at,finished_at=at,checks=checks))
        projected={c['id']:c for c in record_views.caretaker_status(self.conn)['checks']}
        card=self.conn.execute("SELECT subject FROM decisions WHERE kind='failure' AND status='open'").fetchone()['subject']
        self.assertEqual(projected['email']['subject'],card)
        self.assertEqual(card,'workspace:caretaker-email')
        self.assertEqual(projected['origin']['subject'],'workspace:caretaker-origin')  # no card, same identity rule

    def test_running_and_stalled_research_fail_but_a_completed_partial_run_recovers(self):
        # 22 Oct: research nearly always ends partial (unverified routes); a completed run is recovery.
        at=self.now.isoformat()
        save_receipt(self.conn,'research:'+at,dict(status='running',started_at=at))
        self.assertEqual(next(c for c in self.run_checks() if c['id']=='research')['status'],'running')
        self.now+=dt.timedelta(minutes=51)
        self.assertEqual(next(c for c in self.run_checks() if c['id']=='research')['status'],'failed')
        caretaker.record(self.conn,self.run_checks(),self.now)
        self.now+=dt.timedelta(minutes=1);at=self.now.isoformat()
        save_receipt(self.conn,'research:'+at,dict(status='partial',started_at=at,finished_at=at,backlog=4))
        self.now+=dt.timedelta(minutes=1)
        c=next(c for c in self.run_checks() if c['id']=='research')
        self.assertEqual((c['status'],c['verified_at']),('ok',at))
        self.assertIn('research',caretaker.record(self.conn,self.run_checks(),self.now)['cleared'])

    def test_a_stopped_pass_waits_and_names_its_own_call_cap_apart_from_the_account_allowance(self):
        # A stop reason is not recovery evidence, whichever limit stopped the pass.
        at=self.now.isoformat()
        save_receipt(self.conn,'research:'+at,dict(status='partial',started_at=at,finished_at=at,checked=4,backlog=3,
                                                   reason='usage_limit',usage={'calls':6},limits={'max_calls':6}))
        c=next(c for c in self.run_checks() if c['id']=='research')
        self.assertEqual((c['status'],c['verified_at']),('waiting',None))
        self.assertIn('per-pass call cap',c['detail'])
        self.assertNotIn('research',caretaker.record(self.conn,self.run_checks(),self.now)['cleared'])
        later=(self.now+dt.timedelta(minutes=1)).isoformat()
        save_receipt(self.conn,'research:'+later,dict(status='partial',started_at=later,finished_at=later,checked=1,backlog=6,
                                                      reason='usage_limit',usage={'calls':1},limits={'max_calls':6}))
        c=next(c for c in self.run_checks() if c['id']=='research')
        self.assertEqual((c['status'],c['detail']),('waiting','Waiting for Codex allowance; existing evidence still expires.'))

    def test_stopped_timer_and_capacity_and_pause_are_visible(self):
        self.units['wo-email.timer']['ActiveState']='inactive'
        at=self.now.isoformat()
        save_receipt(self.conn,'email:'+at,dict(status='ok',started_at=at,finished_at=at,spool={'count':800},collection={'pending':1},processing={'quarantined':1}))
        checks=self.run_checks()
        self.assertEqual(next(c for c in checks if c['id']=='unit:wo-email.timer')['status'],'failed')
        self.assertEqual(next(c for c in checks if c['id']=='email-capacity')['status'],'failed')
        self.assertEqual(next(c for c in checks if c['id']=='email-backlog')['status'],'failed')
        (self.data/'research.pause').touch()
        self.assertEqual(next(c for c in self.run_checks() if c['id']=='research')['status'],'paused')

    def test_future_timestamp_is_not_healthy_and_parked_incident_is_respected(self):
        (self.data/'backups'/'last-backup.json').write_text(json.dumps({'at':'2099-01-01T00:00:00Z'}))
        checks=self.run_checks();self.assertEqual(next(c for c in checks if c['id']=='backup')['status'],'failed')
        caretaker.record(self.conn,checks,self.now)
        d=self.conn.execute("SELECT * FROM decisions WHERE subject='workspace:caretaker-backup'").fetchone()
        records.answer_decision(self.conn,d['id'],{'choice':'wait','revision':d['revision']},actor='owner')
        records.process_decision(self.conn,d['id'],'done','Left for now.',revision=d['revision']+1)
        self.assertNotIn('backup',caretaker.record(self.conn,checks,self.now)['raised'])

    def test_partial_source_cannot_clear_and_new_failure_after_recovery_can_reopen(self):
        from unittest.mock import patch
        checks=[dict(id='backup',label='Backup',status='failed',detail='failed',verified_at=None)]
        caretaker.record(self.conn,checks,self.now)
        d=self.conn.execute("SELECT * FROM decisions WHERE subject='workspace:caretaker-backup'").fetchone()
        # Answered and processed at the fixture's clock, so the recovery a minute later follows them.
        with patch('db.now',return_value=self.now.isoformat()):
            records.answer_decision(self.conn,d['id'],{'choice':'wait','revision':d['revision']},actor='owner')
            records.process_decision(self.conn,d['id'],'done','Left for now.',revision=d['revision']+1)
        self.now+=dt.timedelta(minutes=1)
        good=[dict(checks[0],status='ok',verified_at=self.now.isoformat())]
        caretaker.record(self.conn,good,self.now)
        self.assertIn('backup',caretaker.record(self.conn,checks,self.now)['raised'])
        at=self.now.isoformat()
        save_receipt(self.conn,'ingest:'+at,dict(status='partial',started_at=at,finished_at=at,sources={
            'board:test':{'state':'partial','complete':False},
            'board:down':{'state':'error','complete':False},
        }))
        by_id={c['id']:c for c in self.run_checks()}
        self.assertEqual(by_id['source:board:test']['status'],'partial')
        self.assertEqual(by_id['source:board:test']['detail'],'Read partly (page cap); not a failure.')
        self.assertEqual(by_id['source:board:down']['status'],'failed')
        self.assertEqual(by_id['source:board:down']['detail'],'Source failed; its failure decision on the Dashboard holds evidence and recovery.')

    def test_published_receipt_is_safely_projected_and_pause_is_zero_work(self):
        import record_views
        with patch.object(caretaker,'inspect',return_value=self.run_checks()):
            receipt=caretaker.run(self.data/'test.db',self.data,True)
        projected=record_views.caretaker_status(self.conn)
        self.assertEqual(projected['model_calls'],0);self.assertEqual(projected['status'],receipt['status'])
        self.assertNotIn('fingerprint',projected['checks'][0])
        (self.data/'caretaker.pause').touch()
        with patch.object(caretaker,'inspect') as inspect:
            self.assertEqual(caretaker.run(self.data/'test.db',self.data,True)['status'],'paused')
        inspect.assert_not_called()

    def test_restored_old_success_receipt_cannot_clear_newer_failure(self):
        old=self.now.isoformat()
        self.now+=dt.timedelta(minutes=5)
        c=dict(id='backup',label='Backup',status='failed',detail='Missing',verified_at=None)
        caretaker.record(self.conn,[c],self.now)
        self.now+=dt.timedelta(minutes=1)
        self.assertNotIn('backup',caretaker.record(self.conn,[dict(c,status='ok',verified_at=old)],self.now)['cleared'])
        self.assertIn('backup',caretaker.record(self.conn,[dict(c,status='ok',verified_at=self.now.isoformat())],self.now)['cleared'])


class CaretakerReceiptTests(unittest.TestCase):
    """Each 15-minute receipt stores only the checks that changed; every receipt's full list is rebuilt exactly."""
    setUp = CaretakerTests.setUp
    good = CaretakerTests.good

    def publish(self, change=lambda checks: checks, minutes=5):
        """One recorded run, later than the last, with the real checks for that time."""
        self.now += dt.timedelta(minutes=minutes)
        real = caretaker.inspect
        def inspect(conn, data, now):
            return change(real(conn, data, now, units=self.units, health=True))
        with patch.object(caretaker, 'inspect', side_effect=inspect):
            return caretaker.run(self.data/'test.db', self.data, True, now=self.now)

    def stored(self):
        return [(r[0], json.loads(r[1])) for r in self.conn.execute(
            "SELECT run_id, receipt_json FROM scan_runs WHERE run_id LIKE 'caretaker:%' ORDER BY run_id")]

    def assertRebuilt(self, published):
        import record_views
        for (run_id, _), receipt in zip(self.stored(), published, strict=True):
            self.assertEqual(record_views.caretaker_checks(self.conn, run_id), receipt['checks'])

    def test_only_changed_checks_are_stored_and_each_receipt_rebuilds_exactly(self):
        import record_views
        down = lambda checks: [dict(c, status='failed', detail='Origin health unavailable.') if c['id'] == 'origin' else c for c in checks]
        published = [self.publish(), self.publish(), self.publish(down)]
        (first_id, first), (_, second), (_, third) = self.stored()
        self.assertNotIn('base', first)
        self.assertEqual((second['base'], second['checks'], second['unchanged']), (first_id, [], len(published[1]['checks'])))
        self.assertEqual([c['id'] for c in third['checks']], ['origin'])
        self.assertLess(len(json.dumps(second)), len(json.dumps(first)) / 5)
        self.assertRebuilt(published)
        status = record_views.caretaker_status(self.conn)
        self.assertEqual(status['status'], 'failed')
        self.assertEqual([c['status'] for c in status['checks'] if c['id'] == 'origin'], ['failed'])
        self.assertEqual(len(status['checks']), len(published[2]['checks']))

    def test_added_removed_and_reordered_checks_rebuild_exactly(self):
        extra = dict(id='new', label='New check', status='ok', detail='Added.', verified_at=None, subject=None, fingerprint='f')
        published = [self.publish(), self.publish(lambda checks: list(reversed(checks))[1:] + [extra]), self.publish()]
        self.assertIn('ids', self.stored()[1][1])
        self.assertRebuilt(published)

    def test_a_full_list_is_stored_at_least_daily(self):
        import record_views
        with patch.object(record_views, 'CARETAKER_FULL_EVERY', 3):
            published = [self.publish() for _ in range(5)]
        rows = self.stored()
        self.assertEqual([r.get('base') for _, r in rows], [None, rows[0][0], rows[1][0], None, rows[3][0]])
        self.assertRebuilt(published)

    def test_a_broken_chain_is_reported_and_the_next_receipt_is_full(self):
        import record_views
        self.publish()
        second = self.publish()
        (first_id, _), (second_id, _) = self.stored()
        self.conn.execute('DELETE FROM scan_runs WHERE run_id=?', (first_id,))
        self.conn.commit()
        self.assertIsNone(record_views.caretaker_checks(self.conn, second_id))
        status = record_views.caretaker_status(self.conn)
        self.assertEqual(status['status'], second['status'])
        self.assertEqual([c['status'] for c in status['checks']], ['unknown'])
        self.publish()
        self.assertNotIn('base', self.stored()[-1][1])


class RetiredPreparationCaretakerTests(CaretakerCase):
    def test_old_preparation_receipts_do_not_create_new_failures(self):
        at=(self.now-dt.timedelta(hours=18)).isoformat()
        save_receipt(self.conn,'research:'+at,dict(status='partial',started_at=at,finished_at=at,
            preparation=dict(finished_at=at,counts={'failed':5},people=[])))
        before=self.conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id=?",('research:'+at,)).fetchone()[0]
        self.assertFalse(any(c['id']=='preparation' for c in self.run_checks()))
        self.assertEqual(self.conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id=?",('research:'+at,)).fetchone()[0],before)
