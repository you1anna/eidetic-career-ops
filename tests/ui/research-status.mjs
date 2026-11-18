import {verifyDashboardActions} from './dashboard-actions.mjs';
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
const tmp=mkdtempSync(join(tmpdir(),'wo-research-ui-')),database=join(tmp,'fixture.db');
execFileSync(py,[join(root,'tools/make_fixture.py'),database]);
execFileSync(py,[join(root,'tests/ui/dashboard_fixture.py'),tmp]);
const port=await new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
const base=`http://127.0.0.1:${port}`;
const server=spawn(py,[join(root,'workspace/server.py'),'--db',database,'--port',String(port)],{stdio:'ignore'});

async function verifyItemPresentation(browser,name,evidence){
 const page=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
 await evidence.attach(page.context());
 const errors=[],writes=[],researchRequests=[];page.on('pageerror',error=>errors.push(error.message));
 page.on('request',request=>{
  if(['POST','PATCH','PUT','DELETE'].includes(request.method())&&!request.url().includes('/api/seen/'))writes.push(request.url());
  if(/\/api\/.*research/.test(request.url()))researchRequests.push(request.url());
 });
 const original=await(await fetch(base+'/api/state?view=summary')).json();
 const source=original.records.find(r=>r.type==='job'&&r.transitions?.status?.includes('pursue'));
 assert.ok(source,'synthetic fixture offers Shortlist and Skip');
 const stamp='2026-10-17T12:02:21Z',time='17 Oct 2026, 13:02 BST';
 const examples=[
  {status:'failed',reason:'RuntimeError',text:`Research failed — earlier evidence kept · ${time}`},
  {status:'unverified',reason:'robots_disallowed',text:`Research incomplete — automated check blocked by the site · ${time}`},
  {status:'usage_limited',reason:'',text:`Research paused; not completed · ${time}`},
  {status:'context_changed',reason:'internal exception text',text:`Research outdated — role or source changed · ${time}`},
  {status:'unknown_enum',reason:'<img src=x onerror=alert(1)>',attempted_at:'bad date',text:'Research outcome unavailable · date not recorded'},
  {status:'not_attempted',reason:'',attempted_at:null,text:'No research attempt recorded.'},
 ];
 const measurements=[];
 for(const [index,example] of examples.entries()){
  const summary=structuredClone(original),record=summary.records.find(r=>r.id===source.id);
  const attempted_at=Object.hasOwn(example,'attempted_at')?example.attempted_at:stamp;
  const scope=index===3?'outside_focus':'maintained';
  const scopeLabel=scope==='maintained'?'Included in opportunity research; runs are subject to available allowance':'Outside the active research set.';
  const attempt={outcome:example.status,attempted_at,reason:example.reason};
  record.research_disposition={scope,label:scopeLabel,last_attempt:example.status==='not_attempted'?null:attempt};
  record.evidence_checks={...record.evidence_checks,vacancy:{status:'unverified',checked_at:stamp,source_url:'https://fixture.example/research',basis:'robots_disallowed'}};
  summary.version=`research-copy-${index}`;
  summary.opportunities={...summary.opportunities,act_now:[],discovery:[],waiting:[],in_progress:[],needs_checking:[{record_id:record.id,reason:'Recorded role worth checking',lane:'contract',research_status:{status:example.status,attempted_at,reason:example.reason}}]};
  // A newer global run must never relabel this older item attempt.
  summary.research_processing={status:'blocked',last_attempt:'2026-11-06T09:00:00Z',reason:index%2?'access':'usage_limit',pass_cap:index%2===0,checked:12,total:12};
  await page.route('**/api/state?view=summary*',route=>route.fulfill({json:summary}));
  await page.goto(base+'/#week');const section=page.locator('.week-needs-checking');await section.waitFor();
  assert.equal(await section.evaluate(node=>node.open),false,'research stays folded');
  await section.locator(':scope > summary').click();const card=section.locator('.needs-checking-card');
  assert.equal(await card.locator('.opportunity-research').innerText(),example.text);
  assert.equal(await card.locator('.opportunity-research').count(),1,'replace the existing status paragraph');
  assert.equal((await section.innerText()).match(/09:00 and 19:00 London time/g)?.length,1,'schedule occurs once per section');
  assert.match(await section.innerText(),/If you want, Shortlist roles worth keeping or Skip unsuitable ones/);
  assert.equal(await card.getByRole('button',{name:'Shortlist',exact:true}).count(),1);
  assert.equal(await card.getByRole('button',{name:'Skip',exact:true}).count(),1);
  assert.doesNotMatch(await card.innerText(),/RuntimeError|robots_disallowed|unknown_enum|internal exception text|onerror/);
  assert.equal(await card.evaluate(node=>node.scrollWidth>node.clientWidth+1),false);
  measurements.push({status:example.status,paragraphs:await card.locator('p').count(),height:Math.round((await card.boundingBox()).height)});
  const capture=process.env.WO_RESEARCH_STATUS_CAPTURE_DIR;
  if(capture&&index===0){mkdirSync(capture,{recursive:true});await card.scrollIntoViewIfNeeded();await page.screenshot({path:join(capture,`needs-checking-${name}.png`)});}
  // Opening renders immediately and again on hashchange; inspect the completed routed sheet.
  await Promise.all([page.evaluate(()=>new Promise(resolve=>window.addEventListener('hashchange',()=>resolve(),{once:true}))),card.locator('.opportunity-title').click()]);
  const detail=page.locator('#detail[open]');await detail.locator('.incoming-research').waitFor();
  const note=detail.locator('.incoming-research');
  assert.equal(await note.locator(':scope > p').count(),2,'retain scope plus one attempt paragraph');
  assert.equal(await note.locator(':scope > p').first().innerText(),scopeLabel);
  assert.equal(await note.locator(':scope > p').last().innerText(),example.text+(scope==='maintained'?(example.status==='not_attempted'?' ':'. ')+'Usual automatic passes: 09:00 and 19:00 London time. A particular role may wait for a later pass.':''));
  const check=detail.locator('.job-approach-brief .vacancy-evidence .evidence-check');
  assert.equal(await check.getAttribute('title'),'Checked 17 Oct 2026, 13:02 BST. Automated check blocked by the site');
  if(capture&&index===0){await note.scrollIntoViewIfNeeded();await page.screenshot({path:join(capture,`research-role-${name}.png`)});}
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  assert.equal(await check.getAttribute('title'),'Checked 17 Oct 2026, 13:02 BST. Automated check blocked by the site','refresh keeps human source explanation');
  assert.equal(await detail.evaluate(node=>node.scrollWidth>node.clientWidth+1),false);
  await page.unroute('**/api/state?view=summary*');await page.goto('about:blank');
 }
 // Exact code mapping changes presentation only; ordinary evidence and HTML-looking prose are literal.
 await page.goto(base+'/#week');await page.locator('.site-line').waitFor();
 const sourceEvidence=await page.evaluate(async()=>{
  const {checkLabel,refreshCheckLabels,vacancyEvidence,personRouteEvidence}=await import('/js/job-evidence.js');
  const check={status:'unverified',checked_at:'2026-10-17T12:02:21Z',basis:'robots_disallowed',source_url:'https://fixture.example/source'};
  const job={id:'synthetic',type:'job',evidence_checks:{vacancy:check}},box=document.createElement('div');
  box.append(checkLabel(check));refreshCheckLabels(box);
  const person=personRouteEvidence({evidence_checks:{contacts:[check]}});
  const prose='The advert says <img src=x onerror=alert(1)> is a literal example.';
  const raw=vacancyEvidence({...job,evidence_checks:{vacancy:{...check,basis:prose}}});
  return {title:box.firstChild.title,person:person.textContent,prose:raw.textContent,images:raw.querySelectorAll('img').length};
 });
 assert.match(sourceEvidence.title,/Automated check blocked by the site/);
 assert.match(sourceEvidence.person,/Automated check blocked by the site/);
 assert.match(sourceEvidence.prose,/The advert says <img src=x onerror=alert\(1\)> is a literal example\./);
 assert.equal(sourceEvidence.images,0);assert.deepEqual(writes,[]);assert.deepEqual(researchRequests,[]);assert.deepEqual(errors,[]);
 await page.close();console.log(`ok research item copy ${name}: six outcomes, own London dates, one status paragraph, scope, source reasons, no new mutations or research requests, layout`);
 console.log('research card measurements',JSON.stringify({browser:name,cards:measurements}));
}
try {
 for(let i=0;i<100;i++){try{if((await fetch(base+'/api/health')).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
 for(const [name,type] of [['chromium',chromium],['webkit',webkit]]){
  if(!(process.env.WO_UI_BROWSERS||'chromium,webkit').includes(name))continue;
  const browser=await launchBrowser(type, name==='chromium'&&process.env.WO_UI_CHROMIUM?{executablePath:process.env.WO_UI_CHROMIUM}:{});
  const evidence=createBrowserEvidence({suite:'research-status',browser:name});let failure;
  try {
   await verifyDashboardActions(browser,name,evidence,base);
   const page=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(page.context());
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   let status={status:'not_started'},sampleId=null;
   // The server-only Dashboard figures are read on the Dashboard alone; other tabs use what is loaded.
   const dashboardReads=[];page.on('request',r=>{if(new URL(r.url()).pathname==='/api/dashboard')dashboardReads.push(new URL(page.url()).hash);});
   await page.route('**/api/state*',async route=>{const res=await route.fetch();const data=decodeSummary(await res.json());sampleId=data.records?.[0]?.id||sampleId;data.research_processing=status;
    data.caretaker_processing={status:'partial',last_attempt:new Date().toISOString(),checks:[{id:'offhost',label:'Off-host copy and restore drill',status:'unknown',detail:'Evidence remains on the Mac.'}]};await route.fulfill({response:res,json:data});});
   const show=async expected=>{await page.goto(base+'/#dashboard');await page.waitForSelector('.research-processing');assert.ok((await page.locator('.research-processing strong').innerText()).includes(expected));};
   await show('Not started');
   assert.equal(await page.locator('.stat-tile').filter({hasText:'Adverts confirmed open, last pass'}).count(),0,'duplicated status counter replaced by decision support');
   assert.doesNotMatch(await page.locator('.research-processing').innerText(),/Latest research pass:|Checks completed/);
   const now=new Date().toISOString();
   status={status:'partial',last_attempt:now,total:5,checked:0,verified:0,contacts_total:5,contacts_verified:0,backlog:5,unverified:0};
   await page.reload();await page.waitForSelector('.research-processing');
   assert.match(await page.locator('.research-processing').innerText(),/Latest research pass: 0 of 5 jobs checked · 0 adverts confirmed open · 0 of 5 contact routes confirmed/);
   status={...status,verified:null,contacts_verified:null,contacts_total:undefined};
   await page.reload();await page.waitForSelector('.research-processing');
   assert.match(await page.locator('.research-processing strong').innerText(),/coverage not fully recorded/);
   assert.match(await page.locator('.research-processing').innerText(),/unknown adverts confirmed open · unknown of unknown contact routes confirmed/);
   status={...status,status:'running'};
   await page.reload();await page.waitForSelector('.research-processing');
   assert.match(await page.locator('.research-processing').innerText(),/Current research pass so far: 0 of 5 jobs checked/);
   assert.doesNotMatch(await page.locator('.research-processing').innerText(),/Latest research pass:|Checks completed/);
   status={status:'ok',last_attempt:now,last_success:now,total:3,checked:3,verified:3,contacts_total:2,contacts_verified:2,backlog:0,unverified:0,engine:'codex_cli',usage:{completed:3,input_tokens:300,output_tokens:100},limits:{max_calls:60}};
   await page.reload();await page.waitForFunction(()=>document.querySelector('.research-processing strong')?.textContent.includes('Checks completed'));
   await page.getByText('Research coverage',{exact:true}).click();
   assert.match(await page.locator('.research-processing').innerText(),/Latest research pass: 3 of 3 jobs checked/);
   assert.match(await page.locator('.research-processing').innerText(),/2 of 2 contact routes confirmed/);
   assert.match(await page.locator('.research-processing').innerText(),/ChatGPT login/);
   assert.doesNotMatch(await page.locator('.research-processing').innerText(),/Budget used/);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   if(process.env.WO_RESEARCH_STATUS_CAPTURE_DIR){mkdirSync(process.env.WO_RESEARCH_STATUS_CAPTURE_DIR,{recursive:true});await page.screenshot({path:join(process.env.WO_RESEARCH_STATUS_CAPTURE_DIR,`research-${name}.png`)});await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:join(process.env.WO_RESEARCH_STATUS_CAPTURE_DIR,`dashboard-${name}.png`)});}
   status={...status,focused:true,people:2,whole_queue:400,outside_focus:399,finished_at:'2026-10-17T12:02:21Z',allowance:{research_day:6,research_week:12,day:6,week:12,research_day_limit:12,research_week_limit:84,day_limit:16,week_limit:100}};
   await page.reload();await page.getByText('Research coverage',{exact:true}).click();
   assert.match(await page.locator('.research-processing').innerText(),/399 of 400 eligible jobs are outside/);
   assert.match(await page.locator('.research-processing').innerText(),/Invocation counts recorded 17 Oct 2026, 13:02 BST: research 6\/12 that day/);
   assert.doesNotMatch(await page.locator('.research-processing').innerText(),/today|this week/);
   assert.match(await page.locator('.caretaker-processing').innerText(),/Checks have gaps/);
   await page.getByText('Caretaker checks',{exact:true}).click();
   assert.match(await page.locator('.caretaker-processing').innerText(),/Evidence remains on the Mac/);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   status={...status,item_failures:[{reason:'ValueError',where:'brief_research.py:88 apply',count:4,ids:sampleId?[sampleId]:[]}]};await page.reload();
   await page.waitForSelector('.dashboard-attention .dash-problem');
   assert.match(await page.locator('.dashboard-attention').innerText(),/Research: 4 items failed with ValueError\s+Raised at brief_research\.py:88 apply/);
   if(sampleId){await page.locator('.dashboard-attention .dash-problem-links button').first().click();await page.waitForSelector('#detail[open]');await page.locator('#close').click();}
   assert.equal(await page.locator('.dashboard-hunt .dashboard-split .dashboard-panel').count(),2);
   await page.locator('.dashboard-hunt button.stat-tile').filter({hasText:'Answered, awaiting Claude'}).click();
   assert.equal(await page.locator('.dashboard-awaiting[open]').count(),1);
   await page.locator('.dashboard-gaps > summary').click();
   assert.equal(await page.locator('.dashboard-gaps .dashboard-table').count(),1);
   await page.goto(base+'/#week');await page.waitForSelector('.site-line');
   assert.match(await page.locator('.site-line').innerText(),/^Site: \d+ needs? attention · Dashboard$/);
   await page.locator('.site-line a').click();await page.waitForSelector('.dashboard-attention');
   for(const tab of ['#work','#week']){await page.goto(base+'/'+tab);await page.reload();await page.waitForSelector('.site-line');}
   assert.ok(dashboardReads.length>0&&dashboardReads.every(hash=>hash==='#dashboard'),`dashboard reads from ${dashboardReads}`);
   await page.goto(base+'/#dashboard');await page.waitForSelector('.dashboard-attention');
   status={...status,item_failures:[]};
   status={...status,status:'partial',backlog:1};await page.reload();await page.waitForFunction(()=>document.querySelector('.research-processing strong')?.textContent.includes('Finished with gaps'));
   status={...status,status:'blocked',reason:'usage_limit'};await page.reload();await page.waitForFunction(()=>document.querySelector('.research-processing strong')?.textContent.includes('Paused — research not completed'));
   status={...status,reason:'access'};await page.reload();await page.waitForFunction(()=>document.querySelector('.research-processing strong')?.textContent.includes('sign-in or research service'));
   status={...status,status:'ok',last_attempt:new Date(Date.now()-17*3600000).toISOString()};await page.reload();await page.waitForFunction(()=>document.querySelector('.research-processing strong')?.textContent.includes('Overdue'));
   status={...status,status:'partial',last_attempt:new Date().toISOString(),current:8,reused:4,briefs:{sufficient:3,total:7,blocking:4,unresolved:9}};
   await page.reload();await page.waitForSelector('.research-processing');await page.getByText('Research coverage',{exact:true}).click();
   const text=await page.locator('.research-processing').innerText();
   assert.match(text,/active work, shortlisted opportunities and the first ten ranked discovery items/);
   assert.match(text,/8 targets already current; 4 interpretations reused/);
   assert.match(text,/3 of 7 work targets have sufficient recorded evidence/);
   assert.doesNotMatch(text,/Weekly approach preparation|unsent notes|approval to send/);
   await page.goto(base+'/#week');await page.reload();await page.waitForSelector('.week-discovery');
   assert.equal(await page.locator('.week-preparation-summary,.preparation-status').count(),0);
   assert.deepEqual(errors,[]);console.log(`ok research status ${name}: never run, success, partial, budget, access, overdue, canonical evidence scope, allowance, reuse, no preparation panel and layout`);
   await verifyItemPresentation(browser,name,evidence);
   // Server figures that fail to load say so with Retry; a later failed refresh keeps the earlier
   // figures, labelled with their time, instead of "Loading…" or silently stale numbers.
   const compare=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(compare.context());
   const comparisonErrors=[],comparisonWrites=[];let comparisonReads=0,comparisonFails=true;
   compare.on('pageerror',error=>comparisonErrors.push(error.message));
   compare.on('request',request=>{if(request.method()!=='GET'&&!request.url().includes('/api/seen/'))comparisonWrites.push(request.url());});
   await compare.route('**/api/intake-review',route=>{comparisonReads++;return comparisonFails?route.fulfill({status:400,json:{error:'Synthetic unreadable pending store'}}):route.continue();});
   await compare.goto(base+'/#dashboard');await compare.waitForSelector('.intake-comparison');
   assert.equal(comparisonReads,0,'ordinary Dashboard loading must not run the comparison');
   const decisionCount=await compare.locator('.dashboard-choices .decision-card').count();
   assert.equal(Number(await compare.locator('.stat-tile').filter({hasText:'Decisions for you'}).locator('.stat-value').innerText()),decisionCount);
   assert.ok(decisionCount>0,'synthetic open operational decision is included');
   assert.equal(await compare.locator('.intake-comparison').evaluate(n=>n.open),false);
   await compare.locator('.intake-comparison > summary').click();
   await compare.getByRole('button',{name:'Compare candidates',exact:true}).click();
   await compare.getByRole('button',{name:'Retry comparison',exact:true}).waitFor();
   assert.match(await compare.locator('.intake-comparison').innerText(),/Synthetic unreadable pending store/);
   comparisonFails=false;await compare.getByRole('button',{name:'Retry comparison',exact:true}).click();
   await compare.getByRole('button',{name:'Refresh comparison',exact:true}).waitFor();
   assert.ok(await compare.locator('.intake-comparison .dash-decision-row').count()>0);
   assert.match(await compare.locator('.intake-comparison').innerText(),/does not refund the rolling additions allowance/);
   assert.ok(await compare.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   const review=compare.locator('.intake-comparison a').filter({hasText:'Review lead'}).first();
   await review.click();await compare.waitForSelector('#detail[open] .lead-email-context');
   assert.match(await compare.locator('#detail').innerText(),/Shortlisting adds this role to Act now/);
   assert.deepEqual(comparisonWrites,[],'comparison and lead review are read-only');
   await compare.locator('#detail').getByRole('button',{name:'Shortlist',exact:true}).click();
   await compare.waitForFunction(()=>location.hash.startsWith('#week/'));
   await compare.locator('#close').click();
   await compare.locator('[data-tab=dashboard]').click();
   await compare.waitForFunction(()=>document.querySelector('.intake-comparison')?.textContent.includes('workspace changed'));
   assert.match(await compare.locator('.intake-comparison').innerText(),/Refresh comparison before choosing/);
   assert.deepEqual(comparisonErrors,[]);await compare.close();
   console.log(`ok dashboard comparison ${name}: on-demand, failed read/retry, count agreement, review lead, no writes`);

   const board=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(board.context());
   const boardErrors=[];board.on('pageerror',e=>boardErrors.push(e.message));
   let failing=true,reads=0;
   await board.clock.install({time:Date.now()});
   await board.route('**/api/dashboard',route=>{reads++;return failing?route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'Synthetic failure.'})}):route.continue();});
   await board.goto(base+'/#dashboard');
   await board.waitForFunction(()=>document.querySelector('.dashboard-figures')?.textContent.includes('could not be loaded'),null,{polling:100});
   assert.match(await board.locator('.dashboard-figures').innerText(),/Server figures could not be loaded\. Synthetic failure\./);
   assert.match(await board.locator('.dashboard-capacity').innerText(),/Unavailable: the server figures could not be loaded/);
   assert.doesNotMatch(await board.locator('.dashboard').innerText(),/Loading…/);
   failing=false;await board.locator('.dashboard-figures').getByRole('button',{name:'Retry'}).click();
   await board.waitForFunction(()=>document.querySelector('.dashboard-figures')?.textContent.startsWith('Server figures updated'),null,{polling:100});
   await board.waitForSelector('.dashboard-capacity .meter, .dashboard-capacity p');
   const collection=board.locator('.dashboard-quotas');
   await collection.getByText('Adzuna: 7 of 10 calls used or reserved',{exact:true}).click();
   assert.match(await collection.innerText(),/Scheduled collection: 6 of 6/);
   assert.match(await collection.innerText(),/Pending\/uncertain reservations: 1/);
   assert.match(await collection.innerText(),/Deferred scheduling decisions today: 1/);
   assert.match(await collection.innerText(),/Provider account allowance: 150\/day/);
   assert.ok(await board.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   const before=reads;failing=true;
   await board.clock.runFor(6*60000);  // the next five-minute read fails
   await board.waitForFunction(()=>document.querySelector('.dashboard-figures')?.textContent.includes('the latest refresh failed'),null,{polling:100});
   assert.ok(reads>before,'a later refresh was attempted');
   assert.match(await board.locator('.dashboard-figures').innerText(),/^Showing server figures from \d\d:\d\d: the latest refresh failed\. Synthetic failure\./);
   assert.doesNotMatch(await board.locator('.dashboard-capacity').innerText(),/Unavailable|Loading/);
   assert.equal(await board.locator('.dashboard-figures').getByRole('button',{name:'Retry'}).count(),1);
   await board.clock.runFor(60000);
   assert.equal(reads,before+1,'a failed read waits for the next cadence instead of retrying every minute');
   // Figures arriving after the tab opens rebuild only the sections built from them: a card the owner
   // is tapping in Needs attention is not swapped out from under the tap.
   const steady=await browser.newPage(name==='webkit'?devices['iPhone 13']:{viewport:{width:1280,height:900}});
   await evidence.attach(steady.context());
   let release;const gate=new Promise(r=>{release=r;});
   await steady.route('**/api/dashboard',async route=>{await gate;await route.continue();});
   await steady.goto(base+'/#dashboard');await steady.waitForSelector('.dashboard-attention');
   await steady.locator('.dashboard-attention').evaluate(n=>{n.dataset.mark='kept';});
   await steady.locator('.dashboard-admin').evaluate(n=>{n.dataset.mark='kept';});
   release();
   await steady.waitForFunction(()=>document.querySelector('.dashboard-figures')?.textContent.startsWith('Server figures updated'),null,{polling:100});
   assert.equal(await steady.locator('.dashboard-attention[data-mark="kept"], .dashboard-admin[data-mark="kept"]').count(),2,'arriving figures rebuilt the sections holding decision cards');
   await steady.close();
   assert.deepEqual(boardErrors,[]);await board.close();console.log(`ok dashboard figures ${name}: failed load, retry, stale refresh labelled`);
  } catch(error){failure=error;throw error;} finally {await evidence.finish({error:failure});await browser.close();}
 }
} finally {server.kill();await new Promise(r=>server.once('exit',r));rmSync(tmp,{recursive:true,force:true});}
