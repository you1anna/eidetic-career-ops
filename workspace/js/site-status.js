// One owner for "does the site need you?". The Dashboard lists these problems; This week and Jobs
// show their count in one line. Each run keeps its own outcome rule (email-, research- and
// caretaker-status.js); this module only gathers them, with no thresholds of its own.
import { E, A } from './dom.js';
import { stamp } from './record-presentation.js';
import { emailOutcome } from './email-status.js';
import { researchOutcome } from './research-status.js';
import { caretakerOutcome } from './caretaker-status.js';

// The rules the folded panels used before the Dashboard, matched in any case: the caretaker's own
// failed label is "Needs attention", which the earlier case-sensitive pattern never counted.
const RUN_ATTENTION = /needs attention|^failed|^blocked|check delivery|^not started/i;
const CARETAKER_ATTENTION = /needs attention|overdue/i;
// A daily job (ingest or backup) is overdue once its last run is more than 30 hours old.
const late = (at, now) => at && (now - new Date(at).valueOf()) / 36e5 > 30;

export function freshness(f = {}, now = Date.now()) {
  const at = f.ingest_at || f.last_success, failing = (f.failures || []).length, capped = (f.capped || []).length;
  if (!at) return { text: 'No source has been read yet.', warn: false, empty: true };
  const run = f.ingest_at
    ? `${late(f.ingest_at, now) ? 'Daily run overdue: last' : 'Last'} ${f.reused_pending ? 'import' : 'ingest'} ${stamp(at)} · ${f.reused_pending ? 'saved observations; ' : ''}${failing ? `${failing} source${failing === 1 ? '' : 's'} need attention` : f.reused_pending ? 'no new source requests' : 'all sources read'}${capped ? ` · ${capped} read partly (page/request caps)` : ''}${f.fetch?.recovered ? ` · ${f.fetch.recovered} request${f.fetch.recovered === 1 ? '' : 's'} recovered on retry` : ''}`
    : `Sources last read ${stamp(at)}; no daily run yet`;
  const backup = f.backup_at ? ` · ${late(f.backup_at, now) ? 'Nightly backup overdue: last' : 'Backup'} ${stamp(f.backup_at)}` : '';
  return { text: run + backup, warn: !!(failing || late(f.ingest_at, now) || late(f.backup_at, now)) };
}

export function freshnessLine(f, now = Date.now()) {
  const line = freshness(f, now);
  return E('p', line.text, 'freshness' + (line.empty ? ' muted' : line.warn ? ' warn' : ''));
}

// Problems need the owner or a repair; gaps are known partial coverage, listed but not counted.
export function siteProblems(state = {}, operations = [], now = Date.now()) {
  const problems = [], gaps = [];
  const subjects = new Set(operations.map(d => d.subject).filter(Boolean));
  const sources = new Map();
  for (const ch of state.coverage?.channels || []) {
    for (const s of ch.sources || []) if (s.scope) sources.set(s.scope, s);
  }
  for (const d of operations) problems.push({ kind: 'decision', title: d.question, decision: d });
  const fresh = freshness(state.freshness, now);
  if (fresh.warn) problems.push({ kind: 'freshness', title: fresh.text });
  const care = caretakerOutcome(state.caretaker_processing, now);
  if (CARETAKER_ATTENTION.test(care)) problems.push({ kind: 'caretaker', title: `Site caretaker · ${care}` });
  for (const c of state.caretaker_processing?.checks || []) {
    if (c.subject && subjects.has(c.subject)) continue;  // its decision card already holds it
    if (c.status === 'failed') problems.push({ kind: 'check', title: `${c.label} · failed`, detail: c.detail });
    else if (['partial', 'unknown'].includes(c.status)) {
      const src = c.id?.startsWith('source:') ? sources.get(c.id.slice(7)) : null;
      const label = src?.label || c.label;
      const detail = (c.status === 'partial' && /failure decision on the Dashboard/i.test(c.detail || ''))
        ? (src?.summary || 'Read partly (page cap); not a failure.')
        : (c.detail || src?.summary || '');
      gaps.push({ kind: 'check', label, status: c.status, title: `${label} · ${c.status}`, detail });
    }
  }
  for (const [name, outcome] of [['Job alert email processing', emailOutcome(state.email_processing, now)],
                                 ['Job and contact research', researchOutcome(state.research_processing, now)]])
    if (RUN_ATTENTION.test(outcome)) problems.push({ kind: 'run', title: `${name} · ${outcome}` });
  // Errors inside an otherwise completed research pass (3 November: a problem, no card).
  const recordById = new Map((state.records || []).map(r => [r.id, r]));
  const failedIssues = (state.research_processing?.issues || []).filter(i => i.outcome === 'failed');
  for (const f of state.research_processing?.item_failures || []) {
    const rawIds = f.ids?.length
      ? f.ids
      : failedIssues.filter(i => (i.reason || 'unknown') === f.reason && (i.where || null) === (f.where || null)).map(i => i.id).filter(Boolean);
    const records = [...new Set(rawIds)].map(id => ({ id, name: recordById.get(id)?.name || id }));
    const item = {
      kind: 'research_item',
      title: `Research: ${f.count} item${f.count === 1 ? '' : 's'} failed with ${f.reason}`,
      detail: f.where ? `Raised at ${f.where}. Earlier evidence is kept.` : 'Earlier evidence is kept.',
    };
    if (records.length) item.records = records;
    problems.push(item);
  }
  for (const [key, label] of [['relevance_error', 'Ranking'], ['intelligence_error', 'Work selection']])
    if (state[key]) problems.push({ kind: 'projection', title: `${label} unavailable`, detail: state[key] });
  return { problems, gaps };
}

export function siteSummaryLine({ problems }, href = '#dashboard') {
  const n = problems.length;
  const line = E('p', null, 'site-line' + (n ? ' warn' : ''));
  const link = E('a', n ? `Site: ${n} need${n === 1 ? 's' : ''} attention · Dashboard` : 'Site: all working · Dashboard');
  link.href = href;
  return A(line, link);
}
