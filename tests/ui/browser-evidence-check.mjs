// Exercise the diagnostic path using synthetic pages only, with no server or external requests.
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, webkit } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { createBrowserEvidence } from './browser-evidence.mjs';

const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
assert.ok(wanted.length && wanted.every(name => ['chromium', 'webkit'].includes(name)), 'known browser engines selected');
for (const name of wanted) {
  const started = performance.now();
  const root = mkdtempSync(join(tmpdir(), 'wo-evidence-browser-'));
  const evidenceRoot = join(root, 'evidence');
  const previousTemp = process.env.TMPDIR;
  // Playwright can leave its own profile/artifact directories when launch validation fails.
  process.env.TMPDIR = root;
  let browser;
  try {
    mkdirSync(evidenceRoot);
    browser = await launchBrowser({ chromium, webkit }[name],
      name === 'chromium' && process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {});
    const context = await browser.newContext({ viewport: { width: 640, height: 360 } });
    const evidence = createBrowserEvidence({ suite: 'self-check', browser: name, artifactRoot: evidenceRoot, trace: true });
    await evidence.attach(context);
    const page = await context.newPage();
    await page.setContent('<h1>Synthetic evidence check</h1><p>No personal data or signed-in browser.</p>');
    await page.route('**/synthetic-failure*', route => route.abort('failed'));
    const pageError = page.waitForEvent('pageerror');
    await page.evaluate(async () => {
      console.error('synthetic console error');
      setTimeout(() => { throw new Error('synthetic page error'); }, 0);
      await fetch('http://127.0.0.1:8761/synthetic-failure?token=synthetic').catch(() => {});
    });
    await pageError;
    const original = new Error('synthetic assertion failure');
    const directory = await evidence.finish({ error: original });
    const report = JSON.parse(readFileSync(join(directory, 'diagnostics.json')));
    assert.match(report.error, /synthetic assertion failure/);
    assert.ok(report.events.some(event => event.kind === 'console' && event.text === 'synthetic console error'));
    assert.ok(report.events.some(event => event.kind === 'pageerror' && event.text === 'synthetic page error'));
    assert.ok(report.events.some(event => event.kind === 'requestfailed' && event.url === 'http://127.0.0.1:8761/synthetic-failure'));
    assert.equal(readFileSync(join(directory, 'failure.png')).subarray(1, 4).toString(), 'PNG');
    assert.equal(readFileSync(join(directory, 'trace-1.zip')).subarray(0, 2).toString(), 'PK');
    assert.deepEqual(report.captureErrors, []);
    await context.close();
    rmSync(directory, { recursive: true });

    const passing = await browser.newContext();
    const clean = createBrowserEvidence({ suite: 'self-check', browser: name, artifactRoot: evidenceRoot, trace: true });
    await clean.attach(passing);
    await (await passing.newPage()).setContent('<p>Recovered</p>');
    assert.equal(await clean.finish(), null);
    assert.deepEqual(readdirSync(evidenceRoot), []);
    await passing.close();

    const lost = await browser.newContext();
    const disconnected = createBrowserEvidence({ suite: 'self-check', browser: name, artifactRoot: evidenceRoot, trace: false });
    await disconnected.attach(lost);
    await (await lost.newPage()).setContent('<p>Synthetic closed browser</p>');
    await lost.close();
    let caught;
    try { throw original; }
    catch (error) { await disconnected.finish({ error }); caught = error; }
    assert.equal(caught, original, 'closed browser capture must not replace the assertion');
    console.log(`ok browser evidence ${name}: console/page/network failure, PNG/trace export, recovery cleanup and disconnected browser (${Math.round(performance.now() - started)}ms)`);
  } finally {
    try { await browser?.close(); }
    finally {
      if (previousTemp === undefined) delete process.env.TMPDIR; else process.env.TMPDIR = previousTemp;
      rmSync(root, { recursive: true, force: true });
    }
  }
}
