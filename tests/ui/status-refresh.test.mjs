import test from 'node:test';
import assert from 'node:assert/strict';
import {statusReadDue, RETURN_GAP_MS, VISIBLE_GAP_MS} from '../../workspace/js/status-refresh.js';
const read=Date.parse('2026-10-28T12:20:00Z');
test('a visible page re-reads run status every five minutes, never while hidden',()=>{
 assert.equal(VISIBLE_GAP_MS,300000);
 assert.equal(statusReadDue(read,read+299999),false);
 assert.equal(statusReadDue(read,read+300000),true);
 assert.equal(statusReadDue(read,read+80*60000,{hidden:true}),false);
});
test('returning to a long-open page re-reads at once, but not twice for one return',()=>{
 assert.equal(RETURN_GAP_MS,30000);
 assert.equal(statusReadDue(read,read+80*60000,{returning:true}),true);
 assert.equal(statusReadDue(read,read+29999,{returning:true}),false);
 assert.equal(statusReadDue(read,read+30000,{returning:true}),true);
 assert.equal(statusReadDue(read,read+80*60000,{returning:true,hidden:true}),false);
});
