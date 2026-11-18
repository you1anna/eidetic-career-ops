"""In-memory responsiveness figures: bounded, content-free and fed by real saves and rebuilds."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import request_timings
import server
from test_api import ServerCase


class RingTests(unittest.TestCase):
    def setUp(self):
        request_timings.clear()

    def test_spread_is_median_nearest_rank_p90_and_maximum(self):
        for ms in (70, 10, 100, 40, 20, 90, 60, 30, 80, 50):
            request_timings.record('save', ms)
        self.assertEqual(request_timings.snapshot()['kinds']['save'],
                         dict(count=10, median_ms=55.0, p90_ms=90.0, max_ms=100.0))

    def test_each_kind_keeps_its_own_bounded_list(self):
        for n in range(80):
            request_timings.record('save', n)
        for n in range(200):
            request_timings.record('refresh_background', 1)
        kinds = request_timings.snapshot()['kinds']
        self.assertEqual(kinds['save']['count'], 50)
        self.assertEqual(kinds['save']['max_ms'], 79.0)  # the oldest thirty fell out, not the newest
        self.assertEqual(kinds['refresh_background']['count'], 50)
        self.assertEqual(kinds['build'], dict(count=0))

    def test_phase_medians_and_unknown_kinds(self):
        request_timings.record('build', 300, dict(relevance=100, gzip=10))
        request_timings.record('build', 500, dict(relevance=200, gzip=30))
        self.assertEqual(request_timings.snapshot()['kinds']['build']['phase_median_ms'],
                         dict(gzip=20.0, relevance=150.0))
        with self.assertRaises(ValueError):
            request_timings.record('/api/records/abc', 1)

    def test_only_saves_and_page_summaries_count_and_a_refresh_says_why(self):
        kind = server.timing_kind
        delta = '/api/state?view=summary&since=0a1b2c3d-4&encoding=strings-v1'
        self.assertEqual(kind('POST', '/api/records/abc/transition', 200), 'save')
        self.assertEqual(kind('PATCH', '/api/records/abc', 409), 'save')
        self.assertEqual(kind('GET', delta, 200, 'save'), 'refresh_after_save')
        self.assertEqual(kind('GET', '/api/state?view=summary&encoding=strings-v1', 200, 'save'), 'refresh_after_save')
        self.assertEqual(kind('GET', delta, 200, 'background'), 'refresh_background')
        self.assertEqual(kind('GET', delta, 200, 'manual'), 'refresh_other')
        self.assertEqual(kind('GET', delta, 200), 'refresh_other')            # an older page sends no purpose
        self.assertEqual(kind('GET', delta, 200, 'anything else'), 'refresh_other')
        self.assertEqual(kind('GET', '/api/state?view=summary&encoding=strings-v1', 200), 'summary_full')
        self.assertIsNone(kind('POST', '/api/seen/week', 200))
        self.assertIsNone(kind('GET', '/api/status', 200))
        self.assertIsNone(kind('GET', '/api/state', 200))
        self.assertIsNone(kind('POST', '/api/records', 500))


class ServerTimingTests(ServerCase):
    def test_a_save_and_its_refresh_are_recorded_with_build_phases_and_no_content(self):
        request_timings.clear()
        first = self.request('GET', '/api/state?view=summary', origin=False)[2]
        record = self.create('company', 'Timing Example Ltd')
        after = self.request('GET', f"/api/state?view=summary&since={first['version']}", origin=False,
                             headers={'X-WO-Refresh': 'save'})[2]
        for _ in range(3):  # a page left open polls; those cheap reads must not count as save refreshes
            self.request('GET', f"/api/state?view=summary&since={after['version']}", origin=False,
                         headers={'X-WO-Refresh': 'background'})
        snapshot = request_timings.snapshot()
        kinds = snapshot['kinds']
        self.assertEqual(kinds['save']['count'], 1)
        self.assertEqual(kinds['summary_full']['count'], 1)
        self.assertEqual(kinds['refresh_after_save']['count'], 1)
        self.assertEqual(kinds['refresh_background']['count'], 3)
        self.assertEqual(kinds['build']['count'], 2)
        self.assertEqual(set(kinds['build']['phase_median_ms']),
                         {'records', 'reads', 'relevance', 'intelligence', 'selection', 'trim', 'encode'})
        text = json.dumps(snapshot)
        self.assertNotIn(record['id'], text)
        self.assertNotIn('Timing Example', text)
        self.assertNotIn('/api/', text)


if __name__ == '__main__':
    unittest.main()
