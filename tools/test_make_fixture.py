from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / 'workspace')]
import db  # noqa: E402
import make_fixture  # noqa: E402
import records  # noqa: E402


class FixtureTests(unittest.TestCase):
    """The synthetic development database (decision #28): current schema, every checked path, nothing real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'dev.db'
        make_fixture.build(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    def test_it_follows_the_current_migrations(self):
        self.assertEqual(db.pending(self.conn), [])

    def test_it_exercises_every_path_the_checks_use(self):
        state = records.state(self.conn)
        self.assertGreaterEqual(len(state['review_queue']), 3)  # three review actions in a row
        self.assertEqual({d['area'] for d in state['decisions'] if d['status'] == 'open'}, {'search', 'operations'})
        pairs = [frozenset((l['from_id'], l['to_id'])) for l in state['links']]
        self.assertGreater(len(pairs), len(set(pairs)))  # a record linked twice to the same partner
        self.assertTrue(any(r['transitions'] for r in state['records']))
        self.assertTrue(state['drafts'] and state['interactions'])

    def test_it_holds_no_real_personal_data(self):
        text = '\n'.join(str(v) for t in ('records', 'review_queue', 'decisions', 'drafts', 'interactions')
                         for row in self.conn.execute(f'SELECT * FROM {t}') for v in tuple(row))
        for email in re.findall(r'[\w.+-]+@[\w.-]+', text):
            self.assertTrue(email.endswith('@example.com'), email)
        for url in re.findall(r'https?://[^\s"\',;]+', text):
            self.assertRegex(url, r'^https?://([\w-]+\.)*example\.com(/|$)|^https://www\.linkedin\.com/in/example-', url)

    def test_decision_intelligence_has_realistic_routes_and_readiness(self):
        state = records.state(self.conn)
        self.assertNotIn('intelligence_error', state)
        actions = state['intelligence']['actions']
        self.assertEqual(len(actions), len({a['id'] for a in actions}))
        self.assertTrue({'close_network', 'known_contact', 'agency', 'direct', 'opportunity'} <= {a['route'] for a in actions})
        self.assertTrue({'ready','conversation'} <= {a['stage'] for a in actions})
        self.assertFalse({'approved','review'} & {a['stage'] for a in actions})
        direct=next(a for a in actions if a['route']=='direct' and a['brief']['facts'])
        self.assertIn('may',direct['brief']['hypothesis'].lower())

    def test_drafts_use_immutable_versions_and_decisions_bind_exactly(self):
        state = records.state(self.conn)
        self.assertTrue(all(d['version_id'] for d in state['drafts']))
        drafts = [d for d in state['decisions'] if d['kind'] == 'draft']
        self.assertTrue(all(d['draft_version_id'] and d['draft_is_current'] for d in drafts))
        approved = next(d for d in drafts if d['answer'] == 'approve')
        self.assertEqual(approved['draft_binding'], 'exact')
        self.assertEqual(approved['draft']['text'], next(d['text'] for d in state['drafts'] if d['version_id'] == approved['draft_version_id']))

    def test_one_person_has_distinct_company_approaches_for_navigation_and_outcomes(self):
        state = records.state(self.conn)
        alex = next(r for r in state['records'] if r['name'] == 'Alex Example')
        actions = [a for a in state['intelligence']['actions'] if a['record_id'] == alex['id']]
        self.assertEqual(len(actions), 2)
        self.assertEqual(len({a['company_id'] for a in actions}), 2)
        self.assertEqual(len({a['brief']['hypothesis'] for a in actions}), 2)
        self.assertTrue(all(a['stage'] == 'ready' and a['brief']['facts'] for a in actions))
        self.assertEqual({a['brief']['facts'][0]['url'] for a in actions},
                         {'https://northwind.example.com/careers', 'https://cedar.example.com/integration'})

    def test_fixture_exercises_multiple_decisions_and_complete_history(self):
        state = records.state(self.conn)
        by_record = {}
        for decision in state['decisions']:
            if decision['record_id'] and decision['status'] == 'open':
                by_record.setdefault(decision['record_id'], []).append(decision)
        rid = next(rid for rid, decisions in by_record.items() if len(decisions) > 1)
        history = records.decisions_history(self.conn, rid)
        self.assertTrue(any(d['status'] == 'done' for d in history))
        self.assertGreaterEqual(len(history), 3)

    def test_it_refuses_to_overwrite(self):
        with self.assertRaises(SystemExit):
            make_fixture.build(self.path)

    def test_prepared_approaches_cover_draft_and_ready_cvs(self):
        actions = records.state(self.conn)['intelligence']['actions']
        plans = {a['person']: a['progress']['plan'] for a in actions if a['progress']['plan']}
        self.assertIn('still a draft', plans['Alex Example']['cv'])
        self.assertNotIn('still a draft', plans['Riley Recruiter']['cv'])
        self.assertTrue(all(p['cv_url'].startswith('https://') for p in plans.values()))


if __name__ == '__main__':
    unittest.main()
