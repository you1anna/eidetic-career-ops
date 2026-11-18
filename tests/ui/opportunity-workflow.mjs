// The replacement work workflow and historical correspondence contract. Synthetic data only.
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
import {applySummaryDelta} from '../../workspace/js/summary-delta.js';
const root=join(dirname(fileURLToPath(import.meta.url)),'../..'),python=join(root,'.venv/bin/python');
const wanted=(process.env.WO_UI_BROWSERS||'chromium,webkit').split(',');
const fixture=String.raw`
import datetime as dt,json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'tools'),str(Path.cwd()/'workspace')]
import db,records,record_imports
c=db.connect(sys.argv[1]);now=dt.datetime.now(dt.timezone.utc).isoformat()
person=c.execute("SELECT id FROM records WHERE name='Alex Example'").fetchone()['id']
records.draft(c,dict(record_id=person,revision=records.get(c,person)['revision'],purpose='message',text='Historical wording: <literal> version one.'),actor='agent')
records.draft(c,dict(record_id=person,revision=records.get(c,person)['revision'],purpose='message',text='Historical wording: final unsent version.'),actor='agent')
ids=[]
for n in range(25):
 url=f'https://fixture.example/jobs/{n}'
 rid=records.create(c,dict(type='job',name=f'Interim Engineering Manager {n:02d}',organisation='Synthetic Ltd',date=(dt.datetime.fromisoformat(now)-dt.timedelta(days=1)).date().isoformat(),status='candidate',engagement='contract',pay='£650/day',location='Remote UK',summary='Lead software engineering teams through platform modernisation and improve delivery quality.',url=url),actor='agent')['record']['id'];ids.append(rid)
 record_imports.import_research_entries(c,[dict(id=rid,reviewed_at=now,source_date=now[:10],summary='Lead software engineering teams through platform modernisation and improve delivery quality.',source_urls=[url],freshness_checks=[dict(scope='vacancy',checked_at=now,status='confirmed',source_url=url,basis='Exact current advertised software role with active application route.')])],'research:ui-work')
# A strong advert whose read could not confirm it stays in Jobs → Discovery but not in Worth a look.
url='https://fixture.example/jobs/unconfirmed'
unconfirmed=records.create(c,dict(type='job',name='Interim Engineering Manager unconfirmed',organisation='Synthetic Ltd',date=now[:10],status='candidate',engagement='contract',pay='£650/day',location='Remote UK',summary='Lead software engineering teams through platform modernisation and improve delivery quality.',url=url),actor='agent')['record']['id']
record_imports.import_research_entries(c,[dict(id=unconfirmed,reviewed_at=now,source_date=now[:10],summary='Lead software engineering teams through platform modernisation and improve delivery quality.',source_urls=[url],freshness_checks=[dict(scope='vacancy',checked_at=now,status='unverified',source_url=url,basis='refused')])],'research:ui-work')
# Canonical field projections must outlive the three-recent-review preview.
for days,fields in [(8,dict(facts=['Canonical older requirement: support the inherited billing service.','Canonical older requirement: provide release traceability.'])),(7,dict(decisive_unknowns=['Canonical older unknown: confirm the delivery owner.'])),(6,dict(public_contact_route='Canonical older route: vacancy-owner@example.test'))]:
 stamp=(dt.datetime.fromisoformat(now)-dt.timedelta(days=days)).isoformat()
 record_imports.import_research_entries(c,[dict(id=ids[0],reviewed_at=stamp,source_date=stamp[:10],source_urls=[f'https://fixture.example/canonical/{days}'],**fields)],'research:canonical-field-projection')
for n in range(5):
 stamp=(dt.datetime.fromisoformat(now)+dt.timedelta(seconds=n+1)).isoformat()
 record_imports.import_research_entries(c,[dict(id=ids[0],reviewed_at=stamp,source_urls=['https://fixture.example/routine'],summary='Lead software engineering teams through platform modernisation and improve delivery quality.')],'research:routine-check')
print(json.dumps(dict(person=person,jobs=ids,unconfirmed=unconfirmed)));c.close()
`;
const freePort=()=>new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const {port}=s.address();s.close(()=>resolve(port));});});
async function verifyDecisionPresentation(context,base,original,mobile){
 const page=await context.newPage(),errors=[],writes=[],results=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',request=>{if(['POST','PATCH','PUT','DELETE'].includes(request.method())&&!request.url().includes('/api/seen/'))writes.push(request.url());});
 const source=original.records.find(r=>r.type==='job'&&r.name.startsWith('Interim Engineering Manager'));
 const template=[...(original.intelligence.actions||[]),...(original.intelligence.research||[])].find(a=>a.record_id===source.id);
 assert.ok(template,'decision fixture has a canonical application action');
 const at=new Date(Date.now()-2*3600000).toISOString(),until=new Date(Date.now()+46*3600000).toISOString();
 const cases=[
  {name:'thin',pay:'',engagement:'',summary:'',fit:[],available:false,next:'Apply through the advert.',default:true},
  {name:'pay',pay:'£94,000–131,000/year',engagement:'',summary:'',fit:[],available:false,next:'Confirm the agreed interview time.',default:false},
  {name:'rich',pay:'£95,000–110,000/year',engagement:'permanent',summary:'Lead the software engineering team.',fit:[{text:'Supported software leadership remit',weight:60,fit:true}],available:true,next:'Apply through the advert.',default:true},
 ];
 for(const example of cases){
  const summary=structuredClone(original),record=summary.records.find(r=>r.id===source.id),action=structuredClone(template);
  Object.assign(record,{summary:example.summary,pay:example.pay,engagement:example.engagement,status:'pursue',location:'London',source_lifecycle:'active',published_deadline:null,
   availability:{status:example.available?'open':'unconfirmed',basis:example.available?'research':null,at:example.available?at:null,until},
   evidence_checks:{vacancy:{status:example.available?'confirmed':'unverified',checked_at:at,basis:'robots_disallowed',source_url:'https://fixture.example/decision'}},
   briefing:{decisive_unknowns:['Confirm the recorded office arrangement.']}});
  record.relevance={...record.relevance,reasons:example.fit,fit:{evidence:example.available?[{kind:'remit',quote:'Lead the software engineering team.',url:'https://fixture.example/decision'}]:[],gaps:[]},qualification:{engagement:example.engagement||'unknown',conflicts:[]}};
  action.progress={...action.progress,next:example.next,next_is_default:example.default,reply_to_record:false,next_step:example.default?null:{kind:'mine',text:example.next},actions:[]};
  const ref={record_id:record.id,action_id:action.id,reason:example.next,uncertainty:'Confirm the recorded office arrangement.',attention:example.default?'shortlisted':'your_step',source:{label:'LinkedIn email',basis:'receipt',at:'2026-10-30T10:00:00Z',url:record.url}};
  summary.version='decision-'+example.name;summary.opportunities={...summary.opportunities,act_now:[ref],waiting:[],in_progress:[],discovery:[],needs_checking:[]};
  summary.intelligence={...summary.intelligence,actions:[action],research:[]};
  await page.route('**/api/state?view=summary*',route=>route.fulfill({json:summary}));
  await page.goto(base+'/#week');const card=page.locator('.week-act-now .week-progress-brief');await card.waitFor();
  const cardText=await card.innerText(),hero=await card.locator(':scope > .approach-next').innerText();
  const paragraphs=await card.locator(':scope > .approach-next,.opportunity-facts > p').count();
  const height=Math.round((await card.boundingBox()).height);
  const capture=process.env.WO_WORK_CAPTURE_DIR;
  if(capture){mkdirSync(capture,{recursive:true});await card.screenshot({path:join(capture,`decision-${example.name}-${mobile?'iphone':'desktop'}.png`)});}
  await card.locator('.approach-name').click();await page.locator('#detail[open] .job-controls').waitFor();
  const detail=page.locator('#detail'),assessment=detail.locator('.work-assessment');
  const blockers=assessment.locator('.decision-blockers');
  results.push({example,cardText,hero,paragraphs,height,detail:await assessment.innerText(),blockers:await blockers.count()?await blockers.textContent():'',
   termsCount:await detail.locator('.job-terms').count(),termsBeforeActions:await detail.evaluate(node=>{
    const terms=node.querySelector('.job-terms'),controls=node.querySelector('.job-controls');return !!(terms&&controls&&(terms.compareDocumentPosition(controls)&Node.DOCUMENT_POSITION_FOLLOWING));
   }),assessmentFirst:await detail.evaluate(node=>{
    const assessment=node.querySelector('.work-assessment'),incoming=node.querySelector('.incoming-process'),company=node.querySelector('.job-company');
    return !!(assessment&&incoming&&company&&(assessment.compareDocumentPosition(incoming)&Node.DOCUMENT_POSITION_FOLLOWING)
      &&(company.compareDocumentPosition(assessment)&Node.DOCUMENT_POSITION_FOLLOWING));
   })});
  if(capture){await detail.locator('#detail-content').evaluate(n=>n.scrollTop=0);await page.screenshot({path:join(capture,`decision-${example.name}-detail-${mobile?'iphone':'desktop'}.png`)});}
  assert.equal(await detail.evaluate(n=>n.scrollWidth>n.clientWidth+1),false,'decision detail does not overflow');
  await page.unroute('**/api/state?view=summary*');await page.goto('about:blank');
 }
 console.log('decision card measurements',JSON.stringify({browser:mobile?'iphone':'desktop',cards:results.map(r=>({case:r.example.name,paragraphs:r.paragraphs,height:r.height}))}));
 for(const result of results){
  const {example}=result;
  assert.equal(result.hero,example.default?'Fit: '+(example.available?'Supported software leadership remit':'Not established from recorded evidence.')+' · Still unknown: Confirm the recorded office arrangement.':example.next,'default Apply is replaced; recorded next steps win');
  assert.ok(result.paragraphs<=3,'compact facts replace existing paragraphs, including a recorded next step');
  assert.ok(result.cardText.includes(example.pay||'Pay not stated'));
  assert.match(result.cardText,example.available?/Advert open · checked/:/Open unknown · last check/);
  assert.ok(result.cardText.includes(example.summary||'Work and requirements not established.'));
  assert.match(result.cardText,/Still unknown: Confirm the recorded office arrangement\./,'compact cards retain material uncertainty');
  assert.match(result.detail,/What is still missing for a decision/);
  assert.doesNotMatch(result.detail,/robots_disallowed|Decisive unknowns/);
  assert.equal(result.termsCount,1,'one terms owner in the opened role');assert.equal(result.termsBeforeActions,true);
  assert.equal(result.assessmentFirst,true,'employer and decision facts lead the role before hiring-process controls');
  if(example.available)assert.equal(result.blockers,'');
  else {assert.match(result.blockers,/advert open not confirmed/);assert.match(result.blockers,/work and requirements not established/);}
 }
 assert.deepEqual(writes,[],'opening decision facts never writes');assert.deepEqual(errors,[]);await page.close();
}
async function verifyEngagementPresentation(context,base,original){
 const page=await context.newPage(),ref=original.opportunities.discovery.find(item=>item.worth_a_look);
 const source=original.records.find(record=>record.id===ref.record_id),errors=[],actual=[],expected=[];
 page.on('pageerror',error=>errors.push(error.message));
 const cases=[
  {engagement:'permanent',conflicts:['Advert text states permanent; Stored engagement says contract.'],expected:'Engagement unresolved · source terms conflict'},
  {engagement:'unknown',conflicts:[],expected:'Engagement not stated'},
  {engagement:'permanent',conflicts:[],expected:'Permanent'},
 ];
 for(const [index,example] of cases.entries()){
  const summary=structuredClone(original),record=summary.records.find(item=>item.id===source.id);
  summary.version=`synthetic-engagement-${index}`;
  record.engagement='contract';record.pay='£85,000';record.location='London';record.ir35='';
  record.relevance.qualification={...record.relevance.qualification,engagement:example.engagement,conflicts:example.conflicts};
  await page.route('**/api/state?view=summary*',route=>route.fulfill({json:summary}));
  await page.goto(base+'/#work/'+source.id);await page.locator('#detail[open] .job-controls').waitFor();
  // Exercise the weekly component independently of admission: conflicting terms must not be recommended.
  const weekly=await page.evaluate(async({record,ref})=>{
   const before=JSON.stringify(record),{createOpportunityCards}=await import('/js/opportunity-card.js');
   const cards=createOpportunityCards({getState:()=>({company_logos:{}}),openRecord:()=>{},mutate:()=>{},canLeave:()=>true,jobStatusSaved:()=>{},showAlert:()=>{}});
   const card=cards.card(record,ref);
   return {terms:card.querySelector('.opportunity-terms').textContent,unchanged:JSON.stringify(record)===before};
  },{record,ref});
  const detail=page.locator('#detail');
  actual.push({weekly:weekly.terms,unchanged:weekly.unchanged,
   assessment:await detail.locator('.work-assessment .job-terms > div').filter({has:page.locator('dt').filter({hasText:/^Engagement$/})}).locator('dd').innerText(),
   lane:await detail.locator('.detail-title .lane').innerText()});
  expected.push({weekly:`${example.expected} · London`,unchanged:true,assessment:example.expected,lane:example.expected});
  await page.unroute('**/api/state?view=summary*');await page.goto('about:blank');
 }
 assert.deepEqual(actual,expected,'all terms surfaces agree without inventing engagement or modifying stored evidence');
 await page.goto(base+'/#week');await page.locator('#connection-status').filter({hasText:'Workspace connected'}).waitFor();
 const summary='Hybrid, based in London, Bath or Edinburgh. Other source context remains in the full brief.';
 const quote='You will lead the software engineering teams; improve release readiness.';
 const url='https://fixture.example/remit?source=advert#duties';
 const remits=await page.evaluate(async({source,ref,summary,quote,url})=>{
  const {createOpportunityCards}=await import('/js/opportunity-card.js'),{workAssessment}=await import('/js/approach-brief.js');
  const cards=createOpportunityCards({getState:()=>({company_logos:{}}),openRecord:()=>{},mutate:()=>{},canLeave:()=>true,jobStatusSaved:()=>{},showAlert:()=>{}});
  const requirement={kind:'requirement',quote:'Experience with quality engineering is essential.',url:'https://fixture.example/requirement'};
  const duty={kind:'remit',quote,url,date:'2026-10-29'};
  return [[requirement,duty],[requirement],[{...duty,url:''}],[{...duty,url:'javascript:alert(1)'}],[{...duty,kind:undefined}]].map(evidence=>{
   const record=structuredClone(source);record.summary=summary;record.relevance.fit.evidence=evidence;
   const before=JSON.stringify(record),card=cards.card(record,ref),assessment=workAssessment(record);
   return {remit:card.querySelector('.opportunity-remit')?.textContent,url:card.querySelector('.opportunity-remit a')?.getAttribute('href') || '',
    fullRemit:[...assessment.querySelectorAll('.brief-part')].find(part=>part.querySelector('h4')?.textContent==='Remit')?.querySelector('p')?.textContent,
    unchanged:JSON.stringify(record)===before};
  });
 },{source,ref,summary,quote,url});
 const fallback={remit:'Hybrid, based in London, Bath or Edinburgh.',url:'',fullRemit:summary,unchanged:true};
 assert.deepEqual(remits,[{remit:`“${quote}”`,url,fullRemit:summary,unchanged:true},fallback,fallback,fallback,fallback],
  'only a sourced remit quote replaces the card excerpt; requirements and unattributed wording cannot become duties');
 assert.deepEqual(errors,[]);await page.close();
}
async function verifyWeeklyRefreshAndLogos(context,base,original,mobile){
 const page=await context.newPage(),errors=[],logoRequests=[];page.on('pageerror',e=>errors.push(e.message));
 const source=original.opportunities.discovery.filter(ref=>ref.worth_a_look).slice(0,3);
 let current=structuredClone(original),reads=0,holdNext=false,releaseHeld,heldStarted;
 const started=new Promise(resolve=>heldStarted=resolve),held=new Promise(resolve=>releaseHeld=resolve);
 current.version='synthetic-week-1';current.opportunities={...current.opportunities,discovery:source,act_now:[],waiting:[],in_progress:[],needs_checking:[]};
 current.company_logos={};
 for(const [index,ref] of source.entries()){
  const record=current.records.find(r=>r.id===ref.record_id);record.logo_company_id='logo-'+index;
  current.company_logos[record.logo_company_id]={url:`https://media.licdn.com/dms/image/test/company-logo-${index}?e=1793200000&signed=unchanged`,company_name:record.organisation};
 }
 const firstId=source[0].record_id,firstRecord=current.records.find(r=>r.id===firstId);
 firstRecord.relevance.fit.requirements=[{quote:'NHS delivery experience',importance:'essential',status:'not_established',url:'https://fixture.example/requirements'},{quote:'Degree not required',importance:'ambiguous',status:'not_required',negated:true,url:'https://fixture.example/requirements'}];
 await page.clock.install({time:Date.now()});
 await page.route('**/api/state?view=summary*',async route=>{reads++;const snapshot=structuredClone(current);if(holdNext){holdNext=false;heldStarted();await held;}return route.fulfill({json:snapshot});});
 await page.route('https://media.licdn.com/**',route=>{
  logoRequests.push({url:route.request().url(),headers:route.request().headers()});
  if(route.request().url().includes('company-logo-2'))return route.abort();
  return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jGZkAAAAASUVORK5CYII=','base64')});
 });
 const response=await page.goto(base+'/#week');
 assert.match(response.headers()['content-security-policy'],/img-src 'self' https:\/\/media\.licdn\.com(?:;|$)/);
 const first=()=>page.locator(`[data-opportunity-id="${firstId}"]`);
 await first().locator('.company-logo img.is-loaded').waitFor();
 assert.equal(await first().locator('.company-logo img').getAttribute('referrerpolicy'),'no-referrer');
 assert.equal(await first().locator('.company-logo img').getAttribute('decoding'),'async');
 assert.equal((await first().locator('.company-logo').boundingBox()).width,mobile?40:64);
 const third=page.locator(`[data-opportunity-id="${source[2].record_id}"]`);
 assert.equal(await third.locator('.company-logo img').getAttribute('src'),null,'off-screen images have no remote src');
 const before=(await third.locator('.company-logo').boundingBox());await third.scrollIntoViewIfNeeded();
 await page.waitForFunction(id=>document.querySelector(`[data-opportunity-id="${id}"] .company-logo img`)===null,source[2].record_id);
 assert.equal(await third.locator('.company-initials').isVisible(),true,'image failure retains initials');
 assert.equal((await third.locator('.company-logo').boundingBox()).height,before.height,'reserved dimensions survive failure');
 assert.ok(logoRequests.length>=2);assert.ok(logoRequests.every(request=>!request.headers.referer));
 assert.ok(logoRequests.every(request=>request.url.endsWith('?e=1793200000&signed=unchanged')));
 // CSP rejects other origins independently of the server's capture validation.
 let forbidden=0;await page.route('https://forbidden.example/**',route=>{forbidden++;return route.abort();});
 await page.evaluate(()=>{window.imagePolicyViolation=false;document.addEventListener('securitypolicyviolation',event=>{if(event.effectiveDirective==='img-src')window.imagePolicyViolation=true;});const img=document.createElement('img');img.src='https://forbidden.example/logo.png';document.body.append(img);img.remove();});
 await page.waitForFunction(()=>window.imagePolicyViolation);assert.equal(forbidden,0);
 await first().scrollIntoViewIfNeeded();await first().locator('.opportunity-evidence > summary').click();
 await first().locator('.work-assessment').waitFor();
 await first().locator('.work-evidence-sources > summary').click();
 assert.match(await first().locator('.requirement-comparison').innerText(),/Essential · Not established from recorded experience/);
 assert.match(await first().locator('.requirement-comparison').innerText(),/Degree not required/);
 assert.equal(await first().locator('.requirement-comparison a[href="https://fixture.example/requirements"]').count(),2);
 await first().locator('.work-evidence-sources > summary').click();await first().locator('.opportunity-evidence > summary').focus();
 current=structuredClone(current);current.version='synthetic-week-2';current.records.find(r=>r.id===firstId).name='Refreshed synthetic priority';
 await page.clock.runFor(31000);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
 await first().getByRole('button',{name:'Refreshed synthetic priority',exact:true}).waitFor();
 assert.ok(await first().locator('.opportunity-evidence').evaluate(node=>node.open),'expanded evidence survives live refresh');
 await page.waitForFunction(()=>document.activeElement?.matches('.opportunity-evidence > summary'));
 const count=reads;await page.evaluate(()=>{window.dispatchEvent(new Event('focus'));document.dispatchEvent(new Event('visibilitychange'));});
 await page.clock.runFor(1000);assert.equal(reads,count,'focus/visibility share the return cooldown');
 // A changed snapshot is announced while a sheet holds a captured revision. It cannot silently replace it.
 await first().locator('.opportunity-title').click();await page.locator('#detail[open]').waitFor();
 current=structuredClone(current);current.version='synthetic-week-3';current.records.find(r=>r.id===firstId).revision=firstRecord.revision+1;
 current.records.find(r=>r.id===firstId).name='Newer unseen priority';
 await page.clock.runFor(31000);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
 await page.locator('#priority-update').waitFor();assert.match(await page.locator('#detail h2').first().innerText(),/Refreshed synthetic priority/);
 let submittedRevision;await page.route(`**/api/records/${firstId}/transition`,async route=>{submittedRevision=route.request().postDataJSON().revision;await route.fulfill({status:409,json:{error:'Synthetic stale revision'}});});
 const shortlist=page.locator('#detail [data-job-status=pursue]');
 for(const ancestor of await shortlist.locator('xpath=ancestor::details[not(@open)]').all())await ancestor.locator(':scope > summary').click();
 await shortlist.click();await page.locator('#alert').filter({hasText:'This record changed elsewhere'}).waitFor();
 assert.equal(submittedRevision,firstRecord.revision,'the save uses the viewed revision, never the pending summary');
 await page.locator('#close').click();await page.locator('#priority-update').click();
 await first().getByRole('button',{name:'Newer unseen priority',exact:true}).waitFor();
 assert.equal(await page.locator('#priority-update').isVisible(),false);
 // A slower background response cannot overwrite the state installed by a later save.
 await page.unroute(`**/api/records/${firstId}/transition`);
 await page.route(`**/api/records/${firstId}/transition`,route=>route.fulfill({json:{ok:true}}));
 current=structuredClone(current);current.version='synthetic-week-4';current.records.find(r=>r.id===firstId).name='Slow older priority';holdNext=true;
 await page.clock.runFor(31000);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await started;
 current=structuredClone(current);current.version='synthetic-week-5';current.records.find(r=>r.id===firstId).name='Latest saved priority';
 await first().locator('[data-job-status=pursue]').click();
 await first().getByRole('button',{name:'Latest saved priority',exact:true}).waitFor();
 releaseHeld();await page.clock.runFor(1000);
 assert.equal(await first().locator('.opportunity-title').innerText(),'Latest saved priority');
 // A modal's text and focus survive the five-minute visible refresh too.
 await page.getByRole('button',{name:'Add incoming role',exact:true}).click();
 await page.locator('#modal').getByLabel('Job title',{exact:true}).fill('Unsaved incoming title');
 current=structuredClone(current);current.version='synthetic-week-6';current.records.find(r=>r.id===firstId).name='Waiting for explicit refresh';
 // The five-minute age gate is sampled once a minute. A prior read can fall between ticks;
 // advance until the periodic read starts, bounded by the age gate plus one sampling tick.
 const beforeVisibleRefresh=reads,periodicResponse=page.waitForResponse(response=>response.url().includes('/api/state?view=summary'));
 for(let tick=0;tick<6 && reads===beforeVisibleRefresh;tick++)await page.clock.runFor(60000);
 await periodicResponse;
 assert.equal(reads,beforeVisibleRefresh+1,'visible polling reads once within five minutes plus one sampling tick');
 await page.locator('#priority-update').waitFor();
 assert.equal(await page.locator('#modal').getByLabel('Job title',{exact:true}).inputValue(),'Unsaved incoming title');
 assert.equal(await first().locator('.opportunity-title').innerText(),'Latest saved priority');
 assert.deepEqual(errors,[]);await page.close();
}

async function run(type,mobile=false){
 const label=mobile?'iPhone WebKit':'desktop Chromium',dir=mkdtempSync(join(tmpdir(),'wo-workflow-')),database=join(dir,'fixture.db');
 execFileSync(python,['tools/make_fixture.py',database],{cwd:root,stdio:'ignore'});
 const ids=JSON.parse(execFileSync(python,['-c',fixture,database],{cwd:root,encoding:'utf8'}));
 const port=await freePort(),base=`http://127.0.0.1:${port}`,server=spawn(python,['workspace/server.py','--db',database,'--port',String(port)],{cwd:root,stdio:'ignore'});let browser,failure;
 const diagnostics=createBrowserEvidence({suite:'opportunity-workflow',browser:label});
 try{
  for(let i=0;i<100;i++){try{if((await fetch(base+'/api/health')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  browser=await launchBrowser(type,!mobile&&process.env.WO_UI_CHROMIUM?{executablePath:process.env.WO_UI_CHROMIUM}:{});const context=await browser.newContext(mobile?devices['iPhone 13']:{viewport:{width:1300,height:950}}),page=await context.newPage();page.setDefaultTimeout(10000);
  await diagnostics.attach(context);
  const errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push([r.method(),r.url()]));
  const read=async()=> (await fetch(base+'/api/state')).json();
  const open=async(hash)=>{await page.goto('about:blank');await page.goto(base+'/#'+hash);await page.locator('#connection-status').filter({hasText:'Workspace connected'}).waitFor();};
  const reveal=async target=>{await target.waitFor({state:'attached'});while(await target.locator('xpath=ancestor::details[not(@open)]').count())await target.locator('xpath=ancestor::details[not(@open)]').first().locator(':scope > summary').click();};
  const noCompose=async()=>{assert.equal(await page.locator('textarea[name=message],textarea[name=sent_message],.note-box,.note-template,.email-handoff').count(),0);assert.equal(await page.getByRole('button',{name:/Copy (draft|note)|Approve.*note|Prepare approach|Open email draft/}).count(),0);};
  const initialResponse=page.waitForResponse(r=>r.url().endsWith('/api/state?view=summary&encoding=strings-v1'));
  await open('week');assert.equal(await page.title(),'work/ops');await page.locator('.week-discovery [data-opportunity-id]').first().waitFor();
  assert.equal((await (await initialResponse).json()).encoding,'strings-v1','the real page uses the budgeted transport');
  assert.equal(requests.filter(([,u])=>u.endsWith('/api/state?view=summary')).length,0,'no ordinary summary fallback on a healthy server');
  let state=await read();assert.equal(state.opportunities.discovery.length,10);const expected=state.opportunities.discovery.filter(r=>r.worth_a_look).map(r=>r.record_id);
  // Three complete decision briefs first; expand to ten without more reads or any history fetch.
  assert.equal(state.opportunities.discovery.find(r=>r.record_id===ids.unconfirmed)?.worth_a_look,false);
  assert.equal(expected.length,9);assert.ok(!expected.includes(ids.unconfirmed));
  assert.deepEqual(await page.locator('.week-discovery [data-opportunity-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.opportunityId)),expected.slice(0,3));
  const beforeExpand=requests.length;
  await page.locator('.week-discovery .show-more').click();
  assert.deepEqual(await page.locator('.week-discovery [data-opportunity-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.opportunityId)),expected.slice(0,10));
  assert.equal(await page.locator('.week-discovery .show-more').isVisible(),false);
  assert.equal(requests.slice(beforeExpand).filter(([,u])=>/\/api\/(state|records)/.test(u)).length,0);
  const synthetic=expected.slice(0,10).find(id=>ids.jobs.includes(id));
  const rich=page.locator(`.week-discovery [data-opportunity-id="${synthetic}"]`);
  assert.equal(await rich.locator('.opportunity-company').innerText(),'Synthetic Ltd');
  assert.equal(await rich.locator('.opportunity-pay').innerText(),'£650/day');
  assert.ok((await rich.locator('.opportunity-source').innerText()).trim());
  assert.ok((await rich.locator('.opportunity-why').innerText()).includes(state.opportunities.discovery.find(r=>r.record_id===synthetic).reason));
  await rich.locator('.opportunity-evidence > summary').click();
  await rich.locator('.work-assessment').waitFor();assert.equal(await rich.locator('.work-assessment').count(),1);
  assert.equal(requests.slice(beforeExpand).filter(([,u])=>u.includes('/timeline')).length,0);
  await page.locator('.week-discovery a').filter({hasText:`See all ${state.opportunities.discovery.length} in Jobs → Discovery`}).click();
  await page.waitForFunction(()=>document.querySelector('#title')?.textContent==='Jobs');
  await page.locator('[data-work-filter=discovery][aria-pressed=true]').waitFor();
  assert.equal(await page.locator('.work-history').count(),1);
  assert.equal(await page.locator('.work-history').evaluate(node=>node.open),false);
  assert.equal(await page.locator('[data-work-filter=other]').isVisible(),false);
  const quietId=ids.jobs.find(id=>!state.opportunities.discovery.some(ref=>ref.record_id===id));
  const quietRecord=state.records.find(record=>record.id===quietId);
  await page.locator('#search').fill(quietRecord.name);
  await page.getByText(quietRecord.name,{exact:true}).first().waitFor();
  await page.locator('#search').fill('');
  await page.locator('.work-history > summary').click();
  assert.equal(await page.locator('[data-work-filter=other]').innerText(),'Reference');
  await page.locator('[data-work-filter=other]').click();
  await page.getByText('No review required. Search finds all records. Opportunities return to the working list when the evidence earns them a place.',{exact:true}).waitFor();
  await page.locator('[data-work-filter=discovery]').click();


  const kept=page.locator(`#content [data-opportunity-id="${ids.unconfirmed}"]`);
  const moreJobs=page.locator('#content > div:has(> .opportunity-list) > .show-more:visible');
  while(!(await kept.count()))await moreJobs.click();
  assert.match(await kept.locator('.evidence-check').innerText(),/Open unknown · last check/);
  await open('week');
  assert.equal(await page.locator('.week-people,.week-tally,.week-preparation-summary,.week-followups,.week-deferred').count(),0);await noCompose();
  assert.equal(requests.filter(([m,u])=>m==='POST'&&/\/api\/week\//.test(u)).length,0);
  // Capacity presentation uses canonical server references and exactly one summary read, never per-card fetches.
  const original=await (await fetch(base+'/api/state?view=summary')).json();
  await verifyDecisionPresentation(context,base,original,mobile);
  // An older server or invalid transport recovers via ordinary JSON, keeping the rich cards.
  for(const response of [{status:404,body:'Not found'},{json:{encoding:'strings-v1',strings:[],value:'~0'}}]){
   await page.route('**/api/state?view=summary&encoding=strings-v1',route=>route.fulfill(response));
   const before=requests.length;await page.reload();await page.locator('.week-discovery [data-opportunity-id]').first().waitFor();
   assert.equal(requests.slice(before).filter(([,u])=>u.endsWith('/api/state?view=summary')).length,1,'one ordinary JSON recovery');
   assert.ok((await page.locator('.week-discovery .opportunity-why').first().innerText()).trim());
   await page.unroute('**/api/state?view=summary&encoding=strings-v1');
  }
  for(const count of [100,1000,3000]){
   const source=original.records.find(r=>r.id===expected[0]),summary=structuredClone(original);delete summary.version;summary.records=Array.from({length:count},(_,n)=>({...source,id:'synthetic-'+n,name:'Synthetic role '+n}));summary.opportunities={discovery:summary.records.map(r=>({record_id:r.id,action_id:null,lane:'contract',reason:'Canonical order',worth_a_look:true})),active:[],shortlisted:[],closed:[],other:[]};
   await page.route('**/api/state?view=summary*',route=>route.fulfill({json:summary}));const before=requests.length;await page.reload();await page.locator('.week-discovery [data-opportunity-id]').first().waitFor();
   assert.equal(await page.locator('.week-discovery [data-opportunity-id]').count(),3);assert.equal(requests.slice(before).filter(([,u])=>u.includes('/api/state?view=summary')).length,1);assert.equal(requests.slice(before).filter(([,u])=>u.includes('/timeline')).length,0);
   await page.unroute('**/api/state?view=summary*');
  }
  // Qualification is the app's background work, folded until the owner asks to inspect it.
  const checkingSummary=structuredClone(original);
  checkingSummary.opportunities.needs_checking=[{...checkingSummary.opportunities.discovery[0],research_status:{status:'not_attempted'}}];
  await page.route('**/api/state?view=summary*',route=>route.fulfill({json:checkingSummary}));
  await open('week');await page.locator('.week-needs-checking').waitFor();
  assert.equal(await page.locator('.week-needs-checking').evaluate(node=>node.tagName==='DETAILS'&&!node.open),true);
  await page.locator('.week-needs-checking > summary').click();
  await page.getByText('Research incomplete. If you want, Shortlist roles worth keeping or Skip unsuitable ones. Usual automatic passes: 09:00 and 19:00 London time. A particular role may wait for a later pass.',{exact:true}).waitFor();
  assert.equal(await page.getByRole('link',{name:'See candidates in Jobs',exact:true}).count(),0);
  await page.unroute('**/api/state?view=summary*');
  await verifyWeeklyRefreshAndLogos(context,base,original,mobile);
  await verifyEngagementPresentation(context,base,original);
  const post=async(id,to)=>{const current=(await read()).records.find(r=>r.id===id);const response=await fetch(`${base}/api/records/${id}/transition`,{method:'POST',headers:{Origin:base,'Content-Type':'application/json'},body:JSON.stringify({revision:current.revision,field:'status',to})});assert.ok(response.ok,await response.text());};
  await post(ids.jobs[1],'applied');await post(ids.jobs[2],'applied');
  // Saving B must retain factual note A on either shared progress surface, including a failed retry.
  for(const tab of ['work','week']){
   await open(tab);if(tab==='work')await page.locator('[data-work-filter=active]').click();
   const a=page.locator(`#content [data-opportunity-id="${ids.jobs[1]}"]`),b=page.locator(`#content [data-opportunity-id="${ids.jobs[2]}"]`);
   await reveal(a);await reveal(b);
   if(tab==='week'){assert.equal(await a.locator('.opportunity-company').innerText(),'Synthetic Ltd');assert.ok((await a.locator('.opportunity-source').innerText()).trim());}
   await a.locator('.week-progress > summary').click();await b.locator('.week-progress > summary').click();
   const note=a.locator('textarea[aria-label="Note for this step"]');await reveal(note);await note.fill(`Unsaved ${tab} factual note A`);
   const bAction=b.locator('.approach-actions > button.primary');
   const endpoint=`**/api/records/${ids.jobs[2]}/transition`;
   await page.route(endpoint,route=>route.fulfill({status:503,json:{error:'Synthetic B unavailable'}}));await bAction.click();
   await b.locator('[role=status]').filter({hasText:'Synthetic B unavailable'}).waitFor();assert.equal(await note.inputValue(),`Unsaved ${tab} factual note A`);await page.unroute(endpoint);
   await bAction.click();await page.locator('#notice').filter({hasText:'Saved:'}).waitFor();
   assert.equal(await a.locator('textarea[aria-label="Note for this step"]').inputValue(),`Unsaved ${tab} factual note A`,'unrelated successful save retains the note');
   const beforeLeave=page.url();page.once('dialog',dialog=>dialog.dismiss());await page.locator('#nav [data-tab=people]').click();assert.equal(page.url(),beforeLeave,'dirty detection still guards navigation after B saves');
   const aNote=a.locator('textarea[aria-label="Note for this step"]');await reveal(aNote);
   await a.locator('.approach-actions > button.primary').click();await page.locator('#notice').filter({hasText:'Saved:'}).waitFor();
   assert.ok((await read()).history.some(h=>h.entity_id===ids.jobs[1]&&h.note===`Unsaved ${tab} factual note A`),'retained note remains saveable');
  }
  await open('pipeline/'+ids.jobs[0]);await page.locator('#detail[open]').waitFor();assert.equal(new URL(page.url()).hash,'#work/'+ids.jobs[0]);await noCompose();
  const detail=page.locator('#detail');assert.equal(await detail.locator('.work-assessment').count(),1);assert.equal(await detail.locator('.problem-brief,.brief-missing-note,.approach-message').count(),0);
  const evidence=detail.locator('.work-evidence-sources');await reveal(evidence);await evidence.locator(':scope > summary').click();
  const assessment=detail.locator('.work-assessment');
  for(const text of ['Canonical older requirement: support the inherited billing service.','Canonical older requirement: provide release traceability.','Canonical older unknown: confirm the delivery owner.','Canonical older route: vacancy-owner@example.test'])assert.ok((await assessment.innerText()).includes(text),`canonical field retained: ${text}`);
  for(const days of [8,7,6])assert.equal(await assessment.locator(`a[href="https://fixture.example/canonical/${days}"]`).count(),1,'each canonical field keeps its own source once');
  assert.equal(await assessment.getByText('Canonical older requirement: support the inherited billing service.',{exact:true}).count(),1,'canonical facts are deduplicated');
  state=await read();const canonical=state.records.find(r=>r.id===ids.jobs[0]).briefing;for(const field of ['facts','public_contact_route','decisive_unknowns']){const node=assessment.locator(`.brief-field-provenance[data-field="${field}"]`);assert.equal(await node.getAttribute('data-source-date'),canonical.provenance[field].source_date);assert.equal(await node.getAttribute('data-reviewed-at'),canonical.provenance[field].reviewed_at);}
  const generatedCheck=await page.evaluate(async source=>{const {workAssessment}=await import('/js/approach-brief.js');source.research.push({approach:{opener:'Generated wording must not become evidence.',talking_points:['Generated wording must not become evidence.'],goal:'Generated wording must not become evidence.'}});return workAssessment(source,{brief:{hypothesis:'Generated wording must not become evidence.',help:'Generated wording must not become evidence.'}}).textContent;},state.records.find(r=>r.id===ids.jobs[0]));
  assert.doesNotMatch(generatedCheck,/Generated wording must not become evidence/);
  // A save reloads only what changed; applied to the page's summary it equals a fresh full one.
  const held=await (await fetch(base+'/api/state?view=summary')).json(),sent=requests.length;
  const shortlist=detail.locator('[data-job-status=pursue]');await reveal(shortlist);const updated=page.waitForResponse(r=>r.url().includes('/api/state?view=summary&since='));
  await shortlist.click();const update=await updated;await page.locator('#notice').filter({hasText:'Application status'}).waitFor();
  const changes=await update.json();assert.equal(new URL(update.url()).searchParams.get('since'),held.version);assert.equal(changes.delta,true);
  assert.ok(JSON.stringify(changes).length<JSON.stringify(held).length/2,'the update is smaller than the summary');
  assert.equal(requests.slice(sent).filter(([,u])=>u.includes('/api/state?view=summary')&&!u.includes('&since=')).length,0,'no whole-summary reload after the save');
  assert.deepEqual(applySummaryDelta(held,changes),await (await fetch(base+'/api/state?view=summary')).json());assert.equal((await read()).records.find(r=>r.id===ids.jobs[0]).status,'pursue');
  await page.locator('#notice').getByRole('button',{name:'Undo',exact:true}).click();await page.locator('#notice').filter({hasText:'Undone'}).waitFor();assert.equal((await read()).records.find(r=>r.id===ids.jobs[0]).status,'candidate');
  await page.locator('#close').click();await open('templates/'+ids.person);await page.locator('#detail[open]').waitFor();assert.equal(new URL(page.url()).hash,'#people/'+ids.person);await noCompose();
  assert.equal(await detail.locator('.person-approach-brief > h3').innerText(),'Do this next');
  assert.equal(await detail.locator('.approach-next').innerText(),'Choose a purpose for contacting them and record the next step.');
  assert.ok(!(await read()).intelligence.actions.find(a=>a.record_id===ids.person).progress.next_step,'the displayed queue advice does not create a recorded commitment');
  const saved=detail.locator('.saved-wording-history');await reveal(saved);await saved.locator(':scope > summary').click();assert.match(await saved.innerText(),/final unsent version/);assert.match(await saved.innerText(),/<literal> version one/);assert.equal(await saved.locator('textarea,button,form').count(),0);
  const log=detail.getByRole('button',{name:'Log contact',exact:true}).first();await reveal(log);await log.click();const modal=page.locator('#modal');assert.equal(await modal.locator('[name=sent_message],[name=wording]').count(),0);
  await modal.getByLabel('How you got in touch',{exact:true}).selectOption('call');await modal.getByLabel('Contact note (optional)',{exact:true}).fill('Synthetic factual phone conversation.');
  const path=`**/api/records/${ids.person}/outcome`;let release;const wait=new Promise(r=>release=r);await page.route(path,async route=>{await wait;await route.fulfill({status:503,json:{error:'Synthetic temporary failure'}});});
  const save=modal.getByRole('button',{name:'Save contact',exact:true});await save.click();assert.equal(await save.isDisabled(),true);await page.keyboard.press('Escape');assert.equal(await modal.isVisible(),true);release();await modal.getByRole('status').filter({hasText:'Synthetic temporary failure'}).waitFor();assert.equal(await modal.getByLabel('Contact note (optional)',{exact:true}).inputValue(),'Synthetic factual phone conversation.');await page.unroute(path);
  // Stale revisions refuse instead of losing history; the same form remains retryable after refresh.
  await page.route(path,route=>route.fulfill({status:409,json:{error:'Synthetic stale revision; refresh before retrying'}}));await save.click();await modal.getByRole('status').filter({hasText:'This record changed elsewhere'}).waitFor();assert.equal(await modal.getByLabel('Contact note (optional)',{exact:true}).inputValue(),'Synthetic factual phone conversation.');await page.unroute(path);
  await save.click();await modal.waitFor({state:'hidden'});state=await read();let outcome=state.outcomes.find(o=>o.record_id===ids.person&&!o.corrected_at&&o.kind==='sent');assert.ok(outcome);assert.equal(outcome.draft_version_id,null);assert.equal(outcome.snapshot.message,undefined);
  await page.locator('#notice').getByRole('button',{name:'Undo',exact:true}).click();await page.locator('#notice').filter({hasText:'Undone:'}).waitFor();assert.ok((await read()).outcomes.find(o=>o.id===outcome.id).corrected_at);
  // Human next steps survive factual contact logging, independent edits, reply and reload.
  const actionId=await detail.locator('.approach-block').first().getAttribute('data-action-id');
  const logAgain=detail.getByRole('button',{name:'Log contact',exact:true}).first();await reveal(logAgain);await logAgain.click();
  await modal.getByLabel('What happens next?',{exact:true}).selectOption('mine');await modal.getByLabel('Actual next step',{exact:true}).fill('Read the agreed role evidence.');
  await modal.getByRole('button',{name:'Save contact',exact:true}).click();await modal.waitFor({state:'hidden'});
  state=await read();let action=state.intelligence.actions.find(a=>a.id===actionId);assert.equal(action.progress.next_step.text,'Read the agreed role evidence.');assert.equal(action.progress.status,'waiting');
  const editStep=detail.getByRole('button',{name:'Update next step',exact:true}).first();await reveal(editStep);await editStep.click();
  await modal.getByLabel('What happens next?',{exact:true}).selectOption('waiting');await modal.getByLabel('Actual next step',{exact:true}).fill('Wait for their role details.');
  await modal.getByRole('button',{name:'Save next step',exact:true}).click();await modal.waitFor({state:'hidden'});
  state=await read();action=state.intelligence.actions.find(a=>a.id===actionId);assert.equal(action.progress.next_step.kind,'waiting');assert.equal(action.progress.next_step.text,'Wait for their role details.');
  const reply=detail.getByRole('button',{name:'They replied',exact:true}).first();await reveal(reply);await reply.click();await page.locator('#notice').filter({hasText:'Saved:'}).waitFor();
  state=await read();assert.ok(state.outcomes.some(o=>o.record_id===ids.person&&!o.corrected_at&&o.kind==='replied'));
  await page.reload();await detail.waitFor({state:'visible'});await noCompose();assert.match(await detail.innerText(),/Do this next/);
  state=await read();assert.equal(state.intelligence.actions.find(a=>a.id===actionId).progress.next_step.text,'Wait for their role details.');
  assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'no horizontal page clipping');
  const captures=process.env.WO_WORK_CAPTURE_DIR;if(captures){mkdirSync(captures,{recursive:true});for(const tab of ['week','work','people']){await open(tab);await page.evaluate(()=>{document.activeElement?.blur();window.scrollTo(0,0);});await page.waitForTimeout(250);assert.equal(await page.evaluate(()=>scrollY),0);await page.screenshot({path:join(captures,`${tab}-${mobile?'iphone':'desktop'}.png`),caret:'initial'});if(tab==='week'){const best=page.locator('.week-discovery');await best.evaluate(node=>window.scrollTo(0,window.scrollY+node.getBoundingClientRect().top-90));await page.screenshot({path:join(captures,`best-opportunities-${mobile?'iphone':'desktop'}.png`),caret:'initial'});}}}
  console.log(`ok    opportunity workflow (${label}): projection order, 3/+7 and Jobs 10/+10, 100/1000/3000 bounded reads, old routes, shortlist/Undo, immutable history, failed/stale retry, logos/CSP/fallback, live-refresh races, contact/Undo/reload, no compose controls`);
 }catch(error){failure=error;throw error;}finally{await diagnostics.finish({error:failure});if(browser)await browser.close();server.kill();rmSync(dir,{recursive:true,force:true});}
}
try{if(wanted.includes('chromium'))await run(chromium);if(wanted.includes('webkit'))await run(webkit,true);}catch(error){console.error(error.stack||error);process.exitCode=1;}
