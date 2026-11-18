import assert from 'node:assert/strict';
import {chromium,webkit,devices} from 'playwright-core';
import {launchBrowser} from '../../tools/browser_launch.mjs';
import {createBrowserEvidence} from './browser-evidence.mjs';
import {execFileSync,spawn} from 'node:child_process';
import {mkdtempSync,rmSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import net from 'node:net';
import {decodeSummary} from '../../workspace/js/summary-transport.js';
const root=join(dirname(fileURLToPath(import.meta.url)),'../..'),py=join(root,'.venv/bin/python');
const tmp=mkdtempSync(join(tmpdir(),'wo-email-ui-')),database=join(tmp,'fixture.db');
execFileSync(py,[join(root,'tools/make_fixture.py'),database]);
const port=await new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
const base=`http://127.0.0.1:${port}`;
const server=spawn(py,[join(root,'workspace/server.py'),'--db',database,'--port',String(port)],{stdio:'ignore'});
try {
 for(let i=0;i<100;i++){try{if((await fetch(base+'/api/health')).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
 for(const [name,type] of [['chromium',chromium],['webkit',webkit]]){
  if(!(process.env.WO_UI_BROWSERS||'chromium,webkit').includes(name))continue;
  const browser=await launchBrowser(type, name==='chromium'&&process.env.WO_UI_CHROMIUM?{executablePath:process.env.WO_UI_CHROMIUM}:{});
  const evidence=createBrowserEvidence({suite:'email-status',browser:name});let failure;
  try {
   const page=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(page.context());
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   let status={status:'not_started'};
   await page.route('**/api/state*',async route=>{const res=await route.fetch();const data=decodeSummary(await res.json());data.email_processing=status;await route.fulfill({response:res,json:data});});
   const show=async expected=>{await page.goto(base+'/#dashboard');await page.waitForFunction(value=>document.querySelector('.email-processing strong')?.textContent.includes(value),expected);assert.ok((await page.locator('.email-processing strong').innerText()).includes(expected));};
   await show('Not started');
   const now=new Date().toISOString();
   status={status:'ok',last_attempt:now,last_success:now,collection:{pending:0},processing:{messages:0,jobs:0,eligible:0,quarantined:0}};
   await page.reload();await page.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('Waiting for first'));
   status={...status,last_alert_processed:now,last_received_at:now,processing:{messages:1,jobs:6,eligible:2,quarantined:0}};
   await page.reload();await page.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('Working'));
   await page.getByText('Email delivery details',{exact:true}).click();
   assert.match(await page.locator('.email-processing').innerText(),/1 alert emails processed, 6 distinct adverts parsed, 2 matched/);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   if(process.env.WO_EMAIL_STATUS_CAPTURE_DIR){mkdirSync(process.env.WO_EMAIL_STATUS_CAPTURE_DIR,{recursive:true});await page.screenshot({path:join(process.env.WO_EMAIL_STATUS_CAPTURE_DIR,`email-${name}.png`)});}
   status={...status,processing:{messages:0,jobs:0,eligible:0,quarantined:0},last_import:{finished_at:now,messages:1,jobs:6,eligible:6,admitted:0,allowance_remaining:0,deferred:{seven_day_limit:6}}};
   await page.reload();await page.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('6 adverts deferred'));
   await page.getByText('Email delivery details',{exact:true}).click();
   const held=await page.locator('.email-processing').innerText();
   assert.match(held,/Latest run: 0 alert emails processed/);
   assert.match(held,/Last alert import.*6 distinct adverts parsed, 6 matched your filters, 0 added; 6 deferred \(6 seven-day limit\)/);
   assert.match(held,/Automatic addition allowance remaining at that import: 0/);
   assert.doesNotMatch(held,/Storage nearly full/);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   status={...status,status:'failed'};await page.reload();await page.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('Failed'));
   status={...status,status:'ok',last_attempt:new Date(Date.now()-3600000).toISOString()};await page.reload();await page.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('Overdue'));
   assert.deepEqual(errors,[]);console.log(`ok email status ${name}: never run, empty, success, failure, overdue and layout`);
   status={...status,status:'ok',last_attempt:now,sources:{
    'alert:linkedin':{label:'LinkedIn email',state:'ok',last_alert_processed:now,messages:1,jobs:3,eligible:2,quarantined:0},
    'alert:jackandjill':{label:'Jack and Jill email',state:'error',last_alert_processed:now,messages:0,jobs:0,eligible:0,quarantined:1}}};
   await page.reload();await page.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('Incomplete'));
   await page.getByText('Email delivery details',{exact:true}).click();
   const mixed=await page.locator('.email-processing').innerText();
   assert.match(mixed,/LinkedIn email: Working/);
   assert.match(mixed,/Jack and Jill email: Incomplete/);
   assert.match(mixed,/1 retained for investigation/);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   assert.deepEqual(errors,[]);console.log(`ok email status ${name}: separate sender failures and bounded layout`);
   // A page left open re-reads run status alone: the false alarm clears, typed text and open sections stay.
   const open=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(open.context());
   const openErrors=[];open.on('pageerror',e=>openErrors.push(e.message));
   const loadedAt=Date.now(),old=new Date(loadedAt-60*60000).toISOString();
   const stale={status:'ok',last_attempt:old,last_success:old,last_alert_processed:old,collection:{pending:0},processing:{messages:0,jobs:0,eligible:0,quarantined:0}};
   let fresh=stale,reads=0;
   await open.clock.install({time:loadedAt});
   await open.route('**/api/state*',async route=>{const res=await route.fetch();const data=decodeSummary(await res.json());data.email_processing=stale;await route.fulfill({response:res,json:data});});
   await open.route('**/api/status',async route=>{reads++;const res=await route.fetch();const data=decodeSummary(await res.json());data.email_processing=fresh;await route.fulfill({response:res,json:data});});
   await open.goto(base+'/#dashboard');await open.waitForSelector('.email-processing');
   assert.match(await open.locator('.email-processing strong').innerText(),/Overdue/);
   await open.getByText('Email delivery details',{exact:true}).click();
   await open.getByText('Review unassessed email leads',{exact:true}).click();
   await open.getByRole('searchbox',{name:'Search email leads'}).fill('Keep this search');
   await open.locator('#new-record').click();
   await open.locator('#modal-body [name=name]').fill('Typed before the refresh');
   const later=new Date(loadedAt+6*60000).toISOString();
   fresh={...stale,last_attempt:later,last_success:later,last_alert_processed:later};
   await open.clock.runFor(6*60000+1000);
   await open.waitForFunction(()=>document.querySelector('.email-processing strong')?.textContent.includes('Working'),null,{polling:100});
   assert.equal(reads,1);
   assert.equal(await open.locator('#modal-body [name=name]').inputValue(),'Typed before the refresh');
   assert.equal(await open.getByRole('searchbox',{name:'Search email leads'}).inputValue(),'Keep this search');
   assert.ok(await open.locator('.email-processing details').filter({has:open.getByText('Email delivery details',{exact:true})}).evaluate(d=>d.open));
   assert.ok(await open.locator('[data-email-leads]').evaluate(d=>d.open));
   assert.doesNotMatch(await open.locator('.email-processing').evaluate(n=>n.textContent),new RegExp(new Date(old).toLocaleString('en-GB',{hour:'2-digit',minute:'2-digit'})));
   await open.evaluate(()=>{window.dispatchEvent(new Event('focus'));document.dispatchEvent(new Event('visibilitychange'));});
   await open.clock.runFor(1000);
   assert.equal(reads,1,'a return just after a read does not read again');
   assert.deepEqual(openErrors,[]);await open.close();console.log(`ok email status ${name}: an open page refreshes run status without losing typed text`);
   const week=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(week.context());
   const weekErrors=[];week.on('pageerror',e=>weekErrors.push(e.message));
   const checked=at=>({status:'ok',last_attempt:at,finished_at:at,checks:[]});
   let caretaker=checked(old);
   await week.clock.install({time:loadedAt});
   await week.route('**/api/state*',async route=>{const res=await route.fetch();const data=decodeSummary(await res.json());data.caretaker_processing=checked(old);await route.fulfill({response:res,json:data});});
   await week.route('**/api/status',async route=>{const res=await route.fetch();const data=decodeSummary(await res.json());data.caretaker_processing=caretaker;await route.fulfill({response:res,json:data});});
   await week.goto(base+'/#dashboard');await week.waitForSelector('.caretaker-processing');
   await week.getByText('Caretaker checks',{exact:true}).click();
   assert.match(await week.locator('.caretaker-processing strong').innerText(),/Overdue/);
   caretaker=checked(new Date(loadedAt+30000).toISOString());
   await week.evaluate(()=>window.dispatchEvent(new Event('focus')));
   await week.clock.runFor(31000);await week.evaluate(()=>window.dispatchEvent(new Event('focus')));
   await week.waitForFunction(()=>document.querySelector('.caretaker-processing strong')?.textContent.includes('Checks completed'),null,{polling:100});
   assert.ok(await week.locator('.caretaker-processing details').evaluate(d=>d.open),'the section the owner opened stays open');
   assert.deepEqual(weekErrors,[]);await week.close();console.log(`ok caretaker status ${name}: returning to an open page refreshes it in place`);
  } catch(error){failure=error;throw error;} finally {await evidence.finish({error:failure});await browser.close();}
 }
} finally {server.kill();await new Promise(r=>server.once('exit',r));rmSync(tmp,{recursive:true,force:true});}
