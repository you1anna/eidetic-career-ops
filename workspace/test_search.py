"""Synthetic search controls: identity, manual decisions and exclusions at the write boundary."""
import datetime as dt
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import records
import relevance


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.connect(Path(self.temp.name) / 'search.db')
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)

    def create(self, typ='job', **values):
        return records.create(self.conn, dict(type=typ, name='Engineering Manager', **values))['record']

    def test_override_is_nullable_revisioned_jobs_only_and_audited(self):
        job = self.create()
        self.assertIsNone(job['cv_override'])
        edited = records.edit(self.conn, job['id'], dict(revision=job['revision'], cv_override='HeadQE'))['record']
        self.assertEqual(edited['cv_override'], 'QE')
        with self.assertRaises(records.Conflict):
            records.edit(self.conn, job['id'], dict(revision=job['revision'], cv_override='Manager'))
        reset = records.edit(self.conn, job['id'], dict(revision=edited['revision'], cv_override=None))['record']
        self.assertIsNone(reset['cv_override'])
        self.assertEqual([(r['from_value'], r['to_value']) for r in self.conn.execute(
            "SELECT * FROM state_events WHERE field='cv_override' ORDER BY id")], [(None, 'QE'), ('QE', None)])
        for value in ('Unknown', '', [], 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.edit(self.conn, job['id'], dict(revision=reset['revision'], cv_override=value))
        person = self.create('person')
        with self.assertRaises(ValueError):
            records.edit(self.conn, person['id'], dict(revision=person['revision'], cv_override='Manager'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE records SET cv_override='QE' WHERE id=?", (person['id'],))

    def test_registry_and_remit_routes_include_agencies_without_score_changes(self):
        agency = records.create(self.conn, dict(type='agency', name='Example Agency'))['record']
        cases = [('Engineering Manager', 'Manager'), ('Director of Engineering', 'Director'),
                 ('QA Engineering Manager', 'QELead'), ('Head of Quality Engineering', 'QE'),
                 ('Senior QA Lead', 'QELead'), ('Forward Deployed Engineer', 'FDE'), ('Founding Engineer', 'Founding')]
        jobs = []
        for title, cv in cases:
            jobs.append((records.create(self.conn, dict(type='job', name=title, organisation=agency['name']))['record'], cv))
        view = records.state(self.conn)
        self.assertEqual(set(view['cvs']), {'Director', 'Manager', 'QE', 'QELead', 'FDE', 'Founding'})
        for job, cv in jobs:
            r = next(r for r in view['records'] if r['id'] == job['id'])
            route = r['relevance']['route']
            self.assertEqual(route['cv'], cv)
            self.assertEqual(route['selection'], 'suggested')
            self.assertTrue(route['via_agency'])
            self.assertTrue(all(k in route for k in ('title', 'url', 'readiness')))
        job = records.create(self.conn, dict(type='job', name='Unmatched specialist'))['record']
        before = next(r for r in records.state(self.conn)['records'] if r['id'] == job['id'])['relevance']
        records.edit(self.conn, job['id'], dict(revision=job['revision'], cv_override='Founding'))
        after = next(r for r in records.state(self.conn)['records'] if r['id'] == job['id'])['relevance']
        self.assertEqual(after['score'], before['score'])
        self.assertEqual(after['route']['selection'], 'manual')
        self.assertEqual(after['route']['cv'], 'Founding')

    def test_routing_aliases_and_hands_on_ai_remits_do_not_change_title_scores(self):
        cases = [('AI Evaluation Engineer', 'QELead'), ('AI Reliability Engineer', 'QELead'),
                 ('Head of AI Evaluation', 'QE'), ('Director of Quality Engineering', 'QE'),
                 ('Director of Software Engineering', 'Director'), ('Vice President of Engineering', 'Director'),
                 ('Chief Technology Officer', 'Director'), ('Systems Engineer', 'FDE'),
                 ('Systems Integration Engineer', 'FDE'), ('Implementation Engineer', 'FDE')]
        cfg = relevance.load_config()
        for title, cv in cases:
            record = dict(id='J', type='job', name=title, summary='Software product engineering and cloud delivery')
            view = dict(records=[record])
            current = relevance.score(view, cfg, dt.date(2026, 10, 6))['J']
            without_routing_rules = relevance.score(view, dict(cfg, cv_routes=[]), dt.date(2026, 10, 6))['J']
            with self.subTest(title=title):
                self.assertEqual(current['route']['cv'], cv)
                self.assertEqual(current['score'], without_routing_rules['score'])

    def test_saved_cv_choice_keeps_current_suggestion_in_full_and_summary_views(self):
        job = self.create(status='pursue')
        manual = records.edit(self.conn, job['id'], dict(revision=job['revision'], cv_override='Founding'))['record']
        changed = records.edit(self.conn, job['id'], dict(revision=manual['revision'], name='Head of Engineering'))['record']
        for compact in (False, True):
            with self.subTest(compact=compact):
                current = next(r for r in records.state(self.conn, compact)['records'] if r['id'] == job['id'])
                self.assertEqual(current['status'], 'pursue')
                self.assertEqual(current['cv_override'], 'Founding')
                self.assertEqual(current['relevance']['route']['cv'], 'Founding')
                self.assertEqual(current['relevance']['route']['suggested_cv'], 'Director')
                self.assertEqual(current['relevance']['route']['suggestion_basis'],
                                 {'kind': 'title_rule', 'term': 'Head of Engineering'})
        with self.assertRaises(records.Conflict):
            records.edit(self.conn, job['id'], dict(revision=manual['revision'], cv_override=None))
        reset = records.edit(self.conn, job['id'], dict(revision=changed['revision'], cv_override=None))['record']
        self.assertEqual(reset['status'], 'pursue')
        current = next(r for r in records.state(self.conn, True)['records'] if r['id'] == job['id'])
        self.assertEqual(current['relevance']['route']['cv'], 'Director')
        self.assertEqual(current['relevance']['route']['selection'], 'suggested')

    def test_outside_domain_manual_cv_does_not_promise_a_suggestion_on_reset(self):
        job = records.create(self.conn, dict(type='job', name='Head of Quality Engineering',
                             summary='Lead quality engineering at a manufacturing plant, responsible for electrical engineering.',
                             status='pursue', cv_override='Manager'))['record']
        current = next(r for r in records.state(self.conn, True)['records'] if r['id'] == job['id'])
        route = current['relevance']['route']
        self.assertEqual(route['cv'], 'Manager')
        self.assertEqual(route['selection'], 'manual')
        self.assertIsNone(route['suggested_cv'])
        self.assertIsNone(route['suggestion_basis'])
        reset = records.edit(self.conn, job['id'], dict(revision=job['revision'], cv_override=None))['record']
        self.assertEqual(reset['status'], 'pursue')
        current = next(r for r in records.state(self.conn, True)['records'] if r['id'] == job['id'])
        self.assertNotIn('route', current['relevance'])

    def test_merge_preserves_override_and_refuses_conflicting_manual_decisions(self):
        source, target = self.create(cv_override='QELead'), self.create()
        saved = records.merge(self.conn, source['id'], target['id'], dict(revision=source['revision']))['record']
        self.assertEqual(saved['cv_override'], 'QELead')
        self.assertEqual(self.conn.execute("SELECT actor FROM record_edits WHERE record_id=? AND field='cv_override'", (target['id'],)).fetchone()[0], 'owner')
        other = self.create(cv_override='Manager')
        with self.assertRaises(records.Conflict):
            records.merge(self.conn, other['id'], target['id'], dict(revision=other['revision']))
        reset = records.edit(self.conn, target['id'], dict(revision=saved['revision'], cv_override=None))['record']
        with self.assertRaises(records.Conflict):
            records.merge(self.conn, other['id'], target['id'], dict(revision=other['revision']))
        self.assertIsNone(records.get(self.conn, target['id'])['cv_override'])

    def test_save_canonical_url_is_atomic_linked_and_existing_is_unchanged(self):
        payload = dict(name='Engineering Manager', organisation='Example &amp; Co',
                       url='https://uk.linkedin.com/jobs/view/manager-at-example-12345?tracking=x', notes='My note')
        saved = records.save_job(self.conn, payload)
        self.assertTrue(saved['created'])
        job = saved['record']
        self.assertEqual((job['url'], job['status']), ('https://www.linkedin.com/jobs/view/12345/', 'pursue'))
        self.assertEqual(job['organisation'], 'Example & Co')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM links WHERE from_id=?', (job['id'],)).fetchone()[0], 1)
        duplicate = records.save_job(self.conn, dict(payload, url='https://www.linkedin.com/comm/jobs/view/12345/', notes='Overwrite'))
        self.assertFalse(duplicate['created'])
        self.assertEqual(duplicate['record'], job)
        archived_org = records.create(self.conn, dict(type='company', name='Archived Company'))['record']
        records.archive(self.conn, archived_org['id'], dict(revision=archived_org['revision'], reason='History'))
        with self.assertRaises(records.Conflict):
            records.save_job(self.conn, dict(payload, organisation=archived_org['name'], url='https://example.test/new'))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)

    def test_save_unknown_employer_keeps_it_blank_and_does_not_invent_a_link(self):
        for suffix, extra in [('omitted', {}), ('empty', dict(organisation='  '))]:
            with self.subTest(employer=suffix):
                payload = dict(name='Engineering Manager', url='https://example.test/' + suffix, **extra)
                saved = records.save_job(self.conn, payload)
                job = saved['record']
                self.assertTrue(saved['created'])
                self.assertEqual((job['organisation'], job['status']), ('', 'pursue'))
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM links WHERE from_id=?', (job['id'],)).fetchone()[0], 0)
                self.assertIsNone(self.conn.execute("SELECT 1 FROM record_edits WHERE record_id=? AND field='organisation'", (job['id'],)).fetchone())
                again = records.save_job(self.conn, dict(payload, organisation='Later claim', notes='Do not overwrite'))
                self.assertFalse(again['created'])
                self.assertEqual(again['record'], job)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type IN ('company','agency')").fetchone()[0], 0)

    def test_verified_excluded_board_url_rejects_unknown_employer_without_substring_matching(self):
        for url in ('https://job-boards.greenhouse.io/excludo/jobs/123456',
                    'https://boards.greenhouse.io/excludo/jobs/123456?source=mail'):
            with self.subTest(url=url), self.assertRaises(records.Conflict):
                records.save_job(self.conn, dict(name='Engineering Manager', url=url))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        for url in ('https://job-boards.greenhouse.io/excludo-labs/jobs/123456',
                    'https://job-boards.greenhouse.io.example.test/excludo/jobs/123456',
                    'https://example.test/excludo/jobs/123456',
                    'https://example.test/?url=https://job-boards.greenhouse.io/excludo/jobs/123456'):
            with self.subTest(url=url):
                job = records.save_job(self.conn, dict(name='Engineering Manager', url=url))['record']
                self.assertIsNone(job['exclusion'])
        for field in ('url', 'detail_url', 'apply_url'):
            job = self.create(status='candidate', **{field: 'https://job-boards.greenhouse.io/excludo/jobs/789'})
            self.assertEqual(job['exclusion']['company'], 'Excludo')
        person = records.create(self.conn, dict(type='person', name='Alex Sample', relationship='warm',
                                               url='https://job-boards.greenhouse.io/excludo/jobs/789'))['record']
        self.assertIsNone(person['exclusion'])
        self.assertEqual(person['relationship'], 'warm')


    def test_save_follows_merge_but_rejects_plain_archive_and_ambiguous_url(self):
        source = self.create(url='https://www.linkedin.com/jobs/view/111?tracking=x')
        survivor = self.create(url='https://example.test/survivor', status='skip')
        records.merge(self.conn, source['id'], survivor['id'], dict(revision=source['revision']))
        payload = dict(name='Ignored', organisation='Ignored', url='https://www.linkedin.com/jobs/view/111/')
        saved = records.save_job(self.conn, payload)
        self.assertEqual((saved['created'], saved['record']['id'], saved['record']['status']), (False, survivor['id'], 'skip'))
        records.archive(self.conn, survivor['id'], dict(revision=saved['record']['revision'], reason='History'))
        with self.assertRaises(records.Conflict):
            records.save_job(self.conn, payload)
        self.create(url='https://example.test/ambiguous')
        self.create(url='https://example.test/ambiguous')
        with self.assertRaises(records.Conflict):
            records.save_job(self.conn, dict(payload, url='https://example.test/ambiguous'))

    def test_concurrent_save_calls_create_only_one_job_and_one_organisation(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier = threading.Barrier(2)
        path = Path(self.temp.name) / 'search.db'
        def save():
            conn = db.connect(path)
            try:
                barrier.wait()
                return records.save_job(conn, dict(name='Engineering Manager', organisation='Example', url='https://example.test/concurrent'))
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save(), range(2)))
        self.assertEqual(sorted(r['created'] for r in results), [False, True])
        self.assertEqual(len({r['record']['id'] for r in results}), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)

    def test_exclusion_exact_company_links_board_and_independent_of_scoring(self):
        company = records.create(self.conn, dict(type='company', name=' EXCLUDO '))['record']
        linked = self.create(organisation='Example Agency', status='candidate')
        records.link(self.conn, dict(from_id=linked['id'], to_id=company['id'], relation='hiring_for', revision=linked['revision']))
        scoped = self.create(organisation='Agency')
        self.conn.execute('INSERT INTO source_records VALUES(?,?,?)', ('board:greenhouse:excludo', 'native-id', scoped['id']))
        mention = records.create(self.conn, dict(type='job', name='Engineer', organisation='Excludo Labs', summary='Previously worked with Excludo'))['record']
        person = self.create('person', relationship='warm')
        records.link(self.conn, dict(from_id=person['id'], to_id=company['id'], relation='works_at', revision=person['revision']))
        with patch.object(relevance, 'score', side_effect=RuntimeError('synthetic scoring failure')):
            view = records.state(self.conn)
        rows = {r['id']: r for r in view['records']}
        for rid in (company['id'], linked['id'], scoped['id']):
            self.assertEqual(rows[rid]['exclusion'], dict(company='Excludo', reason='The owner explicitly excluded Excludo on 6 October 2026.'))
        self.assertIsNone(rows[mention['id']]['exclusion'])
        self.assertIsNone(rows[person['id']]['exclusion'])
        self.assertEqual(rows[person['id']]['relationship'], 'warm')
        self.assertNotIn('pursue', rows[linked['id']]['transitions'].get('status', []))
        with self.assertRaises(records.Conflict):
            records.transition(self.conn, linked['id'], dict(revision=rows[linked['id']]['revision'], field='status', to='pursue'))
        with self.assertRaises(records.Conflict):
            records.save_job(self.conn, dict(url='https://example.test/excluded', name='Director', organisation='excludo'))
        self.assertFalse(any(r['url'] == 'https://example.test/excluded' for r in records.state(self.conn)['records']))

    def test_exclusion_never_reuses_company_bound_personal_draft_as_generic(self):
        co = records.create(self.conn, dict(type='company', name='Excludo'))['record']
        person = records.create(self.conn, dict(type='person', name='Alex Sample', role='CTO', relationship='warm'))['record']
        person = records.link(self.conn, dict(from_id=person['id'], to_id=co['id'], relation='works_at', revision=person['revision']))['record']
        records.draft(self.conn, dict(record_id=person['id'], text='A proposal for Excludo', revision=person['revision']))
        view = records.state(self.conn)
        actions = view['intelligence']['actions'] + view['intelligence']['research']
        self.assertFalse(any(a.get('company_id') == co['id'] for a in actions))
        generic = next(a for a in actions if a.get('record_id') == person['id'])
        self.assertNotIn('draft_record_id', generic)
        self.assertNotEqual(generic['stage'], 'review')
        self.assertEqual(len(view['drafts']), 1)

    def test_decision_exclusion_requires_record_or_explicit_frozen_company_context(self):
        co = records.create(self.conn, dict(type='company', name='Excludo'))['record']
        person = records.create(self.conn, dict(type='person', name='Alex Sample', relationship='warm'))['record']
        person = records.link(self.conn, dict(from_id=person['id'], to_id=co['id'], relation='works_at', revision=person['revision']))['record']
        records.draft(self.conn, dict(record_id=person['id'], text='A proposal for Excludo', revision=person['revision']))
        bound = records.create_decision(self.conn, dict(subject=person['id'], kind='draft', question='Approve this note?'))['decision']
        general = records.create_decision(self.conn, dict(subject=person['id'], kind='approve', question='Ask for a general referral?'))['decision']
        direct = records.create_decision(self.conn, dict(subject=co['id'], kind='approve', question='Investigate?'))['decision']
        self.assertEqual(bound['exclusion']['company'], 'Excludo')
        self.assertEqual(direct['exclusion']['company'], 'Excludo')
        self.assertIsNone(general['exclusion'])
        self.assertEqual(records.get(self.conn, person['id'])['relationship'], 'warm')
        research = records.create_decision(self.conn, dict(subject=person['id'], kind='join',
            question='Investigate the engineering leadership opportunity at Excludo?',
            context='Research only the Excludo role and its named decision owner.'))['decision']
        self.assertEqual(research['exclusion']['company'], 'Excludo')

    def test_excluded_company_merge_alias_keeps_agency_job_excluded(self):
        original = records.create(self.conn, dict(type='company', name='Excludo'))['record']
        survivor = records.create(self.conn, dict(type='company', name='Employer alias'))['record']
        job = self.create(organisation='Example Agency', status='candidate')
        job = records.link(self.conn, dict(from_id=job['id'], to_id=original['id'], relation='hiring_for', revision=job['revision']))['record']
        self.assertTrue(job['exclusion'])
        records.merge(self.conn, original['id'], survivor['id'], dict(revision=original['revision']))
        current = records.get(self.conn, job['id'])
        self.assertEqual(current['exclusion']['company'], 'Excludo')
        with self.assertRaises(records.Conflict):
            records.transition(self.conn, job['id'], dict(revision=current['revision'], field='status', to='pursue'))

    def test_agency_owned_excluded_client_draft_is_not_reused_for_generic_agency_outreach(self):
        co = records.create(self.conn, dict(type='company', name='Excludo'))['record']
        agency = records.create(self.conn, dict(type='agency', name='Example Agency', status='on_books',
                                                summary='Engineering leadership and quality engineering contract desk'))['record']
        agency = records.link(self.conn, dict(from_id=agency['id'], to_id=co['id'], relation='related', revision=agency['revision']))['record']
        records.draft(self.conn, dict(record_id=agency['id'], title='Excludo introduction', text='Engineering leadership for Excludo', revision=agency['revision']))
        decision = records.create_decision(self.conn, dict(subject=agency['id'], kind='draft', question='Approve Excludo note?'))['decision']
        self.assertEqual(decision['exclusion']['company'], 'Excludo')
        view = records.state(self.conn)
        self.assertIsNone(next(r for r in view['records'] if r['id'] == agency['id'])['exclusion'])
        actions = view['intelligence']['actions'] + view['intelligence']['research']
        self.assertFalse(any(a.get('draft_record_id') == agency['id'] for a in actions))
        self.assertEqual(len(view['drafts']), 1)

    def test_archived_company_context_still_constrains_personal_drafts_without_a_decision(self):
        co = records.create(self.conn, dict(type='company', name='Excludo'))['record']
        person = records.create(self.conn, dict(type='person', name='Alex Sample', relationship='warm'))['record']
        person = records.link(self.conn, dict(from_id=person['id'], to_id=co['id'], relation='works_at', revision=person['revision']))['record']
        records.draft(self.conn, dict(record_id=person['id'], text='A proposal for Excludo', revision=person['revision']))
        records.archive(self.conn, co['id'], dict(revision=co['revision'], reason='Preserved history'))
        view = records.state(self.conn)
        actions = view['intelligence']['actions'] + view['intelligence']['research']
        generic = next(a for a in actions if a.get('record_id') == person['id'])
        self.assertNotIn('draft_record_id', generic)
        self.assertNotEqual(generic['stage'], 'review')
        self.assertEqual(next(r for r in view['records'] if r['id'] == person['id'])['relationship'], 'warm')

    def test_archived_client_job_retains_agency_draft_context(self):
        job = self.create(organisation='Excludo', status='candidate')
        agency = records.create(self.conn, dict(type='agency', name='Example Agency', status='on_books',
                                                summary='Engineering leadership contract desk'))['record']
        agency = records.link(self.conn, dict(from_id=agency['id'], to_id=job['id'], relation='advertised_by', revision=agency['revision']))['record']
        records.draft(self.conn, dict(record_id=agency['id'], text='A proposal for Excludo', revision=agency['revision']))
        records.archive(self.conn, job['id'], dict(revision=job['revision'], reason='Preserved history'))
        view = records.state(self.conn)
        actions = view['intelligence']['actions'] + view['intelligence']['research']
        self.assertFalse(any(a.get('draft_record_id') == agency['id'] for a in actions))


class TemplatePackTests(unittest.TestCase):
    """The owner's outreach pack (config/templates.yml): every gap is declared, every rule reads, and the
    site's choices point at templates that exist."""

    GAP = r'\{\{\s*([a-z0-9_]+)\s*\}\}'

    def setUp(self):
        import search_config
        import states
        self.pack = search_config.templates()
        self.kinds = states.ENUMS['person']['kind']
        self.relationships = states.ENUMS['person']['relationship']
        self.cvs = set(search_config.CV_KEYS)
        self.ids = {t['id'] for t in self.pack['templates']} | {f['id'] for f in self.pack['followups']['items']}

    def texts(self):
        import re
        out = []
        def walk(value):
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, dict):
                [walk(v) for v in value.values()]
            elif isinstance(value, list):
                [walk(v) for v in value]
        walk({k: v for k, v in self.pack.items() if k != 'placeholders'})
        return out, re

    def test_every_gap_is_declared_and_every_fill_is_one_the_page_knows(self):
        texts, re = self.texts()
        used = {name for text in texts for name in re.findall(self.GAP, text)}
        self.assertEqual(used - set(self.pack['placeholders']), set())
        unused = set(self.pack['placeholders']) - used - {'blurb_paragraph'}  # added under email sign-offs
        self.assertEqual(unused, set())
        for name, spec in self.pack['placeholders'].items():
            with self.subTest(name=name):
                self.assertTrue(spec.get('label') and spec.get('help'))
                self.assertIn(spec.get('fill'), (None, 'first_name', 'company', 'shapes', 'proofs', 'blurb', 'desk'))
                self.assertIn(spec.get('suggest'), (None, 'linkedin_company', 'months', 'other_desks', 'problems'))
        # The rate is the owner's to set: never filled, never offered.
        self.assertNotIn('fill', self.pack['placeholders']['day_rate'])
        self.assertNotIn('examples', self.pack['placeholders']['day_rate'])

    def test_suggestions_point_at_templates_for_real_kinds(self):
        rules = self.pack['suggest']
        self.assertLessEqual(set(rules['by_kind']), set(self.kinds))
        self.assertLessEqual(set(rules['when_catch_up']), set(rules['by_kind']))
        self.assertLessEqual(set(rules['by_kind'].values()) | set(rules['when_catch_up'].values()) | {rules['otherwise']}, self.ids)
        self.assertLessEqual(set(rules['known_relationships']), set(self.relationships))
        # A catch-up ask never becomes the referral request.
        self.assertEqual(rules['when_catch_up']['former_colleague'], 'warm')

    def test_shapes_desks_and_proofs_refer_to_real_cvs_and_proof_points(self):
        proofs = {p['id'] for p in self.pack['proofs']['items']}
        self.assertEqual([s['id'] for s in self.pack['shapes']], ['lead', 'build', 'fix'])
        for group in (self.pack['shapes'], self.pack['desks']):
            covered = [cv for item in group for cv in item['cvs']]
            self.assertEqual(sorted(covered), sorted(self.cvs))  # every CV leads somewhere, once
        for desk in self.pack['desks']:
            self.assertEqual(len(desk['proofs']), 2)
            self.assertLessEqual(set(desk['proofs']), proofs)

    def test_rules_compile_and_templates_waive_only_real_checks(self):
        import re
        ids = {c['id'] for c in self.pack['checks']}
        for rule in self.pack['dont_say'] + self.pack['checks']:
            with self.subTest(rule=rule['match']):
                re.compile(rule['match'])
                if rule.get('unless'):
                    re.compile(rule['unless'])
                # Python and the browser must read a pattern the same way: no look-behind or named groups.
                self.assertNotRegex(rule['match'], r'\(\?<|\(\?P')
        for t in self.pack['templates']:
            with self.subTest(template=t['id']):
                self.assertTrue(t['title'] and t['for'] and t['body'])
                self.assertLessEqual(set(t.get('waive') or []), ids)
        self.assertEqual({f['step'] for f in self.pack['followups']['items']}, {'follow_up', 'nothing_now', 'introduction', 'update'})

    def test_no_rate_reaches_the_pack(self):
        import re
        texts, _ = self.texts()
        for text in texts:
            self.assertNotRegex(text, re.compile(r'£\s?\d[\d,]*\s*(?:a|per|/)\s*day', re.I))

    def test_state_carries_the_pack_and_survives_a_broken_file(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        conn = db.connect(Path(temp.name) / 'pack.db')
        self.addCleanup(conn.close)
        db.migrate(conn)
        self.assertNotIn('templates',records.state(conn,compact=True))
        self.assertEqual(records.state(conn)['templates']['templates'][0]['id'],'warm')
        with patch('search_config.templates', side_effect=ValueError('bad YAML')):
            view = records.state(conn)
        self.assertIsNone(view['templates'])
        self.assertIn('bad YAML', view['templates_error'])
        self.assertIn('intelligence', view)
