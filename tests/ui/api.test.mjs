import test from 'node:test';
import assert from 'node:assert/strict';
import { api, onSignedOut, SIGNED_OUT, UNREACHABLE } from '../../workspace/js/api.js';

// Swap the global fetch for one test, and always put it back.
async function withFetch(fake, run) {
  const real = globalThis.fetch;
  globalThis.fetch = fake;
  try { await run(); } finally { globalThis.fetch = real; }
}

const reply = (status, body) => async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

test('a request that never reached the site says what to do, not the browser wording', async () => {
  for (const wording of ['Failed to fetch', 'Load failed', 'NetworkError when attempting to fetch resource.']) {
    await withFetch(async () => { throw new TypeError(wording); }, async () => {
      await assert.rejects(api('/api/records/CP-1/draft', { method: 'POST' }), error => {
        assert.equal(error.message, UNREACHABLE);
        assert.equal(error.cause.message, wording);
        return true;
      });
    });
  }
  assert.match(UNREACHABLE, /could not be reached/);
  assert.match(UNREACHABLE, /sign in again/);
});

test('an aborted request is passed through unchanged', async () => {
  const abort = new DOMException('The operation was aborted.', 'AbortError');
  await withFetch(async () => { throw abort; }, async () => {
    await assert.rejects(api('/api/state'), error => error === abort);
  });
});

test('a refusal from the site keeps its own message', async () => {
  await withFetch(reply(400, { error: 'Text is required' }), async () => {
    await assert.rejects(api('/api/records/CP-1/draft', { method: 'POST' }), { message: 'Text is required' });
  });
  await withFetch(reply(409, {}), async () => {
    await assert.rejects(api('/api/records/CP-1/draft', { method: 'POST' }), { message: /changed elsewhere/ });
  });
  await withFetch(reply(200, { ok: true }), async () => {
    assert.deepEqual(await api('/api/state'), { ok: true });
  });
});

// Cloudflare Access answers an ended sign-in with a redirect to its login page. In the browser,
// redirect: 'manual' returns that as an opaque redirect (status 0) instead of following it into
// the page's connect-src 'self' policy, which blocked it as "Failed to fetch" on 23 October.
test('an ended sign-in is reported as signed out, not as an unreachable site', async () => {
  const seen = [];
  const stop = onSignedOut(() => seen.push('signed out'));
  const opaque = { type: 'opaqueredirect', status: 0, ok: false, json: async () => { throw new SyntaxError('no body'); } };
  try {
    let options;
    await withFetch(async (_path, given) => { options = given; return opaque; }, async () => {
      await assert.rejects(api('/api/records/CP-1/draft', { method: 'POST', body: '{}' }), error => {
        assert.equal(error.message, SIGNED_OUT);
        assert.equal(error.signedOut, true);
        return true;
      });
    });
    assert.equal(options.redirect, 'manual');
    assert.equal(options.method, 'POST');
    // Outside a browser the redirect itself comes back; the site never redirects /api, so it means the same.
    await withFetch(async () => new Response(null, { status: 302, headers: { Location: 'https://example.cloudflareaccess.com/' } }), async () => {
      await assert.rejects(api('/api/state'), { message: SIGNED_OUT });
    });
    assert.deepEqual(seen, ['signed out', 'signed out']);
  } finally { stop(); }
  assert.match(SIGNED_OUT, /sign-in has ended/);
  assert.match(SIGNED_OUT, /new tab/);
});
