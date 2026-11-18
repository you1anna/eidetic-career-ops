// Canonical synthetic evidence → API → rendered job and contact routes, per browser.
// Uses real record/link/research writers. Nothing reads or writes the owner's data.
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
const capture = process.env.WO_JOB_EVIDENCE_CAPTURE_DIR;
const freePort = () => new Promise(resolve => {
  const server = net.createServer();
  server.listen(0, '127.0.0.1', () => { const { port } = server.address(); server.close(() => resolve(port)); });
});

const fixture = String.raw`
import datetime as dt,json,sys
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path[:0]=[str(Path.cwd()/'tools'),str(Path.cwd()/'workspace')]
import db,records,record_imports
from unittest.mock import patch
c=db.connect(sys.argv[1]);db.migrate(c)
now=dt.datetime.fromtimestamp(int(sys.argv[2])/1000,dt.timezone.utc)
stamp=lambda seconds=0:(now+dt.timedelta(seconds=seconds)).isoformat()
def create(record_type,name,**fields):
 return records.create(c,dict(type=record_type,name=name,**fields),actor='agent')['record']['id']
def link(a,b,relation):
 records.link(c,dict(from_id=a,to_id=b,relation=relation,revision=records.get(c,a)['revision']),actor='agent')
org=create('company','Evidence Example Ltd')
agency=create('agency','Evidence Agency',kind='agency',status='not_contacted')
other=create('company','Unrelated Example Ltd')
generalorg=create('company','General Route Employer')
general=create('job','Fixture General Employer Route',status='candidate',url='https://example.test/jobs/general')
link(general,generalorg,'hiring_for')
fresh=create('job','Fixture Forward Deployed Engineer',organisation='Evidence Example Ltd',status='candidate',url='https://example.test/jobs/fresh')
old=create('job','Fixture Email-only Opportunity',status='candidate',date='2020-01-01',url='https://example.test/jobs/email')
failed=create('job','Fixture Unavailable Opportunity',status='candidate',url='https://example.test/jobs/failed')
with patch('db.now',return_value=stamp(-17*86400)):
 closed=create('job','Fixture Source Closed Opportunity',status='candidate',url='https://example.test/jobs/closed')
 records.transition(c,closed,dict(field='status',to='applied',revision=records.get(c,closed)['revision']),actor='owner')
deadline=create('job','Fixture Deadline Opportunity',status='candidate',published_deadline=now.astimezone(ZoneInfo('Europe/London')).date().isoformat(),url='https://example.test/jobs/deadline')
current=create('person','Alex Evidence',role='Engineering lead',relationship='warm',priority='first',url='https://www.linkedin.com/in/synthetic-alex')
blocked=create('person','Bailey Blocked',url='https://www.linkedin.com/in/synthetic-bailey')
private=create('person','Parker Private',relationship='dormant')
historic=create('person','Harper Historical',role='Former colleague',relationship='dormant')
reversed=create('person','Reversed Introducer')
dnc=create('person','Private Opt-out',relationship='do_not_contact')
wrong=create('person','Morgan Other Employer')
link(fresh,org,'hiring_for');link(fresh,current,'related');link(current,org,'works_at')
link(fresh,agency,'advertised_by')
link(blocked,org,'works_at');link(private,org,'works_at')
link(historic,org,'formerly_at');link(org,reversed,'introducer');link(fresh,dnc,'related');link(wrong,org,'works_at')
url='https://example.test/current-advert'
def check(at,status='confirmed',**fields):
 return dict(scope='vacancy',checked_at=stamp(at),status=status,source_url=url,
             basis='The current source explicitly states this evidence.',**fields)
def research(rid,at,checks=None,**fields):
 finding=dict(id=rid,reviewed_at=stamp(at),source_urls=[url],**fields)
 if checks is not None:finding['freshness_checks']=checks
 record_imports.import_research_entries(c,[finding],'research:synthetic-job-evidence')
contact=check(-86340);contact.update(scope='contact_route',person_id=current,organisation_id=org)
othercheck=check(-60);othercheck.update(scope='contact_route',person_id=wrong,organisation_id=other)
blockedcheck=check(-120,'unverified');blockedcheck.update(scope='contact_route',person_id=blocked,organisation_id=org,basis='robots_disallowed')
privatecheck=check(-172800);privatecheck.update(scope='contact_route',person_id=private,organisation_id=org,provenance='prior_correspondence')
research(fresh,-50,[check(-172740),contact,othercheck,blockedcheck,privatecheck],
         summary='Build software test automation and improve release readiness for the engineering platform.')
research(closed,-50,[check(-60)])
scope='board:fixture:closed'
record_imports.import_observations(c,Path(sys.argv[1]).parent,dict(generated_at=stamp(),sources={
 scope:dict(provider='fixture',observed_at=stamp(-30),state='ok',complete=True,item_ids=['closed'])},items={
 'closed':dict(source_scope=scope,source_id='closed',kind='job',last_seen_at=stamp(-120),
  source_lifecycle='closed',closed_at=stamp(-30),current=dict(title='Fixture Source Closed Opportunity',
  company='Evidence Example Ltd',url='https://example.test/jobs/closed',eligible=True))}))
assert records.get(c,closed)['source_lifecycle']=='closed'
research(deadline,-50,[check(0)])
record_imports.import_research_entries(c,[dict(id=generalorg,reviewed_at=stamp(-86400),verification='primary-read',source_urls=['https://example.test/careers'],public_contact_route='For future opportunities, send a CV to people@example.test. Responsibility for this vacancy is unconfirmed.')],'research:synthetic-general-route')
for n in range(4):research(generalorg,-100+n,summary='Unrelated later company context.')
research(old,-30,source_date='2020-01-01',summary='An old alert imported and reviewed recently; no current advert read.')
research(failed,-100,[check(-200)])
research(failed,-50,[check(-60,'unverified')])
# Unscoped later reviews push the real checks outside the three-observation preview.
for i in range(4):research(failed,-40+i,summary='Background context only.')
# Human-reported contact remains waiting without a promised date, even after weeks.
with patch('db.now',return_value=stamp(-17*86400)):
 action=next(a for a in records.state(c)['intelligence']['actions'] if a['record_id']==current and a.get('route')!='agency')
 records.outcome(c,current,dict(revision=records.get(c,current)['revision'],action_id=action['id'],kind='sent',channel='linkedin'),actor='owner')
midnight=dt.datetime.combine(now.astimezone(ZoneInfo('Europe/London')).date()+dt.timedelta(days=1),dt.time(),ZoneInfo('Europe/London'))
print(json.dumps(dict(fresh=fresh,old=old,failed=failed,closed=closed,deadline=deadline,
                     midnight=int(midnight.timestamp()*1000),current=current,historic=historic,wrong=wrong,blocked=blocked,private=private,general=general,agency=agency,route_review=stamp(-86400))))
c.close()
`;

async function run(type, mobile = false) {
  const label = mobile ? 'iPhone WebKit' : 'desktop Chromium';
  const dir = mkdtempSync(join(tmpdir(), 'wo-job-evidence-'));
  const database = join(dir, 'evidence.db');
  const now = Math.floor(Date.now() / 1000) * 1000;
  const ids = JSON.parse(execFileSync(python, ['-c', fixture, database, String(now)], { cwd: root, encoding: 'utf8' }));
  const port = await freePort(), base = `http://127.0.0.1:${port}`;
  const server = spawn(python, [join(root, 'workspace/server.py'), '--db', database, '--port', String(port)], { stdio: 'ignore' });
  const evidence = createBrowserEvidence({ suite: 'job-evidence', browser: label });
  let browser, failure;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(`${base}/api/health`)).ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, 'synthetic server starts');
    browser = await launchBrowser(type, !mobile && process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {});
    const context = await browser.newContext(mobile ? devices['iPhone 13'] : { viewport: { width: 1300, height: 950 } });
    await evidence.attach(context);
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [], writes = [];
    let capturing = false;
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() !== 'error') return;
      if (mobile && capturing && message.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'")) return;
      errors.push(message.text());
    });
    page.on('request', request => { if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) writes.push(request.url()); });
    await page.clock.install({ time: now - 1000 });
    const state = async () => (await fetch(`${base}/api/state`)).json();
    const detail = page.locator('#detail');
    const labelOf = () => detail.locator('.job-approach-brief .vacancy-evidence .evidence-check');
    const report = message => console.log(`ok    job evidence (${label}): ${message}`);
    async function reveal(target) {
      await target.waitFor({ state: 'attached' });
      while (await target.locator('xpath=ancestor::details[not(@open)]').count())
        await target.locator('xpath=ancestor::details[not(@open)]').first().locator(':scope > summary').click();
    }
    const open = async id => {
      // A blank document avoids WebKit logging aborted old timeline requests during navigation.
      await page.goto('about:blank');
      await page.goto(`${base}/#work/${id}`);
      try { await page.waitForSelector('#detail[open] .vacancy-evidence'); }
      catch (error) {
        console.error(JSON.stringify({ requested: id, url: page.url(), errors,
          visible: (await page.locator('body').innerText()).slice(0, 2500) }));
        throw error;
      }
      await page.waitForFunction(() => document.querySelector('#connection-status')?.textContent !== 'Refreshing…');
      await page.waitForFunction(() => !document.querySelector('#detail .timeline-section')?.textContent.includes('Loading'));
    };
    await open(ids.old);
    const initial = await state();
    assert.equal(initial.records.find(record => record.id === ids.fresh).relevance.fit.status, 'supported',
      'the availability fixture has sourced work that overlaps recorded experience');
    assert.ok(initial.opportunities.discovery.some(ref => ref.record_id === ids.fresh),
      'the qualified advert belongs in Discovery before its availability ages');
    assert.equal(await page.title(), 'work/ops');
    assert.equal(initial.records.find(record => record.id === ids.closed).status, 'applied');
    assert.equal(initial.intelligence.actions.find(action => action.record_id === ids.closed).progress.status, 'follow_up');
    assert.ok(initial.opportunities.closed.some(ref => ref.record_id === ids.closed));
    assert.ok(!initial.opportunities.research_ids.includes(ids.closed));
    assert.match(await detail.innerText(), /Fixture Email-only Opportunity/);
    assert.equal(await labelOf().getAttribute('data-fresh'), 'false');
    assert.match(await labelOf().innerText(), /Open unknown · not checked yet/);
    assert.match(await detail.locator('.job-contacts').innerText(), /No named contact is shown for this role. Check the advert for a hiring contact/);
    const findHiring = detail.getByRole('link', {name:'Find the hiring manager',exact:true});
    assert.equal(await findHiring.count(),1);
    const search = new URL(await findHiring.getAttribute('href'));
    assert.equal(search.origin,'https://www.linkedin.com');
    assert.equal(search.pathname,'/search/results/people/');
    assert.equal(search.searchParams.get('keywords'),'Fixture Email-only Opportunity hiring');
    const emptyStates = await page.evaluate(async () => {
      const {jobApproachBrief} = await import('/js/approach-brief.js');
      const base = {id:'synthetic-empty',type:'job',name:'Synthetic empty role',availability:{status:'closed'}};
      const options = {jobControls:()=>document.createElement('div'),isExcluded:r=>!!r.exclusion,progress:{},openRecord(){}};
      return [{}, {url:'javascript:alert(1)'}, {url:'https://example.test/closed'}, {url:'https://example.test/excluded',exclusion:{reason:'Excluded'}}]
        .map(change => {
          const job={...base,...change};
          return jobApproachBrief(job,{records:[job],links:[]},options).querySelector('.job-contacts').textContent;
        });
    });
    assert.match(emptyStates[0],/Add an advert or a sourced hiring contact/);
    assert.match(emptyStates[1],/Add an advert or a sourced hiring contact/);
    assert.match(emptyStates[2],/Check the advert for a hiring contact/);
    assert.equal(emptyStates[3],'Contact routes are hidden for this excluded role.');
    emptyStates.forEach(text => assert.doesNotMatch(text,/application route remains available/));
    report('old email and recent generic review do not imply current availability');

    await open(ids.general);
    const generalRoute = detail.locator('.job-contacts');
    assert.match(await generalRoute.innerText(), /General Route Employer publishes this general contact route/);
    assert.match(await generalRoute.innerText(), /For future opportunities, send a CV to people@example.test. Responsibility for this vacancy is unconfirmed/);
    assert.match(await generalRoute.innerText(), /General employer route, not a named hiring contact/);
    const reviewDay = new Date(ids.route_review).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'});
    assert.ok((await generalRoute.innerText()).includes(`Reviewed ${reviewDay}`));
    assert.equal(await generalRoute.getByRole('link',{name:'Employer source ↗',exact:true}).getAttribute('href'),'https://example.test/careers');
    assert.equal(await generalRoute.locator('.contact-route,button,a[href^=mailto]').count(),0);
    if (capture) {
      mkdirSync(capture,{recursive:true}); capturing=true;
      try {
        await generalRoute.evaluate(el => el.scrollIntoView({block:'center'}));
        await page.screenshot({path:join(capture,`people-general-route-${mobile?'mobile':'desktop'}.png`),caret:'initial'});
      } finally {capturing=false;}
    }
    report('an explicitly linked employer supplies a dated general route without inventing a named hiring contact');

    await open(ids.failed);
    assert.match(await labelOf().innerText(), /Open unknown · last check/);
    assert.equal(await labelOf().getAttribute('data-fresh'), 'false');
    report('newer failed read supersedes confirmation outside the three-review window');

    await open(ids.closed);
    assert.equal(await labelOf().getAttribute('data-fresh'), 'false');
    assert.match(await labelOf().innerText(), /Advert closed · source reports/);
    const closedBefore = (await state()).records.map(record => [record.id, record.revision, record.updated_at]);
    // Read-only weekly views cannot promote a closed advert just because an application aged.
    for (const width of [1280, 390]) {
      await page.setViewportSize({width, height: 844});
      await page.goto('about:blank');
      await page.goto(`${base}/#week`);
      await page.locator('.week-head').waitFor();
      assert.doesNotMatch(await page.locator('#content').innerText(), /Fixture Source Closed Opportunity/);
      if (capture) {
        mkdirSync(capture,{recursive:true});
        await page.screenshot({path:join(capture,`closed-advert-week-${width}-${mobile?'webkit':'chromium'}.png`)});
      }
      await page.getByRole('link', {name:'All jobs',exact:true}).click();
      await page.waitForURL('**/#work');
      if (!(await page.locator('[data-work-filter=closed]').isVisible()))
        await page.locator('.work-history > summary').click();
      await page.getByRole('button', {name:'Closed / skipped',exact:true}).click();
      await page.getByRole('button', {name:'Fixture Source Closed Opportunity · Evidence Example Ltd',exact:true}).click();
      await page.waitForSelector('#detail[open] .vacancy-evidence');
      await page.waitForFunction(() => !document.querySelector('#detail .timeline-section')?.textContent.includes('Loading'));
      assert.match(await labelOf().innerText(), /Advert closed · source reports/);
    }
    assert.deepEqual((await state()).records.map(record => [record.id, record.revision, record.updated_at]), closedBefore,
      'weekly selection and retained application detail preserve record state');
    await page.setViewportSize(mobile ? devices['iPhone 13'].viewport : {width:1300,height:950});
    report('closed unanswered application leaves weekly priorities and research, while its application remains readable');
    await open(ids.deadline);
    assert.equal(await labelOf().getAttribute('data-fresh'), 'true', 'a deadline today remains open through the day');
    const deadlineBefore = (await state()).records.map(r => [r.id, r.revision, r.updated_at]);
    const deadlineWrites = writes.length;
    await page.clock.pauseAt(now + 10000);
    await page.clock.setSystemTime(ids.midnight - 1);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await labelOf().getAttribute('data-fresh'), 'true');
    await page.clock.setSystemTime(ids.midnight);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await labelOf().getAttribute('data-fresh'), 'false');
    assert.match(await labelOf().innerText(), /Advert closed · closing date .* has passed/);
    assert.equal(writes.length, deadlineWrites);
    assert.deepEqual((await state()).records.map(r => [r.id, r.revision, r.updated_at]), deadlineBefore);
    await page.clock.setSystemTime(now - 10000);
    await page.clock.resume();
    report('source closure and passing a London date-only deadline override a recent read without data writes');

    await open(ids.fresh);
    await page.clock.pauseAt(now);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await labelOf().getAttribute('data-fresh'), 'true');
    assert.doesNotMatch(await detail.locator('.decision-blockers').textContent(), /advert open not confirmed/);
    const routes = detail.locator('.contact-route');
    await reveal(routes.filter({ hasText: 'Harper Historical' }));
    assert.equal(await routes.count(), 5);
    assert.equal(await detail.locator('.job-contacts > details > summary').innerText(), 'Connections (5)');
    assert.equal(await findHiring.count(),1,'general employer connections and confirmed routes do not hide hiring-contact search');
    assert.match(await detail.locator('.job-contacts').innerText(),/does not establish who handles this hire/);
    assert.equal(await detail.locator(`[data-contact-id="${ids.current}"]`).count(), 1);
    const current = routes.filter({ hasText: 'Alex Evidence' });
    assert.match(await current.innerText(), /Confirm their current employer and connection to this role before approaching/);
    assert.equal(await current.getByRole('link', {name:'Review profile ↗',exact:true}).getAttribute('href'), 'https://www.linkedin.com/in/synthetic-alex');
    assert.match(await current.textContent(), /Recorded at Evidence Example Ltd/);
    assert.equal(await current.locator('.evidence-check').getAttribute('data-fresh'), 'true');
    assert.match(await routes.filter({ hasText: 'Harper Historical' }).textContent(), /Formerly at/);
    const blockedRoute = routes.filter({hasText:'Bailey Blocked'});
    assert.match(await blockedRoute.innerText(), /unverified/);
    assert.match(await blockedRoute.innerText(), /Confirm their current employer/);
    await blockedRoute.locator('summary').click();
    assert.match(await blockedRoute.innerText(), /Automated check blocked by the site/);
    assert.doesNotMatch(await blockedRoute.innerText(), /robots_disallowed/);
    const privateRoute = routes.filter({hasText:'Parker Private'});
    assert.match(await privateRoute.innerText(), /Previously used route .*current reachability unknown/);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.match(await privateRoute.innerText(), /Previously used route .*current reachability unknown/);
    assert.doesNotMatch(await privateRoute.innerText(), /needs recheck|confirmed.*checked.*ago/i);
    assert.equal(await routes.locator('.contact-log-button,.approach-actions').count(), 0, 'role contacts do not borrow an employer approach');
    assert.equal(await routes.filter({ hasText: 'Morgan Other Employer' }).locator('.evidence-check').getAttribute('data-fresh'), 'false');
    assert.doesNotMatch(await detail.locator('.job-contacts').innerText(), /Reversed Introducer|Private Opt-out/);
    report('named routes deduplicate, label history, respect direction and opt-outs, and retain evidence scope');

    await reveal(detail.locator(`[data-contact-id="${ids.current}"]`));
    await detail.locator(`[data-contact-id="${ids.current}"]`).click();
    assert.equal(await page.locator('#detail-title').innerText(), 'Alex Evidence');
    const waiting = detail.locator('.person-approach-brief .approach-block');
    assert.match(await waiting.innerText(), /Contact recorded .*Waiting for a reply/);
    assert.doesNotMatch(await waiting.innerText(), /No next step recorded|No agreed date|Follow.up suggested/);
    if (capture) {
      mkdirSync(capture, {recursive:true}); capturing = true;
      try {
        await waiting.evaluate(el => el.scrollIntoView({block:'center'}));
        await page.screenshot({path:join(capture, `people-waiting-${mobile?'mobile':'desktop'}.png`),caret:'initial'});
      } finally { capturing = false; }
    }
    await detail.getByRole('button', { name: '← Back to Fixture Forward Deployed Engineer', exact: true }).click();
    assert.equal(await page.locator('#detail-title').innerText(), 'Fixture Forward Deployed Engineer');
    report('contact opens its person record and Back returns to the same job');

    const before = (await state()).records.map(r => [r.id, r.revision, r.updated_at]);
    const writesBefore = writes.length;
    await reveal(detail.getByRole('button', { name: 'Edit record', exact: true, includeHidden: true }));
    await detail.getByRole('button', { name: 'Edit record', exact: true }).click();
    const draft = page.locator('#modal textarea[name=summary]');
    await draft.fill('Unsaved synthetic note: retain this while evidence ages.');
    await page.clock.setSystemTime(now + 60000);
    await page.clock.runFor(60000);
    assert.equal(await labelOf().getAttribute('data-fresh'), 'false');
    assert.match(await labelOf().innerText(), /Last confirmed open/);
    assert.match(await detail.locator('.decision-blockers').textContent(), /advert open not confirmed/);
    assert.equal(await draft.inputValue(), 'Unsaved synthetic note: retain this while evidence ages.');
    assert.equal(writes.length, writesBefore, 'clock updates must not send writes');
    assert.deepEqual((await state()).records.map(r => [r.id, r.revision, r.updated_at]), before);
    report('the 60-second refresh expires aged evidence without writes or losing a form draft');

    // A focus refresh responds immediately to a system-clock jump without waiting for another timer.
    await page.clock.setSystemTime(now + 59999);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await labelOf().getAttribute('data-fresh'), 'true');
    assert.doesNotMatch(await detail.locator('.decision-blockers').textContent(), /advert open not confirmed/);
    await page.clock.setSystemTime(now + 60000);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await labelOf().getAttribute('data-fresh'), 'false');
    assert.match(await detail.locator('.decision-blockers').textContent(), /advert open not confirmed/);
    assert.equal(await draft.inputValue(), 'Unsaved synthetic note: retain this while evidence ages.');
    assert.equal(writes.length, writesBefore);
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#modal-cancel').click();
    assert.equal(await page.locator('#modal').evaluate(el => el.open), false);
    report('focus refresh respects the exact 48-hour availability boundary and preserves the same draft');

    await page.clock.setSystemTime(now);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.locator('#detail-content').evaluate(el => { el.scrollTop = 0; });
    assert.ok((await detail.innerText()).trim().length > 100, 'nonblank job sheet');
    assert.equal(await page.locator('dialog[open]').count(), 1, 'no extra modal overlay');
    assert.ok(await detail.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'job sheet has no horizontal overflow');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page has no horizontal overflow');
    if (capture) {
      mkdirSync(capture, { recursive: true });
      capturing = true;
      try {
        await page.screenshot({ path: join(capture, `job-evidence-${mobile ? 'mobile' : 'desktop'}.png`), caret: 'initial' });
        await reveal(detail.locator('.contact-route').first());
        await detail.locator('.job-contacts').evaluate(el => el.scrollIntoView({ block: 'start' }));
        await page.screenshot({ path: join(capture, `job-contacts-${mobile ? 'mobile' : 'desktop'}.png`), caret: 'initial' });
      }
      finally { capturing = false; }
    }
    await page.locator('#close').click();
    await page.locator('#nav').getByRole('button', { name: /^Jobs\b/ }).click();
    await page.waitForFunction(() => document.querySelector('#title')?.textContent === 'Jobs');
    await page.locator('[data-work-filter=discovery]').click();
    const row = page.locator(`[data-opportunity-id="${ids.fresh}"]`);
    for (const disclosure of await page.locator('details.lower:not([open]) > summary').all()) await disclosure.click();
    while(!await row.count() && await page.locator('.show-more:visible').count())await page.locator('.show-more:visible').click();
    await row.waitFor({ state: 'visible' });
    assert.equal(await row.count(), 1);
    assert.equal(await row.getByRole('button').count(), 1, 'the list offers one entry into the complete brief');
    assert.equal(await row.locator('.table-row button').count(), 0, 'no nested interactive controls');
    if (!(await page.locator('[data-work-filter=closed]').isVisible())) await page.locator('.work-history > summary').click();
    await page.locator('[data-work-filter=closed]').click();
    const closedRow = page.locator('[data-opportunity-id]').filter({ hasText: 'Fixture Source Closed Opportunity' });
    assert.equal(await closedRow.locator('.evidence-check').getAttribute('data-fresh'), 'false');
    assert.match(await closedRow.locator('.evidence-check').innerText(), /Advert closed · source reports/);
    if (!(await page.locator('[data-work-filter=other]').isVisible())) await page.locator('.work-history > summary').click();
    await page.locator('[data-work-filter=other]').click();
    const deadlineRow = page.locator('.table-row-wrap').filter({ hasText: 'Fixture Deadline Opportunity' });
    await page.clock.setSystemTime(ids.midnight);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await deadlineRow.locator('.evidence-check').getAttribute('data-fresh'), 'false');
    assert.match(await deadlineRow.locator('.evidence-check').innerText(), /Advert closed · closing date .* has passed/);
    await page.clock.setSystemTime(now);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.locator('[data-work-filter=discovery]').click();
    await row.locator('.job-brief-row').click();
    await reveal(detail.locator(`[data-contact-id="${ids.current}"]`));
    await detail.locator(`[data-contact-id="${ids.current}"]`).click();
    assert.equal(await page.locator('#detail-title').innerText(), 'Alex Evidence');
    await page.goto('about:blank');
    await page.goto(`${base}/#people`);
    await page.locator('.people-agencies .agency-row').waitFor();
    await page.locator('.people-agencies .agency-row').click();
    await detail.locator('.agency-roles [data-role-id]').waitFor();
    assert.equal(await page.locator('.people-agencies .agency-row').count(),1);
    await reveal(detail.getByRole('button',{name:'Edit record',exact:true,includeHidden:true}));
    await detail.getByRole('button',{name:'Edit record',exact:true}).click();
    const agencyDraft=page.locator('#modal textarea[name=summary]');
    await agencyDraft.fill('Unsaved agency note survives role expiry.');
    const agencyBefore=(await state()).records.map(r=>[r.id,r.revision,r.updated_at]), agencyWrites=writes.length;
    await page.clock.setSystemTime(now+60000);
    await page.clock.runFor(60000);
    assert.equal(await page.locator('.people-agencies .agency-row').count(),0,'expired agency roles leave the recommendation list on the minute tick');
    assert.equal(await detail.locator('.agency-roles [data-role-id]').count(),0,'expired agency roles leave the detail on the minute tick');
    assert.equal(await agencyDraft.inputValue(),'Unsaved agency note survives role expiry.');
    await page.clock.setSystemTime(now+59999);
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('.people-agencies .agency-row').count(),1);
    assert.equal(await detail.locator('.agency-roles [data-role-id]').count(),1);
    await page.clock.setSystemTime(now+60000);
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('.people-agencies .agency-row').count(),0,'focus respects the exact role expiry boundary');
    assert.equal(await detail.locator('.agency-roles').isVisible(),false,'no stale Confirmed open roles heading remains');
    assert.equal(await agencyDraft.inputValue(),'Unsaved agency note survives role expiry.');
    assert.equal(writes.length,agencyWrites);
    assert.deepEqual((await state()).records.map(r=>[r.id,r.revision,r.updated_at]),agencyBefore);
    page.once('dialog',dialog=>dialog.accept());await page.locator('#modal-cancel').click();
    report('agency recommendations and detail expire on minute/focus refresh without losing a dirty form or writing records');
    assert.deepEqual(errors, [], 'no app console or page errors');
    report('list links are actionable, screenshots are synthetic, and layout/console checks pass');
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

try {
  if (wanted.includes('chromium')) await run(chromium);
  if (wanted.includes('webkit')) await run(webkit, true);
} catch (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
}
