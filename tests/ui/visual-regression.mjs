// Small visual contract over the real app and synthetic records. No live data or writes.
import assert from 'node:assert/strict';
import { chromium, webkit, devices } from 'playwright-core';
import { launchBrowser } from '../../tools/browser_launch.mjs';
import { createBrowserEvidence } from './browser-evidence.mjs';
import { assertVisualBaseline, assertEnvironment, comparison, lockBaselines } from './visual-diff.mjs';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, renameSync, rmSync, existsSync } from 'node:fs';
import { tmpdir, release, version } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';

const here = dirname(fileURLToPath(import.meta.url)), root = join(here, '../..');
const clock = '2026-11-06T12:00:00.000Z';
const stateNames = ['this-week', 'jobs', 'people', 'sending', 'job-detail', 'new-record', 'dashboard-research', 'dashboard-comparison', 'dashboard-choices', 'dashboard-collection'];
const args = process.argv.slice(2), update = args.includes('--update'), prove = args.includes('--prove-detection');
let selected = stateNames;
for (let i = 0; i < args.length; i++) {
  if (['--update', '--prove-detection'].includes(args[i])) continue;
  if (args[i] === '--state' && stateNames.includes(args[i + 1])) { selected = [args[++i]]; continue; }
  throw new Error('Usage: node tests/ui/visual-regression.mjs [--update | --prove-detection] [--state this-week|jobs|people|job-detail|new-record]');
}
assert.ok(!(update && prove), '--update cannot be combined with --prove-detection');
const wanted = (process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',');
assert.ok(wanted.length && wanted.every(name => ['chromium', 'webkit'].includes(name)), 'WO_UI_BROWSERS must name chromium and/or webkit');
const started = performance.now();
const artifactRoot = process.env.WO_UI_ARTIFACT_DIR || tmpdir();
const previousTemp = process.env.TMPDIR;
let temporary, artifacts, server, unlockBaselines, activeBrowser;
let serverError = '', retained = prove, failures = 0, interrupted;
async function cleanup(label, action) {
  try { await action(); }
  catch (error) { failures++; console.error(`Visual cleanup (${label}): ${error.message}`); }
}
function interrupt(signal) {
  if (interrupted) return;
  interrupted = signal;
  // Unblock in-flight browser/server operations so their finally blocks can release resources.
  server?.kill();
  if (activeBrowser) void cleanup('interrupted browser', () => activeBrowser.close());
}
const onInterrupt = () => interrupt('SIGINT'), onTerminate = () => interrupt('SIGTERM');
process.on('SIGINT', onInterrupt); process.on('SIGTERM', onTerminate);

async function serverAddress() {
  return new Promise((resolve, reject) => {
    let text = '';
    const timer = setTimeout(() => done(new Error(`Visual fixture server did not start: ${serverError}`)), 15000);
    const onExit = () => done(new Error(`Visual fixture server exited: ${serverError}`));
    const onError = error => done(error);
    const onData = bytes => {
      text += bytes;
      const match = text.match(/^http:\/\/127\.0\.0\.1:\d+$/m);
      if (match) done(null, match[0]);
    };
    function done(error, address) {
      clearTimeout(timer); server.off('exit', onExit); server.off('error', onError); server.stdout.off('data', onData);
      if (error) reject(error); else resolve(address);
    }
    server.once('exit', onExit); server.once('error', onError); server.stdout.on('data', onData);
  });
}

async function show(page, base, state, job, mobile) {
  const route = { 'this-week': 'week', jobs: 'work', people: 'people', sending:'people', 'job-detail': `work/${job.id}`, 'new-record': 'week', 'dashboard-research':'dashboard', 'dashboard-comparison':'dashboard', 'dashboard-choices':'dashboard', 'dashboard-collection':'dashboard' }[state];
  // Each state starts afresh so --state produces the same background, filters and scroll position.
  await page.goto('about:blank');
  await page.goto(`${base}/#${route}`);
  await page.locator('#content > *').first().waitFor();
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent !== 'Connecting…');
  if (state === 'job-detail') {
    await page.locator('#detail[open] .job-controls').waitFor();
    await page.waitForFunction(() => !document.querySelector('#detail .timeline-loading'));
  }
  if (state === 'new-record') { await page.locator('#new-record').click(); await page.locator('#modal[open]').waitFor(); }
  if (state === 'sending') {
    await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/message-templates')),page.getByRole('button',{name:'Send messages',exact:true}).click()]);
    const modal=page.locator('#modal');
    await modal.getByLabel('Find people for this batch').fill('Alex Example');
    await modal.getByLabel('Alex Example',{exact:true}).check();
    await modal.getByRole('button',{name:'Start batch',exact:true}).click();
    await modal.getByLabel('Channel',{exact:true}).selectOption('email');
    await modal.getByLabel('Template',{exact:true}).selectOption('former_colleague');
    await modal.getByLabel('Message to personalise').fill('Subject: Catching up\n\nHi Alex,\n\nIt was good working together on the migration. Fancy a catch-up? My recent work is at portfolio.example.com.\n\nThanks,\nMorgan');
    await modal.evaluate(n=>n.scrollTop=0);
  }
  if(state.startsWith('dashboard-')) {
    await page.waitForFunction(() => document.querySelector('.dashboard-figures')?.textContent.startsWith('Server figures updated'));
    if(state==='dashboard-collection')await page.locator('.quota-provider > summary').first().click();
    if(state==='dashboard-comparison') {
      await page.locator('.intake-comparison > summary').click();
      await page.getByRole('button',{name:'Compare candidates',exact:true}).click();
      await page.getByRole('button',{name:'Refresh comparison',exact:true}).waitFor();
    }

  }
  await page.evaluate(async () => {
    await document.fonts.ready;
    document.activeElement?.blur();
    window.scrollTo(0, 0);
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await page.mouse.move(0, 0);
  if (mobile && ['this-week', 'jobs'].includes(state)) {
    await page.locator(state === 'this-week' ? '.week-discovery' : '#content .opportunity-list').evaluate(node => {
      const header = document.querySelector('.topbar').getBoundingClientRect().height;
      window.scrollTo(0, window.scrollY + node.getBoundingClientRect().top - header - 16);
    });
  }
  if(state.startsWith('dashboard-')) {
    await page.locator(state==='dashboard-collection'?'.dashboard-quotas':state==='dashboard-research'?'.research-decisions':state==='dashboard-comparison'?'.intake-comparison':'.dashboard-attention').evaluate(node => {
      window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - 72);
    });
    await page.waitForFunction(selector => document.querySelector(selector).getBoundingClientRect().top < 100, state==='dashboard-collection'?'.dashboard-quotas':state==='dashboard-research'?'.research-decisions':state==='dashboard-comparison'?'.intake-comparison':'.dashboard-attention');
  }
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${state}: horizontal overflow`);
}

async function run(base, name) {
  const mobile = name === 'webkit';
  const options = { ...(mobile ? devices['iPhone 13'] : { viewport: { width: 1300, height: 900 }, deviceScaleFactor: 1 }),
    locale: 'en-GB', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce' };
  let browser, error, evidence;
  try {
    browser = await launchBrowser(mobile ? webkit : chromium, {
      ...(!mobile && process.env.WO_UI_CHROMIUM ? { executablePath: process.env.WO_UI_CHROMIUM } : {}),
      // This entry point owns cooperative cleanup; Playwright must not exit before finally runs.
      handleSIGINT: false, handleSIGTERM: false,
    });
    activeBrowser = browser;
    if (interrupted) throw new Error(`Interrupted by ${interrupted}`);
    const context = await browser.newContext(options);
    evidence = createBrowserEvidence({ suite: 'visual', browser: name, artifactRoot: artifacts });
    await evidence.attach(context);
    // An external request is a fixture defect, never a reason to read production/provider data.
    const externalRequests = [];
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin === base) return route.continue();
      externalRequests.push(`${url.origin}${url.pathname}`);
      return route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    await page.clock.setFixedTime(new Date(clock));
    const errors = [], captureWarnings = [];
    let capturing = false;
    const screenshot = async () => {
      capturing = true;
      try { return await page.screenshot({ scale: 'css', caret: 'initial' }); }
      finally { capturing = false; }
    };
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', message => {
      if (message.type() !== 'error') return;
      // Same narrow exception as user-guide.mjs: WebKit capture injects a stylesheet
      // rejected by the real CSP. Outside capture, this remains an application error.
      if (mobile && capturing && message.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"))
        captureWarnings.push(message.text());
      else errors.push(message.text());
    });
    const data = await (await fetch(`${base}/api/state`)).json();
    const job = data.records.find(record => record.type === 'job' && record.name === 'Interim Engineering Manager' && record.organisation === 'Example Rail Ltd');
    assert.ok(job, 'synthetic job exists');
    const environment = { schema: 1, platform: process.platform, arch: process.arch, osRelease: release(), osVersion: version(),
      browser: name, browserVersion: browser.version(), playwright: JSON.parse(readFileSync(join(here, 'node_modules/playwright-core/package.json'))).version,
      viewport: options.viewport, deviceScaleFactor: options.deviceScaleFactor, locale: options.locale, timezoneId: options.timezoneId,
      colorScheme: options.colorScheme, reducedMotion: options.reducedMotion, screenshotScale: 'css',
      capturePolicy: 'viewport-mobile-content-v1', clock, comparison };
    const baselineDir = join(here, 'visual-baselines', `${process.platform}-${process.arch}-${release()}`, `${name}-${browser.version()}`);
    const manifest = join(baselineDir, 'environment.json');
    if (existsSync(manifest)) assertEnvironment(JSON.parse(readFileSync(manifest)), environment, { replaceAll: update && selected === stateNames });
    else assert.ok(update, `Missing baseline environment: ${baselineDir}. Run --update explicitly on this OS/browser, inspect images, and commit them.`);
    const candidates = [];
    let proved = false;
    for (const state of selected) {
      if (interrupted) throw new Error(`Interrupted by ${interrupted}`);
      await show(page, base, state, job, mobile);
      const actual = await screenshot();
      const baselinePath = join(baselineDir, `${state}.png`), artifactDir = join(artifacts, name);
      if (update) candidates.push([baselinePath, actual]);
      else assertVisualBaseline({ actual, baselinePath, artifactDir, name: state });
      console.log(`ok visual ${name}: ${state}${update ? ' (candidate)' : ''}`);
      if (prove && !proved) {
        const body = page.locator('body');
        const previous = await body.getAttribute('style');
        await body.evaluate(node => { node.style.opacity = '0.5'; });
        const changed = await screenshot();
        assert.throws(() => assertVisualBaseline({ actual: changed, baselinePath, artifactDir, name: `${state}-deliberate-change` }), /Visual mismatch/);
        await body.evaluate((node, value) => value === null ? node.removeAttribute('style') : node.setAttribute('style', value), previous);
        assertVisualBaseline({ actual: await screenshot(), baselinePath, artifactDir, name: `${state}-restored` });
        console.log(`ok visual ${name}: deliberate CSS change failed comparison; restoration passed`);
        proved = true;
      }
    }
    assert.deepEqual(errors, [], 'visual flows contain no browser errors');
    assert.deepEqual(externalRequests, [], 'visual fixtures make no external requests');
    if (captureWarnings.length) console.log(`note visual ${name}: ${captureWarnings.length} known capture-only CSP warnings; app errors remain checked`);
    if (update) {
      mkdirSync(baselineDir, { recursive: true });
      for (const [path, bytes] of candidates) {
        const pending = `${path}.${process.pid}.tmp`;
        writeFileSync(pending, bytes); renameSync(pending, path);
      }
      const pendingManifest = `${manifest}.${process.pid}.tmp`;
      writeFileSync(pendingManifest, JSON.stringify(environment, null, 2) + '\n');
      renameSync(pendingManifest, manifest);
      console.log(`REVIEW visual baselines before committing: ${baselineDir}`);
    }
  } catch (caught) { error = caught; throw caught; }
  finally {
    await evidence?.finish(interrupted ? {} : { failed: Boolean(error), error });
    if (browser) await cleanup('browser', () => browser.close());
    activeBrowser = undefined;
  }
}

try {
  unlockBaselines = lockBaselines(join(here, 'visual-baselines'));
  temporary = mkdtempSync(join(tmpdir(), 'wo-visual-fixture-'));
  // Own Playwright's launch profiles/artifacts even when its startup fails partway through.
  process.env.TMPDIR = temporary;
  mkdirSync(artifactRoot, { recursive: true, mode: 0o700 });
  artifacts = mkdtempSync(join(artifactRoot, 'wo-visual-'));
  server = spawn(join(root, '.venv/bin/python'), [join(here, 'visual_server.py'), join(temporary, 'fixture.db')],
    { env: { ...process.env, TZ: 'UTC' }, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', bytes => { serverError = (serverError + bytes).slice(-4000); });
  const base = await serverAddress();
  for (const name of wanted) {
    if (interrupted) break;
    try { await run(base, name); }
    catch (error) { if (!interrupted) { failures++; retained = true; console.error(`FAIL visual ${name}: ${error.stack}`); } }
  }
} catch (error) { if (!interrupted) { failures++; console.error(error.stack); } }
finally {
  await cleanup('server', async () => {
    if (server?.pid && server.exitCode === null && server.signalCode === null) {
      const exited = once(server, 'exit');
      server.kill();
      const killTimer = setTimeout(() => server.kill('SIGKILL'), 2000);
      try { await exited; } finally { clearTimeout(killTimer); }
    }
  });
  if (temporary) await cleanup('fixture', () => rmSync(temporary, { recursive: true, force: true }));
  if (previousTemp === undefined) delete process.env.TMPDIR; else process.env.TMPDIR = previousTemp;
  if (artifacts && (!retained || interrupted && !failures)) await cleanup('captures', () => rmSync(artifacts, { recursive: true, force: true }));
  else if (artifacts) console.log(`Visual evidence: ${artifacts}`);
  await cleanup('baseline lock', () => unlockBaselines?.());
  process.off('SIGINT', onInterrupt); process.off('SIGTERM', onTerminate);
}
console.log(`Visual suite: ${interrupted || (failures ? 'FAIL' : 'PASS')} in ${((performance.now() - started) / 1000).toFixed(1)}s`);
process.exitCode = interrupted === 'SIGINT' ? 130 : interrupted === 'SIGTERM' ? 143 : failures ? 1 : 0;
