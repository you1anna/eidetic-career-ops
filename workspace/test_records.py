"""Behaviour at the canonical data boundary; no live accounts or real contact mutations."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import record_core
import records
import workspace_state


def summary_record(record):
    """The page-load copy of a full-state record: no raw advert, outcome actions or research hashes."""
    import copy
    out = copy.deepcopy({k: v for k, v in record.items() if k not in ('advert', 'outcome_actions')})
    relevance = out.get('relevance') or {}
    (relevance.get('qualification') or {}).pop('requirements', None)
    (relevance.get('fit') or {}).pop('optional_unknowns', None)
    for reason in relevance.get('reasons') or []:
        reason.pop('evidence', None)
    if out.get('evidence_brief'):
        out['evidence_brief'] = {k: v for k, v in out['evidence_brief'].items()
                                 if k not in ('context_sha', 'researched_context_sha')}
    return out


def summary_intelligence(intelligence):
    """The page-load copy of full-state intelligence: no outgoing wording, and only the brief lists
    and fields the browser reads. Selection and ranking consume the rest before trimming."""
    import copy
    out = copy.deepcopy(intelligence)
    out.pop('eligible_advert_ids', None)
    for action in out['actions'] + out['research']:
        action['progress'].update(say='', points=[], plan=None, message=None, sent=None)
        action['brief'] = {k: v for k, v in (action.get('brief') or {}).items() if k in ('facts', 'unknowns', 'constraints')}
        for key in ('confidence', 'trigger', 'work_evidence'):
            action.pop(key, None)
    return out


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.temp.name) / 'test.db')
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def person(self, **values):
        return records.create(self.conn, dict(type='person', name='Past colleague', **values))['record']

    def test_owners_list_fields_are_declared_person_choices_that_survive_a_merge(self):
        listed = self.person(priority='first', preferred_channel='whatsapp', preferred_ask='introduction')
        self.assertEqual((listed['priority'], listed['preferred_channel'], listed['preferred_ask']), ('first', 'whatsapp', 'introduction'))
        cleared = records.edit(self.conn, listed['id'], dict(priority='', revision=listed['revision']))['record']
        self.assertIsNone(cleared['priority'])
        self.assertEqual([(r['from_value'], r['to_value']) for r in self.conn.execute(
            "SELECT from_value,to_value FROM state_events WHERE entity_id=? AND field='priority' ORDER BY id", (listed['id'],))],
            [('first', None)])
        for bad in (dict(priority='urgent'), dict(preferred_channel='fax'), dict(preferred_ask='job')):
            with self.assertRaises(ValueError, msg=bad):
                records.edit(self.conn, cleared['id'], dict(bad, revision=cleared['revision']))
        company = records.create(self.conn, dict(type='company', name='Example Ltd'))['record']
        with self.assertRaises(ValueError):
            records.edit(self.conn, company['id'], dict(priority='first', revision=company['revision']))
        with self.assertRaises(ValueError):
            records.create(self.conn, dict(type='job', name='Role', preferred_ask='introduction'))
        unchanged = records.edit(self.conn, company['id'], dict(priority='', revision=company['revision']))['record']
        self.assertEqual(unchanged['revision'], company['revision'])  # a blank choice on a company is no change
        duplicate = self.person(priority='next', preferred_channel='call')
        keep = self.person(preferred_channel='email')
        merged = records.merge(self.conn, duplicate['id'], keep['id'], dict(revision=duplicate['revision']))['record']
        self.assertEqual((merged['priority'], merged['preferred_channel']), ('next', 'email'))  # the survivor's own answer wins

    def test_an_outcome_records_how_owner_got_in_touch(self):
        p = self.person()
        saved = records.outcome(self.conn, p['id'], dict(kind='sent', channel='whatsapp', revision=p['revision']))
        self.assertEqual(self.conn.execute('SELECT channel FROM interactions WHERE id=?', (saved['outcome']['interaction_id'],)).fetchone()[0], 'whatsapp')
        met = records.outcome(self.conn, p['id'], dict(kind='replied', channel='in_person', revision=saved['record']['revision']))
        self.assertEqual(self.conn.execute('SELECT channel FROM interactions WHERE id=?', (met['outcome']['interaction_id'],)).fetchone()[0], 'meeting')
        with self.assertRaises(ValueError):
            records.outcome(self.conn, p['id'], dict(kind='followed_up', channel='pigeon', revision=met['record']['revision']))

    def test_a_relationship_can_be_recorded_as_known_from_before(self):
        p = self.person(relationship='none')
        moved = records.transition(self.conn, p['id'], dict(field='relationship', to='dormant', revision=p['revision']))['record']
        self.assertEqual(moved['relationship'], 'dormant')

    def test_demoting_a_person_clears_priority_and_promoting_restores_eligibility(self):
        p = self.person(relationship='warm', priority='first')
        demoted = records.transition(self.conn, p['id'], dict(field='relationship', to='do_not_contact', revision=p['revision']))['record']
        self.assertEqual(demoted['relationship'], 'do_not_contact')
        self.assertIsNone(demoted['priority'])
        events = [(r['field'], r['from_value'], r['to_value']) for r in self.conn.execute(
            "SELECT field, from_value, to_value FROM state_events WHERE entity_id=? AND field IN ('priority', 'relationship') ORDER BY id",
            (p['id'],))]
        self.assertEqual(events, [('priority', 'first', None), ('relationship', 'warm', 'do_not_contact')])
        with self.assertRaises(ValueError):
            records.edit(self.conn, p['id'], dict(priority='first', revision=demoted['revision']))
        with self.assertRaises(ValueError):
            self.person(relationship='do_not_contact', priority='next')
        undone = records.correct(self.conn, p['id'], dict(field='relationship', revision=demoted['revision'], note='Demoted by mistake'))['record']
        self.assertEqual((undone['relationship'], undone['priority']), ('warm', 'first'))
        demoted_again = records.transition(self.conn, p['id'], dict(field='relationship', to='do_not_contact', revision=undone['revision']))['record']
        state_record = next(r for r in records.state(self.conn)['records'] if r['id'] == p['id'])
        self.assertEqual(state_record['transitions']['relationship'], ['none', 'cold', 'warm', 'active', 'dormant'])
        promoted = records.transition(self.conn, p['id'], dict(field='relationship', to='dormant', revision=demoted_again['revision']))['record']
        self.assertEqual((promoted['relationship'], promoted['priority']), ('dormant', None))

    def test_reported_sent_words_are_atomic_exact_and_do_not_replace_a_draft(self):
        p = self.person(relationship='warm', priority='first', preferred_channel='email')
        draft = records.draft(self.conn, dict(record_id=p['id'], revision=p['revision'],
                              purpose='message', text='A prepared note that was not sent.'))
        action = next(a for a in records.state(self.conn)['intelligence']['actions'] if a['record_id'] == p['id'])
        words = '  Hi Example,\n\nI sent different words — including <literal markup>.\n\nMorgan\n'
        payload = dict(kind='sent', revision=draft['record']['revision'], action_id=action['id'],
                       channel='email', sent_message=words)
        saved = records.outcome(self.conn, p['id'], payload, actor='agent')
        self.assertEqual(saved['outcome']['snapshot']['message'], words)
        self.assertEqual(saved['outcome']['snapshot']['message_source'], 'reported')
        self.assertIsNone(saved['outcome']['draft_version_id'])
        self.assertEqual(saved['outcome']['actor'], 'agent')
        self.assertEqual(self.conn.execute('SELECT text FROM drafts WHERE record_id=?', (p['id'],)).fetchone()[0],
                         'A prepared note that was not sent.')
        again = records.outcome(self.conn, p['id'], payload)
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['outcome']['id'], saved['outcome']['id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0], 1)
        records.correct(self.conn, p['id'], dict(revision=saved['record']['revision'], outcome_id=saved['outcome']['id']))
        self.assertEqual(records.get(self.conn, p['id'])['last_touch'], p['last_touch'])
        snapshot = json.loads(self.conn.execute('SELECT snapshot_json FROM outcomes').fetchone()[0])
        self.assertEqual(snapshot['message'], words)

    def test_reported_sent_words_validate_without_partial_writes(self):
        p = self.person()
        for extra in (dict(sent_message=''), dict(sent_message='  '), dict(sent_message=3),
                      dict(sent_message='x' * 8001), dict(sent_message='bad\x00text'),
                      dict(kind='replied', sent_message='Words'),
                      dict(sent_message='Words', draft_version_id=1)):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                records.outcome(self.conn, p['id'], dict(kind='sent', revision=p['revision']) | extra)
        with self.assertRaises(records.Conflict):
            records.outcome(self.conn, p['id'], dict(kind='sent', revision=p['revision'] - 1, sent_message='Words'))
        self.assertEqual(records.get(self.conn, p['id'])['revision'], p['revision'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 0)

    def test_contact_without_wording_does_not_assume_a_prepared_message_was_sent(self):
        p = self.person()
        draft = records.draft(self.conn, dict(record_id=p['id'], revision=p['revision'],
                              purpose='message', text='Do not assume these words were sent.'))
        sent = records.outcome(self.conn, p['id'], dict(kind='sent', revision=draft['record']['revision']))
        self.assertNotIn('message', sent['outcome']['snapshot'])
        self.assertIsNone(sent['outcome']['draft_version_id'])

    def test_outcomes_record_facts_and_keep_commercial_state(self):
        r = records.create(self.conn, dict(type='opportunity',name='Example',stage='lead'))['record']
        saved = records.draft(self.conn, dict(record_id=r['id'],title='Intro',text='Exact content',revision=r['revision']),actor='agent')
        sent = records.outcome(self.conn,r['id'],dict(kind='sent',note='Sent myself',draft_version_id=saved['version_id'],revision=saved['record']['revision']))
        self.assertEqual(sent['record']['stage'],'lead')
        self.assertEqual(sent['outcome']['actor'],'owner')
        again = records.outcome(self.conn,r['id'],dict(kind='sent',draft_version_id=saved['version_id'],revision=sent['record']['revision']))
        self.assertFalse(again['saved'])
        self.assertEqual(again['outcome']['id'],sent['outcome']['id'])
        view = records.state(self.conn)
        self.assertEqual(len(view['outcomes']),1)
        self.assertEqual(len(view['interactions']),1)
        self.assertEqual(view['interactions'][0]['direction'],'out')
        self.assertTrue(sent['record']['last_touch'])
        with self.assertRaises(records.Conflict):
            records.outcome(self.conn,r['id'],dict(kind='replied',revision=saved['record']['revision']))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],1)

    def test_outcomes_validate_kind_record_and_version_before_any_write(self):
        r = self.person(kind='friend')
        other = self.person()
        version = records.draft(self.conn,dict(record_id=other['id'],text='Other text',revision=other['revision']))['version_id']
        for extra in (dict(kind='anything'),dict(kind='sent',draft_version_id=version),dict(kind='sent',draft_version_id=True)):
            with self.assertRaises(ValueError):
                records.outcome(self.conn,r['id'],dict(revision=r['revision'],**extra))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0],0)
        records.outcome(self.conn,r['id'],dict(kind='introduction',revision=r['revision']),actor='agent')
        self.assertEqual(records.state(self.conn)['outcomes'][0]['actor'],'agent')

    def test_a_message_keeps_its_purpose_and_the_words_that_were_sent(self):
        p = self.person(relationship='warm', priority='first', preferred_channel='whatsapp')
        saved = records.draft(self.conn, dict(record_id=p['id'], revision=p['revision'], purpose='message',
                                              title='WhatsApp', text='Hello there.'), actor='agent')
        self.assertEqual(self.conn.execute('SELECT purpose FROM drafts WHERE record_id=?', (p['id'],)).fetchone()['purpose'], 'message')
        version = self.conn.execute('SELECT purpose, actor FROM draft_versions WHERE id=?', (saved['version_id'],)).fetchone()
        self.assertEqual((version['purpose'], version['actor']), ('message', 'agent'))
        state = records.state(self.conn)
        self.assertFalse(any(d['kind'] == 'draft' for d in state['decisions']))
        action = next(a for a in state['intelligence']['actions'] if a['record_id'] == p['id'])
        self.assertEqual(action['progress']['status'], 'ready')
        self.assertIsNone(action['progress']['message'])
        self.assertNotIn('draft_version_id', action)
        # A legacy record-only historical outcome can still bind its immutable saved version.
        sent = records.outcome(self.conn, p['id'], dict(kind='sent', revision=saved['record']['revision'],
                                                        draft_version_id=saved['version_id']))
        self.assertEqual(sent['outcome']['snapshot']['message'], 'Hello there.')
        records.draft(self.conn, dict(record_id=p['id'], revision=sent['record']['revision'], purpose='message',
                                      title='WhatsApp', text='A later edit.'), actor='owner')
        waiting = next(a for a in records.state(self.conn)['intelligence']['actions'] if a['id'] == action['id'])
        self.assertEqual(waiting['progress']['status'], 'waiting')
        self.assertIsNone(waiting['progress']['sent'])
        import record_views
        timeline = record_views.timeline(self.conn, p['id'])
        self.assertEqual([d['text'] for d in timeline['drafts']], ['A later edit.', 'Hello there.'])
        self.assertEqual(timeline['outcomes'][0]['snapshot']['message'], 'Hello there.')
        self.assertIsNone(waiting['progress']['message'])
        with self.assertRaises(ValueError):
            records.draft(self.conn, dict(record_id=p['id'], revision=records.get(self.conn, p['id'])['revision'],
                                          purpose='poem', text='No.'))

    def test_identical_draft_save_keeps_approval_and_version(self):
        r = self.person()
        saved = records.draft(self.conn,dict(record_id=r['id'],text='Same text',revision=r['revision']))
        d = records.create_decision(self.conn,dict(subject=r['id'],kind='draft',question='Approve?'))['decision']
        same = records.draft(self.conn,dict(record_id=r['id'],text='Same text',revision=records.get(self.conn,r['id'])['revision']))
        self.assertFalse(same['saved'])
        self.assertEqual(same['version_id'],saved['version_id'])
        self.assertEqual(records.get_decision(self.conn,d['id'])['status'],'open')

    def test_person_outcome_can_reference_only_an_explicitly_linked_company_draft(self):
        person=self.person()
        co=records.create(self.conn,dict(type='company',name='Company'))['record']
        version=records.draft(self.conn,dict(record_id=co['id'],text='Company outreach',revision=co['revision']))['version_id']
        with self.assertRaises(ValueError):
            records.outcome(self.conn,person['id'],dict(kind='sent',revision=person['revision'],draft_version_id=version))
        person=records.link(self.conn,dict(from_id=person['id'],to_id=co['id'],relation='works_at',revision=person['revision']))['record']
        saved=records.outcome(self.conn,person['id'],dict(kind='sent',revision=person['revision'],draft_version_id=version))
        self.assertEqual(saved['outcome']['record_id'],person['id'])
        self.assertEqual(saved['outcome']['draft_version_id'],version)

    def test_an_outcome_does_not_build_state_while_the_write_lock_is_held(self):
        person = self.person(kind='friend', relationship='warm')
        state = records.state(self.conn)
        action = next(a for a in state['intelligence']['actions'] + state['intelligence']['research'] if a['record_id'] == person['id'])
        real = records.state
        def guarded(conn, compact=False, *, view=None, receipt_history=True):
            self.assertFalse(conn.in_transaction)
            return real(conn, compact, view=view, receipt_history=receipt_history)
        with patch.object(workspace_state, 'state', guarded):
            saved = records.outcome(self.conn, person['id'], dict(kind='sent', revision=person['revision'], action_id=action['id']))
        self.assertTrue(saved['saved'])
        self.assertEqual(saved['outcome']['snapshot']['action_id'], action['id'])
        again = records.outcome(self.conn, person['id'], dict(kind='sent', revision=person['revision'], action_id=action['id']))
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['outcome']['id'], saved['outcome']['id'])

    def test_a_long_write_names_its_caller(self):
        with patch.object(record_core, 'WRITE_LOCK_LOG_SECONDS', 0), self.assertLogs(level='WARNING') as logs:
            with records.transaction(self.conn):
                pass
        text = '\n'.join(logs.output)
        self.assertIn('caller=test_a_long_write_names_its_caller', text)
        self.assertIn('hold_ms=', text)
        self.assertNotIn('BEGIN', text)

    def test_a_slow_state_build_is_logged_without_record_text(self):
        person = self.person()
        records._STATE_CACHE.clear()
        records._JSON_CACHE.clear()
        with patch.object(workspace_state, 'STATE_BUILD_LOG_SECONDS', 0), self.assertLogs(level='INFO') as built:
            records.cached_state(self.conn)
        text = '\n'.join(built.output)
        self.assertIn('view=full', text)
        self.assertIn('cache=miss', text)
        self.assertNotIn(person['name'], text)
        with patch.object(workspace_state, 'STATE_BUILD_LOG_SECONDS', 0), self.assertNoLogs(level='INFO'):
            records.cached_state(self.conn)
        records._JSON_CACHE.clear()
        with patch.object(workspace_state, 'STATE_BUILD_LOG_SECONDS', 0), self.assertLogs(level='INFO') as summary:
            records.cached_summary_json(self.conn)
        summary_text = '\n'.join(summary.output)
        self.assertIn('view=summary', summary_text)
        self.assertNotIn(person['name'], summary_text)

    def test_two_matching_linkedin_rows_queue_once_for_the_same_person(self):
        person = records.create(self.conn, dict(type='person', name='Alex Example'))['record']
        row = 'Alex,Example,https://www.linkedin.com/in/alex-example/,,Former Co,Engineer,01 Jan 2020\n'
        csv_text = 'First Name,Last Name,URL,Email Address,Company,Position,Connected On\n' + row + row
        sql = []
        self.conn.set_trace_callback(sql.append)
        try:
            result = records.import_linkedin(self.conn, csv_text)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual((result['queued'], result['existing']), (1, 1))
        card = records.state(self.conn)['review_queue'][0]
        self.assertEqual(card['payload']['matches'][0]['id'], person['id'])
        people_at = next(i for i, statement in enumerate(sql) if "type='person'" in statement)
        begin_at = next(i for i, statement in enumerate(sql) if statement.startswith('BEGIN IMMEDIATE'))
        self.assertLess(people_at, begin_at)

    def test_outcome_provenance_is_validated_computed_by_server_and_immutable(self):
        person=self.person(kind='friend',relationship='warm')
        state=records.state(self.conn)
        action=next(a for a in state['intelligence']['actions']+state['intelligence']['research'] if a['record_id']==person['id'])
        saved=records.outcome(self.conn,person['id'],dict(kind='sent',revision=person['revision'],action_id=action['id'],
            snapshot={'route':'forged','hypothesis':'invented'},route='forged'))
        snapshot=saved['outcome']['snapshot']
        self.assertEqual(snapshot['action_id'],action['id'])
        self.assertEqual(snapshot['route'],action['route'])
        self.assertEqual(snapshot['brief'],action['brief'])
        self.assertNotIn('forged',str(snapshot))
        self.assertEqual(records.state(self.conn)['outcomes'][0]['snapshot'],snapshot)
        with self.assertRaises(records.Conflict):
            records.outcome(self.conn,person['id'],dict(kind='replied',revision=saved['record']['revision'],action_id='unknown'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],1)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE outcomes SET snapshot_json='{}'")

    def test_factual_contacts_are_idempotent_within_each_company_action_independently_of_saved_wording(self):
        person=self.person(kind='friend',relationship='warm')
        companies=[]
        for name in ('Company One','Company Two'):
            company=records.create(self.conn,dict(type='company',name=name))['record']
            records.research_observation(self.conn,company['id'],'research:test',dict(reviewed_at=db.now(),
                signal='Engineering leadership and engineering effectiveness for an existing software platform.',
                source_urls=['https://example.test/'+company['id']]))
            companies.append(company)
        person=records.link(self.conn,dict(from_id=person['id'],to_id=companies[0]['id'],relation='works_at',revision=person['revision']))['record']
        draft=records.draft(self.conn,dict(record_id=person['id'],revision=person['revision'],text='The same note for two different company conversations'))
        intelligence=records.state(self.conn)['intelligence']
        actions=[a for a in intelligence['actions']+intelligence['research'] if a['record_id']==person['id'] and a.get('company_id')]
        self.assertEqual(len(actions),1)
        first=records.outcome(self.conn,person['id'],dict(kind='sent',revision=draft['record']['revision'],
            action_id=actions[0]['id']))
        # A new company context is a distinct fact; saved wording supplies no status or binding.
        records.archive(self.conn,companies[0]['id'],dict(revision=records.get(self.conn,companies[0]['id'])['revision'],reason='Earlier context retired'))
        person=records.link(self.conn,dict(from_id=person['id'],to_id=companies[1]['id'],relation='works_at',revision=first['record']['revision']))['record']
        intelligence=records.state(self.conn)['intelligence']
        actions.append(next(a for a in intelligence['actions']+intelligence['research']
                            if a['record_id']==person['id'] and a.get('company_id')==companies[1]['id']))
        second=records.outcome(self.conn,person['id'],dict(kind='sent',revision=person['revision'],
            action_id=actions[1]['id']))
        self.assertTrue(second['saved'])
        self.assertNotEqual(first['outcome']['id'],second['outcome']['id'])
        self.assertEqual(second['outcome']['snapshot']['action_id'],actions[1]['id'])
        self.assertEqual(second['outcome']['snapshot']['company_id'],actions[1]['company_id'])
        repeated=records.outcome(self.conn,person['id'],dict(kind='sent',revision=second['record']['revision'],
            action_id=actions[1]['id']))
        self.assertFalse(repeated['saved'])
        self.assertEqual(repeated['outcome']['id'],second['outcome']['id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0],2)

    def test_state_queries_do_not_grow_with_record_count(self):
        def add(start, end):
            self.conn.executemany('INSERT INTO records(id,type,name,created_at,updated_at) VALUES(?,?,?,?,?)',
                                 [(f'CP-test-{i}', 'person', f'Person {i}', db.now(), db.now()) for i in range(start, end)])
        def queries():
            traced = []
            self.conn.set_trace_callback(traced.append)
            view = records.state(self.conn)
            self.conn.set_trace_callback(None)
            return len(view['records']), len(traced)
        add(0, 100)
        small, before = queries()
        add(100, 1000)
        large, after = queries()
        self.assertEqual((small, large), (100, 1000))
        self.assertLess(after, 100)
        self.assertLessEqual(after, before + 3)

    def test_state_ranks_down_an_advert_no_scanner_has_seen_lately(self):
        old, new = (records.create(self.conn, dict(type='job', name=f'Head of Quality {k}'))['record'] for k in 'AB')
        with records.transaction(self.conn):
            records.add_observation(self.conn, old['id'], 'board:ashby:test', '2020-01-01T05:30:00Z', '{"title": "A"}')
            records.add_observation(self.conn, new['id'], 'board:ashby:test', db.now(), '{"title": "B"}')
            records.add_observation(self.conn, new['id'], 'research:test', '2020-01-01T05:30:00Z', '{}')
        view = records.state(self.conn)
        unseen = {r['id']: [x['text'] for x in r['relevance']['reasons'] if x.get('unseen')] for r in view['records']}
        self.assertEqual(unseen, {old['id']: ['No source has listed it since 1 Jan 2020; it may have closed'], new['id']: []})
        self.assertNotIn('sightings', view)

    def test_state_is_one_snapshot_during_a_concurrent_edit(self):
        r = self.person()
        writer = db.connect(Path(self.temp.name) / 'test.db')
        changed = False
        def concurrent_edit(sql):
            nonlocal changed
            if not changed and 'FROM followups' in sql:
                changed = True
                records.edit(writer, r['id'], {'revision': r['revision'], 'name': 'New name'})
        self.conn.set_trace_callback(concurrent_edit)
        try:
            view = records.state(self.conn)
        finally:
            self.conn.set_trace_callback(None)
            writer.close()
        self.assertTrue(changed)
        self.assertEqual(view['records'][0]['name'], 'Past colleague')
        self.assertFalse(any(e['field'] == 'name' for e in view['history']))
        self.assertEqual(records.get(self.conn, r['id'])['name'], 'New name')

    def test_summary_bounds_history_and_receipts_without_changing_scores(self):
        r = self.person()
        for i in range(20):
            records.event(self.conn, r['id'], 'summary', '', f'Change {i}')
            records.event(self.conn, r['id'], 'summary', '', 'Source noise', actor='source')
            self.conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                              (f'run:{i:02}', '2026-09-30', 'ok', json.dumps({'detail': 'Read succeeded', 'records': list(range(100))})))
        full = records.state(self.conn)
        summary = records.state(self.conn, compact=True)
        self.assertEqual([summary_record(r) for r in full['records']], summary['records'])
        self.assertEqual(summary_intelligence(full['intelligence']), summary['intelligence'])
        self.assertEqual(len(summary['history']), 12)
        self.assertTrue(all(e['actor'] == 'owner' for e in summary['history']))
        self.assertNotIn('coverage', summary)
        self.assertEqual(len(full['coverage']['runs']), 20)
        self.assertEqual(len(full['history']), 41)

    def test_compact_state_omits_raw_advert_after_building_identical_advice(self):
        r = records.create(self.conn,dict(type='job',name='Software Engineering Manager',engagement='contract',
                                        url='https://example.com/job',location='UK',date=db.now()[:10]))['record']
        records.add_observation(self.conn,r['id'],'board:example',db.now(),json.dumps(dict(
            title=r['name'],text='Engineering leadership and release reliability for a software platform. '+('Context. '*1000),
            employment_type='contract',url=r['url'])))
        full=records.state(self.conn);summary=records.state(self.conn,compact=True)
        self.assertGreater(len(full['records'][0]['advert']['text']),8000)
        self.assertNotIn('advert',summary['records'][0])
        self.assertEqual(summary_record(full['records'][0])['relevance'],summary['records'][0]['relevance'])
        self.assertEqual(full['records'][0]['relevance']['score'],summary['records'][0]['relevance']['score'])
        self.assertEqual(full['records'][0]['relevance']['fit']['requirements'],summary['records'][0]['relevance']['fit']['requirements'])
        self.assertEqual(full['records'][0]['relevance']['fit']['evidence'],summary['records'][0]['relevance']['fit']['evidence'])
        self.assertEqual(summary_intelligence(full['intelligence']),summary['intelligence'])
        self.assertEqual(full['opportunities'],summary['opportunities'])
        self.assertTrue(summary['intelligence']['actions'][0]['brief']['facts'])
        # The full state, CLI and export keep the complete assessment.
        full_action = full['intelligence']['actions'][0]
        self.assertTrue({'confidence', 'trigger', 'work_evidence'} <= set(full_action))
        self.assertGreater(set(full_action['brief']), {'facts', 'unknowns', 'constraints'})
        self.assertIn('eligible_advert_ids', full['intelligence'])
        self.assertIn('context_sha', full['records'][0]['evidence_brief'])
        self.assertEqual([summary_record(r) for r in full['records']], summary['records'])
        self.assertNotIn('context_sha', summary['records'][0]['evidence_brief'])

    def test_followup_records_who_made_the_change(self):
        r = self.person()
        records.followup(self.conn, dict(id=r['id'], revision=r['revision'], action='Repair text', status='needs_review', owner='agent'), actor='agent')
        actors = [x[0] for x in self.conn.execute("SELECT actor FROM state_events WHERE entity_id=? AND field='working_state'", (r['id'],))]
        self.assertEqual(actors, ['agent'])

    def test_dates_must_be_dates_and_free_text_is_refused(self):
        r = records.create(self.conn, dict(type='event', name='Conf', date='2026-10-23'))['record']
        with self.assertRaises(ValueError):
            records.edit(self.conn, r['id'], {'revision': r['revision'], 'date': 'Check the website'})
        for good in ('2027-02', '2027', '2026-10-23T09:00:00+01:00', ''):
            r = records.get(self.conn, r['id'])
            records.edit(self.conn, r['id'], {'revision': r['revision'], 'date': good})

    def test_owner_takes_ownership_of_an_agent_corrected_field_but_not_the_reverse(self):
        r = self.person()
        records.edit(self.conn, r['id'], {'revision': r['revision'], 'summary': 'Agent text'}, actor='agent')
        actor = lambda: self.conn.execute("SELECT actor FROM record_edits WHERE record_id=? AND field='summary'", (r['id'],)).fetchone()[0]
        self.assertEqual(actor(), 'agent')
        r = records.get(self.conn, r['id'])
        records.edit(self.conn, r['id'], {'revision': r['revision'], 'summary': 'The owner text'})
        self.assertEqual(actor(), 'owner')
        r = records.get(self.conn, r['id'])
        records.edit(self.conn, r['id'], {'revision': r['revision'], 'summary': 'Agent again'}, actor='agent')
        self.assertEqual(actor(), 'owner')

    def test_archive_keeps_the_record_out_of_state_with_its_reason(self):
        r = self.person()
        with self.assertRaises(ValueError):
            records.archive(self.conn, r['id'], {'revision': r['revision']})
        records.archive(self.conn, r['id'], {'revision': r['revision'], 'reason': 'Parse artefact'}, actor='agent')
        self.assertNotIn(r['id'], [x['id'] for x in records.state(self.conn)['records']])
        note = self.conn.execute("SELECT note, actor FROM state_events WHERE entity_id=? AND field='archived'", (r['id'],)).fetchone()
        self.assertEqual(tuple(note), ('Parse artefact', 'agent'))

    def test_merge_moves_links_interactions_and_source_identity_then_archives(self):
        keep = records.create(self.conn, dict(type='agency', name='Client Server'))['record']
        dup = records.create(self.conn, dict(type='company', name='Client Server'))['record']
        job = records.create(self.conn, dict(type='job', name='Engineer'))['record']
        records.link(self.conn, dict(from_id=job['id'], to_id=dup['id'], relation='hiring_for', revision=job['revision']))
        self.conn.execute("INSERT INTO source_records VALUES('board:x','42',?)", (dup['id'],))
        dup = records.get(self.conn, dup['id'])
        records.merge(self.conn, dup['id'], keep['id'], {'revision': dup['revision'], 'reason': 'Agency recorded twice'}, actor='agent')
        links = self.conn.execute('SELECT from_id,to_id FROM links').fetchall()
        self.assertEqual([tuple(x) for x in links], [(job['id'], keep['id'])])
        self.assertEqual(self.conn.execute("SELECT record_id FROM source_records WHERE source_id='42'").fetchone()[0], keep['id'])
        self.assertTrue(records.get(self.conn, dup['id'])['archived_at'])

    def test_retype_turns_a_recruiter_recorded_as_a_company_into_an_agency(self):
        co = records.create(self.conn, dict(type='company', name='Redwing Recruitment'))['record']
        out = records.retype(self.conn, co['id'], dict(type='agency', reason='Advert says our client', revision=co['revision']), actor='agent')['record']
        self.assertEqual((out['type'], out['kind'], out['id']), ('agency', 'agency', co['id']))
        self.assertGreater(out['revision'], co['revision'])
        ev = self.conn.execute("SELECT from_value,to_value,note,actor FROM state_events WHERE entity_id=? AND field='type'", (co['id'],)).fetchone()
        self.assertEqual(tuple(ev), ('company', 'agency', 'Advert says our client', 'agent'))
        person = records.create(self.conn, dict(type='person', name='Someone'))['record']
        for rid, payload in ((person['id'], dict(type='agency', reason='x')), (co['id'], dict(type='agency', reason='x')),
                             (co['id'], dict(type='company', reason=''))):
            with self.assertRaises(ValueError):
                records.retype(self.conn, rid, dict(payload, revision=records.get(self.conn, rid)['revision']))

    def test_a_company_number_needs_evidence_and_is_one_to_one(self):
        co = records.create(self.conn, dict(type='company', name='Zephyr Labs'))['record']
        twin = records.create(self.conn, dict(type='company', name='Zephyr'))['record']
        rev = lambda rid: records.get(self.conn, rid)['revision']
        page = 'https://zephyr.example/legal'
        for bad in (dict(number='123', evidence=page), dict(number='00000001', evidence=''),
                    dict(number='00000001', evidence='ftp://x')):
            with self.assertRaises(ValueError):
                records.company_number(self.conn, co['id'], dict(bad, revision=rev(co['id'])), actor='agent')
        records.company_number(self.conn, co['id'], dict(number='00000001', evidence=page, revision=rev(co['id'])), actor='agent')
        mapped = lambda: self.conn.execute("SELECT source_id,record_id FROM source_records WHERE source_scope='companies-house'").fetchall()
        self.assertEqual([tuple(r) for r in mapped()], [('00000001', co['id'])])
        with self.assertRaisesRegex(ValueError, 'already recorded for'):
            records.company_number(self.conn, twin['id'], dict(number='00000001', evidence=page, revision=rev(twin['id'])))
        records.company_number(self.conn, co['id'], dict(number='sc123456', evidence=page, revision=rev(co['id'])), actor='agent')
        self.assertEqual([tuple(r) for r in mapped()], [('SC123456', co['id'])])
        records.company_number(self.conn, co['id'], dict(number=None, evidence=page, revision=rev(co['id'])), actor='agent')
        self.assertEqual(mapped(), [])
        ev = self.conn.execute("SELECT from_value,to_value,note,actor FROM state_events WHERE entity_id=? AND field='company_number' "
                               "ORDER BY id", (co['id'],)).fetchall()
        self.assertEqual([tuple(e) for e in ev], [(None, '00000001', page, 'agent'), ('00000001', 'SC123456', page, 'agent'),
                                                  ('SC123456', None, page, 'agent')])

    def test_source_health_is_measured_from_reads_and_records_never_from_config(self):
        import datetime as dt
        job = records.create(self.conn, dict(type='job', name='Head of Engineering'))['record']
        self.conn.execute('INSERT INTO source_records VALUES(?,?,?)', ('board:ashby:x', 'j1', job['id']))
        today = dt.date.fromisoformat(job['created_at'][:10])
        read = lambda scope, state='ok', when=today.isoformat(), **extra: dict(scope=scope, provider=scope.split(':')[1], state=state,
                                                                             observed_at=when + 'T05:30:00Z', last_success=when, item_count=3, **extra)
        health = lambda channel, rows: records.channel_health(self.conn, channel, {r['scope']: r for r in rows}, today)
        boards = dict(runs='daily', measure=dict(provider_in=['ashby']))
        ok = health(boards, [read('board:ashby:x')])
        self.assertEqual((ok['health'], ok['count'], ok['new_7']), ('working', 1, 1))
        self.assertEqual(health(boards, [read('board:ashby:x', problem=True)])['health'], 'failing')
        self.assertEqual(health(boards, [read('board:ashby:x', when=(today - dt.timedelta(days=4)).isoformat())])['health'], 'stale')
        self.assertEqual(health(dict(runs='daily', measure=dict(scope_prefix='news:')), [read('news:techeu', state='empty')])['health'], 'empty')
        self.assertEqual(health(dict(runs='daily', measure=dict(provider_in=['uktn'])), [])['health'], 'no_reads')
        self.assertEqual(health(dict(runs='not-built'), [])['health'], 'not_built')
        self.assertEqual(health(dict(runs='off'), [])['health'], 'off')
        self.assertEqual(health(dict(runs='attended', method='research'), [])['health'], 'attended')
        for runs,want in (('off','off'),('not-built','not_built')):
            retained=health(dict(runs=runs,measure=dict(scope_prefix='news:')),[read('news:techeu')])
            self.assertEqual(retained['health'],want)
            self.assertEqual(retained['last_read'],today.isoformat()+'T05:30:00Z')
            self.assertEqual(retained['observed_count'],3)
            self.assertEqual(len(retained['sources']),1)

    def test_board_health_uses_current_provider_and_preserves_retired_yield(self):
        import datetime as dt
        today = dt.date.today()
        channel = dict(runs='daily', measure=dict(provider_in=['greenhouse', 'ashby']))
        old = dict(scope='board:greenhouse:example', provider='greenhouse', state='error', problem=True,
                   observed_at='2000-01-01', last_success='', item_count=90)
        current = dict(scope='board:ashby:example', provider='ashby', state='ok', problem=False,
                       observed_at=today.isoformat(), last_success=today.isoformat(), item_count=3)
        for source in (old, current):
            job = records.create(self.conn, dict(type='job', name=source['scope']))['record']
            self.conn.execute('INSERT INTO source_records VALUES(?,?,?)', (source['scope'], 'job', job['id']))
        result = records.channel_health(self.conn, channel, {s['scope']: s for s in (old, current)}, today,
                                        expected_board_scopes={current['scope']: 'Example'})
        self.assertEqual((result['health'], result['observed_count'], result['count']), ('working', 3, 2))
        self.assertEqual(result['last_read'], current['observed_at'])
        self.assertEqual(result['sources'], [current])
        self.assertEqual(result['retired_sources'], [old])
        self.assertEqual(result['unread_scopes'], [])
        current['problem'] = True
        result = records.channel_health(self.conn, channel, {current['scope']: current}, today,
                                        expected_board_scopes={current['scope']: 'Example'})
        self.assertEqual(result['health'], 'failing')

    def test_unread_and_disabled_boards_cannot_inherit_old_read_health(self):
        import datetime as dt
        today = dt.date.today()
        channel = dict(runs='daily', measure=dict(provider_in=['greenhouse', 'ashby']))
        old = dict(scope='board:greenhouse:example', provider='greenhouse', state='ok', problem=False,
                   observed_at=today.isoformat(), last_success=today.isoformat(), item_count=9)
        current = 'board:ashby:example'
        for expected, health in (({current: 'Example'}, 'no_reads'), ({}, 'off')):
            result = records.channel_health(self.conn, channel, {old['scope']: old}, today, expected_board_scopes=expected)
            self.assertEqual((result['health'], result['last_read'], result['observed_count']), (health, '', 0))
            self.assertEqual(result['retired_sources'], [old])
            self.assertEqual(result['unread_scopes'], list(expected))
            if expected:
                self.assertIn('not been read', result['sources'][0]['summary'])
        result = records.channel_health(self.conn, channel, {old['scope']: old}, today,
                                        expected_board_scopes={old['scope']: 'Old', current: 'New'})
        self.assertEqual(result['health'], 'partial')
        self.assertEqual(result['unread_scopes'], [current])

    def test_state_derives_enabled_board_scopes_from_shared_url_identity(self):
        from unittest.mock import patch
        root = Path(self.temp.name)
        (root / 'config').mkdir()
        (root / 'config/channels.yml').write_text('channels:\n  - id: employer-ats\n    runs: daily\n    measure: {provider_in: [greenhouse, ashby]}\n')
        (root / 'config/portals.yml').write_text('tracked_companies:\n  - {name: Example, careers_url: "https://jobs.ashbyhq.com/example?x=1"}\n  - {name: Retired, careers_url: "https://boards.greenhouse.io/old", enabled: false}\n')
        receipt = dict(started_at=db.now(), failures=['board:greenhouse:old', 'feed:adzuna:example'])
        self.conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                          ('ingest:test', '2026-10-04', 'partial', json.dumps(receipt)))
        with patch.object(workspace_state, 'ROOT', root):
            view = records.state(self.conn)
        channel = view['coverage']['channels'][0]
        self.assertEqual(channel['health'], 'no_reads')
        self.assertEqual(channel['unread_scopes'], ['board:ashby:example'])
        self.assertEqual(channel['sources'][0]['label'], 'Example')
        self.assertEqual(view['freshness']['failures'], ['feed:adzuna:example'])
        self.assertEqual(view['freshness']['retired_failures'], ['board:greenhouse:old'])
        self.assertEqual(view['coverage']['runs'][0]['receipt'], receipt)

    def test_merge_refuses_different_kinds(self):
        a = self.person()
        b = records.create(self.conn, dict(type='company', name='Org'))['record']
        with self.assertRaises(ValueError):
            records.merge(self.conn, a['id'], b['id'], {'revision': a['revision']})

    def test_merge_does_not_separate_outcomes_from_their_record_and_interactions(self):
        duplicate=self.person()
        keep=self.person()
        saved=records.outcome(self.conn,duplicate['id'],dict(kind='conversation',revision=duplicate['revision']))
        with self.assertRaisesRegex(ValueError,'recorded outcomes'):
            records.merge(self.conn,duplicate['id'],keep['id'],dict(revision=saved['record']['revision'],reason='Duplicate'))
        outcome=self.conn.execute('SELECT * FROM outcomes').fetchone()
        interaction=self.conn.execute('SELECT * FROM interactions WHERE id=?',(outcome['interaction_id'],)).fetchone()
        self.assertEqual((outcome['record_id'],interaction['record_id']),(duplicate['id'],duplicate['id']))
        self.assertFalse(records.get(self.conn,duplicate['id'])['archived_at'])

    def test_merge_keeps_original_evidence_and_exposes_it_on_the_surviving_record(self):
        keep = records.create(self.conn, dict(type='company', name='Acme'))['record']
        dup = records.create(self.conn, dict(type='company', name='Acme Ltd'))['record']
        news = json.dumps(dict(company='Acme', published='2026-09-28', amount='£5m'))
        research = json.dumps(dict(reviewed_at='2026-09-28', signal='Building an AI platform'))
        for rid in (dup['id'], keep['id']):
            records.add_observation(self.conn, rid, 'news:uktn', '2026-09-28', news)
        records.add_observation(self.conn, dup['id'], 'research:example.json', '2026-09-28', research)
        records.merge(self.conn, dup['id'], keep['id'], dict(revision=dup['revision'], reason='One company'))
        survivor = records.get(self.conn, keep['id'])
        self.assertEqual(len(survivor['signals']), 1)
        self.assertEqual([r['signal'] for r in survivor['research']], ['Building an AI platform'])
        self.assertEqual(len(records.get(self.conn, dup['id'])['signals']), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations WHERE record_id=?', (dup['id'],)).fetchone()[0], 2)

    def test_merge_never_rewinds_a_survivors_scanner_observation(self):
        keep = records.create(self.conn, dict(type='job', name='Role'))['record']
        dup = records.create(self.conn, dict(type='job', name='Duplicate'))['record']
        for rid, at in ((keep['id'], '2026-09-29T12:00:00Z'), (dup['id'], '2026-09-20T12:00:00Z')):
            records.add_observation(self.conn, rid, 'board:example', at,
                                    json.dumps(dict(source_scope='board:example', kind='job', last_seen_at=at, current=dict(title='Role'))))
        records.merge(self.conn, dup['id'], keep['id'], dict(revision=dup['revision'], reason='One advert'))
        row = self.conn.execute('SELECT observed_at,payload_json FROM observations WHERE record_id=?', (keep['id'],)).fetchone()
        self.assertEqual(row['observed_at'], '2026-09-29T12:00:00Z')
        self.assertEqual(json.loads(row['payload_json'])['last_seen_at'], '2026-09-29T12:00:00Z')

    def test_edit_requires_current_revision_and_preserves_first_write(self):
        r = self.person()
        records.edit(self.conn, r['id'], {'revision': r['revision'], 'known_context': 'Worked together'})
        with self.assertRaises(records.Conflict):
            records.edit(self.conn, r['id'], {'revision': r['revision'], 'name': 'Lost update'})
        self.assertEqual(records.get(self.conn, r['id'])['name'], 'Past colleague')

    def test_working_state_does_not_claim_outreach_or_advance_stage(self):
        r = records.create(self.conn, {'type':'opportunity','name':'Example','stage':'lead'})['record']
        records.followup(self.conn, dict(id=r['id'],revision=r['revision'],action='Review',status='done',owner='owner'))
        saved = records.get(self.conn, r['id'])
        self.assertEqual((saved['stage'],saved['working_state']),('lead','done'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 0)
        self.assertNotIn('due', json.dumps(records.state(self.conn)))

    def test_waiting_requires_context_and_invalid_write_is_atomic(self):
        r = self.person()
        with self.assertRaises(ValueError):
            records.followup(self.conn, dict(id=r['id'],revision=1,action='Reply',status='waiting',owner='owner'))
        self.assertEqual(records.get(self.conn,r['id'])['revision'],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM followups').fetchone()[0],0)

    def test_backdated_interaction_does_not_move_last_touch_backwards(self):
        r = self.person()
        records.interaction(self.conn, dict(record_id=r['id'],revision=1,at='2026-09-19',channel='call',direction='out',summary='Spoke'))
        records.interaction(self.conn, dict(record_id=r['id'],revision=2,at='2024-01-01',channel='email',direction='in',summary='Historical'))
        self.assertEqual(records.get(self.conn,r['id'])['last_touch'],'2026-09-19')
        records.interaction(self.conn, dict(record_id=r['id'],revision=3,at='2026-09-20',channel='email',direction='note',summary='Unsent draft'))
        self.assertEqual(records.get(self.conn,r['id'])['last_touch'],'2026-09-19')

    def test_linkedin_preview_accept_repeat_and_ambiguous_name(self):
        data='First Name,Last Name,URL,Email Address,Company,Position,Connected On\nAlex,Example,https://www.linkedin.com/in/alex-example/,,Former Co,Engineer,01 Jan 2020\n'
        first=records.import_linkedin(self.conn, data)
        self.assertEqual(first['queued'],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0],0)
        q=records.state(self.conn)['review_queue'][0]
        accepted=records.resolve_review(self.conn,q['id'],{'resolution':'accept','revision':q['revision']})['record']
        self.assertEqual(accepted['organisation'],'')  # export is not current employment verification
        self.assertIn('Former Co',accepted['known_context'])
        self.assertEqual(records.import_linkedin(self.conn,data)['queued'],0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='person'").fetchone()[0],1)

    def test_invalid_relationship_target_never_leaves_a_link(self):
        r=self.person()
        with self.assertRaises(ValueError):
            records.link(self.conn,dict(from_id=r['id'],to_id='missing',relation='former_colleague_at',revision=1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM links').fetchone()[0],0)

    def test_export_uses_live_state_and_omits_deadline(self):
        r=records.create(self.conn,{'type':'job','name':'Role','status':'candidate'})['record']
        records.transition(self.conn,r['id'],dict(field='status',to='pursue',revision=1))
        row=records.csv_rows(self.conn)[0]
        self.assertEqual(row['status'],'pursue')
        self.assertNotIn('action_due',row)

    def test_csv_batches_flat_rows_and_keeps_linked_archives_and_followups(self):
        r = self.person(notes='First line  \nSecond line\n')
        company = records.create(self.conn, dict(type='company', name='Archived partner'))['record']
        self.conn.execute('UPDATE records SET archived_at=? WHERE id=?', (db.now(), company['id']))
        self.conn.executemany('INSERT INTO links(from_id,to_id,relation) VALUES(?,?,?)', [
            (r['id'], company['id'], 'related'), (r['id'], company['id'], 'works_at'),
            (company['id'], r['id'], 'related')])
        records.followup(self.conn, dict(id=r['id'], action='Ask about delivery', owner='owner',
            status='waiting', reason='Awaiting context', waiting_on='Partner', revision=r['revision']))
        queries = []
        self.conn.set_trace_callback(queries.append)
        try:
            rows = records.csv_rows(self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['related_ids'], row['related_names']), (company['id'], 'Archived partner'))
        self.assertEqual((row['next_action'], row['action'], row['action_state'], row['action_owner']),
                         ('Ask about delivery', 'Ask about delivery', 'waiting', 'owner'))
        self.assertEqual((row['status_reason'], row['waiting_on']), ('Awaiting context', 'Partner'))
        self.assertEqual(row['notes'], 'First line\nSecond line')
        self.assertFalse({'followup', 'fields_json', 'status_raw', 'revision', 'research', 'advert', 'commercial_signals'} & row.keys())
        self.assertFalse(any('observations' in sql for sql in queries))
        self.assertLessEqual(len(queries), 6)
        with records.transaction(self.conn):
            self.conn.executemany('INSERT INTO records(id,type,name,created_at,updated_at) VALUES(?,?,?,?,?)',
                [(f'CP-csv-{i}', 'person', f'Person {i}', db.now(), db.now()) for i in range(1000)])
        queries.clear()
        self.conn.set_trace_callback(queries.append)
        try:
            rows = records.csv_rows(self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(rows), 1001)
        self.assertEqual([(r['type'], r['id']) for r in rows], sorted((r['type'], r['id']) for r in rows))
        self.assertLessEqual(len(queries), 6)

    def test_csv_uses_one_snapshot_for_records_and_linked_names(self):
        r = self.person()
        linked = records.create(self.conn, dict(type='company', name='Original name'))['record']
        records.link(self.conn, dict(from_id=r['id'], to_id=linked['id'], relation='works_at', revision=r['revision']))
        writer = db.connect(Path(self.temp.name) / 'test.db')
        changed = False
        def concurrent_edit(sql):
            nonlocal changed
            if not changed and 'FROM followups' in sql:
                changed = True
                writer.execute("UPDATE records SET name='New name' WHERE id=?", (linked['id'],))
        self.conn.set_trace_callback(concurrent_edit)
        try:
            rows = {v['id']: v for v in records.csv_rows(self.conn)}
        finally:
            self.conn.set_trace_callback(None)
            writer.close()
        self.assertTrue(changed)
        self.assertEqual(rows[linked['id']]['name'], 'Original name')
        self.assertEqual(rows[r['id']]['related_names'], 'Original name')
        self.assertEqual(records.get(self.conn, linked['id'])['name'], 'New name')

    def test_changed_linkedin_employment_is_reviewed_and_merge_preserves_current_role(self):
        data='First Name,Last Name,URL,Email Address,Company,Position,Connected On\nAlex,Example,https://www.linkedin.com/in/alex-example/,,Old Co,Engineer,01 Jan 2020\n'
        records.import_linkedin(self.conn,data)
        q=records.state(self.conn)['review_queue'][0]
        person=records.resolve_review(self.conn,q['id'],dict(revision=1,resolution='accept'))['record']
        records.edit(self.conn,person['id'],dict(revision=person['revision'],organisation='Verified employer',role='Lead'))
        self.assertEqual(records.import_linkedin(self.conn,data.replace('Old Co','New Co'))['queued'],1)
        q=records.state(self.conn)['review_queue'][0]
        self.assertEqual(q['payload']['matches'][0]['match'],'identifier')
        with self.assertRaises(ValueError): records.resolve_review(self.conn,q['id'],dict(revision=1,resolution='accept'))
        merged=records.resolve_review(self.conn,q['id'],dict(revision=1,resolution='merge',target_id=person['id']))['record']
        self.assertEqual((merged['organisation'],merged['role']),('Verified employer','Lead'))
        self.assertIn('New Co',merged['known_context'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM records').fetchone()[0],1)

    def test_ambiguous_name_import_requires_an_explicit_review_choice(self):
        records.create(self.conn,dict(type='person',name='Alex Example'))
        self.assertEqual(records.import_linkedin(self.conn,'First Name,Last Name\nAlex,Example\n')['queued'],1)
        q=records.state(self.conn)['review_queue'][0]
        self.assertEqual(q['payload']['matches'][0]['match'],'name only')
        records.resolve_review(self.conn,q['id'],dict(revision=1,resolution='reject'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM records').fetchone()[0],1)

    def test_a_name_differing_only_by_hyphen_or_dot_is_offered_as_the_same_person(self):
        # 27 Sep: a real contact's name without its hyphen, from LinkedIn, was not matched and became a duplicate.
        kept=records.create(self.conn,dict(type='person',name='Alex Sample-Harris'))['record']
        self.assertEqual(records.import_linkedin(self.conn,'First Name,Last Name\nAlex,Sample Harris\n')['queued'],1)
        q=records.state(self.conn)['review_queue'][0]
        self.assertEqual([(m['id'],m['match']) for m in q['payload']['matches']],[(kept['id'],'name only')])
        self.assertEqual([m['id'] for m in q['matches']],[kept['id']])
        self.assertEqual([m['id'] for m in records.person_matches(self.conn,{'name':'J. R. Smith'})],[])

    def test_freshness_reports_the_last_nightly_backup_from_its_receipt(self):
        self.assertNotIn('backup_at',records.state(self.conn)['freshness'])
        backups=Path(self.temp.name)/'backups'; backups.mkdir()
        (backups/'last-backup.json').write_text(json.dumps({'at':'2026-09-28T02:15:40Z','pushed':True}))
        self.assertEqual(records.state(self.conn)['freshness']['backup_at'],'2026-09-28T02:15:40Z')
        (backups/'last-backup.json').write_text('not json')
        self.assertNotIn('backup_at',records.state(self.conn)['freshness'])  # a broken receipt never breaks the page

    def test_a_draft_is_saved_replaced_and_cleared_with_history(self):
        r = self.person()
        records.draft(self.conn, dict(record_id=r['id'], revision=r['revision'], title='Profile', text='First text'), actor='agent')
        r = records.get(self.conn, r['id'])
        records.draft(self.conn, dict(record_id=r['id'], revision=r['revision'], title='Profile', text='Second text'), actor='agent')
        rows = self.conn.execute('SELECT title,text FROM drafts WHERE record_id=?', (r['id'],)).fetchall()
        self.assertEqual([tuple(x) for x in rows], [('Profile', 'Second text')])
        r = records.get(self.conn, r['id'])
        records.clear_draft(self.conn, dict(record_id=r['id'], revision=r['revision'], reason='Superseded'), actor='agent')
        self.assertIsNone(self.conn.execute('SELECT 1 FROM drafts WHERE record_id=?', (r['id'],)).fetchone())
        events = self.conn.execute("SELECT actor,note FROM state_events WHERE entity_id=? AND field='draft' ORDER BY id", (r['id'],)).fetchall()
        self.assertEqual([x['actor'] for x in events], ['agent', 'agent', 'agent'])
        self.assertTrue(all(x['note'] for x in events))

    def test_an_empty_oversized_or_stale_draft_writes_nothing(self):
        r = self.person()
        for bad in (dict(text=''), dict(text='   '), dict(text='x' * 8001)):
            with self.assertRaises(ValueError):
                records.draft(self.conn, dict(record_id=r['id'], revision=r['revision'], title='T', **bad))
        with self.assertRaises(records.Conflict):
            records.draft(self.conn, dict(record_id=r['id'], revision=r['revision'] + 1, title='T', text='Fine'))
        with self.assertRaises(ValueError):
            records.clear_draft(self.conn, dict(record_id=r['id'], revision=r['revision']))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM drafts').fetchone()[0], 0)

if __name__ == '__main__':
    unittest.main()


class OutreachRecordTests(unittest.TestCase):
    """Outreach focus (archive/plans/2026-10-14-outreach-focus.md): new outcome kinds, corrections,
    duplicate taps, the week's choices and handover, all through the one writer."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.temp.name) / 'test.db')
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_migration_keeps_outcome_rows_provenance_trigger_and_duplicate_index(self):
        import shutil, sqlite3
        old = Path(self.temp.name) / 'old'
        old.mkdir()
        for path in sorted(db.MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql')):
            if int(path.name[:3]) <= 10:
                shutil.copy(path, old / path.name)
        conn = db.connect(Path(self.temp.name) / 'before.db')
        db.migrate(conn, old)
        person = records.create(conn, dict(type='person', name='Before migration'))['record']
        conn.execute("INSERT INTO outcomes(record_id,kind,at,note,actor) VALUES(?,?,?,?,?)",
                     (person['id'], 'sent', '2026-10-01T10:00:00+01:00', 'kept', 'owner'))
        conn.commit()
        saved = dict(conn.execute('SELECT * FROM outcomes').fetchone())
        db.migrate(conn)
        row = conn.execute('SELECT * FROM outcomes').fetchone()
        self.assertEqual((row['id'], row['kind'], row['note'], row['corrected_at']), (saved['id'], 'sent', 'kept', None))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE outcomes SET snapshot_json='{}'")
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='outcomes'")}
        self.assertTrue({'outcomes_record', 'outcomes_draft_action'} <= indexes)
        person = records.get(conn, person['id'])
        later = records.outcome(conn, person['id'], dict(kind='followed_up', revision=person['revision']))['outcome']
        self.assertGreater(later['id'], saved['id'])
        conn.close()

    def test_new_outcome_kinds_are_scoped_by_record_type(self):
        kinds = lambda typ: {a['kind'] for a in records.states.outcome_actions(dict(type=typ))}
        person, agency, job, company = kinds('person'), kinds('agency'), kinds('job'), kinds('company')
        self.assertTrue({'followed_up', 'no_response', 'referral', 'submitted'} <= person)
        self.assertIn('submitted', agency)
        self.assertIn('submitted', job)
        self.assertNotIn('referral', company)
        self.assertNotIn('referral', job)
        self.assertNotIn('handed_over', person | agency | job | company)
        p = records.create(self.conn, dict(type='person', name='Someone'))['record']
        with self.assertRaises(ValueError):
            records.outcome(self.conn, p['id'], dict(kind='handed_over', revision=p['revision']))

    def test_a_cv_submission_records_which_company_it_went_to(self):
        # The first batch (15 October): which recruiter submitted the owner where, for the clash warning.
        org = records.create(self.conn, dict(type='company', name='ClearBank'))['record']
        r = records.create(self.conn, dict(type='person', name='A Recruiter', kind='recruiter'))['record']
        saved = records.outcome(self.conn, r['id'], dict(kind='submitted', revision=r['revision'], company=' ClearBank '))['outcome']
        self.assertEqual(saved['snapshot']['target'], dict(name='ClearBank', id=org['id']))
        r = records.get(self.conn, r['id'])
        unknown = records.outcome(self.conn, r['id'], dict(kind='submitted', revision=r['revision'], company='Somewhere New',
                                                          note='Second role'))['outcome']
        self.assertEqual(unknown['snapshot']['target'], dict(name='Somewhere New'))
        r = records.get(self.conn, r['id'])
        with self.assertRaises(ValueError):  # a company belongs only to a CV submission
            records.outcome(self.conn, r['id'], dict(kind='sent', revision=r['revision'], company='ClearBank'))

    def test_a_submission_requires_a_company_before_any_write(self):
        r = records.create(self.conn, dict(type='person', name='Example Recruiter', kind='recruiter'))['record']
        for company in (None, '', '  '):
            with self.subTest(company=company), self.assertRaisesRegex(ValueError, 'company'):
                records.outcome(self.conn, r['id'], dict(kind='submitted', revision=r['revision'], company=company))
        self.assertEqual(records.get(self.conn, r['id'])['revision'], r['revision'])
        for table in ('outcomes', 'interactions'):
            self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_same_draft_submission_to_two_companies_is_two_facts(self):
        r = records.create(self.conn, dict(type='person', name='Example Recruiter', kind='recruiter'))['record']
        draft = records.draft(self.conn, dict(record_id=r['id'], revision=r['revision'], text='Example profile'))
        payload = dict(kind='submitted', draft_version_id=draft['version_id'], company='Example One',
                       revision=draft['record']['revision'])
        first = records.outcome(self.conn, r['id'], payload)
        again = records.outcome(self.conn, r['id'], payload)
        self.assertEqual(again['outcome']['id'], first['outcome']['id'])
        second = records.outcome(self.conn, r['id'], dict(payload, company='Example Two', revision=first['record']['revision']))
        self.assertTrue(second['saved'])
        self.assertNotEqual(first['outcome']['id'], second['outcome']['id'])

    def test_submission_migration_preserves_evidence_and_changes_only_destination_uniqueness(self):
        import shutil, sqlite3
        old = Path(self.temp.name) / 'schema12'
        old.mkdir()
        for path in db.MIGRATIONS.glob('*.sql'):
            if int(path.name[:3]) <= 12:
                shutil.copy(path, old / path.name)
        conn = db.connect(Path(self.temp.name) / 'before13.db')
        self.addCleanup(conn.close)
        db.migrate(conn, old)
        r = records.create(conn, dict(type='person', name='Example Recruiter', kind='recruiter'))['record']
        draft = records.draft(conn, dict(record_id=r['id'], revision=r['revision'], text='Example profile'))
        first = records.outcome(conn, r['id'], dict(kind='submitted', company='Example One',
            revision=draft['record']['revision'], draft_version_id=draft['version_id']))
        before = dict(conn.execute('SELECT * FROM outcomes').fetchone())
        self.assertEqual(db.migrate(conn), ['013_submission_company.sql', '014_draft_purpose.sql', '015_questions.sql', '016_research_budget.sql', '017_automation.sql', '018_incoming_roles.sql', '019_approach_next_steps.sql', '020_outcomes_at.sql'])
        self.assertEqual(dict(conn.execute('SELECT * FROM outcomes').fetchone()), before)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE outcomes SET snapshot_json='{}'")
        second = records.outcome(conn, r['id'], dict(kind='submitted', company='Example Two',
            revision=first['record']['revision'], draft_version_id=draft['version_id']))
        self.assertTrue(second['saved'])
        # The database still protects a duplicate destination even if a caller bypasses the service.
        columns = ','.join(k for k in before if k != 'id')
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(f'INSERT INTO outcomes ({columns}) SELECT {columns} FROM outcomes WHERE id=?', (before['id'],))

    def test_a_repeated_tap_returns_the_first_save_even_with_an_old_revision(self):
        p = records.create(self.conn, dict(type='person', name='Someone'))['record']
        first = records.outcome(self.conn, p['id'], dict(kind='sent', revision=p['revision']))
        again = records.outcome(self.conn, p['id'], dict(kind='sent', revision=p['revision']))
        self.assertFalse(again['saved'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['outcome']['id'], first['outcome']['id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0], 1)
        reply = records.outcome(self.conn, p['id'], dict(kind='replied', revision=first['record']['revision']))
        self.assertTrue(reply['saved'])

    def test_correcting_an_outcome_marks_it_and_keeps_it_in_the_history(self):
        p = records.create(self.conn, dict(type='person', name='Someone'))['record']
        sent = records.outcome(self.conn, p['id'], dict(kind='sent', revision=p['revision']))
        fixed = records.correct(self.conn, p['id'], dict(outcome_id=sent['outcome']['id'], note='Tapped the wrong person',
                                                       revision=sent['record']['revision']))
        row = self.conn.execute('SELECT * FROM outcomes WHERE id=?', (sent['outcome']['id'],)).fetchone()
        self.assertTrue(row['corrected_at'])
        self.assertEqual((row['corrected_by'], row['correction_note']), ('owner', 'Tapped the wrong person'))
        view = records.state(self.conn)
        self.assertEqual(len(view['outcomes']), 1)
        self.assertTrue(view['outcomes'][0]['corrected_at'])
        with self.assertRaises(ValueError):
            records.correct(self.conn, p['id'], dict(outcome_id=sent['outcome']['id'], revision=fixed['record']['revision']))
        again = records.outcome(self.conn, p['id'], dict(kind='sent', revision=fixed['record']['revision']))
        self.assertTrue(again['saved'])

    def test_undoing_a_mistaken_send_takes_back_its_contact_and_last_touch(self):
        import record_views
        p = records.create(self.conn, dict(type='person', name='Someone'))['record']
        old = records.interaction(self.conn, dict(record_id=p['id'], revision=p['revision'], at='2015-11-27',
                                                  channel='email', direction='in', summary='An old email'))
        self.assertEqual(old['record']['last_touch'], '2015-11-27')
        sent = records.outcome(self.conn, p['id'], dict(kind='sent', channel='linkedin', revision=old['record']['revision']))
        self.assertNotEqual(sent['record']['last_touch'], '2015-11-27')
        fixed = records.correct(self.conn, p['id'], dict(outcome_id=sent['outcome']['id'], note='Tapped by mistake',
                                                       revision=sent['record']['revision']))
        self.assertEqual(fixed['record']['last_touch'], '2015-11-27')
        live = [i['summary'] for i in records.state(self.conn)['interactions'] if i['record_id'] == p['id']]
        self.assertEqual(live, ['An old email'])
        self.assertEqual([i['summary'] for i in record_views.timeline(self.conn, p['id'])['interactions']], ['An old email'])
        # The send and its correction both stay in the history.
        last = self.conn.execute("SELECT * FROM state_events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (p['id'],)).fetchone()
        self.assertIn('Corrected', last['note'])
        self.assertEqual(json.loads(last['changes_json'])['last_touch'],
                         dict(before=sent['record']['last_touch'], after='2015-11-27'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM interactions WHERE record_id=?', (p['id'],)).fetchone()[0], 2)

    def test_undoing_a_send_keeps_a_later_contact_date(self):
        p = records.create(self.conn, dict(type='person', name='Someone'))['record']
        sent = records.outcome(self.conn, p['id'], dict(kind='sent', revision=p['revision']))
        touch = sent['record']['last_touch']
        later = records.interaction(self.conn, dict(record_id=p['id'], revision=sent['record']['revision'],
                                                    at=touch[:10] + 'T23:59:59' + touch[19:],
                                                    channel='email', direction='in', summary='They wrote back'))
        fixed = records.correct(self.conn, p['id'], dict(outcome_id=sent['outcome']['id'], revision=later['record']['revision']))
        self.assertEqual(fixed['record']['last_touch'], later['record']['last_touch'])

    def test_correcting_a_mistaken_status_change_reverts_only_the_latest_one(self):
        job = records.create(self.conn, dict(type='job', name='Head of QA', status='pursue'))['record']
        job = records.transition(self.conn, job['id'], dict(field='status', to='applied', revision=job['revision']))['record']
        fixed = records.correct(self.conn, job['id'], dict(field='status', note='Not applied yet', revision=job['revision']))
        self.assertEqual(fixed['record']['status'], 'pursue')
        last = self.conn.execute("SELECT * FROM state_events WHERE entity_id=? AND field='status' ORDER BY id DESC LIMIT 1",
                                 (job['id'],)).fetchone()
        self.assertEqual((last['from_value'], last['to_value']), ('applied', 'pursue'))
        self.assertIn('Corrected', last['note'])
        with self.assertRaises(ValueError):
            records.correct(self.conn, job['id'], dict(field='status', revision=fixed['record']['revision']))

    def test_week_choices_are_stored_once_and_a_deferral_names_its_replacement(self):
        a = records.create(self.conn, dict(type='person', name='First'))['record']
        b = records.create(self.conn, dict(type='person', name='Second'))['record']
        items = [dict(action_id=f"action:{a['id']}:", record_id=a['id'], slot='known')]
        self.assertTrue(records.store_week(self.conn, '2026-10-12', items))
        self.assertFalse(records.store_week(self.conn, '2026-10-12', items))
        records.defer_week_item(self.conn, '2026-10-12', f"action:{a['id']}:", 'Not this week',
                                dict(action_id=f"action:{b['id']}:", record_id=b['id'], slot='known'))
        rows = {r['action_id']: dict(r) for r in self.conn.execute('SELECT * FROM week_items')}
        self.assertEqual(rows[f"action:{a['id']}:"]['state'], 'deferred')
        self.assertEqual(rows[f"action:{b['id']}:"]['state'], 'planned')
        self.assertIn('First', rows[f"action:{b['id']}:"]['reason'])
        self.assertEqual(rows[f"action:{a['id']}:"]['position'], rows[f"action:{b['id']}:"]['position'])
        with self.assertRaises(ValueError):
            records.defer_week_item(self.conn, '2026-10-12', 'action:unknown:', 'x', None)

    def test_putting_back_a_set_aside_item_takes_out_the_replacement_he_has_not_used(self):
        a = records.create(self.conn, dict(type='person', name='First'))['record']
        b = records.create(self.conn, dict(type='person', name='Second'))['record']
        c = records.create(self.conn, dict(type='person', name='Third'))['record']
        week = '2026-10-12'
        records.store_week(self.conn, week, [dict(action_id=f"action:{x['id']}:", record_id=x['id'], slot='known') for x in (a, c)])
        records.defer_week_item(self.conn, week, f"action:{a['id']}:", 'Not this week',
                                dict(action_id=f"action:{b['id']}:", record_id=b['id'], slot='known'))
        self.assertEqual(records.restore_week_item(self.conn, week, f"action:{a['id']}:", 'Your list', set()), f"action:{b['id']}:")
        rows = {r['action_id']: dict(r) for r in self.conn.execute('SELECT * FROM week_items WHERE week=?', (week,))}
        self.assertEqual(set(rows), {f"action:{a['id']}:", f"action:{c['id']}:"})
        self.assertEqual((rows[f"action:{a['id']}:"]['state'], rows[f"action:{a['id']}:"]['position'],
                          rows[f"action:{a['id']}:"]['reason']), ('planned', 0, 'Your list'))
        with self.assertRaises(ValueError):
            records.restore_week_item(self.conn, week, f"action:{a['id']}:", '', set())
        # A replacement he has acted on keeps its place: the week grows by one.
        records.defer_week_item(self.conn, week, f"action:{a['id']}:", 'Not this week',
                                dict(action_id=f"action:{b['id']}:", record_id=b['id'], slot='known'))
        self.assertIsNone(records.restore_week_item(self.conn, week, f"action:{a['id']}:", '', {f"action:{b['id']}:"}))
        states = dict(self.conn.execute('SELECT action_id, state FROM week_items WHERE week=?', (week,)).fetchall())
        self.assertEqual(states, {f"action:{x['id']}:": 'planned' for x in (a, b, c)})

    def test_retired_week_history_is_read_only_and_low_level_restore_remains_available(self):
        import approaches
        people = [records.create(self.conn, dict(type='person', name=f'Known {n}',
                                                relationship='warm', priority='first'))['record'] for n in range(2)]
        snapshot = records.state(self.conn)
        self.assertEqual(snapshot['intelligence']['week']['items'], [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM week_items').fetchone()[0], 0)
        week = approaches.week_of(approaches.london_today())
        first, swap = [dict(action_id=f"action:{p['id']}:", record_id=p['id'], slot='known') for p in people]
        records.store_week(self.conn, week, [first])
        records.defer_week_item(self.conn, week, first['action_id'], 'Historical deferral', swap)
        before = [tuple(r) for r in self.conn.execute('SELECT * FROM week_items ORDER BY action_id')]
        count = self.conn.total_changes
        snapshot = records.state(self.conn)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM week_items ORDER BY action_id')], before)
        self.assertEqual(self.conn.total_changes, count)
        self.assertEqual({r['action_id'] for r in snapshot['intelligence']['week']['items']},
                         {first['action_id'], swap['action_id']})
        self.assertTrue(all(not row['actions'] for row in snapshot['intelligence']['week']['items']))
        self.assertEqual(records.restore_week_item(self.conn, week, first['action_id'], 'Restored history', set()), swap['action_id'])
        rows = self.conn.execute('SELECT action_id,state FROM week_items').fetchall()
        self.assertEqual([tuple(r) for r in rows], [(first['action_id'],'planned')])

    def test_including_a_ready_person_adds_them_without_removing_the_week(self):
        import approaches
        kept = records.create(self.conn, dict(type='person', name='Kept Person'))['record']
        added = records.create(self.conn, dict(type='person', name='Added Person', priority='first'))['record']
        added = records.transition(self.conn, added['id'], dict(field='relationship', to='warm', revision=added['revision']))['record']
        week = approaches.week_of(approaches.london_today())
        records.store_week(self.conn, week, [dict(action_id='action:kept:', record_id=kept['id'], slot='known')])
        result = records.include_in_week(self.conn, added['id'])
        self.assertTrue(result['saved'])
        self.assertEqual(result['id'], added['id'])
        order = lambda: [r['record_id'] for r in self.conn.execute(
            'SELECT record_id FROM week_items WHERE week=? ORDER BY position', (week,))]
        # An explicit request goes to the top of the week, ahead of the automatic choice.
        self.assertEqual(order(), [added['id'], kept['id']])
        self.assertEqual(self.conn.execute('SELECT state FROM week_items WHERE record_id=?', (added['id'],)).fetchone()['state'], 'planned')
        self.assertFalse(records.include_in_week(self.conn, added['id'])['saved'])
        # Asking again for someone already saved lower down moves them to the top.
        self.conn.execute('UPDATE week_items SET position=5 WHERE record_id=?', (added['id'],))
        moved = records.include_in_week(self.conn, added['id'])
        self.assertTrue(moved['saved'] and moved['moved'])
        self.assertEqual(order(), [added['id'], kept['id']])

    def test_handover_creates_one_opportunity_carrying_the_approach(self):
        person = records.create(self.conn, dict(type='person', name='Dana Buyer', role='CTO'))['record']
        company = records.create(self.conn, dict(type='company', name='Example Co'))['record']
        person = records.link(self.conn, dict(from_id=person['id'], to_id=company['id'], relation='works_at',
                                              revision=person['revision']))['record']
        spoke = records.outcome(self.conn, person['id'], dict(kind='conversation', revision=person['revision']))
        result = records.handover(self.conn, person['id'], dict(revision=spoke['record']['revision'],
                                  next_step='Send a scoped proposal by Friday', company_id=company['id']))
        opp = result['opportunity']
        self.assertEqual((opp['type'], opp['stage']), ('opportunity', 'conversation'))
        self.assertIn('Example Co', opp['name'])
        self.assertEqual(opp['next_action'], 'Send a scoped proposal by Friday')
        linked = {(l['from_id'], l['to_id']) for l in records.state(self.conn)['links']}
        self.assertIn((opp['id'], person['id']), linked)
        self.assertIn((opp['id'], company['id']), linked)
        handed = self.conn.execute("SELECT * FROM outcomes WHERE kind='handed_over'").fetchone()
        self.assertEqual(json.loads(handed['snapshot_json'])['opportunity_id'], opp['id'])
        again = records.handover(self.conn, person['id'], dict(revision=result['record']['revision'], company_id=company['id']))
        self.assertFalse(again['saved'])
        self.assertEqual(again['opportunity']['id'], opp['id'])

    def test_undoing_a_handover_takes_its_untouched_opportunity_out_of_work(self):
        person = records.create(self.conn, dict(type='person', name='Dana Buyer'))['record']
        spoke = records.outcome(self.conn, person['id'], dict(kind='conversation', revision=person['revision']))
        first = records.handover(self.conn, person['id'], dict(revision=spoke['record']['revision']))
        handed = self.conn.execute("SELECT id FROM outcomes WHERE kind='handed_over'").fetchone()['id']
        records.correct(self.conn, person['id'], dict(outcome_id=handed, revision=first['record']['revision'], note='Mistake'))
        self.assertTrue(records.get(self.conn, first['opportunity']['id'])['archived_at'])
        # Handing over again makes one opportunity in Work, not a second beside the mistaken one.
        second = records.handover(self.conn, person['id'], dict(revision=records.get(self.conn, person['id'])['revision']))
        self.assertTrue(second['saved'])
        live = [r for r in self.conn.execute("SELECT id FROM records WHERE type='opportunity' AND archived_at IS NULL")]
        self.assertEqual([r['id'] for r in live], [second['opportunity']['id']])
        # Once the opportunity has moved on in Work, the handover stays: it is changed there.
        opp = second['opportunity']
        records.transition(self.conn, opp['id'], dict(field='stage', to='proposal', revision=opp['revision']))
        handed = self.conn.execute("SELECT id FROM outcomes WHERE kind='handed_over' AND corrected_at IS NULL").fetchone()['id']
        with self.assertRaises(ValueError):
            records.correct(self.conn, person['id'], dict(outcome_id=handed, revision=records.get(self.conn, person['id'])['revision']))
        self.assertFalse(records.get(self.conn, opp['id'])['archived_at'])
