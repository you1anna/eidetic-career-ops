// Pure presentation tests: no browser, database or network.
import assert from 'node:assert/strict';
import test from 'node:test';
import { statedDate, cvSuggestionText } from '../../workspace/js/record-presentation.js';

test('stated dates format full ISO dates and leave free text as written', () => {
  assert.equal(statedDate('2026-10-17T12:00:00Z'), '17 Oct 2026');
  for (const text of ['Aug 2026', '2026-08', 'last spring', '']) assert.equal(statedDate(text), text);
  assert.equal(statedDate(null), '');
});

test('CV explanations use the canonical title basis and retain the suggestion after a manual choice', () => {
  const cvs = {Manager:{title:'Engineering Manager',readiness:'ready'},Director:{title:'Director of Engineering / CTO',readiness:'draft'}};
  const route = {cv:'Director',selection:'manual',suggested_cv:'Manager',suggestion_basis:{kind:'title_rule',term:'Engineering Manager'}};
  const before = JSON.stringify({route,cvs});
  assert.equal(cvSuggestionText(route,cvs), 'Why suggested: Engineering Manager — title matches “Engineering Manager”. Check this title-based suggestion against the role.');
  assert.equal(cvSuggestionText({...route,suggestion_basis:{kind:'title_family',term:'Engineering Lead'}},cvs),
    'Why suggested: Engineering Manager — title or recorded role matches “Engineering Lead”. Check this title-based suggestion against the role.');
  assert.equal(cvSuggestionText({...route,suggested_cv:'Director',suggestion_basis:{kind:'title_rule',term:'CTO'}},cvs),
    'Why suggested: Director of Engineering / CTO — title matches “CTO”. Check this title-based suggestion against the role.');
  assert.equal(JSON.stringify({route,cvs}),before);
});

test('CV explanation never invents a match from a manual choice or missing basis', () => {
  const cvs={Manager:{title:'Engineering Manager'}};
  for(const route of [null,{cv:'Manager',selection:'manual'},
    {cv:'Manager',suggested_cv:null,suggestion_basis:null},
    {suggested_cv:'Manager',suggestion_basis:{kind:'unknown',term:'Manager'}},
    {suggested_cv:'Manager',suggestion_basis:{kind:'title_rule'}},
    {suggested_cv:'Missing',suggestion_basis:{kind:'title_rule',term:'Manager'}}])
    assert.equal(cvSuggestionText(route,cvs),'No title-based CV suggestion available.');
});
