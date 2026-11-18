"""Daily reporting must not turn parser counters into new jobs or bypass research caps."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workspace'))
import db
import automation_usage as quota
import daily_review as review


class ReportTests(unittest.TestCase):
    def test_real_snapshot_packet_reads_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            before=conn.total_changes
            value=review.current_packet(conn,dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc))
            self.assertEqual(value['automatic_admissions'],[])
            self.assertEqual(conn.total_changes,before)

    def test_admissions_are_unique_and_not_parser_or_legacy_import_counts(self):
        rows=[{'run_id':'email:a','receipt':{'processing':{'new':6},'import':{
            'new':0,'capacity':{'admitted':['J1','J2'],'allowance_remaining':8}}}},
            {'run_id':'email-import:a','receipt':{'import':{'capacity':{'admitted':['J1','J2']}}}},
            {'run_id':'email:b','receipt':{'processing':{'new':6},'import':{
            'new':0,'capacity':{'admitted':[],'allowance_remaining':8}}}}]
        self.assertEqual(review.admissions(rows), ['J1','J2'])

    def test_packet_is_bounded_and_excludes_personal_history(self):
        state={'records':[{'id':f'J{i}','name':'Example','notes':'PRIVATE','summary':'x'*10000}
                          for i in range(3000)],'history':['PRIVATE']*3000,
               'opportunities':{'discovery':[{'record_id':'J1','worth_a_look':True}]},
               'research_processing':{},'discovery_capacity':{'working_limit':10}}
        p=review.packet(state, [], '2026-10-30T10:00:00Z')
        raw=json.dumps(p)
        self.assertLess(len(raw.encode()), 48000)
        self.assertNotIn('PRIVATE',raw)
        self.assertEqual(len(p['opportunities']['discovery']),1)

    def test_command_keeps_isolation_but_persists_report_chat(self):
        import research_codex
        cmd=research_codex.report_command('/tmp/schema','/tmp/result')
        self.assertNotIn('--ephemeral',cmd)
        self.assertNotIn('--output-schema',cmd)
        for value in ('read-only','shell_tool','apps','plugins','hooks','multi_agent',
                      'web_search="disabled"','forced_login_method="chatgpt"'):
            self.assertIn(value,cmd)

    def test_once_per_day_and_existing_research_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            now=dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc)
            result={'report':'Example factual report','thread_id':'12345678-1234-1234-1234-123456789abc'}
            with patch.object(review,'current_packet',return_value={}), patch.object(review.model,'exchange',return_value={'ok':True,'result':result}) as exchange:
                first=review.run(conn, Path(tmp), now)
                second=review.run(conn, Path(tmp), now)
            self.assertEqual(first['status'],'ok')
            self.assertEqual(second,first)
            self.assertEqual(exchange.call_count,1)
            self.assertEqual(quota.summary(conn,now.isoformat())['research_day'],1)

    def test_exhausted_budget_makes_no_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            now=dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc)
            for i in range(12):quota.reserve(conn,str(i),'research',now.isoformat())
            with patch.object(review,'current_packet',return_value={}),patch.object(review.model,'exchange') as exchange:
                result=review.run(conn,Path(tmp),now)
            self.assertEqual(result['status'],'waiting')
            exchange.assert_not_called()
            self.assertEqual(quota.summary(conn,now.isoformat())['research_day'],12)

    def test_unknown_response_is_counted_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            now=dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc)
            with patch.object(review,'current_packet',return_value={}),patch.object(review.model,'exchange',side_effect=TimeoutError) as exchange:
                result=review.run(conn,Path(tmp),now)
                review.run(conn,Path(tmp),now)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(exchange.call_count,1)
            self.assertEqual(quota.summary(conn,now.isoformat())['research_day'],1)

    def test_failed_empty_email_polls_remain_visible(self):
        rows=[{'run_id':'email:failed','receipt':{'status':'failed','failures':['collection']}},
              {'run_id':'email:empty','receipt':{'status':'ok','processing':{'jobs':0}}}]
        value=review.packet({'records':[]},rows,'2026-10-30T10:00:00Z')
        self.assertEqual([r['id'] for r in value['runs']],['email:failed'])

    def test_receipt_overflow_fails_instead_of_silently_undercounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            for i in range(102):
                review.save_receipt(conn,f'ingest:2026-10-30T09:00:00.{i:06d}Z',
                    {'started_at':'2026-10-30T09:00:00Z','status':'ok'})
            with patch.object(review.records,'state_context',return_value={'records':[]}):
                with self.assertRaisesRegex(ValueError,'receipt volume'):
                    review.current_packet(conn,dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc))

    def test_interrupted_attempt_is_failed_without_another_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            now=dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc)
            review.save_receipt(conn,'daily-review:2026-10-30',
                {'started_at':now.isoformat(),'status':'running','model_calls':1,'phase':'model'})
            with patch.object(review.model,'exchange') as exchange:
                receipt=review.run(conn,Path(tmp),now)
            exchange.assert_not_called()
            self.assertEqual(receipt['status'],'failed')
            self.assertEqual(receipt['reason'],'interrupted')
            self.assertTrue((Path(tmp)/'daily-review/2026-10-30.json').exists())

    def test_persisted_report_recovers_missing_output_without_another_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=db.connect(Path(tmp)/'test.db');db.migrate(conn)
            self.addCleanup(conn.close)
            now=dt.datetime(2026,10,30,10,tzinfo=dt.timezone.utc)
            result={'report':'The completed report.','thread_id':'12345678-1234-1234-1234-123456789abc'}
            with patch.object(review,'current_packet',return_value={}),patch.object(review.model,'exchange',return_value={'ok':True,'result':result}) as exchange:
                first=review.run(conn,Path(tmp),now)
                (Path(tmp)/'daily-review/2026-10-30.md').unlink()
                (Path(tmp)/'daily-review/2026-10-30.json').unlink()
                second=review.run(conn,Path(tmp),now)
            self.assertEqual(exchange.call_count,1)
            self.assertEqual(second,first)
            self.assertEqual((Path(tmp)/'daily-review/2026-10-30.md').read_text(),'The completed report.')
