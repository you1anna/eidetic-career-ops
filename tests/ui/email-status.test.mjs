import test from 'node:test';
import assert from 'node:assert/strict';
import {emailOutcome} from '../../workspace/js/email-status.js';
const now=Date.parse('2026-10-17T13:00:00Z');
const ready={last_attempt:'2026-10-17T12:55:00Z',status:'ok',last_alert_processed:'2026-10-17T12:00:00Z'};
test('empty checks cannot prove delivery, while prior delivery survives later empty checks',()=>{
 assert.equal(emailOutcome({},now),'Not started');
 assert.equal(emailOutcome({...ready,last_alert_processed:null},now),'Waiting for first job alert');
 assert.equal(emailOutcome(ready,now),'Working');
});
test('time alone makes a stopped or stalled processor visibly unhealthy',()=>{
 assert.match(emailOutcome(ready,now+50*60*1000),/^Overdue/);
 assert.match(emailOutcome({...ready,status:'running'},now+8*60*1000),/^Processing stalled/);
 assert.match(emailOutcome({...ready,status:'failed'},now),/^Failed/);
 assert.match(emailOutcome({...ready,status:'partial'},now),/^Incomplete/);
 assert.match(emailOutcome({...ready,capacity_warning:true},now),/^Storage nearly full/);
});

test('successful delivery does not hide admission deferrals or confuse them with mail storage',()=>{
 const held={...ready,last_import:{admitted:0,allowance_remaining:0,deferred:{seven_day_limit:6}}};
 assert.equal(emailOutcome(held,now),'Alerts processed — 6 adverts deferred');
 assert.match(emailOutcome({...held,status:'failed'},now),/^Failed/);
 assert.match(emailOutcome({...held,capacity_warning:true},now),/^Storage nearly full/);
 assert.equal(emailOutcome({...ready,last_import:{admitted:2,deferred:{}}},now),'Working');
});

test('one source cannot hide another source failure or delivery silence',()=>{
 const sources={linkedin:{label:'LinkedIn email',state:'ok',last_alert_processed:ready.last_alert_processed},
  jack:{label:'Jack and Jill email',state:'error',quarantined:1,last_alert_processed:null}};
 assert.match(emailOutcome({...ready,sources},now),/Incomplete/);
 sources.jack.state='empty';sources.jack.quarantined=0;
 assert.match(emailOutcome({...ready,sources},now),/Waiting for first alert.*Jack and Jill/);
 sources.jack.last_alert_processed='2026-10-15T10:00:00Z';
 assert.match(emailOutcome({...ready,sources},now),/No recent alerts.*Jack and Jill/);
 sources.jack.last_alert_processed=ready.last_alert_processed;
 assert.equal(emailOutcome({...ready,sources},now),'Working');
});
