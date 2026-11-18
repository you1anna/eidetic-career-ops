#!/usr/bin/env node
// Synthetic JavaScript lookup costs, not DOM/rendering or production latency.
// node --expose-gc tools/benchmark_browser_lookups.mjs --baseline <git-revision>
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createContactRouteLookup } from '../workspace/js/job-evidence.js';
import { createOutreachViews } from '../workspace/js/outreach.js';

const args = process.argv.slice(2);
assert.equal(args.length, 2, 'Usage: node --expose-gc tools/benchmark_browser_lookups.mjs --baseline <git-revision>');
assert.equal(args[0], '--baseline');
const cwd = fileURLToPath(new URL('..', import.meta.url));
const revision = execFileSync('git', ['rev-parse', '--verify', '--end-of-options', `${args[1]}^{commit}`], { cwd, encoding: 'utf8' }).trim();
const directory = mkdtempSync(join(tmpdir(), 'wo-browser-lookups-'));
try {
  // Read committed browser source only; no database, generated views or live records.
  execFileSync('tar', ['-x', '-C', directory], {
    input: execFileSync('git', ['archive', revision, 'workspace/js'], { cwd }),
  });
  writeFileSync(join(directory, 'package.json'), '{"type":"module"}\n');
  const baselineRoutes = await import(pathToFileURL(join(directory, 'workspace/js/job-evidence.js')));
  const baselineActions = await import(pathToFileURL(join(directory, 'workspace/js/outreach.js')));
  const count = 3000, repetitions = 5;
  const jobs = Array.from({ length: count }, (_, i) => ({ id: `J${i}`, name: `Job ${i}`, type: 'job' }));
  const people = Array.from({ length: count }, (_, i) => ({ id: `P${i}`, name: `Person ${i}`, type: 'person' }));
  const state = {
    records: [{ id: 'C', type: 'company', name: 'Synthetic Employer' }, ...jobs, ...people],
    links: jobs.map((job, i) => ({ from_id: job.id, to_id: people[i].id, relation: 'related' })),
    intelligence: { actions: jobs.map((job, i) => ({ id: `A${i}`, record_id: job.id })), research: [] },
  };
  const byId = new Map(state.records.map(record => [record.id, record]));
  const options = { getState: () => state, getRecord: id => byId.get(id) };
  const checks = {
    routesBefore: () => {
      const lookup = baselineRoutes.createContactRouteLookup?.(options);
      return job => lookup ? lookup.forRecord(job) : baselineRoutes.contactRoutes(job, state);
    },
    routesAfter: () => createContactRouteLookup(options).forRecord,
    actionsBefore: () => baselineActions.createOutreachViews(options).forRecord,
    actionsAfter: () => createOutreachViews(options).forRecord,
  };
  const report = { baseline: revision, records: state.records.length, links: state.links.length, actions: count, repetitions, median_ms: {} };
  for (const [name, createLookup] of Object.entries(checks)) {
    const times = [];
    for (let iteration = 0; iteration < repetitions; iteration++) {
      global.gc?.();
      const started = performance.now(), lookup = createLookup();
      const results = jobs.map(lookup);
      times.push(performance.now() - started);
      // Independently pinned complete identities, not a checksum derived from the other implementation.
      for (let i = 0; i < count; i++) {
        assert.equal(results[i].length, 1);
        if (name.startsWith('routes')) {
          assert.equal(results[i][0].person.id, `P${i}`);
          assert.deepEqual([...results[i][0].bases], ['Linked to this vacancy']);
          assert.equal(results[i][0].direct, true);
        } else assert.equal(results[i][0].id, `A${i}`);
      }
    }
    report.median_ms[name] = Number(times.sort((a, b) => a - b)[Math.floor(repetitions / 2)].toFixed(2));
  }
  if (global.gc) {
    global.gc();
    const before = process.memoryUsage().heapUsed;
    const contacts = createContactRouteLookup(options), outreach = createOutreachViews(options);
    contacts.index(); outreach.forRecord(jobs[0]); global.gc();
    report.retained_index_mib = Number(((process.memoryUsage().heapUsed - before) / 1024 / 1024).toFixed(2));
    // Keep the measured indexes reachable through the second heap reading.
    assert.equal(contacts.index().records.size, state.records.length);
    assert.equal(outreach.forRecord(jobs[0])[0].id, 'A0');
  }
  console.log(JSON.stringify(report, null, 2));
} finally {
  rmSync(directory, { recursive: true, force: true });
}
