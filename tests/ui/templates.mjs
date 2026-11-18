// The owner's templates where he writes (config/templates.yml, js/templates.js): a card with no note starts
// from the template that fits, the gaps are his to fill before anything is copied or recorded as sent,
// email keeps its subject apart, a due follow-up starts from the bump, a reply offers the thank-you,
// and Research > Templates holds the whole pack. Synthetic records only; nothing is sent.
import assert from 'node:assert/strict';
import { chromium, webkit, devices } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const root = join(dirname(fileURLToPath(import.meta.url)), '../..');
const python = join(root, '.venv/bin/python');
const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
const pack = JSON.parse(execFileSync(python, ['-c',
  'import json,sys; sys.path.insert(0, "workspace"); import search_config; print(json.dumps(search_config.templates()))'], { cwd: root }));
const freePort = () => new Promise(resolve => {
  const server = net.createServer();
  server.listen(0, '127.0.0.1', () => { const { port } = server.address(); server.close(() => resolve(port)); });
});
// Move a recorded date back, as if days had passed (this test's copy only).
const backdate = (db, sql, ...args) => execFileSync(python, ['-c',
  'import sqlite3,sys\nc=sqlite3.connect(sys.argv[1]);c.execute(sys.argv[2],sys.argv[3:]);c.commit()', db, sql, ...args]);

async function run(type, mobile = false) {
  const label = mobile ? 'iPhone WebKit' : 'desktop Chromium';
  const dir = mkdtempSync(join(tmpdir(), 'wo-templates-'));
  const database = join(dir, 'templates.db');
  execFileSync(python, [join(root, 'tools/make_fixture.py'), database], { stdio: 'ignore' });
  const port = await freePort(), base = `http://127.0.0.1:${port}`;
  const server = spawn(python, [join(root, 'workspace/server.py'), '--db', database, '--port', String(port)], { stdio: 'ignore' });
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100 && !ready; i++) {
      try { ready = (await fetch(`${base}/api/health`)).ok; } catch {}
      if (!ready) await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, 'synthetic server starts');
    browser = await launchBrowser(type, !mobile && process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {});
    const context = await browser.newContext(mobile ? devices['iPhone 13'] : { viewport: { width: 1300, height: 950 } });
    await context.addInitScript(() => Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: async text => { window.__copied = text; } } }));
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    const report = message => console.log(`ok    templates (${label}): ${message}`);
    const state = async () => (await fetch(`${base}/api/state`)).json();
    const settle = () => page.waitForFunction(() => document.querySelector('#connection-status')?.textContent !== 'Refreshing…');
    const notice = async text => {
      await page.waitForFunction(t => !document.querySelector('#notice').hidden && document.querySelector('#notice').textContent.includes(t), text);
      await page.evaluate(() => { document.querySelector('#notice').hidden = true; });
    };
    const copied = () => page.evaluate(() => window.__copied);
    const forget = () => page.evaluate(() => { window.__copied = undefined; });
    const selected = () => page.evaluate(() => { const t = document.activeElement; return t?.value?.slice(t.selectionStart, t.selectionEnd); });
    const open = async hash => { await page.goto('about:blank'); await page.goto(`${base}/#${hash}`); await page.waitForSelector('#content > *'); await settle(); };
    const sheet = async actionId => {
      const a = (await state()).intelligence.actions.find(x => x.id === actionId);
      await open(`outreach/${encodeURIComponent(a.record_id)}/action/${encodeURIComponent(actionId)}`);
      await page.waitForSelector('#detail[open] .approach-block');
      return page.locator('#detail .approach-block');
    };
    const s0 = await state();
    const actionOf = (s, name, kind) => s.intelligence.actions.find(a => a.person === name && (!kind || a.kind === kind));
    const alex = s0.intelligence.week.items.map(i => s0.intelligence.actions.find(a => a.id === i.action_id)).find(a => a?.person === 'Alex Example');
    assert.ok(alex, 'the fixture puts Alex Example on This week');

    // 1. No note yet: the template that fits is offered, and the note appears with it, gaps selected.
    await open('week');
    const card = page.locator(`.week-list .approach-card[data-action-id="${alex.id}"]`);
    await card.locator('.week-preparation > summary').click();
    const note = card.locator('textarea[name=message]');
    assert.match(await card.locator('.approach-missing-note').innerText(), /^No note yet\./);
    assert.equal(await card.locator('select[name=template]').inputValue(), 'former_colleague', 'an introduction ask to a former colleague');
    assert.match(await card.locator('select[name=template] option:checked').innerText(), /Former colleague \(suggested\)/);
    assert.equal(await note.isVisible(), false);
    assert.equal(await card.locator('select[name=lead]').isVisible(), false, 'finer choices wait until a note is started');
    await card.getByRole('button', { name: 'Write from this template', exact: true }).click();
    assert.ok((await note.inputValue()).startsWith('Hi Alex,\n\n{{personal_line}}'));
    assert.match(await note.inputValue(), /Northwind Fintech came to mind because \{\{reason\}\}/, 'the recorded employer is filled in');
    assert.match(await note.inputValue(), /contract first:\n1\. leading an engineering team\n2\. building AI products hands-on\n3\. coming into/,
      'the prepared Director CV leads with leading a team');
    assert.notEqual(await card.locator('.note-preview').getAttribute('open'), null, 'a structured note shows how it reads');
    assert.deepEqual(await card.locator('.note-preview-text ol li').allInnerTexts(), [
      'leading an engineering team', 'building AI products hands-on',
      "coming into a system that's struggling and fixing how it ships"]);
    assert.equal(await card.locator('.note-preview-text strong').first().innerText(),
      "Is there anything there you'd be happy to refer me for?");
    assert.equal(await selected(), '{{personal_line}}', 'typing replaces the first gap');
    assert.deepEqual(await card.locator('.note-gap').allInnerTexts(), ['Personal line', 'Why their company']);
    assert.match(await card.locator('.note-gap-help').innerText(), /Last talked/i);
    assert.equal(await card.locator('.note-templates').getAttribute('open'), null, 'the template choice folds away below the note');
    assert.equal(await card.locator('.note-box > textarea + .note-gaps').count(), 1);
    if (mobile) {
      const box = await card.locator('.note-gap').first().boundingBox();
      assert.ok(box && box.height >= 38, `a full-size tap target (${box?.height}px)`);
    }
    report('a card with no note starts from the suggested template, fills what the record knows and selects the first gap');

    // 1b. The CV space: which CV, and the pack's line added before the sign-off and taken out again.
    const cvSpace = card.locator('.note-cv');
    const started = await note.inputValue();
    assert.match(await cvSpace.innerText(), /No CV in this note: the site's Request CV link covers it\./);
    assert.ok(started.includes('portfolio.example.com'), 'the first approach carries the site link');
    assert.equal(await card.locator('.note-checks li', { hasText: 'No link to portfolio.example.com' }).count(), 0);
    await cvSpace.getByRole('button', { name: 'Add CV line', exact: true }).click();
    const withCv = await note.inputValue();
    assert.ok(withCv.endsWith("My shout.\n\nI've attached my CV in case it's useful.\n\nMorgan"), 'the line goes in above the sign-off');
    assert.match(await cvSpace.innerText(), /Attach your Director of Engineering \/ CTO CV when you send it\./);
    assert.match(await cvSpace.innerText(), /Still a draft/);
    assert.equal(await cvSpace.getByRole('link', { name: 'Open CV ↗' }).count(), 1);
    await cvSpace.getByRole('button', { name: 'Remove CV line', exact: true }).click();
    assert.equal(await note.inputValue(), started);
    await note.evaluate(el => { el.value = el.value.replace('The short version is here: portfolio.example.com', ''); el.dispatchEvent(new Event('input')); });
    assert.equal(await card.locator('.note-checks li', { hasText: 'No link to portfolio.example.com' }).count(), 1, 'a first approach without the link is flagged');
    await note.evaluate((el, text) => { el.value = text; el.dispatchEvent(new Event('input')); }, started);
    report('the CV space names the CV to attach, adds and removes its line, and a missing site link is flagged');

    // 2. Gaps block copying and recording the send.
    await forget();
    await card.getByRole('button', { name: 'Copy', exact: true }).click();
    await notice('Fill in the personal line first.');
    assert.equal(await copied(), undefined);
    await card.getByRole('button', { name: alex.progress.primary.label, exact: true }).click();
    await page.waitForFunction(id => document.querySelector(`.approach-card[data-action-id="${id}"] .approach-status`)?.textContent.includes('Fill in the gaps'), alex.id);
    assert.equal((await state()).outcomes.length, s0.outcomes.length, 'nothing recorded while a gap is left');
    report('a note with a gap is neither copied nor recorded as sent');

    // 3. Fill the gaps; Copy copies the note and saves it on the card as the owner's.
    await card.locator('.note-gap', { hasText: 'Personal line' }).click();
    await page.keyboard.type('Good to see the payments launch went out.');
    await card.locator('.note-gap', { hasText: 'Why their company' }).click();
    assert.equal(await selected(), '{{reason}}');
    await page.keyboard.type('you are hiring an engineering lead');
    assert.equal(await card.locator('.note-gap').count(), 0);
    assert.equal(await card.locator('.approach-top .chip').innerText(), 'Ready to review');
    assert.equal(await card.locator('.week-prepare-button').innerText(), 'Review and send');
    const written = await note.inputValue();
    assert.doesNotMatch(written, /\{\{/);
    await card.getByRole('button', { name: 'Copy', exact: true }).click();
    await notice('Copied and saved on the card. Nothing was sent.');
    await settle();
    const readable = written.replace(/\*\*([^*\n]+)\*\*/g, '$1');
    assert.equal(await copied(), readable, 'copy sends the wording, without emphasis marks');
    assert.notEqual(readable, written, 'the note keeps the emphasis marks');
    let s = await state();
    const saved = s.drafts.find(d => d.record_id === alex.record_id);
    assert.equal(saved?.text, written);
    assert.equal(saved?.actor, 'owner');
    assert.equal(saved?.purpose, 'message');
    assert.equal(s.outcomes.length, s0.outcomes.length, 'copying sends and records nothing');
    const again = page.locator(`.week-list .approach-card[data-action-id="${alex.id}"]`);
    assert.ok(await again.locator('.week-preparation').evaluate(el => el.open), 'copying and saving keeps preparation open for the contact link and recording controls');
    assert.equal(await again.locator('textarea[name=message]').inputValue(), written, 'the saved note is the card\'s note');
    assert.equal(await again.locator('.approach-points > summary').innerText(), 'Why this note');
    await again.getByRole('button', { name: alex.progress.primary.label, exact: true }).click();
    await notice('recorded');
    s = await state();
    const sent = s.outcomes.find(o => o.kind === 'sent' && o.snapshot?.action_id === alex.id);
    assert.equal(sent?.snapshot?.message, written, 'the send keeps the words that were copied');
    assert.equal(await again.locator('.week-progress').evaluate(el => el.open), false, 'waiting starts compact');
    await again.locator('.week-progress > summary').click();
    await again.getByRole('button', { name: 'They replied', exact: true }).waitFor();
    assert.equal(await again.locator('.approach-context').evaluate(el => el.open), false, 'older preparation is folded after a send');
    assert.equal(await again.getByRole('button', { name: 'They replied', exact: true }).isVisible(), true,
      'reply controls remain available on the concise card');
    report('filled gaps copy, save the note as yours, and the recorded send keeps those words');

    // 4. Email: the subject is copied on its own, the desk is chosen, and the rate stays a gap.
    const riley = actionOf(s, 'Riley Recruiter', 'recruiter');
    let block = await sheet(riley.id);
    await block.getByRole('button', { name: 'Write from this template', exact: true }).click();
    const mail = block.locator('textarea[name=message]');
    assert.ok((await mail.inputValue()).startsWith('Subject: Interim Head of QE, available now, London\n\nHi Riley,'), 'the QE CV picks the QE desk');
    assert.ok(await block.locator('.note-gap', { hasText: 'Day rate' }).count() === 1, 'the rate is never filled in');
    assert.match(await block.locator('.note-cv').innerText(), /Attach your .+ CV when you send it\./, "the recruiter email's own \"CV attached\"");
    assert.equal(await block.locator('.note-cv button').count(), 0, 'its own wording is not added to or taken out');
    await block.locator('.note-templates > summary').click();
    await block.locator('select[name=desk]').selectOption('ai');
    assert.ok((await mail.inputValue()).startsWith('Subject: AI engineer, founding or forward-deployed, available now, London'),
      'an untouched note follows the desk');
    assert.match(await mail.inputValue(), /AI pull-request review agent into the developer workflow\./);
    await block.locator('.note-gap', { hasText: 'Where you saw them' }).click();
    await page.keyboard.type('your post about a founding engineer brief');
    await block.locator('.note-gap', { hasText: 'Day rate' }).click();
    await page.keyboard.type('NNN');
    await block.locator('.note-gap', { hasText: 'Other desk' }).click();
    await block.locator('.note-choice', { hasText: 'quality engineering roles' }).click();
    assert.equal(await block.locator('.note-gap').count(), 0);
    await forget();
    await block.getByRole('button', { name: 'Copy subject', exact: true }).click();
    await notice('Subject copied.');
    assert.equal(await copied(), 'AI engineer, founding or forward-deployed, available now, London');
    await block.getByRole('button', { name: 'Copy', exact: true }).click();
    await notice('Copied and saved');
    assert.ok((await copied()).startsWith('Hi Riley,\n\nI saw your post about a founding engineer brief'), 'the body is copied without its subject');
    report('an email keeps its subject apart, follows the chosen desk, and leaves the rate for you');

    // 5. A due follow-up starts from the bump; "I followed up" records it.
    backdate(database, "UPDATE outcomes SET at=? WHERE kind='sent' AND record_id=?", new Date(Date.now() - 8 * 864e5).toISOString(), alex.record_id);
    block = await sheet(alex.id);
    assert.equal((await block.locator('.chip').first().textContent()).trim(), 'Follow-up suggested');
    const bump = 'Hi Alex, bumping this in case it got buried. **Even one name would help.**\n\nThanks,\nMorgan';
    assert.equal(await block.getByLabel('Follow-up to send', { exact: true }).inputValue(), bump);
    assert.equal(await block.locator('.note-cv').count(), 0, 'a follow-up has no CV space');
    assert.equal(await block.locator('.note-checks li', { hasText: 'No link to portfolio.example.com' }).count(), 0, 'nor needs the link');
    assert.equal(await block.locator('.approach-sent > summary').innerText(), 'View sent message');
    assert.notEqual(await page.evaluate(() => document.activeElement?.tagName), 'TEXTAREA', 'a prepared follow-up does not take the keyboard');
    // The fixture's later email from Alex puts "They replied" first; the follow-up step is under More.
    const followed = block.locator('.approach-actions button', { hasText: 'I followed up' }).first();
    if (!(await followed.isVisible())) await block.locator('.approach-more > summary').click();
    await followed.click();
    await notice('recorded');
    s = await state();
    assert.equal(s.outcomes.find(o => o.kind === 'followed_up' && o.snapshot?.action_id === alex.id)?.snapshot?.message, bump);
    report('a due follow-up starts from the bump, and recording it keeps those words');

    // 6. After an introduction, the thank-you is ready; nothing is saved by copying it.
    for (const kind of ['replied', 'introduction']) {
      const rec = (await state()).records.find(r => r.id === alex.record_id);
      const answer = await fetch(`${base}/api/records/${alex.record_id}/outcome`, { method: 'POST',
        headers: { 'Content-Type': 'application/json', Origin: base }, body: JSON.stringify({ kind, revision: rec.revision, action_id: alex.id }) });
      assert.ok(answer.ok, `${kind} recorded through the API`);
    }
    block = await sheet(alex.id);
    const steps = block.locator('.note-step');
    await steps.locator(':scope > summary').click();
    const thanks = steps.locator('.note-box').filter({ hasText: 'Same day as an intro' });
    assert.equal(await thanks.locator('textarea').inputValue(), 'Thanks for the intro to {{name}}, Alex. We\'re speaking on {{date}}. I\'ll let you know how it goes.');
    assert.equal(await steps.locator('.note-box').filter({ hasText: 'When something comes of it' }).count(), 1);
    const draftsBefore = JSON.stringify((await state()).drafts);
    await thanks.locator('.note-gap', { hasText: "Who you're speaking to" }).click();
    await page.keyboard.type('Jamie');
    await thanks.locator('.note-gap', { hasText: 'When' }).click();
    await page.keyboard.type('Tuesday');
    await thanks.getByRole('button', { name: 'Copy', exact: true }).click();
    await notice('Copied.');
    assert.equal(await copied(), 'Thanks for the intro to Jamie, Alex. We\'re speaking on Tuesday. I\'ll let you know how it goes.');
    assert.equal(JSON.stringify((await state()).drafts), draftsBefore, 'a step message is copied, never stored');
    report('after an introduction the thank-you and the later update are ready to fill and copy');

    // 7. The person's recorded ask decides: a catch-up to a peer starts from the warm-contact template.
    const taylor = actionOf(await state(), 'Taylor Fixture');
    block = await sheet(taylor.id);
    assert.equal(await block.locator('select[name=template]').inputValue(), 'warm');

    // A note waiting for approval on the same record is never replaced from the note box. Before contact the
    // card is "Note to review" with no note box; a follow-up that falls due still offers the bump, copy only.
    const post = async (path, body) => (await fetch(`${base}${path}`, { method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: base }, body: JSON.stringify(body) })).ok;
    const revisionOf = async id => (await state()).records.find(r => r.id === id).revision;
    assert.ok(await post(`/api/records/${taylor.record_id}/outcome`, { kind: 'sent', revision: await revisionOf(taylor.record_id), action_id: taylor.id }));
    backdate(database, "UPDATE outcomes SET at=? WHERE kind='sent' AND record_id=?", new Date(Date.now() - 9 * 864e5).toISOString(), taylor.record_id);
    const held = 'An approval note kept for review.';
    assert.ok(await post(`/api/records/${taylor.record_id}/draft`, { revision: await revisionOf(taylor.record_id), text: held, title: 'For review', purpose: 'approval' }));
    block = await sheet(taylor.id);
    assert.equal((await block.locator('.chip').first().textContent()).trim(), 'Follow-up suggested');
    assert.equal(await block.locator('.note-copy-only').count(), 1);
    assert.equal(await block.locator('textarea[name=message]').count(), 0, 'nothing on this card can save over it');
    assert.match(await block.locator('.note-box textarea').inputValue(), /^Hi Taylor, bumping this/);
    await block.getByRole('button', { name: 'Copy', exact: true }).click();
    await notice('Copied.');
    const followUp = block.locator('.approach-actions button', { hasText: 'I followed up' }).first();
    if (!(await followUp.isVisible())) await block.locator('.approach-more > summary').click();
    await followUp.click();
    await notice('recorded');
    s = await state();
    assert.ok(s.outcomes.some(o => o.kind === 'followed_up' && o.snapshot?.action_id === taylor.id), 'the follow-up is recorded');
    const kept = s.drafts.find(d => d.record_id === taylor.record_id);
    assert.equal(kept?.text, held);
    assert.equal(kept?.purpose, 'approval');
    report('a note waiting for approval on the same person is never replaced from the note box');

    // 8. Research > Templates: the whole pack, each piece with its own Copy.
    await open('templates');
    assert.deepEqual(await page.locator('.guide-section > h2').allInnerTexts(), ['How to use this pack', 'Templates', 'Follow-ups and thank-yous',
      'Blurbs others can forward', 'The ask ladder', 'The core', 'Proof points', "Don't say", 'Before you send']);
    assert.equal(await page.locator('.guide-template').count(), pack.templates.length);
    await page.locator('.guide-template', { hasText: 'Friend' }).locator('summary').click();
    assert.equal(await page.locator('.guide-template[data-template=friend] pre').count(), 2, 'email and the WhatsApp version');
    await page.locator('.guide-piece', { hasText: 'Third person' }).getByRole('button', { name: 'Copy', exact: true }).click();
    await notice('Paragraph copied.');
    assert.equal(await copied(), pack.blurbs.paragraph);
    assert.equal(await page.locator('.research-nav [data-research=templates]').getAttribute('aria-pressed'), 'true');
    report('Research › Templates shows the whole pack, and each piece copies');

    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.kill();
    rmSync(dir, { recursive: true, force: true });
  }
}

const failures = [];
for (const [name, type, mobile] of [['chromium', chromium, false], ['webkit', webkit, true]]) {
  if (!wanted.includes(name)) continue;
  try { await run(type, mobile); }
  catch (error) { failures.push(`${name}: ${error.stack || error.message}`); }
}
if (failures.length) {
  console.log(`FAIL  templates\n${failures.join('\n')}`);
  process.exit(1);
}
