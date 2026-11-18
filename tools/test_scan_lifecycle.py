import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.pending_store import ObservationStore


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.tmp.name) / "observations.json")
        self.base = dict(scope="board:greenhouse:acme", source_type="job", provider="greenhouse")

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, when, items, complete=True, state="ok"):
        return self.store.observe(**self.base, observed_at=when, complete=complete, state=state,
                                  items=[{"source_id": sid, "current": facts} for sid, facts in items])

    def record(self, data, sid="1"):
        return data["items"][ObservationStore.key(self.base["scope"], sid)]

    def test_present_facts_refresh_without_rediscovery(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {"title": "Old"})])
        data = self.observe("2026-09-21T00:00:00Z", [("1", {"title": "New"})])
        rec = self.record(data)
        self.assertEqual(rec["current"]["title"], "New")
        self.assertEqual(rec["first_seen_at"], "2026-09-20T00:00:00Z")
        self.assertEqual(rec["source_lifecycle"], "active")

    def test_partial_and_failure_do_not_create_absence(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {"title": "Role"})])
        for state in ("partial", "error", "http-500"):
            data = self.observe("2026-09-21T00:00:00Z", [], complete=False, state=state)
            self.assertEqual(self.record(data)["absence_observations"], [])
            self.assertEqual(self.record(data)["source_lifecycle"], "active")

    def test_two_complete_absences_at_least_24h_close(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {}), ("2", {})])
        one = self.observe("2026-09-20T12:00:00Z", [("2", {})])
        self.assertEqual(self.record(one)["source_lifecycle"], "absent")
        same_day = self.observe("2026-09-20T18:00:00Z", [("2", {})])
        self.assertEqual(self.record(same_day)["source_lifecycle"], "absent")
        closed = self.observe("2026-09-21T12:00:00Z", [("2", {})])
        self.assertEqual(self.record(closed)["source_lifecycle"], "closed")

    def test_anomalous_whole_source_empty_does_not_close(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {})])
        data = self.observe("2026-09-22T00:00:00Z", [], complete=True, state="empty")
        self.assertEqual(data["sources"][self.base["scope"]]["state"], "anomalous-empty")
        self.assertEqual(self.record(data)["absence_observations"], [])

    def test_existing_invalid_evidence_is_never_replaced_with_an_empty_store(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {"title": "Original"})])
        valid = self.store.path.read_text()
        invalid = ['{"schema_version":1', '[]', 'null', '{"schema_version":2}',
                   '{"schema_version":1,"sources":{},"sources":{},"items":{}}',
                   '{"schema_version":1,"sources":[],"items":{}}',
                   '{"schema_version":1,"sources":{},"items":[]}']
        for section in ('sources', 'items'):
            broken = json.loads(valid)
            broken[section][next(iter(broken[section]))] = []
            invalid.append(json.dumps(broken))
        broken = json.loads(valid)
        next(iter(broken['items'].values()))['current'] = []
        invalid.append(json.dumps(broken))
        for text in invalid:
            with self.subTest(text=text[:80]):
                self.store.path.write_text(text)
                before = self.store.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.observe("2026-09-21T00:00:00Z", [("2", {"title": "New"})])
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_read_errors_propagate_without_replacing_evidence(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {"title": "Original"})])
        before = self.store.path.read_bytes()
        with patch.object(Path, 'open', side_effect=PermissionError('unreadable evidence')):
            with self.assertRaises(PermissionError):
                self.observe("2026-09-21T00:00:00Z", [("2", {})])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_only_a_missing_observation_file_initializes_an_empty_store(self):
        self.assertEqual(self.store.load(), dict(schema_version=1, generated_at=None, sources={}, items={}))
        self.assertFalse(self.store.path.exists())

    def test_empty_presence_only_sources_are_successful_and_preserve_previous_items(self):
        for scope, kind in [('feed:reed:uk', 'job'), ('news:test', 'signal'),
                            ('tender:contracts_finder', 'tender')]:
            with self.subTest(scope=scope):
                kwargs = dict(scope=scope, source_type=kind, provider='test', complete=True,
                              absence_authoritative=False)
                first = self.store.observe(**kwargs, observed_at='2026-09-20T00:00:00Z', state='ok',
                                           items=[dict(source_id='1', current={'title': 'Original'})])
                key = self.store.key(scope, '1')
                empty = self.store.observe(**kwargs, observed_at='2026-09-22T00:00:00Z', state='empty', items=[])
                self.assertEqual(empty['sources'][scope]['state'], 'empty')
                self.assertTrue(empty['sources'][scope]['complete'])
                self.assertEqual(empty['items'][key], first['items'][key])

    def test_source_scope_prevents_cross_source_closure(self):
        self.observe("2026-09-20T00:00:00Z", [("1", {"url": "same"})])
        self.store.observe(scope="feed:reed:uk", source_type="job", provider="reed",
                           observed_at="2026-09-22T00:00:00Z", state="ok", complete=True,
                           items=[{"source_id": "1", "current": {"url": "same"}}])
        data = json.loads(self.store.path.read_text())
        self.assertEqual(len(data["items"]), 2)

    def test_explicit_source_closure_is_immediate(self):
        data = self.store.observe(**self.base, observed_at="2026-09-20T10:15:00Z",
                                  complete=False, state="partial", items=[{
                                      "source_id": "1", "current": {"status": "withdrawn"},
                                      "explicit_closure": {"reason": "source-status:withdrawn"}}])
        rec = self.record(data)
        self.assertEqual(rec["source_lifecycle"], "closed")
        self.assertEqual(rec["closure_reason"], "source-status:withdrawn")


class CompactionTests(unittest.TestCase):
    """Adverts closed a week ago or unseen for a month keep their identity, lifecycle and short
    facts; only the advert text goes, and only when the caller says the database holds it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.tmp.name) / "observations.json")
        self.facts = {"title": "Head of QA", "company": "Acme", "url": "https://jobs.example/1",
                      "expires": "2026-12-01", "eligible": True, "engagement": "contract",
                      "text": "<p>" + "Long advert. " * 200 + "</p>", "summary": "Lead QA", "description": "More"}

    def tearDown(self):
        self.tmp.cleanup()

    def board(self, when, ids):
        return self.store.observe(scope="board:greenhouse:acme", source_type="job", provider="greenhouse",
                                  observed_at=when, state="ok", complete=True,
                                  items=[{"source_id": sid, "current": dict(self.facts)} for sid in ids])

    def feed(self, when, ids):
        return self.store.observe(scope="feed:reed:uk", source_type="job", provider="reed", observed_at=when,
                                  state="ok", complete=True, absence_authoritative=False,
                                  items=[{"source_id": sid, "current": dict(self.facts)} for sid in ids])

    def item(self, scope, sid):
        return json.loads(self.store.path.read_text())["items"][ObservationStore.key(scope, sid)]

    def close_board_advert(self):
        self.board("2026-09-01T05:00:00Z", ["1", "2"])
        self.board("2026-09-02T05:00:00Z", ["2"])
        self.board("2026-09-03T06:00:00Z", ["2"])
        self.assertEqual(self.item("board:greenhouse:acme", "1")["source_lifecycle"], "closed")

    def test_closed_for_a_week_keeps_identity_and_short_facts_but_not_text(self):
        self.close_board_advert()
        before = self.item("board:greenhouse:acme", "1")
        self.assertEqual(self.store.compact("2026-09-11T06:00:00Z", held=lambda item: True), 1)
        after = self.item("board:greenhouse:acme", "1")
        self.assertEqual(after["compacted_at"], "2026-09-11T06:00:00Z")
        for field in ("text", "summary", "description"):
            self.assertNotIn(field, after["current"])
        for field in ("title", "company", "url", "expires", "eligible", "engagement"):
            self.assertEqual(after["current"][field], before["current"][field])
        for field in ("source_lifecycle", "closed_at", "closure_reason", "first_seen_at", "last_seen_at",
                      "last_complete_seen_at", "absence_observations"):
            self.assertEqual(after[field], before[field])
        self.assertIn("text", self.item("board:greenhouse:acme", "2")["current"])
        self.store.load()

    def test_nothing_changes_before_a_week_or_while_the_database_lacks_the_advert(self):
        self.close_board_advert()
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.compact("2026-09-09T06:00:00Z", held=lambda item: True), 0)
        self.assertEqual(self.store.compact("2026-09-30T06:00:00Z", held=lambda item: False), 0)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_an_advert_a_presence_only_feed_stopped_reporting_compacts_after_a_month(self):
        self.feed("2026-09-01T05:00:00Z", ["1"])
        self.feed("2026-09-20T05:00:00Z", ["2"])
        self.assertEqual(self.store.compact("2026-09-30T05:00:00Z", held=lambda item: True), 0)
        self.assertEqual(self.store.compact("2026-10-02T05:00:00Z", held=lambda item: True), 1)
        rec = self.item("feed:reed:uk", "1")
        self.assertEqual(rec["source_lifecycle"], "active")
        self.assertNotIn("text", rec["current"])
        self.assertIn("text", self.item("feed:reed:uk", "2")["current"])

    def test_a_compacted_advert_seen_again_is_whole_and_keeps_its_first_sighting(self):
        self.close_board_advert()
        self.store.compact("2026-09-11T06:00:00Z", held=lambda item: True)
        self.board("2026-09-12T05:00:00Z", ["1", "2"])
        rec = self.item("board:greenhouse:acme", "1")
        self.assertNotIn("compacted_at", rec)
        self.assertIn("text", rec["current"])
        self.assertEqual(rec["first_seen_at"], "2026-09-01T05:00:00Z")
        self.assertEqual(rec["source_lifecycle"], "active")

    def test_signals_and_registry_facts_are_left_alone(self):
        self.store.observe(scope="news:uktn", source_type="signal", provider="uktn",
                           observed_at="2026-08-01T05:00:00Z", state="ok", complete=True,
                           absence_authoritative=False, items=[{"source_id": "s", "current": dict(self.facts)}])
        self.assertEqual(self.store.compact("2026-10-01T05:00:00Z", held=lambda item: True), 0)


if __name__ == "__main__":
    unittest.main()
