// A ready-to-use brief is the default view. Synthetic records only; no external messages or reads.
import assert from 'node:assert/strict';
import { chromium, webkit, devices } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const root = join(dirname(fileURLToPath(import.meta.url)), '../..');
const python = join(root, '.venv/bin/python');
const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
const capture = process.env.WO_APPROACH_BRIEF_CAPTURE_DIR;
const generalMessage = 'Hi Alex — a general catch-up about our old payments team.';
const jobMessage = 'Hi Aaron — I am interested in the specific Senior QA Engineer vacancy at Northwind. Could we discuss its release-readiness remit?';
const freePort = () => new Promise(resolve => {
  const server = net.createServer();
  server.listen(0, '127.0.0.1', () => { const { port } = server.address(); server.close(() => resolve(port)); });
});
const prepare = String.raw`
import datetime as dt,json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'tools'),str(Path.cwd()/'workspace')]
import db,records,record_imports
c=db.connect(sys.argv[1]);now=dt.datetime.now(dt.timezone.utc).isoformat()
person=c.execute("SELECT id FROM records WHERE name='Alex Example'").fetchone()['id']
org=c.execute("SELECT id FROM records WHERE name='Northwind Fintech'").fetchone()['id']
def create(name,url):
 rid=records.create(c,dict(type='job',name=name,organisation='Northwind Fintech',status='candidate',url=url,
  role='Senior QA Engineer',pay='£90,000',location='London',engagement='permanent',
  summary='Software quality engineering and release readiness for a payments product.'),actor='agent')['record']['id']
 records.edit(c,rid,dict(revision=records.get(c,rid)['revision'],cv_override='QE'),actor='agent')
 return rid
def link(a,b,relation):
 records.link(c,dict(from_id=a,to_id=b,relation=relation,revision=records.get(c,a)['revision']),actor='agent')
job=create('Brief Fixture Senior QA Engineer','https://northwind.example.com/jobs/brief')
no_note=create('Brief Fixture Lead QA Engineer','https://northwind.example.com/jobs/no-note')
alternative=records.create(c,dict(type='person',name='Aaron Alternative',relationship='warm',priority='next'),actor='agent')['record']['id']
for rid in [job,no_note]:
 link(rid,org,'hiring_for');link(rid,person,'related');link(rid,alternative,'related')
 source='https://northwind.example.com/jobs/brief' if rid==job else 'https://northwind.example.com/jobs/no-note'
 record_imports.import_research_entries(c,[dict(id=rid,reviewed_at=now,source_urls=[source],freshness_checks=[
  dict(scope='vacancy',checked_at=now,status='confirmed',source_url=source,basis='The employer advert has an active application route.'),
  dict(scope='contact_route',checked_at=now,status='confirmed',source_url=source,person_id=person,organisation_id=org,
       basis='Alex is listed at this employer; hiring responsibility is not established.'),
  dict(scope='contact_route',checked_at=now,status='confirmed',source_url=source,person_id=alternative,organisation_id=org,
       basis='Aaron is another recorded contact route at the employer.')])],'research:approach-brief-fixture')
record_imports.import_research_entries(c,[dict(id=job,reviewed_at=now,
 source_urls=['https://northwind.example.com/engineering-context'],
 facts=['The payments team needs stronger release readiness.', 'CV evidence: software quality leadership and practical automation are relevant.'],
 decisive_unknowns=['Confirm whether two London office days are required.'],
 public_contact_route='Ask the recorded engineering contact about the advertised remit.')],'research:approach-brief-context')
# Four routine checks must not displace the prepared context or make its review date look current.
for n in range(1,5):
 later=(dt.datetime.fromisoformat(now)+dt.timedelta(seconds=n)).isoformat()
 source='https://northwind.example.com/jobs/brief'
 record_imports.import_research_entries(c,[dict(id=job,reviewed_at=later,source_urls=[source],freshness_checks=[
  dict(scope='vacancy',checked_at=now,status='confirmed',source_url=source,
       basis='The employer advert still has an active application route.')])],'research:approach-brief-checks')
# A personal route and an employer route are separate claims; a private-record confirmation stays dated.
record_imports.import_research_entries(c,[dict(id=person,reviewed_at=now,source_urls=['https://alex.example.com/about',
  'https://mail.google.com/mail/u/0/#all/fixture','https://www.linkedin.com/in/example-alex'],freshness_checks=[
  dict(scope='contact_route',checked_at=now,status='confirmed',source_url='https://alex.example.com/about',person_id=person,
       provenance='public_read',basis='Their own page links this exact profile.'),
  dict(scope='contact_route',checked_at=now,status='confirmed',source_url='https://mail.google.com/mail/u/0/#all/fixture',
       person_id=person,basis='Matched to an earlier direct email.'),
  dict(scope='contact_route',checked_at=now,status='unverified',source_url='https://www.linkedin.com/in/example-alex',
       person_id=person,organisation_id=org,provenance='public_read',basis='Source read, but the employer route could not be established.')])],
  'research:approach-brief-person-routes')
records.draft(c,dict(record_id=person,revision=records.get(c,person)['revision'],purpose='message',text=sys.argv[2]),actor='agent')
records.draft(c,dict(record_id=job,revision=records.get(c,job)['revision'],purpose='message',title='For Aaron Alternative — Senior QA Engineer',text=sys.argv[3]),actor='agent')
blocked=records.create(c,dict(type='person',name='Brief Fixture Do Not Contact',relationship='do_not_contact',
  preferred_channel='email',email='opt-out@example.com',url='https://www.linkedin.com/in/example-opt-out/'),actor='agent')['record']['id']
link(job,blocked,'related')
fallback=records.create(c,dict(type='person',name='Brief Fixture Prepared Person',preferred_channel='email',
  known_context='Met at an invented engineering meetup.',url='https://www.linkedin.com/in/example-prepared/'),actor='agent')['record']['id']
records.draft(c,dict(record_id=fallback,revision=records.get(c,fallback)['revision'],purpose='message',
  text='Hello — picking up our specific discussion at the engineering meetup.'),actor='agent')
# A research run's receipt: the panel shows its time and outcome in words, never the raw values.
c.execute("INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?)",('research:approach-brief-attempt',now[:10],
 json.dumps(dict(items=[dict(id=job,attempted_at='2026-10-17T12:02:21Z',outcome='unverified')])),'partial'));c.commit()
print(json.dumps(dict(person=person,job=job,no_note=no_note,blocked=blocked,fallback=fallback)))
c.close()
`;

async function run(type, mobile = false) {
  const label = mobile ? 'iPhone WebKit' : 'desktop Chromium';
  const dir = mkdtempSync(join(tmpdir(), 'wo-approach-brief-'));
  const database = join(dir, 'brief.db');
  execFileSync(python, [join(root, 'tools/make_fixture.py'), database], { stdio: 'ignore' });
  const ids = JSON.parse(execFileSync(python, ['-c', prepare, database, generalMessage, jobMessage], { cwd: root, encoding: 'utf8' }));
  const port = await freePort(), base = `http://127.0.0.1:${port}`;
  const server = spawn(python, [join(root, 'workspace/server.py'), '--db', database, '--port', String(port)], { stdio: 'ignore' });
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(`${base}/api/health`)).ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, 'synthetic server starts');
    browser = await launchBrowser(type, !mobile && process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {});
    const context = await browser.newContext(mobile ? devices['iPhone 13'] : { viewport: { width: 1300, height: 950 } });
    await context.addInitScript(() => Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: async text => { window.__copied = text; } }
    }));
    await context.route(/^https:\/\//, route => route.fulfill({ status: 200, contentType: 'text/html', body: '<title>Synthetic destination</title>' }));
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
    page.on('request', request => {
      if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method()) && !request.url().includes('/api/seen/')) writes.push(request.url());
    });
    const state = async () => (await fetch(`${base}/api/state`)).json();
    const commercial = s => ({ records: s.records.map(r => [r.id,r.revision,r.status,r.relationship]),
      outcomes: s.outcomes, drafts: s.drafts.map(d => [d.record_id,d.version_id,d.text]) });
    const initial = commercial(await state());
    const detail = page.locator('#detail');
    const report = message => console.log(`ok    approach brief (${label}): ${message}`);
    const open = async (id, kind) => {
      await page.goto('about:blank');
      await page.goto(`${base}/#work/${id}`);
      await page.waitForSelector(`#detail[open] .${kind}-approach-brief`);
      await page.waitForFunction(() => document.querySelector('#connection-status')?.textContent !== 'Refreshing…');
    };
    async function captureSheet(name) {
      assert.ok(await detail.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'sheet has no horizontal overflow');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page has no horizontal overflow');
      assert.equal(await page.locator('dialog[open]').count(), 1, 'only the intended sheet is open');
      if (!capture) return;
      mkdirSync(capture, { recursive: true });
      await page.locator('#detail-content').evaluate(el => { el.scrollTop = 0; });
      capturing = true;
      try {
        await page.screenshot({ path: join(capture, `${name}-${mobile ? 'mobile' : 'desktop'}.png`), caret: 'initial' });
        // Capture actual viewport states: a tall element screenshot crosses the modal's scroll
        // boundary and includes the obscured page, so it is not a faithful full-brief image.
        if (name.startsWith('job')) {
          await detail.locator('.suggested-contact').scrollIntoViewIfNeeded();
          await page.screenshot({ path: join(capture, `${name}-contact-${mobile ? 'mobile' : 'desktop'}.png`), caret: 'initial' });
        }
        const lower = detail.locator(name.startsWith('job') ? '.job-message' : '.brief-relevant-jobs').first();
        if (await lower.count()) {
          await lower.scrollIntoViewIfNeeded();
          await page.screenshot({ path: join(capture, `${name}-context-${mobile ? 'mobile' : 'desktop'}.png`), caret: 'initial' });
        }
      }
      finally { capturing = false; }
    }

    await open(ids.job, 'job');
    const jobBrief = detail.locator('.job-approach-brief');
    const hiring = detail.locator('.incoming-process');
    const hiringText = await hiring.innerText();
    assert.match(hiringText, /Last research check: 17 Oct(,| at) \d\d:\d\d · Some sources not confirmed/);
    assert.doesNotMatch(hiringText, /\d{4}-\d{2}-\d{2}T\d\d:\d\d|\bunverified\b/, 'no raw timestamps or outcome codes');
    assert.ok(await hiring.evaluate(el => {
      const note = el.querySelector('.incoming-research'), actions = el.querySelector('.incoming-actions');
      return !!(note && actions && note.compareDocumentPosition(actions) & Node.DOCUMENT_POSITION_FOLLOWING);
    }), 'research status and its reason come before the hiring-process actions');
    assert.match(await jobBrief.innerText(), /Alex Example/);
    const routeBadge = jobBrief.locator('.brief-route > :last-child');
    const routeText = await routeBadge.innerText();
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await routeBadge.innerText(), routeText, 'the periodic check refresh leaves the route badge as rendered');
    assert.match(await jobBrief.locator('.brief-job-context').innerText(), /payments team needs stronger release readiness/);
    assert.match(await jobBrief.locator('.job-fit').innerText(), /CV evidence: software quality leadership/);
    assert.match(await jobBrief.locator('.job-fit').innerText(), /Confirm whether two London office days are required/);
    assert.match(await jobBrief.locator('.brief-advert-contact').innerText(), /recorded engineering contact/);
    assert.equal(await jobBrief.locator('.suggested-contact [data-contact-id]').getAttribute('data-contact-id'), ids.person,
      'equally fresh relationships honour Contact first before Next, rather than alphabetical order');
    assert.doesNotMatch(await jobBrief.innerText(), /Brief Fixture Do Not Contact/);
    assert.equal(await jobBrief.getByLabel('Draft for this job', { exact: true }).inputValue(), jobMessage);
    assert.ok(await jobBrief.getByText('For Aaron Alternative — Senior QA Engineer', { exact: true }).isVisible(),
      'the saved draft names its intended recipient even when another contact is suggested');
    assert.equal(await jobBrief.getByLabel('Draft for this job', { exact: true }).getAttribute('readonly'), '');
    assert.doesNotMatch(await jobBrief.innerText(), /general catch-up/);
    await jobBrief.getByRole('button', { name: 'Copy draft', exact: true }).click();
    assert.equal(await page.evaluate(() => window.__copied), jobMessage);
    assert.equal(await detail.locator('.cv-override').isVisible(), false, 'CV editing is secondary');
    assert.equal(await jobBrief.getByRole('link', { name: 'Open CV ↗', exact: true }).getAttribute('href'),
      (await state()).cvs.QE.url, 'the selected suitable CV is accessible from the brief');
    assert.equal(await detail.locator('.detail-actions').isVisible(), false, 'record management is secondary');
    assert.ok(await jobBrief.locator('.vacancy-evidence .evidence-check[data-fresh=true]').isVisible(), 'vacancy check visible in brief');
    assert.ok(await jobBrief.locator('.suggested-contact .evidence-check[data-fresh=true]').isVisible(), 'suggested contact check visible in brief');
    assert.equal(await detail.getByRole('button', { name: 'I applied', exact: true }).count(), 1, 'one visible action records an application');
    const contact = jobBrief.locator('a.approach-contact');
    assert.equal(await contact.getAttribute('href'), 'https://wa.me/447700900123');
    const popup = context.waitForEvent('page');
    await contact.click();
    const destination = await popup; await destination.waitForLoadState('domcontentloaded');
    assert.equal(destination.url(), 'https://wa.me/447700900123');
    assert.equal(await destination.title(), 'Synthetic destination'); await destination.close();
    assert.deepEqual(commercial(await state()), initial, 'opening and copying never sends or changes application status');
    assert.deepEqual(writes, [], 'brief and channel actions make no mutations');
    await captureSheet('job-approach');
    report('one job brief holds the exact job note, current route and channel; copy/open make no writes');
    await detail.locator('.brief-supporting > summary').click();
    const contextSource = detail.locator('.brief-supporting a[href="https://northwind.example.com/engineering-context"]');
    assert.ok(await contextSource.first().isVisible(), 'the original preparation source remains readable after four routine checks');
    assert.match(await detail.locator('.brief-supporting').innerText(), /preparation date, not an availability check/);
    await detail.locator('.brief-supporting > summary').click();
    report('freshness-only updates preserve prepared fit, material gaps and contact context with original provenance');

    await jobBrief.locator(`[data-contact-id="${ids.person}"]`).click();
    await page.waitForSelector('#detail .person-approach-brief');
    assert.equal(await page.locator('#detail-title').innerText(), 'Alex Example');
    const personBrief = detail.locator('.person-approach-brief');
    const evidence = personBrief.getByLabel('Evidence for this approach');
    assert.ok(await evidence.isVisible(), 'evidence brief is independent of the editable message');
    assert.match(await evidence.innerText(), /Intended ask:/);
    const status = evidence.locator('.brief-status .chip');
    assert.match(await status.innerText(), /^(Context available|Needs your input|Research pending|Research blocked)$/);
    assert.match(await status.getAttribute('class'), /\bstate-(ready|waiting|in_progress|blocked)\b/, 'evidence status carries its colour');
    await evidence.getByText('Evidence sources and dates', { exact: true }).click();
    assert.match(await evidence.innerText(), /Recorded context|The owner-confirmed/);
    assert.match(await evidence.innerText(), /Original date not recorded|Source\/check date:/);
    assert.doesNotMatch(await evidence.innerText(), /\d{4}-\d{2}-\d{2}T\d\d:\d\d/, 'evidence dates are readable');
    await evidence.getByText('Evidence sources and dates', { exact: true }).click();
    assert.equal(await personBrief.getByLabel('Note to send', { exact: true }).inputValue(), generalMessage);
    assert.match(await personBrief.innerText(), /payments platform launch/);
    assert.ok(await personBrief.getByRole('link', { name: /^CV:/ }).count() === 1);
    assert.match(await personBrief.innerText(), /Brief Fixture Senior QA Engineer/);
    const routes = await detail.locator('.person-route-evidence').innerText();
    assert.match(routes, /Personal route:\s*Contact route confirmed · checked/);
    assert.match(routes, /Personal route:\s*Confirmed from your records \d{4}-\d{2}-\d{2} · not a fresh public check/);
    assert.match(routes, /Via Northwind Fintech:\s*Contact route unverified/);
    assert.equal(await detail.locator('.list-panel').isVisible(), false, 'person setup does not obstruct the brief');
    await captureSheet('person-approach');
    await detail.getByRole('button', { name: '← Back to Brief Fixture Senior QA Engineer', exact: true }).click();
    assert.equal(await page.locator('#detail-title').innerText(), 'Brief Fixture Senior QA Engineer');
    report('contact navigation shows message, prior context, CV and relevant jobs; Back returns to the job');

    await open(ids.no_note, 'job');
    assert.equal(await detail.getByLabel('Draft for this job', { exact: true }).count(), 0);
    assert.match(await detail.locator('.job-approach-brief').innerText(), /A note for this job has not been prepared yet/);
    assert.doesNotMatch(await detail.locator('.job-approach-brief').innerText(), /general catch-up/);
    assert.equal(await detail.getByRole('button', { name: 'Copy draft', exact: true }).count(), 0);
    await open(ids.blocked, 'person');
    assert.equal(await detail.locator('.person-approach-brief a.approach-contact').count(), 0);
    assert.equal(await detail.locator('.person-approach-brief textarea[name=message]').count(), 0);
    assert.deepEqual(commercial(await state()), initial);
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
    report('missing job preparation is explicit, personal catch-up is not substituted, and opt-outs have no outreach route');

    await open(ids.fallback, 'person');
    const fallback = detail.locator('.person-approach-brief');
    assert.equal(await fallback.locator('.approach-block').count(), 0, 'no synthetic progress action is invented');
    const fallbackText = 'Hello — picking up our specific discussion at the engineering meetup.';
    assert.equal(await fallback.getByLabel('Draft for this person', { exact: true }).inputValue(), fallbackText);
    await fallback.getByRole('button', { name: 'Copy draft', exact: true }).click();
    assert.equal(await page.evaluate(() => window.__copied), fallbackText);
    assert.deepEqual(commercial(await state()), initial, 'a saved message remains usable without a generated approach');
    assert.deepEqual(writes, []);
    await fallback.getByRole('button', { name: 'Add email address', exact: true }).click();
    const email = detail.locator('.list-panel input[name=email]');
    assert.ok(await email.isVisible(), 'missing-contact shortcut reveals the editable field directly');
    await email.fill('prepared@example.com');
    const saved = page.waitForResponse(response => response.request().method() === 'PATCH' && response.url().includes(`/api/records/${ids.fallback}`));
    await detail.locator('.list-panel').getByRole('button', { name: 'Save', exact: true }).click();
    assert.ok((await saved).ok());
    await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('place on your list'));
    assert.equal((await state()).records.find(record => record.id === ids.fallback).email, 'prepared@example.com');
    assert.equal(await detail.locator('.person-approach-brief a.approach-contact').getAttribute('href'), 'mailto:prepared@example.com');
    assert.equal(await detail.locator('.list-panel').isVisible(), false, 'saved contact returns to the concise brief');
    assert.deepEqual(errors, []);
    report('saved person draft survives without generated progress; Add email opens and saves the exact field');

    await open(ids.person, 'person');
    const pending = (await state()).intelligence.actions.find(action => action.record_id === ids.person && action.progress?.message);
    const sentAction = pending.progress.actions.find(action => action.kind === 'sent');
    await detail.locator('.approach-block').getByRole('button', { name: sentAction.label, exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#detail .approach-block .chip')?.textContent === 'Waiting for a reply');
    const afterSend = await state();
    const unsent = 'A later saved note is still available while the earlier message awaits a reply.';
    const replaced = await fetch(`${base}/api/records/${ids.person}/draft`, { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: base },
      body: JSON.stringify({ revision: afterSend.records.find(record => record.id === ids.person).revision,
        purpose: 'message', title: 'Later unsent note', text: unsent }) });
    assert.ok(replaced.ok, 'fixture revision replaces the saved message through the canonical API');
    await open(ids.person, 'person');
    assert.equal(await detail.locator('.person-approach-brief textarea[name=message]').count(), 0, 'waiting state does not imply a new send action');
    const retained = detail.locator('.brief-supporting .draft').filter({ hasText: unsent });
    assert.equal(await retained.count(), 1, 'saved note remains accessible when progress omits its message');
    while (await retained.locator('xpath=ancestor::details[not(@open)]').count())
      await retained.locator('xpath=ancestor::details[not(@open)]').first().locator(':scope > summary').click();
    assert.equal(await retained.innerText(), unsent);
    await detail.locator('.approach-sent > summary').click();
    assert.match(await detail.locator('.approach-sent').innerText(), /general catch-up/, 'the recorded send keeps its original wording');
    assert.deepEqual(errors, []);
    report('a later saved note remains in Sources and details while the prior exact message waits for a reply');
  } finally {
    if (browser) await browser.close();
    server.kill(); rmSync(dir, { recursive: true, force: true });
  }
}
try {
  if (wanted.includes('chromium')) await run(chromium);
  if (wanted.includes('webkit')) await run(webkit, true);
} catch (error) { console.error(error.stack || error); process.exitCode = 1; }
