// The Dashboard: what needs the owner, where the job hunt stands and how the site is running.
// It reads the loaded summary and `/api/status` for everything those already carry, and
// `/api/dashboard` (server-only figures) only while this tab is open. Counts link to the pages
// that hold the items; nothing here writes or offers a new action.
import { decisionsForYou } from './decisions.js';
import { researchDecisionPanel, createIntakeComparison } from './dashboard-decisions.js';
import { E, A, typeName } from './dom.js';
import { JOB_STATUS_NAMES, OUTCOME_NAMES } from './labels.js';
import { stamp } from './record-presentation.js';
import { freshnessLine } from './site-status.js';
import { caretakerStatusCard } from './caretaker-status.js';
import { emailStatusCard } from './email-status.js';
import { researchStatusCard, researchCount } from './research-status.js';
import { peopleContext } from './people-context.js';
import { bestOpportunities } from './week.js';
import { replaceKeepingOpen } from './status-refresh.js';

const MIB = 1024 * 1024, GIB = 1024 * MIB;
export const bytes = b => typeof b !== 'number' ? 'unknown' : b >= GIB ? `${(b / GIB).toFixed(1)} GiB`
  : b >= MIB ? `${Math.round(b / MIB)} MiB` : `${Math.round(b / 1024)} KiB`;
const whole = n => n.toLocaleString('en-GB');
const ms = v => typeof v !== 'number' ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
const STAGES_SHOWN = ['lead', 'contacted', 'conversation', 'proposal'];
const RECENT_JOB = ['applied', 'responded', 'interview', 'offer', 'rejected', 'closed'];
const AWAITING = ['queued', 'working', 'blocked', ''];
const DEFERRAL_REASONS = {
  working_limit: 'working limit',
  seven_day_limit: 'seven-day limit',
  unresolved_stale: 'unresolved and stale',
  not_recommended: 'not recommended',
};
// Which sections each source feeds, so an update rebuilds only those. Server figures arrive just
// after the tab opens; rebuilding Needs attention then would swap a card out from under a tap.
export const SERVER_SECTIONS = ['figures', 'hunt', 'capacity', 'quotas', 'speed'];
export const STATUS_SECTIONS = ['attention', 'hunt', 'services', 'admin'];
export const ORDER = ['hunt', 'attention', 'figures', 'services', 'admin', 'capacity', 'quotas', 'speed'];

export function applicationRows(records = [], recent = null) {
  const counts = Object.fromEntries(RECENT_JOB.map(s => [s, records.filter(r => r.type === 'job' && r.status === s).length]));
  return RECENT_JOB.map(s => [
    JOB_STATUS_NAMES[s] || typeName(s),
    String(counts[s] || 0),
    recent?.state === 'ok' ? String(recent.job_status?.[s] || 0) : '…',
  ]);
}

export function stageRows(records = []) {
  return STAGES_SHOWN.map(s => [typeName(s), String(records.filter(r => r.type === 'opportunity' && r.stage === s).length)]);
}

export function deferralSummary(deferred = {}) {
  return Object.entries(deferred || {})
    .filter(([, n]) => Number.isSafeInteger(n) && n > 0)
    .map(([k, n]) => `${n} ${DEFERRAL_REASONS[k] || k.replaceAll('_', ' ')}`)
    .join(' · ');
}

// Severity by share of a limit: under the warning share, then warning, then 95% and over.
export function level(used, limit, warnAt = 0.8) {
  if (typeof used !== 'number' || typeof limit !== 'number' || limit <= 0) return 'unknown';
  const share = used / limit;
  return share >= 0.95 ? 'danger' : share >= warnAt ? 'warn' : 'ok';
}

function meter(label, used, limit, { format = whole, warnAt = 0.8, target = null, note = '' } = {}) {
  const severity = level(used, limit, warnAt);
  const share = severity === 'unknown' ? '' : ` (${Math.round(used / limit * 100)}%)`;
  const row = E('div', null, 'meter');
  row.dataset.level = severity;
  row.append(E('span', `${label}: ${typeof used === 'number' ? format(used) : 'unknown'} of ${typeof limit === 'number' ? format(limit) : 'unknown'}${share}${severity === 'danger' ? ' · near the limit' : severity === 'warn' ? ' · high' : ''}${note}`, 'meter-label'));
  const track = E('span', null, 'meter-track');
  track.setAttribute('aria-hidden', 'true');  // the label above carries the figures
  if (severity !== 'unknown') {
    const fill = E('span', null, 'meter-fill');
    fill.style.width = `${Math.min(100, used / limit * 100)}%`;
    track.append(fill);
  }
  if (target) {
    const mark = E('span', null, 'meter-target');
    mark.style.left = `${target * 100}%`;
    track.append(mark);
  }
  return A(row, track);
}

function tile(label, value, open, { note = '', toggle = false } = {}) {
  const node = E(open ? 'button' : 'div', null, `stat-tile${open ? '' : ' stat-static'}${toggle ? ' stat-toggle' : ''}`);
  if (open) { node.type = 'button'; node.onclick = open; }
  const numeric = typeof value === 'number' ? value : Number.parseInt(String(value), 10);
  if (Number.isFinite(numeric) && numeric > 0) node.dataset.active = 'true';
  const head = A(E('span', null, 'stat-head'), E('span', label, 'stat-label'));
  if (open) {
    const icon = E('span', toggle ? '▾' : '→', 'stat-affordance');
    icon.setAttribute('aria-hidden', 'true');
    head.append(icon);
  }
  node.append(head, E('strong', value, 'stat-value'));
  if (note) node.append(E('span', note, 'stat-note muted'));
  return node;
}

function heading(title, count) {
  const head = A(E('div', null, 'section-head'), E('h2', title));
  if (count !== undefined) head.append(E('span', count, 'count'));
  return head;
}

const section = (key, title, count) => A(E('section', null, `dashboard-section dashboard-${key}`), heading(title, count));
const when = at => new Date(at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

// One line for the server-only figures: loading, last good update, or a failed read with Retry.
// Earlier figures stay on screen after a failure, labelled with the time they were read.
export function serverFiguresNote(data, { loading = false, error = null, updatedAt = 0 } = {}) {
  if (loading && !error) return { text: data ? `Refreshing server figures (last updated ${when(updatedAt)})…` : 'Loading server figures…', level: 'muted' };
  if (error && data) return { text: `Showing server figures from ${when(updatedAt)}: the latest refresh failed. ${error}`, level: 'warn', retry: true };
  if (error) return { text: `Server figures could not be loaded. ${error}`, level: 'warn', retry: true };
  if (data) return { text: `Server figures updated ${when(updatedAt)}.`, level: 'muted' };
  return { text: 'Loading server figures…', level: 'muted' };
}

function table(head, rows, { numericFrom = 1 } = {}) {
  const t = E('table', null, 'dashboard-table');
  t.append(A(E('thead'), A(E('tr'), ...head.map((h, n) => E('th', h, n >= numericFrom ? 'num' : '')))));
  t.append(A(E('tbody'), ...rows.map(r => A(E('tr'), ...r.map((c, n) => E('td', c, n >= numericFrom ? 'num' : ''))))));
  return A(E('div', null, 'table-scroll'), t);
}

export function createDashboardView({ emailLeads, getState, getData, getMeta = () => ({}), getProblems, decisionsIn, decisionButton,
  excludedDecision, go, openWeek, openJobs, openRecord, getContactIndex }) {
  const comparison = createIntakeComparison({openRecord});
  const toWeek = group => () => (openWeek ? openWeek(group) : go('week'));
  // A section without server figures says why: still loading, or the read failed (the note above says how).
  const unavailable = part => {
    if (part?.state === 'unknown') return E('p', `Unknown: the server could not read this (${part.reason}).`, 'muted');
    return E('p', getMeta().error ? 'Unavailable: the server figures could not be loaded (see above).' : 'Loading…', 'muted');
  };
  function figuresNote(data) {
    const meta = getMeta(), note = serverFiguresNote(data, meta);
    const line = E('p', note.text, `dashboard-figures ${note.level}`);
    line.setAttribute('role', 'status');
    if (note.retry && meta.retry) {
      const retry = E('button', 'Retry', 'notice-action'); retry.type = 'button'; retry.onclick = meta.retry;
      line.append(' ', retry);
    }
    return line;
  }
  function attention() {
    const { problems, gaps } = getProblems();
    const box = section('attention', 'Needs attention');
    const choices = decisionsForYou(getState().decisions, excludedDecision);
    const repairs = problems.filter(p => p.kind !== 'decision');
    const choiceBox = A(E('div', null, 'dashboard-choices'), E('h3', `Your decisions (${choices.length})`));
    choiceBox.id = 'dashboard-decisions';
    if (!choices.length) choiceBox.append(E('p', 'No unanswered choices.', 'muted'));
    for (const decision of choices) choiceBox.append(decisionButton(decision));
    box.append(choiceBox);
    if (!repairs.length) box.append(E('p', 'No other repair or check needs attention.', 'muted'));
    else box.append(E('h3', 'Repairs and checks'), E('p', 'These need investigation or recovery; no choice is requested from you here.', 'muted'));
    for (const p of repairs) {
      const item = A(E('div', null, 'dash-problem'), E('strong', p.title), p.detail ? E('p', p.detail, 'muted') : null);
      if (p.records?.length && openRecord) {
        const links = A(E('p', null, 'dash-problem-links'), E('span', 'Affected: ', 'muted'));
        p.records.forEach((r, i) => {
          if (i) links.append(' · ');
          const btn = E('button', r.name || r.id, 'linkish');
          btn.type = 'button';
          btn.onclick = () => openRecord(r.id);
          links.append(btn);
        });
        item.append(links);
      }
      box.append(item);
    }
    if (gaps.length) {
      const folded = A(E('details', null, 'dashboard-gaps'), E('summary', `Known coverage gaps (${gaps.length}), not counted`));
      folded.append(table(['Check / source', 'Status', 'Detail'],
        gaps.map(g => [g.label || g.title, g.status || 'partial', g.detail || '—']),
        { numericFrom: 99 }));
      box.append(folded);
    }
    return box;
  }

  function hunt(data) {
    const state = getState(), sel = state.opportunities || {}, records = state.records || [];
    const box = section('hunt', 'Job hunt');
    const choices = decisionsForYou(state.decisions, excludedDecision);
    const awaiting = decisionsIn('answered').filter(d => d.area !== 'operations' && AWAITING.includes(d.execution_status || ''));
    const awaitingBox = A(E('details', null, 'dashboard-awaiting'),
      E('summary', `Answered, awaiting Claude (${awaiting.length})`));
    if (!awaiting.length) {
      awaitingBox.append(E('p', 'No answered decisions are waiting on Claude.', 'muted'));
    } else {
      const now = Date.now();
      for (const d of awaiting) {
        const days = d.answered_at ? Math.floor((now - Date.parse(d.answered_at)) / 864e5) : null;
        awaitingBox.append(decisionButton(d),
          E('p', `${days === null ? 'Answered' : `Answered ${days} day${days === 1 ? '' : 's'} ago`} · Claude: ${d.execution_status || 'not started'}${d.claimed_at ? ` since ${stamp(d.claimed_at)}` : ''}.`, 'muted dash-age'));
      }
    }
    const slot = E('div'); slot.dataset.intakeComparisonSlot = '';
    box.append(A(E('div', null, 'stat-row'),
      tile('Act now', sel.act_now?.length ?? 0, toWeek('act-now')),
      tile('Waiting on others', sel.waiting?.length ?? 0, toWeek('waiting')),
      tile('In progress', sel.in_progress?.length ?? 0, toWeek('in-progress')),
      tile('Shortlisted', sel.shortlisted?.length ?? 0, () => openJobs('shortlisted')),
      tile('Active processes', sel.active?.length ?? 0, () => openJobs('active')),
      tile('Best opportunities', bestOpportunities(state).length, toWeek('discovery')),
      tile('Decisions for you', choices.length, () => document.getElementById('dashboard-decisions')?.scrollIntoView({block:'start'})),
      tile('Answered, awaiting Claude', awaiting.length, () => {
        awaitingBox.open = !awaitingBox.open;
        if (awaitingBox.open) awaitingBox.scrollIntoView({ block: 'nearest' });
      }, { toggle: true })),
      awaitingBox);

    const replies = A(E('section', null, 'dashboard-panel dashboard-replies'));
    if (!state.intelligence || state.intelligence_error) {
      replies.append(E('h3', 'Outstanding replies'), E('p', 'Outstanding replies are unavailable until the current workspace assessment loads.', 'muted'));
    } else {
      const context = peopleContext(state, new Date(), getContactIndex?.());
      const pending = context.queue.filter(person => context.byId.get(person.id).action.key === 'needs_reply');
      replies.append(E('h3', `Outstanding replies (${pending.length})`));
      if (!pending.length) replies.append(E('p', 'No outstanding replies recorded.', 'muted'));
      for (const person of pending) {
        const {item, action} = context.byId.get(person.id);
        const link = E('button', person.name, 'linkish'); link.type = 'button';
        link.onclick = () => openRecord(person.id, true, item.id);
        replies.append(A(E('div', null, 'dashboard-reply'), link, E('p', action.next, 'muted')));
      }
    }
    box.append(replies, researchDecisionPanel(state, openRecord), slot);

    const recent = data?.hunt;
    const appsPanel = A(E('div', null, 'dashboard-panel'),
      E('h3', 'Applications & pipeline'),
      table(['Application status', 'Now', recent?.state === 'ok' ? `Moved (${recent.days}d)` : 'Moved (30d)'],
        applicationRows(records, recent)));
    if (recent && recent.state !== 'ok' || getMeta().error) appsPanel.append(unavailable(recent));
    appsPanel.append(E('h4', 'Opportunities by stage'), table(['Stage', 'Active'], stageRows(records)));

    const allTime = state.intelligence?.outcomes_summary?.counts || {};
    const kinds = [...new Set([...Object.keys(allTime), ...Object.keys(recent?.outcomes || {})])];
    const outcomesPanel = A(E('div', null, 'dashboard-panel'), E('h3', 'Recorded outcomes'));
    if (!kinds.length) outcomesPanel.append(E('p', 'No outcomes recorded yet.', 'muted'));
    else outcomesPanel.append(table(['Outcome', recent?.state === 'ok' ? `Last ${recent.days} days` : 'Last 30 days', 'All time'],
      kinds.map(k => [OUTCOME_NAMES[k] || typeName(k), recent?.state === 'ok' ? String(recent.outcomes[k] || 0) : '…', String(allTime[k] || 0)])));
    box.append(A(E('div', null, 'dashboard-split'), appsPanel, outcomesPanel));

    const supplyPanel = A(E('div', null, 'dashboard-panel'), E('h3', 'Intake allowance'));
    const policy = state.discovery_capacity;
    if (recent?.state === 'ok' && policy)
      supplyPanel.append(meter('Added automatically, last 7 days', recent.admissions_seven_days, policy.additions_per_seven_days));
    else supplyPanel.append(unavailable(recent));
    const deferredMap = state.email_processing?.last_import?.deferred || {};
    if (deferralSummary(deferredMap)) supplyPanel.append(E('p', `Last alert import: ${deferralSummary(deferredMap)}. These counts do not measure opportunity quality.`, 'muted'));
    // Keep the comparison form/result node alive across automatic server/status refreshes.
    box.append(supplyPanel);
    return box;
  }

  function services() {
    const state = getState();
    const box = section('services', 'Site functions');
    const email = emailStatusCard(state.email_processing);
    const slot = E('div'); slot.dataset.emailLeadsSlot = '';
    email.append(slot);
    box.append(freshnessLine(state.freshness), caretakerStatusCard(state.caretaker_processing),
      A(E('div', null, 'run-status-block'), email,
        researchStatusCard(state.research_processing, { records: state.records, openRecord })));
    return box;
  }

  function admin() {
    const now = Date.now();
    const box = section('admin', 'Open admin items');
    const items = decisionsIn('answered').filter(d => d.area === 'operations' && AWAITING.includes(d.execution_status || ''));
    if (!items.length) box.append(E('p', 'No approved site or security step is waiting.', 'muted'));
    for (const d of items) {
      const days = d.answered_at ? Math.floor((now - Date.parse(d.answered_at)) / 864e5) : null;
      box.append(decisionButton(d), E('p', `${days === null ? 'Answered' : `Answered ${days} day${days === 1 ? '' : 's'} ago`} · Claude: ${d.execution_status || 'not started'}${d.claimed_at ? ` since ${stamp(d.claimed_at)}` : ''}.`, 'muted dash-age'));
    }
    return box;
  }

  function speed(data) {
    const box = section('speed', 'Responsiveness');
    const r = data?.responsiveness;
    if (!r) return A(box, unavailable(null));
    // Each refresh is counted by why the page asked, so background polls never stand in for saves.
    const rows = [['Saves', r.kinds.save], ['Refresh after a save', r.kinds.refresh_after_save],
      ['Background refreshes', r.kinds.refresh_background], ['Other refreshes', r.kinds.refresh_other],
      ['Full page loads', r.kinds.summary_full], ['Server rebuilds (any cause)', r.kinds.build]];
    box.append(table(['Since the last restart', 'Count', 'Median', '90th percentile', 'Slowest'],
      rows.map(([name, k]) => [name, String(k?.count || 0), ms(k?.median_ms), ms(k?.p90_ms), ms(k?.max_ms)])));
    const phases = r.kinds.build?.phase_median_ms;
    if (phases) box.append(E('p', `Rebuild split (median): ${Object.entries(phases).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${ms(v)}`).join(' · ')}.`, 'muted'));
    box.append(E('p', `Kept in the server's memory only, last ${r.per_kind} of each, since ${stamp(r.since)}. A deploy clears it.`, 'muted'));
    return box;
  }

  function capacity(data) {
    const box = section('capacity', 'Capacity and headroom');
    const c = data?.capacity;
    if (c?.state !== 'ok') return A(box, unavailable(c));
    const latest = c.scans[0];
    if (latest) {
      const target = 1 - c.headroom_target;
      box.append(E('p', `Daily scan, ${stamp(latest.finished_at)}. The line marks ${Math.round(target * 100)}%, leaving the ${Math.round(c.headroom_target * 100)}% headroom target.`, 'muted'),
        meter('Largest process (the scan or its largest child)', latest.largest_process_bytes, latest.limit_bytes, { format: bytes, warnAt: target, target }),
        meter('Service peak, including file cache', latest.cgroup_peak_bytes, latest.limit_bytes, { format: bytes, warnAt: target, target }),
        meter('Anonymous memory', latest.anon_bytes, latest.limit_bytes, { format: bytes, warnAt: target, target }));
      box.append(A(E('details', null, 'dashboard-scans'), E('summary', `Last ${c.scans.length} daily scans`),
        table(['Finished', 'Largest process', 'Service peak', 'Anonymous', 'Pressure events'],
          c.scans.map(s => [stamp(s.finished_at), bytes(s.largest_process_bytes), bytes(s.cgroup_peak_bytes), bytes(s.anon_bytes), s.pressure_events ?? 'unknown']))));
    } else box.append(E('p', 'No daily scan has recorded its memory yet.', 'muted'));
    box.append(meter('Mail archive', c.spool.bytes, c.spool.warn_bytes, { format: bytes, note: ` · ${c.spool.count ?? 'unknown'} of ${c.spool.warn_count} messages` }),
      meter('Disk used', c.disk.total_bytes - c.disk.free_bytes, c.disk.total_bytes, { format: bytes }),
      E('p', `Database ${bytes(c.database.bytes)} (${bytes(c.database.free_bytes)} reusable) · write-ahead log ${bytes(c.database.wal_bytes)}.`));
    return box;
  }

  function quotas(data) {
    const box = section('quotas', 'Quotas and usage');
    const q = data?.quotas;
    if (q?.state !== 'ok') return A(box, unavailable(q));
    const x = q.codex;
    box.append(E('h3', 'Codex invocations (London day, Monday week)'),
      meter('Research today', x.research_day, x.research_day_limit), meter('Research this week', x.research_week, x.research_week_limit),
      meter('All automation today', x.day, x.day_limit), meter('All automation this week', x.week, x.week_limit),
      E('p', 'Failed or unknown calls count. These are invocation limits, not a price.', 'muted'));
    const collection = q.collection;
    box.append(E('h3', 'Collection API calls (London day)'),
      E('p', 'Scanner, research and attended API reads share each provider’s daily limit. Email polling and Codex invocations are separate.', 'muted'));
    if (collection?.state !== 'ok') box.append(E('p', 'Collection accounting unavailable; usage is unknown and covered API reads are blocked.', 'warn'));
    else for (const [provider, f] of Object.entries(collection.providers)) {
      const group = E('details', null, 'quota-provider');
      group.append(E('summary', `${typeName(provider)}: ${f.used} of ${f.daily_limit} calls used or reserved`),
        E('p', `Scheduled collection: ${f.scheduled_used} of ${f.scheduled_target}. Pending/uncertain reservations: ${f.reserved}. Deferred scheduling decisions today: ${f.deferred}.`),
        E('p', `Last attempted check: ${f.last_attempt ? stamp(f.last_attempt) : 'not recorded'}. Last completed task: ${f.last_success ? stamp(f.last_success) : 'not recorded'}.`, 'muted'));
      if (f.hold_until && Date.parse(f.hold_until) > Date.now()) group.append(E('p', `Cutover/recovery hold until ${stamp(f.hold_until)}.`, 'warn'));
      if (collection.blocked_reason === 'clock-rewind') group.append(E('p', 'Collection paused: the accounting clock is ahead of the server clock.', 'warn'));
      if (f.account?.day) group.append(E('p', `Provider account allowance: ${f.account.day}/day, ${f.account.week}/week, ${f.account.month}/month (observed ${f.account.observed}). Other applications are outside this site's accounting.`, 'muted'));
      box.append(group);
    }
    box.append(E('p', `Research spend: ${q.spend}. The Codex route records invocations, not money.`, 'muted'));
    return box;
  }

  const builders = { attention, figures: figuresNote, hunt, services, admin, capacity, quotas, speed };
  function build(data = getData()) {
    const board = A(E('div', null, 'dashboard'), ...ORDER.map(key => builders[key](data)));
    board.querySelector('[data-intake-comparison-slot]').append(comparison.panel(getState().version));
    if (emailLeads) board.querySelector('[data-email-leads-slot]').append(emailLeads.panel());
    return board;
  }
  // Rebuild the named sections in place, leaving any whose content is unchanged untouched.
  function refresh(root, keys = ORDER) {
    const data = getData();
    for (const key of keys) {
      const old = root.querySelector(`:scope > .dashboard-${key}`), next = builders[key](data);
      if (old && !old.isEqualNode(next)) {
        // Keep this live form and its results during run-status refreshes.
        const panel = old.querySelector('[data-email-leads]'), focus = old.contains(document.activeElement) ? document.activeElement : null;
        replaceKeepingOpen(old, next);
        if (key === 'hunt') next.querySelector('[data-intake-comparison-slot]').append(comparison.panel(getState().version));
        if (panel) next.querySelector('[data-email-leads-slot]')?.append(panel);
        focus?.focus({preventScroll:true});
      }
    }
  }
  return { build, refresh, render: container => container.append(build()) };
}
