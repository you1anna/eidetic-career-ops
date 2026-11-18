import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pending_store import PendingStore, atomic_write  # noqa: E402
from pending_store import ObservationStore

COLUMNS = ["date_seen", "url", "title"]


class PendingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PendingStore(Path(self.tmp.name) / "pending", COLUMNS, "url")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        rows = [{"date_seen": "2026-09-17", "url": "https://a/1", "title": "Test Manager"}]
        self.store.save("2026-09-17", rows, {"https://a/1"}, {"totals": {"new": 1}})
        self.assertEqual(self.store.load_day("2026-09-17"), rows)
        self.assertEqual(self.store.load_seen(), {"https://a/1"})
        self.assertEqual(self.store.last_run()["totals"]["new"], 1)

    def test_resume_recovers_keys_from_the_day_file(self):
        """The day's rows are written before the seen index, so a crash between the two must not
        cause the next run to propose the same rows again."""
        rows = [{"date_seen": "2026-09-17", "url": "https://a/1", "title": "T"}]
        self.store.save("2026-09-17", rows, {"https://a/1"}, {})
        self.store.seen_path.write_text("[]")  # index lost
        recovered, seen = self.store.resume("2026-09-17")
        self.assertEqual(recovered, rows)
        self.assertIn("https://a/1", seen)

    def test_an_unreadable_day_file_raises_rather_than_dropping_rows(self):
        path = self.store.day_path("2026-09-17")
        path.write_text("url\tsomething_else\nhttps://a/1\tx\n")
        with self.assertRaises(ValueError):
            self.store.load_day("2026-09-17")
        path.write_text("date_seen\turl\ttitle\n2026-09-17\t\tT\n")  # empty key
        with self.assertRaises(ValueError):
            self.store.load_day("2026-09-17")

    def test_missing_files_are_an_empty_start_not_an_error(self):
        rows, seen = self.store.resume("2026-09-17")
        self.assertEqual((rows, seen), ([], set()))
        self.assertEqual(self.store.last_run(), {})

    def test_tabs_and_newlines_in_a_value_stay_on_one_line(self):
        rows = [{"date_seen": "2026-09-17", "url": "https://a/1", "title": "A\tB\nC\rD"}]
        text = self.store.render(rows)
        self.assertEqual(len(text.strip().splitlines()), 2)
        self.assertIn("A B C D", text)

    def test_atomic_write_leaves_no_temp_file_behind(self):
        p = Path(self.tmp.name) / "x.txt"
        atomic_write(p, "hello")
        self.assertEqual(p.read_text(), "hello")
        self.assertEqual([f.name for f in p.parent.iterdir() if f.name.startswith(".")], [])

    def test_coalesced_first_sighting_is_atomic_and_cannot_be_in_the_future(self):
        store=ObservationStore(Path(self.tmp.name)/'observations.json')
        args=dict(scope='alert:example',source_type='job',provider='example',
                  observed_at='2026-11-05T09:00:00Z',state='ok',complete=True,absence_authoritative=False)
        store.observe(**args,items=[dict(source_id='1',first_seen_at='2026-11-01T09:00:00Z',current={'title':'New facts'})])
        before=store.path.read_bytes()
        self.assertEqual(store.load()['items']['alert:example\x1f1']['first_seen_at'],'2026-11-01T09:00:00Z')
        with self.assertRaises(ValueError):
            store.observe(**args,items=[dict(source_id='2',first_seen_at='2026-11-06T09:00:00Z',current={})])
        self.assertEqual(store.path.read_bytes(),before)

    def test_source_update_preserves_large_evidence_without_serialization_copies(self):
        import tracemalloc
        store = ObservationStore(Path(self.tmp.name) / 'observations.json')
        args = dict(scope='board:example', source_type='job', provider='example',
                    observed_at='2026-10-28T09:00:00Z', state='ok', complete=True)
        items = [dict(source_id=str(i), current={'text': str(i) + ' Synthetic advert.' * 1000})
                 for i in range(150)]
        before = store.observe(**args, items=items)
        size = store.path.stat().st_size
        tracemalloc.start()
        try:
            after = store.observe(**dict(args, scope='alert:linkedin'), items=[], absence_authoritative=False)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(after['items'], before['items'])
        self.assertEqual(store.load(), after)
        self.assertLess(peak, size * 2.6, 'Updating one source must not materialize a whole serialized copy')

    def test_an_unsuitable_advert_keeps_identity_and_short_facts_without_its_text(self):
        store = ObservationStore(Path(self.tmp.name) / 'observations.json')
        args = dict(scope='board:example', source_type='job', provider='example', state='ok', complete=True)
        store.observe(**args, observed_at='2026-11-04T05:30:00Z', items=[dict(
            source_id='1', compaction_reason='title-unmatched',
            current={'title': 'Director of Marketing', 'url': 'https://jobs.example/1', 'pay_min': '800',
                     'text': 'Long advert', 'summary': 'Short', 'qualification': {'eligible': True}})])
        item = store.load()['items'][ObservationStore.key('board:example', '1')]
        self.assertEqual(item['current'], {'title': 'Director of Marketing', 'url': 'https://jobs.example/1', 'pay_min': '800'})
        self.assertEqual((item['compacted_at'], item['compaction_reason'], item['source_lifecycle']),
                         ('2026-11-04T05:30:00Z', 'title-unmatched', 'active'))
        # A later suitable sighting is ordinary evidence again: full facts, no compaction marker.
        store.observe(**args, observed_at='2026-11-05T05:30:00Z', items=[dict(
            source_id='1', current={'title': 'Head of Quality', 'text': 'Full advert'})])
        item = store.load()['items'][ObservationStore.key('board:example', '1')]
        self.assertEqual(item['current'], {'title': 'Head of Quality', 'text': 'Full advert'})
        self.assertNotIn('compacted_at', item)
        self.assertNotIn('compaction_reason', item)
        self.assertEqual(item['first_seen_at'], '2026-11-04T05:30:00Z')

    def test_compaction_also_drops_derived_qualification(self):
        store = ObservationStore(Path(self.tmp.name) / 'observations.json')
        store.observe(scope='board:example', source_type='job', provider='example', state='ok', complete=True,
                      observed_at='2026-01-01T05:30:00Z', items=[dict(
                          source_id='1', current={'title': 'Head of QA', 'text': 'Long', 'qualification': {'eligible': True}})])
        self.assertEqual(store.compact('2026-03-01T05:30:00Z', held=lambda item: True), 1)
        item = store.load()['items'][ObservationStore.key('board:example', '1')]
        self.assertEqual(item['current'], {'title': 'Head of QA'})

    def test_protection_manifest_names_held_evidence_and_fails_closed(self):
        from pending_store import Protection, write_protection
        path = Path(self.tmp.name) / 'protected-sources.json'
        self.assertEqual(Protection.load(path), (None, 'missing'))
        write_protection(path, at='2026-11-04T05:30:00Z', identities=[('board:greenhouse:ghco', '6')],
                         urls=['https://jobs.example/held'], roles=[('gh co', 'director of marketing')])
        protection, state = Protection.load(path)
        self.assertEqual(state, 'ok')
        held = dict(scope='board:greenhouse:ghco', source_id='9', url='https://jobs.example/9', company='Other', title='Other')
        self.assertFalse(protection.holds(**held))
        self.assertTrue(protection.holds(**dict(held, source_id='6')))
        self.assertTrue(protection.holds(**dict(held, url='https://JOBS.example/held#apply')))
        self.assertTrue(protection.holds(**dict(held, company='GH Co', title='Director of Marketing')))
        # An advert URL the importer could not normalise is kept whole rather than guessed at.
        self.assertTrue(protection.holds(**dict(held, url='not a url')))
        for broken in ('{"schema_version": 1', '{"schema_version": 2, "identities": [], "urls": [], "roles": []}',
                       '{"schema_version": 1, "identities": "x", "urls": [], "roles": []}', '[]'):
            with self.subTest(broken=broken):
                path.write_text(broken)
                self.assertEqual(Protection.load(path), (None, 'invalid'))

    def test_interrupted_observation_replacement_keeps_prior_evidence(self):
        from unittest.mock import patch
        store = ObservationStore(Path(self.tmp.name) / 'observations.json')
        args = dict(scope='board:example', source_type='job', provider='example',
                    observed_at='2026-10-28T09:00:00Z', state='ok', complete=True)
        store.observe(**args, items=[dict(source_id='1', current={'text': 'Preserve me'})])
        before = store.path.read_bytes()
        with patch('pending_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.observe(**args, items=[dict(source_id='2', current={'text': 'New'})])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(list(store.path.parent.glob('.observations.json.*')), [])


if __name__ == "__main__":
    unittest.main()
