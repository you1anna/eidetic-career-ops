// What the VPS prepared for this week's five people, from the newest research receipt. Read-only:
// every slot shows its state and why, so an empty slot is explained, never hidden.
import { E, A } from './dom.js';

const ORDER = ['ready', 'route_unverified', 'carried_forward', 'needs_information', 'budget_waiting',
  'follow_up', 'in_progress', 'failed', 'cleared', 'prepare'];
const SHORT = { ready: 'ready to review', route_unverified: 'route unverified', carried_forward: 'existing note kept',
  needs_information: 'needs information', budget_waiting: 'waiting for allowance', follow_up: 'follow-up due',
  in_progress: 'in progress', failed: 'failed', cleared: 'cleared by you', prepare: 'not yet prepared' };
const when = v => v ? new Date(v).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short',
  hour: '2-digit', minute: '2-digit' }) : 'never';

export function preparationSummary(prep) {
  const counts = prep?.counts || {};
  return ORDER.filter(k => counts[k]).map(k => `${counts[k]} ${SHORT[k]}`).join(' · ');
}

export function preparationPanel(prep, { week, records = [], openRecord } = {}) {
  if (!prep) return null;
  const panel = E('section', null, 'notice preparation-status');
  panel.setAttribute('aria-label', 'Weekly preparation');
  if (week && prep.week !== week) {
    panel.append(E('strong', 'Weekly preparation · not run this week yet'),
      E('p', `The last preparation was for the week of ${prep.week}. The VPS runs at 09:00 and 19:00 London time.`, 'muted'));
    return panel;
  }
  panel.append(E('strong', `Weekly preparation · ${preparationSummary(prep) || 'nothing recorded'}`),
    E('p', `Last pass ${when(prep.finished_at)}. Nothing is sent, and a note that already exists is never replaced.`, 'muted'));
  const list = E('ul', null, 'preparation-people');
  for (const slot of prep.people || []) {
    const person = records.find(r => r.id === slot.id);
    const name = E('button', person?.name || 'Someone on your list', 'linkish');
    name.type = 'button'; name.onclick = () => openRecord?.(slot.id);
    const row = A(E('li', null, `preparation-${slot.state}`), name, E('span', ` · ${slot.label}`, 'preparation-state'));
    if (slot.reason) row.append(E('p', slot.reason));
    if (slot.route && !['in_progress', 'follow_up'].includes(slot.state)) row.append(E('p', `Contact route: ${slot.route}`, 'muted'));
    list.append(row);
  }
  const details = A(E('details'), E('summary', 'Why each slot is in this state'), list);
  const used = prep.sublimit || {};
  const tokens = prep.tokens_per_saved ? ` About ${prep.tokens_per_saved.toLocaleString('en-GB')} tokens per saved note.` : '';
  details.append(E('p', `${prep.eligible_remaining ?? 0} people on your list not yet approached · ${prep.unsent_drafts ?? 0} unsent notes. `
    + `Preparation calls this week: ${used.used ?? 0} of ${used.limit ?? 15}, inside the research limits.${tokens} `
    + 'No reply is known unless you record it.', 'muted'));
  panel.append(details);
  return panel;
}
