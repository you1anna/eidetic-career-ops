// Browser smoke for the local workspace, run by ops/check.sh. Desktop Chromium and iPhone WebKit
// each get a fresh backup copy of the database on a free port, so the live service and its data are
// never touched. Prints one line per check; exits non-zero on any failure.
import { checkSystemMap, captureSystemMap } from './system-map.mjs';
import { chromium, webkit, devices } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { createBrowserEvidence } from './browser-evidence.mjs';
import { execFileSync, spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = join(root, '.venv/bin/python');
const source = process.env.WO_UI_DB || join(root, 'data/work-ops.db');
// ops/check.sh names the browsers this machine has, and a pre-installed Chromium when the pinned
// build's own is missing (a cloud session). Run directly, both browsers run with their own builds.
const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
const chromiumLaunch = process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {};
const failures = [];

function check(name, ok, detail = '') {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${name}${ok || !detail ? '' : ` — ${detail}`}`);
  if (!ok) failures.push(name);
}

const freePort = () => new Promise(resolve => {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
});

async function startServer(dir) {
  const db = join(dir, 'ui.db');
  execFileSync(python, [join(root, 'tools/wo.py'), '--db', source, 'backup', db], { stdio: 'ignore' });
  const port = await freePort();
  const proc = spawn(python, [join(root, 'workspace/server.py'), '--db', db, '--port', String(port)], { stdio: 'ignore' });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/api/health`)).ok) return { proc, base }; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  proc.kill();
  throw new Error('workspace server did not start');
}

// Visible text that shows a URL without a link, and external links that do not open a new tab.
// Buttons cannot hold links, so card and row previews must show a URL's host, never the raw URL.
function unlinked() {
  const found = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node; (node = walker.nextNode());) {
    const el = node.parentElement;
    if (!/https?:\/\//i.test(node.nodeValue) || !el || !el.getClientRects().length) continue;
    if (el.closest('a,textarea,input,select,option,script,style')) continue;
    found.push(`${el.closest('button') ? 'raw URL in a card: ' : ''}${node.nodeValue.trim().slice(0, 80)}`);
  }
  for (const a of document.querySelectorAll('a[href^="http"]')) {
    if (a.target !== '_blank' || !/noopener/.test(a.rel)) found.push(`link without new tab: ${a.href}`);
  }
  return found;
}

async function settle(page) {
  await page.waitForFunction(() => document.querySelector('#connection-status')?.textContent !== 'Refreshing…');
}

// Follow the same disclosure path as a reader; folding diagnostics must not remove their coverage.
async function reveal(target) {
  await target.waitFor({ state: 'attached' });
  while (await target.locator('xpath=ancestor::details[not(@open)]').count())
    await target.locator('xpath=ancestor::details[not(@open)]').first().locator(':scope > summary').click();
}

async function peopleQueue(context, base, name, original) {
  const page = await context.newPage(), state = structuredClone(original);
  const names = ['Morgan Example','Taylor Fixture','Riley Recruiter','Sam Sample-Jones','Alex Example'];
  const people = names.map(value => state.records.find(r=>r.name===value));
  const job = state.records.find(r=>r.type==='job' && r.name==='Interim Engineering Manager');
  for (const person of people) { person.priority=null; person.relationship='warm'; }
  people[0].email='queue@example.test'; people[0].preferred_channel='email';
  people[1].email=''; people[1].phone=''; people[1].preferred_channel=''; people[1].url='https://www.linkedin.com/in/synthetic-queue';
  people[4].priority='first';
  state.records = [...people,job];
  state.links = [{from_id:people[3].id,to_id:job.id,relation:'related'}];
  state.opportunities = {...state.opportunities,act_now:[{record_id:job.id}]};
  state.intelligence = {actions: people.map((person,i)=>({id:`queue:${person.id}`,record_id:person.id,person_id:person.id,
    progress:{status:['replied','waiting','waiting','ready','ready'][i],label:['Replied','Waiting','Waiting','Ready','Ready'][i],
      since:i===1?'2026-10-20':i===2?'2026-11-05':null,next_step:null,actions:[],history:i<3?[{kind:i===0?'replied':'sent',at:'2026-11-05'}]:[]}})),research:[]};
  state.review_queue=[];
  await page.clock.setFixedTime(new Date('2026-11-06T12:00:00Z'));
  await page.route('**/api/state?view=summary*',route=>route.fulfill({json:state}));
  await page.goto(`${base}/#people`); await page.locator('.person-row').first().waitFor();
  const writes=[];
  page.on('request',request=>{if(['POST','PUT','PATCH','DELETE'].includes(request.method()))writes.push(request.url());});
  const snapshot=await(await fetch(`${base}/api/state`)).json();
  const contact=page.locator(`[data-person-id="${people[0].id}"] .person-contact`);
  check(`${name} People Contact: email and LinkedIn handoffs use recorded channels`,
    await contact.getAttribute('href')==='mailto:queue@example.test' &&
    await page.locator(`[data-person-id="${people[1].id}"] .person-contact`).getAttribute('href')==='https://www.linkedin.com/in/synthetic-queue');
  // Capture activation locally instead of starting a real mail app or contacting LinkedIn.
  await contact.evaluate(link=>link.addEventListener('click',event=>{event.preventDefault();link.dataset.activated='true';},{once:true}));
  await contact.click();
  check(`${name} People Contact: activating the external handoff does not record a send`,
    await contact.getAttribute('data-activated')==='true' && writes.length===0 &&
    JSON.stringify((await(await fetch(`${base}/api/state`)).json()).records.map(r=>[r.id,r.revision]))===JSON.stringify(snapshot.records.map(r=>[r.id,r.revision])));
  const ids = () => page.locator('#content .person-row').evaluateAll(rows=>rows.map(r=>r.dataset.id));
  check(`${name} People queue: replies precede oldest waits, Act now and saved priority`,
    JSON.stringify(await ids())===JSON.stringify(people.map(r=>r.id)) &&
    await page.locator('[data-tab=people] .nav-count').textContent()==='5' &&
    (await page.locator('.people-count').textContent()).includes('5 unique people') &&
    (await page.locator('#content').textContent()).includes('Waiting 17 days') &&
    !(await page.locator('#content').textContent()).includes('Not known'));
  await page.locator('[data-people-chip=needs_reply]').click();
  check(`${name} People queue: reply chip filters the toolbar and unique count`,
    JSON.stringify(await ids())===JSON.stringify([people[0].id]) &&
    await page.locator('[data-people-filter=needs_reply]').getAttribute('aria-pressed')==='true' &&
    (await page.locator('.people-count').textContent()).includes('1 unique person'));
  await page.locator('[data-people-filter=waiting]').click();
  check(`${name} People queue: Waiting filters two people in factual age order`,
    JSON.stringify(await ids())===JSON.stringify([people[1].id,people[2].id]));
  await page.locator('[data-people-filter=act_now]').click();
  check(`${name} People queue: For Act now identifies only the linked contact`,
    JSON.stringify(await ids())===JSON.stringify([people[3].id]) &&
    await page.locator('.person-role-link').getAttribute('href')===`#work/${job.id}` &&
    await page.locator('button a,button button').count()===0 &&
    await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.locator('.person-row').click();
  await page.locator('#detail[open] .person-approach-brief').waitFor();
  check(`${name} People detail: next action leads and relationship context is folded`,
    await page.locator('#detail .person-approach-brief>h3').textContent()==='Do this next' &&
    await page.locator('#detail .person-relationship-context[open]').count()===0);
  check(`${name} People detail: Contact opens a recorded email without generating message content`,
    await page.locator('#detail .person-approach-brief .approach-contact').getAttribute('href')==='mailto:sam@example.com' &&
    await page.locator('#detail .email-handoff, #detail textarea[name=message]').count()===0);
  await page.locator('#detail [data-role-id]').first().click();
  await page.waitForURL(`**/#work/${job.id}`);
  check(`${name} People detail: role opens Jobs and linked-record Back retains People`,await page.locator('#detail .job-controls').isVisible());
  await page.locator('#detail .back-link').click();
  await page.waitForURL(`**/#people/${people[3].id}/action/**`);
  await page.locator('#close').click(); await settle(page);
  check(`${name} People queue: closing returned detail retains Act now filter`,
    await page.locator('[data-people-filter=act_now]').getAttribute('aria-pressed')==='true');
  // Playwright's clock is context-wide; restore current time before the ordinary fixture checks.
  await page.clock.setFixedTime(new Date());
  await page.close();
}

async function run(browserType, device, launch = {}) {
  const name = device || 'desktop';
  const dir = mkdtempSync(join(tmpdir(), 'wo-ui-'));
  const { proc, base } = await startServer(dir);
  const browser = await launchBrowser(browserType, launch);
  const evidence = createBrowserEvidence({ suite: 'smoke', browser: name });
  const failuresBefore = failures.length;
  let failure;
  try {
    const context = await browser.newContext(device ? devices[device] : { viewport: { width: 1400, height: 900 } });
    await evidence.attach(context);
    // Assert the exact bytes sent to the browser clipboard API without changing the user's clipboard.
    await context.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async text => { window.__smokeCopiedText = text; },
      } });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [], refused = [];
    let expectedConflictPath = '';
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => {
      if (m.type() !== 'error') return;
      if (expectedConflictPath && /^Failed to load resource:.*409.*Conflict/.test(m.text())) return;
      errors.push(m.text());
    });
    page.on('response', r => {
      if (expectedConflictPath && new URL(r.url()).pathname === expectedConflictPath && r.status() === 409) return;
      if (r.request().method() !== 'GET' && r.status() >= 400) refused.push(`${r.request().method()} ${new URL(r.url()).pathname} → ${r.status()}`);
    });

    await checkSystemMap(page, base, name, check);

    for (const tab of ['week', 'people', 'work']) {
      await page.goto(`${base}/#${tab}`);
      await page.waitForSelector('#content > *');
      await settle(page);
      check(`${name} ${tab}: correct page with meaningful content`,
        await page.title() === 'work/ops' && (await page.evaluate(() => location.hash)) === `#${tab}` &&
        (await page.locator('#content').innerText()).trim().length > 20);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${name} ${tab}: no horizontal overflow`, overflow <= 1, `${overflow}px too wide`);
      const bad = await page.evaluate(unlinked);
      check(`${name} ${tab}: URLs are links that open a new tab`, !bad.length, bad.slice(0, 3).join(' | '));
      if (tab === 'week') {
        // This week's first approach and its one primary step are visible without scrolling.
        const firstMove = page.locator('.week-section [data-opportunity-id]').first();
        const box = await firstMove.locator('h3').first().boundingBox();
        const viewport = page.viewportSize();
        check(`${name} week: this week's first approach is visible in the first screen`,
          !!box && box.y >= 0 && box.y + box.height <= viewport.height,
          box ? `first approach name ends at ${box.y + box.height}px; viewport ${viewport.height}px` : 'No approach this week');
        if (device) {
          const small = await page.locator('.week-list .approach-card button, .week-list .approach-card summary, .opportunity-actions button, .opportunity-actions a').evaluateAll(els =>
            els.filter(e => e.getClientRects().length).map(e => e.getBoundingClientRect().height).filter(h => h < 44));
          check(`${name} week: step buttons are comfortable touch targets (44 px)`, !small.length, small.join(', '));
        } else {
          await firstMove.locator('h3 button').first().focus();
          const ring = await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
          check(`${name} week: keyboard focus is visible on an approach`, ring !== 'none', ring);
        }
        check(`${name} week: site status is one line below this week, linking to the Dashboard`,
          await page.locator('#content .caretaker-processing, #content .email-processing').count() === 0 &&
          await page.locator('.site-line a').getAttribute('href') === '#dashboard' &&
          await page.locator('#content').evaluate(c => c.querySelector('.week-section').compareDocumentPosition(c.querySelector('.site-line')) & Node.DOCUMENT_POSITION_FOLLOWING));
      }
    }

    const navLabels = await page.locator('#nav button > span:first-child').allTextContents();
    check(`${name} nav: This week, People, Jobs and Dashboard`, JSON.stringify(navLabels) === JSON.stringify(['This week','People','Jobs','Dashboard']));
    for(const [old,now] of [['status','dashboard'],['admin','dashboard'],['overview','week'],['network','people'],['sources','people'],['companies','work'],['pipeline','work'],['templates','work'],['outreach','work']]){
      await page.goto(`${base}/#${old}`);await settle(page);
      check(`${name} nav: #${old} opens #${now}`,new URL(page.url()).hash===`#${now}`);
    }

    await peopleQueue(context,base,name,await(await fetch(`${base}/api/state?view=summary`)).json());
    // People: one action queue, then the remaining contacts without duplicated people.
    await page.goto(`${base}/#people`); await settle(page);
    const firstGroup = await page.locator('#content .section-head h2').first().textContent();
    const alexRow = page.locator('#content .person-row').filter({ hasText: 'Alex Example' }).first();
    check(`${name} people: needs attention comes first with an actionable personal context`,
      firstGroup === 'Needs attention' && (await alexRow.textContent()).includes('record the next step') &&
      await page.locator('#content .person-row').filter({hasText:'Alex Example'}).count()===1);
    await page.getByRole('button', {name:'Recruiters & agencies', exact:true}).click();
    check(`${name} people: recruiter filter preserves saved priorities and shows a researched agency`,
      await page.locator('#content .person-row').filter({hasText:'Riley Recruiter'}).count() === 1 && await alexRow.count() === 0 &&
      await page.locator('#content .agency-row').count() === 1 &&
      await page.getByRole('button', {name:'Recruiters & agencies', exact:true}).getAttribute('aria-pressed') === 'true');
    await page.locator('#content .agency-row').click();
    await page.waitForSelector('#detail[open]');
    check(`${name} people: agency reuses the detail dialog and linked consultant`,
      (await page.locator('#detail').textContent()).includes('Sam Sample-Jones'));
    check(`${name} people: agency detail exposes its sourced desk and public contact route`,
      (await page.locator('#detail').textContent()).includes('https://acme-recruitment.example.com/contact') &&
      (await page.locator('#detail').textContent()).includes('Current briefs need confirming.'));
    check(`${name} people: agency sheet shows shared approach progress above the fold and collapses background details`,
      await page.locator('#detail .agency-approach-brief .approach-block').isVisible() &&
      !await page.locator('#detail .detail-actions').isVisible() &&
      !await page.locator('#detail .facts').isVisible());
    await page.locator('#detail .agency-roles button').first().click();
    await page.waitForURL('**/#work/**');
    check(`${name} people: agency role title opens the Jobs detail`,await page.locator('#detail .job-controls').isVisible());
    await page.locator('#detail .back-link').click(); await page.waitForURL('**/#people/**');
    await page.locator('#close').click(); await settle(page);
    check(`${name} people: closing the agency retains the filter`, await alexRow.count() === 0);
    await page.getByRole('button', {name:'Everyone', exact:true}).click();
    check(`${name} people: Everyone restores the personal list`, await alexRow.count() === 1);
    // Changing someone's place on the list from their sheet uses only choices the API accepts.
    const listState = await (await fetch(`${base}/api/state`)).json();
    const jo = listState.records.find(r => r.name === 'Jo Placeholder');
    await page.goto(`${base}/#people/${encodeURIComponent(jo.id)}`);
    await page.waitForSelector('#detail[open] .list-panel', { state: 'attached' });
    check(`${name} people: setup is folded behind the approach`, !await page.locator('#detail .list-panel').isVisible());
    await reveal(page.locator('#detail .list-panel'));
    await page.locator('#detail .list-panel select[name=priority]').selectOption('next');
    await page.locator('#detail .list-panel select[name=preferred_channel]').selectOption('call');
    await page.locator('#detail .list-panel select[name=relationship]').selectOption('active');
    await page.locator('#detail .list-panel [type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('your list'));
    const joNow = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === jo.id);
    check(`${name} people: Your list saves priority, channel and how well you know them`,
      joNow.priority === 'next' && joNow.preferred_channel === 'call' && joNow.relationship === 'active' && !refused.length,
      refused.join(', ') || JSON.stringify([joNow.priority, joNow.preferred_channel, joNow.relationship]));
    await page.locator('#close').click();
    await page.goto(`${base}/#people`); await settle(page);
    check(`${name} people: the change shows once in the action queue straight away`,
      await page.locator('#content .person-row').filter({ hasText: 'Jo Placeholder' }).count()===1);
    await reveal(page.getByRole('button', { name: 'Demote Jo Placeholder to Not contacting', exact: true, includeHidden: true }));
    await page.getByRole('button', { name: 'Demote Jo Placeholder to Not contacting', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Moved Jo Placeholder to Not contacting.'));
    const joDemoted = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === jo.id);
    check(`${name} people: demoting moves the person to Not contacting at the bottom and clears priority`,
      joDemoted.relationship === 'do_not_contact' && joDemoted.priority === null &&
      await page.locator('#people-never > summary').textContent().then(t => t.startsWith('Not contacting (')));
    await page.locator('#notice').getByRole('button',{name:'Undo',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('is back in your contacts'));
    const joUndone=(await(await fetch(`${base}/api/state`)).json()).records.find(r=>r.id===jo.id);
    check(`${name} people: Undo restores the saved relationship and priority`,joUndone.relationship==='active'&&joUndone.priority==='next');
    await reveal(page.getByRole('button', { name: 'Demote Jo Placeholder to Not contacting', exact: true, includeHidden: true }));
    await page.getByRole('button', { name: 'Demote Jo Placeholder to Not contacting', exact: true }).click();
    await page.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('Moved Jo Placeholder to Not contacting.'));
    await page.locator('#people-never > summary').click();
    await page.getByRole('button', { name: 'Promote Jo Placeholder to current contacts', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Promoted Jo Placeholder to current contacts.'));
    const joPromoted = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === jo.id);
    check(`${name} people: promoting from Not contacting moves the person to Current contacts`,
      joPromoted.relationship === 'dormant' && joPromoted.priority === null &&
      await page.locator('#people-current').count() === 1);

    // An introduction offered: add the person with who introduced them, straight onto the list.
    await page.locator('#content .toolbar button', { hasText: 'Add a person' }).click();
    await page.waitForSelector('#modal[open]');
    await page.locator('#modal input[name=name]').fill('Quinn Introduced');
    const alexId = listState.records.find(r => r.name === 'Alex Example').id;
    await page.locator('#modal select[name=introducer]').selectOption(alexId);
    await page.locator('#modal select[name=preferred_channel]').selectOption('email');
    await page.locator('#modal textarea[name=known_context]').fill('Met at an invented meetup');
    await page.locator('#modal-form [type=submit]').click();
    await page.waitForSelector('#detail[open] .list-panel', { state: 'attached' });
    const added = await (await fetch(`${base}/api/state`)).json();
    const quinn = added.records.find(r => r.name === 'Quinn Introduced');
    check(`${name} people: an added person lands on your list with who introduced them`,
      quinn?.priority === 'first' && quinn?.preferred_channel === 'email' && !refused.length &&
      /^Your note \(\d{1,2} [A-Z][a-z]{2} \d{4}\): Met at an invented meetup$/.test(quinn?.known_context || '') &&
      added.links.some(l => l.from_id === alexId && l.to_id === quinn.id && l.relation === 'introducer') &&
      (await page.locator('#detail .link-item').filter({ hasText: 'Alex Example' }).textContent()).includes('Introduced by'),
      refused.join(', '));
    await page.locator('#close').click();

    // One shortlist, manual applications, six CVs and retained excluded context.
    await page.goto(`${base}/#work`); await settle(page);
    const workState = await (await fetch(`${base}/api/state`)).json();
    const job = workState.records.find(r => r.type === 'job' && r.detail_url && r.apply_url);
    const excludedJob = workState.records.find(r => r.type === 'job' && r.exclusion);
    const excludedCompany = workState.records.find(r => r.type === 'company' && r.exclusion);
    check(`${name} work: excluded roles are hidden from discovery`, !!excludedJob &&
      !(await page.locator('#content').innerText()).includes(excludedJob.name));
    if (!(await page.locator('[data-work-filter=other]').isVisible())) await page.locator('.work-history > summary').click();
    await page.locator('[data-work-filter=other]').click();
    while(await page.locator('.opportunity-list [data-opportunity-id]').filter({hasText:excludedJob.name}).count()===0 && await page.locator('.show-more:visible').count())await page.locator('.show-more:visible').click();
    check(`${name} work: Reference retains excluded context`, (await page.locator('#content').innerText()).includes(excludedJob.name));
    await page.goto(`${base}/#work/${encodeURIComponent(excludedJob.id)}`); await settle(page);
    check(`${name} excluded detail: banner, history and logging remain without shortlist/application suggestions`,
      await page.locator('#detail .exclusion-banner').isVisible() &&
      await page.locator('#detail [data-job-status=pursue], #detail [data-job-status=applied], #detail .cv-override').count() === 0 &&
      await page.locator('#detail .history-details').count() === 1 &&
      await page.locator('#detail .detail-actions button', { hasText: 'Log interaction' }).count() === 1);
    await page.goto(`${base}/#outreach`); await settle(page);
    await page.locator('[data-work-filter=discovery]').click();
    const hiddenCompany = !(await page.locator('#content').innerText()).includes(excludedCompany.name);
    await page.goto(`${base}/#people`); await settle(page);
    check(`${name} approaches: excluded company hidden while its warm person remains available`, hiddenCompany &&
      await page.locator('#content .person-row').filter({ hasText: 'Exclusion Fixture Contact' }).count() === 1);
    await page.locator('#search').fill(excludedJob.name);
    await page.locator('.table-row').filter({ hasText: excludedJob.name }).click();
    check(`${name} search: excluded history is searchable with a visible banner`, await page.locator('#detail .exclusion-banner').isVisible());
    await page.goto(`${base}/#work/${encodeURIComponent(job.id)}`); await settle(page);
    const currentRoute = job.relevance.route;
    check(`${name} job: title, company and stated terms are clear`,
      (await page.locator('#detail-title').textContent()) === job.name &&
      (await page.locator('.job-company').textContent()) === job.organisation &&
      (await page.locator('.job-terms').textContent()).includes(job.pay));
    check(`${name} job: application route leads and source advert remains available`,
      await page.getByRole('link', { name: 'Open application' }).getAttribute('href') === job.apply_url &&
      await page.locator(`#detail a[href="${job.detail_url}"]`).count() > 0);
    check(`${name} job: supporting details stay collapsed and CV choice is visible`,
      !await page.locator('#detail .detail-actions').isVisible() &&
      await page.locator('#detail .cv-override').isVisible() &&
      !await page.locator('#detail .facts').isVisible());
    check(`${name} job: CV registry has six choices plus reset and explicit readiness`,
      await page.locator('.cv-override option').count() === 7 &&
      (await page.locator('.cv-status').textContent()).toLowerCase().includes(currentRoute.readiness));
    const externalPages = /^https:\/\//;
    await context.route(externalPages, route => route.fulfill({ status: 200, contentType: 'text/html', body: '<title>Synthetic external link</title>' }));
    const linkNames = ['Open application', ...(currentRoute.url ? ['Open CV'] : [])];
    for (const label of linkNames) {
      const popup = context.waitForEvent('page');
      await page.getByRole('link', { name: label }).click();
      const opened = await popup; await opened.waitForLoadState();
      check(`${name} job: ${label} uses a synthetic destination`, await opened.title() === 'Synthetic external link');
      await opened.close();
    }
    await context.unroute(externalPages);
    const afterLinks = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === job.id);
    check(`${name} job: opening advert/application/CV links leaves application status and revision unchanged`,
      afterLinks.status === job.status && afterLinks.revision === job.revision);
    if (currentRoute.url) check(`${name} job: Open CV uses the selected registry document`,
      await page.getByRole('link', { name: 'Open CV' }).getAttribute('href') === currentRoute.url);
    await reveal(page.locator('.cv-override'));
    await page.locator('.cv-override select').selectOption('Founding');
    await page.locator('.cv-override button').click(); await settle(page);
    await page.waitForFunction(() => document.querySelector('.cv-status')?.textContent.includes('Your selection'));
    const chosen = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === job.id);
    check(`${name} job: revisioned CV override persists on refresh`, chosen.cv_override === 'Founding' && chosen.relevance.route.cv === 'Founding');
    await page.reload(); await settle(page);
    check(`${name} job: chosen CV is shown after refresh`, await page.locator('.cv-override select').inputValue() === 'Founding');
    await reveal(page.locator('.cv-override'));
    await page.locator('.cv-override select').selectOption('');
    await page.locator('.cv-override button').click(); await settle(page);
    await page.waitForFunction(() => document.querySelector('.cv-status')?.textContent.includes('Suggested'));
    const reset = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === job.id);
    check(`${name} job: Use suggestion clears the override`, reset.cv_override === null && reset.relevance.route.cv === currentRoute.cv);
    // Since 14 October a discovered role can be applied for directly; both moves are allowed by the API.
    check(`${name} job: candidate offers Shortlist and I applied, and nothing the API refuses`,
      await page.locator('[data-job-status=pursue]').count() === 1 && await page.locator('[data-job-status=applied]').count() === 1
      && await page.locator('[data-job-status=responded]').count() === 0);
    await reveal(page.locator('[data-job-status=pursue]'));
    await page.locator('[data-job-status=pursue]').click(); await settle(page);
    await page.waitForFunction(() => document.querySelector('.application-status')?.textContent.includes('Shortlisted'));
    await page.goto(`${base}/#work`); await settle(page);
    await page.locator('[data-work-filter=shortlisted]').click();
    check(`${name} work: Shortlist transition appears in the single shortlisted view`,
      await page.locator('[data-opportunity-id]').filter({ hasText: job.name }).count() === 1);
    await page.locator('[data-opportunity-id]').filter({ hasText: job.name }).locator('.approach-name').click();
    await page.locator('[data-job-status=applied]').click(); await settle(page);
    await page.waitForFunction(() => document.querySelector('.application-status')?.textContent.includes('Applied'));
    check(`${name} job: I applied records only the allowed transition`,
      await page.locator('[data-job-status=responded]').count() === 1 && await page.locator('[data-job-status=applied]').count() === 0);
    await page.goto(`${base}/#work`); await settle(page);
    await page.locator('[data-work-filter=active]').click();
    check(`${name} work: application progress filter shows applied work`, await page.locator('[data-opportunity-id]').filter({ hasText: job.name }).count() === 1);
    await page.locator('.work-actions button', { hasText: 'Save to shortlist' }).click();
    await page.getByLabel('Source link', { exact: true }).fill('https://jobs.example.com/manually-saved');
    await page.getByLabel('Job title', { exact: true }).fill('Synthetic saved Engineering Manager');
    await page.getByLabel('Employer (if known)', { exact: true }).fill('Manual Fixture Company');
    const pasted = page.getByLabel('Pasted advert details', { exact: false });
    await pasted.fill('x'.repeat(20001));
    await page.locator('#modal-form [type=submit]').click();
    check(`${name} save job: excessive pasted text remains intact with a clear limit error`,
      (await pasted.inputValue()).length === 20001 && (await page.locator('#modal-status').textContent()).includes('20,000'));
    await pasted.fill('Pasted software engineering team delivery remit.');
    await page.getByLabel('Why interested', { exact: false }).fill('Interested in leading a product engineering team.');
    await page.locator('#modal-form [type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('#modal').open && document.querySelector('#detail').open);
    await settle(page);
    const savedState = await (await fetch(`${base}/api/state`)).json();
    const saved = savedState.records.find(r => r.name === 'Synthetic saved Engineering Manager');
    check(`${name} save job: new job enters the one shortlist with details and interest retained`,
      saved?.status === 'pursue' && saved.summary === 'Interested in leading a product engineering team.' &&
      saved.notes === 'Pasted software engineering team delivery remit.' && (await page.locator('#notice').textContent()) === 'Added to shortlist.');
    await page.locator('[data-job-status=applied]').click(); await settle(page);
    await page.waitForFunction(() => document.querySelector('.application-status')?.textContent.includes('Applied'));
    const beforeDuplicate = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === saved.id);
    await page.goto(`${base}/#work`); await settle(page);
    await page.locator('.work-actions button', { hasText: 'Save to shortlist' }).click();
    await page.getByLabel('Source link', { exact: true }).fill(saved.url);
    await page.getByLabel('Job title', { exact: true }).fill('A duplicate title must not replace the saved record');
    await page.getByLabel('Employer (if known)', { exact: true }).fill('Manual Fixture Company');
    await page.locator('#modal-form [type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('#modal').open && document.querySelector('#detail').open);
    await settle(page);
    const duplicated = (await (await fetch(`${base}/api/state`)).json()).records.find(r => r.id === saved.id);
    check(`${name} save job: duplicate opens the existing record unchanged and says Already saved`,
      duplicated.revision === beforeDuplicate.revision && duplicated.status === 'applied' && duplicated.name === saved.name &&
      (await page.locator('#notice').textContent()).startsWith('Already saved.') && (await page.locator('#detail-title').textContent()) === saved.name);
    check(`${name} job: controls have no nested interactive rows or horizontal overflow`,
      await page.locator('button button, button a').count() === 0 &&
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    await reveal(page.locator('[data-job-status=closed]'));
    await page.locator('[data-job-status=closed]').click(); await settle(page);
    await page.waitForFunction(() => document.querySelector('.application-status')?.textContent.includes('Closed'));
    await page.goto(`${base}/#work`); await settle(page);
    if (!(await page.locator('[data-work-filter=closed]').isVisible())) await page.locator('.work-history > summary').click();
    await page.locator('[data-work-filter=closed]').click();
    check(`${name} work: closed filter keeps completed application history`,
      await page.locator('[data-opportunity-id]').filter({ hasText: saved.name }).count() === 1);
    await page.locator('[data-work-filter=shortlisted]').click();
    check(`${name} work: applied jobs leave the shortlist`, await page.locator('[data-opportunity-id]').filter({ hasText: saved.name }).count() === 0);
    await page.locator('.work-actions button', { hasText: 'Save to shortlist' }).click();
    await page.getByLabel('Source link', { exact: true }).fill(excludedJob.url);
    await page.getByLabel('Job title', { exact: true }).fill(excludedJob.name);
    await page.getByLabel('Employer (if known)', { exact: true }).fill(excludedJob.organisation);
    expectedConflictPath = '/api/jobs/save';
    await page.locator('#modal-form [type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#modal-status')?.textContent.length > 0);
    check(`${name} save job: excluded save explains the exclusion and keeps entered fields`,
      await page.locator('#modal').evaluate(d => d.open) && await page.getByLabel('Source link', { exact: true }).inputValue() === excludedJob.url &&
      (await page.locator('#modal-status').textContent()) === excludedJob.exclusion.reason);
    expectedConflictPath = '';
    page.once('dialog', d => d.accept());
    await page.locator('#modal-cancel').click();
    await page.locator('.work-actions button', { hasText: 'Save to shortlist' }).click();
    await page.getByLabel('Source link', { exact: true }).fill('https://jobs.example.com/employer-not-stated');
    await page.getByLabel('Job title', { exact: true }).fill('Synthetic QA Lead with unknown employer');
    check(`${name} save job: employer can be left unknown`,
      await page.getByLabel('Employer (if known)', { exact: true }).evaluate(input => !input.required && input.value === ''));
    await page.locator('#modal-form [type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('#modal').open && document.querySelector('#detail').open);
    await settle(page);
    const unknownState = await (await fetch(`${base}/api/state`)).json();
    const unknownEmployer = unknownState.records.find(r => r.name === 'Synthetic QA Lead with unknown employer');
    check(`${name} save job: title and link save to shortlist without inventing an employer`,
      unknownEmployer?.status === 'pursue' && !unknownEmployer.organisation &&
      !unknownState.links.some(l => l.from_id === unknownEmployer.id || l.to_id === unknownEmployer.id) &&
      (await page.locator('#detail .job-company').textContent()) === 'Company not stated');
    const researchState = await (await fetch(`${base}/api/state`)).json();
    const researched = researchState.records.find(r => r.research?.some(e => e.facts?.length && e.assumptions?.length));
    check(`${name} research: fixture has facts and assumptions`, !!researched);
    if (researched) {
      await page.goto(`${base}/#work/${encodeURIComponent(researched.id)}`);
      await reveal(page.locator('#detail .research-note').first());
      const evidenceText = await page.locator('#detail .work-assessment').allTextContents();
      check(`${name} research: facts, assumptions and suggested work remain distinct`,
        evidenceText.some(t => t.includes('The source describes an existing software service.') &&
          t.includes('Assumptions: A bounded engagement may be useful.') &&
          t.includes('What is still missing for a decision')));
      await page.locator('#close').click();
    }

    // One person can provide two company routes. Navigation and outcomes must retain the selected action.
    const multiCompanyState = await (await fetch(`${base}/api/state`)).json();
    const perPerson = new Map();
    for (const a of multiCompanyState.intelligence.actions.filter(a => a.person_id && a.company_id && a.stage === 'ready')) {
      if (!perPerson.has(a.record_id)) perPerson.set(a.record_id, []);
      perPerson.get(a.record_id).push(a);
    }
    const companyPair = [...perPerson.values()].find(as => new Set(as.map(a => a.company_id)).size > 1);
    check(`${name} outreach: fixture has distinct company problems for one person`, !!companyPair &&
      new Set(companyPair.map(a => a.brief.hypothesis)).size > 1);
    if (companyPair) {
      const first = companyPair[0];
      const second = companyPair.find(a => a.company_id !== first.company_id);
      // One card per person: the second company's approach is reached from the person's sheet.
      await page.goto(`${base}/#people/${encodeURIComponent(first.record_id)}/action/${encodeURIComponent(first.id)}`);
      await page.waitForSelector('#detail[open] .approach-block');
      if (await page.locator(`#detail [data-other-approach="${second.id}"]`).count()) {
        await reveal(page.locator(`#detail [data-other-approach="${second.id}"]`));
        await page.locator(`#detail [data-other-approach="${second.id}"]`).click();
      }
      check(`${name} people: second company selects its own action`,await page.locator('#detail .approach-block').getAttribute('data-action-id')===second.id);
      await page.reload();await settle(page);
      check(`${name} people: selected action survives reload`,await page.locator('#detail .approach-block').getAttribute('data-action-id')===second.id);
      await page.locator('#detail .approach-block .approach-more > summary').click();
      await page.locator('#detail .approach-block textarea[aria-label="Note for this step"]').fill('Synthetic result: only this company was contacted.');
      await page.locator('#detail .approach-block button.primary').click();
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Saved:'));
      await settle(page);
      const scopedState = await (await fetch(`${base}/api/state`)).json();
      const scopedOutcome = scopedState.outcomes.find(o => o.note === 'Synthetic result: only this company was contacted.');
      check(`${name} outreach: result retains the chosen company and hypothesis`,
        scopedOutcome?.snapshot?.action_id === second.id && scopedOutcome?.snapshot?.company_id === second.company_id);
      check(`${name} outreach: recording one company's approach leaves the person's other approach as it was`,
        scopedState.intelligence.actions.find(a => a.id === second.id)?.stage === 'sent' &&
        scopedState.intelligence.actions.find(a => a.id === first.id)?.stage === first.stage);
    }

    // Multiple decisions on one subject must open the chosen decision, not the first matching record.
    const forDecisions = await (await fetch(`${base}/api/state`)).json();
    const grouped = new Map();
    for (const d of forDecisions.decisions.filter(d => d.record_id && d.status === 'open').sort((a,b)=>Number(b.kind==='draft')-Number(a.kind==='draft'))) {
      if (!grouped.has(d.record_id)) grouped.set(d.record_id, []);
      grouped.get(d.record_id).push(d);
    }
    const multiple = [...grouped.values()].find(ds => ds.length > 1);
    check(`${name} decision: fixture has two live decisions on one record`, !!multiple);
    if (multiple) {
      await page.goto(`${base}/#outreach/decision/${multiple[0].id}`);
      await page.waitForSelector('#detail[open] .decision-panel');
      check(`${name} decision: direct link selects the requested question`,
        await page.locator('#detail .decision-panel .decision-question').textContent() === multiple[0].question);
      await page.locator('#detail .other-decisions summary').click();
      await page.locator('#detail .other-decisions .decision-card').filter({ hasText: multiple[1].question }).click();
      check(`${name} decision: switching selects the other question and keeps its identity in the URL`,
        await page.locator('#detail .decision-panel .decision-question').textContent() === multiple[1].question &&
        (await page.evaluate(() => location.hash)).endsWith(`/decision/${multiple[1].id}`));
      await page.waitForSelector('#detail .decision-history', { state: 'attached' });
      await page.locator('#detail .decision-history > summary').click();
      const history = await page.locator('#detail .decision-history > details > summary').allTextContents();
      check(`${name} decision: full history retains completed and active decisions`,
        history.length >= 3 && history.some(t => /Done/.test(t)) && history.some(t => t.includes(multiple[0].question)) &&
        history.some(t => t.includes(multiple[1].question)), history.join(' | '));
      const selectedDecisionHash = await page.evaluate(() => location.hash);
      await page.locator('#detail .decision-note textarea').fill('Unsaved decision context must survive Back cancellation.');
      const cancelBack = page.waitForEvent('dialog').then(dialog => dialog.dismiss());
      await Promise.all([page.goBack(), cancelBack]);
      await page.waitForFunction(hash => location.hash === hash, selectedDecisionHash);
      check(`${name} decision: cancelling Back retains the selected decision URL and unsaved note`,
        await page.locator('#detail .decision-panel .decision-question').textContent() === multiple[1].question &&
        await page.locator('#detail .decision-note textarea').inputValue() === 'Unsaved decision context must survive Back cancellation.');
      page.once('dialog', dialog => dialog.accept());
      await page.locator('#close').click();
      const repeatedPrompts = [];
      const rejectRepeatedPrompt = dialog => { repeatedPrompts.push(dialog.message()); return dialog.dismiss(); };
      page.on('dialog', rejectRepeatedPrompt);
      await page.goto(`${base}${selectedDecisionHash}`);
      await page.waitForSelector('#detail[open] .decision-panel');
      check(`${name} decision: confirmed discard clears dirty state before reopening`, repeatedPrompts.length === 0);
      page.off('dialog', rejectRepeatedPrompt);
      await page.locator('#close').click();
    }

    // The owner's report: the top card accepted, then later accepts were refused.
    // Import review lives on People while candidates wait.
    await page.goto(`${base}/#people`);
    await settle(page);
    const available = await page.locator('.review-card').count();
    let taken = 0, stalled = '';
    for (; taken < 3; taken++) {
      const cards = await page.locator('.review-card').count();
      const primary = page.locator('.review-card .review-actions button.primary').first();
      if (!cards || !(await primary.count())) break;
      // Wait on the answer to this card's POST, then on the reload it triggers; a timeout says which
      // step stalled and what the page showed, instead of ending the whole run.
      const posted = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.startsWith('/api/review/'));
      await primary.click();
      try {
        await posted;
        await page.waitForFunction(n => document.querySelectorAll('.review-card').length < n || !document.querySelector('#alert').hidden, cards);
      } catch (e) {
        const seen = await page.evaluate(() => ({ cards: document.querySelectorAll('.review-card').length,
          status: document.querySelector('#connection-status')?.textContent, modal: !!document.querySelector('dialog[open]') }));
        stalled = `review action ${taken + 1} stalled (${e.message.split('\n')[0]}): ${seen.cards} of ${cards} cards, status "${seen.status}", dialog open ${seen.modal}`;
        break;
      }
      if (!(await page.locator('#alert').isHidden())) break;
    }
    const alert = (await page.locator('#alert').isHidden()) ? '' : await page.locator('#alert').textContent();
    check(`${name} import review: three consecutive review actions`,
      available >= 3 && taken === 3 && !alert && !stalled && !refused.length,
      alert || stalled || refused.join(', ') || `${available} initial review cards; ${taken} actions completed`);

    // A decision: open the first one waiting on the owner, pick the first answer with a note, see it go to Claude.
    await page.goto(`${base}/#week`);
    await settle(page);
    const pendingDecisions = await page.locator('.record-card.decision-card').count();
    check(`${name} decision: fixture has a question waiting on you`, pendingDecisions > 0);
    if (pendingDecisions) {
      await page.locator('.record-card.decision-card').first().click();
      await page.waitForSelector('#detail[open] .decision-panel');
      const chosenKind = await page.locator('#detail .decision-panel h3').textContent();
      await page.locator('#detail .decision-choice').first().click();
      await page.locator('#detail .decision-note textarea').fill('Smoke check');
      await page.locator('#detail .decision-form [type=submit]').click();
      // 27 Sep: sending an answer closes the dialog; a short notice confirms it went to Claude.
      await page.waitForFunction(() => !document.querySelector('#detail').open
        || [...document.querySelectorAll('#detail [role=status]')].some(x => x.textContent && x.textContent !== 'Sending…'));
      const closed = !(await page.locator('#detail').evaluate(d => d.open));
      const notice = (await page.locator('#notice').isVisible()) ? await page.locator('#notice').textContent() : '';
      const answered = (await (await fetch(`${base}/api/state`)).json()).decisions.some(d => d.status === 'answered' && d.note === 'Smoke check');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      const answerNotice = chosenKind === 'Approve a draft'
        ? notice === 'Approved. Your exact note is ready to copy from its approach card.' : /^Answer saved:/.test(notice);
      check(`${name} decision: sending an answer closes the dialog and confirms it`, closed && answerNotice && answered && !refused.length && overflow <= 1,
            refused.join(', ') || (closed ? `notice "${notice}", answered ${answered}` : await page.locator('#detail [role=status]').last().textContent()) || `${overflow}px too wide`);
      if (!closed) await page.locator('#close').click();
    }

    // Browser Back (an iPhone back swipe) from an open item returns to the tab with the item closed.
    await page.goto(`${base}/#work`);
    await settle(page);
    await page.locator('[data-work-filter=discovery]').click();
    await page.locator('.table-row').first().click();
    await page.waitForSelector('#detail[open]');
    await page.goBack();
    await page.waitForFunction(() => !document.querySelector('#detail').open, null, { timeout: 5000 }).catch(() => {});
    check(`${name} back from an item closes it`, !(await page.locator('#detail').evaluate(d => d.open)) && (await page.evaluate(() => location.hash)) === '#work');

    // A record with a state move: save it unchanged, then take the first move offered.
    const state = await (await fetch(`${base}/api/state`)).json();
    const target = state.records.find(r => ['job', 'opportunity'].includes(r.type) && Object.keys(r.transitions || {}).length)
      || state.records.find(r => Object.keys(r.transitions || {}).length);
    if (target) {
      let releaseTimeline;
      const timelineGate = new Promise(resolve => { releaseTimeline = resolve; });
      const delayTimeline = route => timelineGate.then(() => route.continue());
      await page.route('**/api/records/*/timeline', delayTimeline);
      await page.goto(`${base}/#work/${encodeURIComponent(target.id)}`);
      await page.waitForSelector('#detail[open]');
      await reveal(page.locator('#detail .detail-actions'));
      await page.locator('#detail .detail-actions button', { hasText: 'Edit record' }).click();
      const notes = page.locator('#modal textarea[name=notes]');
      // History arriving late must never re-render a form or discard an unsaved edit.
      await page.locator('#modal .more-fields summary').click();
      await notes.fill('Unsaved timeline check');
      releaseTimeline();
      await page.waitForSelector('#detail .timeline-item', { state: 'attached' });
      check(`${name} record detail: delayed history preserves unsaved form input`,
        await notes.inputValue() === 'Unsaved timeline check' && await page.locator('#modal').evaluate(d => d.open));
      await page.unroute('**/api/records/*/timeline', delayTimeline);
      page.once('dialog', d => d.accept());
      await page.locator('#modal-cancel').click();
      const bad = await page.evaluate(unlinked);
      check(`${name} record detail: URLs are links that open a new tab`, !bad.length, bad.slice(0, 3).join(' | '));
      for (const [button, what] of [['Edit record', 'unchanged save'], ['Change record state', 'first offered state change'],
                                    ['Update work state', 'work state update'], ['Log interaction', 'logged interaction']]) {
        // A record with a live decision keeps its record tools under "Edit details".
        await reveal(page.locator('#detail .detail-actions'));
        await page.locator('#detail .detail-actions button', { hasText: button }).click();
        await page.waitForSelector('#modal[open]');
        for (const field of ['action', 'summary']) {
          const box = page.locator(`#modal-form textarea[name=${field}]`);
          if (await box.count() && !(await box.inputValue())) await box.fill('Smoke check');
        }
        await page.locator('#modal-form [type=submit]').click();
        await page.waitForFunction(() => !document.querySelector('#modal').open || document.querySelector('#modal-status').textContent);
        const status = await page.locator('#modal-status').textContent();
        check(`${name} record detail: ${what}`, !status && !refused.length, status || refused.join(', '));
        if (status) await page.locator('#modal-cancel').click();
        await settle(page);
      }
      await reveal(page.locator('#detail .timeline-item').first());
      await page.waitForSelector('#detail .timeline-item');
      const timeline = await page.locator('#detail .timeline-item').allTextContents();
      check(`${name} record detail: history loads after saving an interaction`,
        timeline.some(t => t.includes('Smoke check')), timeline.slice(0, 2).join(' | '));
    }
    await page.goto(`${base}/#work`);
    await settle(page);
    await page.locator('[data-work-filter=discovery]').click();
    const routeState = await (await fetch(`${base}/api/state`)).json();
    for (const [key, expected] of [['Director', 'Director CV'], ['Manager', 'Manager CV · via agency']]) {
      const example = routeState.records.find(r => r.type === 'job' && !r.exclusion && r.relevance?.route?.cv === key &&
        (key !== 'Manager' || r.relevance.route.via_agency));
      check(`${name} work: a ${key} CV route is available`, !!example);
      if (!example) continue;
      await page.goto(`${base}/#work/${encodeURIComponent(example.id)}`); await settle(page);
      // Hash navigation can return before hashchange renders the selected record. The
      // connection label can still say connected while the previous sheet is present.
      await page.waitForFunction(name => document.querySelector('#detail[open] #detail-title')?.textContent === name, example.name);
      const shownRoute = await page.locator('#detail .detail-title .chip.route').textContent();
      const shownCV = await page.getByRole('link', { name: 'Open CV' }).getAttribute('href');
      check(`${name} job brief: ${expected} remains clear and linked`,
        shownRoute === expected && shownCV === example.relevance.route.url,
        JSON.stringify({shownRoute, shownCV, expected, expectedCV: example.relevance.route.url}));
    }
    await page.goto(`${base}/#week`);await settle(page);
    const remaining=await (await fetch(`${base}/api/state`)).json();
    const operations=remaining.decisions.filter(d=>d.status==='open'&&d.area==='operations');
    const weekQuestions=await page.locator('#content .decision-card strong').allTextContents();
    await page.goto(`${base}/#dashboard`);await settle(page);await page.waitForSelector('.dashboard-attention');
    const maintenanceQuestions=await page.locator('.dashboard-attention .decision-card strong').allTextContents();
    check(`${name} dashboard: operational questions are listed under Needs attention, not on This week`,
      operations.length>0&&operations.every(d=>maintenanceQuestions.includes(d.question)&&!weekQuestions.includes(d.question)));
    const dashWidth=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
    check(`${name} dashboard: every section renders without horizontal scrolling`,
      await page.locator('.dashboard-section').count()===7&&dashWidth<=1,`overflow ${dashWidth}px`);

    // The record linked most often to the same partners: one row per linked record, readable labels.
    const fresh = await (await fetch(`${base}/api/state`)).json();
    const partners = new Map();
    const coded = new Set();
    for (const l of fresh.links || []) for (const [a, b] of [[l.from_id, l.to_id], [l.to_id, l.from_id]]) {
      if (!partners.has(a)) partners.set(a, []);
      partners.get(a).push(b);
      if (l.relation?.includes('_')) coded.add(a);
    }
    const busiest = [...partners].map(([id, xs]) => [id, xs.length - new Set(xs).size])
      .sort((a, b) => coded.has(b[0]) - coded.has(a[0]) || b[1] - a[1])[0];
    if (busiest?.[1]) {
      await page.goto(`${base}/#network/${encodeURIComponent(busiest[0])}`);
      await page.waitForSelector('#detail[open]');
      await reveal(page.locator('#detail .link-item').first());
      const text = await page.evaluate(() => {
        const rows = [...document.querySelectorAll('#detail .link-item, #detail .timeline-item')];
        const run = rows.filter(x => { const s = x.querySelector('strong')?.textContent; return s && x.innerText.includes(s) && /[A-Za-z0-9]/.test(x.innerText.split(s)[1]?.[0] || ''); });
        const names = [...document.querySelectorAll('#detail .link-item')].map(x => x.dataset.id);
        const raw = [...document.querySelectorAll('#detail .link-item small, #detail .timeline-item, #detail .facts dt')]
          .map(x => x.innerText).filter(t => /\b[a-z]+_[a-z_]+\b|\bIr35\b|\bUrls\b/.test(t));
        return { run: run.map(x => x.innerText.slice(0, 60)), repeated: names.length - new Set(names).size, raw: raw.map(t => t.slice(0, 60)) };
      });
      check(`${name} record detail: a name never runs into its label`, !text.run.length, text.run.slice(0, 3).join(' | '));
      check(`${name} record detail: each linked record listed once`, text.repeated === 0, `${text.repeated} repeated`);
      check(`${name} record detail: no raw field names or codes`, !text.raw.length, text.raw.slice(0, 3).join(' | '));
    }

    // The record dialogs the steps above do not reach: link a record, a new record and a LinkedIn import.
    // They run last because each adds data that earlier checks count.
    const savedDialog = async () => {
      await page.waitForFunction(() => !document.querySelector('#modal').open || document.querySelector('#modal-status').textContent);
      const status = await page.locator('#modal-status').textContent();
      if (status) {
        // A failed save leaves the form unsaved; discard it so the remaining checks still report.
        page.once('dialog', d => d.accept());
        await page.locator('#modal-cancel').click();
      }
      return status;
    };
    if (target) {
      await page.goto(`${base}/#work/${encodeURIComponent(target.id)}`);
      await page.waitForSelector('#detail[open]');
      await reveal(page.locator('#detail .detail-actions'));
      await page.locator('#detail .detail-actions button', { hasText: 'Link record' }).click();
      await page.waitForSelector('#modal[open]');
      const linkTo = await page.locator('#modal select[name=to_id]').inputValue();
      await page.locator('#modal-form [type=submit]').click();
      const linkStatus = await savedDialog();
      const linked = await (await fetch(`${base}/api/state`)).json();
      check(`${name} record detail: link record`, !linkStatus && !refused.length &&
        linked.links.some(l => l.from_id === target.id && l.to_id === linkTo && l.relation === 'related'), linkStatus || refused.join(', '));
    }
    if (await page.locator('#detail').evaluate(d => d.open)) await page.locator('#close').click();
    await page.locator('#new-record').click();
    await page.waitForSelector('#modal[open]');
    await page.locator('#modal select[name=type]').selectOption('company');
    await page.locator('#modal input[name=name]').fill('Smoke Created Ltd');
    await page.locator('#modal-form [type=submit]').click();
    const createStatus = await savedDialog();
    const created = await (await fetch(`${base}/api/state`)).json();
    check(`${name} new record: created from the top bar`, !createStatus && !refused.length &&
      created.records.some(r => r.type === 'company' && r.name === 'Smoke Created Ltd'), createStatus || refused.join(', '));
    await page.goto(`${base}/#people`); await settle(page);
    await page.locator('#content .toolbar button', { hasText: 'Import LinkedIn CSV' }).click();
    await page.waitForSelector('#modal[open]');
    await page.locator('#modal input[type=file]').setInputFiles({ name: 'Connections.csv', mimeType: 'text/csv',
      buffer: Buffer.from('First Name,Last Name,URL,Email Address,Company,Position,Connected On\n' +
        'Smoke,Importee,https://www.linkedin.com/in/smoke-importee/,,Invented Co,Engineer,01 Jan 2024\n') });
    await page.locator('#modal-form [type=submit]').click();
    const importStatus = await savedDialog();
    const importAlert = await page.locator('#alert').textContent();
    check(`${name} people: a LinkedIn CSV is queued for review, not added`, !importStatus && !refused.length &&
      importAlert === '1 queued, 0 already present, 0 invalid.', importStatus || importAlert);
    check(`${name}: no page errors`, !errors.length, errors.slice(0, 3).join(' | '));
    if (process.env.WO_MAP_SCREENSHOTS) {
      const beforeCapture = errors.length;
      await captureSystemMap(page, base, name);
      const captureErrors = errors.slice(beforeCapture);
      const unexpected = captureErrors.filter(e => !device || !e.startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"));
      check(`${name} system map screenshots: no unrelated capture errors`, !unexpected.length, unexpected.join(' | '));
      if (captureErrors.length && !unexpected.length)
        console.log(`note  ${name}: ${captureErrors.length} capture-only WebKit stylesheet CSP warnings; app-flow console checked before capture`);
    }
    // Screenshot capture itself injects a stylesheet in WebKit, which a strict CSP rejects.
    // Check the actual app flow before that instrumentation, then reject any unrelated capture errors.
    if (process.env.WO_UI_SCREENSHOTS) {
      const beforeCapture = errors.length;
      mkdirSync(process.env.WO_UI_SCREENSHOTS, { recursive: true });
      await page.goto(`${base}/#week`); await page.reload(); await settle(page);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: join(process.env.WO_UI_SCREENSHOTS, `${name.replaceAll(' ', '-')}-home.png`), caret: 'initial' });
      for (const tab of ['people', 'pipeline']) {
        await page.goto(`${base}/#${tab}`); await settle(page);
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({ path: join(process.env.WO_UI_SCREENSHOTS, `${name.replaceAll(' ', '-')}-${tab}.png`), caret: 'initial' });
        if (tab === 'people') {
          await page.getByRole('button', {name:'Recruiters & agencies',exact:true}).click();
          await page.locator('.people-agencies').scrollIntoViewIfNeeded();
          await page.screenshot({ path: join(process.env.WO_UI_SCREENSHOTS, `${name.replaceAll(' ', '-')}-agencies.png`), caret: 'initial' });
          await page.getByRole('button', {name:'Everyone',exact:true}).click();
        }
      }
      await page.goto(`${base}/#work`); await settle(page);
      await page.locator('[data-work-filter=active]').click();
      await page.screenshot({ path: join(process.env.WO_UI_SCREENSHOTS, `${name.replaceAll(' ', '-')}-work.png`), caret: 'initial' });
      await page.goto(`${base}/#work/${encodeURIComponent(job.id)}`); await settle(page);
      await page.waitForSelector('#detail[open] .job-controls');
      await page.locator('#detail-content').evaluate(el => { el.scrollTop = 0; });
      await page.screenshot({ path: join(process.env.WO_UI_SCREENSHOTS, `${name.replaceAll(' ', '-')}-job.png`), caret: 'initial' });
      const captureErrors = errors.slice(beforeCapture);
      const unexpected = captureErrors.filter(e => !device || !e.startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"));
      check(`${name} screenshots: no unrelated capture errors`, !unexpected.length, unexpected.join(' | '));
      if (captureErrors.length && !unexpected.length)
        console.log(`note  ${name}: WebKit screenshot instrumentation triggered ${captureErrors.length} stylesheet CSP warnings; app-flow console was checked before capture`);
    }
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    await evidence.finish({ failed: failures.length > failuresBefore, error: failure });
    await browser.close();
    proc.kill();
    rmSync(dir, { recursive: true, force: true });
  }
}

try {
  if (wanted.includes('chromium')) await run(chromium, undefined, chromiumLaunch);
  if (wanted.includes('webkit')) await run(webkit, 'iPhone 13');
} catch (error) {
  check('smoke run', false, error.stack || error.message);
}
process.exit(failures.length ? 1 : 0);
