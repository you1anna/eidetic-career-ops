import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import preparation_eval as ev


class EvaluationTests(unittest.TestCase):
    def comparison(self):
        return dict(cases=[dict(id=str(i), name='Example', revision=3,
            baseline=dict(text='Old note', ask='catch_up', version_id=7, actor='owner'),
            candidate='Hi Example, catch up? portfolio.example.com', proposed_ask='catch_up',
            ask_reason='Keep the recorded ask.', evidence=['Recorded shared project'],
            gaps=['Current availability unknown'],
            factors=dict(context='No new facts', ask='Unchanged', wording='Shortened')) for i in range(5)])

    def test_attended_review_binds_entire_comparison_and_never_proves_worker_quality(self):
        report = ev.attended_report(self.comparison())
        reviews = dict(reviews=[dict(id=r['id'], saved_sha256=r['saved_sha256'],
            facts_supported=True, ask_correct=True, sendable=True, editing_minutes=1)
            for r in report['comparisons']])
        self.assertEqual(ev.review_summary(report)['usefulness'], 'pending')
        result = ev.review_summary(report, reviews)
        self.assertEqual(result['usefulness'], 'pass')
        self.assertEqual(result['worker_quality'], 'not evaluated')
        for key, value in [('candidate', 'Changed'), ('proposed_ask', 'introduction'),
                           ('evidence', ['Changed evidence'])]:
            changed = self.comparison(); changed['cases'][0][key] = value
            self.assertEqual(ev.review_summary(ev.attended_report(changed), reviews)['usefulness'], 'pending')
        changed_report = copy.deepcopy(report)
        changed_report['comparisons'][0]['candidate'] = 'Edited after review'
        self.assertEqual(ev.review_summary(changed_report, reviews)['usefulness'], 'pending')
        duplicate = copy.deepcopy(reviews)
        duplicate['reviews'].append(duplicate['reviews'][0])
        self.assertEqual(ev.review_summary(report, duplicate)['usefulness'], 'pending')
        reviews['reviews'][0]['ask_correct'] = False
        self.assertEqual(ev.review_summary(report, reviews)['usefulness'], 'pending')

    def test_attended_requires_five_unique_complete_cases(self):
        for change in ('short', 'duplicate', 'missing'):
            data = self.comparison()
            if change == 'short': data['cases'].pop()
            if change == 'duplicate': data['cases'][1]['id'] = '0'
            if change == 'missing': del data['cases'][0]['baseline']['actor']
            with self.assertRaises(ValueError): ev.attended_report(data)

    def test_attended_cli_is_private_read_only_and_reuses_review_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); source = path/'input.json'
            data = self.comparison(); data['cases'][0]['baseline']['text'] = '<script>bad()</script>'
            source.write_text(json.dumps(data)); before = source.read_bytes()
            with patch.object(ev.model, 'exchange', side_effect=AssertionError('No dispatch')), \
                 patch.object(ev.db, 'connect', side_effect=AssertionError('No database')):
                self.assertEqual(ev.main(['--mode','attended','--comparison',str(source),
                                          '--output',str(path/'run')]), 0)
            self.assertEqual(source.read_bytes(), before)
            text = (path/'run/review.html').read_text()
            self.assertIn('&lt;script&gt;bad()', text)
            self.assertEqual(text.count('<fieldset '), 5)
            self.assertNotIn('type="checkbox" checked', text)
            self.assertEqual((path/'run').stat().st_mode & 0o777, 0o700)
            for file in ('report.json', 'review.html'):
                self.assertEqual((path/'run'/file).stat().st_mode & 0o777, 0o600)

    def test_private_samples_are_read_only_and_hold_out_contacted_or_protected_records(self):
        people = [dict(type='person', id=str(i), name='Alex Example', priority='first',
                       relationship='warm', kind='former_colleague', preferred_ask='introduction',
                       preferred_channel='email', email='alex@example.com',
                       known_context='Your note: We worked together on the example migration.') for i in range(8)]
        people[4]['exclusion'] = 'Excluded'
        people[5]['archived_at'] = '2026-10-18'
        people[6]['relationship'] = 'do_not_contact'
        state = dict(records=people, drafts=[dict(record_id='1')],
                     outcomes=[dict(record_id='2'), dict(record_id='7', corrected_at='2026-10-18')],
                     decisions=[dict(record_id='3', kind='draft', status='open')])
        original = copy.deepcopy(state)
        coverage = ev.recorded_candidates(state)
        self.assertEqual({s['id'] for s in coverage['samples']}, {'0', '7'})
        self.assertEqual(state, original)

    def test_an_existing_fresh_or_copied_ledger_cannot_supply_socket_allowance(self):
        import db
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            ledger = db.connect(path/'fresh.db'); db.migrate(ledger); ledger.close()
            # Canonical identity is a fixture too: never stat the protected production DB in a test.
            canonical = path/'canonical.db'
            canonical.touch()
            with patch.object(ev, 'SHARED_USAGE_DB', canonical), \
                    patch.object(ev.model, 'exchange', side_effect=AssertionError('No dispatch allowed')):
                self.assertEqual(ev.main(['--output',str(path/'run'),'--mode','socket','--usage-db',str(path/'fresh.db')]), 2)
            report = json.loads((path/'run/report.json').read_text())
            self.assertEqual(report['real_model_calls'], 0)
            self.assertIn('retired', report['real_model_status'])

    def test_old_scenarios_cannot_dispatch_or_create_new_message_work(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ev.model,'exchange') as exchange:
            for scenario in ev.SCENARIOS:
                with self.subTest(scenario=scenario), self.assertRaisesRegex(ValueError,'retired'):
                    ev.run_scenario(Path(tmp)/scenario,scenario)
            self.assertEqual(list(Path(tmp).iterdir()),[])
        exchange.assert_not_called()

    def test_usefulness_needs_real_execution_bound_reviews_and_zero_fact_failures(self):
        rows = [dict(case={'id':str(i)}, saved_sha256=str(i)) for i in range(5)]
        report = dict(mode='synthetic', completed_preparation_calls=0,
                      scenarios=[dict(scenario='normal', passed=True, rows=rows)])
        reviews = dict(reviews=[dict(id=str(i), saved_sha256=str(i), facts_supported=True,
                                    ask_correct=True, sendable=i != 4, editing_minutes=1) for i in range(5)])
        self.assertEqual(ev.review_summary(report, reviews)['usefulness'], 'pending')
        report.update(mode='socket', completed_preparation_calls=5)
        self.assertEqual(ev.review_summary(report, reviews)['usefulness'], 'pass')
        for field, value in [('saved_sha256','wrong'), ('facts_supported',False), ('ask_correct',False)]:
            changed = copy.deepcopy(reviews); changed['reviews'][4][field] = value
            self.assertEqual(ev.review_summary(report, changed)['usefulness'], 'pending')
        self.assertEqual(ev.review_summary(report)['usefulness'], 'pending')

    def test_exhausted_allowance_blocks_before_socket_or_accounting_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); state = path / 'state.json'
            state.write_text(json.dumps(dict(research_processing=dict(allowance=dict(
                research_day=12,research_day_limit=12,research_week=12,research_week_limit=84,
                day=12,day_limit=16,week=12,week_limit=100)))))
            with patch.object(ev.model, 'exchange', side_effect=AssertionError('No dispatch allowed')):
                self.assertEqual(ev.main(['--output',str(path/'run'),'--mode','socket','--admission-state',str(state)]), 2)
            report = json.loads((path/'run/report.json').read_text())
            self.assertEqual(report['real_model_calls'], 0)
            self.assertIn('retired', report['real_model_status'])
            self.assertEqual(report['review']['usefulness'], 'pending')

    def test_report_escapes_evidence_and_does_not_preselect_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = dict(mode='synthetic', policy='test', scenarios=[dict(scenario='normal', passed=False, rows=[
                dict(case=dict(id='x',name='<script>bad()</script>',context='unknown',ask='catch_up',expected='a call'),
                     state={},saved='<img src=x onerror=bad()>',saved_sha256='123',exchanges=[],errors=[])])])
            ev.write_pack(Path(tmp), report)
            text = (Path(tmp)/'review.html').read_text()
            self.assertNotIn('<script>bad()', text)
            self.assertNotIn('<img src=x', text)
            self.assertNotIn('type="checkbox" checked', text)


if __name__ == '__main__':
    unittest.main()
