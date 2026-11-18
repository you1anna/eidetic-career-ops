// One approach card and one set of step controls, used by This week, Outreach and the record sheet.
// Everything shown and every action offered comes from the server's progress view (workspace/approaches.py).
import { E, A, L } from './dom.js';
import { OUTCOME_NAMES, CHANNEL_NAMES } from './labels.js';
import { contactRoute } from './week-brief.js';
import { contactAction, createContactLog, CONTACT_CHANNELS } from './contact-log.js';
import { nextStepFields } from './next-step.js';

const statusClass = s => ({ follow_up: 'progress-follow-up', replied: 'progress-replied', conversation: 'progress-replied',
  submitted: 'state-in_progress', proposal: 'state-in_progress', interview: 'state-in_progress', offer: 'state-in_progress',
  waiting: 'progress-waiting', no_response: 'state-paused', held: 'state-paused', won: 'state-done', handed_over: 'state-done',
  closed: 'state-done' })[s] || '';

// People share the server's factual contact/reply context. Generated outreach advice
// stays hidden; only a current human commitment can have an agreed date.
export function personalProgressContext(p) {
  if (p?.reply_to_record) return p.next || '';
  if (!p || ['research','held','no_response','closed','skipped','won','handed_over'].includes(p.status)) return '';
  if (['mine','waiting'].includes(p.next_step?.kind))
    return p.next ? `${p.next}${p.next_step.agreed_on ? '' : ' · No agreed date'}` : '';
  return p.status === 'waiting' && !p.next_is_default ? p.next || '' : '';
}

export function createProgressViews({ getState, getRecord, openRecord, openContactDetails, openDecision, mutate, showNotice, setDirty, openModal, revealAction }) {
  const button = (text, fn, cls = '') => { const b = E('button', text, cls); b.type = 'button'; b.onclick = fn; return b; };
  const openLog = createContactLog({ openModal, setDirty,
    save: (item, action, reported, status, fields) => run(item, action, '', status, fields, reported) });
  const logContact = item => {
    const action = contactAction(item);
    if (action) openLog({ item, action, revision: getRecord(action.record_id)?.revision });
  };
  const logButton = item => {
    const action = contactAction(item);
    return action ? button(action.kind === 'followed_up' ? 'Log follow-up' : 'Log contact', () => logContact(item), 'contact-log-button') : null;
  };
  const stamp = at => { const d = new Date(at); return Number.isNaN(d.valueOf()) ? '' : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); };
  const progressLabel = p => p.status === 'waiting' && p.history?.some(h => ['sent', 'followed_up'].includes(h.kind))
    ? 'Sent · waiting for a reply' : p.label;
  function who(item) {
    const record = getRecord(item.record_id) || {};
    if (item.person) return `${item.person}${item.role ? ', ' + item.role : ''}${item.company ? ' · ' + item.company : ''}`;
    const name = record.name || item.role || item.company || item.title || 'Untitled';
    return item.company && item.company !== name ? `${name} · ${item.company}` : name;
  }

  // One tap to the person on the channel this move uses: their email, LinkedIn profile or phone.
  // When their preferred channel lacks a phone or email, fall back to their LinkedIn profile or an inline hint.
  function contact(item) {
    const person = getRecord(item.person_id || item.record_id) || {};
    if (person.type !== 'person' || item.exclusion) return null;
    const route = contactRoute(person, item.progress?.channel);
    const { missing } = route;
    if (!route.href && !missing) return null;
    const reach = E('p', null, 'approach-reach');
    if (route.href) {
      const a = E('a', 'Contact', 'approach-contact');
      a.href = route.href;
      a.title = route.label;
      a.setAttribute('aria-label', `Contact · ${route.label}`);
      if (route.href.startsWith('https:')) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
      reach.append(a);
    }
    if (missing) {
      const add = button(`Add ${missing}`, () => openContactDetails
        ? openContactDetails(person.id, missing === 'phone number' ? 'phone' : 'email', item.id)
        : openRecord(person.id, true, item.id), 'linkish');
      reach.append(route.href ? E('span', ` · no ${missing} recorded (`, 'muted') : E('span', `No ${missing} recorded · `, 'muted'),
        add, route.href ? E('span', ')', 'muted') : null);
    }
    return reach;
  }

  async function run(item, action, note, status, controls, reported = null) {
    const fromCard = controls?.closest('.approach-card') || (reported &&
      [...document.querySelectorAll('.approach-card')].find(c => c.dataset.actionId === item.id));
    const busy = controls ? [...controls.querySelectorAll('button')] : [];
    busy.forEach(b => { b.disabled = true; });
    // Any message left on the line is a problem to fix (a refusal or a failed save); success clears it.
    const show = (text, problem) => {
      if (!status) return;
      status.textContent = text;
      status.classList.toggle('is-error', problem);
    };
    show('Saving…', false);
    const done = msg => { busy.forEach(b => { b.disabled = false; }); show(msg || '', !!msg); return !msg; };
    try {
      const rid = action.record_id;
      const revision = controls?.recordRevisions?.get(rid) ?? getRecord(rid)?.revision;
      let result = null, undo = null;
      if (action.method === 'next_step') {
        editNextStep(item, action);
        return done();
      } else if (action.method === 'outcome' && action.asks === 'company') {
        return submission(item, action, note, done);
      } else if (action.method === 'outcome') {
        const how = reported ? reported.channel : ['sent', 'followed_up'].includes(action.kind) ? controls?.querySelector('select[name=how]')?.value || action.channel : '';
        result = await mutate(`/api/records/${encodeURIComponent(rid)}/outcome`, { kind: action.kind,
          revision: reported ? reported.revision : revision, action_id: action.action_id, note: reported?.note || note,
          ...(how ? { channel: how } : {}), ...(reported?.next_step !== undefined ? { next_step:reported.next_step } : {}) });
        if (result?.outcome?.id && !result.duplicate) undo = { method: 'correct', undo: true, record_id: rid, outcome_id: result.outcome.id };
      } else if (action.method === 'transition') {
        result = await mutate(`/api/records/${encodeURIComponent(rid)}/transition`, { field: action.field, to: action.to, revision, note });
        undo = { method: 'correct', undo: true, record_id: rid, field: action.field };
      } else if (action.method === 'handover') {
        return handover(item, action, done);
      } else if (action.method === 'correct') {
        if (!action.undo && !confirm('Correct this entry? It stays in the history, marked as corrected.')) return done();
        result = await mutate(`/api/records/${encodeURIComponent(rid)}/correct`, { revision, note: note || 'Corrected',
          ...(action.outcome_id !== undefined ? { outcome_id: action.outcome_id } : { field: action.field }) });
      } else if (action.method === 'open') {
        openRecord(action.record_id);
        return done();
      }
      setDirty(!!document.querySelector('#content .approach-card[data-progress-dirty]'));
      if (fromCard) revealAction?.(item.id);
      // A status change can shorten the card considerably. Keep its identity in view,
      // including when a reply changes the displayed next step.
      if (fromCard) requestAnimationFrame(() => {
        const card = [...document.querySelectorAll('.approach-card')].find(c => c.dataset.actionId === item.id);
        const heading = card?.querySelector('.approach-name');
        heading?.focus({ preventScroll: true });
        heading?.scrollIntoView({ block: 'center' });
      });
      if (action.method === 'correct') {
        showNotice(action.undo ? 'Undone: the card is back as it was before.' : 'Corrected: it stays in the history, marked as corrected.');
        return done();
      }
      showNotice(`Saved: ${action.label}${result?.duplicate ? ' (already recorded)' : ''}. Nothing was sent.`,
        undo ? ['Undo', () => run(item, { ...undo, label: 'Undone' }, 'Undone straight after saving', null, null)] : null);
      done();
    } catch (error) {
      done(error.message);
      // Keep failures visible even if a concurrent reload detached the original status.
      if (!status?.isConnected) showNotice(`Not saved: ${error.message}`);
      return false;
    }
  }

  function editNextStep(item, action) {
    const fields = nextStepFields(item.progress.next_step);
    const revision = getRecord(action.record_id)?.revision;
    fields.box.addEventListener('input', () => setDirty(true));
    openModal('Update next step', fields.box, async () => {
      await mutate(`/api/records/${encodeURIComponent(action.record_id)}/next-step`, {
        revision, action_id:action.action_id, next_step:fields.read(),
      });
      setDirty(!!document.querySelector('#content .approach-card[data-progress-dirty]'));
      revealAction?.(item.id);
      showNotice('Next step saved. Contact history is unchanged.');
    }, 'Save next step');
  }

  function handover(item, action, done) {
    const revision = getRecord(action.record_id)?.revision;
    const fields = E('div', null, 'form-grid');
    const field = A(E('label', null, 'field span-2'), E('span', 'Agreed next step (optional)'));
    const input = E('textarea'); input.name = 'next_step'; input.rows = 3; input.maxLength = 500;
    if (['mine', 'waiting'].includes(item.progress?.next_step?.kind)) input.value = item.progress.next_step.text;
    input.oninput = () => setDirty(true);
    field.append(input);
    fields.append(field, E('p', 'Creates an opportunity in Work with this conversation\'s history. Nothing is sent.', 'muted span-2'));
    openModal('Hand over to Work', fields, async data => {
      const rid = action.record_id;
      const result = await mutate(`/api/records/${encodeURIComponent(rid)}/handover`, { revision,
        action_id: action.action_id, company_id: action.company_id || '', next_step: data.next_step || '' });
      setDirty(false);
      showNotice('Handed over to Work.', result?.opportunity ? ['Open it', () => openRecord(result.opportunity.id)] : null);
    }, 'Hand over');
    done();
  }

  // Which company a CV went to (the owner's rule, 15 October): only with his yes, and never into a referral under way.
  function submission(item, action, note, done) {
    const revision = getRecord(action.record_id)?.revision;
    const fields = E('div', null, 'form-grid');
    const field = A(E('label', null, 'field span-2'), E('span', 'Which company was your CV submitted to?'));
    const input = E('input'); input.name = 'company'; input.required = true; input.maxLength = 120; input.autocomplete = 'organization';
    input.oninput = () => setDirty(true);
    field.append(input);
    fields.append(field, E('p', 'Only with your yes for this role. If someone you know is referring you there, settle that first. Nothing is sent.',
      'muted span-2'));
    openModal('CV submitted', fields, async data => {
      const rid = action.record_id;
      const result = await mutate(`/api/records/${encodeURIComponent(rid)}/outcome`, { kind: action.kind, revision,
        action_id: action.action_id, note, company: data.company || '' });
      setDirty(false);
      const undo = result?.outcome?.id && !result.duplicate ? { method: 'correct', undo: true, record_id: rid, outcome_id: result.outcome.id } : null;
      showNotice(`Saved: CV submitted to ${data.company}${result?.duplicate ? ' (already recorded)' : ''}. Nothing was sent.`,
        undo ? ['Undo', () => run(item, { ...undo, label: 'Undone' }, 'Undone straight after saving', null, null)] : null);
    }, 'Save');
    done();
  }

  function controls(item, extra = []) {
    const p = item.progress;
    const box = E('div', null, 'approach-actions');
    const status = E('p', '', 'approach-status muted'); status.setAttribute('role', 'status');
    const actions = [...(p.actions || []), ...extra].filter(a => !['copy','decision','week','week_restore'].includes(a.method));
    box.recordRevisions = new Map(actions.map(action => [action.record_id, getRecord(action.record_id)?.revision]));
    const note = E('textarea'); note.rows = 2; note.placeholder = 'Optional note for this step';
    note.setAttribute('aria-label', 'Note for this step'); note.maxLength = 2000;
    note.oninput = () => setDirty(!!note.value);
    const primary = actions.find(a => a.primary);
    if (primary) box.append(button(primary.label, () => run(item, primary, note.value, status, box), 'primary'));
    const log = logButton(item);
    if (log) box.append(log);
    const nextStepAction = actions.find(a => a.method === 'next_step');
    if (nextStepAction) box.append(button(nextStepAction.label, () => run(item, nextStepAction, '', status, box)));
    const secondary = actions.filter(a => a !== primary && a !== nextStepAction && !(log && ['sent','followed_up'].includes(a.kind)));
    const afterContact = actions.some(a => a.method === 'outcome' && ['replied', 'conversation'].includes(a.kind));
    if (afterContact) {
      note.placeholder = 'What they said, or useful context for this event (optional)';
      box.prepend(A(E('label', null, 'field approach-note-field'), E('span', 'Contact note (optional)'), note));
    }
    if (secondary.length || primary) {
      const more = A(E('details', null, 'approach-more'), E('summary', 'More'));
      if (!afterContact) more.append(A(E('label', null, 'field'), E('span', 'Note (optional)'), note));
      const contact = actions.find(a => a.method === 'outcome' && ['sent', 'followed_up'].includes(a.kind));
      if (contact && item.person_id) {
        const how = E('select'); how.name = 'how'; how.setAttribute('aria-label', 'How you got in touch');
        const first = contact.channel || p.channel;
        [...new Set([first, ...CONTACT_CHANNELS].filter(Boolean))].forEach(x => { const o = E('option', CHANNEL_NAMES[x] || x); o.value = x; how.append(o); });
        if (!first) how.value = 'other';
        more.append(A(E('label', null, 'field'), E('span', 'How you got in touch'), how));
      }
      const row = E('div', null, 'approach-more-actions');
      secondary.forEach(a => row.append(button(a.label, () => run(item, a, note.value, status, box))));
      more.append(row);
      box.append(more);
    }
    box.append(status);
    return box;
  }

  // A done card keeps the Undo for a step recorded this week, so a mistaken tap is put right where it happened;
  // an item set aside this week keeps its Put back the same way.
  function undoControls(item, actions, note = 'Undone from This week') {
    const box = E('div', null, 'approach-actions approach-undo');
    const status = E('p', '', 'approach-status muted'); status.setAttribute('role', 'status');
    actions.forEach(a => box.append(button(a.label, () => run(item, a, note, status, box))));
    box.append(status);
    return box;
  }

  function history(item) {
    const rows = item.progress.history || [];
    if (!rows.length) return null;
    const list = E('ol', null, 'approach-history');
    rows.forEach(h => list.append(A(E('li'), E('strong', OUTCOME_NAMES[h.kind] || 'Recorded'), E('span', ` · ${stamp(h.at)}`, 'muted'),
      h.note ? L('span', ` — ${h.note}`) : null)));
    return list;
  }

  // Factual progress is shared by Jobs, This week and the record sheet.
  function card(item, {eyebrow='', extra=[], done=false}={}) {
    const p=item.progress,c=E('article',null,'approach-card');c.dataset.actionId=item.id;c.dataset.recordId=item.record_id;c.dataset.progress=p.status;
    c.append(A(E('div',null,'approach-top'),E('span',eyebrow||p.kind_label,'eyebrow'),E('span',progressLabel(p),'chip '+statusClass(p.status))),
      A(E('h3'),button(who(item),()=>openRecord(item.record_id,true,item.id),'approach-name linkish')));
    if(p.next)c.append(E('p',p.next,'approach-next'));
    if(p.warning)c.append(E('p',p.warning,'approach-warning'));
    c.append(controls(item,extra));return c;
  }
  const focusCard = card;

  // Small weekly cards share the existing editor and controls, created only on expansion.
  const openAttention = new Set();
  function attentionCard(item, options = {}) {
    const p = item.progress, compact = options.compact;
    const c = E('article', null, 'approach-card week-attention-card' + (compact ? ' is-compact' : ''));
    c.dataset.actionId = item.id; c.dataset.recordId = item.record_id; c.dataset.progress = p.status;
    c.recordRevision = getRecord(item.record_id)?.revision;
    c.addEventListener('input', () => { c.dataset.progressDirty = 'true'; setDirty(true); });
    c.addEventListener('change', () => { c.dataset.progressDirty = 'true'; setDirty(true); });
    c.append(A(E('div', null, 'approach-top'), E('span', p.kind_label, 'eyebrow'),
      E('span', p.reply_to_record ? 'Reply to record' : progressLabel(p), 'chip ' + statusClass(p.status))),
      A(E('h3'), button(who(item), () => openRecord(item.record_id, true, item.id), 'approach-name linkish')));
    c.append(E('p', options.reason || p.next, 'approach-next'));
    const key = `${item.id}:${p.status}`;
    const detail = A(E('details', null, 'week-progress'), E('summary', compact ? 'View progress' :
      'Update progress', compact ? '' : 'primary'));
    const extra = options.extra;
    const log = compact ? null : logButton(item);
    const quick = E('div', null, 'week-quick-actions');
    if (log) quick.append(log);

    if (quick.childElementCount) c.append(quick);
    let built = false;
    const build = () => {
      if (built) return; built = true;
      const full = card(item, { ...options, extra });
      // Identity and next step already appear above. Keep one action-id and one card per approach.
      full.querySelector('.approach-top')?.remove(); full.querySelector('h3')?.remove();
      full.querySelector('.approach-next')?.remove();
      if (log) full.querySelector('.contact-log-button')?.remove();
      detail.append(...full.childNodes);
      const h = history(item);
      if (h) detail.append(A(E('details', null, 'approach-history-box'), E('summary', `History (${p.history.length})`), h));
    };
    detail.open = openAttention.has(key);
    if (detail.open) build();
    detail.addEventListener('toggle', () => {
      if (detail.open) { openAttention.add(key); build(); } else openAttention.delete(key);
    });
    c.append(detail);
    return c;
  }

  // The fuller block on a record's sheet: the same status, next step and actions, plus the history.
  function block(item, options = {}) {
    const p = item.progress;
    const personal = getRecord(item.record_id)?.type === 'person' && item.route !== 'agency';
    const next = options.next ?? (personal ? personalProgressContext(p) : p.next);
    const label = personal && !p.history?.length ? 'No contact outcome recorded' : p.label;
    const sec = A(E('section', null, 'detail-section approach-block'), A(E('h3', null, 'why-head'), E('span', 'Where this stands'),
      E('span', label, 'chip ' + statusClass(p.status))));
    sec.dataset.actionId = item.id;
    sec.dataset.recordId = item.record_id;
    if (next) sec.append(E('p', next, 'approach-next'));
    else if (personal) sec.append(E('p', 'No next step recorded.', 'muted'));
    if (p.warning) sec.append(E('p', p.warning, 'approach-warning'));
    const reach = contact(item);
    if (reach) sec.append(reach);
    sec.append(controls(item));
    const h = history(item);
    if (h) sec.append(A(E('details', null, 'approach-history-box'), E('summary', `History (${p.history.length})`), h));
    return sec;
  }

  // Shared list editors survive another record's save on either work surface. Retain
  // the original DOM/handlers only while its revision and progress still match.
  const hasEdits = () => !!document.querySelector('#content .approach-card[data-progress-dirty]');
  function retainEdits(exceptRecord) {
    return [...document.querySelectorAll('#content .approach-card[data-progress-dirty]')]
      .filter(node => node.dataset.recordId !== exceptRecord)
      .map(node => ({ node, revision: node.recordRevision }));
  }
  function restoreEdits(saved) {
    const cards = new Map([...document.querySelectorAll('#content .approach-card[data-action-id]')]
      .map(node => [node.dataset.actionId, node]));
    for (const { node, revision } of saved) {
      const next = cards.get(node.dataset.actionId);
      if (next && next.dataset.progress === node.dataset.progress && getRecord(node.dataset.recordId)?.revision === revision)
        next.replaceWith(node);
    }
  }
  return { card, focusCard, attentionCard, block, controls, undoControls, run, who, contact, retainEdits, restoreEdits, hasEdits };
}
