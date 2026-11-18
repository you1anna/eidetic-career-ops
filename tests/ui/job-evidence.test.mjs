// Pure evidence-boundary tests: no browser, external data or local database.
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { availabilityStatus, checkStatus, contactRouteIndex, createContactRouteLookup, contactRoutes, relevantAgencyRole, confirmedOpenAgencyRole, routeLine, refreshCheckLabels } from '../../workspace/js/job-evidence.js';

const now = Date.parse('2026-10-17T12:00:00Z');
const check = (changes = {}) => ({ scope: 'vacancy', status: 'confirmed',
  checked_at: '2026-10-16T12:00:00Z', source_url: 'https://example.test/vacancy',
  basis: 'The current advert accepts applications.', ...changes });

test('agency recommendations require a dated current open answer, with canonical availability overriding legacy checks', () => {
  const job={id:'J',type:'job',relevance:{score:50},evidence_checks:{vacancy:check({checked_at:'2026-10-17T11:00:00Z'})}};
  assert.equal(confirmedOpenAgencyRole(job,now),true,'older payloads retain explicit source-check support');
  assert.equal(confirmedOpenAgencyRole({...job,evidence_checks:{}},now),false);
  const availability={status:'open',basis:'research',at:'2026-10-17T11:00:00Z',until:'2026-10-19T11:00:00Z'};
  assert.equal(confirmedOpenAgencyRole({...job,availability},now),true);
  assert.equal(confirmedOpenAgencyRole({...job,availability:{...availability,basis:'listing',source:'feed:reed'}},now),false,
    'a feed sighting is not an explicit check that the role remains open');
  for(const changed of [{status:'unconfirmed'},{status:'closed'},{until:'2026-10-17T12:00:00Z'},
    {at:''},{at:'2026-10-18T11:00:00Z'},{until:''}])
    assert.equal(confirmedOpenAgencyRole({...job,availability:{...availability,...changed}},now),false,JSON.stringify(changed));
  assert.equal(confirmedOpenAgencyRole({...job,availability,source_lifecycle:'closed'},now),false);
  assert.equal(confirmedOpenAgencyRole({...job,evidence_checks:{vacancy:check()}},now),false,'stale legacy check is not an open role');
});

test('explicitly selected roles can bypass discovery fit but retain the same closure and exclusion boundary', () => {
  const job = { id: 'J', type: 'job', relevance: { score: 20, band: 'low', qualification: { eligible: false } } };
  assert.equal(relevantAgencyRole(job, now), false);
  assert.equal(relevantAgencyRole(job, now, { selected: true }), true);
  for (const changed of [
    { archived_at: '2026-10-16' }, { exclusion: { reason: 'Excluded' } }, { exclusion_contexts: ['C'] },
    { status: 'closed' }, { status: 'rejected' }, { status: 'skip' }, { status: 'lost' }, { status: 'declined' },
    { source_lifecycle: 'closed' }, { source_lifecycle: 'expired' }, { source_lifecycle: 'removed' }, { source_lifecycle: 'historical' },
    { availability: { status: 'closed' } }, { evidence_checks: { vacancy: { status: 'closed' } } },
    { published_deadline: '2026-10-16' }, { relevance: { score: 50, duplicate_of: 'other' } },
  ]) assert.equal(relevantAgencyRole({ ...job, ...changed }, now, { selected: true }), false, JSON.stringify(changed));
  assert.equal(relevantAgencyRole({ ...job, published_deadline: '2026-10-17' }, now, { selected: true }), true);
});

test('24 hours is an exact expiry boundary, including equivalent time zones', () => {
  assert.equal(checkStatus(check(), 'vacancy', now - 1).fresh, true);
  assert.equal(checkStatus(check(), 'vacancy', now).fresh, false);
  assert.equal(checkStatus(check(), 'vacancy', now + 1).tone, 'stale');
  assert.equal(checkStatus(check({ checked_at: '2026-10-16T13:00:00+01:00' }), 'vacancy', now).tone, 'stale');
});

test('failed reads, closure and missing explicit evidence cannot be fresh', () => {
  for (const evidence of [null, {}, { reviewed_at: new Date(now).toISOString() },
    check({ checked_at: '2026-10-17' }), check({ checked_at: '2026-10-17T11:00:00' }),
    check({ checked_at: '2026-10-17T12:05:00.001Z' }),
    check({ checked_at: '2026-10-17T11:00:00Z', status: 'unverified' }),
    check({ checked_at: '2026-10-17T11:00:00Z', status: 'closed' })])
    assert.equal(checkStatus(evidence, 'vacancy', now).fresh, false);
  assert.equal(checkStatus(check({ status: 'closed' }), 'contact_route', now).fresh, false);
});

test('unknown availability names only the actual attempt, never a record edit or raw failure', () => {
  const base = {availability:{status:'unconfirmed',basis:null,at:null},date:'2026-10-17',updated_at:'2026-10-17T12:00:00Z'};
  assert.equal(availabilityStatus(base,now).label, 'Open unknown · not checked yet');
  assert.equal(availabilityStatus({...base,evidence_checks:{vacancy:check({status:'unverified',checked_at:'2026-10-17T10:00:00Z',basis:'robots_disallowed'})}},now).label,
    'Open unknown · last check 2h ago');
  for (const checked_at of ['invalid','2026-10-17','2026-10-18T10:00:00Z'])
    assert.equal(availabilityStatus({...base,evidence_checks:{vacancy:check({checked_at})}},now).label, 'Open unknown · not checked yet');
});

test('configured closed source lifecycles override a fresh confirmation without changing its evidence', () => {
  const config = readFileSync(new URL('../../config/relevance.yml', import.meta.url), 'utf8');
  const gone = JSON.parse(config.match(/^\s*gone_lifecycles:\s*(\[[^\n]+\])/m)[1]);
  const evidence = check({ checked_at: '2026-10-17T11:00:00Z' });
  const original = JSON.stringify(evidence);
  for (const source_lifecycle of gone) {
    const result = checkStatus(evidence, 'vacancy', now, { source_lifecycle });
    assert.equal(result.fresh, false, source_lifecycle);
    assert.match(result.label, /needs recheck.*source/i);
  }
  assert.equal(checkStatus(evidence, 'vacancy', now, { source_lifecycle: 'active' }).fresh, true);
  assert.equal(checkStatus(evidence, 'contact_route', now, { source_lifecycle: 'closed' }).fresh, true);
  assert.equal(JSON.stringify(evidence), original);
});

test('date-only deadlines expire at the next London midnight, not their start or UTC midnight', () => {
  const evidence = check({ checked_at: '2026-10-17T21:00:00Z' });
  const job = { published_deadline: '2026-10-17' };
  assert.equal(checkStatus(evidence, 'vacancy', Date.parse('2026-10-17T22:59:59.999Z'), job).fresh, true);
  const expired = checkStatus(evidence, 'vacancy', Date.parse('2026-10-17T23:00:00Z'), job);
  assert.equal(expired.fresh, false);
  assert.match(expired.label, /needs recheck.*closing date.*passed/i);
  assert.equal(checkStatus(evidence, 'contact_route', Date.parse('2026-10-17T23:00:00Z'), job).fresh, true);
  assert.equal(checkStatus(evidence, 'vacancy', Date.parse('2026-10-17T23:00:00Z'),
    { published_deadline: '2026-10-18' }).fresh, true);
  assert.equal(checkStatus(evidence, 'vacancy', Date.parse('2026-10-17T23:00:00Z'),
    { published_deadline: '2026-10-17T18:00:00+01:00' }).fresh, false);
});

test('advert labels follow the server availability and name the listing source', () => {
  const at = '2026-10-17T06:00:00Z', until = '2026-10-19T06:00:00Z';
  const label = (availability, job = {}) => availabilityStatus({ ...job, availability }, now);
  assert.deepEqual(label({ status: 'open', basis: 'listing', at, until, source: 'feed:adzuna' }),
    { label: 'Still listed on Adzuna · seen 6h ago', tone: 'fresh', fresh: true });
  assert.match(label({ status: 'open', basis: 'listing', at, until, source: 'board:ashby' }).label, /employer's Ashby job board/);
  assert.match(label({ status: 'open', basis: 'listing', at, until, source: 'board:smartrecruiters' }).label, /employer's SmartRecruiters job board/);
  assert.equal(label({ status: 'open', basis: 'research', at, until, source: null }).label, 'Advert open · checked 6h ago');
  assert.equal(label({ status: 'unconfirmed', basis: 'listing', at: '2026-10-10T06:00:00Z', source: 'feed:reed' }).label, 'Not listed by Reed since 10 Oct');
  assert.equal(label({ status: 'unconfirmed', basis: null, at: null, source: null }).fresh, false);
  assert.match(label({ status: 'closed', basis: 'deadline' }, { published_deadline: '2026-10-16' }).label, /closing date 2026-10-16 has passed/);
  assert.match(label({ status: 'closed', basis: 'listing' }, { source_lifecycle: 'expired' }).label, /source reports expired/);
  for (const status of ['unconfirmed', 'closed']) assert.equal(label({ status, basis: 'listing', at, source: 'feed:adzuna' }).fresh, false);
});

test('an open page ages the server answer at its until time and at the London closing date', () => {
  const open = { status: 'open', basis: 'listing', at: '2026-10-15T12:00:00Z', until: '2026-10-17T12:00:00Z', source: 'feed:adzuna' };
  assert.equal(availabilityStatus({ availability: { ...open, until: undefined } }, Date.parse('2026-10-15T13:00:00Z')).fresh, false,
    'an open answer without an expiry is not shown as open');
  assert.equal(availabilityStatus({ availability: open }, Date.parse('2026-10-17T11:59:59Z')).fresh, true);
  assert.equal(availabilityStatus({ availability: open }, Date.parse('2026-10-17T12:00:00Z')).label, 'Not listed by Adzuna since 15 Oct');
  const today = { availability: { ...open, at: '2026-10-17T06:00:00Z', until: '2026-10-19T06:00:00Z' }, published_deadline: '2026-10-17' };
  assert.equal(availabilityStatus(today, Date.parse('2026-10-17T22:59:59Z')).fresh, true);
  assert.match(availabilityStatus(today, Date.parse('2026-10-17T23:00:00Z')).label, /closing date 2026-10-17 has passed/);
});

test('contact routes deduplicate direct and employer links, exclude unsafe directions and opt-outs', () => {
  const job = { id: 'JOB-1', type: 'job' };
  const org = { id: 'CO-1', type: 'company', name: 'Example Employer' };
  const person = (id, extra = {}) => ({ id, type: 'person', name: id, ...extra });
  const state = { records: [job, org, person('CP-direct'), person('CP-history'), person('CP-reversed'),
    person('CP-dnc', { relationship: 'do_not_contact' }), person('CP-archive', { archived_at: '2020-01-01' }),
    person('CP-excluded', { exclusion: { reason: 'Excluded' } })], links: [
    ['JOB-1', 'CO-1', 'hiring_for'], ['JOB-1', 'CP-direct', 'related'], ['CP-direct', 'CO-1', 'works_at'],
    ['CP-history', 'CO-1', 'formerly_at'], ['CO-1', 'CP-reversed', 'introducer'],
    ['JOB-1', 'CP-dnc', 'related'], ['JOB-1', 'CP-archive', 'related'], ['JOB-1', 'CP-excluded', 'related'],
  ].map(([from_id, to_id, relation]) => ({ from_id, to_id, relation })) };
  const routes = contactRoutes(job, state);
  assert.deepEqual(routes.map(r => r.person.id), ['CP-direct', 'CP-history']);
  assert.equal(routes[0].bases.size, 2);
  assert.ok([...routes[1].bases].some(b => /Formerly/.test(b)));
});

test('a check at an unrelated employer cannot freshen this contact route', () => {
  const person = { id: 'CP-1', type: 'person', name: 'Example Contact' };
  const job = { id: 'JOB-1', type: 'job', evidence_checks: { contacts: [check({ scope: 'contact_route',
    person_id: person.id, organisation_id: 'CO-other', checked_at: '2026-10-17T11:00:00Z' })] } };
  const org = { id: 'CO-1', type: 'company', name: 'Example Employer' };
  const state = { records: [job, org, person], links: [
    { from_id: job.id, to_id: org.id, relation: 'hiring_for' },
    { from_id: person.id, to_id: org.id, relation: 'works_at' },
  ] };
  const [route] = contactRoutes(job, state);
  assert.ok(route);
  assert.equal(checkStatus(route.check, 'contact_route', now).fresh, false);
});

test('the reusable route index excludes sibling vacancies from organisation-to-person adjacency', () => {
  const employer = { id: 'C', type: 'company', name: 'Employer' }, person = { id: 'P', type: 'person', name: 'Person' };
  const jobs = Array.from({ length: 20 }, (_, n) => ({ id: `J${n}`, type: 'job' }));
  const state = { records: [employer, person, ...jobs], links: [
    { from_id: 'P', to_id: 'C', relation: 'works_at' },
    ...jobs.map(job => ({ from_id: job.id, to_id: 'C', relation: 'hiring_for' })),
  ] };
  const before = structuredClone(state), index = contactRouteIndex(state);
  assert.equal(index.peopleByOrganisation.get('C').length, 1);
  assert.equal(index.linksByRecord.get('C').length, 21);
  assert.deepEqual(contactRoutes(jobs[0], state, index).map(route => route.person.id), ['P']);
  assert.deepEqual(contactRoutes(jobs[1], state, index).map(route => route.person.id), ['P']);
  assert.deepEqual(state, before);
});


test('private route provenance survives refresh and never receives public-check expiry', () => {
  const previous = { document:globalThis.document, now:Date.now };
  globalThis.document = { createElement:() => ({dataset:{}}) }; Date.now = () => now;
  try {
    for (const [provenance,source_url,expected] of [
      ['owner_confirmed','https://example.test/team','The owner-confirmed 2026-10-16 · not a fresh public check'],
      ['prior_correspondence','https://example.test/team','Previously used route (2026-10-16) · current reachability unknown'],
      [undefined,'https://mail.google.com/mail/u/0/#all/synthetic','Confirmed from your records 2026-10-16 · not a fresh public check'],
    ]) {
      const evidence = check({scope:'contact_route',provenance,source_url,organisation_id:'CO-1'});
      const before = structuredClone(evidence);
      const {node,scope} = routeLine(evidence,new Map([['CO-1','Example Employer']]));
      assert.equal(scope,'Via Example Employer'); assert.equal(node.textContent,expected);
      refreshCheckLabels({querySelectorAll: selector => selector === '.evidence-check' ? [node] : []});
      assert.equal(node.textContent,expected); assert.equal(node.dataset.fresh,'false');
      assert.deepEqual(evidence,before);
    }
    const {node} = routeLine(check({scope:'contact_route',provenance:'public_read',checked_at:'2026-10-17T11:00:00Z'}));
    assert.equal(node.dataset.fresh,'true'); Date.now = () => now+24*3600000;
    refreshCheckLabels({querySelectorAll: selector => selector === '.evidence-check' ? [node] : []});
    assert.equal(node.dataset.fresh,'false'); assert.match(node.textContent,/Contact route needs recheck/);
  } finally { globalThis.document = previous.document; Date.now = previous.now; }
});

test('an empty role can use only a sourced latest employer route through its explicit hiring_for link', async () => {
  const { employerContactRoute } = await import('../../workspace/js/job-evidence.js');
  const job = {id:'J',type:'job'}, employer = {id:'C',type:'company',name:'Synthetic Employer',research:[
    {reviewed_at:'2026-10-16T11:00:00Z',verification:'primary-read',public_contact_route:'People team: people@example.test',source_urls:['https://example.test/careers']},
    {reviewed_at:'2026-10-15T11:00:00Z',verification:'primary-read',public_contact_route:'Old route',source_urls:['https://example.test/old']},
  ]};
  const state = {records:[job,employer],links:[{from_id:'J',to_id:'C',relation:'hiring_for'}]}, before = structuredClone(state);
  const route = employerContactRoute(job,state,{now});
  assert.equal(route.company.id,'C'); assert.equal(route.text,'People team: people@example.test');
  assert.equal(route.reviewedAt,'2026-10-16T11:00:00Z'); assert.equal(route.sourceUrl,'https://example.test/careers');
  assert.deepEqual(state,before);
  for (const change of [
    {links:[]}, {links:[{from_id:'J',to_id:'C',relation:'related'}]},
    {links:[{from_id:'unrelated-job',to_id:'C',relation:'hiring_for'}]},
    {records:[job,{...employer,type:'agency'}]}, {records:[job,{...employer,archived_at:'2026-10-16'}]},
    {records:[job,{...employer,exclusion:{reason:'Excluded'}}]}, {records:[job,{...employer,exclusion_contexts:['C']}]},
  ]) assert.equal(employerContactRoute(job,{...state,...change},{now}),null,JSON.stringify(change));
  assert.equal(employerContactRoute(job,state,{now,isExcluded:()=>true}),null);
  for (const latest of [
    {public_contact_route:''},{public_contact_route:'   '},{verification:'search-verified'},
    {verification:'unconfirmed'},{source_urls:[]},{source_urls:['javascript:alert(1)']},
    {source_urls:['https://user:secret@example.test/careers']},{source_urls:['https://mail.google.com/mail/u/0/#all/synthetic']},
    {reviewed_at:'2026-10-18T11:00:00Z'},{reviewed_at:'2026-02-30T11:00:00Z'},
  ]) {
    const changed={...employer,research:[{...employer.research[0],...latest},employer.research[1]]};
    assert.equal(employerContactRoute(job,{...state,records:[job,changed]},{now}),null,JSON.stringify(latest));
  }
  const unrelated={reviewed_at:'2026-10-17T11:00:00Z',verification:'primary-read',summary:'Unrelated update'};
  assert.equal(employerContactRoute(job,{...state,records:[job,{...employer,research:[unrelated,...employer.research.slice().reverse()]}]},{now}).text,'People team: people@example.test');
});

test('the canonical employer route survives unrelated reviews and authoritative blank/unverified findings never fall back to preview', async () => {
  const {employerContactRoute} = await import('../../workspace/js/job-evidence.js');
  const job={id:'J',type:'job'}, provenance={reviewed_at:'2026-10-16T11:00:00Z',verification:'primary-read',source_urls:['https://example.test/original']};
  const company={id:'C',type:'company',name:'Employer',briefing:{public_contact_route:'General People team: people@example.test',provenance:{public_contact_route:provenance}},research:[
    {reviewed_at:'2026-10-17T11:00:00Z',verification:'primary-read',summary:'Newer unrelated context'},
    {...provenance,public_contact_route:'Obsolete preview route',source_urls:['https://example.test/wrong']},
  ]};
  const state={records:[job,company],links:[{from_id:'J',to_id:'C',relation:'hiring_for'}]};
  const result=employerContactRoute(job,state,{now});
  assert.equal(result.text,'General People team: people@example.test');
  assert.equal(result.reviewedAt,'2026-10-16T11:00:00Z');assert.equal(result.sourceUrl,'https://example.test/original');
  for(const briefing of [
    {public_contact_route:'',provenance:{public_contact_route:provenance}},
    {public_contact_route:'Unverified replacement',provenance:{public_contact_route:{...provenance,verification:'unconfirmed'}}},
    {public_contact_route:'Undated replacement',provenance:{public_contact_route:null}},
  ]) assert.equal(employerContactRoute(job,{...state,records:[job,{...company,briefing}]},{now}),null);
});

test('snapshot contact lookup visits source edges once across job rows and refreshes replaced inputs', () => {
  const company = {id:'C',name:'Employer',type:'company'}, person = {id:'P',name:'Pat',type:'person'};
  const jobs = Array.from({length:1000}, (_,i) => ({id:`J${i}`,name:`Job ${i}`,type:'job'}));
  let visits = 0;
  const links = [{from_id:'P',to_id:'C',relation:'works_at'},
    ...jobs.map(job => ({from_id:job.id,to_id:'C',relation:'hiring_for'}))]
    .map(link => new Proxy(link, {get(target,key) { if(key==='from_id') visits++; return target[key]; }}));
  let state = {records:[company,person,...jobs],links};
  const lookup = createContactRouteLookup({getState:()=>state});
  for (const job of jobs) {
    const routes = lookup.forRecord(job);
    assert.deepEqual(routes.map(route=>route.person.id), ['P']);
    assert.deepEqual([...routes[0].bases], ['Recorded at Employer']);
  }
  assert.ok(visits <= links.length * 2, `${visits} source edge reads for ${links.length} links`);
  const originalIndex=lookup.index();
  Object.assign(state,{research_processing:{status:'running'}});
  assert.equal(lookup.index(),originalIndex,'status-only refresh does not rebuild unchanged relationships');
  state={...state,records:state.records.map(record=>record.id==='P'?{...record,relationship:'do_not_contact'}:record)};
  assert.deepEqual(lookup.forRecord(jobs[0]),[],'a new summary cannot retain a demoted route');
  state.records=state.records.map(record=>record.id==='P'?{...record,relationship:'warm',name:'Updated Pat'}:record);
  assert.equal(lookup.forRecord(jobs[0])[0].person.name,'Updated Pat','replaced record arrays invalidate within an envelope');
  state.links=[{from_id:'P',to_id:'J0',relation:'introducer',notes:'New route'}];
  assert.deepEqual([...lookup.forRecord(jobs[0])[0].bases],['Recorded introducer']);
  assert.deepEqual([...lookup.forRecord(jobs[0])[0].notes],['New route']);
  assert.deepEqual(lookup.forRecord(jobs[1]),[],'removed links do not survive in the index');
});
