"""Out-of-order source receipts must never rewind the current workspace."""
import json
import datetime as dt
from unittest.mock import patch
import evidence_checks
from pathlib import Path
import tempfile
import unittest

from tools.test_wo import wodb, wo


class ObservationImportCase(unittest.TestCase):
    def setUp(self):
        # Exercise historical receipt ordering with a contemporary admission clock.
        for owner, name, value in [(wodb, 'now', '2026-09-23T12:00:00Z'),
                                   (evidence_checks, 'utc_now', dt.datetime(2026,9,23,12,tzinfo=dt.timezone.utc))]:
            clock = patch.object(owner, name, return_value=value)
            clock.start(); self.addCleanup(clock.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.conn = wodb.connect(self.folder / 'test.db')
        wodb.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def observe(self, when, title='Engineering Manager — Current role', *, scope='hn:who-is-hiring:1',
                lifecycle='active', closed_at=None, state='ok', item=True):
        payload = dict(source_scope=scope, source_id='job1', kind='job',
                       current=dict(title=title, company='Example', url='https://example.org/job', location='London', eligible=True),
                       last_seen_at=when, source_lifecycle=lifecycle, closed_at=closed_at)
        data = dict(generated_at=closed_at or when,
                    sources={scope: dict(provider='hn', observed_at=closed_at or when,
                                        state=state, complete=state == 'ok', item_ids=['job1'] if item else [])},
                    items={'job': payload} if item else {})
        (self.folder / 'observations.json').write_text(json.dumps(data))
        with patch.object(wodb, 'now', return_value=when), patch.object(evidence_checks, 'utc_now',
                return_value=dt.datetime.fromisoformat(when.replace('Z', '+00:00'))):
            return wo.cmd_import_scans(self.conn, self.folder)

    def record(self):
        return dict(self.conn.execute("SELECT * FROM records WHERE type='job'").fetchone())


class ObservationImportTests(ObservationImportCase):
    def test_scan_import_decodes_prior_clocks_before_taking_write_lock(self):
        import record_imports
        before = '2026-09-22T10:00:00Z'
        self.observe(before)
        advance = record_imports._advance_observation_clock
        prior_reads = []
        def check(clocks, item):
            if item.get('last_seen_at') == before:
                prior_reads.append(self.conn.in_transaction)
            return advance(clocks, item)
        with patch.object(record_imports, '_advance_observation_clock', side_effect=check):
            self.observe('2026-09-23T10:00:00Z', 'Engineering Manager — Updated title')
        self.assertEqual(prior_reads, [False])
        self.assertEqual(self.record()['name'], 'Engineering Manager — Updated title')

    def test_concurrent_observation_recomputes_prepared_clocks(self):
        import record_imports
        self.observe('2026-09-21T10:00:00Z')
        read = record_imports._observation_watermarks
        reads = []
        def concurrent_write(conn):
            clocks = read(conn)
            if conn is self.conn:
                reads.append(clocks)
                if len(reads) == 1:
                    other = wodb.connect(self.folder / 'test.db')
                    try:
                        payload = json.loads((self.folder / 'observations.json').read_text())['items']['job']
                        payload['last_seen_at'] = '2026-09-23T10:00:00Z'
                        payload['current']['title'] = 'Engineering Manager — Concurrent newer title'
                        with record_imports.store.transaction(other):
                            record_imports.import_observations(other, self.folder,
                                data={'sources': {}, 'items': {'job': payload}})
                    finally:
                        other.close()
            return clocks
        with patch.object(record_imports, '_observation_watermarks', side_effect=concurrent_write):
            report = self.observe('2026-09-22T10:00:00Z', 'Engineering Manager — Stale candidate')
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.record()['name'], 'Engineering Manager — Concurrent newer title')
        self.assertEqual(report['stale_observations'], 1)

    def test_malformed_unrelated_history_still_fails_closed(self):
        import record_imports
        self.observe('2026-09-21T10:00:00Z')
        with self.conn:
            self.conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES"
                              "('CO-unrelated','company','Unrelated fixture','','')")
            self.conn.execute('INSERT INTO observations(record_id,source,observed_at,payload_json,payload_sha) '
                              'VALUES(?,?,?,?,?)', ('CO-unrelated', 'fixture:unrelated',
                              '2026-09-21T10:00:00Z', '{invalid', 'invalid'))
        before = wo.dump(self.conn)['tables']
        with self.assertRaises(json.JSONDecodeError):
            record_imports._observation_watermarks(self.conn)
        with self.assertRaises(json.JSONDecodeError):
            with record_imports.store.transaction(self.conn):
                record_imports.import_observations(self.conn, self.folder, data={'sources': {}, 'items': {}})
        self.assertEqual(wo.dump(self.conn)['tables'], before)

    def test_same_batch_email_sequences_advance_prepared_clocks(self):
        self.observe('2026-09-22T10:00:00Z', scope='alert:jackandjill')
        path = self.folder / 'observations.json'
        data = json.loads(path.read_text())
        item = data['items']['job']
        data['items'] = {
            'newer': {**item, 'current': {**item['current'], 'receipt_sequence': 3, 'rate': 'GBP 120000/year'}},
            'older': {**item, 'current': {**item['current'], 'receipt_sequence': 2, 'rate': 'GBP 100000/year'}},
        }
        path.write_text(json.dumps(data))
        wo.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(self.record()['pay'], 'GBP 120000/year')

    def test_caller_owned_transaction_still_rolls_back_scan_import(self):
        import record_imports
        self.observe('2026-09-21T10:00:00Z')
        before = wo.dump(self.conn)['tables']
        with self.assertRaisesRegex(ValueError, 'Caller cancelled'):
            with record_imports.store.transaction(self.conn):
                self.observe('2026-09-22T10:00:00Z', 'Engineering Manager — Cancelled change')
                self.assertEqual(self.record()['name'], 'Engineering Manager — Cancelled change')
                raise ValueError('Caller cancelled')
        self.assertEqual(wo.dump(self.conn)['tables'], before)

    def test_old_facts_are_archived_without_rewinding_facts_or_health(self):
        self.observe('2026-09-22T10:00:00Z', 'Engineering Manager — New title')
        before = self.record()
        report = self.observe('2026-09-21T10:00:00Z', 'Engineering Manager — Old title')
        self.assertEqual(self.record(), before)
        self.assertEqual(report['stale_observations'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 2)
        health = dict(self.conn.execute('SELECT * FROM source_health').fetchone())
        self.assertEqual(health['observed_at'], '2026-09-22T10:00:00Z')
        self.assertEqual(health['last_success'], '2026-09-22T10:00:00Z')

    def test_closure_uses_evidence_time_and_old_presence_cannot_reopen(self):
        self.observe('2026-09-20T10:00:00Z')
        self.observe('2026-09-20T10:00:00Z', lifecycle='closed', closed_at='2026-09-22T10:00:00Z')
        self.assertEqual(self.record()['source_lifecycle'], 'closed')
        self.observe('2026-09-21T10:00:00Z')
        self.assertEqual(self.record()['source_lifecycle'], 'closed')
        self.observe('2026-09-23T10:00:00Z', 'Engineering Manager — Reopened role')
        self.assertEqual(self.record()['source_lifecycle'], 'active')
        self.assertEqual(self.record()['name'], 'Engineering Manager — Reopened role')

    def test_source_failure_stays_visible_when_older_success_arrives(self):
        self.observe('2026-09-23T10:00:00Z', state='error', item=False)
        self.observe('2026-09-22T10:00:00Z')
        health = dict(self.conn.execute('SELECT * FROM source_health').fetchone())
        self.assertEqual(health['state'], 'error')
        self.assertEqual(health['observed_at'], '2026-09-23T10:00:00Z')
        self.assertEqual(health['last_success'], '2026-09-22T10:00:00Z')

    def test_older_second_source_cannot_rewind_shared_record(self):
        self.observe('2026-09-22T10:00:00Z', 'Engineering Manager — New title')
        self.observe('2026-09-21T10:00:00Z', 'Engineering Manager — Old title', scope='hn:who-is-hiring:2')
        self.assertEqual(self.record()['name'], 'Engineering Manager — New title')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 2)

    def test_equivalent_timezone_receipt_is_not_newer(self):
        self.observe('2026-09-22T10:00:00Z', 'Engineering Manager — New title')
        self.observe('2026-09-22T11:00:00+01:00', 'Engineering Manager — Conflicting title')
        self.assertEqual(self.record()['name'], 'Engineering Manager — New title')

    def test_email_sequence_orders_equal_time_imports_without_overriding_other_sources(self):
        import record_imports
        when = '2026-09-22T10:00:00Z'
        def ingest(sequence, pay, *, scope='alert:jackandjill', sid='job1', at=when):
            payload = dict(source_scope=scope, source_id=sid, kind='job', last_seen_at=at,
                           source_lifecycle='active', current=dict(title='Engineering Manager', company='Example',
                           url='https://example.org/job', location='London', eligible=True, rate=pay,
                           received_at=at, receipt_sequence=sequence))
            with self.conn:
                record_imports.import_observations(self.conn, self.folder, data=dict(items={'job': payload}, sources={}))
        ingest(1, 'GBP 90000/year')
        ingest(2, 'GBP 100000/year', at='2026-09-22T12:00:00+02:00')
        self.assertEqual(self.record()['pay'], 'GBP 100000/year')
        before = self.record()
        ingest(2, 'GBP 105000/year')
        self.assertEqual(self.record(), before)
        ingest(1, 'GBP 90000/year')
        self.assertEqual(self.record(), before)
        ingest(999, 'GBP 95000/year', at='2026-09-22T09:59:00Z')
        self.assertEqual(self.record(), before)
        ingest(99, 'GBP 110000/year', scope='alert:linkedin')
        self.assertEqual(self.record(), before)
        ingest(100, 'GBP 120000/year', scope='alert:linkedin')
        self.assertEqual(self.record(), before)
        # Equal-time competing evidence must not confer a later preference on either source.
        ingest(100, 'GBP 120000/year')
        self.assertEqual(self.record(), before)
        ingest(101, 'GBP 120000/year', scope='alert:linkedin')
        self.assertEqual(self.record(), before)
        ingest(101, 'GBP 130000/year', at='2026-09-22T11:00:00Z')
        self.assertEqual(self.record()['pay'], 'GBP 130000/year')
        before = self.record()
        ingest(102, 'GBP 140000/year', sid='different-job', at='2026-09-22T11:00:00Z')
        self.assertEqual(self.record(), before)

    def test_encoded_advert_is_normalized_before_becoming_current_summary(self):
        self.observe('2026-09-22T10:00:00Z')
        path = self.folder / 'observations.json'
        data = json.loads(path.read_text())
        data['items']['job']['last_seen_at'] = '2026-09-23T10:00:00Z'
        data['items']['job']['current']['text'] = '&lt;p&gt;Lead our test &amp;amp; platform team.&lt;/p&gt;'
        path.write_text(json.dumps(data))
        wo.cmd_import_scans(self.conn,self.folder)
        self.assertEqual(self.record()['summary'],'Lead our test & platform team.')
        payloads = [r[0] for r in self.conn.execute('SELECT payload_json FROM observations')]
        self.assertTrue(any('&lt;p&gt;' in p for p in payloads))


T1, T2, T3 = '2026-09-20T06:30:00Z', '2026-09-21T06:30:00Z', '2026-09-30T06:30:00Z'


class CrossSourceTests(unittest.TestCase):
    """One role seen by several sources is one record; distinct adverts are flagged, never merged."""

    def setUp(self):
        # Exercise historical receipt ordering with a contemporary admission clock.
        for owner, name, value in [(wodb, 'now', '2026-09-23T12:00:00Z'),
                                   (evidence_checks, 'utc_now', dt.datetime(2026,9,23,12,tzinfo=dt.timezone.utc))]:
            clock = patch.object(owner, name, return_value=value)
            clock.start(); self.addCleanup(clock.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.conn = wodb.connect(self.folder / 'test.db')
        wodb.migrate(self.conn)
        self.items = {}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def see(self, scope, sid, title, company, url, when, lifecycle='active', **facts):
        self.items[scope + '\x1f' + sid] = dict(source_scope=scope, source_id=sid, kind='job', last_seen_at=when,
                                                source_lifecycle=lifecycle, current=dict(title=title, company=company, url=url, eligible=True,
                                                text='Lead software engineering teams and improve release readiness.', **facts))

    def run_import(self, when):
        (self.folder / 'observations.json').write_text(json.dumps(dict(generated_at=when, sources={}, items=self.items)))
        with patch.object(wodb, 'now', return_value=when), patch.object(evidence_checks, 'utc_now',
                return_value=dt.datetime.fromisoformat(when.replace('Z', '+00:00'))):
            return wo.cmd_import_scans(self.conn, self.folder)

    def jobs(self):
        return [dict(r) for r in self.conn.execute("SELECT id,name,url,source_lifecycle FROM records WHERE type='job' AND archived_at IS NULL ORDER BY id")]

    def test_one_role_seen_by_two_sources_is_one_record_keeping_the_first_facts(self):
        self.see('board:greenhouse:meta', '1', 'QA Engineering Lead', 'Meta', 'https://meta.example/1', T1)
        self.run_import(T1)
        self.see('feed:adzuna:London', '99', 'QA Engineering Lead', 'Meta ', 'https://adzuna.example/99', T2)
        self.run_import(T2)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['url'], 'https://meta.example/1')
        scopes = {r[0] for r in self.conn.execute('SELECT source_scope FROM source_records')}
        self.assertEqual(scopes, {'board:greenhouse:meta', 'feed:adzuna:London'})

    def test_a_source_rename_updates_matching_keys_before_later_sightings(self):
        self.see('board:greenhouse:co', '1', 'Head of Quality', 'Co', 'https://co.example/1', T1)
        self.run_import(T1)
        self.see('board:greenhouse:co', '1', 'Director of Engineering', 'Co', 'https://co.example/1', T2)
        self.see('feed:reed:uk', '2', 'Head of Quality', 'Co', 'https://reed.example/2', T2)
        self.see('feed:adzuna:uk', '3', 'Director of Engineering', 'Co', 'https://adzuna.example/3', T2)
        report = self.run_import(T2)
        self.assertEqual({r['name'] for r in self.jobs()}, {'Head of Quality', 'Director of Engineering'})
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(report['attached_sightings'], 1)
        ids = {r['source_scope']: r['record_id'] for r in self.conn.execute('SELECT * FROM source_records')}
        self.assertEqual(ids['board:greenhouse:co'], ids['feed:adzuna:uk'])
        self.assertNotEqual(ids['board:greenhouse:co'], ids['feed:reed:uk'])

    def test_a_protected_title_retains_its_match_key_after_a_source_rename(self):
        self.see('board:greenhouse:co', '1', 'Head of Quality', 'Co', 'https://co.example/1', T1)
        self.run_import(T1)
        rid = self.jobs()[0]['id']
        self.conn.execute("INSERT INTO record_edits(record_id,field,actor) VALUES(?, 'name', 'owner')", (rid,))
        self.see('board:greenhouse:co', '1', 'Director of Engineering', 'Co', 'https://co.example/1', T2)
        self.see('feed:reed:uk', '2', 'Head of Quality', 'Co', 'https://reed.example/2', T2)
        report = self.run_import(T2)
        self.assertEqual([(r['id'], r['name']) for r in self.jobs()], [(rid, 'Head of Quality')])
        self.assertEqual(report['attached_sightings'], 1)
        self.assertEqual(report['conflicts'][0]['fields']['name'], 'Director of Engineering')

    def test_a_role_match_never_overwrites_a_record_no_source_created(self):
        self.conn.execute("INSERT INTO records(id,type,name,organisation,url,summary,source_file,created_at,updated_at) "
                          "VALUES('Job009','job','Programme Test Lead','La Fosse','https://lafosse.example/9','The owner noted this',"
                          "'commercial/pipeline.md','2026-09-01','2026-09-01')")
        self.see('feed:reed:London', '7', 'Programme Test Lead', 'La Fosse', 'https://reed.example/7', T1)
        self.items['feed:reed:London\x1f7']['current']['summary'] = 'Feed text'
        self.run_import(T1)
        self.run_import(T2)
        job = dict(self.conn.execute("SELECT url,summary FROM records WHERE id='Job009'").fetchone())
        self.assertEqual(job, {'url': 'https://lafosse.example/9', 'summary': 'The owner noted this'})
        self.assertEqual(len(self.jobs()), 1)

    def test_different_stated_locations_do_not_attach_cross_source_sightings(self):
        self.see('board:greenhouse:co', '1', 'Head of Engineering', 'Co', 'https://co.example/1', T1, location='London')
        self.see('feed:reed:uk', '2', 'Head of Engineering', 'Co', 'https://reed.example/2', T2, location='Manchester')
        report = self.run_import(T2)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(report['attached_sightings'], 0)
        self.assertEqual(report['possible_matches'], 1)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT location FROM records WHERE type='job'")}, {'London', 'Manchester'})

    def test_different_stated_engagements_do_not_attach_cross_source_sightings(self):
        self.see('board:greenhouse:co', '1', 'Head of Engineering', 'Co', 'https://co.example/1', T1, employment_type='contract')
        self.see('feed:reed:uk', '2', 'Head of Engineering', 'Co', 'https://reed.example/2', T2, employment_type='permanent')
        report = self.run_import(T2)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(report['attached_sightings'], 0)
        self.assertEqual(report['possible_matches'], 1)

    def test_distinct_adverts_are_flagged_never_merged(self):
        self.see('feed:adzuna:London', '1', 'QA Engineering Lead', 'Meta', 'https://adzuna.example/1', T1)
        self.see('feed:adzuna:London', '2', 'QA Engineering Lead', 'Meta', 'https://adzuna.example/2', T1)
        self.see('feed:adzuna:London', '3', 'QA Engineering Lead, AI Native', 'Meta', 'https://adzuna.example/3', T1)
        self.run_import(T1)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 3)
        links = [tuple(r) for r in self.conn.execute("SELECT from_id,to_id FROM links WHERE relation='possible_match'")]
        self.assertEqual(len(links), 1)
        native = next(j['id'] for j in jobs if 'AI Native' in j['name'])
        self.assertNotIn(native, [x for link in links for x in link])

    def test_a_presence_only_sighting_keeps_a_role_open_only_while_recent(self):
        self.see('board:greenhouse:meta', '1', 'QA Engineering Lead', 'Meta', 'https://meta.example/1', T1)
        self.see('feed:adzuna:London', '99', 'QA Engineering Lead', 'Meta', 'https://adzuna.example/99', T1)
        self.run_import(T1)
        self.see('board:greenhouse:meta', '1', 'QA Engineering Lead', 'Meta', 'https://meta.example/1', T1, lifecycle='closed')
        self.items['board:greenhouse:meta\x1f1']['closed_at'] = T2
        self.run_import(T2)
        self.assertEqual(self.jobs()[0]['source_lifecycle'], 'active')  # the feed saw it a day ago
        self.items['board:greenhouse:meta\x1f1']['closed_at'] = T3
        self.run_import(T3)
        self.assertEqual(self.jobs()[0]['source_lifecycle'], 'closed')  # the feed sighting is now stale



class DailyAuditTests(ObservationImportCase):
    def test_actual_changes_keep_before_after_and_repeat_import_is_quiet(self):
        self.observe('2026-09-22T10:00:00Z', 'Engineering Manager — First title')
        before = self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0]
        self.observe('2026-09-23T10:00:00Z', 'Engineering Manager — First title')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0], before)
        self.observe('2026-09-24T10:00:00Z', 'Engineering Manager — New title')
        row = self.conn.execute("SELECT * FROM state_events WHERE field='source_observation' ORDER BY id DESC").fetchone()
        self.assertEqual(json.loads(row['changes_json'])['name'], {'before':'Engineering Manager — First title', 'after':'Engineering Manager — New title'})
        self.assertEqual(row['source_scope'], 'hn:who-is-hiring:1')
        self.assertTrue(row['recorded_at'])
        self.assertEqual(row['at'], '2026-09-24T10:00:00Z')

    def test_run_association_and_rollback_are_atomic(self):
        self.observe('2026-09-22T10:00:00Z')
        payload = json.loads((self.folder/'observations.json').read_text())
        payload['items']['job']['current']['title'] = 'Changed'
        payload['items']['job']['last_seen_at'] = '2026-09-24T10:00:00Z'
        (self.folder/'observations.json').write_text(json.dumps(payload))
        wo.cmd_import_scans(self.conn, self.folder, run_id='ingest:test')
        row = self.conn.execute("SELECT * FROM state_events WHERE run_id='ingest:test'").fetchone()
        self.assertEqual(json.loads(row['changes_json'])['name']['after'], 'Changed')
        from unittest.mock import patch
        before = wo.dump(self.conn)['tables']
        payload['items']['job']['current']['title'] = 'Must roll back'
        payload['items']['job']['last_seen_at'] = '2026-09-25T10:00:00Z'
        (self.folder/'observations.json').write_text(json.dumps(payload))
        import record_imports
        original = record_imports.import_observations
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('Interrupted import')
        with patch.object(record_imports, 'import_observations', side_effect=fail):
            with self.assertRaises(ValueError):
                wo.cmd_import_scans(self.conn, self.folder, run_id='ingest:failed')
        self.assertEqual(wo.dump(self.conn)['tables'], before)

    def test_research_records_changed_values_without_repeating_identical_evidence(self):
        self.observe('2026-09-22T10:00:00Z')
        rid = self.record()['id']
        path = self.folder/'research.json'
        path.write_text(json.dumps([dict(id=rid, reviewed_at='2026-09-24', summary='Reviewed summary', source_urls=['https://example.org'])]))
        wo.import_research(self.conn, path)
        row = self.conn.execute("SELECT * FROM state_events WHERE field='evidence_review'").fetchone()
        self.assertEqual(json.loads(row['changes_json'])['summary'], {'before':'', 'after':'Reviewed summary'})
        self.assertEqual(row['source_scope'], 'research:research.json')
        count = self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0]
        wo.import_research(self.conn, path)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM state_events').fetchone()[0], count)


if __name__ == "__main__":
    unittest.main()
