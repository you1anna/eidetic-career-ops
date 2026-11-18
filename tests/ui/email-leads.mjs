import assert from 'node:assert/strict';
import {chromium, webkit, devices} from 'playwright-core';
import {launchBrowser} from '../../tools/browser_launch.mjs';
import {createBrowserEvidence} from './browser-evidence.mjs';
import {execFileSync, spawn} from 'node:child_process';
import {mkdtempSync, rmSync, mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import net from 'node:net';
const root=join(dirname(fileURLToPath(import.meta.url)), '../..'), py=join(root,'.venv/bin/python');
const fixture=String.raw`
import json,sys
from pathlib import Path
sys.path[:0]=['workspace','tools']
import db,records
from test_email_leads import item
from mail_intake import MailSpool
p=Path(sys.argv[1]);c=db.connect(p/'site.db');db.migrate(c)
raw=b'From: alerts@example.test\r\nMessage-ID: <original@example.test>\r\nSubject: Example role\r\n\r\nOriginal private email'
receipt=MailSpool(p/'email/mail.db').receive(raw,envelope_from='alerts@example.test',envelope_to='alerts@example.com',received_at='2026-11-05T10:00:00Z')
items=[item(n,title='QA Lead '+str(n),received_at=f'2026-11-{n:02}T10:00:00Z',receipt_id=receipt['receipt_id']) for n in range(1,26)]
(p/'pending').mkdir();(p/'pending/observations.json').write_text(json.dumps(dict(items={str(n):r for n,r in enumerate(items)},sources={})))
c.close()
`;
for (const [name,type] of [['chromium',chromium],['webkit',webkit]]) {
 if (!(process.env.WO_UI_BROWSERS||'chromium,webkit').includes(name)) continue;
 const dir=mkdtempSync(join(tmpdir(),'wo-email-leads-'));
 execFileSync(py,['-c',fixture,dir],{cwd:root});
 const port=await new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
 const base=`http://127.0.0.1:${port}`, server=spawn(py,['workspace/server.py','--db',join(dir,'site.db'),'--port',String(port)],{cwd:root,stdio:'ignore'});
 const evidence=createBrowserEvidence({suite:'email-leads',browser:name});
 let browser,failure;
 try {
  for(let n=0;n<100;n++){try{if((await fetch(base+'/api/health')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  browser=await launchBrowser(type,name==='chromium'&&process.env.WO_UI_CHROMIUM?{executablePath:process.env.WO_UI_CHROMIUM}:{});
  const page=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
  await evidence.attach(page.context());
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let reads=0;page.on('request',r=>{if(r.url().includes('/api/email-leads?'))reads++;});
  await page.goto(base+'/#dashboard');
  await page.getByText('Review unassessed email leads',{exact:true}).click();
  await page.waitForSelector('.email-lead-row');
  assert.equal(await page.locator('.email-lead-row').count(),20);
  assert.match(await page.locator('.email-lead-row').first().innerText(),/QA Lead 25/);
  assert.doesNotMatch(await page.locator('.email-leads').innerText(),/strong match/);
  const search=page.getByRole('searchbox',{name:'Search email leads'});
  await search.fill('Lead 1');
  assert.equal(reads,1,'typing alone does not scan the archive');
  await page.locator('.lead-search').getByRole('button',{name:'Search',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.email-lead-row').length===11);
  const review=await page.locator('.email-lead-row').first().getByRole('link',{name:'Review',exact:true}).getAttribute('href');
  await page.goto(base+review);
  await page.getByRole('heading',{name:'Email says — unverified'}).waitFor();
  assert.equal(await page.locator('#detail-kind').textContent(),'Email lead · Unassessed');
  assert.match(await page.locator('#detail-content').innerText(),/£90,000–£110,000/);
  await page.getByText('Original email summary',{exact:true}).click();
  assert.match(await page.locator('.lead-original-summary').innerText(),/A strong match\. Lead a team\./);
  assert.match(await page.getByRole('link',{name:'Find original email in Gmail'}).getAttribute('href'),/rfc822msgid%3Aoriginal%40example.test/);
  const original=await page.getByRole('link',{name:'Download original email'}).getAttribute('href');
  const response=await fetch(base+original);
  assert.match(response.headers.get('content-disposition'),/attachment/);
  assert.match(await response.text(),/Original private email/);
  assert.equal((await (await fetch(base+'/api/state')).json()).records.length,0,'review does not save or shortlist');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  if(process.env.WO_EMAIL_LEADS_CAPTURE_DIR){mkdirSync(process.env.WO_EMAIL_LEADS_CAPTURE_DIR,{recursive:true});await page.screenshot({path:join(process.env.WO_EMAIL_LEADS_CAPTURE_DIR,`lead-${name}.png`)});}
  await page.getByRole('button',{name:'Shortlist',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.job-controls')?.textContent.includes('Shortlisted'));
  const state=await (await fetch(base+'/api/state')).json(), job=state.records.find(r=>r.type==='job');
  assert.equal(job.status,'pursue');assert.equal(job.pay,'');assert.notEqual(job.availability.status,'open');
  await page.getByRole('button',{name:'Review email context',exact:true}).click();
  await page.getByRole('button',{name:'Open saved role',exact:true}).waitFor();
  await page.getByRole('button',{name:'Open saved role',exact:true}).click();
  await page.locator('#close').click();
  await page.locator('#nav [data-tab="week"]').click();
  await page.waitForFunction(()=>document.querySelector('#content')?.textContent.includes('Act now'));
  assert.match(await page.locator('#content').innerText(),/QA Lead 19/);
  await page.goto(base+review);
  await page.getByRole('button',{name:'Open saved role',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Shortlist',exact:true}).count(),0);
  assert.equal((await (await fetch(base+'/api/state')).json()).records.filter(r=>r.type==='job').length,1);
  assert.deepEqual(errors,[]);
  console.log(`ok deferred leads ${name}: bounded search, direct Review, original, manual Shortlist, Act now and replay`);
 } catch(error) {failure=error;throw error;} finally {
  await evidence.finish({error:failure});
  if(browser)await browser.close();server.kill();await new Promise(resolve=>server.once('exit',resolve));rmSync(dir,{recursive:true,force:true});
 }
}
