import test from 'node:test';
import assert from 'node:assert/strict';
import { personApproachBrief, agencyApproachBrief } from '../../workspace/js/approach-brief.js';

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.childNodes = []; this.dataset = {}; }
  append(...children) { this.childNodes.push(...children); }
  replaceChildren(...children) { this.childNodes = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this['on'+name] = handler; }
  set textContent(value) { this.childNodes = [String(value)]; }
  get textContent() { return this.childNodes.map(child => typeof child === 'string' ? child : child.textContent).join(' '); }
}
const all = node => [node, ...node.childNodes.filter(child => child instanceof Element).flatMap(all)];
function dom(run) {
  const previous = {document:globalThis.document, Node:globalThis.Node};
  globalThis.document = {createElement:tag => new Element(tag)}; globalThis.Node = Element;
  try { return run(); } finally { Object.assign(globalThis, previous); }
}
const person = {id:'P', type:'person', name:'Synthetic Person', known_context:'Met through a past project.'};
const job = {id:'J', type:'job', name:'Engineering Lead', relevance:{score:60}, status:'shortlisted'};
const company = {id:'C', type:'company', name:'Example Employer'};
const state = {records:[person,job,company], links:[{from_id:'J',to_id:'C',relation:'hiring_for'},
  {from_id:'P',to_id:'C',relation:'works_at'}], opportunities:{act_now:[{record_id:'J'}]}};

test('person panel leads with the actionable next step and folds relationship context', () => dom(() => {
  let options;
  const current = {id:'person:P',record_id:'P',progress:{status:'conversation',label:'In conversation',actions:[]}};
  const node = personApproachBrief(person,state,{current,progress:{block(item,opts){ options=opts; return new Element('section'); }}});
  assert.equal(node.childNodes[0].textContent, 'Do this next');
  assert.equal(options.next, 'Agree and record the next step.');
  assert.equal(all(node).find(child => child.tagName==='DETAILS').childNodes[0].textContent, 'Relationship and source context');
  assert.equal(node.childNodes.filter(child => child.tagName==='P').some(child => child.textContent.includes('Met through')),false);
}));

test('person role links use Jobs navigation and do not claim hiring authority', () => dom(() => {
  const opened=[];
  const node=personApproachBrief(person,state,{progress:{contact:()=>null},openJob:id=>opened.push(id)});
  const role=all(node).find(child=>child.dataset.roleId==='J');
  assert.ok(role,'linked current role is visible'); role.onclick();
  assert.deepEqual(opened,['J']);
  assert.match(node.textContent,/Recorded at Example Employer/);
  assert.match(node.textContent,/does not establish hiring authority/);
}));

test('held, terminal and completed contacts do not receive a new action prompt', () => dom(() => {
  for (const change of [{status:'held'}, {status:'closed'}, {status:'conversation',next_step:{kind:'done',text:'Finished'}}]) {
    let options;
    const current={id:'person:P',record_id:'P',progress:{actions:[],...change}};
    personApproachBrief(person,state,{current,progress:{block(item,opts){options=opts;return new Element('section');}}});
    assert.equal(options.next,'No action is queued for this contact.');
  }
}));

test('default detail retains the queue action for a role-linked contact with no recorded next step', () => dom(() => {
  let options;
  const current={id:'person:P',record_id:'P',progress:{status:'ready',actions:[]}};
  personApproachBrief(person,{...state,intelligence:{actions:[current]}},
    {current,progress:{block(item,opts){options=opts;return new Element('section');}}});
  assert.equal(options.next,'Review the linked role and confirm how they can help.');
}));

test('agency role links use Jobs navigation', () => dom(() => {
  const agency={id:'A',type:'agency',name:'Example Desk'};
  const role={...job, availability:{status:'open',basis:'research',at:'2026-10-30T11:00:00Z',until:'2099-01-01T00:00:00Z'}};
  const opened=[];
  const node=agencyApproachBrief(agency,{records:[agency,role],links:[{from_id:'J',to_id:'A',relation:'advertised_by'}]},
    {progress:{contact:()=>null},openJob:id=>opened.push(id)});
  const link=all(node).find(child=>child.dataset.roleId==='J');
  assert.ok(link);link.onclick();assert.deepEqual(opened,['J']);
}));

test('blocked relationship routes offer no new contact prompt or contact control', () => dom(() => {
  for (const fields of [{exclusion_contexts:['C']},{relevance:{reach:{tier:'blocked'}}}]) {
    const contact={...person,...fields};
    const node=personApproachBrief(contact,{...state,records:[contact,job,company]},
      {progress:{contact(){assert.fail('a blocked contact cannot offer a route');}}});
    assert.match(node.textContent,/Excluded from new work/);
    assert.doesNotMatch(node.textContent,/Choose a role|Review the linked role/);
  }
}));

test('large role collections render only three initial links and expand in bounded pages', () => dom(() => {
  const roles=Array.from({length:100},(_,n)=>({job:{...job,id:`J${n}`},bases:['Recorded connection'],actNow:false}));
  const node=personApproachBrief(person,state,{context:{roles,action:{}},progress:{contact:()=>null}});
  const count=()=>all(node).filter(child=>child.dataset.roleId).length;
  assert.equal(count(),3);
  const more=all(node).find(child=>child.className==='person-more-roles');
  more.open=true;more.ontoggle();assert.equal(count(),15);
  all(more).find(child=>child.tagName==='BUTTON' && child.textContent.startsWith('Show more')).onclick();
  assert.equal(count(),27);
}));
