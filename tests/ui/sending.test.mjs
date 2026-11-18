import test from 'node:test';
import assert from 'node:assert/strict';
import { candidates, handoff, contactPayload } from '../../workspace/js/sending-model.js';

const person = {id:'P',type:'person',name:'Sam Example',revision:4,email:'sam@example.test',phone:'+447700900123',url:'https://www.linkedin.com/in/sam-example'};
const action = {method:'outcome',kind:'sent',record_id:'P',action_id:'ask:P'};
const item = {id:'ask:P',record_id:'P',person_id:'P',progress:{actions:[action]}};
test('batch eligibility deduplicates people and excludes missing, archived and disallowed contacts',()=>{
  const state={records:[person,{...person,id:'N',relationship:'do_not_contact'},{...person,id:'X',exclusion:true}],intelligence:{actions:[item,item,...['N','X'].map(id=>({...item,record_id:id,person_id:id,progress:{actions:[{...action,record_id:id}]}}))]}};
  assert.deepEqual(candidates(state).map(x=>x.person.id),['P']);
  assert.equal(candidates({...state,records:[{...person,archived_at:'2026-11-11'}]}).length,0);
  assert.equal(candidates({...state,intelligence:{actions:[{...item,progress:{actions:[]}}]}}).length,0);
});
test('handoffs use the selected channel and never silently fall back to another one',()=>{
  const gmail=new URL(handoff(person,'email','Subject: Catch up\n\nHi Sam & friends').href);
  assert.equal(gmail.origin,'https://mail.google.com');
  assert.equal(gmail.searchParams.get('to'),'sam@example.test');
  assert.equal(gmail.searchParams.get('su'),'Catch up');
  assert.equal(gmail.searchParams.get('body'),'Hi Sam & friends');
  assert.equal(new URL(handoff(person,'whatsapp','Hello Sam').href).searchParams.get('text'),'Hello Sam');
  assert.equal(handoff({...person,email:''},'email','Hello').href,'');
  assert.equal(handoff(person,'linkedin','Hello').href,person.url);
  assert.equal(handoff(person,'instagram','Hello').href,'https://www.instagram.com/direct/inbox/');
  assert.equal(handoff({...person,url:'javascript:alert(1)'},'linkedin','Hello').href,'');
  for (const channel of ['email','whatsapp','linkedin','instagram']) {
    assert.equal(handoff(person,channel,'Hi {{personal_line}}').href,'');
    assert.equal(handoff({...person,relationship:'do_not_contact'},channel,'Hello').href,'');
  }
  assert.equal(handoff(person,'email','x'.repeat(3000)).prefilled,false);
  assert.equal(new URL(handoff(person,'email','Subject: Catch up\n\n'+'x'.repeat(3000)).href).searchParams.get('su'),'Catch up');
});
test('quick logging preserves revision and next step, never claims prepared wording was sent',()=>{
  assert.deepEqual(contactPayload(action,4,'email',''),{revision:4,kind:'sent',action_id:'ask:P',channel:'email',note:''});
  assert.deepEqual(contactPayload(action,4,'instagram','Caught up'),{revision:4,kind:'sent',action_id:'ask:P',channel:'other',note:'Instagram: Caught up'});
  assert.throws(()=>contactPayload(action,4,'unknown',''),/channel/i);
});
test('large directories scan each action once and return unique permitted people',()=>{
  let reads=0;
  const records=Array.from({length:3000},(_,n)=>({...person,id:`P${n}`}));
  const actions=records.map(p=>({person_id:p.id,record_id:p.id,get progress(){reads++;return {actions:[{...action,record_id:p.id}]};}}));
  assert.equal(candidates({records,intelligence:{actions}}).length,3000);
  assert.equal(reads,3000);
});
