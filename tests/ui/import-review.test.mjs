import test from 'node:test';
import assert from 'node:assert/strict';
import { createImportReview } from '../../workspace/js/import-review.js';

// Minimal DOM for exercising the complete card renderer without a browser dependency.
class Element {
  constructor(tag) { this.tagName = tag; this.childNodes = []; }
  append(...children) { this.childNodes.push(...children); }
}
function render(url) {
  const priorDocument = globalThis.document, priorNode = globalThis.Node;
  globalThis.document = { createElement: tag => new Element(tag) };
  globalThis.Node = Element;
  try {
    const container = new Element('main');
    createImportReview({ getState: () => ({ review_queue: [{
      kind: 'linkedin_person', payload: { name: 'Synthetic Candidate', evidence: {}, url },
    }] }), factualDate: x => x }).render(container);
    const flatten = node => [node, ...(node.childNodes || []).flatMap(flatten)];
    return flatten(container);
  } finally { globalThis.document = priorDocument; globalThis.Node = priorNode; }
}

test('imported profile URLs cannot create executable or local-file links', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,hello', 'file:///tmp/private',
    'JaVaScRiPt:alert(1)', 'java\nscript:alert(1)', '/relative', 'not a URL']) {
    const nodes = render(url);
    assert.equal(nodes.filter(n => n.tagName === 'a').length, 0, url);
    assert.ok(nodes.some(n => n.textContent === 'LinkedIn profile ↗'), url);
  }
});

test('ordinary imported web profiles retain safe new-tab navigation', () => {
  for (const url of ['https://www.linkedin.com/in/synthetic', 'http://example.invalid/profile']) {
    const links = render(url).filter(n => n.tagName === 'a');
    assert.equal(links.length, 1);
    assert.equal(links[0].href, url);
    assert.equal(links[0].target, '_blank');
    assert.equal(links[0].rel, 'noopener noreferrer');
  }
});
