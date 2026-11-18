import test from 'node:test';
import assert from 'node:assert/strict';
import { createRecordTimeline } from '../../workspace/js/record-timeline.js';

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.isConnected = true; this.value = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent ?? child).join(''); }
}
const response = data => ({ok:true,status:200,json:async()=>data});
const timeline = (label = 'Recorded change', revision = 1) => ({revision,interactions:[],
  history:[{id:1,at:'2026-10-30T10:00:00Z',summary:label,actor:'agent'}]});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes,no) => { resolve=yes; reject=no; });
  return {promise,resolve,reject};
};
async function fixture(run) {
  const previous={document:globalThis.document,Node:globalThis.Node,fetch:globalThis.fetch};
  globalThis.document={createElement:tag=>new Element(tag)}; globalThis.Node=Element;
  const records=new Map(Array.from({length:12},(_,i)=>[`P${i}`,{id:`P${i}`,revision:1}]));
  try { await run({records,view:createRecordTimeline({getRecord:id=>records.get(id)}),section:()=>new Element('section')}); }
  finally { Object.assign(globalThis,previous); }
}

test('timeline reuses in-flight reads and keeps every historical event without replacing surrounding input',()=>fixture(async({records,view,section})=>{
  let calls=0; const reply=deferred(); globalThis.fetch=()=>{calls++;return reply.promise;};
  const first=section(), second=section(), input=new Element('input'), parent=new Element('article');
  parent.append(input,first); input.value='Unsaved user text';
  const a=view.render(records.get('P0'),first), b=view.render(records.get('P0'),second);
  assert.equal(calls,1);
  reply.resolve(response({...timeline('Old change'),interactions:[{at:'2026-10-31T10:00:00Z',summary:'Recent contact',channel:'email'}],
    drafts:[{text:'Preserved wording',created_at:'2026-10-29',version_id:1}],
    outcomes:[{kind:'replied',at:'2026-10-31',note:'Historic reply'}],decisions:[]}));
  await Promise.all([a,b]);
  for(const text of ['Old change','Recent contact','Preserved wording','Historic reply']) assert.ok(first.textContent.includes(text),text);
  assert.equal(first.textContent,second.textContent); assert.equal(input.value,'Unsaved user text');
  assert.equal(parent.children[1],first);
  await view.render(records.get('P0'),section()); assert.equal(calls,1);
}));

test('completed timeline cache retains eight recent records and refreshes recency on use',()=>fixture(async({records,view,section})=>{
  let calls=0; globalThis.fetch=async()=>{calls++;return response(timeline());};
  for(let i=0;i<8;i++) await view.render(records.get(`P${i}`),section());
  assert.equal(calls,8);
  await view.render(records.get('P0'),section()); assert.equal(calls,8);
  await view.render(records.get('P8'),section()); assert.equal(calls,9);
  await view.render(records.get('P0'),section()); assert.equal(calls,9,'recently used entry remains cached');
  await view.render(records.get('P1'),section()); assert.equal(calls,10,'least recent completed entry was evicted');
}));

test('evicting completed histories does not duplicate an outstanding request',()=>fixture(async({records,view,section})=>{
  const pending=deferred(); let pendingCalls=0;
  globalThis.fetch=async url=>url.includes('/P0/')?(pendingCalls++,pending.promise):response(timeline());
  const first=view.render(records.get('P0'),section());
  for(let i=1;i<=9;i++) await view.render(records.get(`P${i}`),section());
  const second=view.render(records.get('P0'),section());
  assert.equal(pendingCalls,1); pending.resolve(response(timeline())); await Promise.all([first,second]);
}));

for(const outcome of ['resolve','reject']) test(`cleared ${outcome} response cannot displace a newer same-record request`,()=>fixture(async({records,view,section})=>{
  const old=deferred(), fresh=deferred(); let calls=0;
  globalThis.fetch=()=>{calls++;return calls===1?old.promise:fresh.promise;};
  const oldSection=section(); oldSection.textContent='Existing history';
  const oldRender=view.render(records.get('P0'),oldSection);
  view.clear();
  const freshSection=section(), freshRender=view.render(records.get('P0'),freshSection);
  if(outcome==='resolve') old.resolve(response(timeline('Obsolete history'))); else old.reject(new Error('Old failure'));
  await oldRender;
  assert.equal(oldSection.textContent,'Existing history','cleared request cannot redraw a section');
  const sameSection=section(), sameRender=view.render(records.get('P0'),sameSection);
  assert.equal(calls,2,'obsolete settlement cannot evict the new in-flight entry');
  fresh.resolve(response(timeline('Current history'))); await Promise.all([freshRender,sameRender]);
  assert.ok(freshSection.textContent.includes('Current history')); assert.equal(sameSection.textContent,freshSection.textContent);
  const cached=section(); await view.render(records.get('P0'),cached); assert.equal(calls,2);
  assert.equal(cached.textContent,freshSection.textContent);
}));

test('a completion after clear cannot repopulate the response cache',()=>fixture(async({records,view,section})=>{
  const old=deferred(); let calls=0;
  globalThis.fetch=()=>{calls++;return calls===1?old.promise:Promise.resolve(response(timeline('Current history')));};
  const stale=section(); stale.isConnected=false;
  const pending=view.render(records.get('P0'),stale); view.clear(); old.resolve(response(timeline('Obsolete history'))); await pending;
  const current=section(); await view.render(records.get('P0'),current);
  assert.equal(calls,2); assert.ok(current.textContent.includes('Current history'));
}));

test('disconnected or revised sections ignore late history; failed reads remain retryable',()=>fixture(async({records,view,section})=>{
  const old=deferred(); globalThis.fetch=()=>old.promise;
  const detached=section(), revised=section(); detached.textContent='Detached input'; revised.textContent='Edited input';
  const first=view.render(records.get('P0'),detached), second=view.render(records.get('P1'),revised);
  detached.isConnected=false; records.set('P1',{id:'P1',revision:2}); old.resolve(response(timeline())); await Promise.all([first,second]);
  assert.equal(detached.textContent,'Detached input'); assert.equal(revised.textContent,'Edited input');
  let calls=0; globalThis.fetch=async()=>{calls++;if(calls===1)throw new Error('Temporary failure');return response(timeline('Recovered history'));};
  const failed=section(); await view.render(records.get('P2'),failed);
  assert.ok(failed.textContent.includes('Temporary failure'));
  const retry=failed.children.find(child=>child.tagName==='button'); assert.ok(retry);
  await retry.onclick(); assert.equal(calls,2); assert.ok(failed.textContent.includes('Recovered history'));
}));

test('Retry refetches a response that failed to render instead of reusing invalid cached history',()=>fixture(async({records,view,section})=>{
  let calls=0;
  globalThis.fetch=async()=>response(++calls===1?{revision:1,history:[]}:timeline('Recovered valid history'));
  const target=section(); await view.render(records.get('P0'),target);
  const retry=target.children.find(child=>child.tagName==='button'); assert.ok(retry);
  await retry.onclick();
  assert.equal(calls,2); assert.ok(target.textContent.includes('Recovered valid history'));
  await view.render(records.get('P0'),section()); assert.equal(calls,2);
}));
