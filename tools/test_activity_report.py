"""The activity report measures stored history correctly, writes nothing and reveals no names or words."""
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import activity_report as ar  # noqa: E402
import wo  # noqa: E402

SENT_AT = '2026-10-20T12:19:05+01:00'
PREPARED = ("Hi Sam, thanks again for the tips on the harbour project. Since the spring I have been "
            "consulting on data platforms and small AI projects. Fancy a catch-up soon?")
REWRITTEN = ("Hi Sam, hope the move went smoothly. Could you point me to anyone hiring, or perhaps a "
             "recruiter you rate? I would be glad to meet for a coffee as well.")


class ActivityReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'activity.db'
        self.conn = wo.wodb.connect(self.db)
        wo.wodb.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # Rows are written the way the importers and history leave them, independently of the writers.
    def person(self, rid, relationship='dormant', kind='', priority='first', created='2026-10-01T09:00:00+01:00'):
        self.conn.execute("INSERT INTO records(id,type,name,relationship,kind,priority,created_at,updated_at) "
                          "VALUES(?,'person',?,?,?,?,?,?)",
                          (rid, f'Private Person {rid}', relationship, kind, priority, created, created))

    def version(self, rid, actor, text, at):
        return self.conn.execute("INSERT INTO draft_versions(record_id,title,text,created_at,actor,purpose) "
                                 "VALUES(?,'Email',?,?,?,'message')", (rid, text, at, actor)).lastrowid

    def outcome(self, rid, kind='sent', at=SENT_AT, version=None, snapshot=None, corrected=None):
        self.conn.execute("INSERT INTO outcomes(record_id,kind,at,actor,draft_version_id,snapshot_json,corrected_at) "
                          "VALUES(?,?,?,'owner',?,?,?)", (rid, kind, at, version, json.dumps(snapshot or {}), corrected))

    def event(self, rid, field, to, at, actor='agent', changes='{}', scope=''):
        self.conn.execute("INSERT INTO state_events(entity_id,field,to_value,at,actor,changes_json,recorded_at,source_scope) "
                          "VALUES(?,?,?,?,?,?,?,?)", (rid, field, to, at, actor, changes, at, scope))

    def report(self):
        return ar.report(self.conn, dt.date(2026, 10, 15), dt.date(2026, 10, 26))

    def only_send(self):
        sends = self.report()['preparation']['sends']
        self.assertEqual(len(sends), 1)
        return sends[0]

    def test_a_rewrite_saved_just_before_logging_is_measured_against_the_agent_draft(self):
        self.person('CP-1')
        self.version('CP-1', 'agent', PREPARED, '2026-10-18T18:41:55+01:00')
        mine = self.version('CP-1', 'owner', REWRITTEN, '2026-10-20T12:19:00+01:00')
        self.outcome('CP-1', version=mine)
        send = self.only_send()
        # The version marked sent is the owner's own copy, so comparing with it would read "unchanged".
        self.assertEqual(ar.similarity(REWRITTEN, REWRITTEN), 1.0)
        self.assertEqual(send['prepared'], 'rewritten')
        self.assertLess(send['similarity'], ar.EDITED)
        self.assertEqual(send['entry'], ar.RESAVED)
        self.assertTrue(send['reentered'])

    def test_a_prepared_draft_sent_as_it_was_is_unchanged_and_not_entered_again(self):
        self.person('CP-1')
        prepared = self.version('CP-1', 'agent', PREPARED, '2026-10-18T18:41:55+01:00')
        self.outcome('CP-1', version=prepared)
        send = self.only_send()
        self.assertEqual((send['prepared'], send['similarity'], send['entry'], send['reentered']),
                         ('unchanged', 1.0, 'saved draft', False))

    def test_pasted_wording_counts_as_entered_again_and_an_old_own_version_does_not(self):
        self.person('CP-1')
        self.person('CP-2')
        self.version('CP-1', 'agent', PREPARED, '2026-10-18T18:41:55+01:00')
        self.outcome('CP-1', snapshot={'message': REWRITTEN, 'message_source': 'reported'})
        old = self.version('CP-2', 'owner', REWRITTEN, '2026-10-17T10:00:00+01:00')
        self.outcome('CP-2', version=old)
        prep = self.report()['preparation']
        by_id = {s['record_id']: s for s in prep['sends']}
        self.assertEqual((by_id['CP-1']['entry'], by_id['CP-1']['prepared']), (ar.PASTED, 'rewritten'))
        self.assertEqual((by_id['CP-2']['entry'], by_id['CP-2']['prepared']), ('saved draft', 'no agent draft'))
        self.assertEqual(prep['reentered'], 1)

    def test_a_send_without_wording_is_unknown_and_a_corrected_send_is_ignored(self):
        self.person('CP-1')
        self.person('CP-2')
        self.outcome('CP-1')
        self.outcome('CP-2', corrected='2026-10-21T09:00:00+01:00')
        send = self.only_send()
        self.assertEqual((send['record_id'], send['prepared'], send['entry']), ('CP-1', 'unknown', 'no wording recorded'))

    def test_a_next_step_counts_only_when_recorded_after_the_send(self):
        self.person('CP-1')
        self.outcome('CP-1')
        self.event('CP-1', 'next_action', 'Prepare the meeting', '2026-10-19T09:00:00+01:00', actor='owner')
        self.conn.execute('DROP TABLE IF EXISTS approach_next_steps')
        prep = self.report()['preparation']
        self.assertEqual(prep['next_step_recorded'], 0)
        self.assertIn('approach next steps not in this database', prep['next_step_source'])
        self.conn.execute("CREATE TABLE approach_next_steps(record_id TEXT, action_id TEXT, step_json TEXT, "
                          "revision INTEGER, updated_at TEXT, actor TEXT)")
        self.conn.execute("INSERT INTO approach_next_steps VALUES('CP-1','a','{}',1,'2026-10-20T12:20:00+01:00','owner')")
        self.assertEqual(self.report()['preparation']['next_step_recorded'], 1)

    def test_segments_count_outcomes_and_the_list_is_rebuilt_as_it_was_on_15_october(self):
        self.person('CP-a', relationship='dormant')
        self.event('CP-a', 'relationship', 'warm', '2026-10-15T14:19:00+01:00')
        self.event('CP-a', 'relationship', 'dormant', '2026-10-20T09:00:00+01:00')
        self.person('CP-b', relationship='dormant')
        self.person('CP-c', relationship='warm', kind='recruiter')
        self.person('CP-gone', relationship='active')
        self.person('CP-early', relationship='dormant')
        self.person('CP-late', relationship='dormant', created='2026-10-21T09:00:00+01:00')
        self.person('CP-off', relationship='active', priority='')
        self.outcome('CP-early', at='2026-10-14T14:24:00+01:00')
        self.event('CP-gone', 'priority', '', '2026-10-14T09:00:00+01:00')
        for rid, kind in (('CP-a', 'sent'), ('CP-a', 'replied'), ('CP-b', 'sent'), ('CP-c', 'conversation'),
                          ('CP-off', 'sent'), ('CP-off', 'won')):
            self.outcome(rid, kind=kind)
        out = self.report()['outreach']
        self.assertEqual(out['list_check']['derived'], {'A': 1, 'B': 1, 'C': 1})
        self.assertFalse(out['list_check']['matches'])
        self.assertEqual(out['list_check']['excluded_already_under_way'], 1)
        rows = out['by_segment']
        self.assertEqual((rows['B']['people_contacted'], rows['B']['sent'], rows['B']['replied']), (2, 2, 1))
        self.assertEqual((rows['C']['conversation'], rows['C']['people_contacted']), (1, 0))
        self.assertEqual((rows['-']['sent'], rows['-']['other']), (1, 1))
        self.assertEqual(rows['A']['sent'], 0)

    def test_research_yield_counts_unchanged_reviews_codex_runs_and_sources_acted_on(self):
        for at, changes in (('2026-10-16T09:00:00+01:00', '{}'), ('2026-10-17T09:00:00+01:00', '{}'),
                            ('2026-10-18T09:00:00+01:00', '{"evidence_urls": {"before": "", "after": "x"}}'),
                            ('2026-10-10T09:00:00+01:00', '{}')):
            self.event('CP-r', 'evidence_review', '', at, actor='research', changes=changes)
        self.event('CP-r2', 'evidence_review', '', '2026-10-19T09:00:00+01:00', actor='research')
        for i, (lane, status, day) in enumerate((('research', 'completed', '2026-10-16'), ('research', 'unknown', '2026-10-17'),
                                                 ('incident', 'completed', '2026-10-20'), ('research', 'completed', '2026-10-01'))):
            self.conn.execute("INSERT INTO codex_invocations(id,run_id,lane,started_at,day,week,status) VALUES(?,?,?,?,?,?,?)",
                              (f'i{i}', 'r', lane, day + 'T09:00:00Z', day, day, status))
        for rid, scope in (('JOB-1', 'feed:adzuna:London leadership'), ('JOB-2', 'feed:adzuna:Remote'),
                           ('JOB-3', 'alert:linkedin'), ('CP-h', ''), ('JOB-4', 'research:file.json')):
            self.event(rid, 'created', 'job', '2026-10-16T05:30:00+01:00', scope=scope)
        self.event('JOB-1', 'stage', 'shortlisted', '2026-10-17T09:00:00+01:00', actor='owner')
        q = self.report()['research']
        self.assertEqual((q['reviews'], q['records_reviewed'], q['reviews_without_change']), (4, 2, 3))
        self.assertEqual((q['most_reviews_on_one_record'], q['median_reviews_per_record']), (3, 2))
        self.assertEqual(q['codex_invocations'], {'incident': {'completed': 1}, 'research': {'completed': 1, 'unknown': 1}})
        self.assertEqual(q['sources']['feed:adzuna'], {'created': 2, 'acted_on': 1})
        self.assertEqual(q['sources']['alert:linkedin'], {'created': 1, 'acted_on': 0})
        self.assertEqual(set(q['sources']), {'feed:adzuna', 'alert:linkedin', 'entered by hand', 'research'})

    def test_the_report_writes_nothing_and_shows_no_names_or_message_words(self):
        self.person('CP-1')
        self.version('CP-1', 'agent', PREPARED, '2026-10-18T18:41:55+01:00')
        self.outcome('CP-1', snapshot={'message': REWRITTEN, 'message_source': 'reported'})
        self.conn.commit()
        before = self.conn.total_changes
        result = self.report()
        text = ar.format_report(result) + json.dumps(result)
        self.assertEqual(self.conn.total_changes, before)
        for private in ('Private Person', 'Hi Sam', 'recruiter you trust', 'catch-up'):
            self.assertNotIn(private, text)
        self.assertIn('CP-1', text)

    def test_wo_activity_runs_on_a_backup_behind_the_code_and_other_commands_still_refuse(self):
        self.person('CP-1')
        self.outcome('CP-1')
        last = self.conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
        self.conn.execute('DELETE FROM schema_version WHERE version=?', (last,))
        self.conn.commit()
        out = io.StringIO()
        with redirect_stdout(out):
            code = wo.main(['--db', str(self.db), 'activity', '--since', '2026-10-15', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())['preparation']['sends']), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wo.main(['--db', str(self.db), 'quality']), 2)


if __name__ == '__main__':
    unittest.main()
