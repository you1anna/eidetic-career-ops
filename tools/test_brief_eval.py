from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import brief_eval


class EvaluationTests(unittest.TestCase):
    def test_synthetic_controls_cover_acceptance_and_rejection_without_real_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            report = brief_eval.run(Path(folder)/'report')
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(len(report['cases']), 5)
            self.assertEqual(report['usage']['calls'], 5)
            self.assertTrue(all(c['control_pass'] for c in report['cases']))
            self.assertEqual(report['human_review'], 'pending')
            self.assertTrue(any(g['code']=='relationship' for g in report['cases'][-1]['brief']['gaps']))
            self.assertTrue(any(g['code']=='employer_conflict' for g in report['cases'][1]['brief']['gaps']))
            self.assertEqual((Path(folder)/'report/report.json').stat().st_mode & 0o777, 0o600)

    def test_socket_refuses_without_production_ledger_before_any_dispatch(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(brief_eval, 'SHARED_USAGE_DB', Path(folder)/'absent.db'), \
             patch.object(brief_eval.model, 'exchange') as call:
            with self.assertRaises(ValueError): brief_eval.run(Path(folder)/'report','socket')
            call.assert_not_called()


if __name__ == '__main__': unittest.main()
