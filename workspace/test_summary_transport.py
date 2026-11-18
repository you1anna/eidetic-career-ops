"""Lossless summary transport: no evidence, source or distinction disappears."""
import gzip
import json
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from summary_transport import encode, FORMAT


def decode(envelope):
    """Independent reference decoder; the browser owns the production decoder."""
    strings = envelope['strings']
    def read(value):
        if isinstance(value, str) and value.startswith('~'):
            return value[1:] if value.startswith('~~') else strings[int(value[1:])]
        if isinstance(value, list):
            return [read(v) for v in value]
        if isinstance(value, dict):
            return {k: read(v) for k, v in value.items()}
        return value
    return read(envelope['value'])


class SummaryTransportTests(unittest.TestCase):
    def test_exact_roundtrip_including_reserved_keys_and_source_text(self):
        quote = 'Essential: experience with A or B; C is not required. £700/day — résumé'
        url = 'https://media.licdn.com/dms/image/company-logo?e=12&v=beta&t=a%2Fb%2Bc'
        summary = dict(records=[dict(id='a', quote=quote, url=url, missing=None),
                                dict(id='b', quote=quote, url=url)],
                       special=[{'$text': 0}, {'$object': [['a', quote]]},
                                {'__proto__': quote}, [], {}, 0, False, None, '~0', '~~literal', '~',
                                '~repeated source quote', '~repeated source quote'])
        before = json.dumps(summary)
        envelope = json.loads(encode(summary))
        self.assertEqual(envelope['encoding'], FORMAT)
        self.assertEqual(envelope['strings'].count(quote), 1)
        self.assertEqual(envelope['strings'].count(url), 1)
        self.assertEqual(decode(envelope), summary)
        self.assertEqual(json.dumps(summary), before)

    def test_an_owned_tree_is_packed_in_place_to_the_same_bytes(self):
        quote = 'Essential: experience with A or B; C is not required.'
        value = {'records': [{'id': 'a', 'quote': quote, 'tags': ['~literal', quote, 'short']},
                             {'id': 'b', 'quote': quote, 'nested': {'again': quote, 'n': 1, 'none': None}}],
                 'top': quote}
        expected = encode(value)
        owned = json.loads(json.dumps(value))
        self.assertEqual(encode(owned, owned=True), expected)
        self.assertEqual(decode(json.loads(expected)), value)
        # The caller gave the tree up: it now holds the references, not a second copy.
        self.assertEqual(owned['records'][0]['quote'], '~0')
        self.assertEqual(owned['records'][0]['tags'], ['~~literal', '~0', 'short'])
        for scalar in ('~reserved', quote, 7, None, [], {}):
            self.assertEqual(decode(json.loads(encode(scalar, owned=True))), scalar)

    def test_unique_and_short_strings_remain_literal(self):
        value = ['short', 'short', 'unique longer source text here', 42]
        envelope = json.loads(encode(value))
        self.assertEqual(envelope['strings'], [])
        self.assertEqual(envelope['value'], value)

    def test_distant_repeated_evidence_is_shared_without_truncation(self):
        # Copies separated beyond gzip's window are the summary's measurable cost.
        quotes = [f'Quoted advert {i}: ' + ('different retained evidence %d ' % i) * 5 for i in range(400)]
        value = {'records': [{'quote': q, 'source': f'https://example.test/{i}'} for i, q in enumerate(quotes)],
                 'comparisons': quotes, 'briefs': quotes}
        raw = json.dumps(value, separators=(',', ':')).encode()
        packed = encode(value)
        self.assertEqual(decode(json.loads(packed)), value)
        self.assertLess(len(packed), len(raw) * .7)
        self.assertLess(len(gzip.compress(packed, compresslevel=5)),
                        len(gzip.compress(raw, compresslevel=5)) * .9)
