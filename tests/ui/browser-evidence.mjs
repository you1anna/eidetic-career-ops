// Diagnostic evidence only: the calling suite owns pass/fail and expected negative paths.
// Keep browser launch, assertions and fixture ownership in the existing harness.
import { chmodSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const clip = (value, limit = 1000) => String(value ?? '').slice(0, limit);
const label = value => clip(value, 48).replace(/[^a-zA-Z0-9_-]/g, '-') || 'browser';
function safeUrl(value) {
  try { const url = new URL(value); return clip(`${url.origin}${url.pathname}`); }
  catch { return '[unavailable URL]'; }
}

export function createBrowserEvidence({ suite, browser, artifactRoot = process.env.WO_UI_ARTIFACT_DIR,
  trace = process.env.WO_UI_TRACE === '1' } = {}) {
  const events = [], captureErrors = [], listeners = [], contexts = new Set(), tracing = [], pages = [];
  let finished, retainedBytes = 0;
  const note = error => { if (captureErrors.length < 20) captureErrors.push(clip(error?.message ?? error)); };
  const record = event => { events.push(event); if (events.length > 100) events.shift(); };
  const listen = (target, event, handler) => { target.on(event, handler); listeners.push(() => target.off(event, handler)); };
  function observe(page) {
    if (pages.includes(page)) return;
    pages.push(page);
    listen(page, 'pageerror', error => record({ kind: 'pageerror', text: clip(error.message) }));
    listen(page, 'console', message => {
      if (['error', 'warning'].includes(message.type())) record({ kind: 'console', text: clip(message.text()) });
    });
    listen(page, 'requestfailed', request => record({ kind: 'requestfailed', method: clip(request.method()),
      url: safeUrl(request.url()), text: clip(request.failure()?.errorText) }));
    listen(page, 'response', response => {
      if (response.status() >= 400) record({ kind: 'http', method: clip(response.request().method()),
        url: safeUrl(response.url()), status: response.status() });
    });
  }
  async function attach(context) {
    if (contexts.has(context)) return;
    contexts.add(context);
    context.pages().forEach(observe);
    listen(context, 'page', observe);
    if (trace && tracing.length < 4) {
      try { await context.tracing.start({ screenshots: true, snapshots: true }); tracing.push(context); }
      catch (error) { note(error); }
    } else if (trace) note('Trace context limit reached (4); later contexts are not recorded.');
  }
  // Captures are bounded to one viewport image and four traces, totalling at most 20 MiB.
  // Tracing itself is opt-in and has collection cost even when a passing trace is discarded.
  function retain(path) {
    const bytes = statSync(path).size;
    if (retainedBytes + bytes > 20 * 1024 * 1024) {
      rmSync(path, { force: true });
      note('Capture omitted because the 20 MiB retained-artifact limit was exceeded.');
    } else { retainedBytes += bytes; chmodSync(path, 0o600); }
  }
  async function capture(path, operation) {
    try { await operation(path); retain(path); }
    catch (error) {
      note(error);
      try { rmSync(path, { force: true }); } catch (removeError) { note(removeError); }
    }
  }
  async function save({ failed = false, error } = {}) {
    for (const remove of listeners) remove();
    let directory = null;
    if (failed || error) {
      try {
        const root = artifactRoot || tmpdir();
        mkdirSync(root, { recursive: true, mode: 0o700 });
        directory = mkdtempSync(join(root, `${label(suite)}-${label(browser)}-`));
        chmodSync(directory, 0o700);
      } catch (captureError) { note(captureError); }
    }
    const page = pages.findLast(candidate => !candidate.isClosed());
    const report = { suite, browser, error: error ? clip(error.stack || error, 4000) : null,
      url: null, events, captureErrors };
    if (directory && page) {
      try { report.url = safeUrl(page.url()); } catch (captureError) { note(captureError); }
      try { report.visibleText = clip(await page.locator('body').innerText({ timeout: 1000 }), 1600); }
      catch (captureError) { note(captureError); }
      await capture(join(directory, 'failure.png'), path => page.screenshot({ path, fullPage: false, timeout: 2000 }));
    }
    for (const [index, context] of tracing.entries()) {
      try {
        if (directory) {
          await capture(join(directory, `trace-${index + 1}.zip`), path => context.tracing.stop({ path }));
        } else await context.tracing.stop();
      } catch (captureError) { note(captureError); }
    }
    if (directory) {
      try {
        writeFileSync(join(directory, 'diagnostics.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
        console.error(`Browser failure evidence: ${directory}`);
      } catch (captureError) { note(captureError); console.error(`Browser evidence could not be saved: ${clip(captureError.message)}`); }
    } else if (captureErrors.length) console.error(`Browser evidence unavailable: ${captureErrors.join('; ')}`);
    return directory;
  }
  return {
    attach,
    // Never let diagnostic I/O replace the assertion/exception that the suite is reporting.
    finish(options) {
      return finished ??= save(options).catch(error => {
        console.error(`Browser evidence unavailable: ${clip(error.message)}`);
        return null;
      });
    },
  };
}
