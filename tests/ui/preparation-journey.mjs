// A real worker-produced fixture -> real HTTP state -> browser edit/copy/send/reply -> worker rerun.
// No state interception. Only the model and public source reads are scripted; nothing is sent.
import assert from 'node:assert/strict';
import {chromium, webkit, devices} from 'playwright-core';
import {launchBrowser} from '../../tools/browser_launch.mjs';
import {execFileSync, spawn, spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, mkdirSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import net from 'node:net';
const root=join(dirname(fileURLToPath(import.meta.url)),'../..'), py=join(root,'.venv/bin/python');
const freePort=()=>new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
for (const [name,type,mobile] of [['chromium',chromium,false],['webkit',webkit,true]]) {
  if (!(process.env.WO_UI_BROWSERS||'chromium,webkit').split(',').includes(name)) continue;
  const dir=mkdtempSync(join(tmpdir(),'wo-preparation-journey-')), out=join(dir,'evaluation');
  execFileSync(py,['tools/preparation_eval.py','--output',out,'--scenario','normal'],{cwd:root});
  const report=JSON.parse(readFileSync(join(out,'report.json'),'utf8')), scenario=report.scenarios[0];
  assert.equal(scenario.passed,true);
  const database=scenario.database, person=scenario.rows.find(r=>r.case.name==='Alex Evaluation');
  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  const server=spawn(py,['workspace/server.py','--db',database,'--port',String(port)],{cwd:root,stdio:'ignore'});
  let browser;
  try {
    let ready=false;
    for(let i=0;i<100&&!ready;i++){try{ready=(await fetch(base+'/api/health')).ok;}catch{}if(!ready)await new Promise(r=>setTimeout(r,100));}
    assert.ok(ready,'local fixture server starts');
    browser=await launchBrowser(type, !mobile&&process.env.WO_UI_CHROMIUM?{executablePath:process.env.WO_UI_CHROMIUM}:{});
    const context=await browser.newContext(mobile?devices['iPhone 13']:{viewport:{width:1300,height:950}});
    await context.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__copied=text;}}}));
    const page=await context.newPage(), errors=[];
    let expectedFailure=false, capturing=false;
    const captureWarnings=[];
    page.on('pageerror',error=>errors.push(error.message));page.setDefaultTimeout(10000);
    page.on('console',msg=>{
      if(msg.type()!=='error')return;
      // WebKit screenshot instrumentation injects a stylesheet; the site's CSP rightly refuses it.
      if(mobile&&capturing&&msg.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'")){
        captureWarnings.push(msg.text());return;
      }
      if(!(expectedFailure&&msg.text().includes('Failed to load resource')))errors.push(msg.text());
    });
    const capture=async options=>{capturing=true;try{await page.screenshot(options);}finally{capturing=false;}};
    const state=async()=>await(await fetch(base+'/api/state')).json();
    const notice=async (text,keep=false)=>{await page.waitForFunction(t=>!document.querySelector('#notice').hidden&&document.querySelector('#notice').textContent.includes(t),text);if(!keep)await page.evaluate(()=>{document.querySelector('#notice').hidden=true;});};
    await page.goto(base+'/#week');
    await page.waitForSelector('.week-preparation-summary');
    await page.locator('.week-preparation-summary > summary').click();
    let s=await state();
    assert.equal(s.research_processing.preparation.saved_this_week,5,'real worker receipt reaches state');
    for(const row of scenario.rows)assert.equal(s.drafts.find(d=>d.record_id===row.case.id).text,row.saved);
    const action=s.intelligence.actions.find(a=>a.record_id===person.case.id);
    const card=page.locator(`.week-list .approach-card[data-action-id="${action.id}"]`);
    await card.locator('.week-preparation > summary').click();
    const note=card.locator('textarea[name=message]');
    assert.equal(await note.inputValue(),person.saved,'saved worker output appears verbatim');
    assert.match(await page.locator('.preparation-status').innerText(),/route is unverified|route unverified/i);
    if(process.env.WO_PREPARATION_CAPTURE_DIR){mkdirSync(process.env.WO_PREPARATION_CAPTURE_DIR,{recursive:true});await capture({caret:'initial',path:join(process.env.WO_PREPARATION_CAPTURE_DIR,`prepared-${name}.png`),fullPage:true});}
    let edited=person.saved.replace('Thanks,\nMorgan',
      'I can help with:\n- Engineering leadership\n- Product engineering\n- Delivery improvement\n\nBest,\nMorgan');
    await note.fill(edited);
    const plain=edited.replace(/\*\*([^*\n]+)\*\*/g,'$1');
    const added=['Engineering leadership','Product engineering','Delivery improvement'];
    const expectReading=async (root,label)=>{
      const items=await root.locator('li').allInnerTexts();
      for (const line of added) assert.ok(items.includes(line),`${label}: ${items.join(' | ')}`);
      assert.match(await root.innerText(),/portfolio\.example\.com/,label);
      assert.equal(await root.locator('p').first().evaluate(el=>getComputedStyle(el).whiteSpace),'pre-wrap',label);
    };
    const preview=card.locator('.note-preview');
    if (await preview.getAttribute('open')===null) await card.getByText('Preview message',{exact:true}).click();
    await expectReading(card.locator('.note-preview-text'),'preview');
    const before=s.outcomes.length;
    await card.getByRole('button',{name:'Copy',exact:true}).click();
    await notice('Copied and saved');
    assert.equal(await page.evaluate(()=>window.__copied),plain);
    s=await state();
    assert.equal(s.outcomes.length,before,'Copy is not a send');
    const draft=s.drafts.find(d=>d.record_id===person.case.id);
    assert.equal(draft.text,edited);assert.equal(draft.actor,'owner');
    // A draft save rebuilds the card. A subsequent outcome conflict must still be visible.
    edited=edited.replace('Best,\nMorgan','Thanks again,\nMorgan');
    await card.locator('textarea[name=message]').fill(edited);
    expectedFailure=true;
    await page.route('**/api/records/*/outcome',route=>route.fulfill({
      status:409,contentType:'application/json',body:JSON.stringify({error:'Synthetic stale send. Reload and try again.'}),
    }),{times:1});
    await card.getByRole('button',{name:action.progress.primary.label,exact:true}).click();
    await notice('Not saved: This record changed elsewhere');
    expectedFailure=false;
    s=await state();
    assert.equal(s.outcomes.length,before,'failed outcome creates no send');
    assert.equal(s.drafts.find(d=>d.record_id===person.case.id).text,edited,'saved wording survives failed outcome');
    assert.equal(await card.locator('textarea[name=message]').inputValue(),edited);
    await card.getByRole('button',{name:action.progress.primary.label,exact:true}).click();
    await notice('recorded, including your saved wording',true);
    s=await state();
    const sent=s.outcomes.find(o=>o.record_id===person.case.id&&o.kind==='sent');
    assert.equal(sent.snapshot.message,edited,'send keeps exactly the human-edited version');
    assert.equal(await card.evaluate(el=>el.closest('[data-week-group]').dataset.weekGroup),'waiting');
    assert.equal(await card.locator('.week-progress').evaluate(el=>el.open),false);
    assert.equal(await card.locator('textarea').count(),0,'waiting content is created only on expansion');
    assert.match(await card.locator('.chip').innerText(),/Sent · waiting for a reply/);
    await page.waitForFunction(()=>document.activeElement?.classList.contains('approach-name'));
    assert.ok(await card.locator('.approach-name').evaluate(el=>{
      const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;
    }),'the changed card remains in view');
    assert.equal(await card.locator('.approach-name').evaluate(el=>el===document.activeElement),true);
    if(process.env.WO_PREPARATION_CAPTURE_DIR)await capture({caret:'initial',path:join(process.env.WO_PREPARATION_CAPTURE_DIR,`waiting-${name}.png`)});
    await card.locator('.week-progress > summary').click();
    await card.getByRole('button',{name:'They replied',exact:true}).waitFor();
    await page.locator('#notice').getByRole('button',{name:'View sent message',exact:true}).click();
    await page.waitForSelector('#detail[open] .approach-sent[open]');
    await expectReading(page.locator('#detail .approach-sent .message-reading'),'receipt opens the saved message');
    await page.locator('#close').click();
    await card.getByText('View sent message',{exact:true}).click();
    await expectReading(card.locator('.approach-sent .message-reading'),'the card shows the saved message');
    if(process.env.WO_PREPARATION_CAPTURE_DIR)await capture({caret:'initial',path:join(process.env.WO_PREPARATION_CAPTURE_DIR,`sent-${name}.png`)});
    await page.reload();await page.waitForSelector('.week-preparation-summary');
    await page.locator('.week-preparation-summary > summary').click();
    await card.locator('.week-progress > summary').click();
    await card.getByRole('button',{name:'They replied',exact:true}).waitFor();
    assert.equal(await card.getByRole('button',{name:'They replied',exact:true}).isVisible(),true);
    await card.getByText('View sent message',{exact:true}).click();
    await expectReading(card.locator('.approach-sent .message-reading'),'sent wording survives reload');
    const block=card;
    await block.getByLabel('Note for this step',{exact:true}).fill('Arrange a short call about the synthetic migration.');
    const reply=block.getByRole('button',{name:'They replied',exact:true});
    if(!await reply.isVisible())await block.locator('.approach-more > summary').click();
    await reply.click();await notice('Saved');
    await page.waitForFunction(()=>document.activeElement?.classList.contains('approach-name'));
    assert.ok(await card.locator('.approach-name').evaluate(el=>{
      const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;
    }),'recording a reply also keeps the changed card in view');
    if(process.env.WO_PREPARATION_CAPTURE_DIR)await capture({caret:'initial',path:join(process.env.WO_PREPARATION_CAPTURE_DIR,`replied-${name}.png`)});
    s=await state();
    assert.ok(s.outcomes.some(o=>o.record_id===person.case.id&&o.kind==='replied'));
    assert.equal(s.outcomes.find(o=>o.record_id===person.case.id&&o.kind==='replied').note,'Arrange a short call about the synthetic migration.');
    assert.equal(s.intelligence.actions.find(a=>a.id===action.id).progress.status,'replied');
    assert.equal(await card.evaluate(el=>el.closest('[data-week-group]').dataset.weekGroup),'active');
    await card.locator('.week-progress > summary').click();
    assert.equal(await block.getByLabel('Follow-up to send',{exact:true}).count(),0,'reply removes the first-message follow-up');
    // Run the actual worker again, forbidding any new preparation request for an existing note.
    const rerun=JSON.parse(execFileSync(py,['-c',`import json,sys
sys.path[:0]=['tools','workspace']
from unittest.mock import patch
import preparation_eval as ev
import research_worker as w
import research_model as m
import research_cache as cache
provider=ev.ScriptedModel('normal')
def exchange(request):
    if request['operation']=='preparation': raise AssertionError('Existing notes must not regenerate')
    return provider(request)
with patch.object(m,'exchange',side_effect=exchange),patch.object(cache,'read_source',side_effect=ev.source_reader('normal')):
    print(json.dumps(w.run(__import__('pathlib').Path(sys.argv[1]),__import__('pathlib').Path(sys.argv[2]),None,dict(ev.LIMITS))))
`,database,join(out,'normal')],{cwd:root,encoding:'utf8'}));
    assert.equal(rerun.preparation.people.find(p=>p.id===person.case.id).state,'in_progress');
    s=await state();assert.equal(s.drafts.find(d=>d.record_id===person.case.id).text,edited);
    assert.equal(s.outcomes.find(o=>o.record_id===person.case.id&&o.kind==='sent').snapshot.message,edited);
    await page.goto(base+'/#week');await page.waitForSelector('.week-preparation-summary');
    await page.locator('.week-preparation-summary > summary').click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no horizontal overflow');

    // The review artifact itself renders on both devices and exports text-bound human reviews.
    await page.goto('file://'+join(out,'review.html'));
    await page.waitForSelector('fieldset');
    assert.equal(await page.locator('input[type=checkbox]:checked').count(),0,'reviews are never preselected');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'review pack fits the viewport');
    const fields=page.locator('fieldset');assert.equal(await fields.count(),5);
    for(let i=0;i<5;i++){
      await fields.nth(i).getByLabel('Every factual claim is supported').check();
      await fields.nth(i).getByLabel('The ask is appropriate').check();
      if(i<4)await fields.nth(i).getByLabel('I would send this after minor edits').check();
      await fields.nth(i).getByLabel('Editing minutes').fill('1');
    }
    const downloaded=page.waitForEvent('download');
    await page.getByRole('button',{name:'Download my review',exact:true}).click();
    const download=await downloaded, reviews=join(dir,'review.json');await download.saveAs(reviews);
    const reviewed=JSON.parse(readFileSync(reviews,'utf8'));
    assert.equal(reviewed.reviews.length,5);
    assert.equal(reviewed.reviews[0].saved_sha256,scenario.rows[0].saved_sha256);
    const score=spawnSync(py,['tools/preparation_eval.py','--score',join(out,'report.json'),'--reviews',reviews],{cwd:root,encoding:'utf8'});
    assert.equal(score.status,2,'even accepted synthetic notes cannot pass real-model usefulness');
    assert.equal(JSON.parse(score.stdout).usefulness,'pending');
    if(process.env.WO_PREPARATION_CAPTURE_DIR){await page.evaluate(()=>scrollTo(0,0));await capture({caret:'initial',path:join(process.env.WO_PREPARATION_CAPTURE_DIR,`review-${name}.png`)});}
    // Attended comparisons share the form, but their pass must never imply worker quality.
    const input=join(dir,'comparison.json'), attended=join(dir,'attended');
    writeFileSync(input,JSON.stringify({cases:scenario.rows.map(r=>({
      id:r.case.id,name:r.case.name,revision:1,
      baseline:{text:r.saved,ask:r.case.ask,version_id:r.draft_version,actor:'agent'},
      candidate:r.saved,proposed_ask:r.case.ask,ask_reason:'Keep the synthetic ask.',
      evidence:['Synthetic source only'],gaps:['Current availability unknown'],
      factors:{context:'Same fixture',ask:'Unchanged',wording:'Comparison form test'}
    }))}));
    execFileSync(py,['tools/preparation_eval.py','--mode','attended','--comparison',input,'--output',attended],{cwd:root});
    await page.goto('file://'+join(attended,'review.html'));
    assert.equal(await page.getByRole('heading',{name:'Current note',exact:true}).count(),5);
    assert.equal(await page.locator('input:checked').count(),0);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'attended pack fits viewport');
    for(let i=0;i<5;i++){
      const f=page.locator('fieldset').nth(i);
      await f.getByLabel('Every factual claim is supported').check();
      await f.getByLabel('The ask is appropriate').check();
      if(i<4)await f.getByLabel('I would send this after minor edits').check();
      await f.getByLabel('Editing minutes').fill('2');
    }
    const attendedDownload=page.waitForEvent('download');
    await page.getByRole('button',{name:'Download my review',exact:true}).click();
    const attendedReviews=join(dir,'attended-review.json');
    await(await attendedDownload).saveAs(attendedReviews);
    const attendedScore=spawnSync(py,['tools/preparation_eval.py','--score',join(attended,'report.json'),'--reviews',attendedReviews],{cwd:root,encoding:'utf8'});
    assert.equal(attendedScore.status,0,attendedScore.stderr);
    assert.equal(JSON.parse(attendedScore.stdout).scope,'attended comparison only');
    assert.equal(JSON.parse(attendedScore.stdout).worker_quality,'not evaluated');
    if(process.env.WO_PREPARATION_CAPTURE_DIR){await page.evaluate(()=>scrollTo(0,0));await capture({caret:'initial',path:join(process.env.WO_PREPARATION_CAPTURE_DIR,`attended-${name}.png`)});}
    assert.deepEqual(errors,[]);
    if(captureWarnings.length)console.log(`NOTE ${name}: ${captureWarnings.length} capture-only CSP warnings; app-flow console is clean.`);
    console.log(`PASS preparation journey ${name}: worker → API → review/edit/copy → send snapshot → reply → no regeneration; review pack export/scoring`);
  } finally {
    if(browser)await browser.close();server.kill();await new Promise(resolve=>server.once('exit',resolve));
    rmSync(dir,{recursive:true,force:true});
  }
}
