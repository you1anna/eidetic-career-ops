import test from 'node:test';
import assert from 'node:assert/strict';
import { launchBrowser, seatbeltSandbox } from '../../tools/browser_launch.mjs';

test('a macOS agent seatbelt is the only sandbox that blocks a browser', () => {
  assert.equal(seatbeltSandbox('darwin', { CURSOR_SANDBOX: 'seatbelt' }), true);
  assert.equal(seatbeltSandbox('darwin', { CODEX_SANDBOX: 'seatbelt' }), true);
  assert.equal(seatbeltSandbox('darwin', {}), false);
  assert.equal(seatbeltSandbox('darwin', { CURSOR_SANDBOX: 'none' }), false);
  assert.equal(seatbeltSandbox('linux', { CURSOR_SANDBOX: 'seatbelt' }), false);
});

test('the seatbelt returns before Playwright starts the browser', async () => {
  if (process.platform !== 'darwin') return;
  const previous = process.env.CURSOR_SANDBOX;
  process.env.CURSOR_SANDBOX = 'seatbelt';
  let called = false;
  try {
    await assert.rejects(
      () => launchBrowser({ name: () => 'webkit', launch() { called = true; } }),
      /was not started/);
    assert.equal(called, false);
  } finally {
    if (previous === undefined) delete process.env.CURSOR_SANDBOX;
    else process.env.CURSOR_SANDBOX = previous;
  }
});
