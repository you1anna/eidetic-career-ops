"""New data folds into existing records: one URL is one advert, names match whatever their encoding,
and date cells hold dates."""
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wo  # noqa: E402
import pending_store  # noqa: E402
import record_imports  # noqa: E402

COLS = ["date_seen", "company", "title", "location", "url", "posted", "engagement", "rate"]


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = wo.wodb.connect(self.dir / "t.db")
        wo.wodb.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def scan(self, rows):
        pending = self.dir / "pending"
        pending.mkdir(exist_ok=True)
        lines = ["\t".join(COLS)] + ["\t".join(r.get(c, "") for c in COLS) for r in rows]
        (pending / "2026-09-24.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return wo.cmd_import_scans(self.conn, pending)

    def test_a_url_already_recorded_is_not_recorded_again(self):
        kept = wo.store.create(self.conn, dict(type="job", name="Interim EM", url="https://jobs.example/1"))["record"]
        report = self.scan([dict(company="Acme", title="Interim EM", url="https://jobs.example/1", date_seen="2026-09-24")])
        self.assertEqual(report["url_already_recorded"], 1)
        jobs = [r[0] for r in self.conn.execute("SELECT id FROM records WHERE type='job'")]
        self.assertEqual(jobs, [kept["id"]])

    def test_adverts_link_to_the_organisation_whatever_its_encoding_and_prefer_the_agency(self):
        agency = wo.store.create(self.conn, dict(type="agency", name="Newton's Tree"))["record"]
        wo.store.create(self.conn, dict(type="company", name="Newton's Tree"))
        self.scan([dict(company="Newton&#x27;s Tree", title="Forward Deployed Engineer", url="https://jobs.example/2",
                        date_seen="2026-09-24")])
        job = self.conn.execute("SELECT id, organisation FROM records WHERE url='https://jobs.example/2'").fetchone()
        self.assertEqual(job["organisation"], "Newton's Tree")
        links = [tuple(r) for r in self.conn.execute("SELECT to_id, relation FROM links WHERE from_id=?", (job["id"],))]
        self.assertEqual(links, [(agency["id"], "hiring_for")])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM records WHERE type IN ('company','agency')").fetchone()[0], 2)

    def test_a_name_merged_into_another_organisation_links_to_where_it_went(self):
        agency = wo.store.create(self.conn, dict(type="agency", name="Sanderson Recruitment"))["record"]
        self.scan([dict(company="Sanderson", title="Test Lead", url="https://jobs.example/5", date_seen="2026-09-24")])
        copy = self.conn.execute("SELECT id, revision FROM records WHERE type='company'").fetchone()
        wo.store.merge(self.conn, copy["id"], agency["id"], dict(revision=copy["revision"]), actor="agent")
        self.scan([dict(company="Sanderson", title="QA Lead", url="https://jobs.example/6", date_seen="2026-09-25")])
        job = self.conn.execute("SELECT id FROM records WHERE url='https://jobs.example/6'").fetchone()
        links = [tuple(r) for r in self.conn.execute("SELECT to_id, relation FROM links WHERE from_id=?", (job["id"],))]
        self.assertEqual(links, [(agency["id"], "hiring_for")])

    def test_a_plainly_archived_organisation_is_not_followed(self):
        old = wo.store.create(self.conn, dict(type="company", name="Gone Ltd"))["record"]
        wo.store.archive(self.conn, old["id"], dict(revision=old["revision"], reason="Closed"), actor="agent")
        self.assertIsNone(wo.store.match_organisation(self.conn, "Gone Ltd"))

    def test_an_archived_imported_organisation_is_not_reported_created_or_linked(self):
        rid, _ = wo.ensure_organisation(self.conn, 'Gone Ltd', 'review', '2026-09-24')
        old = wo.store.get(self.conn, rid)
        wo.store.archive(self.conn, rid, dict(revision=old['revision'], reason='No longer relevant'))
        self.assertEqual(wo.ensure_organisation(self.conn, 'Gone Ltd', 'review', '2026-09-29'), (None, False))
        self.scan([dict(company='Gone Ltd', title='Head of QA', url='https://jobs.example/gone', date_seen='2026-09-29')])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM links WHERE to_id=?', (rid,)).fetchone()[0], 0)
        self.assertTrue(wo.store.get(self.conn, rid)['archived_at'])

    def test_an_archived_manual_organisation_does_not_reappear_on_import(self):
        old = wo.store.create(self.conn, dict(type='company', name='Gone Ltd'))['record']
        wo.store.archive(self.conn, old['id'], dict(revision=old['revision'], reason='No longer relevant'))
        self.assertEqual(wo.ensure_organisation(self.conn, 'Gone Ltd', 'review', '2026-09-29'), (None, False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='company'").fetchone()[0], 1)

    def test_an_unknown_organisation_is_created_once_with_a_clean_name(self):
        self.scan([dict(company="Acme &amp; Co", title="Head of QA", url="https://jobs.example/3", date_seen="2026-09-24"),
                   dict(company="Acme & Co", title="QA Lead", url="https://jobs.example/4", date_seen="2026-09-24")])
        orgs = [tuple(r) for r in self.conn.execute("SELECT name FROM records WHERE type='company'")]
        self.assertEqual(orgs, [("Acme & Co",)])

    def test_research_fills_a_field_an_agent_emptied_but_never_overrides_owner(self):
        import json
        a = wo.store.create(self.conn, dict(type="company", name="A", summary="Greenhouse slug 404"), actor="agent")["record"]
        wo.store.edit(self.conn, a["id"], dict(revision=a["revision"], summary=""), actor="agent")
        b = wo.store.create(self.conn, dict(type="company", name="B", summary="The owner's words"))["record"]
        batch = self.dir / "batch.json"
        batch.write_text(json.dumps([dict(id=x, reviewed_at="2026-09-24", summary="Sourced description", source_urls=["https://example.com"])
                                     for x in (a["id"], b["id"])]))
        report = wo.import_research(self.conn, batch)
        self.assertEqual(wo.store.get(self.conn, a["id"])["summary"], "Sourced description")
        self.assertEqual(wo.store.get(self.conn, b["id"])["summary"], "The owner's words")
        self.assertEqual([c["record_id"] for c in report["conflicts"]], [b["id"]])

    def test_date_cells_hold_dates(self):
        self.scan([dict(company="Acme", title="Head of QA", url="https://jobs.example/5", posted="Recently", date_seen="2026-09-24")])
        row = self.conn.execute("SELECT date, notes FROM records WHERE url='https://jobs.example/5'").fetchone()
        self.assertEqual(row["date"], "")
        self.assertIn("Date note: Recently", row["notes"])

    def test_tsv_candidates_use_cross_source_observations_before_creating_records(self):
        pending = self.dir / "pending"
        pending.mkdir()
        sources, items = {}, {}
        for provider in ('adzuna', 'reed'):
            scope = 'feed:' + provider + ':test'
            sources[scope] = dict(provider=provider, observed_at='2026-09-24T12:00:00Z', state='ok', complete=True, item_ids=['1'])
            items[provider] = dict(source_scope=scope, source_id='1', kind='job',
                                   last_seen_at=wo.wodb.now(), source_lifecycle='active',
                                   current=dict(title='Head of QA', company='Acme', url=f'https://{provider}/1',
                                                text='Lead software engineering teams and improve release readiness.'))
        (pending / 'observations.json').write_text(json.dumps(dict(generated_at='2026-09-24T12:00:00Z', sources=sources, items=items)))
        rows = [dict(company='Acme', title='Head of QA', url=f'https://{p}/1', date_seen='2026-09-24') for p in ('adzuna','reed')]
        report = self.scan(rows)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 2)
        self.assertEqual(report['attached_sightings'], 1)
        self.scan(rows)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)


class CompactionTests(unittest.TestCase):
    """The daily ingest drops a closed advert's text from observations.json only once the database
    holds that exact observation, or never imported it; later imports leave the record alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending = Path(self.tmp.name) / "pending"
        self.pending.mkdir()
        self.conn = wo.wodb.connect(Path(self.tmp.name) / "t.db")
        wo.wodb.migrate(self.conn)
        scope = "board:greenhouse:acme"
        closed = dict(source_lifecycle="closed", closed_at="2026-09-03T06:00:00Z",
                      closure_reason="absent-from-two-complete-source-observations-24h-apart")
        def advert(sid, eligible):
            return dict(source_scope=scope, source_id=sid, provider="greenhouse", kind="job",
                        first_seen_at="2026-09-01T05:00:00Z", last_seen_at="2026-09-01T05:00:00Z",
                        last_complete_seen_at="2026-09-01T05:00:00Z", absence_observations=[], **closed,
                        current=dict(title=f"Head of QA {sid}", company="Acme", url=f"https://jobs.example/{sid}",
                                     eligible=eligible, text="<p>" + "Long advert. " * 200 + "</p>"))
        self.items = {pending_store.ObservationStore.key(scope, sid): advert(sid, eligible)
                      for sid, eligible in (("kept", True), ("filtered", False))}
        # Establish the held job while open; newly discovered closed jobs are now rejected.
        kept = self.items[pending_store.ObservationStore.key(scope, 'kept')]
        for field in closed:
            kept.pop(field)
        kept['source_lifecycle'] = 'active'
        # This fixture exercises evidence compaction for a previously saved job, not new admission.
        wo.store.create(self.conn, dict(type='job', name='Head of QA kept', status='candidate',
                                       url='https://jobs.example/kept'))
        self.write()
        wo.cmd_import_scans(self.conn, self.pending)
        kept.update(closed)
        self.write()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def write(self):
        (self.pending / "observations.json").write_text(json.dumps(dict(
            schema_version=1, generated_at="2026-09-03T06:00:00Z", sources={}, items=self.items)))

    def stored(self):
        return json.loads((self.pending / "observations.json").read_text())["items"]

    def counts(self):
        return [self.conn.execute(q).fetchone()[0] for q in (
            "SELECT COUNT(*) FROM observations", "SELECT COALESCE(SUM(revision),0) FROM records",
            "SELECT COUNT(*) FROM state_events")]

    def test_held_and_never_imported_adverts_lose_their_text_and_later_imports_change_nothing(self):
        wo.cmd_import_scans(self.conn, self.pending)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records WHERE type='job'").fetchone()[0], 1)
        before = self.counts()
        self.assertEqual(record_imports.compact_observations(self.conn, self.pending, "2026-09-11T06:00:00Z"), 2)
        self.assertTrue(all("text" not in item["current"] and item["compacted_at"] for item in self.stored().values()))
        report = wo.cmd_import_scans(self.conn, self.pending)
        self.assertEqual(self.counts(), before)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["malformed"], [])

    def test_an_advert_whose_latest_facts_the_database_lacks_keeps_its_text(self):
        wo.cmd_import_scans(self.conn, self.pending)
        key = next(k for k in self.items if k.endswith("kept"))
        self.items[key]["current"]["title"] = "Head of Quality"
        self.write()
        self.assertEqual(record_imports.compact_observations(self.conn, self.pending, "2026-09-11T06:00:00Z"), 1)
        self.assertIn("text", self.stored()[key]["current"])


if __name__ == "__main__":
    unittest.main()


class SignalTests(unittest.TestCase):
    """Funding-news signals attach to an organisation; they are never adverts and never summaries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = wo.wodb.connect(self.dir / "t.db")
        wo.wodb.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def signals(self, stories, at="2026-09-25T06:30:00Z"):
        pending = self.dir / "pending"
        pending.mkdir(exist_ok=True)
        items = {f"news:uktn\x1f{s['guid']}": dict(source_scope="news:uktn", source_id=s["guid"], provider="uktn", kind="signal",
                                                   current=dict(eligible=True, **s), first_seen_at=at, last_seen_at=at,
                                                   source_lifecycle="active") for s in stories}
        data = dict(schema_version=1, generated_at=at, items=items,
                    sources={"news:uktn": dict(source_scope="news:uktn", provider="uktn", state="ok", complete=True,
                                               observed_at=at, item_ids=[s["guid"] for s in stories])})
        (pending / "observations.json").write_text(json.dumps(data))
        return wo.cmd_import_scans(self.conn, pending)

    def story(self, company, guid, **extra):
        return dict(guid=guid, company=company, amount="£3m", currency="GBP", amount_millions="3", published="2026-09-23",
                    region="UK", url=guid, headline=f"{company} secures £3m", summary="Luggage storage network.", **extra)

    def test_a_raise_creates_the_company_once_without_a_summary(self):
        first = self.signals([self.story("Stasher", "https://uktn.test/1")])
        again = self.signals([self.story("Stasher", "https://uktn.test/1")])
        orgs = [dict(r) for r in self.conn.execute("SELECT id,name,summary FROM records WHERE type='company'")]
        self.assertEqual([(o["name"], o["summary"]) for o in orgs], [("Stasher", "")])
        self.assertEqual((first["signals"], first["signal_organisations_created"]), (1, 1))
        self.assertEqual(again["signal_organisations_created"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM observations WHERE source='news:uktn'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM records WHERE type='job'").fetchone()[0], 0)
        signal = wo.store.get(self.conn, orgs[0]["id"])["signals"][0]
        self.assertEqual((signal["headline"], signal["published"]), ("Stasher secures £3m", "2026-09-23"))

    def test_a_raise_attaches_to_a_known_organisation_by_name_and_prefers_the_agency(self):
        agency = wo.store.create(self.conn, dict(type="agency", name="Stasher Ltd"))["record"]
        known = wo.store.create(self.conn, dict(type="company", name="Magic AI", summary="The owner's words"))["record"]
        report = self.signals([self.story("Magic AI", "https://uktn.test/2")])
        self.assertEqual(report["signal_organisations_created"], 0)
        self.assertEqual(wo.store.get(self.conn, known["id"])["summary"], "The owner's words")
        self.assertEqual(len(wo.store.get(self.conn, known["id"])["signals"]), 1)
        self.assertEqual(wo.store.get(self.conn, agency["id"])["signals"], [])


class ResearchCreateTests(unittest.TestCase):
    """A research batch may introduce a new organisation explicitly; it is found by name first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = wo.wodb.connect(self.dir / "t.db")
        wo.wodb.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def batch(self, entries):
        path = self.dir / "2026-09-25-qe.json"
        path.write_text(json.dumps(entries))
        return wo.import_research(self.conn, path)

    def entry(self, name, **create):
        return dict(create=dict(type="company", name=name, **create), reviewed_at="2026-09-25",
                    summary=f"{name} is a quality engineering consultancy.", source_urls=[f"https://{name.lower()}.example"],
                    verification="primary-read")

    def test_a_new_organisation_is_created_once_with_its_sourced_facts(self):
        first = self.batch([self.entry("Ten10", url="https://ten10.example", subtype="QE consultancy")])
        again = self.batch([self.entry("Ten10", url="https://ten10.example", subtype="QE consultancy")])
        rows = [dict(r) for r in self.conn.execute("SELECT id,name,url,subtype,summary,verification_level FROM records")]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["url"], rows[0]["subtype"], rows[0]["verification_level"]),
                         ("https://ten10.example", "QE consultancy", "primary-read"))
        self.assertEqual((first["accepted"], first["created"], again["created"]), (1, 1, 0))

    def test_a_known_organisation_is_matched_by_name_not_duplicated(self):
        known = wo.store.create(self.conn, dict(type="agency", name="BJSS"))["record"]
        report = self.batch([self.entry("BJSS", subtype="QE consultancy")])
        self.assertEqual(report["created"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        self.assertEqual(wo.store.get(self.conn, known["id"])["summary"], "BJSS is a quality engineering consultancy.")

    def test_an_entry_without_id_or_create_is_still_reported_unknown(self):
        report = self.batch([dict(id="CO-missing", reviewed_at="2026-09-25", summary="x", source_urls=["https://x.example"])])
        self.assertEqual(report["unknown_ids"], ["CO-missing"])

    def test_research_reports_an_archived_organisation_without_reactivating_it(self):
        self.batch([self.entry('Gone')])
        old = dict(self.conn.execute('SELECT * FROM records').fetchone())
        wo.store.archive(self.conn, old['id'], dict(revision=old['revision'], reason='Closed'))
        report = self.batch([self.entry('Gone')])
        self.assertEqual((report['accepted'], report['created'], report['unknown_ids']), (0, 0, []))
        self.assertEqual(report['archived_organisations'], ['Gone'])

    def test_older_research_is_retained_without_rewinding_current_facts(self):
        self.batch([self.entry("Acme")])
        rid = self.conn.execute('SELECT id FROM records').fetchone()[0]
        new = dict(id=rid, reviewed_at='2026-09-29T10:00:00Z', summary='Current description',
                   source_urls=['https://current.example'], verification='primary-read')
        self.batch([new])
        before = wo.store.get(self.conn, rid)
        old = dict(new, reviewed_at='2026-09-20', summary='Old description',
                   source_urls=['https://old.example'], verification='search-verified')
        report = self.batch([old])
        after = wo.store.get(self.conn, rid)
        self.assertEqual((after['summary'], after['evidence_urls'], after['verification_level'], after['revision']),
                         ('Current description', 'https://current.example', 'primary-read', before['revision']))
        self.assertEqual(report['stale'], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observations WHERE record_id=?", (rid,)).fetchone()[0], 3)
        self.assertEqual(self.batch([old])['accepted'], 0)

    def test_research_uses_shared_normalization_and_keeps_unstated_facts(self):
        self.batch([dict(self.entry('Acme'), summary='  R&amp;D consultancy.  ')])
        rid = self.conn.execute('SELECT id FROM records').fetchone()[0]
        self.assertEqual(wo.store.get(self.conn, rid)['summary'], 'R&D consultancy.')
        self.batch([dict(id=rid, reviewed_at='2026-09-29', signal='New contract', source_urls=['https://acme.example/news'])])
        self.assertEqual(wo.store.get(self.conn, rid)['summary'], 'R&D consultancy.')

    def test_equivalent_research_timestamps_do_not_rewrite_facts(self):
        self.batch([self.entry('Acme')])
        rid = self.conn.execute('SELECT id FROM records').fetchone()[0]
        self.batch([dict(id=rid, reviewed_at='2026-09-29T10:00:00Z', summary='Current', source_urls=['https://acme.example'])])
        self.batch([dict(id=rid, reviewed_at='2026-09-29T11:00:00+01:00', summary='Same-time disagreement', source_urls=['https://other.example'])])
        self.assertEqual(wo.store.get(self.conn, rid)['summary'], 'Current')

    def test_blank_research_values_cannot_clear_facts_or_replace_evidence(self):
        self.batch([self.entry('Acme')])
        rid = self.conn.execute('SELECT id FROM records').fetchone()[0]
        before = wo.store.get(self.conn, rid)
        self.batch([dict(id=rid, reviewed_at='2026-09-29', summary='   ', source_urls=['https://acme.example/new'])])
        self.assertEqual(wo.store.get(self.conn, rid)['summary'], before['summary'])
        with self.assertRaises(ValueError):
            self.batch([dict(id=rid, reviewed_at='2026-09-30', source_urls=['   '])])
        self.assertEqual(wo.store.get(self.conn, rid)['evidence_urls'], 'https://acme.example/new')


class RegistryTests(unittest.TestCase):
    """Companies House facts attach to the company they were read for; they create nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = wo.wodb.connect(self.dir / "t.db")
        wo.wodb.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def registry(self, facts, at="2026-10-01T05:30:00Z"):
        pending = self.dir / "pending"
        pending.mkdir(exist_ok=True)
        items = {f"companies-house\x1f{f['company_number']}": dict(
            source_scope="companies-house", source_id=f["company_number"], provider="companies-house", kind="registry",
            current=f, first_seen_at=at, last_seen_at=at, source_lifecycle="active") for f in facts}
        data = dict(schema_version=1, generated_at=at, items=items,
                    sources={"companies-house": dict(source_scope="companies-house", provider="companies-house", state="ok",
                                                     complete=True, observed_at=at, item_ids=[f["company_number"] for f in facts])})
        (pending / "observations.json").write_text(json.dumps(data))
        return wo.cmd_import_scans(self.conn, pending)

    def facts(self, rid, number="00000001", **extra):
        return {**dict(record_id=rid, name="Zephyr Labs", matched="matched by exact name", company_name="ZEPHYR LABS LIMITED",
                    company_number=number, company_status="active", incorporated_on="2019-04-02", sic_codes=["62012"],
                    officers=[{"name": "QUILL, Ada", "role": "director", "appointed_on": "2019-04-02"}],
                    allotments=[{"filed_on": "2026-09-09", "allotted_on": "2026-08-28", "form": "SH01", "capital": [],
                                 "url": "https://find.test/00000001/filing-history"}],
                    url="https://find.test/00000001"), **extra}

    def number(self, rid, number="00000001"):
        wo.store.company_number(self.conn, rid, dict(number=number, evidence="https://zephyr.example/terms",
                                                     revision=wo.store.get(self.conn, rid)["revision"]), actor="agent")

    def test_facts_attach_to_the_company_once_and_change_nothing_else(self):
        co = wo.store.create(self.conn, dict(type="company", name="Zephyr Labs", summary="The owner's words"))["record"]
        self.number(co["id"])
        first = self.registry([self.facts(co["id"])])
        again = self.registry([self.facts(co["id"])])
        self.assertEqual((first["registry"], again["registry"]), (1, 1))
        got = wo.store.get(self.conn, co["id"])
        self.assertEqual(got["summary"], "The owner's words")
        self.assertEqual(len(got["registry"]), 1)
        self.assertEqual((got["registry"][0]["company_number"], got["registry"][0]["officers"][0]["role"]), ("00000001", "director"))
        self.assertNotIn("record_id", got["registry"][0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM observations WHERE source='companies-house'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM records").fetchone()[0], 1)

    def test_changed_facts_add_a_newer_observation(self):
        co = wo.store.create(self.conn, dict(type="company", name="Zephyr Labs"))["record"]
        self.number(co["id"])
        self.registry([self.facts(co["id"])])
        self.registry([self.facts(co["id"], company_status="liquidation")], at="2026-10-02T05:30:00Z")
        self.assertEqual(wo.store.get(self.conn, co["id"])["registry"][0]["company_status"], "liquidation")

    def test_a_corrected_number_hides_the_old_companys_facts(self):
        co = wo.store.create(self.conn, dict(type="company", name="Zephyr Labs"))["record"]
        self.number(co["id"])
        self.registry([self.facts(co["id"])])
        self.number(co["id"], "00000002")
        self.assertEqual(wo.store.get(self.conn, co["id"])["registry"], [])

    def test_facts_for_a_number_no_active_record_holds_are_skipped(self):
        named = wo.store.create(self.conn, dict(type="company", name="Zephyr Labs"))["record"]
        archived = wo.store.create(self.conn, dict(type="company", name="Gone"))["record"]
        self.number(archived["id"], "00000002")
        wo.store.archive(self.conn, archived["id"], dict(revision=wo.store.get(self.conn, archived["id"])["revision"],
                                                          reason="Closed"), actor="agent")
        report = self.registry([self.facts(named["id"], "00000001"), self.facts(archived["id"], "00000002")])
        self.assertEqual((report["registry"], report["registry_skipped"]), (0, 2))
        self.assertEqual(wo.store.get(self.conn, named["id"])["registry"], [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM observations WHERE source='companies-house'").fetchone()[0], 0)
