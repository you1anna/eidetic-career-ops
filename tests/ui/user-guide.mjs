// Follow USER-GUIDE.md using visible controls on a fresh synthetic database per browser.
// No personal records, external sends or Google Docs reads. Optional captures are local only.
import assert from 'node:assert/strict';
import { chromium, webkit, devices } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { createBrowserEvidence } from './browser-evidence.mjs';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const root = join(dirname(fileURLToPath(import.meta.url)), '../..');
const python = join(root, '.venv/bin/python');
const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
const capture = process.env.WO_GUIDE_CAPTURE_DIR;
const freePort = () => new Promise(resolve => {
  const server = net.createServer();
  server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); });
});

async function run(type, mobile = false) {
  const label = mobile ? 'iPhone WebKit' : 'desktop Chromium';
  const dir = mkdtempSync(join(tmpdir(), 'wo-guide-'));
  const db = join(dir, 'guide.db');
  execFileSync(python, [join(root, 'tools/make_fixture.py'), db]);
  const port = await freePort(), base = `http://127.0.0.1:${port}`;
  const server = spawn(python, [join(root, 'workspace/server.py'), '--db', db, '--port', String(port)], { stdio: 'ignore' });
  const evidence = createBrowserEvidence({ suite: 'user-guide', browser: label });
  let browser, failure;
  try {
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
      await new Promise(r => setTimeout(r, 100));
    }
    browser = await launchBrowser(type, !mobile && process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {});
    const context = await browser.newContext(mobile ? devices['iPhone 13'] : { viewport: { width: 1300, height: 900 } });
    await evidence.attach(context);
    await context.addInitScript(() => Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: async text => { window.__copied = text; } }
    }));
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    const captureWarnings = [];
    let failedSave = false, capturing = false;
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => {
      if (m.type() !== 'error') return;
      // Playwright's WebKit capture injects a stylesheet that the production CSP correctly rejects.
      // Isolate only this exact capture-time warning; all app-flow errors still fail the walkthrough.
      if (mobile && capturing && m.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"))
        captureWarnings.push(m.text());
      else if (!(failedSave && /^Failed to load resource:.*409.*Conflict/.test(m.text()))) errors.push(m.text());
    });
    const state = async () => (await fetch(`${base}/api/state`)).json();
    const content = page.locator('#content');
    const block = () => page.locator('#detail .approach-block');
    const status = () => block().locator('.chip').first().textContent();
    const report = s => console.log(`ok    user guide (${label}): ${s}`);
    async function reveal(target) {
      await target.waitFor({ state: 'attached' });
      while (await target.locator('xpath=ancestor::details[not(@open)]').count())
        await target.locator('xpath=ancestor::details[not(@open)]').first().locator(':scope > summary').click();
    }
    async function saved(click) {
      const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().startsWith(base)
        && !new URL(r.url()).pathname.startsWith('/api/seen/'));
      await click(); await response;
      await page.waitForFunction(() => document.querySelector('#connection-status').textContent !== 'Refreshing…');
    }
    async function step(scope, name) {
      const button = scope.getByRole('button', { name, exact: true, includeHidden: true });
      await reveal(button);
      await saved(() => button.click());
    }
    async function nav(name) {
      await page.locator('#nav').getByRole('button', { name: new RegExp(`^${name}(\\s|$)`) }).click();
      await page.waitForFunction(() => document.querySelector('#content').childElementCount > 0);
    }
    async function screenshot(name) {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'horizontal overflow');
      if (capture) {
        mkdirSync(capture, { recursive: true });
        capturing = true;
        try { await page.screenshot({ path: join(capture, `${name}${mobile ? '-mobile' : ''}.png`), caret: 'initial' }); }
        finally { capturing = false; }
      }
    }
    await page.goto(`${base}/#week`);
    try { await page.waitForSelector('.week-discovery'); }
    catch (error) {
      console.error(JSON.stringify({ url: page.url(), errors,
        visible: (await page.locator('body').innerText()).slice(0, 2500) }));
      throw error;
    }
    assert.equal(await page.title(),'work/ops');assert.equal(await page.locator('#title').textContent(),'This week');
    const initial=await state();
    assert.deepEqual(await content.locator('.week-discovery [data-opportunity-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.opportunityId)),initial.opportunities.discovery.filter(r=>r.worth_a_look).slice(0,3).map(r=>r.record_id));
    assert.equal(await content.locator('.week-focus-card,.week-tally,.week-tools').count(),0);
    await screenshot('this-week');report('This week shows active work and the first three ranked decision briefs with evidence and source');
    await nav('People');await content.locator('.person-row').filter({hasText:'Alex Example'}).click();
    assert.equal(await page.locator('.list-panel').isVisible(),false);
    await reveal(page.locator('.list-panel'));await page.locator('.list-panel select[name=preferred_channel]').selectOption('email');
    const patched=page.waitForResponse(r=>r.request().method()==='PATCH');
    await page.locator('.list-panel').getByRole('button',{name:'Save',exact:true}).click();await patched;
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('place on your list'));
    assert.equal((await state()).records.find(r=>r.name==='Alex Example').preferred_channel,'email');
    await page.locator('#close').click();report('People → Edit details → Your list → Channel → Save');
    await nav('Jobs');
    const job=initial.records.find(r=>r.type==='job'&&r.name==='Interim Engineering Manager'&&r.organisation==='Example Rail Ltd');
    await page.goto(`${base}/#work/${job.id}`);await page.waitForSelector('#detail[open] .job-controls');
    assert.ok(await page.locator('.cv-override').isVisible(),'CV chooser is visible without opening More application options');
    const chooser=page.locator('select[name=cv_override]'),saveCV=page.getByRole('button',{name:'Save CV choice',exact:true});
    const cvLink=page.getByRole('link',{name:'Open CV ↗',exact:true}),cvStatus=page.locator('.cv-status'),explanation=page.locator('.cv-suggestion');
    const expectedWhy='Why suggested: Engineering Manager — title matches “Engineering Manager”. Check this title-based suggestion against the role.';
    assert.equal(await explanation.innerText(),expectedWhy);
    assert.match(await cvStatus.innerText(),/^Engineering Manager · Ready · Suggested/);
    assert.ok(await saveCV.isVisible());assert.ok(await cvLink.isVisible());
    assert.equal(await page.locator('.cv-choice').evaluate(node=>{
      const nodes=[node.querySelector('.cv-override'),node.querySelector('.cv-status'),node.querySelector('.cv-suggestion'),node.querySelector('a')];
      return nodes.every((n,i)=>n&&(!i||Boolean(nodes[i-1].compareDocumentPosition(n)&Node.DOCUMENT_POSITION_FOLLOWING)));
    }),true,'chooser, save, readiness and explanation precede Open CV');
    assert.match(await chooser.locator('option[value=Director]').innerText(),/Draft$/);
    assert.match(await chooser.locator('option[value=Manager]').innerText(),/Ready$/);
    await page.locator('.cv-choice').scrollIntoViewIfNeeded();await screenshot('cv-suggested');
    const patchPath=`/api/records/${job.id}`;
    const patchResponse=()=>page.waitForResponse(r=>new URL(r.url()).pathname===patchPath&&r.request().method()==='PATCH');
    const readJob=async()=>(await state()).records.find(r=>r.id===job.id);
    const currentChoice=async(key,selection)=>{
      await page.waitForFunction(({key,selection})=>document.querySelector('select[name=cv_override]')?.value===key
        &&!document.querySelector('select[name=cv_override]').disabled&&document.querySelector('.cv-status')?.textContent.includes(selection),{key,selection});
    };
    // A delayed response cannot admit a second save or a later chooser edit.
    let releaseSave,startedSave,patches=0;
    const held=new Promise(resolve=>releaseSave=resolve),started=new Promise(resolve=>startedSave=resolve);
    await page.route(`**${patchPath}`,async route=>{
      if(route.request().method()!=='PATCH')return route.continue();
      patches++;startedSave();await held;return route.continue();
    });
    await chooser.selectOption('Director');
    assert.equal(await cvLink.isVisible(),false,'unsaved choice cannot open the previous CV by mistake');
    assert.equal(await page.locator('.cv-override [role=status]').innerText(),'Save your choice to update the CV link.');
    const pending=patchResponse();await saveCV.click();await started;
    assert.equal(await chooser.isDisabled(),true);assert.equal(await saveCV.isDisabled(),true);
    await page.locator('.cv-override').evaluate(form=>form.requestSubmit());
    releaseSave();assert.equal((await pending).status(),200);await currentChoice('Director','Your selection');
    await page.unroute(`**${patchPath}`);assert.equal(patches,1,'pending submit is guarded');
    assert.match(await cvStatus.innerText(),/^Director of Engineering \/ CTO · Draft · Your selection/);
    assert.match(await cvStatus.innerText(),/Review this draft before use/);
    assert.equal(await explanation.innerText(),expectedWhy,'manual choice retains the independent suggestion');
    assert.equal(await cvLink.getAttribute('href'),initial.cvs.Director.url);
    assert.equal((await readJob()).status,job.status);
    await page.locator('.cv-choice').scrollIntoViewIfNeeded();await screenshot('cv-saved-draft');
    await page.locator('#close').click();await page.goto(`${base}/#work/${job.id}`);await currentChoice('Director','Your selection');
    await page.reload();await currentChoice('Director','Your selection');
    // Another editor changes both the selected CV and title; the stale form must preserve its input.
    const beforeConflict=await readJob();
    const changed=await fetch(`${base}${patchPath}`,{method:'PATCH',headers:{Origin:base,'Content-Type':'application/json'},
      body:JSON.stringify({revision:beforeConflict.revision,cv_override:'QE',name:'Forward Deployed Engineer'})});
    assert.equal(changed.status,200);
    await chooser.selectOption('Founding');failedSave=true;
    const stale=patchResponse();await saveCV.click();assert.equal((await stale).status(),409);
    await page.waitForFunction(()=>document.querySelector('.cv-override [role=status]')?.textContent.includes('changed elsewhere'));
    failedSave=false;
    assert.equal(await chooser.inputValue(),'Founding');assert.equal(await chooser.isDisabled(),false);
    assert.equal((await readJob()).cv_override,'QE','stale request cannot overwrite another editor');
    assert.equal(await cvLink.isVisible(),false);
    page.once('dialog',dialog=>dialog.dismiss());await page.locator('#close').click();
    assert.equal(await chooser.inputValue(),'Founding','failed save retains dirty-form protection');
    page.once('dialog',dialog=>dialog.accept());await page.locator('#close').click();
    await page.locator('#reload').click();await page.waitForFunction(()=>document.querySelector('#connection-status')?.textContent!=='Refreshing…');
    await page.goto(`${base}/#work/${job.id}`);await currentChoice('QE','Your selection');
    assert.equal(await explanation.innerText(),'Why suggested: Forward Deployed Engineer — title matches “Forward Deployed”. Check this title-based suggestion against the role.');
    await chooser.selectOption('');const reset=patchResponse();await saveCV.click();assert.equal((await reset).status(),200);
    await currentChoice('','Suggested');
    assert.match(await cvStatus.innerText(),/^Forward Deployed Engineer · Ready · Suggested/);
    assert.equal(await cvLink.getAttribute('href'),initial.cvs.FDE.url);
    const resetJob=await readJob();assert.equal(resetJob.cv_override,null);assert.equal(resetJob.status,job.status);
    assert.equal(await page.locator('#detail .work-assessment').count(),1);await screenshot('job-assessment');
    await page.locator('#close').click();report('Jobs → visible CV choice → saved Draft → reopen/refresh → stale-save recovery → current suggestion reset; application status unchanged');
    const excluded=initial.records.find(r=>r.type==='job'&&r.exclusion);
    await page.goto(`${base}/#work/${excluded.id}`);await page.waitForSelector('#detail[open] .job-controls');
    assert.equal(await page.locator('.cv-override,.cv-suggestion').count(),0,'excluded role retains history without CV selection or suggestion advice');
    await page.locator('#close').click();
    await nav('Dashboard');
    await content.locator('.dashboard-attention .decision-card').filter({hasText:'The daily run cannot read Example Board'}).click();
    await page.locator('.decision-choice').filter({hasText:'Fix it'}).click();
    await saved(()=>page.getByRole('button',{name:'Send: Fix it',exact:true}).click());
    assert.ok((await state()).decisions.some(d=>d.kind==='failure'&&d.answer==='fix'&&d.execution_status==='queued'));
    report('Dashboard → operational failure → Fix it queues agent work');
    assert.equal(await page.locator('textarea[name=message],textarea[name=sent_message],.note-box').count(),0);
    assert.deepEqual(errors, []);
    await screenshot('walkthrough-final');
    assert.deepEqual(errors, []);
    if (captureWarnings.length) console.log(`note  ${label}: ${captureWarnings.length} capture-only WebKit stylesheet CSP warnings; policy unchanged`);
    report('no relevant console/page errors or horizontal overflow');
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    await evidence.finish({ error: failure });
    if (browser) await browser.close();
    server.kill();
    rmSync(dir, { recursive: true, force: true });
  }
}
if (wanted.includes('chromium')) await run(chromium);
if (wanted.includes('webkit')) await run(webkit, true);
