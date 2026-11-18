// Read-only People selection. Recorded progress owns responsibility; shared contact routes
// own role connections. Neither the queue nor elapsed time changes stored state.
import { contactRouteIndex, contactRoutes, relevantAgencyRole } from './job-evidence.js';
import { PERSON_TERMINAL, queueKey } from './machines.js';

const DAY = 86400000;
const ZONED = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const LONDON = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit' });
const RECORDED_DAY = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', day: 'numeric', month: 'short', year: 'numeric' });
const TERMINAL = new Set(PERSON_TERMINAL);
const ORDER = { needs_reply: 0, next_step: 1, waiting: 2, act_now: 3, priority: 4 };
const blocked = r => !r || r.archived_at || r.exclusion || r.exclusion_contexts?.length
  || r.relationship === 'do_not_contact' || r.relevance?.reach?.tier === 'blocked';
const empty = () => ({ key: '', label: '', next: '', days: null, agreedOn: '' });
const stopped = p => TERMINAL.has(p?.status) || p?.next_step?.kind === 'done';

function calendarDay(value) {
  const day = String(value || '').slice(0, 10), stamp = Date.parse(day);
  return /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(stamp)
    && new Date(stamp).toISOString().slice(0, 10) === day ? day : '';
}

function waitingDays(value, now) {
  if (!calendarDay(value) || !Number.isFinite(now.getTime())) return null;
  const text = String(value), timestamp = Date.parse(text);
  if (text.length !== 10 && !ZONED.test(text)) return null;
  if (!Number.isFinite(timestamp) || timestamp > now.getTime()) return null;
  const day = text.length === 10 ? text : LONDON.format(new Date(timestamp));
  const days = (Date.parse(LONDON.format(now)) - Date.parse(day)) / DAY;
  return days >= 0 ? days : null;
}

// `agreed_on` is the agreement's date, not a deadline. Missing/invalid/future contact dates
// stay unknown; a completed commitment or terminal approach cannot create a new action.
export function personAction(person, progress, now = new Date()) {
  const action = empty(), p = progress || {}, step = p.next_step;
  if (blocked(person)) return action;
  const key = queueKey(p);
  if (!key) return action;
  if (p.reply_to_record) return { ...action, key, label: 'Needs reply', next: p.next || 'Record their reply and decide the next step.' };
  if (['mine', 'waiting'].includes(step?.kind) && step.text?.trim()) {
    const days = step.kind === 'waiting' && p.status === 'waiting' ? waitingDays(p.since, now) : null;
    const agreedOn = calendarDay(step.agreed_on);
    const next = (step.kind === 'mine' ? step.text : `Waiting for them: ${step.text}`)
      + (agreedOn ? ` · agreed ${RECORDED_DAY.format(new Date(agreedOn))}` : '');
    return { ...action, key,
      label: step.kind === 'mine' ? 'Your next step' : days === null ? 'Waiting' : `Waiting ${days} ${days === 1 ? 'day' : 'days'}`,
      next, days, agreedOn };
  }
  if (key === 'needs_reply') return { ...action, key, label: 'Needs reply',
    next: p.status === 'conversation' ? 'Agree and record the next step.' : 'Respond to their reply and record the next step.' };
  if (key === 'waiting') {
    const days = waitingDays(p.since, now);
    const recorded = days === null ? '' : `Contact recorded ${RECORDED_DAY.format(new Date(p.since))}. `;
    return { ...action, key: 'waiting', label: days === null ? 'Waiting' : `Waiting ${days} ${days === 1 ? 'day' : 'days'}`,
      next: `${recorded}Waiting for a reply${days === null ? '' : ` (${days} ${days === 1 ? 'day' : 'days'})`}. Review the conversation before deciding whether to follow up.`, days };
  }
  return action;
}

function actionOrder(a, b) {
  return (ORDER[a.key] ?? 99) - (ORDER[b.key] ?? 99)
    || (a.key === 'waiting' ? (b.days ?? -1) - (a.days ?? -1) : 0);
}

export function peopleContext(state, now = new Date(), index = contactRouteIndex(state)) {
  const byId = new Map(), candidates = new Map();
  const actNow = new Set((state.opportunities?.act_now || []).map(ref => ref.record_id));
  for (const person of index.records.values())
    if (person.type === 'person' && !person.archived_at) byId.set(person.id, { roles: [], action: empty(), item: null });
  for (const item of [...(state.intelligence?.actions || []), ...(state.intelligence?.research || [])]) {
    const id = item.person_id || item.record_id;
    if (!byId.has(id) || !item.progress || item.exclusion) continue;
    const candidate = { item, action: personAction(index.records.get(id), item.progress, now) };
    const previous = candidates.get(id);
    const tie = previous && Number(stopped(previous.item.progress)) - Number(stopped(item.progress));
    if (!previous || actionOrder(candidate.action, previous.action) < 0
      || (actionOrder(candidate.action, previous.action) === 0 && (tie < 0 || (!tie && item.id.localeCompare(previous.item.id) < 0))))
      candidates.set(id, candidate);
  }
  for (const job of index.records.values()) {
    if (job.type !== 'job' || !relevantAgencyRole(job, now.getTime(), { selected: actNow.has(job.id) })) continue;
    for (const route of contactRoutes(job, state, index)) {
      const entry = byId.get(route.person.id);
      if (entry && !blocked(route.person)) entry.roles.push({ job, bases: [...route.bases], direct: route.direct, actNow: actNow.has(job.id) });
    }
  }
  const queue = [];
  for (const [id, entry] of byId) {
    const person = index.records.get(id), candidate = candidates.get(id);
    if (candidate) { entry.item = candidate.item; entry.action = candidate.action; }
    entry.roles.sort((a, b) => Number(b.actNow) - Number(a.actNow) || a.job.name.localeCompare(b.job.name, 'en-GB') || a.job.id.localeCompare(b.job.id));
    const p = entry.item?.progress;
    if (!entry.action.key && !blocked(person) && !stopped(p)) {
      if (entry.roles.some(role => role.actNow)) entry.action = { ...empty(), key: 'act_now', label: 'For Act now', next: 'Review the linked role and confirm how they can help.' };
      else if (['first', 'next'].includes(person.priority)) entry.action = { ...empty(), key: 'priority', label: person.priority === 'first' ? 'First priority' : 'Next priority', next: 'Choose a purpose for contacting them and record the next step.' };
    }
    if (entry.action.key) queue.push(person);
  }
  queue.sort((a, b) => actionOrder(byId.get(a.id).action, byId.get(b.id).action)
    || (a.priority === 'first' ? 0 : a.priority === 'next' ? 1 : 2) - (b.priority === 'first' ? 0 : b.priority === 'next' ? 1 : 2)
    || a.name.localeCompare(b.name, 'en-GB') || a.id.localeCompare(b.id));
  return { byId, queue };
}
