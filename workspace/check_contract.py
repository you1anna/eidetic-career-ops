"""Explicit development-copy checks, separate from the exhaustive synthetic API tests.

ops/check.sh contract supplies WO_CONTRACT_DB. Every case migrates and writes only its disposable
backup. Four disjoint partitions cover every record without one long pytest item;
on the VPS unittest executes those same partitions serially.
"""
import os
from pathlib import Path
import unittest

import test_api


def partition(records, index):
    return records[index::4]


class DatabaseContractTests(test_api.ServerCase):
    def setUp(self):
        source = os.environ.get('WO_CONTRACT_DB')
        if not source or not Path(source).is_file():
            raise ValueError('WO_CONTRACT_DB must name the selected development database')
        super().setUp()
        self.http.db = test_api.seeded_copy(
            'selected-contract', lambda path: test_api.migrated_contract_copy(source, path),
            self.path.with_name('contract.db'))

    def state(self):
        state = self.request('GET', '/api/state', origin=False)[2]
        self.assertTrue(state['records'], 'a contract database must contain records')
        return state

    send = test_api.ContractTests.send
    test_accepting_cards_in_turn_keeps_working = test_api.ContractTests.test_accepting_cards_in_turn_keeps_working


def transition_partition(index):
    def test(self):
        for record in partition(self.state()['records'], index):
            revision = record['revision']
            for field, values in record['transitions'].items():
                with self.subTest(record=record['id'], field=field, to=values[0]):
                    revision = self.request('POST', f"/api/records/{record['id']}/transition", {
                        'revision': revision, 'field': field, 'to': values[0]})[2]['record']['revision']
    return test


def save_partition(index):
    def test(self):
        for record in partition(self.state()['records'], index):
            with self.subTest(record=record['id']):
                body = {key: record.get(key) or '' for key in test_api.EDIT_FORM}
                self.assertEqual(self.request('PATCH', f"/api/records/{record['id']}",
                    dict(body, revision=record['revision']))[0], 200)
    return test


for _index in range(4):
    setattr(DatabaseContractTests, f'test_declared_transitions_partition_{_index}', transition_partition(_index))
    setattr(DatabaseContractTests, f'test_unchanged_saves_partition_{_index}', save_partition(_index))


if __name__ == '__main__':
    unittest.main()
