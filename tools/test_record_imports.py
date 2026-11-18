"""Import replay and commercial signals use shared identity without changing workflow state."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / 'workspace')]
import db
import record_imports as imports
import records


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.conn = db.connect(self.folder / 'test.db')
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)


class ImportTests(DatabaseCase):
    def test_import_can_fill_unknown_manual_employer_without_changing_saved_facts(self):
        job = records.save_job(self.conn, dict(name='My title', organisation='', notes='My note',
                                              url='https://www.linkedin.com/jobs/view/123456/'))['record']
        item = dict(kind='job', source_scope='alert:linkedin', source_id='123456', last_seen_at='2026-10-06T00:00:00Z',
                    current=dict(company='Known Employer', title='Source title', eligible=True,
                                 url='https://www.linkedin.com/comm/jobs/view/123456/?trk=email'))
        imports.import_observations(self.conn, self.folder, dict(items={'one': item}, sources={}))
        current = records.get(self.conn, job['id'])
        self.assertEqual((current['organisation'], current['name'], current['notes'], current['status']),
                         ('Known Employer', 'My title', 'My note', 'pursue'))
        link = self.conn.execute('SELECT r.name FROM links l JOIN records r ON r.id=l.to_id WHERE l.from_id=?', (job['id'],)).fetchone()
        self.assertEqual(link['name'], 'Known Employer')

    def test_manual_save_recognises_an_alternate_url_already_attached_as_source_evidence(self):
        original = records.create(self.conn, dict(type='job', name='Engineering Manager', organisation='Example',
                                                  url='https://example.test/employer-advert', status='applied'))['record']
        item = dict(kind='job', source_scope='alert:linkedin', source_id='333', last_seen_at='2026-10-06T00:00:00Z',
                    current=dict(company='Example', title='Engineering Manager', eligible=True,
                                 url='https://www.linkedin.com/comm/jobs/view/333/?trk=email'))
        imports.import_observations(self.conn, self.folder, dict(items={'one': item}, sources={}))
        saved = records.save_job(self.conn, dict(name='LinkedIn title', organisation='Example', url='https://www.linkedin.com/jobs/view/333/'))
        self.assertFalse(saved['created'])
        self.assertEqual((saved['record']['id'], saved['record']['status']), (original['id'], 'applied'))

    def test_observation_attaches_to_manual_canonical_url_before_company_title_match(self):
        saved = records.save_job(self.conn, dict(url='https://uk.linkedin.com/jobs/view/manager-at-example-123456?tracking=x',
                                               name='My title', organisation='My employer', notes='Keep my notes'))['record']
        records.edit(self.conn, saved['id'], dict(revision=saved['revision'], cv_override='QELead'))
        other = records.create(self.conn, dict(type='job', name='Source title', organisation='Source employer'))['record']
        item = dict(kind='job', source_scope='alert:linkedin', source_id='123456', last_seen_at='2026-10-06T00:00:00Z',
                    current=dict(company='Source employer', title='Source title', eligible=True, cv_override='Manager',
                                 url='https://www.linkedin.com/comm/jobs/view/123456/?trk=email', alert_date='2026-10-05T12:00:00Z'))
        report = imports.import_observations(self.conn, self.folder, dict(items={'one': item}, sources={}))
        self.assertEqual(report['new_observed'], 0)
        self.assertEqual(self.conn.execute('SELECT record_id FROM source_records').fetchone()[0], saved['id'])
        result = records.get(self.conn, saved['id'])
        self.assertEqual((result['name'], result['organisation'], result['notes'], result['status'], result['cv_override']),
                         ('My title', 'My employer', 'Keep my notes', 'pursue', 'QELead'))
        self.assertEqual(result['date'], '')
        observed = json.loads(self.conn.execute('SELECT payload_json FROM observations').fetchone()[0])
        self.assertEqual(observed['current']['alert_date'], '2026-10-05T12:00:00Z')
        self.assertEqual(records.get(self.conn, other['id'])['revision'], other['revision'])

    def test_legacy_scan_does_not_duplicate_manual_linkedin_job(self):
        saved = records.save_job(self.conn, dict(url='https://www.linkedin.com/jobs/view/123456/', name='My title', organisation='Example'))['record']
        self.scan('2026-10-06', [dict(company='Different employer', title='Source title', url='https://www.linkedin.com/jobs/view/title-123456?x=1')])
        report = imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(report['url_already_recorded'], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)
        self.assertEqual(records.get(self.conn, saved['id'])['status'], 'pursue')

    def test_scan_canonical_url_follows_merge_and_reports_plain_archive_without_recreating(self):
        original = records.create(self.conn, dict(type='job', name='Original', url='https://www.linkedin.com/jobs/view/222?trk=old'))['record']
        survivor = records.create(self.conn, dict(type='job', name='Survivor', url='https://example.test/job'))['record']
        records.merge(self.conn, original['id'], survivor['id'], dict(revision=original['revision']))
        item = dict(kind='job', source_scope='alert:linkedin', source_id='222', last_seen_at='2026-10-06T00:00:00Z',
                    current=dict(company='Example', title='Title', eligible=True, url='https://www.linkedin.com/jobs/view/title-222/'))
        report = imports.import_observations(self.conn, self.folder, dict(items={'one': item}, sources={}))
        self.assertEqual(report['new_observed'], 0)
        self.assertEqual(self.conn.execute('SELECT record_id FROM source_records').fetchone()[0], survivor['id'])
        current = records.get(self.conn, survivor['id'])
        records.archive(self.conn, survivor['id'], dict(revision=current['revision'], reason='No longer relevant'))
        report = imports.import_observations(self.conn, self.folder, dict(items={'one': item}, sources={}))
        self.assertEqual(report['new_observed'], 0)
        self.assertTrue(report['conflicts'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 2)

    def scan(self, date, rows):
        columns = ['date_seen', 'company', 'title', 'url', 'why']
        (self.folder / (date + '.tsv')).write_text('\t'.join(columns) + '\n' + '\n'.join(
            '\t'.join(row.get(c, '') for c in columns) for row in rows) + '\n')

    def test_root_scan_files_are_parsed_once_per_import_and_replayed_after_changes(self):
        row = dict(company='Example', title='Head of Quality', url='https://example.test/job', why='First')
        self.scan('2026-10-01', [row])
        self.scan('2026-10-02', [{**row, 'why': 'Latest raw evidence'}])
        with patch.object(imports.ed, 'scan_tsv', wraps=imports.ed.scan_tsv) as read:
            imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(read.call_count, 2)
        job = dict(self.conn.execute("SELECT * FROM records WHERE type='job'").fetchone())
        self.assertTrue(job['source_file'].endswith('/2026-10-01.tsv'))
        self.assertEqual(json.loads(job['fields_json'])['scan']['why'], 'Latest raw evidence')
        self.scan('2026-10-01', [{**row, 'title': 'Director of Engineering'}])
        imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(self.conn.execute("SELECT name FROM records WHERE id=?", (job['id'],)).fetchone()[0],
                         'Director of Engineering')

    def test_replay_imports_newly_eligible_observation_without_a_new_tsv(self):
        item = dict(kind='job', source_scope='board:example', source_id='1', last_seen_at='2026-10-01T00:00:00Z',
                    current=dict(company='Example', title='Head of Quality', url='https://example.test/job', eligible=False))
        data = dict(sources={}, items={'one': item})
        path = self.folder / 'observations.json'
        path.write_text(json.dumps(data))
        imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        item['current']['eligible'] = True
        item['last_seen_at'] = db.now()  # A fresh qualifying sighting, not an old stale advert.
        path.write_text(json.dumps(data))
        imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)

    def test_new_unsuitable_jobs_are_filtered_but_held_evidence_survives(self):
        bad = [dict(title='Junior Engineer'), dict(text='Lead electrical engineering and plant maintenance.'),
               dict(location='United States (Remote)'), dict(pay_min='40000', pay_max='50000', pay_currency='GBP', pay_period='year')]
        for n, fields in enumerate(bad):
            current = dict(company='Example', title='Engineering Manager', url=f'https://example.test/reject/{n}', eligible=True)
            current.update(fields)
            item = dict(kind='job', source_scope='alert:linkedin', source_id=str(n), last_seen_at='2026-10-29T12:00:00Z', current=current)
            data = dict(sources={}, items={'one':item})
            report = imports.import_observations(self.conn, self.folder, data)
            self.assertEqual(report['new_observed'], 0)
            held = records.create(self.conn, dict(type='job', name='Saved role', url=current['url'], notes='Keep my history', status='pursue'))['record']
            imports.import_observations(self.conn, self.folder, data)
            self.assertEqual(records.get(self.conn, held['id'])['notes'], 'Keep my history')
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations WHERE record_id=?', (held['id'],)).fetchone()[0], 1)

    def test_legacy_discovery_cannot_create_a_new_negative_title(self):
        self.scan('2026-10-01', [dict(company='Example', title='Junior Engineer', url='https://example.test/junior')])
        imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_closed_and_excluded_new_jobs_are_counted_and_can_qualify_later(self):
        item = dict(kind='job', source_scope='alert:linkedin', source_id='reconsider',
                    last_seen_at='2026-10-29T12:00:00Z', source_lifecycle='closed',
                    current=dict(company='Example', title='Engineering Manager', eligible=True, url='https://example.test/reconsider'))
        data = dict(sources={}, items={'one': item})
        path = self.folder / 'observations.json'
        path.write_text(json.dumps(data))
        self.assertEqual(imports.cmd_import_scans(self.conn, self.folder)['intake_rejected'], {'source-closed': 1})
        item['source_lifecycle'] = 'active'
        item['current']['company'] = 'Excludo'
        path.write_text(json.dumps(data))
        self.assertEqual(imports.cmd_import_scans(self.conn, self.folder)['intake_rejected'], {'excluded': 1})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        item['current']['company'] = 'Example'
        item['last_seen_at'] = db.now()  # A fresh qualifying sighting, not an old stale advert.
        path.write_text(json.dumps(data))
        self.assertEqual(imports.cmd_import_scans(self.conn, self.folder)['new_observed'], 1)
        self.assertEqual(imports.cmd_import_scans(self.conn, self.folder)['new_observed'], 0)

    def test_a_save_after_admission_preparation_keeps_the_record_and_history(self):
        item = dict(kind='job', source_scope='alert:linkedin', source_id='race', last_seen_at='2026-10-29T12:00:00Z',
                    current=dict(title='Junior Engineer', eligible=True, url='https://example.test/race'))
        (self.folder / 'observations.json').write_text(json.dumps(dict(items={'one': item}, sources={})))
        other = db.connect(self.folder / 'test.db')
        self.addCleanup(other.close)
        real, held = imports.intake_decisions, []
        def decide(*args, **kwargs):
            result = real(*args, **kwargs)
            if not held:
                held.append(records.save_job(other, dict(name='My deliberate exception', notes='My personal notes',
                                                        url=item['current']['url']))['record'])
            return result
        with patch.object(imports, 'intake_decisions', decide):
            report = imports.cmd_import_scans(self.conn, self.folder)
        current = records.get(self.conn, held[0]['id'])
        self.assertEqual((current['name'], current['notes'], current['status']),
                         ('My deliberate exception', 'My personal notes', 'pursue'))
        self.assertEqual(report['new_observed'], 0)
        self.assertEqual(report['intake_rejected'], {})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 1)

    def test_legacy_hn_is_checked_before_the_write_lock(self):
        (self.folder / 'hn').mkdir()
        (self.folder / 'hn/2026-10-29.tsv').write_text(
            'comment_id\tthread_id\tdate_seen\tcompany\ttitle\turl\n'
            '1\t100\t2026-10-29T12:00:00Z\tExample\tJunior Engineer\thttps://example.test/hn\n')
        real, inside = imports.intake_decisions, []
        def decide(*args, **kwargs):
            inside.append(self.conn.in_transaction)
            return real(*args, **kwargs)
        with patch.object(imports, 'intake_decisions', decide):
            report = imports.cmd_import_scans(self.conn, self.folder)
        self.assertTrue(inside)
        self.assertFalse(any(inside))
        self.assertEqual(report['new_observed'], 0)
        self.assertEqual(sum(report['intake_rejected'].values()), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_repeated_organisation_names_resolve_once_within_each_import(self):
        self.scan('2026-10-01', [dict(company='Example &amp; Co' if n % 2 else 'Example & Co',
                                   title=f'Engineering Manager {n}', url=f'https://example.test/{n}') for n in range(10)])
        import record_identity
        with patch.object(record_identity, 'match_organisation', wraps=record_identity.match_organisation) as match:
            imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(match.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='company'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM links WHERE relation='hiring_for'").fetchone()[0], 10)


class CommercialSignalTests(DatabaseCase):
    def test_scanner_fixture_imports_supplier_evidence_without_vacancies_or_budgets(self):
        import scan_awards
        cfg = self.folder / 'portals.yml'
        cfg.write_text('''commercial_awards:
  filter: {cpv_prefixes: ['722'], keywords: [AI assurance], negative: [construction]}
  sources:
    - {source: contracts_finder, enabled: true}
    - {source: find_a_tender, enabled: true}
''')
        scan_awards.run(cfg, self.folder, '2026-10-04', Path(__file__).parent / 'fixtures' / 'awards')
        report = imports.cmd_import_scans(self.conn, self.folder / 'pending')
        self.assertEqual(report['commercial_signals'], 4)
        rows = self.conn.execute('SELECT type,name,pay,engagement FROM records').fetchall()
        self.assertEqual({r['name'] for r in rows}, {'Example Software Partner', 'Example Evaluation Company',
                                                  'Example Integration Company', 'Example Small Partner'})
        self.assertTrue(all((r['type'], r['pay'], r['engagement']) == ('company', '', '') for r in rows))
        envelopes = [json.loads(r[0]) for r in self.conn.execute('SELECT payload_json FROM observations')]
        self.assertEqual(len(envelopes), 4)
        self.assertTrue(all(e['kind'] == 'commercial_signal' for e in envelopes))
        self.assertTrue(any(e['current']['supplier_count'] == 2 for e in envelopes))

    def award(self, **changes):
        current = dict(signal_type='contract_award', eligible=True, company='Example Delivery', supplier_id='GB-COH-001',
                       buyer='Public Buyer', title='Software integration', summary='Operate an existing service',
                       award_date='2026-09-10', contract_start='2026-10-01', contract_end='2027-10-01',
                       amount='250000', currency='GBP', url='https://buyer.test/award',
                       source_url='https://api.test/release', published='2026-09-11', headline='Award to Example Delivery')
        current.update(changes)
        return dict(kind='commercial_signal', source_scope='award:contracts_finder',
                    source_id='ocid|award1|supplier1', last_seen_at='2026-10-04T01:00:00Z', current=current)

    def ingest(self, item):
        (self.folder / 'observations.json').write_text(json.dumps(dict(sources={}, items={'award': item})))
        return imports.cmd_import_scans(self.conn, self.folder)

    def test_award_repeat_creates_only_the_supplier_company_and_preserves_raw_evidence(self):
        item = self.award()
        report = self.ingest(item)
        again = self.ingest(item)
        self.assertEqual((report['commercial_signals'], report['commercial_organisations_created']), (1, 1))
        self.assertEqual((again['commercial_signals'], again['commercial_organisations_created']), (1, 0))
        rows = [dict(r) for r in self.conn.execute('SELECT * FROM records')]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['type'], rows[0]['name'], rows[0]['summary'], rows[0]['engagement']),
                         ('company', 'Example Delivery', '', ''))
        evidence = self.conn.execute('SELECT * FROM observations').fetchall()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['observed_at'], item['last_seen_at'])
        self.assertEqual(json.loads(evidence[0]['payload_json']), item)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 1)

    def test_a_changed_or_no_longer_eligible_award_updates_evidence_without_a_new_company(self):
        self.ingest(self.award())
        changed = self.award(amount='260000', eligible=False, freshness='ended')
        changed['last_observation_at'] = '2026-10-05T01:00:00Z'
        report = self.ingest(changed)
        self.assertEqual((report['commercial_signals'], report['commercial_organisations_created']), (1, 0))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
        evidence = self.conn.execute('SELECT * FROM observations ORDER BY id').fetchall()
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[-1]['observed_at'], changed['last_observation_at'])
        self.assertFalse(json.loads(evidence[-1]['payload_json'])['current']['eligible'])

    def test_an_unchanged_award_resighting_advances_one_row_and_old_replay_cannot_rewind_it(self):
        item = self.award()
        self.ingest(item)
        again = {**item, 'last_seen_at': '2026-10-05T01:00:00Z'}
        self.ingest(again)
        self.ingest(item)
        evidence = self.conn.execute('SELECT * FROM observations').fetchall()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['observed_at'], again['last_seen_at'])
        self.assertEqual(json.loads(evidence[0]['payload_json'])['last_seen_at'], again['last_seen_at'])

    def test_an_ineligible_new_award_creates_nothing(self):
        report = self.ingest(self.award(eligible=False))
        self.assertEqual(report['commercial_signals'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_a_presence_only_award_does_not_imply_a_contract_is_still_active_forever(self):
        item = {**self.award(), 'source_lifecycle': 'active'}
        self.assertEqual(imports.record_lifecycle([item], imports.observation_time('2026-10-05')), 'active')
        self.assertEqual(imports.record_lifecycle([item], imports.observation_time('2026-10-20')), 'unknown')
        item['source_lifecycle'] = 'closed'
        self.assertEqual(imports.record_lifecycle([item], imports.observation_time('2026-10-20')), 'closed')

    def test_an_archived_supplier_is_not_recreated_or_given_a_new_observation(self):
        self.ingest(self.award())
        company = dict(self.conn.execute('SELECT * FROM records').fetchone())
        records.archive(self.conn, company['id'], dict(revision=company['revision'], reason='Not relevant'), actor='agent')
        report = self.ingest(self.award(amount='999999'))
        self.assertEqual(report['commercial_signals'], 0)
        self.assertEqual(report['archived_organisations'], ['Example Delivery'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_a_renamed_supplier_with_the_same_archived_native_identity_stays_archived(self):
        self.ingest(self.award())
        company = dict(self.conn.execute('SELECT * FROM records').fetchone())
        records.archive(self.conn, company['id'], dict(revision=company['revision'], reason='Not relevant'), actor='agent')
        report = self.ingest(self.award(company='Renamed Delivery'))
        self.assertEqual(report['commercial_signals'], 0)
        self.assertEqual(report['archived_organisations'], ['Renamed Delivery'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT record_id FROM source_records').fetchone()[0], company['id'])

    def test_a_merged_supplier_identity_keeps_refreshing_the_survivor_after_a_source_rename(self):
        self.ingest(self.award())
        original = dict(self.conn.execute('SELECT * FROM records').fetchone())
        survivor = records.create(self.conn, dict(type='company', name='Canonical Delivery'))['record']
        records.merge(self.conn, original['id'], survivor['id'], dict(revision=original['revision']), actor='agent')
        report = self.ingest(self.award(company='Another Source Spelling'))
        self.assertEqual((report['commercial_signals'], report['commercial_organisations_created']), (1, 0))
        self.assertEqual(self.conn.execute('SELECT record_id FROM source_records').fetchone()[0], survivor['id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records WHERE archived_at IS NULL').fetchone()[0], 1)
        latest = self.conn.execute('SELECT payload_json FROM observations WHERE record_id=? ORDER BY id DESC', (survivor['id'],)).fetchone()
        self.assertEqual(json.loads(latest[0])['current']['company'], 'Another Source Spelling')


class WriteLockTests(DatabaseCase):
    """Advert wording is read before the write lock: every 15-minute email run replays all adverts."""
    def test_advert_terms_are_read_outside_the_import_transaction(self):
        import job_rules
        item = dict(kind='job', source_scope='board:example', source_id='42', last_seen_at=db.now(),
                    source_lifecycle='active',
                    current=dict(company='Example', title='Head of QA', eligible=True, url='https://example.test/jobs/42',
                                 text='A six month contract role at £600 per day, outside IR35.'))
        (self.folder / 'observations.json').write_text(json.dumps(dict(items={'one': item}, sources={})))
        real, inside = job_rules.observed_terms, []
        def observed(job, engagement_filter):
            inside.append(self.conn.in_transaction)
            return real(job, engagement_filter)
        with patch.object(job_rules, 'observed_terms', observed):
            imports.cmd_import_scans(self.conn, self.folder)
        self.assertEqual(inside, [False])
        job = self.conn.execute("SELECT engagement, pay, ir35 FROM records WHERE type='job'").fetchone()
        self.assertEqual(tuple(job), ('contract', '£600/day', 'outside'))


if __name__ == '__main__':
    unittest.main()
