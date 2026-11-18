"""The quality report finds each defect class, and repair fixes the deterministic ones."""
import datetime
import sys
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quality  # noqa: E402
import wo  # noqa: E402


class QualityTests(unittest.TestCase):
    def test_name_variant_index_matches_pairwise_rules_with_one_normalisation_per_name(self):
        names = ['Sanderson Recruitment', 'Sanderson', 'Sanderson Limited', 'Capital One UK',
                 'Capital One', 'Meta', 'Metaswitch', '', 'ACME &amp; Sons', 'ACME and Sons UK',
                 'Café', 'Café Systems', 'Capital One UK Services']
        firms = [dict(id=str(i), name=name) for i, name in enumerate(names)]
        expected = [' ~ '.join(sorted((a['id'], b['id']))) for i, a in enumerate(firms)
                    for b in firms[i + 1:] if quality.normalise.org_key(a['name']) != quality.normalise.org_key(b['name'])
                    and quality.relevance.same_advertiser(a['name'], b['name'])]
        with patch.object(quality.normalise, 'org_key', wraps=quality.normalise.org_key) as key:
            self.assertEqual(quality.organisation_name_variants(firms), expected)
            self.assertEqual(key.call_count, len(firms))

    def test_legacy_merge_evidence_is_a_defect_and_repair_preserves_the_original(self):
        src = wo.store.create(self.conn, dict(type='company', name='Old copy'))['record']
        middle = wo.store.create(self.conn, dict(type='company', name='Middle copy'))['record']
        keep = wo.store.create(self.conn, dict(type='company', name='Survivor'))['record']
        payload = json.dumps({'desk_summary': 'Sourced evidence', 'reviewed_at': '2026-09-24'})
        wo.store.add_observation(self.conn, src['id'], 'research:legacy.json', '2026-09-24', payload)
        wo.store.merge(self.conn, src['id'], middle['id'], dict(revision=src['revision']))
        wo.store.merge(self.conn, middle['id'], keep['id'], dict(revision=wo.store.get(self.conn, middle['id'])['revision']))
        # The old merger moved identity but did not copy observations to the survivor.
        self.conn.execute('DELETE FROM observations WHERE record_id IN (?,?)', (middle['id'], keep['id']))
        self.assertEqual(quality.check(self.conn)['defects']['merged_evidence_unavailable']['count'], 1)
        self.assertEqual(quality.repair(self.conn, dry_run=True)['would_fix']['merged_evidence_unavailable'], 1)
        quality.repair(self.conn, actor='agent')
        self.assertEqual(quality.check(self.conn)['defects']['merged_evidence_unavailable']['count'], 0)
        original = self.conn.execute('SELECT payload_json FROM observations WHERE record_id=?', (src['id'],)).fetchone()[0]
        restored = self.conn.execute('SELECT payload_json FROM observations WHERE record_id=?', (keep['id'],)).fetchone()[0]
        self.assertEqual((original, restored), (payload, payload))
        self.assertEqual(wo.store.get(self.conn, keep['id'])['summary'], '')
        self.assertEqual(quality.repair(self.conn, actor='agent')['fixed'], {})

    def test_a_research_signal_shared_by_three_records_is_a_gap(self):
        ids = [wo.store.create(self.conn, dict(type='company', name=f'Co {n}'))['record']['id'] for n in 'ABCD']
        for rid in ids[:3]:
            wo.store.add_observation(self.conn, rid, 'research:batch.json', '2026-09-24', json.dumps(
                {'signal': 'What the company does, from its own job advert.', 'reviewed_at': '2026-09-24'}))
        wo.store.add_observation(self.conn, ids[3], 'research:batch.json', '2026-09-24', json.dumps(
            {'signal': 'Launched an eval harness in August.', 'reviewed_at': '2026-09-24'}))
        gap = quality.check(self.conn)['gaps']['generic_research_signal']
        self.assertEqual((gap['count'], sorted(gap['items'])), (3, sorted(ids[:3])))
        # A later, specific signal is the one shown, so the record no longer has the gap.
        wo.store.add_observation(self.conn, ids[0], 'research:later.json', '2026-10-01', json.dumps(
            {'signal': 'Advertising a Head of Quality Engineering.', 'reviewed_at': '2026-10-01'}))
        self.assertEqual(sorted(quality.check(self.conn)['gaps']['generic_research_signal']['items']), sorted(ids[1:3]))

    def test_generic_research_signal_is_no_gap_when_a_current_advert_gives_the_reason(self):
        ids = [wo.store.create(self.conn, dict(type='company', name=f'Co {n}'))['record']['id'] for n in 'ABC']
        for rid in ids:
            wo.store.add_observation(self.conn, rid, 'research:batch.json', '2026-09-24', json.dumps(
                {'signal': 'Existing evidence inventory only; no fresh verification.', 'reviewed_at': '2026-09-24'}))
        today = datetime.date.today().isoformat()
        job = wo.store.create(self.conn, dict(type='job', name='Head of Engineering', organisation='Co A',
                                              summary='Lead software engineering', engagement='contract',
                                              location='London', date=today))['record']
        wo.store.add_observation(self.conn, job['id'], 'board:greenhouse:co-a', f'{today}T05:30:00Z',
                                 json.dumps(dict(source_scope='board:greenhouse:co-a', source_id='1', kind='job')))
        wo.store.link(self.conn, dict(from_id=job['id'], to_id=ids[0], relation='hiring_for',
                                      revision=wo.store.get(self.conn, job['id'])['revision']))
        gap = quality.check(self.conn)['gaps']['generic_research_signal']
        # Co A shows "Hiring now" from its advert, so the batch sentence hides nothing; B and C show no reason.
        self.assertEqual(sorted(gap['items']), sorted(ids[1:]))

    def test_cross_source_fact_disagreement_is_a_gap_and_is_not_split_by_repair(self):
        r = wo.store.create(self.conn, dict(type='job', name='Interim EM', organisation='Co'))['record']
        for source, location in (('board:a', 'London'), ('feed:b', 'Manchester')):
            payload = dict(source_scope=source, source_id='1', kind='job', current=dict(location=location, engagement='contract'))
            wo.store.add_observation(self.conn, r['id'], source, '2026-09-30', json.dumps(payload))
            self.conn.execute('INSERT INTO source_records VALUES(?,?,?)', (source, '1', r['id']))
        self.assertEqual(quality.check(self.conn)['gaps']['cross_source_fact_disagreement']['items'], [r['id']])
        quality.repair(self.conn, actor='agent')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_records WHERE record_id=?', (r['id'],)).fetchone()[0], 2)
        self.assertEqual(quality.check(self.conn)['gaps']['cross_source_fact_disagreement']['items'], [r['id']])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = wo.wodb.connect(Path(self.tmp.name) / "q.db")
        wo.wodb.migrate(self.conn)
        # Imported-style rows: written without the edit service, as the hub and scan importers do.
        rows = [
            ("CO-a", "company", "Newton&#x27;s Tree", "", "", ""),
            ("CO-b", "company", "Newton's Tree", "", "", ""),
            ("AG-1", "agency", "Client Server", "", "", ""),
            ("CO-c", "company", "Client Server", "Existing directory links this company to: x", "", ""),
            ("J-1", "job", "Interim EM", "", "https://jobs.example/1", "Unknown"),
            ("S-1", "job", "Interim EM", "", "https://jobs.example/1", ""),
            ("J-2", "job", "AI Engineer | apply: acme.com/jobs/1", "", "https://jobs.example/2", ""),
            ("J-3", "job", "Head of QA", "<p>Advert</p>", "https://jobs.example/3", ""),
        ]
        for rid, kind, name, summary, url, date in rows:
            self.conn.execute("INSERT INTO records(id,type,name,summary,url,date,organisation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (rid, kind, name, summary, url, date, "Client Server" if kind == "job" else "", "2026-09-24", "2026-09-24"))
        self.conn.execute("INSERT INTO followups(record_id,action,owner,status,title,source,updated_at) VALUES('J-1','Review','owner','needs_review','x','local','2026-09-24')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_each_defect_class_is_found(self):
        d = quality.check(self.conn)["defects"]
        for name, expected in [("html_entities", 1), ("duplicate_organisation", 1), ("agency_company_overlap", 1),
                               ("advert_recorded_twice", 1), ("title_fragment", 1), ("bookkeeping_summary", 1),
                               ("date_free_text", 1), ("unlinked_advert", 4), ("markup_in_text", 1)]:
            self.assertEqual(d[name]["count"], expected, name)
        self.assertEqual(d["advert_recorded_twice"]["items"], ["S-1 -> J-1"])

    def test_people_recorded_twice_are_a_defect_and_shared_names_a_gap(self):
        for rid, name, url in (("P-1", "Ann", "https://www.linkedin.com/in/ann"), ("P-2", "Ann", "https://www.linkedin.com/in/ann/"),
                               ("P-3", "Bob", ""), ("P-4", "bob", "")):
            self.conn.execute("INSERT INTO records(id,type,name,url,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                              (rid, "person", name, url, "2026-09-24", "2026-09-24"))
        report = quality.check(self.conn)
        self.assertEqual(report["defects"]["person_recorded_twice"]["items"], ["P-1 = P-2"])
        self.assertIn("P-3 = P-4", report["gaps"]["people_sharing_a_name"]["items"])

    def test_organisation_name_variants_are_a_gap_that_repair_leaves_alone(self):
        for rid, typ, kind, name in (("AG-9", "agency", "agency", "Sanderson Recruitment"), ("CO-9", "company", "company", "Sanderson"),
                                     ("CO-10", "company", "company", "Capital One UK"), ("CO-11", "company", "company", "Capital One"),
                                     ("AG-10", "agency", "community", "SnapLogic IntegrateAI London"), ("CO-12", "company", "company", "SnapLogic"),
                                     ("CO-13", "company", "company", "Meta"), ("CO-14", "company", "company", "Metaswitch")):
            self.conn.execute("INSERT INTO records(id,type,kind,name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                              (rid, typ, kind, name, "2026-09-24", "2026-09-24"))
        items = quality.check(self.conn)["gaps"]["organisations_sharing_a_name"]["items"]
        self.assertEqual(sorted(items), ["AG-9 ~ CO-9", "CO-10 ~ CO-11"])
        quality.repair(self.conn, actor="agent")
        self.assertFalse(wo.store.get(self.conn, "CO-9")["archived_at"])

    def test_repair_leaves_only_manual_defects_and_is_idempotent(self):
        quality.repair(self.conn, actor="agent")
        remaining = {k: v["count"] for k, v in quality.check(self.conn)["defects"].items() if v["count"]}
        self.assertEqual(remaining, {"markup_in_text": 1})  # manual classes stay reported
        self.assertEqual(quality.repair(self.conn, actor="agent")["fixed"], {})
        self.assertEqual(wo.store.get(self.conn, "J-2")["name"], "AI Engineer")
        self.assertTrue(wo.store.get(self.conn, "S-1")["archived_at"])
        self.assertTrue(wo.store.get(self.conn, "CO-c")["archived_at"])
        actors = {r[0] for r in self.conn.execute("SELECT DISTINCT actor FROM state_events")}
        self.assertEqual(actors, {"agent"})

    def test_contact_details_in_a_person_name_move_to_their_field(self):
        self.conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES('P-9','person','Jack (jack@agency.example, on file)','2026-09-24','2026-09-24')")
        self.assertEqual(quality.check(self.conn)["defects"]["contact_in_name"]["items"], ["P-9"])
        quality.repair(self.conn, actor="agent")
        jack = wo.store.get(self.conn, "P-9")
        self.assertEqual((jack["name"], jack["email"]), ("Jack", "jack@agency.example"))
        self.assertIn("on file", jack["notes"])
        self.assertEqual(quality.check(self.conn)["defects"]["contact_in_name"]["count"], 0)

    def test_unlinked_person_organisation_is_detected_and_repaired_preserving_unverified_employment(self):
        org = wo.store.create(self.conn, dict(type="company", name="Matched Employer"))["record"]
        p1 = wo.store.create(self.conn, dict(type="person", name="Existing Org Contact"))["record"]
        wo.store.add_observation(self.conn, p1["id"], "LinkedIn Connections CSV", "2026-10-01",
                                 json.dumps(dict(observed_company="Matched Employer", observed_role="CTO")))
        p2 = wo.store.create(self.conn, dict(type="person", name="Priority Contact", priority="first"))["record"]
        wo.store.add_observation(self.conn, p2["id"], "LinkedIn Connections CSV", "2026-10-01",
                                 json.dumps(dict(observed_company="Brand New Employer", observed_role="Head of QA")))
        items = quality.check(self.conn)["defects"]["unlinked_person_organisation"]["items"]
        self.assertEqual(sorted(items), sorted([p1["id"], p2["id"]]))
        quality.repair(self.conn, actor="agent")
        self.assertEqual(quality.check(self.conn)["defects"]["unlinked_person_organisation"]["count"], 0)
        links1 = [dict(r) for r in self.conn.execute("SELECT * FROM links WHERE from_id=?", (p1["id"],))]
        self.assertEqual([(l["to_id"], l["relation"], l["notes"]) for l in links1],
                         [(org["id"], "works_at", "LinkedIn export; unverified")])
        self.assertEqual(wo.store.get(self.conn, p1["id"])["organisation"], "")
        links2 = [dict(r) for r in self.conn.execute("SELECT * FROM links WHERE from_id=?", (p2["id"],))]
        self.assertEqual(len(links2), 1)
        new_org = wo.store.get(self.conn, links2[0]["to_id"])
        self.assertEqual((new_org["type"], new_org["name"]), ("company", "Brand New Employer"))


if __name__ == "__main__":
    unittest.main()
