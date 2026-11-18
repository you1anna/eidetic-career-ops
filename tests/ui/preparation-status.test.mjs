// Pure tests for the weekly preparation summary and route provenance: no browser or database.
import assert from 'node:assert/strict';
import test from 'node:test';
import { preparationSummary } from '../../workspace/js/preparation-status.js';
import { routeProvenance } from '../../workspace/js/job-evidence.js';

test('the summary names every non-empty state in a fixed order', () => {
  assert.equal(preparationSummary({ counts: { needs_information: 1, ready: 2, budget_waiting: 1, route_unverified: 1 } }),
    '2 ready to review · 1 route unverified · 1 needs information · 1 waiting for allowance');
  assert.equal(preparationSummary({ counts: {} }), '');
  assert.equal(preparationSummary(null), '');
});

test('only a public read follows the public freshness rule', () => {
  assert.equal(routeProvenance({ provenance: 'public_read', source_url: 'https://example.test/team' }), 'public');
  assert.equal(routeProvenance({ provenance: 'owner_confirmed', source_url: 'https://mail.google.com/mail/u/0/#all/x' }), 'owner_confirmed');
  assert.equal(routeProvenance({ provenance: 'prior_correspondence', source_url: 'https://mail.google.com/mail/u/0/#all/x' }), 'prior_correspondence');
  // Legacy checks without provenance: a private mailbox link was never a public read.
  assert.equal(routeProvenance({ source_url: 'https://mail.google.com/mail/u/0/#all/x' }), 'private');
  assert.equal(routeProvenance({ source_url: 'https://example.test/team' }), 'public');
});
