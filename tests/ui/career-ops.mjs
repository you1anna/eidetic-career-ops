import assert from 'node:assert/strict';
import {execFileSync, spawn} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {mkdir} from 'node:fs/promises';
import net from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createBrowserEvidence} from './browser-evidence.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = join(root, '.venv/bin/python');
const modulePath = process.env.PLAYWRIGHT_MODULE || new URL('./node_modules/playwright-core/index.mjs', import.meta.url).pathname;
if (!modulePath) throw new Error('Set PLAYWRIGHT_MODULE to an installed Playwright module.');
if (process.env.PLAYWRIGHT_BROWSERS_PATH && !existsSync(process.env.PLAYWRIGHT_BROWSERS_PATH)) {
  delete process.env.PLAYWRIGHT_BROWSERS_PATH;
}
const {chromium, webkit, devices} = await import(pathToFileURL(modulePath));
const {launchBrowser} = await import(new URL('../../tools/browser_launch.mjs', import.meta.url));

const wanted = new Set((process.env.WO_UI_BROWSERS || 'chromium,webkit').split(',').filter(Boolean));
const chromiumLaunch = process.env.WO_UI_CHROMIUM ? {executablePath: process.env.WO_UI_CHROMIUM} : {};
const screenshots = process.env.SCREENSHOT_DIR || '/tmp/career-ops-qa';
await mkdir(screenshots, {recursive: true});

const bannedPhrases = [
  'career/ops would',
  'In this fictional example',
  'No live claim is made',
  'do not assume causation',
  'autonomously rewrite product code',
];

const freePort = () => new Promise(resolve => {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1', () => {
    const {port} = probe.address();
    probe.close(() => resolve(port));
  });
});

async function startLocalServer() {
  const dir = mkdtempSync(join(tmpdir(), 'wo-career-ops-'));
  const db = join(dir, 'ui.db');
  if (process.env.WO_UI_DB && existsSync(process.env.WO_UI_DB)) {
    execFileSync(python, [join(root, 'tools/wo.py'), '--db', process.env.WO_UI_DB, 'backup', db], {stdio: 'ignore'});
  } else {
    execFileSync(python, [join(root, 'tools/make_fixture.py'), db], {stdio: 'ignore'});
  }
  const port = await freePort();
  const proc = spawn(python, [join(root, 'workspace/server.py'), '--db', db, '--port', String(port)], {stdio: 'ignore'});
  const url = `http://127.0.0.1:${port}/career-ops`;
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(url)).ok) return {url, stop: () => { proc.kill(); rmSync(dir, {recursive: true, force: true}); }};
    } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  proc.kill();
  rmSync(dir, {recursive: true, force: true});
  throw new Error('workspace server did not start for /career-ops');
}

const allCases = [
  ['desktop', 'chromium', chromium, {viewport: {width: 1440, height: 1000}}],
  ['concept-size', 'chromium', chromium, {viewport: {width: 1086, height: 1448}}],
  ['mobile', 'webkit', webkit, {...devices['iPhone 13']}],
  ['narrow', 'chromium', chromium, {viewport: {width: 320, height: 800}}],
].filter(([, kind]) => wanted.has(kind));

const names = ['Direction', 'Discover', 'Assess', 'Connect', 'Continue', 'Review'];
const readmePlain = readFileSync(join(root, 'docs', 'product.md'), 'utf8').replace(/\*\*/g, '').replace(/(Relevant work for each candidate|Reasons behind the recommendations|Less software to build)\./g, '$1').replace(/\s+/g, ' ');

const local = process.env.BASE_URL ? null : await startLocalServer();
const base = process.env.BASE_URL || local.url;

try {
  for (const [name, kind, engine, options] of allCases) {
    const launchOpts = kind === 'chromium' ? {headless: true, ...chromiumLaunch} : {headless: true};
    const browser = await launchBrowser(engine, launchOpts);
    const evidence = createBrowserEvidence({suite: 'career-ops', browser: name});
    let failure;
    try {
      const context = await browser.newContext({...options, reducedMotion: 'reduce'});
      await evidence.attach(context);
      const page = await context.newPage();
      const errors = [];
      const externalRequests = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
      page.on('request', r => { if (new URL(r.url()).origin !== new URL(base).origin) externalRequests.push(r.url()); });
      const response = await page.goto(base, {waitUntil: 'networkidle'});
      assert.equal(response.status(), 200);
      assert.match(await page.title(), /career\/ops/);
      const headline = await page.locator('h1').innerText();
      assert.equal(headline, 'Your next job is probably one conversation away.');
      assert.equal(await page.locator('h1').count(), 1);
      assert.equal(await page.locator('[role=tab][aria-selected=true]').innerText(), '03\nAssess');

      // A changed DOM far below the phone viewport looks like an inert control.
      // Keep the result visible without a second gesture or manual scroll.
      if (page.viewportSize().width <= 760) {
        for (const [view, title] of [
          ['adviser', 'Help Alex weigh the evidence'],
          ['candidate', 'Northbank: worth a closer look'],
        ]) {
          await page.locator('.perspectives').evaluate(el => el.scrollIntoView({block: 'start', behavior: 'instant'}));
          const button = page.locator(`[data-view=${view}]`);
          if (options.hasTouch) await button.tap();
          else await button.click();
          assert.equal(await button.getAttribute('aria-pressed'), 'true', 'One tap must switch perspective');
          assert.equal(await page.locator('#example-title').innerText(), title);
          const visible = await page.locator('#example-title').evaluate(el => {
            const rect = el.getBoundingClientRect();
            return rect.top >= 0 && rect.bottom <= innerHeight;
          });
          assert.ok(visible, `${name}: changed perspective title must be visible after one tap`);
        }
      }

      const technology = await page.locator('.technology-points li').allInnerTexts();
      assert.deepEqual(await page.locator('.technology-points h3').allInnerTexts(), ['Relevant work for each candidate', 'Reasons behind the recommendations', 'Less software to build']);
      const valuePoints = await page.locator('.value-points li').allInnerTexts();
      assert.equal(valuePoints.length, 3);

      const titles = new Set();
      for (const view of ['candidate', 'adviser']) {
        await page.locator(`[data-view=${view}]`).click();
        assert.equal(await page.locator(`[data-view=${view}]`).getAttribute('aria-pressed'), 'true');
        assert.equal(await page.locator('.example-heading .eyebrow').textContent(), view === 'adviser' ? 'Proposed adviser experience' : 'Alex’s workspace');
        for (let i = 0; i < names.length; i++) {
          await page.locator(`#step-${i}`).click();
          assert.equal(await page.locator(`#step-${i}`).getAttribute('aria-selected'), 'true');
          assert.equal(await page.locator('[role=tab][aria-selected=true]').count(), 1);
          assert.equal(await page.locator('#journey-panel').getAttribute('aria-labelledby'), `step-${i}`);
          assert.equal(await page.locator('#stage-kicker').textContent(), `${String(i + 1).padStart(2, '0')} / ${names[i]}`);
          assert.ok((await page.locator('#example-body').innerText()).length > 150);
          assert.match(await page.locator('#choice-label').innerText(), view === 'adviser' ? /Candidate permission/ : /Alex stays/);
          titles.add(await page.locator('#example-title').innerText());
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name}/${view}/${names[i]} horizontal overflow`);
          const panelText = await page.locator('#journey-panel').innerText();
          for (const phrase of bannedPhrases) {
            assert.equal(panelText.includes(phrase), false, `Banned phrase "${phrase}" found in ${view}/${names[i]}`);
          }
        }
      }
      assert.equal(titles.size, 12);
      await page.locator('#step-5').press('ArrowRight');
      assert.equal(await page.locator('#step-0').getAttribute('aria-selected'), 'true');
      await page.locator('#step-0').press('End');
      assert.equal(await page.locator('#step-5').getAttribute('aria-selected'), 'true');
      await page.locator('#step-5').press('Home');
      assert.equal(await page.locator('#step-0').getAttribute('aria-selected'), 'true');
      await page.locator('#next-stage').click();
      assert.equal(await page.locator('#step-1').getAttribute('aria-selected'), 'true');
      for (const detail of await page.locator('details').all()) {
        const wasOpen = await detail.getAttribute('open') !== null;
        await detail.locator('summary').click();
        assert.equal(await detail.getAttribute('open') !== null, !wasOpen);
        await detail.locator('summary').click();
        assert.equal(await detail.getAttribute('open') !== null, wasOpen);
      }
      const bodyText = await page.locator('body').innerText();
      for (const phrase of bannedPhrases) {
        assert.equal(bodyText.includes(phrase), false, `Banned phrase "${phrase}" found on page`);
      }
      for (const href of ['#journey', '#technology', '#vision', '#partner']) {
        await page.locator(`nav a[href="${href}"]`).click();
        assert.equal(new URL(page.url()).hash, href);
      }
      const [download] = await Promise.all([
        page.waitForEvent('download'),
        page.getByRole('link', {name: 'Download discussion brief'}).click(),
      ]);
      assert.equal(download.suggestedFilename(), 'career-ops-discussion-brief.txt');
      assert.equal(await download.failure(), null);
      const briefPath = await download.path();
      const briefPlain = readFileSync(briefPath, 'utf8').replace(/\s+/g, ' ');
      assert.ok(briefPlain.includes(headline), 'Discussion brief missing headline');
      assert.ok(readmePlain.includes(headline), 'docs/product.md missing headline');
      for (const point of [...valuePoints, ...technology]) {
        const normalised = point.replace(/\s+/g, ' ').trim();
        assert.ok(briefPlain.includes(normalised), `Discussion brief out of sync for point: ${normalised}`);
        assert.ok(readmePlain.includes(normalised), `docs/product.md out of sync for point: ${normalised}`);
      }
      await page.locator('[data-view=candidate]').click();
      await page.locator('#step-2').click();
      assert.deepEqual(errors, []);
      assert.deepEqual(externalRequests, [], 'Public demo must not call private APIs or third-party tracking');
      errors.length = 0;
      await page.screenshot({path: `${screenshots}/${name}-full.png`, fullPage: true});
      await page.locator('#journey').screenshot({path: `${screenshots}/${name}-journey.png`});
      await page.locator('#technology').screenshot({path: `${screenshots}/${name}-technology.png`});
      await page.locator('#vision').screenshot({path: `${screenshots}/${name}-vision.png`});
      await page.locator('#partner').screenshot({path: `${screenshots}/${name}-partner.png`});
      const unexpected = errors.filter(e => engine !== webkit || !e.startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"));
      assert.deepEqual(unexpected, []);
      if (errors.length) console.log(`NOTE ${name}: ${errors.length} screenshot-only WebKit CSP warnings`);
      await context.close();
      console.log(`PASS ${name}: identity, copy parity, 12 journey states, keyboard, next step, disclosures, anchors, download, no overflow/errors/external requests`);
    } catch (error) {
      failure = error;
      throw error;
    } finally {
      await evidence.finish({error: failure});
      await browser.close();
    }
  }

  if (wanted.has('chromium')) {
    const browser = await launchBrowser(chromium, {headless: true, ...chromiumLaunch});
    const evidence = createBrowserEvidence({suite: 'career-ops-fallback', browser: 'chromium'});
    let failure;
    try {
      const page = await browser.newPage({javaScriptEnabled: false});
      await evidence.attach(page.context());
      await page.goto(base);
      assert.match(await page.locator('#journey-panel').innerText(), /Northbank: worth a closer look/);
      assert.equal(await page.locator('noscript').isVisible(), true);
      const fallback = await page.locator('#journey-panel').innerText();
      const jsPage = await browser.newPage();
      await evidence.attach(jsPage.context());
      await jsPage.goto(base);
      assert.equal(await jsPage.locator('#journey-panel').innerText(), fallback, 'Static and interactive initial assessment must match');
      await jsPage.close();
      console.log('PASS no-JavaScript fallback: useful initial journey and explanation remain visible');
    } catch (error) {
      failure = error;
      throw error;
    } finally {
      await evidence.finish({error: failure});
      await browser.close();
    }
  }
} finally {
  if (local) local.stop();
}
