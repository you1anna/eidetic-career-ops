"""Opportunity research spends effort on evidence and never recreates outgoing wording."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import db
import records
import research_worker as worker
import research_codex
import brief_research
import evidence_briefs
import wo
import io
from contextlib import redirect_stderr, redirect_stdout


class OpportunityResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.db'
        self.conn = db.connect(self.path)
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def job(self, **extra):
        values = dict(type='job', name='Software Engineering Manager', status='pursue',
                      engagement='contract', url='https://example.com/jobs/engineering')
        values.update(extra)
        return records.create(self.conn, values, actor='agent')['record']

    def test_research_uses_shared_work_selection_and_never_personal_week(self):
        jobs = [dict(id=i, type='job', status='candidate', contacts=[]) for i in ('J1', 'J2', 'J3')]
        person = dict(id='P1', type='person', name='Example Person')
        state = dict(records=[person, *jobs], links=[], opportunities=dict(research_ids=['J2', 'J3']),
                     intelligence={'week': {'focus_ids': ['a1'], 'items': [
                         dict(action_id='a1', record_id='P1', state='planned')]}})
        selected = worker.focus_work(self.conn, jobs, state)
        self.assertEqual({r['id'] for r in selected}, {'J2', 'J3'})
        self.assertFalse(any(r['type'] == 'person' for r in selected))

    def test_alert_qualification_uses_saved_attempts_in_the_real_state_build(self):
        from pending_store import ObservationStore
        import datetime as dt
        folder = self.path.parent / 'pending'
        ObservationStore(folder / 'observations.json').observe(
            scope='alert:linkedin', source_type='job', provider='linkedin_alert',
            observed_at='2026-10-28T08:00:00Z', state='ok', complete=True, absence_authoritative=False,
            items=[dict(source_id=str(i), current=dict(title='Engineering Manager', company=f'Example {i}',
                   location='London', eligible=True, url=f'https://example.test/job/{i}')) for i in range(12)])
        with patch('db.now', return_value='2026-10-28T08:00:00Z'), patch('evidence_checks.utc_now',
                return_value=dt.datetime(2026,10,28,8,tzinfo=dt.timezone.utc)):
            wo.cmd_import_scans(self.conn, folder)
        with patch('approaches.london_today', return_value=dt.date(2026,10,28)):
            before = records.state_context(self.conn)
            first = before['opportunities']['research_ids']
            self.assertEqual(len(first), 10)
            self.assertEqual(before['opportunities']['discovery'], [])
            with self.conn:
                self.conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                    ('research:qualification-test','2026-10-28','partial',json.dumps(dict(items=[
                        dict(id=rid,attempted_at='2026-10-28T09:00:00Z',outcome='unverified') for rid in first[:8]]))))
            after = records.state_context(self.conn)
            selected = after['opportunities']['research_ids']
            unattempted = {r['id'] for r in before['records'] if r['type']=='job'} - set(first[:8])
            self.assertEqual(len(unattempted), 4)
            self.assertEqual(set(selected[:4]), unattempted)
            self.assertEqual(len(selected), 10)
            self.assertEqual(set(r['id'] for r in worker.select_work(self.conn, after)), set(selected))

    def test_old_finding_note_cannot_create_or_replace_a_draft(self):
        job = self.job()
        at = worker.sources.stamp()
        finding = dict(id=job['id'], reviewed_at=at, source_urls=[job['url']],
                       facts=['This is a software engineering management role.'])
        job['context_snapshot']=worker.brief_research.snapshot(self.conn,dict(records.get(self.conn,job['id']),contacts=job.get('contacts',[])))
        worker.apply_finding(self.conn, job, finding, 'research:vps:retired-note',
                             {'text': 'An old generated message', 'recipient_id': ''})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM drafts').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM draft_versions').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_message_preparation_operation_is_refused_before_runner_execution(self):
        with self.assertRaisesRegex(ValueError, 'operation|retired'):
            research_codex.validate_request({'operation': 'preparation', 'prompt': {}})

    def test_retired_cli_writes_leave_records_and_history_unchanged(self):
        job = self.job()
        before = list(self.conn.iterdump())
        for args in (['draft', job['id'], '--text-file', '-'], ['week', 'include', job['id']],
                     ['decision', 'add', job['id'], '--kind', 'draft', '--question', 'Approve this?']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
                result = wo.main(['--db', str(self.path), *args])
                self.assertEqual(result, 2)
                self.assertIn('retired', err.getvalue().lower())
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_work_evidence_does_not_demand_relationship_or_personal_ask(self):
        for kind in ('job', 'company', 'opportunity', 'tender'):
            with self.subTest(kind=kind):
                view = evidence_briefs.build(dict(id='work', type=kind, name='Example Work',
                    url='https://example.com/work', summary='A specific software integration remit.'))
                self.assertFalse({'relationship', 'ask', 'last_contact'} & {g['code'] for g in view['gaps']})

    def test_evidence_replay_derives_and_validates_outside_the_writer_transaction(self):
        job = dict(self.job(), contacts=[])
        snapshot = brief_research.snapshot(self.conn, job, focused=False)
        result = dict(vacancy={}, contacts=[], candidates=[])
        sources = [dict(url=job['url'], checked_at=worker.sources.stamp(), ok=True,
                        text='Software Engineering Manager is advertised.', reason='read')]
        finding = brief_research.checked_result(job, json.loads(json.dumps(result)), sources)
        outcome = dict(result=result, sources=sources, finding=finding)
        observed = []
        original = records.state_context
        def state(conn):
            observed.append(conn.in_transaction)
            return original(conn)
        checked = brief_research.checked_result
        def validate(*args):
            observed.append(self.conn.in_transaction)
            return checked(*args)
        with patch.object(records, 'state_context', side_effect=state), \
             patch.object(brief_research, 'checked_result', side_effect=validate):
            imported = brief_research.apply(self.conn, snapshot, outcome, 'research:vps:brief-test')
        self.assertIn(imported['outcome'], ('updated', 'unchanged'))
        self.assertTrue(observed)
        self.assertFalse(any(observed), 'Whole-state building and source validation must precede BEGIN IMMEDIATE')


if __name__ == '__main__':
    unittest.main()


class WorkResearchLifecycleTests(unittest.TestCase):
    """Exercise real selection, accounting, retention and import; only public I/O is faked."""
    def setUp(self):
        OpportunityResearchTests.setUp(self)
        self.work = self.job(organisation='Example Labs', summary='Lead software delivery for the API platform.')
        self.source = dict(url=self.work['url'], ok=True, reason='read', checked_at=worker.sources.stamp(),
            text='Example Labs is hiring a Software Engineering Manager. Apply now for this contract role.')
        self.limits = dict(enabled=True, focused=True, briefs=True, prepare=True, max_calls=6)

    job = OpportunityResearchTests.job

    def response(self):
        return dict(vacancy=dict(status='confirmed', source=0, quote=self.source['text'], basis='Advert'),
                    contacts=[], candidates=[], unknowns=[])

    def exchange(self, request):
        self.assertEqual(request['operation'], 'brief')
        return dict(ok=True, result=self.response(), usage=dict(input_tokens=100, output_tokens=50))

    def test_real_work_pass_reuses_current_evidence_and_builds_one_state(self):
        original = records.state_context
        with patch.object(brief_research.memo, 'read_source', return_value=self.source), \
             patch.object(worker.model, 'exchange', side_effect=self.exchange) as exchange, \
             patch.object(records, 'state_context', wraps=original) as state:
            first = worker.run(self.path, self.path.parent, None, self.limits)
            self.assertEqual(state.call_count, 1)
            second = worker.run(self.path, self.path.parent, None, self.limits)
            self.assertEqual(state.call_count, 2)
        self.assertEqual(first['status'], 'ok', first)
        self.assertEqual(first['briefs']['sufficient'], 1)
        self.assertEqual(second['current'], 1, second)
        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(first['usage']['calls'], 1)
        self.assertEqual(second['usage']['calls'], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM drafts').fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM decisions WHERE kind='draft'").fetchone()[0], 0)

    def test_legacy_focus_flag_cannot_expand_research_beyond_shared_selection(self):
        with patch.object(worker,'focus_work',return_value=[]) as focus, patch.object(worker.model,'exchange') as exchange, \
             patch.object(brief_research.memo,'read_source',return_value=self.source):
            receipt=worker.run(self.path,self.path.parent,None,dict(self.limits,focused=False))
        focus.assert_called_once()
        exchange.assert_not_called()
        self.assertEqual(receipt['total'],0)

    def test_crash_replays_evidence_without_new_call_or_changed_read_time(self):
        with patch.object(brief_research.memo, 'read_source', return_value=self.source), \
             patch.object(worker.model, 'exchange', side_effect=self.exchange), \
             patch.object(brief_research, 'apply', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker.run(self.path, self.path.parent, None, self.limits)
        with patch.object(worker.model, 'exchange') as exchange:
            self.assertEqual(worker.recover_batches(self.conn, self.path.parent), 1)
            self.assertEqual(worker.recover_batches(self.conn, self.path.parent), 0)
        exchange.assert_not_called()
        check=records.get(self.conn,self.work['id'])['evidence_checks']['vacancy']
        self.assertEqual(check['checked_at'],self.source['checked_at'])
        self.assertEqual(check['status'],'confirmed')

    def test_exhausted_allowance_preserves_waiting_work_and_never_dispatches(self):
        import automation_usage as quota
        for _ in range(12):quota.reserve(self.conn,'existing','research',worker.sources.stamp())
        with patch.object(brief_research.memo,'read_source',return_value=self.source), \
             patch.object(worker.model,'exchange') as exchange:
            receipt=worker.run(self.path,self.path.parent,None,self.limits)
        exchange.assert_not_called()
        self.assertEqual((receipt['status'],receipt['reason']),('blocked','usage_limit'))
        self.assertIn(self.work['id'],receipt['waiting_ids'])

    def test_skip_after_snapshot_prevents_import(self):
        work=dict(self.work,contacts=[])
        saved=brief_research.snapshot(self.conn,work,focused=True)
        result=self.response()
        outcome=dict(result=result,sources=[self.source],finding=brief_research.checked_result(work,result,[self.source]))
        records.transition(self.conn,work['id'],dict(field='status',to='skip',revision=work['revision']),actor='owner')
        self.assertEqual(brief_research.apply(self.conn,saved,outcome,'research:vps:skip')['outcome'],'no_longer_active')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM observations').fetchone()[0],0)

    def test_oldest_attempt_rotates_blocked_targets_and_active_wins_ties(self):
        jobs=[dict(id=i,type='job',contacts=[]) for i in ('new','blocked','active')]
        state=dict(records=jobs,opportunities=dict(research_ids=['new','blocked','active'],
            active=[dict(record_id='active')]),research_attempts={
            'blocked':dict(attempted_at='2026-10-25',outcome='failed')})
        self.assertEqual([j['id'] for j in worker.focus_work(self.conn,jobs,state)],['active','new','blocked'])


class WorkRouteScopeTests(unittest.TestCase):
    setUp = OpportunityResearchTests.setUp
    def test_company_route_checks_require_existing_link_and_cannot_claim_a_vacancy(self):
        import research_intake
        company=records.create(self.conn,dict(type='company',name='Example Labs'),actor='agent')['record']
        person=records.create(self.conn,dict(type='person',name='Example Person'),actor='agent')['record']
        records.link(self.conn,dict(from_id=person['id'],to_id=company['id'],relation='works_at',revision=person['revision']),actor='agent')
        check=dict(scope='contact_route',person_id=person['id'],organisation_id=company['id'])
        research_intake.validate_signal_targets(self.conn,company['id'],dict(freshness_checks=[check]))
        unrelated=records.create(self.conn,dict(type='company',name='Unrelated'),actor='agent')['record']
        with self.assertRaises(ValueError):
            research_intake.validate_signal_targets(self.conn,unrelated['id'],dict(freshness_checks=[check]))
        with self.assertRaises(ValueError):
            research_intake.validate_signal_targets(self.conn,company['id'],dict(freshness_checks=[dict(scope='vacancy')]))


class DirectWorkResearchTests(unittest.TestCase):
    setUp = OpportunityResearchTests.setUp

    def test_confirmed_discovery_job_remains_selected_after_research(self):
        job=OpportunityResearchTests.job(self,status='candidate',organisation='Example Labs',
            name='Engineering Manager',location='London',pay='£750/day',date=worker.sources.stamp()[:10],
            summary='Lead software engineering teams and improve delivery quality.')
        self.assertIn(job['id'],records.state_context(self.conn)['opportunities']['research_ids'])

        quote='Example Labs is hiring an Engineering Manager. Apply now for this contract role.'
        source=dict(url=job['url'],ok=True,reason='read',checked_at=worker.sources.stamp(),text=quote)
        result=dict(vacancy=dict(status='confirmed',source=0,quote=quote,basis='Advert'),contacts=[],candidates=[])
        target=dict(job,contacts=[])
        outcome=dict(result=result,sources=[source],finding=brief_research.checked_result(target,result,[source]))
        imported=brief_research.apply(self.conn,brief_research.snapshot(self.conn,target),outcome,'research:vps:discovery')
        self.assertEqual(imported['outcome'],'updated')
        self.assertIn(job['id'],records.state_context(self.conn)['opportunities']['research_ids'])

    def test_closed_advert_leaves_research_until_a_real_hiring_process_continues(self):
        import record_imports
        job=OpportunityResearchTests.job(self,status='candidate',organisation='Example Labs',
            location='London',pay='£750/day',date=worker.sources.stamp()[:10],
            summary='Lead software engineering teams and improve delivery quality.')
        finding=dict(id=job['id'],reviewed_at=worker.sources.stamp(),source_urls=[job['url']],
            freshness_checks=[dict(scope='vacancy',status='closed',checked_at=worker.sources.stamp(),
                source_url=job['url'],basis='Applications have closed for this position.')])
        record_imports.import_research_entries(self.conn,[finding],'research:closed')
        projection=records.state_context(self.conn)['opportunities']
        self.assertNotIn(job['id'],projection['research_ids'])
        self.assertIn(job['id'],[r['record_id'] for r in projection['closed']])
        records.transition(self.conn,job['id'],dict(field='status',to='pursue',revision=records.get(self.conn,job['id'])['revision']),actor='owner')
        self.assertNotIn(job['id'],records.state_context(self.conn)['opportunities']['research_ids'])
        records.transition(self.conn,job['id'],dict(field='status',to='applied',revision=records.get(self.conn,job['id'])['revision']),actor='owner')
        self.assertNotIn(job['id'],records.state_context(self.conn)['opportunities']['research_ids'])
        records.transition(self.conn,job['id'],dict(field='status',to='responded',revision=records.get(self.conn,job['id'])['revision']),actor='owner')
        self.assertIn(job['id'],records.state_context(self.conn)['opportunities']['research_ids'])

    def test_direct_remit_survives_repeated_validated_briefs_beyond_recent_reviews(self):
        company,_,text=self.company()
        for i in range(4):
            target=dict(records.get(self.conn,company['id']),contacts=[])
            source=dict(url='https://example.com/need',ok=True,reason='read',checked_at=worker.sources.stamp(),text=text)
            result=dict(vacancy={},contacts=[],candidates=[dict(subject_id=company['id'],kind='context',
                value=text,quote=text,source=0)])
            outcome=dict(result=result,sources=[source],finding=brief_research.checked_result(target,result,[source]))
            brief_research.apply(self.conn,brief_research.snapshot(self.conn,target),outcome,f'research:vps:remit-{i}')
            self.assertIn(company['id'],records.state_context(self.conn)['opportunities']['research_ids'])

    def test_alternative_research_rejects_new_exclusion_and_unsafe_legacy_context(self):
        job=OpportunityResearchTests.job(self)
        target=dict(job,contacts=[],context_snapshot=brief_research.snapshot(self.conn,dict(job,contacts=[])))
        finding=dict(id=job['id'],reviewed_at=worker.sources.stamp(),source_urls=[job['url']],facts=['Synthetic work evidence.'])
        excluded=records.create(self.conn,dict(type='company',name='Excludo'),actor='agent')['record']
        records.link(self.conn,dict(from_id=excluded['id'],to_id=job['id'],relation='related',revision=excluded['revision']),actor='owner')
        self.assertEqual(worker.apply_finding(self.conn,target,finding,'research:vps:alternate',None)['outcome'],'no_longer_active')
        self.assertEqual(worker.apply_finding(self.conn,job,finding,'research:vps:legacy',None)['outcome'],'context_changed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM observations').fetchone()[0],0)

    def company(self, contact=False):
        import record_imports
        company=records.create(self.conn,dict(type='company',name='Example Labs',
            summary='Improve release readiness and incident rate for existing software.'),actor='agent')['record']
        text='Example Labs needs to improve release readiness and incident rate for existing software.'
        record_imports.import_research_entries(self.conn,[dict(id=company['id'],reviewed_at=worker.sources.stamp(),
            source_urls=['https://example.com/need'],signal=text)],'research:fixture')
        person=None
        if contact:
            person=records.create(self.conn,dict(type='person',name='Alex Example',url='https://example.com/team'),actor='agent')['record']
            records.link(self.conn,dict(from_id=person['id'],to_id=company['id'],relation='works_at',revision=person['revision']),actor='agent')
        return records.get(self.conn,company['id']),person,text

    def run_company(self, company, person, text, briefs=True):
        source=dict(url='https://example.com/need',ok=True,reason='read',checked_at=worker.sources.stamp(),
            text=text+' Alex Example works at Example Labs.')
        def exchange(request):
            self.assertEqual(request['operation'],'brief')
            contacts=[] if person is None else [dict(person_id=person['id']+'@'+company['id'],claim=dict(
                status='confirmed',source=0,quote='Alex Example works at Example Labs.',basis='Current role'))]
            return dict(ok=True,result=dict(vacancy=dict(status='unverified',source=0,quote='',basis=''),
                contacts=contacts,candidates=[dict(subject_id=company['id'],kind='context',value=text,quote=text,source=0)],
                unknowns=[]),usage=dict(input_tokens=100,output_tokens=50))
        with patch.object(brief_research.memo,'read_source',return_value=source) as read, \
             patch.object(worker.model,'exchange',side_effect=exchange) as dispatch:
            receipt=worker.run(self.path,self.path.parent,None,dict(enabled=True,focused=True,briefs=briefs,max_calls=6))
        return receipt,read,dispatch

    def test_direct_remit_without_homepage_reads_its_source_and_reuses_contactless_evidence(self):
        company,person,text=self.company()
        self.assertIn(company['id'],records.state_context(self.conn)['opportunities']['research_ids'])
        first,read,dispatch=self.run_company(company,person,text)
        self.assertTrue(read.called,first)
        self.assertEqual(first['usage']['calls'],1,first)
        self.assertEqual(first['items'][0]['outcome'],'confirmed',first)
        second,read,dispatch=self.run_company(company,person,text)
        self.assertEqual(second['current'],1,second)
        self.assertFalse(read.called)
        self.assertFalse(dispatch.called)

    def test_direct_route_round_trips_and_legacy_alternate_flag_cannot_emit_vacancy(self):
        company,person,text=self.company(contact=True)
        first,_,_=self.run_company(company,person,text,briefs=False)
        self.assertEqual(first['items'][0]['outcome'],'confirmed',first)
        shown=records.get(self.conn,company['id'])
        self.assertIsNone(shown['evidence_checks']['vacancy'])
        self.assertEqual(shown['evidence_checks']['contacts'][0]['status'],'confirmed')
        second,read,dispatch=self.run_company(company,person,text,briefs=False)
        self.assertEqual(second['current'],1,second)
        self.assertFalse(dispatch.called)

    def test_new_dependency_between_validation_and_commit_prevents_import(self):
        job=OpportunityResearchTests.job(self)
        excluded=records.create(self.conn,dict(type='company',name='Excludo'),actor='agent')['record']
        target=dict(job,contacts=[])
        saved=brief_research.snapshot(self.conn,target)
        source=dict(url=job['url'],checked_at=worker.sources.stamp(),ok=True,text='Synthetic public evidence',reason='read')
        result=dict(vacancy={},contacts=[],candidates=[])
        finding=brief_research.checked_result(target,result,[source])
        outcome=dict(result=result,sources=[source],finding=finding)
        original=brief_research.checked_result
        def concurrent_link(*args):
            value=original(*args)
            records.link(self.conn,dict(from_id=excluded['id'],to_id=job['id'],relation='related',
                revision=excluded['revision']),actor='owner')
            return value
        with patch.object(brief_research,'checked_result',side_effect=concurrent_link):
            imported=brief_research.apply(self.conn,saved,outcome,'research:vps:concurrent-exclusion')
        self.assertEqual(imported['outcome'],'context_changed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM observations').fetchone()[0],0)
