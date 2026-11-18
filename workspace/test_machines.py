"""Stored transitions, derived progress and the browser queue agree with the declared machines."""
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'workspace'), str(ROOT / 'tools')]
import db
import machines
import records
import states
import work_selection

TYPES = {'opportunity': 'opportunity', 'org': 'agency', 'job': 'job', 'person': 'person'}
QUEUE_CASES = (
    {'status': 'replied', 'next_step': {'kind': 'waiting', 'text': 'Their role description'}},
    {'status': 'replied', 'next_step': {'kind': 'mine', 'text': 'Send details'}},
    {'status': 'replied', 'next_step': {'kind': 'done', 'text': 'Old task'}},
    {'status': 'waiting', 'reply_to_record': True, 'next': 'Record their reply.'},
    {'status': 'no_response', 'reply_to_record': True},
    {'status': 'conversation'},
    {'status': 'held', 'next_step': {'kind': 'mine', 'text': 'Stale task'}},
    {'status': 'waiting', 'since': '2026-10-20'},
    {'status': 'ready'},
)
QUEUE_KEYS = ('waiting', 'next_step', '', 'needs_reply', 'needs_reply', 'needs_reply', '', 'waiting', '')
CONTACT = (
    ('sent', True, True, 'waiting'),
    ('sent', False, False, 'waiting'),
    ('sent', False, True, 'follow_up'),
    ('followed_up', True, True, 'waiting'),
    ('followed_up', False, False, 'waiting'),
    ('followed_up', False, True, 'no_response'),
)
# Membership captured from tools/make_fixture.py before this change. Names are synthetic.
# Sorted names. Ties among equal scores are not a pinned order.
WEEK = {
    'act_now': (),
    'best': (
        ('Cedar Example Software', ''),
        ('Harbour Analytics', ''),
        ('Head of Quality Engineering', 'Example Cover Systems'),
        ('Interim Engineering Manager', 'Example Rail Ltd'),
        ('Northwind Fintech', ''),
    ),
    'needs_checking': (
        ('Example Council test services', ''),
        ('Interim Engineering Manager', 'Acme Tech Recruitment'),
        ('Interim Head of Engineering', 'Harbour Analytics'),
        ('Quality Engineering Lead — payments verification', 'Northwind Fintech'),
    ),
    'in_progress': (),
    'waiting': (),
}
SECTIONS = {
    'adverts': {
        ('Example Council test services', '', 'tender', 'ready', 'adverts'),
        ('Head of Quality Engineering', 'Example Cover Systems', 'application', 'ready', 'adverts'),
        ('Interim Engineering Manager', 'Acme Tech Recruitment', 'application', 'ready', 'adverts'),
        ('Interim Engineering Manager', 'Example Rail Ltd', 'application', 'ready', 'adverts'),
        ('Interim Head of Engineering', 'Harbour Analytics', 'application', 'ready', 'adverts'),
        ('Quality Engineering Lead — payments verification', 'Northwind Fintech', 'application', 'ready', 'adverts'),
    },
    'decision_makers': {
        ('Avery Partner', '', 'note', 'ready', 'decision_makers'),
        ('Cedar Example Software', '', 'note', 'ready', 'decision_makers'),
        ('Example Rail Ltd', '', 'note', 'ready', 'decision_makers'),
        ('Harbour Analytics', '', 'note', 'ready', 'decision_makers'),
        ('Harper Cover', '', 'note', 'ready', 'decision_makers'),
        ('Jamie Leader', '', 'note', 'ready', 'decision_makers'),
        ('Northwind Fintech', '', 'note', 'ready', 'decision_makers'),
    },
    'desks': {
        ('Riley Recruiter', '', 'recruiter', 'ready', 'desks'),
        ('Sam Sample-Jones', 'Acme Tech Recruitment', 'desk', 'ready', 'desks'),
    },
    'in_progress': {('Morgan Example', 'Harbour Analytics', 'note', 'conversation', 'in_progress')},
    'opportunities': {('Acme Tech Recruitment / Sam Sample-Jones', '', 'opportunity', 'ready', 'opportunities')},
    'people': {
        ('Alex Example', 'Northwind Fintech', 'catch_up', 'ready', 'people'),
        ('Bailey Pilot', '', 'ask', 'ready', 'people'),
        ('Drew Pilot', '', 'ask', 'ready', 'people'),
        ('Ellis Pilot', '', 'ask', 'ready', 'people'),
        ('Frankie Pilot', '', 'ask', 'ready', 'people'),
        ('Georgie Pilot', '', 'ask', 'ready', 'people'),
        ('Rory Introducer', '', 'ask', 'ready', 'people'),
        ('Taylor Fixture', 'Northwind Fintech', 'catch_up', 'ready', 'people'),
    },
    'research': {
        ('Casey Placeholder', 'Brightpath Robotics', 'note', 'research', 'research'),
        ('Rory Introducer', '', 'catch_up', 'research', 'research'),
    },
}
PEOPLE = (
    ('Morgan Example', 'needs_reply'),
    ('Alex Example', 'priority'),
    ('Bailey Pilot', 'priority'),
    ('Drew Pilot', 'priority'),
    ('Ellis Pilot', 'priority'),
    ('Frankie Pilot', 'priority'),
    ('Georgie Pilot', 'priority'),
    ('Riley Recruiter', 'priority'),
)


def _js_array(name):
    text = (ROOT / 'workspace/js/machines.js').read_text()
    match = re.search(rf'export const {name} = \[(.*?)\];', text, re.S)
    if not match:
        raise AssertionError(f'{name} is missing from workspace/js/machines.js')
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


def _node(script, payload):
    completed = subprocess.run(
        ['node', '--input-type=module', '-e', script], input=json.dumps(payload),
        text=True, capture_output=True, check=False, cwd=ROOT)
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


class MachineDeclarationTests(unittest.TestCase):
    def test_browser_lists_match_the_python_machine(self):
        self.assertEqual(_js_array('PERSON_TERMINAL'), machines.PERSON_TERMINAL)
        self.assertEqual(_js_array('NEEDS_REPLY'), machines.NEEDS_REPLY)

    def test_contact_timing_and_queue_keys_are_fixed(self):
        for stage, person, elapsed, expected in CONTACT:
            self.assertEqual(machines.progress_after_contact(stage, person=person, elapsed=elapsed), expected)
        with self.assertRaises(machines.MachineError) as caught:
            machines.progress_after_contact('replied', person=False, elapsed=True)
        self.assertIn("'replied'", str(caught.exception))
        self.assertIn('sent', str(caught.exception))
        self.assertEqual(tuple(machines.person_queue_key(case) for case in QUEUE_CASES), QUEUE_KEYS)

    def test_the_browser_queue_key_matches_the_declared_cases(self):
        script = """
import { queueKey } from './workspace/js/machines.js';
import { personAction } from './workspace/js/people-context.js';
const cases = JSON.parse(await new Promise(resolve => {
  let raw = ''; process.stdin.on('data', chunk => raw += chunk); process.stdin.on('end', () => resolve(raw));
}));
const person = { id: 'P', type: 'person', name: 'P' };
const now = new Date('2026-11-06T12:00:00Z');
console.log(JSON.stringify(cases.map(progress => [queueKey(progress), personAction(person, progress, now).key])));
"""
        self.assertEqual([tuple(pair) for pair in _node(script, QUEUE_CASES)], [(key, key) for key in QUEUE_KEYS])

    def test_every_progress_outcome_is_offered_or_refused_by_name(self):
        outcome_kinds = list(states.OUTCOME_KINDS)
        for kind in ('ask', 'catch_up', 'note', 'desk', 'recruiter', 'opportunity', 'application'):
            table = machines.APPLICATION_OFFERS if kind == 'application' else machines.OUTREACH_OFFERS
            for status in table:
                allowed = [offer.event for offer in machines.offers(kind, status) if offer.method == 'outcome']
                for event in allowed:
                    machines.require_progress_event(kind, status, event)
                for event in outcome_kinds:
                    if event in allowed:
                        continue
                    with self.subTest(kind=kind, status=status, event=event):
                        with self.assertRaises(machines.MachineError) as caught:
                            machines.require_progress_event(kind, status, event)
                        message = str(caught.exception)
                        self.assertIn(repr(status), message)
                        self.assertIn(repr(event), message)
                        self.assertIn('Allowed:', message)
                        for name in allowed:
                            self.assertIn(name, message)
                        if not allowed:
                            self.assertIn('none', message)
        for event in ('won', 'sent'):
            machines.require_progress_event('note', 'research', event)
        for offer in machines.offers('application', 'ready'):
            self.assertIn(offer.event, states.ENUMS['job']['status'])

    def test_every_decision_move_and_every_refusal(self):
        actions = sorted({action for _, action in machines.DECISION_MOVES})
        for (status, action), move in machines.DECISION_MOVES.items():
            self.assertEqual(machines.decision_move(status, action), move.to)
        for status in ('open', 'answered', 'done', 'withdrawn'):
            allowed = [action for (current, action) in machines.DECISION_MOVES if current == status]
            for action in [*actions, 'invented']:
                if (status, action) in machines.DECISION_MOVES:
                    continue
                with self.subTest(status=status, action=action):
                    with self.assertRaises(machines.MachineError) as caught:
                        machines.decision_move(status, action)
                    message = str(caught.exception)
                    self.assertIn(repr(status), message)
                    self.assertIn(repr(action), message)
                    self.assertIn('Allowed:', message)
                    for name in allowed:
                        self.assertIn(name, message)


class StoredTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / 'machines.db')
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _make(self, entity, field, start):
        payload = {'type': TYPES[entity], 'name': f'{entity} {field} {start}'}
        if start:
            payload[field] = start
        return records.create(self.conn, payload)['record']

    def test_every_allowed_stored_transition_and_every_refusal(self):
        for (entity, field), table in states.TRANSITIONS.items():
            vocabulary = states.ENUMS[entity][field]
            for start in (None, *table):
                allowed = states.allowed_from(entity, field, start)
                shown = 'unset' if start in (None, '') else start
                probe = self._make(entity, field, start)
                before = probe[field]
                events = self.conn.execute('SELECT COUNT(*) FROM state_events WHERE entity_id=?', (probe['id'],)).fetchone()[0]
                for value in vocabulary:
                    if value in allowed:
                        continue
                    with self.subTest(entity=entity, field=field, start=start, to=value):
                        with self.assertRaises(ValueError) as caught:
                            records.transition(self.conn, probe['id'], dict(
                                field=field, to=value, revision=probe['revision']))
                        message = str(caught.exception)
                        self.assertIn(f"from {shown!r}", message)
                        self.assertIn(f"to {value!r}", message)
                        self.assertIn('Allowed:', message)
                        for choice in allowed:
                            self.assertIn(choice, message)
                        if not allowed:
                            self.assertIn('none', message)
                self.assertEqual(records.get(self.conn, probe['id'])[field], before)
                self.assertEqual(self.conn.execute(
                    'SELECT COUNT(*) FROM state_events WHERE entity_id=?', (probe['id'],)).fetchone()[0], events)
                for value in allowed:
                    record = self._make(entity, field, start)
                    with self.subTest(entity=entity, field=field, start=start, to=value, allowed=True):
                        saved = records.transition(self.conn, record['id'], dict(
                            field=field, to=value, revision=record['revision']))
                        self.assertEqual(saved['record'][field], value)

    def test_an_unknown_value_names_the_vocabulary_and_the_legacy_wrapper_keeps_the_refusal(self):
        job = self._make('job', 'status', 'candidate')
        with self.assertRaises(ValueError) as caught:
            records.transition(self.conn, job['id'], dict(field='status', to='nope', revision=job['revision']))
        self.assertIn('job.status', str(caught.exception))
        self.assertIn('candidate', str(caught.exception))
        lead = self._make('opportunity', 'stage', 'lead')
        with self.assertRaises(states.StateError) as caught:
            states.transition(self.conn, lead['id'], 'stage', 'won')
        message = str(caught.exception)
        self.assertIn("from 'lead'", message)
        self.assertIn("to 'won'", message)
        self.assertIn('contacted', message)
        self.assertIn('lost', message)
        self.assertEqual(records.get(self.conn, lead['id'])['stage'], 'lead')

    def test_a_named_approach_refuses_an_unoffered_outcome_and_a_repeat_stays_the_same_fact(self):
        person = records.create(self.conn, dict(type='person', name='Queue Person', relationship='warm', kind='friend'))['record']
        action = next(item for item in records.state(self.conn)['intelligence']['actions'] if item['record_id'] == person['id'])
        sent = records.outcome(self.conn, person['id'], dict(
            kind='sent', revision=person['revision'], action_id=action['id']))
        self.assertTrue(sent['saved'])
        again = records.outcome(self.conn, person['id'], dict(
            kind='sent', revision=sent['record']['revision'], action_id=action['id']))
        self.assertFalse(again['saved'])
        self.assertTrue(again.get('duplicate'))
        progressed = next(item for item in records.state(self.conn)['intelligence']['actions'] if item['id'] == action['id'])
        self.assertIn(progressed['progress']['status'], ('waiting', 'follow_up'))
        with self.assertRaises(machines.MachineError) as caught:
            records.outcome(self.conn, person['id'], dict(
                kind='won', revision=sent['record']['revision'], action_id=action['id']))
        message = str(caught.exception)
        self.assertIn(repr(progressed['progress']['status']), message)
        self.assertIn("'won'", message)
        self.assertIn('replied', message)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0], 1)

    def test_a_fact_without_an_approach_can_still_be_recorded(self):
        person = records.create(self.conn, dict(type='person', name='Fact Person'))['record']
        saved = records.outcome(self.conn, person['id'], dict(kind='conversation', revision=person['revision']))
        self.assertEqual(saved['outcome']['kind'], 'conversation')

    def test_an_open_decision_cannot_be_closed_and_can_be_withdrawn(self):
        person = records.create(self.conn, dict(type='person', name='Decision Person'))['record']
        created = records.create_decision(self.conn, dict(
            subject=person['id'], kind='approve', question='Confirm the example route?',
            recommendation='approve', reason='Machine test'))
        decision = created['decision']
        with self.assertRaises(ValueError) as caught:
            records.process_decision(self.conn, decision['id'], 'done', 'Too soon.', revision=decision['revision'])
        message = str(caught.exception)
        self.assertIn("'open'", message)
        self.assertIn("'done'", message)
        self.assertIn('withdraw', message)
        self.assertEqual(records.get_decision(self.conn, decision['id'])['status'], 'open')
        withdrawn = records.process_decision(
            self.conn, decision['id'], 'withdraw', 'The example changed.', revision=decision['revision'])
        self.assertEqual(withdrawn['decision']['status'], 'withdrawn')


class FixtureSectionTests(unittest.TestCase):
    def test_fixture_sections_and_people_filters_match_the_previous_derivation(self):
        import make_fixture
        path = Path(tempfile.mkdtemp()) / 'fixture.db'
        make_fixture.build(path)
        conn = db.connect(path)
        try:
            state = records.state(conn)
        finally:
            conn.close()
        by_id = {record['id']: record for record in state['records']}
        page = work_selection.week_page(state['opportunities'])
        shown = {key: tuple(sorted(
            (by_id[ref['record_id']]['name'], by_id[ref['record_id']].get('organisation') or '')
            for ref in page[key])) for key in WEEK}
        self.assertEqual(shown, WEEK)
        items = {item['id']: item for item in state['intelligence']['actions'] + state['intelligence']['research']}
        actual = {}
        for section, ids in state['intelligence']['sections'].items():
            actual[section] = Counter(
                (by_id[items[item_id]['record_id']]['name'],
                 by_id[items[item_id]['record_id']].get('organisation') or '',
                 items[item_id]['kind'], items[item_id]['progress']['status'],
                 items[item_id]['progress']['section'])
                for item_id in ids)
        self.assertEqual({key: set(counter) for key, counter in actual.items()}, SECTIONS)
        self.assertTrue(all(count == 1 for counter in actual.values() for count in counter.values()))
        script = """
import { peopleContext } from './workspace/js/people-context.js';
const state = JSON.parse(await new Promise(resolve => {
  let raw = ''; process.stdin.on('data', chunk => raw += chunk); process.stdin.on('end', () => resolve(raw));
}));
const context = peopleContext(state, new Date('2026-11-11T12:00:00Z'));
console.log(JSON.stringify(context.queue.map(person => [person.name, context.byId.get(person.id).action.key])));
"""
        slim = {key: state[key] for key in ('records', 'links', 'opportunities', 'intelligence')}
        self.assertEqual(tuple(tuple(row) for row in _node(script, slim)), PEOPLE)


if __name__ == '__main__':
    unittest.main()
