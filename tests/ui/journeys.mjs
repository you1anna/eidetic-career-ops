// Complete outreach journeys (archive/plans/2026-10-14-outreach-focus.md, requirement 7), run by ops/check.sh on
// synthetic records: a warm introduction, a cold approach, an agency conversation and an application,
// each followed through reply, follow-up, parking and handover. Every step checks that the same status
// shows wherever the approach appears, that context is kept when moving between records, and that a
// failed save keeps what was typed. Time passing is simulated by moving recorded dates back in the
// test's own copy of the database; nothing live is touched.
import { chromium, webkit, devices } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = join(root, '.venv/bin/python');
const source = process.env.WO_UI_DB;
const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
const chromiumLaunch = process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {};
const failures = [];
if (!source) { console.log('FAIL  journeys need WO_UI_DB (a synthetic fixture from tools/make_fixture.py)'); process.exit(1); }

function check(name, ok, detail = '') {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${name}${ok || !detail ? '' : ` — ${detail}`}`);
  if (!ok) failures.push(name);
}
const freePort = () => new Promise(resolve => {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
});

async function startServer(dir) {
  const db = join(dir, 'journeys.db');
  execFileSync(python, [join(root, 'tools/wo.py'), '--db', source, 'backup', db], { stdio: 'ignore' });
  const port = await freePort();
  const proc = spawn(python, [join(root, 'workspace/server.py'), '--db', db, '--port', String(port)], { stdio: 'ignore' });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/api/health`)).ok) return { proc, base, db }; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  proc.kill();
  throw new Error('workspace server did not start');
}

// Move a recorded date back, as if days had passed (the test's copy only).
function backdate(db, sql, ...args) {
  execFileSync(python, ['-c', 'import sqlite3,sys\nc=sqlite3.connect(sys.argv[1]);c.execute(sys.argv[2],sys.argv[3:]);c.commit()', db, sql, ...args]);
}
const daysAgo = n => new Date(Date.now() - n * 864e5).toISOString();

async function run(browserType, device, launch = {}) {
  const name = device || 'desktop';
  const dir = mkdtempSync(join(tmpdir(), 'wo-journeys-'));
  const { proc, base, db } = await startServer(dir);
  const browser = await launchBrowser(browserType, launch);
  try {
    const context = await browser.newContext(device ? devices[device] : { viewport: { width: 1300, height: 900 } });
    await context.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__copied = text; } } });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    let allowFailure = '';
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !(allowFailure && m.text().includes('Failed to load resource'))) errors.push(m.text()); });

    const settle = () => page.waitForFunction(() => document.querySelector('#connection-status')?.textContent !== 'Refreshing…');
    const state = async () => (await fetch(`${base}/api/state`)).json();
    // A change of the #hash alone does not reload the page, so load the address afresh to read data written
    // elsewhere. Changing the hash and then reloading made the old page start the route's timeline request
    // and the reload abort it, which WebKit logs as a page error ("due to access control checks").
    const fresh = async url => { await page.goto('about:blank'); await page.goto(url); };
    const go = async tab => { await fresh(`${base}/#${tab}`); await page.waitForSelector('#content > *'); await settle(); };
    const approachOf = (s, test) => s.intelligence.actions.find(test);
    const card = (scope, text) => page.locator(`${scope} .approach-card`).filter({ hasText: text }).first();
    // Press a step by its label: the primary and its companion are in view, the rest under More.
    async function reveal(target) {
      await target.waitFor({ state: 'attached' });
      while (await target.locator('xpath=ancestor::details[not(@open)]').count())
        await target.locator('xpath=ancestor::details[not(@open)]').first().locator(':scope > summary').click();
    }
    async function step(scope, label) {
      await page.evaluate(() => { document.querySelector('#notice').hidden = true; });
      const target = scope.locator('button', { hasText: label }).first();
      await reveal(target);
      await target.click();
      await settle();
      await page.waitForFunction(() => !document.querySelector('#notice').hidden || document.querySelector('#modal')?.open);
    }
    async function sheet(personOrRecord, actionId) {
      const s = await state();
      const a = approachOf(s, x => x.id === actionId);
      await fresh(`${base}/#outreach/${encodeURIComponent(a.record_id)}/action/${encodeURIComponent(actionId)}`);
      await reveal(page.locator('#detail .approach-block')); await settle();
      return page.locator('#detail .approach-block');
    }
    const blockStatus = async () => (await page.locator('#detail .approach-block .chip').first().textContent()).trim();
    // The same approach must show the same status in Pipeline and on the record sheet.
    async function sameStatus(label, actionId, expected) {
      await go('pipeline');
      const chip = page.locator(`.approach-card[data-action-id="${actionId}"] .approach-top .chip`).first();
      while (!(await chip.count()) && await page.locator('.show-more').count()) await page.locator('.show-more').first().click();
      const pipelineLabel = (await chip.textContent()).trim();
      await sheet(null, actionId);
      const sheetLabel = await blockStatus();
      const titleLabel = (await page.locator('#detail .detail-title .chip').last().textContent()).trim();
      const s = await state();
      const serverLabel = approachOf(s, a => a.id === actionId)?.progress.label;
      check(`${name} ${label}: one status everywhere (${expected})`,
        [pipelineLabel, sheetLabel, titleLabel, serverLabel].every(x => x === expected), [pipelineLabel, sheetLabel, titleLabel, serverLabel].join(' / '));
    }

    let s = await state();
    await go('week');
    await page.waitForFunction(() => document.querySelector('.week-list'));
    for (let i = 0; i < 50 && !(s = await state()).intelligence.week.stored; i++) await new Promise(r => setTimeout(r, 100));
    check(`${name} this week: the list is stored on first visit`, s.intelligence.week.stored === true);

    // Ask Claude…: the question waits on its card, an agent answers through wo, and the answer shows there.
    {
      const first = page.locator('.week-people .approach-card').first();
      const actionId = await first.getAttribute('data-action-id');
      const onCard = sel => page.locator(`.week-list .approach-card[data-action-id="${actionId}"] ${sel}`);
      const ask = onCard('.approach-ask-button');
      await reveal(ask);
      if (device) {
        const box = await ask.boundingBox();
        check(`${name} ask Claude: a full-size tap target`, !!box && box.height >= 38, `${box?.height}px`);
      }
      await ask.click();
      await page.locator('#modal textarea[name=question]').fill('Prepare me for a call with them');
      await page.locator('#modal-form [type=submit]').click();
      await page.waitForFunction(() => !document.querySelector('#notice').hidden);
      await settle();
      check(`${name} ask Claude: the question waits on its card and says when it is answered`,
        (await onCard('.approach-ask-wait').textContent() || '').includes('next session'));
      const rid = approachOf(await state(), a => a.id === actionId).record_id;
      const q = (await state()).questions.find(x => x.record_id === rid && !x.answered_at);
      execFileSync(python, [join(root, 'tools/wo.py'), '--db', db, '--actor', 'agent', 'question', 'answer', String(q.id), '--text-file', '-'],
        { input: 'Ask what they are shipping next quarter.', stdio: ['pipe', 'ignore', 'inherit'] });
      await go('week');
      check(`${name} ask Claude: the answer shows on the card at the next page load`,
        (await onCard('.approach-ask-a').textContent() || '').includes('next quarter'));
    }

    // 0. "Not this week" is never one-way: the notice's Undo, and later Put back, return the week as it was.
    {
      const before = s.intelligence.week.items.map(x => x.action_id);
      const entry = s.intelligence.week.items.find(x => x.slot === 'known' && !x.done && x.state === 'planned'
        && !['Alex Example', 'Riley Recruiter'].includes(approachOf(s, a => a.id === x.action_id)?.person));
      const who = entry && approachOf(s, a => a.id === entry.action_id)?.person;
      check(`${name} not this week: a planned person to set aside`, !!who);
      if (who) {
        const sameWeek = w => JSON.stringify(w.items.map(x => x.action_id)) === JSON.stringify(before) && !w.deferred.length;
        await go('week');
        await step(card('.week-list', who), 'Not this week');
        check(`${name} not this week: the notice offers Undo`,
          (await page.locator('#notice').textContent()).includes('Moved out of this week') && await page.locator('#notice .notice-action').count() === 1);
        s = await state();
        check(`${name} not this week: the person is set aside`, s.intelligence.week.deferred.some(d => d.action_id === entry.action_id));
        await page.locator('#notice .notice-action').click();
        await settle();
        await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes("Back on this week's list"));
        s = await state();
        check(`${name} not this week: Undo puts them back and takes the replacement out`, sameWeek(s.intelligence.week),
          s.intelligence.week.items.map(x => x.action_id).join(','));
        await go('week');
        await step(card('.week-list', who), 'Not this week');
        await go('week');
        await page.locator('.week-deferred > summary').click();
        const back = page.locator('.week-deferred-item').filter({ hasText: who }).locator('button', { hasText: 'Put back' });
        check(`${name} not this week: the set-aside list offers Put back`, await back.count() === 1);
        const box = await back.boundingBox();
        check(`${name} not this week: Put back is a full-size tap target`, !!box && box.height >= 38, `${box?.height}px`);
        await back.click();
        await settle();
        await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes("Back on this week's list"));
        s = await state();
        check(`${name} not this week: Put back later the same week returns the week as it was`, sameWeek(s.intelligence.week));
        check(`${name} not this week: their card is back on the list`, await card('.week-list', who).count() === 1);
      }
    }

    // 1. Warm introduction: someone you know, through reply and introduction to a handover into Work.
    const warm = approachOf(s, a => ['ask', 'catch_up'].includes(a.kind) && a.progress.status === 'ready' && a.person === 'Alex Example');
    check(`${name} warm: someone you know leads the week`, !!warm && s.intelligence.week.items[0]?.action_id === warm.id);
    if (warm) {
      const touchBefore = s.records.find(r => r.id === warm.record_id)?.last_touch || '';
      await step(card('.week-list', 'Alex Example'), warm.progress.primary.label);
      check(`${name} warm: the send is confirmed with an undo`,
        (await page.locator('#notice').textContent()).includes('recorded') && await page.locator('#notice').getByRole('button', { name: 'Undo', exact: true }).count() === 1);
      check(`${name} warm: the week keeps the item in Waiting`,
        await card('.week-list', 'Alex Example').locator('.chip', { hasText: 'Sent · waiting for a reply' }).count() === 1);
      // A mistaken tap is put right on the same card, after the notice has gone: Undo brings everything back.
      await go('week');
      await card('.week-waiting', 'Alex Example').locator('.week-progress > summary').click();
      const undo = card('.week-list', 'Alex Example').locator('.approach-undo button', { hasText: 'Undo' });
      await undo.waitFor({state:'attached'});
      check(`${name} warm: the expanded waiting card keeps an Undo`, await undo.count() === 1);
      await reveal(undo);
      await undo.click();
      await settle();
      await page.waitForFunction(() => !document.querySelector('#notice').hidden);
      s = await state();
      const undone = approachOf(s, a => a.id === warm.id);
      check(`${name} warm: Undo puts the card back as it was`,
        undone?.progress.status === 'ready' && !s.outcomes.some(o => !o.corrected_at && o.snapshot?.action_id === warm.id)
        && (s.records.find(r => r.id === warm.record_id)?.last_touch || '') === touchBefore
        && await card('.week-list', 'Alex Example').locator('.chip', { hasText: 'Sent · waiting for a reply' }).count() === 0,
        `${undone?.progress.status} · ${s.records.find(r => r.id === warm.record_id)?.last_touch} vs ${touchBefore}`);
      await step(card('.week-list', 'Alex Example'), warm.progress.primary.label);
      await sameStatus('warm', warm.id, 'Waiting for a reply');
      let block = await sheet(null, warm.id);
      await step(block, 'They replied');
      block = page.locator('#detail .approach-block');
      await step(block, 'They offered an introduction');
      check(`${name} warm: an introduction keeps the approach in conversation`, await blockStatus() === 'Replied');
      await step(page.locator('#detail .approach-block'), 'We spoke');
      check(`${name} warm: we spoke moves it to in conversation`, await blockStatus() === 'In conversation');
      await page.locator('#detail .approach-block button', { hasText: 'Hand over to Work' }).click();
      await page.locator('#modal textarea[name=next_step]').fill('Send a scoped proposal for the introduction');
      await page.locator('#modal-form [type=submit]').click();
      await settle();
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Handed over'));
      s = await state();
      const opp = s.records.find(r => r.type === 'opportunity' && r.next_action === 'Send a scoped proposal for the introduction');
      check(`${name} warm: handover creates one opportunity in Work with the agreed next step`, !!opp && opp.stage === 'conversation');
      check(`${name} warm: the approach now says it continues in Work`, await blockStatus() === 'In Work');
      await page.locator('#detail .approach-block button', { hasText: 'Open in Work' }).click();
      await page.waitForSelector('#detail .back-link');
      check(`${name} warm: opening the opportunity keeps a way back`, (await page.locator('#detail .back-link').textContent()).includes('Alex Example'));
      await page.locator('#detail .back-link').click();
      await page.waitForSelector('#detail[open] .approach-block');
      check(`${name} warm: back returns to the same approach`, await blockStatus() === 'In Work' &&
        (await page.locator('#detail h2').first().textContent()).includes('Alex Example'));
      await page.locator('#close').click();
    }

    // 2. Cold approach: approve a note, copy and send it, one follow-up, parking, then a late reply.
    s = await state();
    const review = s.decisions.find(d => d.kind === 'draft' && d.status === 'open');
    check(`${name} cold: a note waits for approval`, !!review);
    const cold = approachOf(s, a => a.kind === 'note' && a.progress.status === 'approved');
    check(`${name} cold: an approved note offers Copy then I sent this`, !!cold && cold.progress.primary.method === 'copy');
    if (cold) {
      let block = await sheet(null, cold.id);
      await block.locator('button', { hasText: 'Copy the note' }).click();
      const draft = s.drafts.find(d => d.version_id === cold.draft_version_id);
      check(`${name} cold: Copy the note copies the approved text exactly`, await page.evaluate(() => window.__copied) === draft?.text);
      // A failed save keeps the note typed for this step, and the retry succeeds.
      await block.locator('.approach-more > summary').click();
      await block.locator('textarea[aria-label="Note for this step"]').fill('Sent by LinkedIn message');
      allowFailure = 'outcome';
      await page.route('**/outcome', r => r.abort());
      await block.locator('button', { hasText: 'I sent this' }).click();
      await page.waitForFunction(() => (document.querySelector('#detail .approach-block [role=status]')?.textContent || '').length > 0
        && !document.querySelector('#detail .approach-block [role=status]').textContent.includes('Saving'));
      check(`${name} cold: a failed save keeps the typed note and nothing is recorded`,
        await block.locator('textarea[aria-label="Note for this step"]').inputValue() === 'Sent by LinkedIn message' &&
        (await state()).outcomes.length === s.outcomes.length);
      await page.unroute('**/outcome');
      allowFailure = '';
      await block.locator('button', { hasText: 'I sent this' }).click();
      await settle();
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('recorded'));
      s = await state();
      check(`${name} cold: the retry saves the send once, with its note`,
        s.outcomes.filter(o => o.kind === 'sent' && o.snapshot?.action_id === cold.id).length === 1 &&
        s.outcomes.find(o => o.snapshot?.action_id === cold.id)?.note === 'Sent by LinkedIn message');
      // Eleven days pass with no reply: one follow-up is suggested on This week.
      backdate(db, "UPDATE outcomes SET at=? WHERE kind='sent' AND json_extract(snapshot_json,'$.action_id')=?", daysAgo(11), cold.id);
      await go('week');
      check(`${name} cold: after ten days a follow-up is suggested on This week`,
        await page.locator('.approach-card').filter({ hasText: 'Casey Placeholder' }).locator('.chip', { hasText: 'Follow-up suggested' }).count() > 0);
      await sameStatus('cold', cold.id, 'Follow-up suggested');
      await step(page.locator('#detail .approach-block'), 'I followed up');
      check(`${name} cold: after the follow-up it waits again`, await blockStatus() === 'Waiting for a reply');
      // Another eleven days: parked as no response, without a second reminder.
      backdate(db, "UPDATE outcomes SET at=? WHERE kind='followed_up' AND json_extract(snapshot_json,'$.action_id')=?", daysAgo(11), cold.id);
      await go('pipeline');
      await page.locator('.pipeline-closed > summary').click();
      check(`${name} cold: no reply after one follow-up parks the approach in Pipeline's closed list`,
        await page.locator('.pipeline-closed .approach-card').filter({ hasText: 'Casey Placeholder' }).locator('.chip', { hasText: 'No response' }).count() === 1);
      s = await state();
      check(`${name} cold: a parked approach is not suggested again`, !s.intelligence.followups.includes(cold.id));
      await step(page.locator('.pipeline-closed .approach-card').filter({ hasText: 'Casey Placeholder' }), 'They replied after all');
      s = await state();
      const late = approachOf(s, a => a.id === cold.id);
      check(`${name} cold: a late reply joins the same history`,
        late.progress.status === 'replied' && late.progress.history.map(h => h.kind).join() === 'sent,followed_up,replied');
    }

    // 3. Agency conversation: contact the desk, reply, CV submitted, a call, handover.
    s = await state();
    const desk = approachOf(s, a => a.kind === 'desk' && a.progress.status === 'ready');
    check(`${name} desk: an agency desk is ready`, !!desk);
    if (desk) {
      let block = await sheet(null, desk.id);
      for (const [label, expected] of [['I contacted the desk', 'Waiting for a reply'], ['They replied', 'Replied'],
        ['CV submitted', 'CV submitted'], ['Interview or call arranged', 'In conversation']]) {
        await step(page.locator('#detail .approach-block'), label);
        if (label === 'CV submitted') {
          // The company is required, and with no referral there, nothing is flagged.
          await page.locator('#modal-form [type=submit]').click();
          check(`${name} desk: the company is required before a submission is saved`,
            await page.locator('#modal').evaluate(d => d.open) && !(await state()).outcomes.some(o => o.kind === 'submitted'));
          await page.locator('#modal input[name=company]').fill('Unrelated Example Software');
          await page.locator('#modal-form [type=submit]').click();
          await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('CV submitted to Unrelated Example Software'));
          await settle();
          check(`${name} desk: no clash is flagged where no referral is under way`,
            (await page.locator('#detail .approach-block .approach-warning').count()) === 0);
        }
        check(`${name} desk: ${label} → ${expected}`, await blockStatus() === expected);
      }
      await page.locator('#detail .approach-block button', { hasText: 'Hand over to Work' }).click();
      await page.locator('#modal-form [type=submit]').click();
      await settle();
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Handed over'));
      check(`${name} desk: the desk conversation is handed over`, await blockStatus() === 'In Work');
      await page.locator('#close').click();
    }

    // 4. Application: apply, correct a mistake, apply, follow up, a response, interview, offer, close.
    s = await state();
    const app = approachOf(s, a => a.kind === 'application' && a.progress.status === 'ready' && a.company === 'Example Rail Ltd');
    check(`${name} application: a contract role is ready to apply for`, !!app);
    if (app) {
      let block = await sheet(null, app.id);
      await step(block, 'I applied');
      await sameStatus('application', app.id, 'Applied');
      await go('work');
      await page.locator('[data-work-filter=applications]').click();
      check(`${name} application: Work shows the same status`,
        (await page.locator('.table-row').filter({ hasText: 'Interim Engineering Manager' }).first().textContent()).includes('Applied'));
      block = await sheet(null, app.id);
      check(`${name} application: the job's own status agrees`, (await page.locator('#detail .application-status').textContent()).includes('Applied'));
      await page.once('dialog', d => d.accept());
      await step(page.locator('#detail .approach-block'), 'Correct the last status change');
      s = await state();
      check(`${name} application: a mistaken "I applied" can be corrected and stays in the history`,
        s.records.find(r => r.id === app.record_id).status === 'candidate' &&
        s.history.some(h => h.entity_id === app.record_id && (h.note || '').startsWith('Corrected')));
      await step(page.locator('#detail .approach-block'), 'I applied');
      backdate(db, "UPDATE state_events SET at=? WHERE entity_id=? AND field='status' AND to_value='applied'", daysAgo(11), app.record_id);
      await sheet(null, app.id);
      check(`${name} application: after ten days a follow-up is suggested`, await blockStatus() === 'Follow-up suggested');
      for (const [label, expected] of [['I followed up', 'Applied'], ['They responded', 'Responded'],
        ['Interview arranged', 'Interview'], ['Offer received', 'Offer'], ['Close the application', 'Closed']]) {
        await step(page.locator('#detail .approach-block'), label);
        check(`${name} application: ${label} → ${expected}`, await blockStatus() === expected);
      }
      await page.locator('#close').click();
    }

    // 5. A recruiter on the owner's list: talking points, then a contact recorded with how he got in touch.
    s = await state();
    const recruiter = approachOf(s, a => a.kind === 'recruiter' && a.person === 'Riley Recruiter');
    check(`${name} recruiter: a recruiter on your list is in this week's contract slots`,
      !!recruiter && s.intelligence.week.items.some(x => x.action_id === recruiter.id && x.slot === 'contract'));
    if (recruiter) {
      await go('week');
      const rc = card('.week-list', 'Riley Recruiter');
      check(`${name} recruiter: the card shows the channel and the availability ask`,
        (await rc.locator('.approach-how').textContent()).includes('Email') &&
        (await rc.locator('.approach-points').textContent()).includes('available'));
      check(`${name} recruiter: one tap emails them at the address on their record`,
        await rc.locator('a.approach-contact').getAttribute('href') === 'mailto:riley@example.com' &&
        (await rc.locator('a.approach-contact').textContent()) === 'Email Riley');
      await reveal(rc.locator('select[name=how]'));
      await rc.locator('select[name=how]').selectOption('linkedin');
      await rc.locator('button', { hasText: "I've been in touch" }).click();
      await settle();
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('recorded'));
      s = await state();
      const contact = s.outcomes.find(o => o.kind === 'sent' && o.snapshot?.action_id === recruiter.id);
      check(`${name} recruiter: the contact keeps the channel you chose`, contact?.snapshot?.channel === 'linkedin');
      await sameStatus('recruiter', recruiter.id, 'Waiting for a reply');
      for (const [label, expected] of [['They replied', 'Replied'], ['CV submitted', 'CV submitted'],
        ['Interview or call arranged', 'In conversation']]) {
        await step(page.locator('#detail .approach-block'), label);
        if (label === 'CV submitted') {
          // Which company the CV went to; Alex Example's referral at the same company is under way (journey 1).
          await page.locator('#modal input[name=company]').fill(warm.company);
          await page.locator('#modal-form [type=submit]').click();
          await page.waitForFunction(company => document.querySelector('#notice')?.textContent.includes(`CV submitted to ${company}`), warm.company);
          await settle();
          s = await state();
          check(`${name} recruiter: the submission records the company`,
            s.outcomes.some(o => o.kind === 'submitted' && o.snapshot?.target?.name === warm.company));
          const warning = page.locator('#detail .approach-block .approach-warning');
          check(`${name} recruiter: a referral under way at the same company is flagged`,
            (await warning.count()) > 0 && (await warning.first().textContent()).includes('Alex Example'));
        }
        check(`${name} recruiter: ${label} → ${expected}`, await blockStatus() === expected);
      }
      await page.locator('#close').click();
      await go('week');
      check(`${name} tally: this week's contacts and replies are counted`,
        /So far this week: [1-9]\d* contacts? · [1-9]\d* repl(y|ies)/.test(await page.locator('.week-tally').textContent()));
    }

    // A working message: edit it, copy it, record the send, and keep those words after the note changes.
    s = await state();
    const noted = approachOf(s, a => a.person === 'Taylor Fixture' && ['ask', 'catch_up', 'recruiter'].includes(a.kind)
      && a.progress.status === 'ready' && !a.progress.message);
    check(`${name} note: someone still to contact has no note yet`, !!noted);
    if (noted) {
      let block = await sheet(null, noted.id);
      check(`${name} note: without a note the talking points stay open and a template is offered`,
        (await block.locator('.approach-missing-note').textContent()).startsWith('No note yet.') &&
        await block.locator('.note-templates[open] .note-use').textContent() === 'Write from this template' &&
        await block.locator('.approach-points li').count() > 0 &&
        await block.locator('.approach-points > summary').count() === 0);
      const person = (await state()).records.find(r => r.id === noted.record_id);
      const drafted = await page.evaluate(async ({ id, revision }) => {
        const response = await fetch(`/api/records/${encodeURIComponent(id)}/draft`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision, text: 'Hello, a short note.', title: 'WhatsApp', purpose: 'message' }),
        });
        return response.json();
      }, { id: noted.record_id, revision: person.revision });
      check(`${name} note: saving a message does not ask for approval`, drafted.saved === true && drafted.version_id > 0);
      block = await sheet(null, noted.id);
      check(`${name} note: the card leads with the note and folds the talking points`,
        await block.locator('textarea[name=message]').inputValue() === 'Hello, a short note.' &&
        (await block.locator('.approach-points > summary').textContent()) === 'Why this note');
      await block.locator('textarea[name=message]').fill('Hello, the edited note.');
      await block.locator('.approach-message button', { hasText: 'Copy' }).click();
      check(`${name} note: Copy copies the edited note`, await page.evaluate(() => window.__copied) === 'Hello, the edited note.');
      // Copy also saves the edited note on the card, so it survives a reload before the send is recorded.
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Copied and saved'));
      await settle();
      const copiedDraft = (await state()).drafts.find(d => d.record_id === noted.record_id);
      check(`${name} note: Copy saves the edited note as yours`, copiedDraft?.text === 'Hello, the edited note.' && copiedDraft.actor === 'owner');
      const label = approachOf(await state(), a => a.id === noted.id).progress.primary.label;
      await step(block, label);
      s = await state();
      const sentNote = s.outcomes.find(o => o.kind === 'sent' && o.snapshot?.action_id === noted.id);
      check(`${name} note: I've reached out keeps the edited words`,
        sentNote?.snapshot?.message === 'Hello, the edited note.' && sentNote.draft_version_id !== drafted.version_id);
      const current = s.records.find(r => r.id === noted.record_id);
      await page.evaluate(async ({ id, revision }) => {
        await fetch(`/api/records/${encodeURIComponent(id)}/draft`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision, text: 'A later edit that was not sent.', title: 'WhatsApp', purpose: 'message' }),
        });
      }, { id: noted.record_id, revision: current.revision });
      block = await sheet(null, noted.id);
      check(`${name} note: a later edit does not rewrite what was sent`,
        (await block.locator('.approach-sent').textContent()).includes('Hello, the edited note.') &&
        await block.locator('textarea[name=message]').count() === 0);
      check(`${name} note: the cold approval is untouched`,
        (await state()).decisions.some(d => d.kind === 'draft' && d.status === 'open'));
    }

    // Four routes refine the same cards and lifecycle; source evidence never implies a mandate.
    for (const routeKind of ['interim_cover', 'new_leader', 'delivery_partner', 'warm_introduction']) {
      s = await state();
      const approach = approachOf(s, a => a.brief.approach_route?.kind === routeKind && a.progress.status === 'ready');
      check(`${name} ${routeKind}: a qualified recommendation exists`, !!approach);
      if (!approach) continue;
      // People you know are approached from People; Approaches holds desks and decision-makers.
      if (['catch_up', 'ask'].includes(approach.kind)) {
        await fresh(`${base}/#people/${encodeURIComponent(approach.record_id)}/action/${encodeURIComponent(approach.id)}`);
        await page.waitForSelector('#detail[open]'); await settle();
      } else {
        await go('outreach');
        let routeCard = page.locator(`.approach-card[data-action-id="${approach.id}"]`);
        while (!(await routeCard.count()) && await page.locator('.show-more').count()) await page.locator('.show-more').first().click();
        check(`${name} ${routeKind}: card shows route and tailored question`,
          (await routeCard.textContent()).includes(approach.brief.approach_route.label) &&
          (await routeCard.textContent()).includes(approach.brief.approach_route.question));
        await routeCard.locator('.approach-name').click();
      }
      await reveal(page.locator('#detail .problem-brief .approach-route'));
      check(`${name} ${routeKind}: brief retains the same route and primary action`,
        (await page.locator('#detail .problem-brief .approach-route').textContent()).includes(approach.brief.approach_route.label) &&
        (await page.locator('#detail .approach-block').textContent()).includes(approach.progress.primary.label));
      const routeBox = page.locator('#detail .problem-brief .approach-route');
      if (routeKind === 'warm_introduction') {
        await routeBox.locator('button', { hasText: 'Open Jamie Leader' }).click();
        await page.waitForSelector('#detail .back-link');
        await page.locator('#detail .back-link').click();
        check(`${name} warm route: target navigation returns to the introducer and same approach`,
          (await page.locator('#detail h2').first().textContent()).includes('Rory Introducer') &&
          page.url().includes(encodeURIComponent(approach.id)));
      } else {
        check(`${name} ${routeKind}: first-party evidence is linked`, await routeBox.locator('a[target="_blank"]').count() > 0);
      }
      check(`${name} ${routeKind}: brief fits the viewport`,
        await page.locator('#detail').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      await step(page.locator('#detail .approach-block'), approach.progress.primary.label);
      await sameStatus(routeKind, approach.id, 'Waiting for a reply');
      await step(page.locator('#detail .approach-block'), 'They replied');
      await step(page.locator('#detail .approach-block'), 'We spoke');
      await page.locator('#detail .approach-block button', { hasText: 'Hand over to Work' }).click();
      await page.locator('#modal textarea[name=next_step]').fill(`Agree the ${routeKind} scope and handover`);
      await page.locator('#modal-form [type=submit]').click();
      await settle();
      await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Handed over'));
      s = await state();
      check(`${name} ${routeKind}: handover retains route, history and agreed next step`,
        approachOf(s, a => a.id === approach.id)?.brief.approach_route?.kind === routeKind &&
        s.outcomes.some(o => o.kind === 'sent' && o.snapshot?.action_id === approach.id && o.snapshot?.brief?.approach_route?.kind === routeKind) &&
        s.records.some(r => r.type === 'opportunity' && r.next_action === `Agree the ${routeKind} scope and handover`));
      await page.locator('#close').click();
    }

    // Existing approvals still work: approve the waiting note from the decision panel.
    if (review) {
      await page.goto(`${base}/#week/decision/${review.id}`);
      await page.waitForSelector('#detail[open] .decision-panel');
      await page.locator('#detail .decision-choice', { hasText: 'Approve as is' }).click();
      // settle() alone can return before the answer's reload begins; read state only after the POST lands.
      const answered = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/decisions/${review.id}`);
      await page.locator('#detail .decision-form [type=submit]').click();
      await answered;
      await settle();
      s = await state();
      const approvedNow = approachOf(s, a => a.draft_version_id === review.draft_version_id);
      check(`${name} approvals: approving a note makes it ready to copy`, approvedNow?.progress.status === 'approved' &&
        approvedNow.progress.primary?.method === 'copy');
    }

    // Open the complete job brief before recording a status; a mistaken tap still has Undo.
    s = await state();
    const role = s.records.find(r => r.type === 'job' && r.status === 'candidate' && r.organisation === 'Acme Tech Recruitment'
      && (r.transitions?.status || []).includes('applied'));
    check(`${name} jobs: a discovered role to mark`, !!role);
    if (role) {
      await go('work');
      const row = page.locator('.table-row-wrap').filter({ hasText: role.name }).filter({ hasText: role.organisation }).first();
      check(`${name} jobs: the list offers one complete brief`, await row.getByRole('button').count() === 1);
      await row.locator('.job-brief-row').click();
      const quick = page.locator('#detail [data-job-status=applied]');
      check(`${name} jobs: the brief has one "I applied" action`, await quick.count() === 1);
      if (await quick.count()) {
        await page.evaluate(() => { document.querySelector('#notice').hidden = true; });
        await quick.click();
        await settle();
        await page.waitForFunction(() => !document.querySelector('#notice').hidden);
        check(`${name} jobs: the status notice offers Undo`, await page.locator('#notice .notice-action', { hasText: 'Undo' }).count() === 1);
        await page.locator('#notice .notice-action').click();
        await settle();
        await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Undone'));
        s = await state();
        check(`${name} jobs: Undo returns the role to Discovered and keeps the change in the history`,
          s.records.find(r => r.id === role.id).status === 'candidate'
          && s.history.some(h => h.entity_id === role.id && (h.note || '').startsWith('Corrected')));
      }
    }

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(`${name} journeys: no horizontal overflow`, overflow <= 1, `${overflow}px`);
    check(`${name} journeys: no page errors`, !errors.length, errors.slice(0, 3).join(' | '));
  } finally {
    await browser.close();
    proc.kill();
    rmSync(dir, { recursive: true, force: true });
  }
}

try {
  if (wanted.includes('chromium')) await run(chromium, undefined, chromiumLaunch);
  if (wanted.includes('webkit')) await run(webkit, 'iPhone 13');
} catch (error) {
  check('journeys run', false, error.stack || error.message);
}
process.exit(failures.length ? 1 : 0);
