"""The explicit data check must cover each record once and use a disposable source copy."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check_contract
import db
import records


class ContractInputTests(unittest.TestCase):
    def test_partitions_are_disjoint_and_include_the_tail(self):
        expected = [[0, 4, 8], [1, 5, 9], [2, 6], [3, 7]]
        self.assertEqual([check_contract.partition(list(range(10)), n) for n in range(4)], expected)

    def test_selected_source_is_copied_and_only_the_copy_is_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'chosen.db'
            conn = db.connect(source)
            db.migrate(conn)
            created = records.create(conn, {'type': 'person', 'name': 'Only in chosen input'}, actor='agent')['record']
            conn.close()
            before = source.read_bytes()
            # A fresh cache key for this test; the data gate itself builds one template per worker.
            with patch.dict(os.environ, WO_CONTRACT_DB=str(source)), patch.dict(check_contract.test_api._seeded, clear=True):
                case = check_contract.DatabaseContractTests('test_unchanged_saves_partition_0')
                case.setUp()
                try:
                    self.assertNotEqual(case.http.db, source)
                    self.assertEqual([row['name'] for row in case.state()['records']], ['Only in chosen input'])
                    case.request('PATCH', f"/api/records/{created['id']}", {'revision': 1, 'name': 'Changed copy'})
                    self.assertEqual(case.state()['records'][0]['name'], 'Changed copy')
                finally:
                    case.tearDown()
            self.assertEqual(source.read_bytes(), before)

    def test_missing_input_cannot_silently_use_a_synthetic_fixture(self):
        with patch.dict(os.environ, WO_CONTRACT_DB=''):
            case = check_contract.DatabaseContractTests('test_unchanged_saves_partition_0')
            with self.assertRaisesRegex(ValueError, 'WO_CONTRACT_DB'):
                case.setUp()
