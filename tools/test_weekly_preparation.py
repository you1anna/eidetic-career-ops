"""Historical preparation preservation and retirement: no generated messages or new approvals.

Synthetic fixture records only (decision #28). The model socket is faked at research_model.exchange,
so the real ledger, cache, validation and import run.
"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workspace'))
import db
import records
import record_views
import approaches
import automation_usage as quota
import approach_preparation as prep
import profile_rules
from test_profile_rules import EXAMPLE
import research_worker as worker
import research_cache as memo
import make_fixture

LIMITS = {'enabled': True, 'focused': True, 'prepare': True, 'max_calls': 6, 'engine': 'codex_cli'}
_FIXTURE = {}


def fixture_copy(folder):
    if 'path' not in _FIXTURE:
        base = Path(tempfile.mkdtemp(prefix='wo-prep-fixture-'))
        make_fixture.build(base / 'fixture.db')
        conn = db.connect(base / 'fixture.db')
        riley = next(r for r in records.state(conn)['records'] if r['name'] == 'Riley Recruiter')
        records.edit(conn, riley['id'], dict(revision=riley['revision'],
                     known_context='Your note: We discussed engineering leadership briefs at the synthetic hiring event.'), actor='owner')
        conn.close()
        _FIXTURE['path'] = base / 'fixture.db'
    target = Path(folder) / 'work.db'
    shutil.copy(_FIXTURE['path'], target)
    return target


def good_message(packet):
    return next((o['message'] for o in packet.get('draft_options', [])), '')


class FakeCodex:
    """Answers each operation like the socket runner; records what it was asked."""
    def __init__(self, message=good_message, status='ready', during=None):
        self.requests, self.message, self.status, self.during = [], message, status, during

    def __call__(self, request):
        self.requests.append(request)
        op, prompt = request['operation'], request['prompt']
        usage = {'input_tokens': 1000, 'cached_input_tokens': 200, 'output_tokens': 150}
        if op == 'discovery':
            return {'ok': True, 'result': {'urls': []}, 'usage': usage}
        if op == 'finding':
            claim = {'status': 'unverified', 'source': 0, 'quote': '', 'basis': ''}
            return {'ok': True, 'usage': usage, 'result': {'vacancy': claim, 'contacts': [], 'facts': [], 'fit': '', 'gaps': [],
                                                           'public_contact_route': '', 'note': '', 'recipient_id': ''}}
        if op == 'brief':
            claim = {'status': 'unverified', 'source': 0, 'quote': '', 'basis': ''}
            return {'ok': True, 'usage': usage, 'result': {
                'vacancy': claim, 'contacts': [], 'candidates': [], 'unknowns': []}}
        if self.during:
            self.during(prompt)
        packet = prompt['packet']
        message = self.message(packet) if callable(self.message) else self.message
        return {'ok': True, 'usage': usage, 'result': {
            'status': self.status, 'contacts': [], 'reason': 'They lead the team you would most like to learn from.',
            'ask': 'A short call.', 'message': message if self.status == 'ready' else '', 'facts': [],
            'unknowns': ['How you know them is not recorded.'] if self.status != 'ready' else []}}

    def ops(self, name):
        return [r for r in self.requests if r['operation'] == name]


def readable(conn, url, cache, at=None):
    return dict(url=url, ok=True, checked_at=worker.sources.stamp(), reason='read', links=[],
                text='A public page about engineering teams and delivery, long enough to be read as evidence.')


class PreparationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = fixture_copy(self.root)
        self.conn = db.connect(self.path); self.addCleanup(self.conn.close)

    def run_pass(self, codex=None, limits=LIMITS, read=readable):
        codex = codex or FakeCodex()
        with patch.object(worker.model, 'exchange', side_effect=codex), patch.object(memo, 'read_source', side_effect=read):
            receipt = worker.run(self.path, self.root, None, dict(limits))
        return receipt, codex

    def state(self):
        return records.state(self.conn, compact=False)

    def focus(self):
        return prep.survey(self.conn, self.state())

    def draft(self, rid):
        return self.conn.execute('SELECT * FROM drafts WHERE record_id=?', (rid,)).fetchone()


class RetiredPreparationTests(PreparationCase):
    def test_legacy_prepare_flag_cannot_generate_messages_or_approvals(self):
        before={table:[tuple(r) for r in self.conn.execute('SELECT * FROM '+table)]
                for table in ('drafts','draft_versions','decisions','outcomes','week_items')}
        receipt, codex=self.run_pass(limits=dict(LIMITS,briefs=True))
        self.assertFalse(codex.ops('preparation'))
        self.assertNotIn('preparation',receipt)
        self.assertLessEqual(receipt['usage']['calls'],6)
        for table, rows in before.items():
            self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM '+table)],rows,table)

    def test_retired_preparation_boundary_never_calls_model_or_writes(self):
        before=list(self.conn.iterdump())
        with patch.object(worker.model,'exchange') as exchange:
            with self.assertRaisesRegex(ValueError,'retired'):
                prep.prepare(self.conn,'research:test',LIMITS,{}, {},{})
            self.assertEqual(prep.apply(self.conn,{}, {'message':'Historical wording'},'research:old')['outcome'],'retired')
        exchange.assert_not_called()
        self.assertEqual(list(self.conn.iterdump()),before)

    def test_legacy_pending_batch_is_retained_without_recreating_a_draft(self):
        person=next(r for r in self.state()['records'] if r['type']=='person')
        name='a'*32+'.json';path=self.root/'research-server'/name
        original=dict(finding=dict(id=person['id']),preparation=dict(outcome=dict(message='Old generated wording')))
        digest=worker.private_json(path,original)
        receipt=dict(status='running',started_at=worker.sources.stamp(),items=[dict(id=person['id'],batch=name,sha256=digest)])
        worker.save_receipt(self.conn,'research:retired-replay',receipt)
        before={t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in ('drafts','draft_versions','decisions','observations')}
        with patch.object(worker.model,'exchange') as exchange:
            self.assertEqual(worker.recover_batches(self.conn,self.root),1)
            self.assertEqual(worker.recover_batches(self.conn,self.root),0)
        exchange.assert_not_called()
        self.assertEqual(json.loads(path.read_text()),original)
        for t,rows in before.items():self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM '+t)],rows)
        saved=json.loads(self.conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id='research:retired-replay'").fetchone()[0])
        self.assertEqual(saved['items'][0]['import']['outcome'],'retired')

    def test_backup_restore_keeps_exact_saved_wording_and_factual_correction_support(self):
        person=next(r for r in self.state()['records'] if r['type']=='person')
        for text in ('First saved wording','Second saved wording'):
            records.draft(self.conn,dict(record_id=person['id'],revision=records.get(self.conn,person['id'])['revision'],text=text))
        before=record_views.timeline(self.conn,person['id'])
        restored=db.connect(self.root/'restored.db')
        try:
            self.conn.backup(restored)
            self.assertEqual(record_views.timeline(restored,person['id']),before)
            self.assertEqual([r['name'] for r in restored.execute('PRAGMA table_info(outcomes)')].count('corrected_at'),1)
            self.assertTrue(restored.execute("SELECT 1 FROM schema_version WHERE version=19").fetchone())
        finally:restored.close()


class PureChecksTests(unittest.TestCase):
    def packet(self, **person):
        return prep.packet(dict(name='Alex Example', kind='peer', preferred_ask='catch_up',
                                known_context='Your note: We worked together on the synthetic payments migration.', **person), {}, [])

    def test_message_checks(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        ok = good_message(self.packet())
        self.assertEqual(prep.message_issues(ok, self.packet(), {}), [])
        cases = {'link': ok.replace('portfolio.example.com', 'portfolio.example.com and example.com/x'),
                 'contact': ok + '\nalex@example.com', 'figure': ok.replace('30-minute', '42-minute'),
                 'name': ok.replace('Hi Alex', 'Hi there'), 'private': ok + '\nMy medical leave is over.', 'gap': ok.replace('Alex,', '{{first_name}},')}
        for name, text in cases.items():
            with self.subTest(name):
                self.assertTrue(prep.message_issues(text, self.packet(), {}))
        self.assertTrue(prep.message_issues(ok.replace('30-minute call', '30-minute call about Acme'), self.packet(),
                                            {'exclusion_contexts': [{'company': 'Acme'}]}))
        # A number present in career facts cannot be reassigned to another assertion.
        self.assertTrue(prep.message_issues(ok.replace('30-minute call', 'call about growing from 4 to 15'), self.packet(), {}))

    def test_greeting_identifies_the_person_including_short_forms(self):
        pk = lambda name: dict(person=dict(first_name=name.split()[0], name=name), relationship=dict(recorded_context='', your_note=''))
        for text, name, expected in [('Hi Steve,\n', 'Stephen Example', True), ('Hey Hamza,\n', 'Hamzah Example', True),
                                     ('Subject: Catching up\n\nHi Alex,', 'Alex Example', True), ('Hey mate,\n', 'Jay Example', False),
                                     ('Hi there,\n', 'Alex Example', False), ('Hi Bob,\n', 'Alex Example', False)]:
            with self.subTest(text=text):
                self.assertEqual(prep.greets(text, pk(name)), expected)

    def test_london_week_boundaries_follow_the_clock_change(self):
        self.assertEqual(quota.periods('2026-10-18T23:30:00Z')[1], '2026-10-19')  # 00:30 BST, Monday
        self.assertEqual(quota.periods('2026-10-25T23:30:00Z')[1], '2026-10-19')  # 23:30 GMT, still Sunday
        self.assertEqual(quota.periods('2026-10-26T00:30:00Z')[1], '2026-10-26')

    def test_report_counts_the_remaining_pool_and_unsent_notes(self):
        state = {'records': [dict(id=f'P{i}', type='person', priority='first') for i in range(4)] + [dict(id='X', type='person', priority='')],
                 'outcomes': [dict(record_id='P1')], 'drafts': [dict(record_id='P2', purpose='message')]}
        targets = [dict(record_id='P0', action_id='a', state='ready', reason='r', week='2026-10-19')]
        with tempfile.TemporaryDirectory() as folder:
            conn = db.connect(Path(folder) / 'r.db'); db.migrate(conn)
            try:
                out = prep.report(conn, state, targets, '2026-10-19T09:00:00Z')
            finally: conn.close()
        self.assertEqual((out['eligible_remaining'], out['unsent_drafts']), (2, 1))
        self.assertEqual(out['counts'], {'ready': 1})


if __name__ == '__main__':
    unittest.main()
