"""Evidence completeness is independent of message wording and draft lifecycle."""
import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workspace'))
import evidence_briefs as briefs

NOW = dt.datetime(2026, 10, 19, 10, tzinfo=dt.timezone.utc)


class BriefTests(unittest.TestCase):
    def test_server_entry_point_imports_without_test_runner_paths(self):
        root=Path(__file__).resolve().parents[1]
        environment={key:value for key,value in os.environ.items() if key!='PYTHONPATH'}
        with tempfile.TemporaryDirectory() as folder:
            result=subprocess.run([sys.executable,str(root/'workspace/server.py'),'--help'],
                cwd=folder,env=environment,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--db',result.stdout)

    def test_public_literal_work_quote_fills_remit_but_does_not_confirm_vacancy_or_fit(self):
        job = dict(id='J1',type='job',name='Engineering Manager',url='https://example.org/job')
        quote = 'Lead two software teams and improve release reliability.'
        item = dict(kind='context',value=quote,quote=quote,subject_id='J1',
                    source_url=job['url'],checked_at='2026-10-19T09:00:00Z')
        evidence = dict(version=1,items=[item],gaps=[],context_sha='x')
        result = briefs.build(job,researched=evidence,now=NOW)
        self.assertNotIn('remit',[g['code'] for g in result['gaps']])
        self.assertIn('vacancy',[g['code'] for g in result['gaps']])
        self.assertFalse(result['sufficient'])
        self.assertEqual(result['items'][0]['provenance'],'public_source')
        self.assertEqual(result['items'][0]['at'],'2026-10-19T09:00:00Z')
        self.assertNotIn('summary',job)
        self.assertNotIn('relevance',job)

    def test_malformed_unrelated_and_nonwork_items_cannot_fill_remit(self):
        job = dict(id='J1',type='job',name='Engineering Manager',url='https://example.org/job')
        quote = 'Lead two software teams and improve release reliability.'
        item = dict(kind='context',value=quote,quote=quote,subject_id='J1',
                    source_url=job['url'],checked_at='2026-10-19T09:00:00Z')
        for change in (dict(kind='employer'),dict(kind='route'),dict(kind='vacancy'),
                       dict(subject_id='other'),dict(source_url='https://mail.google.com/mail/u/0/'),
                       dict(checked_at='unknown'),dict(quote=''),dict(value='Model paraphrase'),dict(quote='Short',value='Short'),
                       dict(value='Pay: GBP 101600–152400/year',quote='Pay: GBP 101600–152400/year'),
                       dict(value='Location: London. Workplace: Hybrid.',quote='Location: London. Workplace: Hybrid.'),
                       dict(value='Build a career you can be proud of.',quote='Build a career you can be proud of.'),
                       dict(kind='role',value='Engineering Manager',quote='Our advertised title is Engineering Manager.'),
                       dict(value='You will not:\nlead software engineering teams.',quote='You will not:\nlead software engineering teams.'),
                       dict(value='You might:\nlead software engineering teams.',quote='You might:\nlead software engineering teams.')):
            with self.subTest(change=change):
                result=briefs.build(job,researched=dict(version=1,items=[dict(item,**change)],gaps=[],context_sha='x'),now=NOW)
                self.assertIn('remit',[g['code'] for g in result['gaps']])

    def test_sourced_role_quote_and_existing_recorded_work_keep_completeness_separate_from_fit(self):
        job = dict(id='J1',type='job',name='Engineering Manager',url='https://example.org/job')
        role = dict(kind='role',value='Engineering Manager',quote='Engineering Manager. Lead software engineering teams.',
                    subject_id='J1',source_url=job['url'],checked_at='2026-10-19T09:00:00Z')
        self.assertNotIn('remit',[g['code'] for g in briefs.build(job,
            researched=dict(version=1,items=[role],gaps=[],context_sha='x'),now=NOW)['gaps']])
        for existing in (dict(summary='Recorded remit'),dict(advert={'title':'Engineering Manager'}),
                         dict(status='interview',incoming=dict(current=True,status='interview',label='Interview'))):
            with self.subTest(existing=existing):
                self.assertNotIn('remit',[g['code'] for g in briefs.build(dict(job,**existing),now=NOW)['gaps']])

    def person(self, **changes):
        return dict(id='P1', type='person', name='Alex Example', known_context='Worked together at Example.',
                    preferred_ask='catch_up', preferred_channel='email', email='alex@example.org',
                    evidence_checks={'contacts': []}, **changes)

    def test_missing_ask_is_not_invented_and_recorded_context_is_not_confirmation(self):
        p = self.person(); p['preferred_ask'] = ''
        result = briefs.build(p, now=NOW)
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['ask'], '')
        self.assertIn('ask', [g['code'] for g in result['gaps']])
        self.assertEqual(result['items'][0]['provenance'], 'recorded')
        self.assertEqual(result['items'][0]['at'], '')

    def test_only_human_edit_provenance_is_owner_confirmed(self):
        p = self.person()
        for actor, expected in [('agent', 'recorded'), ('owner', 'owner_confirmed')]:
            result = briefs.build(p, edits={'known_context': {'actor': actor, 'edited_at': '2026-10-18'}}, now=NOW)
            self.assertEqual(result['items'][0]['provenance'], expected)

    def test_missing_last_contact_and_employer_do_not_block_personal_approach(self):
        result = briefs.build(self.person(), now=NOW)
        self.assertTrue(result['sufficient'])
        self.assertEqual(result['status'], 'available')
        self.assertIn('last_contact', [g['code'] for g in result['gaps'] if not g['blocking']])
        self.assertFalse(result['route']['verified'])

    def test_no_route_is_a_research_gap_not_a_fake_confirmation(self):
        p = self.person(); p['email'] = ''
        result = briefs.build(p, now=NOW)
        self.assertFalse(result['sufficient'])
        self.assertEqual(result['status'], 'pending')

    def test_dates_and_conflicts_are_kept_and_drafts_have_no_effect(self):
        p = self.person(organisation='Old Labs')
        evidence = dict(version=1, items=[dict(kind='employer', value='New Labs', quote='Alex Example works at New Labs.',
                        source_url='https://example.org/team', checked_at='2026-10-19T09:00:00Z', subject_id='P1')],
                        gaps=[], context_sha='x')
        first = briefs.build(p, researched=evidence, now=NOW)
        p['draft'] = 'My message'; p['draft_cleared'] = True
        self.assertEqual(first, briefs.build(p, researched=evidence, now=NOW))
        self.assertTrue(any(g['code'] == 'employer_conflict' for g in first['gaps']))
        self.assertEqual(first['items'][-1]['at'], '2026-10-19T09:00:00Z')

    def test_job_vacancy_ages_without_changing_timestamp(self):
        p = dict(id='J1', type='job', name='Engineer', status='pursue', url='https://example.org/job',
                 evidence_checks={'vacancy': dict(status='confirmed', checked_at='2026-10-17T09:00:00Z',
                                                  source_url='https://example.org/job'), 'contacts': []})
        result = briefs.build(p, now=NOW)
        self.assertFalse(result['sufficient'])
        self.assertIn('vacancy', [g['code'] for g in result['gaps']])

    def test_a_current_source_listing_answers_the_vacancy_question_despite_a_refused_read(self):
        p = dict(id='J1', type='job', name='Engineer', status='candidate', url='https://www.adzuna.co.uk/jobs/land/ad/1',
                 summary='Lead the QA team.', engagement='contract',
                 advert=dict(source='feed:adzuna:London', observed_at='2026-10-19T05:30:00+00:00'),
                 evidence_checks={'vacancy': dict(status='unverified', checked_at='2026-10-19T09:00:00Z', basis='refused',
                                                  source_url='https://www.adzuna.co.uk/jobs/land/ad/1'), 'contacts': []})
        self.assertNotIn('vacancy', [g['code'] for g in briefs.build(p, now=NOW)['gaps']])
        p['advert']['observed_at'] = '2026-10-16T05:30:00+00:00'
        self.assertIn('vacancy', [g['code'] for g in briefs.build(p, now=NOW)['gaps']])

    def test_profile_confirmation_does_not_verify_an_email(self):
        p = self.person(url='https://example.org/alex')
        p['evidence_checks']['contacts'] = [dict(status='confirmed', person_id='P1', source_url=p['url'],
            checked_at='2026-10-19T09:00:00Z', basis='Alex Example profile read.')]
        self.assertFalse(briefs.build(p, now=NOW)['route']['verified'])

    def test_generated_approach_never_becomes_independent_evidence(self):
        p = self.person(prepared_approach={'approach':dict(role='internal_advocate',ask='Generated ask',
            goal='Generated objective',last='Generated shared memory')})
        p['preferred_ask'] = ''
        result = briefs.build(p, edits={'preferred_ask':dict(actor='owner')}, now=NOW)
        self.assertEqual(result['ask'],'')
        self.assertEqual(result['objective'],'')
        self.assertFalse(any(i['kind'] in ('ask','objective','last_context') for i in result['items']))
        before=briefs.fingerprint(p)
        p['prepared_approach']['approach']['ask']='Different generated ask'
        self.assertEqual(briefs.fingerprint(p),before)


if __name__ == '__main__':
    unittest.main()
