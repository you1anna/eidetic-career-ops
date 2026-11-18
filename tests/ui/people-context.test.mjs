import test from 'node:test';
import assert from 'node:assert/strict';
import { peopleContext, personAction } from '../../workspace/js/people-context.js';

const now = new Date('2026-11-06T12:00:00Z');
const person = (id, more = {}) => ({ id, type: 'person', name: id, ...more });
const job = (id, more = {}) => ({ id, type: 'job', name: id, relevance: { score: 50 }, ...more });
const link = (from_id, to_id, relation = 'related') => ({ from_id, to_id, relation });
const item = (id, progress, more = {}) => ({ id: `action:${id}:`, record_id: id, person_id: id, progress, ...more });

test('the action queue ranks replies, own commitments, oldest waiting, exact Act now routes, then priorities once per person', () => {
  const records = [person('friend', { priority: 'first', relationship: 'warm' }), person('reply'), person('mine'),
    person('old-wait'), person('new-wait'), person('role'), person('quiet'), job('J')];
  const state = { records, links: [link('role', 'J'), link('J', 'role')], opportunities: { act_now: [{ record_id: 'J' }] },
    intelligence: { actions: [item('reply', { status: 'replied' }), item('mine', { status: 'conversation', next_step: { kind: 'mine', text: 'Read their remit', agreed_on: '2026-10-01' } }),
      item('old-wait', { status: 'waiting', since: '2026-10-20' }), item('new-wait', { status: 'waiting', since: '2026-11-05' }),
      item('reply', { status: 'ready' }, { id: 'action:reply:other' })] } };
  const before = structuredClone(state), result = peopleContext(state, now);
  assert.deepEqual(result.queue.map(p => p.id), ['reply', 'mine', 'old-wait', 'new-wait', 'role', 'friend']);
  assert.equal(result.byId.get('old-wait').action.label, 'Waiting 17 days');
  assert.match(result.byId.get('old-wait').action.next, /^Contact recorded 20 Oct 2026\./);
  assert.match(result.byId.get('old-wait').action.next, /Waiting for a reply \(17 days\)/);
  assert.equal(result.byId.get('mine').action.key, 'next_step');
  assert.equal(result.byId.get('mine').action.agreedOn, '2026-10-01');
  assert.equal(result.byId.get('mine').action.next, 'Read their remit · agreed 1 Oct 2026');
  assert.equal(result.byId.get('role').roles.length, 1);
  assert.equal(result.byId.get('role').roles[0].actNow, true);
  assert.deepEqual(state, before);
});

test('current responsibility and completion override a generic reply; incoming unrecorded replies remain actionable', () => {
  const p = person('P', { priority: 'first' });
  assert.equal(personAction(p, { status: 'replied', next_step: { kind: 'waiting', text: 'Their role description' } }, now).key, 'waiting');
  assert.equal(personAction(p, { status: 'replied', next_step: { kind: 'mine', text: 'Send details' } }, now).key, 'next_step');
  assert.equal(personAction(p, { status: 'replied', next_step: { kind: 'done', text: 'Old task' } }, now).key, '');
  assert.equal(personAction(p, { status: 'waiting', reply_to_record: true, next: 'Record their reply.' }, now).next, 'Record their reply.');
  assert.equal(personAction(p, { status: 'no_response', reply_to_record: true }, now).key, 'needs_reply');
  assert.equal(personAction(p, { status: 'conversation' }, now).next, 'Agree and record the next step.');
  for (const status of ['held', 'closed', 'skipped', 'won', 'handed_over', 'no_response', 'research'])
    assert.equal(personAction(p, { status, next_step: { kind: 'mine', text: 'Stale task' } }, now).key, '', status);
});

test('waiting age uses factual valid London calendar dates, never an agreement date or future date', () => {
  const p = person('P');
  for (const since of ['invalid', '2026-02-30', '2026-11-07', '2026-11-06T23:00:00Z', '2026-11-06T01:00:00']) {
    const action = personAction(p, { status: 'waiting', since }, now);
    assert.equal(action.days, null, since); assert.equal(action.label, 'Waiting');
  }
  const spring = new Date('2026-03-30T00:05:00+01:00');
  assert.equal(personAction(p, { status: 'waiting', since: '2026-03-28' }, spring).days, 2);
  assert.equal(personAction(p, { status: 'waiting', since: '2026-03-29T00:30:00Z' }, spring).days, 1);
  assert.equal(personAction(p, { status: 'replied', next_step: { kind: 'waiting', text: 'Their task', agreed_on: '2020-01-01' } }, now).days, null);
});

test('completed and terminal approaches cannot be replaced by generic priority through a second empty approach', () => {
  for (const progress of [{ status: 'held' }, { status: 'replied', next_step: { kind: 'done' } }]) {
    const result = peopleContext({ records: [person('P', { priority: 'first' })], intelligence: {
      actions: [item('P', { status: 'ready' }, { id: 'a' }), item('P', progress, { id: 'z' })],
    } }, now);
    assert.deepEqual(result.queue, []);
    assert.equal(result.byId.get('P').item.id, 'z');
  }
});

test('role routes retain canonical direct and historical bases without guessing employment or hiring authority', () => {
  const company = { id: 'C', type: 'company', name: 'Employer' };
  const records = [company, job('J'), person('direct'), person('employee'), person('former'), person('reverse'),
    person('name-only', { organisation: 'Employer' }), person('blocked', { relationship: 'do_not_contact' })];
  const state = { records, links: [link('J', 'C', 'hiring_for'), link('direct', 'J'), link('employee', 'C', 'works_at'),
    link('employee', 'J'), link('former', 'C', 'formerly_at'), link('C', 'reverse', 'introducer'), link('blocked', 'J')] };
  const result = peopleContext(state, now);
  assert.deepEqual(['direct', 'employee', 'former'].map(id => result.byId.get(id).roles.length), [1, 1, 1]);
  assert.deepEqual(result.byId.get('former').roles[0].bases, ['Formerly at Employer']);
  assert.equal(result.byId.get('employee').roles[0].direct, true);
  for (const id of ['reverse', 'name-only', 'blocked']) assert.deepEqual(result.byId.get(id).roles, []);
  assert.deepEqual(result.queue, []);
});

test('excluded people and closed, duplicate or excluded roles cannot enter the queue through Act now', () => {
  const safe = person('safe');
  const variants = [job('closed', { status: 'closed' }), job('duplicate', { relevance: { score: 50, duplicate_of: 'original' } }),
    job('excluded', { exclusion_contexts: ['C'] }), job('archived', { archived_at: '2026-11-01' }),
    job('source-closed', { availability: { status: 'closed' } }), job('expired', { source_lifecycle: 'expired' }),
    job('past-deadline', { published_deadline: '2026-11-05' }), job('check-closed', { evidence_checks: { vacancy: { status: 'closed' } } })];
  const state = { records: [safe, person('excluded-person', { priority: 'first', exclusion_contexts: ['C'] }),
    person('demoted', { priority: 'first', relationship: 'do_not_contact' }), person('blocked-tier', { priority: 'first', relevance: { reach: { tier: 'blocked' } } }), ...variants],
    links: variants.map(j => link('safe', j.id)), opportunities: { act_now: variants.map(j => ({ record_id: j.id })) } };
  const result = peopleContext(state, now);
  assert.deepEqual(result.queue, []); assert.deepEqual(result.byId.get('safe').roles, []);
  const chosen = job('chosen', { relevance: { score: 20, band: 'low' } });
  const manual = peopleContext({ records: [safe, chosen], links: [link('safe', 'chosen')], opportunities: { act_now: [{ record_id: 'chosen' }] } }, now);
  assert.equal(manual.byId.get('safe').roles[0].job.id, 'chosen', 'a manually selected role preserves its explicit Act now context');
});

test('many roles at one employer consume each source link only once and retain every actual route', () => {
  const records = [{ id: 'C', type: 'company', name: 'Employer' }, person('P')];
  const links = [link('P', 'C', 'works_at')];
  for (let n = 0; n < 3000; n++) { records.push(job(`J${n}`)); links.push(link(`J${n}`, 'C', 'hiring_for')); }
  let visits = 0;
  const countedLinks = links.map(row => new Proxy(row, { get(target, key) { if (key === 'from_id') visits++; return target[key]; } }));
  const started = performance.now(), result = peopleContext({ records, links: countedLinks }, now);
  assert.equal(result.byId.get('P').roles.length, 3000);
  assert.ok(visits <= links.length * 8, `${visits} link reads should grow with edges, not jobs × edges`);
  console.log(`People roles: 3000 jobs, 3000 routes, ${visits} link reads in ${(performance.now() - started).toFixed(1)} ms`);
});
