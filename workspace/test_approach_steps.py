"""Approach commitments use canonical revisions, contact correction and durable history."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import records
from record_core import Conflict


STEP = {'kind': 'mine', 'text': 'Send the platform recovery example', 'agreed_on': '2026-10-27'}


class ApproachStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.temp.name) / 'steps.db')
        db.migrate(self.conn)
        self.rid = records.create(self.conn, {'type':'person', 'name':'Synthetic colleague',
            'relationship':'warm', 'kind':'friend', 'priority':'first'})['record']['id']
        self.aid = next(a['id'] for a in records.state(self.conn)['intelligence']['actions'] if a['record_id'] == self.rid)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def revision(self):
        return records.get(self.conn, self.rid)['revision']

    def value(self):
        row = self.conn.execute('SELECT step_json FROM approach_next_steps WHERE record_id=? AND action_id=?',
                                (self.rid, self.aid)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, value=STEP, **extra):
        return records.next_step(self.conn, self.rid, dict(revision=self.revision(), action_id=self.aid, next_step=value, **extra))

    def contact(self, **extra):
        return records.outcome(self.conn, self.rid, dict(kind='sent', revision=self.revision(), action_id=self.aid, **extra))

    def test_step_edit_does_not_record_contact_or_change_commercial_state(self):
        before = records.get(self.conn, self.rid)
        self.save()
        after = records.get(self.conn, self.rid)
        self.assertEqual(self.value(), STEP)
        self.assertEqual(after['last_touch'], before['last_touch'])
        self.assertEqual(after['working_state'], before['working_state'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 0)
        self.assertEqual(after['revision'], before['revision'] + 1)

    def test_two_connections_cannot_overwrite_the_same_revision(self):
        revision = self.revision()
        barrier = Barrier(2)
        path = Path(self.temp.name) / 'steps.db'
        def write(body):
            conn = db.connect(path)
            try:
                barrier.wait()
                try:
                    records.next_step(conn, self.rid, dict(revision=revision, action_id=self.aid, next_step={**STEP, 'text':body}))
                    return body
                except Conflict: return 'conflict'
            finally: conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ['First device', 'Second device']))
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(self.value()['text'], next(x for x in results if x != 'conflict'))
        self.assertEqual(self.revision(), revision + 1)

    def test_commitment_survives_recommendation_removal_and_blocks_unsafe_merge(self):
        import approaches
        self.save()
        state = records.state(self.conn)
        intel = approaches.annotate(dict(actions=[], research=[]), state)
        action = next(a for a in intel['actions'] if a['id'] == self.aid)
        self.assertEqual(action['progress']['next_step'], STEP)
        import work_selection
        self.assertNotIn(self.aid, [x['action_id'] for x in work_selection.build(dict(state, intelligence=intel))['act_now']])
        other = records.create(self.conn, {'type':'person', 'name':'Synthetic duplicate'})['record']['id']
        with self.assertRaisesRegex(ValueError, 'next step'):
            records.merge(self.conn, self.rid, other, {'revision':self.revision()})

    def test_summary_reads_current_steps_once_without_history_growth(self):
        self.save()
        def summary():
            queries = []
            self.conn.set_trace_callback(queries.append)
            try: result = records.state(self.conn, compact=True)
            finally: self.conn.set_trace_callback(None)
            current_reads = [q for q in queries if q.startswith('SELECT') and 'FROM approach_next_steps' in q]
            self.assertEqual(len(current_reads), 1)
            self.assertNotIn('approach_next_steps', result)
            return len(json.dumps(result)), result
        size, _ = summary()
        for n in range(100): self.save({**STEP, 'text':f'Example {n}'})
        later_size, _ = summary()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM approach_next_steps').fetchone()[0], 1)
        self.assertLess(later_size - size, 10000, 'Summary must not retain a growing step archive')

    def test_contact_and_step_are_atomic_and_correction_restores_previous_step(self):
        self.save()
        changed = {**STEP, 'text':'Send two examples'}
        saved = self.contact(next_step=changed, sent_message='I will send two examples.')
        self.assertEqual(self.value(), changed)
        records.correct(self.conn, self.rid, {'revision':self.revision(), 'outcome_id':saved['outcome']['id']})
        self.assertEqual(self.value(), STEP)
        hit = records.state(self.conn)['outcomes'][0]
        self.assertEqual(hit['snapshot']['message'], 'I will send two examples.')
        self.assertTrue(hit['corrected_at'])

    def test_contact_omission_and_duplicate_retry_preserve_existing_step(self):
        self.save()
        first = self.contact()
        retry = self.contact(next_step={'kind':'none'})
        self.assertTrue(retry['duplicate'])
        self.assertEqual(first['outcome']['id'], retry['outcome']['id'])
        self.assertEqual(self.value(), STEP)

    def test_correction_does_not_overwrite_a_later_step_edit(self):
        first = self.contact(next_step=STEP)
        later = {**STEP, 'text':'Prepare for the agreed conversation'}
        self.save(later)
        records.correct(self.conn, self.rid, {'revision':self.revision(), 'outcome_id':first['outcome']['id']})
        self.assertEqual(self.value(), later)

    def test_stale_invalid_and_agent_writes_cannot_partially_change_state(self):
        stale = self.revision()
        self.save()
        before = self.conn.total_changes
        with self.assertRaises(Conflict):
            records.next_step(self.conn, self.rid, {'revision':stale, 'action_id':self.aid, 'next_step':{'kind':'none'}})
        for value in ({'kind':'mine','text':''}, {'kind':'mine','text':'x','agreed_on':'2026-02-30'}, {'kind':'overdue'}):
            with self.assertRaises(ValueError): self.contact(next_step=value)
        with self.assertRaises(ValueError):
            records.next_step(self.conn, self.rid, {'revision':self.revision(), 'action_id':self.aid, 'next_step':STEP}, actor='agent')
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.value(), STEP)

    def test_action_scope_completion_clear_and_restore_copy(self):
        second = self.aid + ':second'
        actions = [dict(id=aid, record_id=self.rid, kind='ask') for aid in (self.aid, second)]
        with patch('record_outcomes.cached_context', return_value={'intelligence':{'actions':actions, 'research':[]}}):
            self.save()
            records.next_step(self.conn, self.rid, {'revision':self.revision(), 'action_id':second,
                'next_step':{'kind':'waiting','text':'Wait for another team’s reply'}})
            self.save({'kind':'done','text':STEP['text']})
            self.assertEqual(self.value()['kind'], 'done')
            self.save({'kind':'none'})
        self.assertEqual(self.value()['kind'], 'none')
        other = self.conn.execute('SELECT step_json FROM approach_next_steps WHERE action_id=?', (second,)).fetchone()[0]
        self.assertEqual(json.loads(other)['kind'], 'waiting')
        restored = db.connect(Path(self.temp.name) / 'restored.db')
        try:
            self.conn.backup(restored)
            self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM approach_next_steps ORDER BY action_id')],
                             [tuple(r) for r in restored.execute('SELECT * FROM approach_next_steps ORDER BY action_id')])
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0],
                             restored.execute('SELECT COUNT(*) FROM state_events').fetchone()[0])
        finally: restored.close()

    def test_actual_step_leads_on_return_without_changing_contact_stage_or_week(self):
        self.contact(next_step=STEP)
        snapshot = records.state(self.conn)
        action = next(a for a in snapshot['intelligence']['actions'] if a['id'] == self.aid)
        self.assertEqual(action['progress']['status'], 'waiting')
        self.assertEqual(action['progress']['next_step'], STEP)
        self.assertIn(STEP['text'], action['progress']['next'])
        self.assertNotIn(self.aid, [r['action_id'] for r in snapshot['opportunities']['act_now']])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM week_items').fetchone()[0], 0)
        self.save({'kind':'done', 'text':STEP['text']})
        snapshot = records.state(self.conn)
        self.assertNotIn(self.aid, [r['action_id'] for r in snapshot['opportunities']['act_now']])

    def test_incoming_reply_is_not_hidden_by_an_existing_commitment(self):
        saved = self.contact(next_step=STEP)
        self.conn.execute("UPDATE outcomes SET at='2026-01-01T12:00:00Z' WHERE id=?", (saved['outcome']['id'],))
        self.conn.commit()
        records.interaction(self.conn, {'record_id':self.rid, 'revision':self.revision(), 'at':db.now(),
            'channel':'email', 'direction':'in', 'summary':'They replied'})
        action = next(a for a in records.state(self.conn)['intelligence']['actions'] if a['id'] == self.aid)
        self.assertTrue(action['progress']['reply_to_record'])
        self.assertIn('record their reply', action['progress']['next'])
        self.assertEqual(action['progress']['next_step'], STEP)
