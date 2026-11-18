import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync, truncateSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createBrowserEvidence } from './browser-evidence.mjs';

function fixture(t, options = {}) {
  const root = mkdtempSync(join(tmpdir(), 'wo-evidence-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const context = new EventEmitter(), page = new EventEmitter();
  const calls = { start: 0, stop: [], screenshots: 0 };
  context.pages = () => [page];
  context.tracing = {
    start: async () => { calls.start++; },
    stop: async stopOptions => {
      calls.stop.push(stopOptions);
      if (stopOptions?.path) {
        writeFileSync(stopOptions.path, 'synthetic trace');
        if (options.traceBytes) truncateSync(stopOptions.path, options.traceBytes);
      }
    },
  };
  page.isClosed = () => false;
  page.url = () => 'http://127.0.0.1:1234/#work';
  page.screenshot = async ({ path }) => { calls.screenshots++; writeFileSync(path, 'synthetic PNG'); };
  page.locator = () => ({ innerText: async () => 'Synthetic page' });
  const evidence = createBrowserEvidence({ suite: 'test/suite', browser: 'chromium', artifactRoot: root, trace: false, ...options });
  return { root, context, page, calls, evidence };
}

test('passing checks retain no files or trace and detach their observers', async t => {
  const { evidence, context, page, root, calls } = fixture(t);
  await evidence.attach(context);
  assert.equal(page.listenerCount('console'), 1);
  page.emit('console', { type: () => 'error', text: () => 'expected negative-path response' });
  assert.equal(await evidence.finish({ failed: false }), null);
  assert.deepEqual(readdirSync(root), []);
  assert.deepEqual(calls, { start: 0, stop: [], screenshots: 0 });
  assert.equal(page.listenerCount('console'), 0);
  assert.equal(context.listenerCount('page'), 0);
});

test('failed checks retain bounded recent evidence, screenshot and original error', async t => {
  const { evidence, context, page, calls } = fixture(t);
  await evidence.attach(context);
  for (let i = 0; i < 130; i++) page.emit('console', { type: () => 'error', text: () => `${i}:${'x'.repeat(2000)}` });
  page.emit('requestfailed', { method: () => 'GET', url: () => 'https://example.test/private?token=secret', failure: () => ({ errorText: 'connection closed' }) });
  const dir = await evidence.finish({ error: new Error('original assertion') });
  const report = JSON.parse(readFileSync(join(dir, 'diagnostics.json')));
  assert.equal(report.events.length, 100);
  assert.match(report.events[0].text, /^31:/);
  assert.equal(report.events[0].text.length, 1000);
  assert.equal(report.events.at(-1).url, 'https://example.test/private');
  assert.match(report.error, /original assertion/);
  assert.equal(calls.screenshots, 1);
  assert.equal(statSync(dir).mode & 0o777, 0o700);
  assert.equal(statSync(join(dir, 'diagnostics.json')).mode & 0o777, 0o600);
});

test('concurrent failures with identical labels have independent private output', async t => {
  const first = fixture(t), second = createBrowserEvidence({ suite: 'test/suite', browser: 'chromium', artifactRoot: first.root, trace: false });
  const [a, b] = await Promise.all([first.evidence.finish({ failed: true }), second.finish({ failed: true })]);
  assert.notEqual(a, b);
  assert.equal(readdirSync(first.root).length, 2);
  assert.ok(a.startsWith(first.root + '/test-suite-chromium-'));
  assert.ok(b.startsWith(first.root + '/test-suite-chromium-'));
});

test('capture failure records a limitation without replacing the assertion', async t => {
  const { evidence, context, page } = fixture(t);
  await evidence.attach(context);
  page.screenshot = async () => { throw new Error('browser disconnected'); };
  const error = new Error('assertion must remain primary');
  const dir = await evidence.finish({ error });
  const report = JSON.parse(readFileSync(join(dir, 'diagnostics.json')));
  assert.match(report.error, /assertion must remain primary/);
  assert.ok(report.captureErrors.some(item => item.includes('browser disconnected')));
});

test('opt-in tracing starts once per context and discards successful traces', async t => {
  const { evidence, context, calls, root } = fixture(t, { trace: true });
  await evidence.attach(context); await evidence.attach(context);
  await evidence.finish({ failed: false });
  assert.equal(calls.start, 1);
  assert.deepEqual(calls.stop, [undefined]);
  assert.deepEqual(readdirSync(root), []);
});

test('failed tracing retains a trace and finish is idempotent', async t => {
  const { evidence, context, calls } = fixture(t, { trace: true });
  await evidence.attach(context);
  const dir = await evidence.finish({ failed: true });
  assert.equal(await evidence.finish({ failed: true }), dir);
  assert.equal(calls.stop.length, 1);
  assert.equal(readFileSync(join(dir, 'trace-1.zip'), 'utf8'), 'synthetic trace');
});

test('new pages are observed and HTTP failures alone do not fail a check', async t => {
  const { evidence, context, page, root } = fixture(t);
  await evidence.attach(context);
  const second = new EventEmitter(); second.isClosed = () => false;
  context.emit('page', second);
  assert.equal(second.listenerCount('response'), 1);
  second.emit('response', { status: () => 503, url: () => 'http://127.0.0.1/api/status', request: () => ({ method: () => 'GET' }) });
  await evidence.finish({ failed: false });
  assert.deepEqual(readdirSync(root), []);
  assert.equal(page.listenerCount('pageerror'), 0);
  assert.equal(second.listenerCount('response'), 0);
});

test('oversize trace is removed while the failure report remains', async t => {
  const { evidence, context } = fixture(t, { trace: true, traceBytes: 21 * 1024 * 1024 });
  await evidence.attach(context);
  const dir = await evidence.finish({ error: new Error('original failure') });
  assert.deepEqual(readdirSync(dir).sort(), ['diagnostics.json', 'failure.png']);
  const report = JSON.parse(readFileSync(join(dir, 'diagnostics.json')));
  assert.ok(report.captureErrors.some(item => item.includes('20 MiB')));
  assert.match(report.error, /original failure/);
});

test('only four context traces can be retained', async t => {
  const first = fixture(t, { trace: true });
  const all = [first, ...Array.from({ length: 4 }, () => fixture(t))];
  for (const item of all) await first.evidence.attach(item.context);
  const dir = await first.evidence.finish({ failed: true });
  assert.deepEqual(all.map(item => item.calls.start), [1, 1, 1, 1, 0]);
  assert.equal(readdirSync(dir).filter(name => name.endsWith('.zip')).length, 4);
});

test('unwritable artifact root cannot replace a test failure', async t => {
  const { root, context } = fixture(t);
  const blocker = join(root, 'file-not-directory'); writeFileSync(blocker, 'occupied');
  const evidence = createBrowserEvidence({ suite: 'blocked', artifactRoot: blocker, trace: true });
  await evidence.attach(context);
  assert.equal(await evidence.finish({ error: new Error('original failure') }), null);
});

test('incomplete captures are removed when a browser disconnects during export', async t => {
  const { evidence, context, page } = fixture(t, { trace: true });
  page.screenshot = async ({ path }) => { writeFileSync(path, 'incomplete'); throw new Error('screenshot disconnected'); };
  context.tracing.stop = async ({ path }) => { writeFileSync(path, 'incomplete'); throw new Error('trace disconnected'); };
  await evidence.attach(context);
  const dir = await evidence.finish({ failed: true });
  assert.deepEqual(readdirSync(dir), ['diagnostics.json']);
});
