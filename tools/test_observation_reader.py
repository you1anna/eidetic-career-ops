"""Lossless, bounded reads of the existing observation JSON format."""
import io
import json
from pathlib import Path
import sys
import tracemalloc
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))


class ObservationReaderTests(unittest.TestCase):
    def test_streaming_visitor_sees_items_without_retaining_them(self):
        from observation_reader import read
        seen = []
        result = read(io.StringIO('{"items":{"a":{"value":1},"b":{"value":2}},"sources":{}}'),
                      sources_only=True, visit_item=lambda key, value: seen.append((key, value)))
        self.assertEqual(seen, [('a', {'value':1}), ('b', {'value':2})])
        self.assertEqual(result, {'sources':{}})

    def test_chunk_boundaries_and_source_only_reads_preserve_values(self):
        from observation_reader import read
        value = {'schema_version': 1, 'items': {str(i): {'current': {'text': 'Snow ☃ \\"\n' * 100}, 'n': 12345} for i in range(4)},
                 'sources': {'one': {'item_ids': ['a', 'b'], 'detail': {'ok': True}}}, 'unknown': [None, -1.23e7], 'top_number': 1.2345e37}
        encoded = json.dumps(value, ensure_ascii=False)
        for chunk in (1, 7, 1024):
            self.assertEqual(read(io.StringIO(encoded), chunk_size=chunk), value)
            self.assertEqual(read(io.StringIO(encoded), sources_only=True, chunk_size=chunk), {'sources': value['sources']})

    def test_rejects_invalid_or_truncated_json_even_in_discarded_items(self):
        from observation_reader import read
        bad = ['{"items":{"a": {"x":1,"x":2}}}', '{"items":{},"items":{}}',
               '{"items":{"a":1,}}', '{"sources":{}} trailing', '{"sources":',
               '{"items":{"a":12x}}', '{"items":{"a":true false}}', '{"items":{},}']
        for text in bad:
            for source_only in (False, True):
                with self.subTest(text=text, source_only=source_only), self.assertRaises(ValueError):
                    read(io.StringIO(text), sources_only=source_only, chunk_size=7)

    def test_source_metadata_does_not_retain_advert_archive(self):
        from observation_reader import read
        text = json.dumps({'items': {str(i): {'text': str(i) + 'synthetic advert ' * 1000} for i in range(400)},
                           'sources': {'one': {'state': 'ok'}}})
        source = io.StringIO(text)
        tracemalloc.start()
        try:
            result = read(source, sources_only=True)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result, {'sources': {'one': {'state': 'ok'}}})
        self.assertLess(peak, 1024 * 1024)
