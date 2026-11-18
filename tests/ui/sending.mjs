// Real browser + canonical writes on disposable synthetic data. No external app is contacted.
import assert from 'node:assert/strict';
import {chromium,webkit,devices} from 'playwright-core';
import {launchBrowser} from '../../tools/browser_launch.mjs';
import {createBrowserEvidence} from './browser-evidence.mjs';
import {execFileSync,spawn} from 'node:child_process';
import {mkdtempSync,rmSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
const root=join(dirname(fileURLToPath(import.meta.url)),'../..'),python=join(root,'.venv/bin/python');
const wanted=(process.env.WO_UI_BROWSERS||'chromium,webkit').split(',');
async function run(type,mobile){
  const label=mobile?'iPhone WebKit':'desktop Chromium',dir=mkdtempSync(join(tmpdir(),'wo-sending-'));
  const server=spawn(python,[join(root,'tests/ui/visual_server.py'),join(dir,'fixture.db')],{stdio:['ignore','pipe','pipe']});
  let browser,error,stderr='';server.stderr.on('data',b=>stderr+=b);
  const evidence=createBrowserEvidence({suite:'sending',browser:label});
  try{
    const base=await new Promise((resolve,reject)=>{
      let text='';const timer=setTimeout(()=>reject(new Error(stderr||'Fixture startup timed out')),15000);
      server.stdout.on('data',b=>{text+=b;const m=text.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});
      server.once('error',reject);server.once('exit',()=>{clearTimeout(timer);reject(new Error(stderr));});
    });
    browser=await launchBrowser(type,!mobile&&process.env.WO_UI_CHROMIUM?{executablePath:process.env.WO_UI_CHROMIUM}:{});
    const context=await browser.newContext(mobile?devices['iPhone 13']:{viewport:{width:1300,height:950}});
    await evidence.attach(context);
    await context.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__copied=text;}}}));
    const page=await context.newPage();page.setDefaultTimeout(10000);
    const errors=[],writes=[],requests=[];page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{requests.push(r.url());if(r.method()==='POST'&&r.url().includes('/api/records/'))writes.push(JSON.parse(r.postData()));});
    let acceptDialogs=true;
    page.on('dialog',d=>acceptDialogs?d.accept():d.dismiss());
    const read=async()=>await (await fetch(base+'/api/state')).json();
    let initial=await read(),alex=initial.records.find(r=>r.name==='Alex Example');
    await fetch(base+`/api/records/${alex.id}`,{method:'PATCH',headers:{'Content-Type':'application/json',Origin:base},
      body:JSON.stringify({revision:alex.revision,url:'https://www.linkedin.com/in/synthetic-example',phone:'+447700900123'})});
    initial=await read();alex=initial.records.find(r=>r.name==='Alex Example');
    const step=initial.intelligence.actions.find(a=>a.record_id===alex.id)?.progress.actions.find(a=>a.method==='next_step');
    assert.ok(step);
    const agreed=await fetch(base+`/api/records/${alex.id}/next-step`,{method:'POST',headers:{'Content-Type':'application/json',Origin:base},
      body:JSON.stringify({revision:alex.revision,action_id:step.action_id,next_step:{kind:'mine',text:'Read the agreed role description.',agreed_on:''}})});
    assert.equal(agreed.status,200);initial=await read();alex=initial.records.find(r=>r.name==='Alex Example');
    for(let n=0;n<22;n++){
      const response=await fetch(base+'/api/records',{method:'POST',headers:{'Content-Type':'application/json',Origin:base},
        body:JSON.stringify({type:'person',name:`Batch Candidate ${String(n).padStart(2,'0')}`,relationship:'warm',kind:'former_colleague',priority:'next'})});
      assert.equal(response.status,200);
    }
    const activeOutcomes=async id=>(await read()).outcomes.filter(o=>o.record_id===id&&!o.corrected_at);
    const before=(await activeOutcomes(alex.id)).length;
    await page.goto(base+'/#people');await page.getByRole('button',{name:'Send messages',exact:true}).waitFor();
    assert.equal(requests.some(u=>u.endsWith('/api/message-templates')),false,'templates absent from ordinary navigation');
    await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/message-templates')),page.getByRole('button',{name:'Send messages',exact:true}).click()]);
    const modal=page.locator('#modal'),submit=modal.locator('[type=submit]');
    await modal.getByLabel('Find people for this batch').fill('Batch Candidate');
    assert.equal(await modal.locator('.sending-choice').count(),20);
    for(let n=0;n<10;n++)await modal.getByLabel(`Batch Candidate ${String(n).padStart(2,'0')}`,{exact:true}).check();
    assert.equal(await modal.getByLabel('Batch Candidate 10',{exact:true}).isDisabled(),true,'batch capped at ten');
    for(let n=0;n<10;n++)await modal.getByLabel(`Batch Candidate ${String(n).padStart(2,'0')}`,{exact:true}).uncheck();
    await modal.getByLabel('Find people for this batch').fill('Alex Example');
    await modal.getByLabel('Alex Example',{exact:true}).check();
    await modal.getByLabel('Find people for this batch').fill('Riley Recruiter');
    await modal.getByLabel('Riley Recruiter',{exact:true}).check();await submit.click();
    const text=modal.getByLabel('Message to personalise'),channel=modal.getByLabel('Channel',{exact:true});
    assert.equal(await modal.locator('h3').innerText(),'Alex Example');
    await channel.selectOption('linkedin');await modal.getByLabel('Template',{exact:true}).selectOption('former_colleague');
    await modal.getByRole('button',{name:'Use template',exact:true}).click();
    assert.match(await text.inputValue(),/^Hi Alex,/);assert.match(await text.inputValue(),/\{\{personal_line\}\}/);
    assert.equal(await modal.getByRole('button',{name:'Copy message',exact:true}).isDisabled(),true);
    assert.equal(await modal.locator('.sending-open').getAttribute('href'),null);
    const wording='Hi Alex,\n\nIt was good working together on the migration. Fancy a catch-up?\n\nMorgan';
    await text.fill(wording);await modal.getByRole('button',{name:'Copy message',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.__copied),wording);assert.equal(writes.length,0,'copy never logs contact');
    await page.evaluate(()=>{navigator.clipboard.writeText=async()=>{throw new Error('Synthetic clipboard failure');};});
    await modal.getByRole('button',{name:'Copy message',exact:true}).click();
    assert.match(await modal.locator('.sending-status').innerText(),/message is selected/);
    assert.equal(await text.evaluate(n=>n.value.slice(n.selectionStart,n.selectionEnd)),wording);
    acceptDialogs=false;await modal.locator('#modal-close').click();assert.equal(await modal.isVisible(),true);acceptDialogs=true;
    assert.equal(await modal.locator('.sending-open').getAttribute('href'),alex.url);
    const routeReads=requests.length;
    await modal.getByRole('button',{name:'Skip for now',exact:true}).click();
    assert.equal(await modal.locator('h3').innerText(),'Riley Recruiter');assert.equal(await text.inputValue(),'');
    await text.fill('Hi Riley, a separate personal message.');await modal.getByRole('button',{name:'Previous',exact:true}).click();
    assert.equal(await text.inputValue(),wording);assert.equal(requests.length,routeReads,'batch navigation needs no server requests');
    assert.equal(await modal.evaluate(n=>n.scrollWidth>n.clientWidth+1),false,'no horizontal overflow');
    const targets=await modal.locator('.sending-actions button').evaluateAll(nodes=>nodes.map(n=>n.getBoundingClientRect().height));
    assert.ok(targets.every(h=>h>=44),'touch targets at least 44px');
    if(process.env.SCREENSHOT_DIR){mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});await modal.evaluate(n=>n.scrollTop=0);await page.screenshot({path:join(process.env.SCREENSHOT_DIR,`sending-${mobile?'iphone':'desktop'}.png`)});}
    const path=`**/api/records/${alex.id}/outcome`;
    await page.route(path,r=>r.fulfill({status:503,json:{error:'Synthetic save unavailable'}}));
    await submit.click();await modal.locator('.sending-status').filter({hasText:'Synthetic save unavailable'}).waitFor();
    assert.equal(await modal.locator('h3').innerText(),'Alex Example');assert.equal(await text.inputValue(),wording);
    assert.equal((await activeOutcomes(alex.id)).length,before,'failed write records nothing');await page.unroute(path);
    // A real concurrent edit must conflict; explicit refresh keeps the prepared wording.
    const change=await fetch(base+`/api/records/${alex.id}`,{method:'PATCH',headers:{'Content-Type':'application/json',Origin:base},
      body:JSON.stringify({revision:alex.revision,summary:'A newer synthetic relationship note.'})});assert.equal(change.status,200);
    await submit.click();await modal.locator('.sending-status').filter({hasText:/changed|reload|revision/i}).waitFor();
    assert.equal(await text.inputValue(),wording);assert.equal((await activeOutcomes(alex.id)).length,before);
    await modal.getByRole('button',{name:'Refresh contact after a conflict',exact:true}).click();
    await modal.locator('.sending-status').filter({hasText:'Contact refreshed'}).waitFor();assert.equal(await text.inputValue(),wording);
    // Hold the real request while checking controls; a second submit must not add a duplicate.
    let release;const hold=new Promise(r=>release=r);await page.route(path,async r=>{await hold;await r.continue();});
    const start=performance.now(),sentBefore=writes.length;
    await submit.click();await page.waitForFunction(()=>document.querySelector('#modal-form').getAttribute('aria-busy')==='true');
    assert.equal(await channel.isDisabled(),true);assert.equal(await text.isDisabled(),true);
    await modal.locator('form').evaluate(f=>f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    release();await modal.locator('h3').filter({hasText:'Riley Recruiter'}).waitFor();
    await modal.locator('form').evaluate(f=>f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    assert.equal(writes.length,sentBefore+1,'a trailing double-tap cannot log the next person');
    assert.equal(await modal.locator('h3').evaluate(n=>n===document.activeElement),true,'next recipient receives focus');
    console.log(`sending (${label}): save and advance ${Math.round(performance.now()-start)} ms`);
    assert.equal(writes.length,sentBefore+1);assert.equal(writes.at(-1).sent_message,undefined);assert.equal(writes.at(-1).next_step,undefined);
    assert.equal((await activeOutcomes(alex.id)).length,before+1);assert.equal(await text.inputValue(),'Hi Riley, a separate personal message.');
    assert.equal((await read()).intelligence.actions.find(a=>a.record_id===alex.id).progress.next_step.text,'Read the agreed role description.');
    await page.unroute(path);
    await modal.getByRole('button',{name:'Undo send to Alex Example',exact:true}).click();
    await modal.locator('.sending-status').filter({hasText:'Send record undone'}).waitFor();
    assert.equal((await activeOutcomes(alex.id)).length,before);assert.equal(await text.inputValue(),wording);
    await channel.selectOption('instagram');await submit.click();await modal.locator('h3').filter({hasText:'Riley Recruiter'}).waitFor();
    const logged=(await activeOutcomes(alex.id)).at(-1);assert.equal(logged.snapshot.channel,'other');assert.equal(logged.note,'Instagram');
    await modal.getByRole('button',{name:'Finish batch',exact:true}).click();assert.equal(await modal.locator('h3').innerText(),'Batch finished');
    await submit.click();await modal.waitFor({state:'hidden'});
    await page.reload();await page.getByRole('button',{name:'Send messages',exact:true}).waitFor();
    assert.equal((await activeOutcomes(alex.id)).length,before+1,'contact survives reload');
    await page.route('**/api/message-templates',r=>r.fulfill({status:503,json:{error:'Synthetic template failure'}}));
    await page.getByRole('button',{name:'Send messages',exact:true}).click();
    await modal.locator('.sending-status').filter({hasText:'Templates could not load'}).waitFor();
    await modal.getByLabel('Find people for this batch').fill('No such person');
    assert.equal(await modal.locator('.sending-choice').count(),0);assert.equal(await submit.isDisabled(),true);
    await modal.getByLabel('Find people for this batch').fill('Batch Candidate 00');
    await modal.getByLabel('Batch Candidate 00',{exact:true}).check();await submit.click();
    await text.fill('A manual message while templates are unavailable.');
    assert.equal(await modal.getByRole('button',{name:'Use template',exact:true}).isDisabled(),true);
    await page.unroute('**/api/message-templates');await modal.getByRole('button',{name:'Retry templates',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.sending select[aria-label="Template"]').disabled);
    assert.equal(await text.inputValue(),'A manual message while templates are unavailable.');
    const removed=(await read()).records.find(r=>r.name==='Batch Candidate 00');
    const removal=await fetch(base+`/api/records/${removed.id}/transition`,{method:'POST',headers:{'Content-Type':'application/json',Origin:base},
      body:JSON.stringify({revision:removed.revision,field:'relationship',to:'do_not_contact'})});assert.equal(removal.status,200);
    await submit.click();await modal.locator('.sending-status').filter({hasText:'Not saved'}).waitFor();
    await modal.getByRole('button',{name:'Refresh contact after a conflict',exact:true}).click();
    await modal.locator('.sending-status').filter({hasText:'no longer available'}).waitFor();assert.equal(await submit.isDisabled(),true);
    assert.equal(await modal.locator('.sending-open').getAttribute('href'),null);
    await modal.getByRole('button',{name:'Finish batch',exact:true}).click();
    assert.match(await modal.innerText(),/0 contacts recorded/);assert.equal((await activeOutcomes(removed.id)).length,0);
    await submit.click();await modal.waitFor({state:'hidden'});
    assert.deepEqual(errors,[]);console.log(`ok    sending (${label}): templates, channels, per-person drafts, bounded UI, failure, conflict, double tap, Undo, reload`);
    await evidence.finish();
  }catch(e){error=e;await evidence.finish({failed:true,error:e});throw e;}
  finally{if(browser)await browser.close();if(server.exitCode===null){server.kill();await once(server,'exit');}rmSync(dir,{recursive:true,force:true});}
}
if(wanted.includes('chromium'))await run(chromium,false);
if(wanted.includes('webkit'))await run(webkit,true);
