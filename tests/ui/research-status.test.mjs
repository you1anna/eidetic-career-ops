import test from 'node:test';import assert from 'node:assert/strict';
import {researchOutcome, researchTime, researchAttemptText, sourceCheckText, RESEARCH_SCHEDULE} from '../../workspace/js/research-status.js';
import {readFileSync} from 'node:fs';
import {researchStatus} from '../../workspace/js/opportunity-card.js';
const now=Date.parse('2026-10-17T12:00:00Z');const good={status:'ok',last_attempt:'2026-10-17T11:50:00Z',total:3,checked:3,verified:3,contacts_total:2,contacts_verified:2,backlog:0,unverified:0};
test('success requires complete coverage and cannot hide missing contact checks',()=>{assert.equal(researchOutcome(good,now),'Checks completed');assert.equal(researchOutcome({...good,contacts_verified:1},now),'Finished with gaps');assert.equal(researchOutcome({...good,backlog:1},now),'Finished with gaps');assert.equal(researchOutcome({...good,unverified:1},now),'Finished with gaps');});
test('budget, access, crashes and missed schedules stay distinct',()=>{assert.equal(researchOutcome({},now),'Not started');assert.match(researchOutcome({...good,status:'blocked',reason:'budget'},now),/spending limit/);assert.equal(researchOutcome({...good,status:'blocked',reason:'access'},now),'Blocked — sign-in or research service needs attention');assert.match(researchOutcome({...good,status:'running'},now+3600000),/Stalled/);assert.match(researchOutcome(good,now+17*3600000),/Overdue/);assert.match(researchOutcome({...good,status:'failed'},now),/Failed/);});

test('ambiguous stops never infer provider allowance, daily cap or concurrency cause',()=>{
 for(const pass_cap of [false,true])assert.equal(researchOutcome({...good,status:'partial',reason:'usage_limit',pass_cap},now),'Paused — research not completed');
 assert.equal(researchOutcome({...good,status:'blocked',reason:'access'},now),'Blocked — sign-in or research service needs attention');
 assert.equal(researchOutcome({...good,status:'blocked',reason:'unknown'},now),'Blocked — research needs attention');
});
test('quiet passes and failed runs retain attention independently of ambiguous limit metadata',()=>{
 const capped={...good,status:'partial',reason:'usage_limit',pass_cap:true,backlog:3};
 assert.equal(researchOutcome(capped,now),'Paused — research not completed');
 assert.equal(researchOutcome({...capped,quiet_passes:3,quiet_attention:false},now),'Paused — research not completed');
 assert.equal(researchOutcome({...capped,quiet_passes:22,quiet_attention:true},now),'Nothing confirmed in 22 passes — needs attention');
 assert.match(researchOutcome({...capped,status:'failed',quiet_attention:true},now),/^Failed/);
 assert.equal(researchOutcome({...good,status:'blocked',reason:'unknown',quiet_attention:true,quiet_passes:4},now),'Blocked — research needs attention');
});

test('item research wording uses its own outcome and London attempt time without raw diagnostics',()=>{
 const at='2026-10-17T12:02:21Z';
 const cases=[
  ['failed','RuntimeError','Research failed — earlier evidence kept'],
  ['unverified','robots_disallowed','Research incomplete — automated check blocked by the site'],
  ['context_changed','private path','Research outdated — role or source changed'],
  ['usage_limited','429','Research paused; not completed'],
  ['access_blocked','','Research blocked — sign-in or research service needs attention'],
  ['unexpected_enum','<img onerror=alert(1)>','Research outcome unavailable'],
 ];
 for(const [status,reason,label] of cases)
  assert.equal(researchStatus({status,reason,attempted_at:at}),`${label} · 17 Oct 2026, 13:02 BST`);
 assert.equal(researchStatus({status:'not_attempted'}),'No research attempt recorded.');
 assert.equal(researchStatus('Research failed · RuntimeError'),'Research outcome unavailable · date not recorded');
 for(const attempted_at of [null,'not-a-date','2026-10-17','2026-10-17T12:02:21','2026-02-31T09:00:00Z','2026-10-17T24:00:00Z'])
  assert.equal(researchStatus({status:'failed',attempted_at,updated_at:at,reviewed_at:at}),'Research failed — earlier evidence kept · date not recorded');
 assert.equal(researchStatus({status:'confirmed',attempted_at:'2026-11-06T09:00:00Z'}),'Research confirmed evidence · 6 Nov 2026, 09:00 GMT');
});

test('missing coverage and malformed counters cannot become completed checks',()=>{
 for(const change of [{checked:null},{total:undefined},{contacts_verified:null},{contacts_total:-1},{verified:'3'},{unverified:null},{backlog:null}])
  assert.equal(researchOutcome({...good,...change},now),'Finished — coverage not fully recorded');
});

test('source reason mapping preserves ordinary prose and never derives a new evidence claim',()=>{
 assert.equal(sourceCheckText('robots_disallowed'),'Automated check blocked by the site');
 assert.equal(sourceCheckText('Research could not verify this source: RuntimeError'),'Could not verify from public sources — earlier evidence kept.');
 for(const prose of ['Source says the advert remains open.','The advert discusses robots_disallowed as a product setting.','<img src=x onerror=alert(1)>'])
  assert.equal(sourceCheckText(prose),prose);
});

test('London timestamps distinguish the repeated hour and ordinary passes match the declared timer',()=>{
 assert.equal(researchTime('2026-10-25T00:30:00Z'),'25 Oct 2026, 01:30 BST');
 assert.equal(researchTime('2026-10-25T01:30:00Z'),'25 Oct 2026, 01:30 GMT');
 assert.equal(RESEARCH_SCHEDULE,'Usual automatic passes: 09:00 and 19:00 London time. A particular role may wait for a later pass.');
 const timer=readFileSync(new URL('../../ops/vps/wo-research.timer',import.meta.url),'utf8');
 assert.match(timer,/^OnCalendar=\*-\*-\* 09,19:00:00 Europe\/London$/m);
});

import {caretakerOutcome} from '../../workspace/js/caretaker-status.js';
test('caretaker separates missing, partial, paused, failed and overdue evidence',()=>{
 assert.equal(caretakerOutcome({},now),'Not started');
 const s={last_attempt:good.last_attempt,status:'ok'};
 assert.equal(caretakerOutcome(s,now),'Checks completed');
 assert.equal(caretakerOutcome({...s,status:'partial'},now),'Checks have gaps');
 assert.equal(caretakerOutcome({...s,status:'paused'},now),'Paused');
 assert.equal(caretakerOutcome({...s,status:'failed'},now),'Needs attention');
 assert.match(caretakerOutcome(s,now+3600000),/Overdue/);
 assert.match(caretakerOutcome({...s,last_attempt:'2099-01-01T00:00:00Z'},now),/Overdue/);
});

test('provider allowance is separate from Codex exhaustion and inaccessible sources',()=>{
 const now=Date.parse('2026-11-08T12:00:00Z');
 assert.equal(researchOutcome({last_attempt:'2026-11-08T11:00:00Z',status:'blocked',reason:'collection_limit'},now),'Waiting — provider collection allowance');
 assert.match(researchAttemptText({outcome:'collection_deferred',attempted_at:'2026-11-08T11:00:00Z'}),/^Research waiting — provider collection allowance/);
});
