import test from 'node:test';
import assert from 'node:assert/strict';
import { createNavigation } from '../../workspace/js/navigation.js';

function setup() {
  const records = [{ id: 'person/a', type: 'person' }, { id: 'job-b', type: 'job' }];
  const decisions = [{ id: 7, record_id: 'person/a' }, { id: 8, record_id: null }];
  const effects = [];
  const location = { hash: '#week' };
  let open = false, discard = false;
  const nav = createNavigation({
    getRecord: id => records.find(r => r.id === id), getDecisions: () => decisions,
    sheet: {
      isOpen: () => open,
      close: () => { open = false; effects.push(['close']); },
      showRecord: r => { open = true; nav.setDirty(false); effects.push(['record', r.id]); },
      showDecision: (d, r) => { open = true; nav.setDirty(false); effects.push(['decision', d.id, r?.id]); },
      showLead: id => { open = true; effects.push(['lead', id]); },
    },
    render: () => effects.push(['render']), onTabChange: () => effects.push(['tab']),
    seen: view => effects.push(['seen', view]), location,
    history: {
      pushState: (_, __, hash) => { location.hash = hash; effects.push(['push', hash]); },
      replaceState: (_, __, hash) => { location.hash = hash; effects.push(['replace', hash]); },
    },
    confirmDiscard: () => { effects.push(['confirm']); return discard; },
  });
  return { nav, location, effects, decisions, isOpen: () => open,
    allowDiscard: () => { discard = true; } };
}

test('record/action routes round-trip encoded identities and retain linked-record context', () => {
  const { nav, location, effects } = setup();
  nav.openRecord('person/a', true, 'ask/one');
  assert.equal(location.hash, '#week/person%2Fa/action/ask%2Fone');
  nav.routeRecord();
  assert.equal(nav.recordId, 'person/a');
  assert.equal(nav.actionId, 'ask/one');
  assert.equal(nav.backTarget, undefined, 'same-record hashchange cannot duplicate the stack');
  nav.openRecord('job-b');
  assert.deepEqual(nav.backTarget, ['person/a', 'ask/one']);
  const [id, action] = nav.backTarget;
  nav.popBack();
  nav.openRecord(id, true, action, true);
  assert.equal(nav.actionId, 'ask/one');
  assert.equal(nav.backTarget, undefined);
  assert.deepEqual(effects.filter(e => e[0] === 'seen'), [['seen', 'week/record']]);
});

test('lead Review links open independently of canonical records and close cleanly', () => {
  const { nav, location, effects, isOpen } = setup();
  const id = 'a'.repeat(64);
  location.hash = '#dashboard/lead/' + id;
  nav.routeRecord();
  assert.equal(nav.tab, 'dashboard');
  assert.equal(nav.leadId, id);
  assert.deepEqual(effects.filter(e => e[0] === 'lead'), [['lead', id]]);
  nav.closeDetail();
  assert.equal(isOpen(), false);
  assert.equal(nav.leadId, null);
});

test('cancelled browser Back preserves the decision, route and unsaved form without rendering', () => {
  const { nav, location, decisions, effects } = setup();
  nav.openDecision(decisions[0]);
  nav.setDirty(true);
  effects.length = 0;
  location.hash = '#week';
  nav.routeRecord();
  assert.equal(location.hash, '#week/decision/7');
  assert.equal(nav.decisionId, 7);
  assert.equal(nav.recordId, 'person/a');
  assert.equal(nav.dirty, true);
  assert.deepEqual(effects, [['confirm'], ['push', '#week/decision/7']]);
});

test('accepted discard closes the sheet and clears every selected identity with only one prompt', () => {
  const { nav, location, effects, allowDiscard, isOpen } = setup();
  nav.openRecord('person/a', true, 'ask/one');
  nav.setDirty(true);
  allowDiscard();
  location.hash = '#people';
  nav.routeRecord();
  assert.equal(nav.tab, 'people');
  assert.equal(isOpen(), false);
  assert.equal(nav.dirty, false);
  for (const key of ['recordId', 'decisionId', 'actionId', 'route']) assert.equal(nav[key], null);
  assert.equal(effects.filter(e => e[0] === 'confirm').length, 1);
});

test('decision selection keeps same-record action context and clears it for workspace decisions', () => {
  const { nav, decisions } = setup();
  nav.openRecord('person/a', true, 'ask/one');
  nav.openDecision(decisions[0]);
  assert.equal(nav.actionId, 'ask/one');
  nav.openDecision(decisions[1]);
  assert.equal(nav.recordId, null);
  assert.equal(nav.actionId, null);
  assert.equal(nav.decisionId, 8);
  assert.equal(nav.route, '#week/decision/8');
});

test('retired Pipeline and Research links preserve record/action context and route people to People', () => {
  const { nav, location, effects } = setup();
  for (const old of ['pipeline', 'outreach', 'templates', 'companies', 'research']) {
    location.hash = `#${old}/person%2Fa/action/ask%2Fone`; nav.routeRecord();
    assert.equal(nav.tab, 'people'); assert.equal(location.hash, '#people/person%2Fa/action/ask%2Fone');
    assert.equal(nav.actionId, 'ask/one');
    location.hash = `#${old}/job-b`; nav.routeRecord();
    assert.equal(nav.tab, 'work'); assert.equal(location.hash, '#work/job-b');
  }
  assert.ok(effects.some(e => e[0] === 'replace'));
});

test('blocked tab change, explicit close and native Escape all preserve unsaved state', () => {
  const { nav, location, isOpen } = setup();
  nav.openRecord('person/a');
  nav.setDirty(true);
  nav.go('work');
  nav.closeDetail();
  assert.equal(nav.cancelDetail(), false);
  assert.equal(nav.tab, 'week');
  assert.equal(nav.recordId, 'person/a');
  assert.equal(location.hash, '#week/person%2Fa');
  assert.equal(isOpen(), true);
  assert.equal(nav.dirty, true);
});

test('explicit close clears linked-record history and missing records do not replace selection', () => {
  const { nav, location, isOpen } = setup();
  nav.openRecord('person/a');
  nav.openRecord('job-b');
  nav.openRecord('missing');
  assert.equal(nav.recordId, 'job-b');
  nav.closeDetail();
  assert.equal(nav.backTarget, undefined);
  assert.equal(nav.recordId, null);
  assert.equal(location.hash, 'week');
  assert.equal(isOpen(), false);
});

test('focused-field navigation can update history without dispatching a second render', () => {
  const { nav, location, effects } = setup();
  nav.openRecord('person/a', false, 'ask/one');
  assert.equal(location.hash, '#week');
  nav.keepRoute();
  assert.equal(location.hash, '#week/person%2Fa/action/ask%2Fone');
  assert.equal(effects.filter(e => e[0] === 'record').length, 1);
  assert.equal(effects.filter(e => e[0] === 'render').length, 0);
});

test('People role links open Jobs and preserve the person and original tab on return', () => {
  const { nav, location } = setup();
  nav.go('people');
  nav.openRecord('person/a', true, 'ask/one');
  nav.openJob('job-b');
  assert.equal(nav.tab, 'work');
  assert.equal(location.hash, '#work/job-b');
  const [id, action] = nav.backTarget;
  assert.equal(id, 'person/a');
  nav.popBack();
  nav.openRecord(id, true, action, true);
  assert.equal(nav.tab, 'people');
  assert.equal(location.hash, '#people/person%2Fa/action/ask%2Fone');
});

test('a cancelled role link preserves dirty People state and an invalid target does nothing', () => {
  const { nav, location, effects } = setup();
  nav.go('people'); nav.openRecord('person/a'); nav.setDirty(true);
  effects.length = 0;
  nav.openJob('job-b');
  assert.equal(nav.tab, 'people');
  assert.equal(location.hash, '#people/person%2Fa');
  assert.deepEqual(effects, [['confirm']]);
  nav.setDirty(false); effects.length = 0;
  nav.openJob('person/a');
  assert.deepEqual(effects, []);
});

test('linked-record Back guards dirty input before changing the tab, background or stack', () => {
  const {nav, location, effects, allowDiscard} = setup();
  nav.go('people'); nav.openRecord('person/a',true,'ask/one'); nav.openJob('job-b');
  nav.setDirty(true); effects.length=0;
  nav.goBack();
  assert.equal(nav.tab,'work'); assert.equal(location.hash,'#work/job-b');
  assert.equal(nav.recordId,'job-b'); assert.equal(nav.backTarget[0],'person/a');
  assert.equal(nav.dirty,true); assert.deepEqual(effects,[['confirm']]);
  allowDiscard(); effects.length=0; nav.goBack();
  assert.equal(nav.tab,'people'); assert.equal(location.hash,'#people/person%2Fa/action/ask%2Fone');
  assert.equal(nav.actionId,'ask/one'); assert.equal(nav.backTarget,undefined);
  assert.equal(effects.filter(([kind])=>kind==='confirm').length,1);
});


test('weekly section destinations survive hash routing, reload and browser Back', () => {
  const { nav, location, effects } = setup();
  nav.go('dashboard');
  nav.openWeek('waiting');
  assert.equal(location.hash, '#week/section/waiting');
  assert.equal(nav.weekGroup, 'waiting');
  nav.routeRecord();
  assert.equal(nav.weekGroup, 'waiting', 'hashchange must retain the section being revealed');
  nav.go('dashboard');
  location.hash = '#week/section/waiting'; nav.routeRecord();
  assert.equal(nav.tab, 'week'); assert.equal(nav.weekGroup, 'waiting');
  assert.equal(effects.filter(e => e[0] === 'record').length, 0);
  nav.openWeek('in-progress'); nav.routeRecord();
  assert.equal(nav.weekGroup, 'in-progress');
  nav.go('week'); nav.routeRecord();
  assert.equal(nav.weekGroup, null, 'ordinary This week returns to the full overview');
  nav.openWeek('unknown'); assert.equal(nav.weekGroup, null);
});

test('weekly section links respect unsaved changes', () => {
  const { nav, location, effects } = setup();
  nav.openRecord('person/a'); nav.setDirty(true); effects.length = 0;
  nav.openWeek('waiting');
  assert.equal(location.hash, '#week/person%2Fa');
  assert.equal(nav.weekGroup, null);
  assert.deepEqual(effects, [['confirm']]);
});
