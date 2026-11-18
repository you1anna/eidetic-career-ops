import test from 'node:test';
import assert from 'node:assert/strict';
import { PEOPLE_GROUPS, groupPerson, recruitmentContext } from '../../workspace/js/people.js';
import { createOutreachViews } from '../../workspace/js/outreach.js';

const now = new Date('2026-10-30T12:00:00Z');
const review = (overrides = {}) => ({ reviewed_at: '2026-10-30', verification: 'primary-read',
  desk_summary: 'Engineering leadership and AI product recruitment.',
  public_contact_route: 'https://example.com/contact', source_urls: ['https://example.com/team'], ...overrides });
const agency = (id, overrides = {}) => ({ id, name: id, type: 'agency', kind: 'agency', research: [review()], ...overrides });
const openAvailability = () => ({status:'open',basis:'research',at:'2026-10-30T11:00:00Z',until:'2026-11-01T11:00:00Z'});
const withOpenRoles = records => ({records:[...records,...records.filter(r=>r.type==='agency').map(r=>({
  id:`job-${r.id}`,type:'job',name:'Engineering Manager',relevance:{score:50},availability:openAvailability()}))],
  links:records.filter(r=>r.type==='agency').map(r=>({from_id:`job-${r.id}`,to_id:r.id,relation:'advertised_by'}))});

test('recruiter filter reuses explicit kinds and agency consultant links without inferring a relationship', () => {
  const state = { records: [agency('A'), agency('N', {kind:'network'}),
    {id:'warm',type:'person',kind:'recruiter',priority:'first',relationship:'warm'},
    {id:'linked',type:'person'}, {id:'internal',type:'person',kind:'in_house_talent'},
    {id:'community',type:'person'}, {id:'other',type:'person'},
    {id:'blocked',type:'person',kind:'recruiter',relationship:'do_not_contact'}],
    links: [{from_id:'linked',to_id:'A',relation:'consultant_at'},
      {from_id:'A',to_id:'linked',relation:'consultant_at'},
      {from_id:'community',to_id:'N',relation:'consultant_at'},
      {from_id:'other',to_id:'A',relation:'related'}] };
  const before = structuredClone(state), result = recruitmentContext(state, now);
  assert.deepEqual([...result.people].sort(), ['internal','linked','warm']);
  assert.equal(result.organisations.get('linked'), 'A');
  assert.deepEqual(state, before, 'filtering never edits saved personal or commercial state');
});

test('five desk recommendations require recent primary evidence and route, excluding non-agencies and withdrawn findings', () => {
  const records = Array.from({length:7}, (_,i) => agency(`A${i}`));
  records.push(agency('community',{kind:'community'}),agency('unknown',{kind:null}),
    agency('declined',{status:'declined'}),agency('no-desk',{status:'no_relevant_desk'}),
    agency('archived',{archived_at:'2026-10-29'}),agency('excluded',{exclusion:{reason:'Excluded'}}),
    agency('future',{research:[review({reviewed_at:'2026-11-01'})]}),
    agency('old',{research:[review({reviewed_at:'2026-07-01'})]}),
    agency('invalid',{research:[review({reviewed_at:'2026-02-30'})]}),
    agency('search',{research:[review({verification:'search-verified'})]}),
    agency('no-route',{research:[review({public_contact_route:''})]}),
    agency('no-source',{research:[review({source_urls:[]})]}),
    agency('withdrawn',{research:[review(),review({reviewed_at:'2026-10-30T11:00:00Z',desk_summary:''})]}),
    agency('failed',{research:[review(),review({reviewed_at:'2026-10-30T11:00:00Z',verification:'unconfirmed'})]}));
  assert.deepEqual(recruitmentContext(withOpenRoles(records),now).agencies.map(x=>x.record.id), ['A0','A1','A2','A3','A4']);
  const rejected = records.slice(7);
  assert.deepEqual(recruitmentContext(withOpenRoles(rejected),now).agencies, []);
});

test('newest desk review wins independently of observation order; unrelated research cannot renew it', () => {
  const records = [agency('Z',{research:[review({reviewed_at:'2026-10-29'})]}),agency('A',{
    research:[review({reviewed_at:'2026-10-28'}),{reviewed_at:'2026-10-30',summary:'Unrelated news'}]}),
    agency('B',{research:[review(),review({reviewed_at:'2026-10-27'})]})];
  assert.deepEqual(recruitmentContext(withOpenRoles(records),now).agencies.map(x=>x.record.id), ['B','Z','A']);
});

test('large retained directory still returns only five agency rows and deduplicated contact membership', () => {
  const records = Array.from({length:3000},(_,i)=>agency(`agency-${String(i).padStart(4,'0')}`));
  const start = performance.now();
  const selected = recruitmentContext(withOpenRoles(records),now);
  assert.deepEqual(selected.agencies.map(x=>x.record.id), ['agency-0000','agency-0001','agency-0002','agency-0003','agency-0004']);
  console.log(`People: 3000 agency records selected in ${(performance.now()-start).toFixed(1)} ms`);
});

test('review dates validate the calendar day without dropping ISO timestamps with a timezone', () => {
  const records = [agency('offset',{research:[review({reviewed_at:'2026-10-30T00:30:00+01:00'})]}),
    agency('invalid-first',{research:[review({reviewed_at:'invalid'}),review()]}),
    agency('invalid-day',{research:[review({reviewed_at:'2026-09-31T00:30:00+01:00'})]})];
  assert.deepEqual(recruitmentContext(withOpenRoles(records),now).agencies.map(x=>x.record.id), ['invalid-first','offset']);
});

test('desks and warm consultants accompany confirmed open roles, while sub-threshold or conflicted roles do not qualify', () => {
  const records = [
    agency('verified-desk', { name: 'Alpha Desk' }),
    { id: 'warm-agency', name: 'Bravo Warm Agency', type: 'agency', kind: 'agency',
      research: [{ reviewed_at: '2026-10-30', verification: 'primary-read',
        summary: 'Warm recruiter route covering engineering leadership.', source_urls: ['https://bravo.example.com'] }] },
    { id: 'warm-person', name: 'Jay Warm', type: 'person', kind: 'recruiter', relationship: 'warm', role: 'Managing Director' },
    { id: 'role-agency', name: 'Charlie Contract Agency', type: 'agency', kind: 'agency', research: [] },
    { id: 'job-good', name: 'Software Engineering Manager', type: 'job', status: 'candidate', pay: '£750/day', engagement: 'contract',
      availability:openAvailability(), relevance: { score: 40, qualification: { conflicts: [] } } },
    { id: 'low-role-agency', name: 'Delta Subthreshold Agency', type: 'agency', kind: 'agency', research: [] },
    { id: 'job-low', name: 'Founding Software Engineer', type: 'job', status: 'candidate',
      availability:openAvailability(), relevance: { score: 32, qualification: { conflicts: [] } } },
    { id: 'conflict-role-agency', name: 'Echo Conflict Agency', type: 'agency', kind: 'agency', research: [] },
    { id: 'job-conflict', name: 'Forward Deployed AI Engineer', type: 'job', status: 'candidate',
      availability:openAvailability(), relevance: { score: 40, qualification: { conflicts: ['Advert text states interim; employment_type says permanent.'] } } },
  ];
  const links = [
    { from_id: 'warm-person', to_id: 'warm-agency', relation: 'consultant_at' },
    { from_id: 'job-good', to_id: 'role-agency', relation: 'hiring_for' },
    { from_id: 'job-good', to_id: 'verified-desk', relation: 'advertised_by' },
    { from_id: 'job-good', to_id: 'warm-agency', relation: 'advertised_by' },
    { from_id: 'job-low', to_id: 'low-role-agency', relation: 'hiring_for' },
    { from_id: 'job-conflict', to_id: 'conflict-role-agency', relation: 'hiring_for' },
  ];
  const selected = recruitmentContext({ records, links }, now);
  assert.deepEqual(selected.agencies.map(x => x.record.id), ['verified-desk', 'warm-agency', 'role-agency']);
  const warmEntry = selected.agencies.find(x => x.record.id === 'warm-agency');
  assert.equal(warmEntry.consultant?.id, 'warm-person');
  assert.match(warmEntry.summary, /Warm recruiter route/);
  const roleEntry = selected.agencies.find(x => x.record.id === 'role-agency');
  assert.equal(roleEntry.roles?.length, 1);
  assert.equal(roleEntry.roles[0].id, 'job-good');
});

test('agency outreach lookup resolves the shared consultant approach so agency and person sheets share one progress state', () => {
  const ag = { id: 'AG-1', type: 'agency', name: 'Specialist Desk' };
  const consultant = { id: 'CP-1', type: 'person', name: 'Alex Consultant' };
  const shared = { id: 'action:CP-1:AG-1', record_id: 'CP-1', person_id: 'CP-1', company_id: 'AG-1', route: 'agency', stage: 'ready' };
  const map = new Map([[ag.id, ag], [consultant.id, consultant]]);
  const outreach = createOutreachViews({
    getState: () => ({ intelligence: { actions: [shared], research: [] } }),
    getRecord: id => map.get(id),
  });
  assert.deepEqual(outreach.forRecord(ag).map(a => a.id), ['action:CP-1:AG-1']);
  assert.deepEqual(outreach.forRecord(consultant).map(a => a.id), ['action:CP-1:AG-1']);
});


test('agency role recommendations respect disqualification, vacancy closure, deadlines and duplicates', () => {
  const base = { type: 'job', status: 'candidate', name: 'Engineering Lead', availability:openAvailability(), relevance: { score: 50 } };
  const variations = [
    { relevance: { score: 50, qualification: { eligible: false } } },
    { relevance: { score: 50, qualification: { domain: 'outside' } } },
    { evidence_checks: { vacancy: { status: 'closed' } } },
    { published_deadline: '2026-10-29' },
    { relevance: { score: 50, duplicate_of: 'other' } },
  ];
  for (const variation of variations) {
    const records = [agency('A', {research: []}), {...base, id: 'J', ...variation}];
    const links = [{from_id: 'J', to_id: 'A', relation: 'hiring_for'}];
    assert.deepEqual(recruitmentContext({records, links}, now).allAgencies, [], JSON.stringify(variation));
  }
});

test('people groups consolidate Current contacts warmest-first, place Not contacting at the bottom, and suppress demoted outreach until promoted', () => {
  assert.deepEqual(PEOPLE_GROUPS.map(([key, title]) => [key, title]), [
    ['first', 'First priority'],
    ['next', 'Next'],
    ['current', 'Current contacts'],
    ['recruiters', 'Recruiters'],
    ['meet', 'To meet'],
    ['others', 'Everyone else'],
    ['never', 'Not contacting'],
  ]);
  const ag = agency('AG-1');
  const firstP = { id: 'P-first', type: 'person', name: 'First Person', priority: 'first', relationship: 'warm' };
  const nextP = { id: 'P-next', type: 'person', name: 'Next Person', priority: 'next', relationship: 'dormant' };
  const closeP = { id: 'P-close', type: 'person', name: 'Close Contact', relationship: 'warm', relevance: { reach: { tier: 'close' } } };
  const touchP = { id: 'P-touch', type: 'person', name: 'In Touch Contact', relationship: 'active', relevance: { reach: { tier: 'in_touch' } } };
  const knownP = { id: 'P-known', type: 'person', name: 'Known Contact', relationship: 'dormant', relevance: { reach: { tier: 'known' } } };
  const recP = { id: 'P-rec', type: 'person', name: 'Recruiter Contact', kind: 'recruiter', relationship: 'cold' };
  const meetP = { id: 'P-meet', type: 'person', name: 'Decision Maker', verification_level: 'primary-read', relationship: 'none' };
  const otherP = { id: 'P-other', type: 'person', name: 'Unclassified Contact', relationship: 'none' };
  const demotedP = { id: 'P-demoted', type: 'person', name: 'Demoted Contact', relationship: 'do_not_contact', priority: 'first', kind: 'recruiter' };
  const state = {
    records: [ag, firstP, nextP, closeP, touchP, knownP, recP, meetP, otherP, demotedP],
    links: [{ from_id: 'P-demoted', to_id: 'AG-1', relation: 'consultant_at' }],
    intelligence: {
      actions: [{ id: 'action:P-demoted:AG-1', record_id: 'P-demoted', person_id: 'P-demoted', company_id: 'AG-1', route: 'agency', stage: 'ready' }],
      research: [],
    },
  };
  const recCtx = recruitmentContext(state, now);
  assert.equal(groupPerson(firstP, recCtx), 'first');
  assert.equal(groupPerson(nextP, recCtx), 'next');
  assert.equal(groupPerson(closeP, recCtx), 'current');
  assert.equal(groupPerson(touchP, recCtx), 'current');
  assert.equal(groupPerson(knownP, recCtx), 'current');
  assert.equal(groupPerson(recP, recCtx), 'recruiters');
  assert.equal(groupPerson(meetP, recCtx), 'meet');
  assert.equal(groupPerson(otherP, recCtx), 'others');
  assert.equal(groupPerson(demotedP, recCtx), 'never');

  const byId = new Map(state.records.map(r => [r.id, r]));
  const outreach = createOutreachViews({ getState: () => state, getRecord: id => byId.get(id) });
  assert.deepEqual(outreach.forRecord(demotedP), []);
  assert.deepEqual(outreach.forRecord(ag), []);

  // Promoting back to current contacts (dormant, priority cleared) restores group and outreach eligibility.
  demotedP.relationship = 'dormant';
  demotedP.priority = null;
  const promotedCtx = recruitmentContext(state, now);
  assert.equal(groupPerson(demotedP, promotedCtx), 'current');
  assert.deepEqual(outreach.forRecord(demotedP).map(a => a.id), ['action:P-demoted:AG-1']);
  assert.deepEqual(outreach.forRecord(ag).map(a => a.id), ['action:P-demoted:AG-1']);
});

// Small DOM surface for the real People/progress renderers; no alternate presentation logic.
class Element {
  constructor(tag) { this.tagName = tag; this.childNodes = []; this.dataset = {}; this.attributes = {}; }
  append(...nodes) { this.childNodes.push(...nodes); }
  prepend(...nodes) { this.childNodes.unshift(...nodes); }
  replaceChildren(...nodes) { this.childNodes = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  set textContent(value) { this.childNodes = [String(value)]; }
  get textContent() { return this.childNodes.map(n => typeof n === 'string' ? n : n.textContent).join(' '); }
}
function withDom(run) {
  const previous = { document: globalThis.document, Node: globalThis.Node };
  globalThis.document = { createElement: tag => new Element(tag) }; globalThis.Node = Element;
  try { return run(); }
  finally { globalThis.document = previous.document; globalThis.Node = previous.Node; }
}
const flatten = node => [node, ...(node.childNodes || []).filter(n => typeof n !== 'string').flatMap(flatten)];
const { createProgressViews } = await import('../../workspace/js/progress.js');
const { createPeopleView } = await import('../../workspace/js/people.js');
function renderProgress(changes = {}) {
  const person = {id:'P',type:'person',name:'Synthetic Contact'};
  const progress = { status:'waiting', label:'Waiting for a reply', next:'Contact recorded 20 Oct. Waiting for a reply.',
    next_step:null, next_is_default:false, history:[{kind:'sent',at:'2026-10-20'}], actions:[], ...changes };
  const item = {id:'person:P',record_id:'P',person_id:'P',progress};
  return withDom(() => createProgressViews({getRecord:()=>person}).block(item).textContent);
}

test('personal waiting preserves canonical contact context without inventing an agreed date or follow-up', () => {
  const text = renderProgress();
  assert.match(text, /Contact recorded 20 Oct\. Waiting for a reply\./);
  assert.doesNotMatch(text, /No agreed date|No next step recorded|Follow.up suggested|overdue/i);
  for (const status of ['waiting','no_response'])
    assert.match(renderProgress({status,reply_to_record:true,next:'They wrote on 5 Nov: record their reply.'}), /They wrote on 5 Nov: record their reply/);
});

test('only an active recorded commitment gets No agreed date; dated, completed and cleared steps stay distinct', () => {
  assert.match(renderProgress({next_step:{kind:'mine',text:'Read the role details'},next:'Read the role details'}), /Read the role details · No agreed date/);
  assert.match(renderProgress({next_step:{kind:'waiting',text:'Wait for the remit'},next:'Waiting for them: Wait for the remit'}), /Waiting for them: Wait for the remit · No agreed date/);
  const dated = renderProgress({next_step:{kind:'mine',text:'Read the role details',agreed_on:'2026-11-09'},next:'Read the role details · agreed 9 Nov'});
  assert.match(dated, /agreed 9 Nov/); assert.doesNotMatch(dated, /No agreed date/);
  for (const kind of ['done','none']) assert.doesNotMatch(renderProgress({next_step:{kind,text:'Old step'}}), /Old step|No agreed date/);
  for (const status of ['research','held','no_response','closed','skipped','won','handed_over'])
    assert.doesNotMatch(renderProgress({status,next_step:{kind:'mine',text:'Old step'},next:'Old step'}), /Old step|No agreed date/, status);
});

test('personal sheets keep generated outreach advice suppressed when there is no recorded step', () => {
  for (const status of ['ready','replied','conversation']) {
    const text = renderProgress({status,next:'Prepare a note and ask for an introduction.',next_is_default:status==='ready',history:[]});
    assert.doesNotMatch(text, /Prepare a note|No agreed date/);
    assert.match(text, /No next step recorded/);
  }
});

test('rendered People uses a unique attention queue and filters without changing saved data', () => {
  const person = (id,name,priority,tier,score=0) => ({id,name,type:'person',priority,relevance:{reach:{tier},score}});
  const records = [person('known','High Score Known','first','known',100),person('close-z','Zed Close','first','close',10),
    person('touch','In Touch','first','in_touch',200),person('close-a','Alex Close','first','close',10),
    person('close-high','Close High','first','close',20),person('next','Next Close','next','close',1000)];
  const state = {records,links:[]}, before = structuredClone(state);
  const progress = {status:'waiting',label:'Waiting for a reply',next:'Contact recorded 20 Oct. Waiting for a reply.',history:[{kind:'sent'}]};
  const container = withDom(() => { const node = new Element('main'); createPeopleView({getState:()=>state,recordProgress:()=>progress,importReview:{render(){}}}).render(node); return node; });
  const rows = flatten(container).filter(n => n.className?.split(' ').includes('person-row'));
  assert.equal(new Set(rows.map(n => n.dataset.id)).size, 6);
  assert.match(container.textContent, /Needs attention/);
  assert.match(container.textContent, /6 unique people/);
  assert.doesNotMatch(container.textContent, /Contact first|Not known/);
  assert.deepEqual(state,before);
});

test('Needs reply chip filters the same unique people as the toolbar and More contains demotion', () => withDom(() => {
  const records = [{id:'reply',name:'Reply Person',type:'person',relationship:'active'},
    {id:'wait',name:'Wait Person',type:'person',relationship:'warm'}];
  const state = {records,links:[],intelligence:{actions:[
    {id:'a-reply',record_id:'reply',progress:{status:'replied',label:'Replied',history:[{kind:'replied',at:'2026-11-05'}]}},
    {id:'a-wait',record_id:'wait',progress:{status:'waiting',label:'Waiting for a reply',history:[{kind:'sent',at:'2026-10-20'}]}}
  ]}};
  const container = new Element('main');
  const view = createPeopleView({getState:()=>state,recordProgress:()=>null,importReview:{render(){}},demotePerson(){}});
  view.render(container);
  assert.equal(view.count(), 2);
  assert.deepEqual(flatten(container).filter(n=>n.className?.split(' ').includes('person-row')).map(n=>n.dataset.id), ['reply','wait']);
  const chip = flatten(container).find(n=>n.dataset.peopleChip==='needs_reply');
  assert.ok(chip, 'state chip is an actual filter control');
  chip.onclick();
  assert.deepEqual(flatten(container).filter(n=>n.className?.split(' ').includes('person-row')).map(n=>n.dataset.id), ['reply']);
  assert.match(container.textContent,/1 unique person/);
  const more = flatten(container).find(n=>n.tagName==='details' && n.className==='person-more');
  assert.ok(more);
  assert.ok(flatten(more).some(n=>n.dataset.personAction==='demote'));
}));

test('People Contact links open only recorded routes and do not log or mutate anything', () => withDom(() => {
  const state={records:[
    {id:'email',type:'person',name:'Email Person',relationship:'warm',email:'alex@example.test',url:'https://www.linkedin.com/in/synthetic-alex'},
    {id:'linkedin',type:'person',name:'LinkedIn Person',relationship:'warm',url:'https://www.linkedin.com/in/synthetic-linkedin'},
    {id:'preferred',type:'person',name:'Preferred Person',relationship:'warm',preferred_channel:'linkedin',email:'sam@example.test',url:'https://www.linkedin.com/in/synthetic-sam'},
    {id:'blocked',type:'person',name:'Blocked Person',relationship:'do_not_contact',email:'blocked@example.test'},
    {id:'missing',type:'person',name:'Missing Person',relationship:'warm'}],links:[]};
  const before=structuredClone(state), container=new Element('main');
  createPeopleView({getState:()=>state,importReview:{render(){}},openRecord(){assert.fail('Contact is an external handoff');}}).render(container);
  const links=flatten(container).filter(n=>n.className==='person-contact');
  assert.deepEqual(links.map(n=>n.href),['mailto:alex@example.test','https://www.linkedin.com/in/synthetic-linkedin','https://www.linkedin.com/in/synthetic-sam']);
  for(const link of links) {assert.equal(link.tagName,'a');assert.equal(link.textContent,'Contact');assert.equal(link.onclick,undefined);}
  assert.deepEqual(state,before);
  const progress=createProgressViews({getRecord:()=>state.records[0],mutate(){assert.fail('Opening contact cannot log activity');}});
  const panel=progress.contact({person_id:'email',progress:{}});
  const link=flatten(panel).find(n=>n.className==='approach-contact');
  assert.equal(link.href,'mailto:alex@example.test');
  assert.equal(link.onclick,undefined);
  assert.equal(progress.contact({person_id:'email',progress:{},exclusion:{reason:'Excluded context'}}),null);
}));

test('a researched agency without a confirmed open role is not recommended for contact', () => {
  assert.deepEqual(recruitmentContext({records:[agency('A')],links:[]},now).agencies,[]);
  const job={id:'J',type:'job',name:'Engineering Manager',relevance:{score:50}};
  const state={records:[agency('A'),job],links:[{from_id:'J',to_id:'A',relation:'advertised_by'}]};
  assert.deepEqual(recruitmentContext(state,now).agencies,[]);
  job.availability={status:'open',basis:'research',at:'2026-10-30T11:00:00Z',until:'2026-11-01T11:00:00Z'};
  assert.deepEqual(recruitmentContext(state,now).agencies.map(a=>a.record.id),['A']);
  job.availability.until='2026-10-30T12:00:00Z';
  assert.deepEqual(recruitmentContext(state,now).agencies,[]);
});

test('agency rows prefer role-specific contact evidence and label a fallback consultant as a desk contact', () => withDom(() => {
  const job={id:'J',type:'job',name:'Engineering Lead',relevance:{score:50},
    availability:{status:'open',basis:'research',at:new Date().toISOString(),until:'2099-01-01T00:00:00Z'},
    briefing:{public_contact_route:'Taylor Role handles this advertised role: taylor@example.test.'}};
  const saved={id:'P',type:'person',name:'Morgan Desk',kind:'recruiter',role:'Engineering consultant'};
  const state={records:[agency('A',{research:[]}),job,saved],links:[
    {from_id:'J',to_id:'A',relation:'advertised_by'},{from_id:'P',to_id:'A',relation:'consultant_at'}]};
  const render=()=>{
    const node=new Element('main');createPeopleView({getState:()=>state,importReview:{render(){}}}).render(node);
    return flatten(node).find(n=>n.className==='record-card agency-row');
  };
  assert.match(render().textContent,/Engineering Lead: Taylor Role handles this advertised role/);
  assert.doesNotMatch(render().textContent,/Morgan Desk/);
  job.briefing={};
  assert.match(render().textContent,/Desk contact: Morgan Desk/);
}));

test('outreach action lookup preserves order and duplicates with bounded traversal and snapshot invalidation', () => {
  const person={id:'P',type:'person'}, agency={id:'A',type:'agency'}, other={id:'O',type:'person'};
  const records=new Map([person,agency,other].map(record=>[record.id,record]));
  let reads=0;
  const counted=action=>new Proxy(action,{get(target,key){if(key==='record_id') reads++;return target[key];}});
  const shared=counted({id:'shared',record_id:'P',company_id:'A',route:'agency'});
  let state={intelligence:{actions:[shared,counted({id:'direct',record_id:'A',company_id:'A',route:'agency'}),
    counted({id:'wrong-route',record_id:'O',company_id:'A',route:'company'}),
    ...Array.from({length:1000},(_,i)=>counted({id:`unrelated-${i}`,record_id:`other-${i}`}))],research:[shared]}};
  const outreach=createOutreachViews({getState:()=>state,getRecord:id=>records.get(id)});
  for(let i=0;i<100;i++) {
    assert.deepEqual(outreach.forRecord(agency).map(action=>action.id),['shared','direct','shared']);
    assert.deepEqual(outreach.forRecord(person).map(action=>action.id),['shared','shared']);
  }
  assert.ok(reads<10000,`${reads} action record reads should not scale with all actions per row`);
  person.relationship='do_not_contact';
  assert.deepEqual(outreach.forRecord(agency).map(action=>action.id),['direct'],'current exclusions are checked at lookup');
  person.relationship='warm';
  state={intelligence:{actions:[{id:'new',record_id:'P',company_id:'A',route:'agency'}],research:[]}};
  assert.deepEqual(outreach.forRecord(agency).map(action=>action.id),['new']);
  state.intelligence.actions=[{id:'replaced',record_id:'P'}];
  assert.deepEqual(outreach.forRecord(agency),[]);
  assert.deepEqual(outreach.forRecord(person).map(action=>action.id),['replaced']);
});
