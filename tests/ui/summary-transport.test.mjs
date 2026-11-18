import assert from 'node:assert/strict';
import test from 'node:test';
import { execFileSync } from 'node:child_process';
import { decodeSummary } from '../../workspace/js/summary-transport.js';
import { applySummaryDelta } from '../../workspace/js/summary-delta.js';

const encode = value => JSON.parse(execFileSync('.venv/bin/python', ['-c',
  'import sys,json;sys.path.insert(0,"workspace");from summary_transport import encode;sys.stdout.buffer.write(encode(json.load(sys.stdin)))'],
  { input: JSON.stringify(value), encoding: 'utf8' }));

test('Python transport restores all evidence, literal tags, nulls and missing fields in JavaScript', () => {
  const quote = 'Essential: A or B; C is not required. £700/day — résumé';
  const url = 'https://media.licdn.com/dms/image/company-logo?e=12&v=beta&t=a%2Fb%2Bc';
  const value = JSON.parse(JSON.stringify({
    version: '12345678-1', records: [{ id: 'A', quote, url, missing: null }, { id: 'B', quote, url }],
    literal: [{ $text: 0 }, { $object: [['a', quote]] }, [], {}, 0, false, null,
      '~0', '~~literal', '~', '~repeated source quote', '~repeated source quote'],
  }));
  value.literal.push(JSON.parse('{"__proto__":{"polluted":true}}'));
  const packed = encode(value), result = decodeSummary(packed);
  assert.deepEqual(result, value);
  assert.equal(packed.strings.filter(s => s === quote).length, 1);
  assert.equal({}.polluted, undefined);
  result.records[0].quote = 'local edit';
  assert.equal(result.records[1].quote, quote, 'sharing strings never aliases mutable records');
});

test('ordinary replies and deltas retain the existing protocol', () => {
  const summary = { version: '12345678-1', records: [{ id: 'A', notes: 'Quoted evidence retained' }],
    intelligence: { actions: [], research: [] } };
  assert.equal(decodeSummary(summary), summary);
  const none = { upsert: [], remove: [] };
  const delta = { delta: true, base: summary.version, version: '12345678-2', set: {}, unset: [],
    intelligence: { set: {}, unset: [] }, collections: { records: {
      upsert: [{ id: 'A', notes: 'Updated sourced evidence' }], remove: [] }, actions: none, research: none } };
  const after = applySummaryDelta(decodeSummary(encode(summary)), decodeSummary(delta));
  assert.equal(after.records[0].notes, 'Updated sourced evidence');
  assert.equal(after.version, delta.version);
});

test('invalid encoding fails explicitly so fetchSummary can recover through ordinary JSON', () => {
  const envelope = value => ({ encoding: 'strings-v1', strings: ['evidence'], value });
  for (const bad of [
    { ...envelope({}), encoding: 'strings-v2' }, { ...envelope({}), strings: [0] },
    { encoding: 'strings-v1', strings: [] },
    ...['~-1', '~1', '~.5', '~01', '~1e0', '~', '~word', '~9007199254740992'].map(envelope),
  ]) assert.throws(() => decodeSummary(bad), /Invalid summary encoding/);
});
