"""Historical status contracts and capacity, independent of the selection implementation."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_intelligence


def item(action='A', stage='ready'):
    return dict(id=action, stage=stage, readiness={'label': 'Initial'},
                brief={'recommendation': 'Initial', 'approach_route': {'kind': 'current'}})


def outcome(id, *, record='R', action='A', kind='sent', at='2026-10-01', **extra):
    return dict(id=id, record_id=record, kind=kind, at=at,
                snapshot={'action_id': action}, **extra)


def decision(id, **extra):
    return dict(id=id, record_id='R', kind='draft', **extra)


def builder(outcomes=(), decisions=()):
    return decision_intelligence._Builder(dict(records=[], outcomes=outcomes, decisions=decisions),
                                          dt.date(2026, 10, 29))


def status(history, action='A', stage='ready', **record):
    return history.status(item(action, stage), dict(id='R', type='person', **record), None)


class ApproachHistoryTests(unittest.TestCase):
    def test_latest_draft_only_and_first_input_wins_equal_ids(self):
        hold = decision(2, status='answered', answer='hold')
        pending = decision(3, status='pending')
        other = dict(id=9, record_id='R', kind='join', status='done', answer='drop')
        self.assertEqual(status(builder(decisions=[hold, pending, other]))['stage'], 'ready')
        tied = decision(2, status='answered', answer='approve')
        self.assertEqual(status(builder(decisions=[hold, tied]))['stage'], 'held')
        self.assertEqual(status(builder(decisions=[tied, hold]))['stage'], 'ready')

    def test_corrected_events_are_ignored_but_unknown_latest_blocks_fallback(self):
        events = [outcome(1, kind='won'), outcome(3, kind='no_fit', corrected_at='2026-10-03')]
        self.assertEqual(status(builder(events))['stage'], 'won')
        events.append(outcome(2, kind='unrecognised'))
        self.assertEqual(status(builder(events))['stage'], 'ready')

    def test_record_and_action_scopes_keep_legacy_fallback(self):
        events = [outcome(1, action=None, kind='conversation'), outcome(2, kind='won'),
                  outcome(3, action='B', kind='no_fit'), outcome(4, record='other', kind='proposal')]
        history = builder(events)
        self.assertEqual(status(history)['stage'], 'won')
        self.assertEqual(status(history, 'B')['stage'], 'held')
        legacy = status(history, 'C')
        self.assertEqual(legacy['stage'], 'conversation')
        self.assertIn('legacy record-level outcome', legacy['readiness']['reason'])
        events.append(outcome(5, action='', kind='replied'))
        self.assertEqual(status(builder(events))['stage'], 'replied')

    def test_timestamp_id_and_stable_ties_preserve_original_route(self):
        first = outcome(2, kind='submitted')
        first['snapshot']['brief'] = {'approach_route': {'kind': 'first-input'}}
        tied = outcome(2, kind='won')
        tied['snapshot']['brief'] = {'approach_route': {'kind': 'last-input'}}
        events = [first, tied, outcome(1, at='2026-09-01'), outcome(9, at='2026-08-01')]
        result = status(builder(events))
        self.assertEqual(result['last_outcome']['kind'], 'submitted')
        self.assertEqual(result['brief']['approach_route'], {'kind': 'last-input', 'recorded': True})
        newer = outcome(1, at='2026-10-02', kind='replied')
        self.assertEqual(status(builder([*events, newer]))['stage'], 'replied')

    def test_corrections_and_other_contexts_cannot_supply_original_route(self):
        events = [outcome(4, kind='won')]
        for n, changes in enumerate([{'action': 'B'}, {'record': 'other'},
                                     {'corrected_at': '2026-10-02'}, {'action': None}]):
            event = outcome(n, **changes)
            event['snapshot']['brief'] = {'approach_route': {'kind': 'wrong'}}
            events.append(event)
        self.assertEqual(status(builder(events))['brief']['approach_route'], {'kind': 'current'})

    def test_research_and_commercial_stage_precedence(self):
        self.assertEqual(status(builder(), stage='research')['stage'], 'research')
        held = builder(decisions=[decision(1, status='done', answer='drop')])
        self.assertEqual(status(held, stage='research')['stage'], 'held')
        self.assertEqual(status(builder([outcome(1, kind='replied')]), stage='research')['stage'], 'replied')
        record = dict(id='R', type='opportunity', stage='proposal')
        self.assertEqual(builder().status(item(stage='research'), record, None)['stage'], 'proposal')
        self.assertEqual(builder([outcome(1, kind='unknown')]).status(item(stage='research'), record, None)['stage'], 'research')

    def test_snapshot_inputs_are_unchanged_and_instances_are_independent(self):
        events = [outcome(1, kind='replied')]
        decisions = [decision(1, status='done', answer='hold')]
        before = copy.deepcopy((events, decisions))
        history = builder(events, decisions)
        result = status(history)
        result['last_outcome']['kind'] = 'won'
        self.assertEqual(status(history)['stage'], 'replied')
        self.assertEqual(status(builder())['stage'], 'ready')
        self.assertEqual((events, decisions), before)

    def test_history_inspections_grow_linearly_with_records_and_events(self):
        class Counted(dict):
            reads = 0

            def get(self, key, default=None):
                Counted.reads += 1
                return super().get(key, default)

        for size in (40, 160):
            with self.subTest(records=size):
                events = [Counted(outcome(n, record=f'R{n % size}')) for n in range(size * 8)]
                decisions = [Counted(dict(id=n, record_id=f'R{n % size}', kind='draft',
                                         status='answered', answer='hold')) for n in range(size * 2)]
                Counted.reads = 0
                history = builder(events, decisions)
                for n in range(size):
                    result = history.status(item(), dict(id=f'R{n}', type='person'), None)
                    self.assertEqual(result['stage'], 'sent')
                self.assertLessEqual(Counted.reads, 20 * (len(events) + len(decisions) + size))

    def test_outcome_summary_retains_all_uncorrected_kinds(self):
        events = [outcome(1), outcome(2, kind='won'), outcome(3, kind='won'),
                  outcome(4, kind='unknown'), outcome(5, kind=''),
                  outcome(6, kind='won', corrected_at='2026-10-03')]
        self.assertEqual(builder(events).build()['outcomes_summary'],
                         {'counts': {'sent': 1, 'won': 2, 'unknown': 1}, 'total': 4})


if __name__ == '__main__':
    unittest.main()
