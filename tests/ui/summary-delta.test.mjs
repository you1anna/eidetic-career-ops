// Save updates: a patched summary equals the server's new summary, or the page reloads in full.
import assert from 'node:assert/strict';
import test from 'node:test';
import { applySummaryDelta } from '../../workspace/js/summary-delta.js';

const base = () => ({
  version: 'a1b2c3d4-1', loaded: 'then', relevance_error: 'old',
  records: [{ id: 'A', name: 'Alpha' }, { id: 'B', name: 'Beta' }, { id: 'C', name: 'Gamma' }],
  intelligence: { actions: [{ id: 'x', stage: 'ready' }], research: [{ id: 'r', stage: 'qualify' }], sections: { now: ['x'] } },
  opportunities: { discovery: [] }, coverage: { channels: [], runs: [] },
});
const none = { upsert: [], remove: [] };
const delta = (fields = {}) => ({
  delta: true, base: 'a1b2c3d4-1', version: 'a1b2c3d4-2', set: { loaded: 'now' }, unset: [],
  intelligence: { set: {}, unset: [] }, collections: { records: none, actions: none, research: none }, ...fields,
});

test('changed, added, removed and reordered entries rebuild the new summary', () => {
  const before = base(), frozen = JSON.stringify(before);
  const after = applySummaryDelta(before, delta({
    set: { loaded: 'now', opportunities: { discovery: [{ record_id: 'D' }] } }, unset: ['relevance_error'],
    intelligence: { set: { sections: { now: [] } }, unset: [] },
    collections: {
      records: { upsert: [{ id: 'B', name: 'Beta 2' }, { id: 'D', name: 'Delta' }], remove: ['A'], order: ['D', 'C', 'B'] },
      actions: { upsert: [{ id: 'x', stage: 'done' }], remove: [] }, research: none,
    },
  }));
  assert.deepEqual(after, {
    version: 'a1b2c3d4-2', loaded: 'now', coverage: { channels: [], runs: [] },
    records: [{ id: 'D', name: 'Delta' }, { id: 'C', name: 'Gamma' }, { id: 'B', name: 'Beta 2' }],
    intelligence: { actions: [{ id: 'x', stage: 'done' }], research: [{ id: 'r', stage: 'qualify' }], sections: { now: [] } },
    opportunities: { discovery: [{ record_id: 'D' }] },
  });
  assert.equal(JSON.stringify(before), frozen, 'the previous state is never modified');
  assert.equal(after.records[1], before.records[2], 'unchanged entries are reused');
});

test('without a new order the previous order stands', () => {
  const after = applySummaryDelta(base(), delta({ collections: { records: { upsert: [{ id: 'C', name: 'Gamma 2' }], remove: [] }, actions: none, research: none } }));
  assert.deepEqual(after.records.map(r => r.id), ['A', 'B', 'C']);
  assert.equal(after.records[2].name, 'Gamma 2');
});

test('a mismatched or inconsistent update is refused so the page reloads in full', () => {
  assert.throws(() => applySummaryDelta(base(), delta({ base: 'a1b2c3d4-9' })), /does not match/);
  assert.throws(() => applySummaryDelta({ ...base(), version: undefined }, delta()), /does not match/);
  assert.throws(() => applySummaryDelta(base(), { ...base() }), /does not match/);
  const bad = (records) => delta({ collections: { records, actions: none, research: none } });
  assert.throws(() => applySummaryDelta(base(), bad({ upsert: [{ id: 'D' }], remove: [] })), /does not match/);
  assert.throws(() => applySummaryDelta(base(), bad({ upsert: [], remove: ['Z'] })), /does not match/);
  assert.throws(() => applySummaryDelta(base(), bad({ upsert: [], remove: [], order: ['A', 'B', 'Z'] })), /does not match/);
});
