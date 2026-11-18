// Page sections and order come from work_selection; this module only bounds their presentation.
import {E, A} from './dom.js';
import {availabilityStatus} from './job-evidence.js';
import {RESEARCH_SCHEDULE} from './research-status.js';

export function capacityText(policy) {
  if (policy?.research_pool_limit) return `Up to ${policy.working_limit} opportunities; ${policy.research_pool_limit} candidates available for research. At most ${policy.additions_per_seven_days} new records per seven days. Saved work stays separate.`;
  return policy ? `Up to ${policy.working_limit} opportunities to consider. Saved jobs and active processes stay separate. Everything else is searchable reference, with no review required. ${policy.research_pool_limit ? `Research can assess up to ${policy.research_pool_limit} candidates before selection. ` : ''}At most ${policy.additions_per_seven_days} new opportunities are added automatically over seven days.` : '';
}

// Best opportunities: discovery items worth a look whose advert, if any, still looks open. This week
// lists them and the Dashboard counts them, from this one rule.
export function bestOpportunities(state, byId = new Map((state.records || []).map(r => [r.id, r]))) {
  return (state.opportunities?.discovery || []).filter(ref => {
    const record = byId.get(ref.record_id);
    return record && ref.worth_a_look && (!record.availability || availabilityStatus(record).fresh);
  });
}

export function createWeekView({getState, getGroup = () => null, progress, openRecord, renderDecisions, cards, addIncoming, openJobs}) {
  const limits = new Map();
  function render(container) {
    const state = getState(), selection = state.opportunities;
    if (!selection || state.intelligence_error) {
      container.append(E('p', 'This week is unavailable. Jobs and People still hold your records.', 'alert')); return;
    }
    cards.reset();
    const links = E('div', null, 'toolbar');
    const all = E('a', 'All jobs'); all.href = '#work'; links.append(all);
    if (addIncoming) {
      const add = E('button', 'Add incoming role'); add.type = 'button'; add.onclick = addIncoming; links.append(add);
    }
    container.append(A(E('header', null, 'week-head'), links));
    if (state.discovery_capacity) container.append(E('p', capacityText(state.discovery_capacity), 'muted'));
    const byId = new Map((state.records || []).map(record => [record.id, record]));
    const actions = new Map([...(state.intelligence?.actions || []), ...(state.intelligence?.research || [])].map(action => [action.id, action]));
    const group = (key, title, refs, {brief = false, checking = false, folded = false} = {}) => {
      if (!refs.length && getGroup() !== key) return;
      const section = E(folded ? 'details' : 'section', null, `week-section week-${key}`);
      section.dataset.weekGroup = key; section.dataset.persistKey = `week:${key}`;
      section.append(A(E(folded ? 'summary' : 'div', null, 'section-head'),
        E(folded ? 'span' : 'h2', title), E('span', refs.length, 'count')));
      if (checking) section.append(E('p', `Research incomplete. If you want, Shortlist roles worth keeping or Skip unsuitable ones. ${RESEARCH_SCHEDULE}`, 'muted'));
      if (!refs.length) section.append(E('p', 'No records in this section right now.', 'empty'));
      const list = E('div', null, brief ? 'opportunity-list decision-list' : 'approach-list week-list');
      section.append(list);
      const more = E('button', '', 'show-more'); more.type = 'button';
      let shown = 0;
      const initial = brief ? (checking ? 6 : 3) : 10, maximum = brief ? (checking ? 6 : 10) : refs.length;
      const show = () => {
        const end = Math.min(refs.length, maximum, limits.get(key) || initial);
        for (; shown < end; shown++) {
          const ref = refs[shown], record = byId.get(ref.record_id), action = actions.get(ref.action_id);
          if (!record) continue;
          let card;
          if (brief) card = cards.card(record, ref, {checking, position: checking ? null : shown + 1, item: action});
          else if (action?.progress && action.record_id === ref.record_id)
            card = cards.enrichProgress(progress.attentionCard(action, {reason: ref.reason, compact: folded}), record, ref,
              {decision:key === 'act-now', item:action});
          else {
            const open = E('button', record.name, 'linkish approach-name'); open.type = 'button'; open.onclick = () => openRecord(record.id);
            card = cards.enrichProgress(A(E('article', null, 'approach-card'), A(E('h3'), open), E('p', ref.reason, 'approach-next')), record, ref,
              {decision:key === 'act-now'});
          }
          card.dataset.recordId = ref.record_id; card.dataset.opportunityId = ref.record_id;
          list.append(card);
        }
        more.hidden = shown >= Math.min(refs.length, maximum);
        more.textContent = brief ? `Show next ${Math.min(7, refs.length - shown)}` : `Show more (${refs.length - shown} remaining)`;
      };
      more.onclick = () => { limits.set(key, brief ? 10 : shown + 10); show(); };
      show(); section.append(more);
      if (brief && !checking) {
        const rest = E('a', `See all ${selection.discovery?.length || refs.length} in Jobs → Discovery`);
        rest.href = '#work'; rest.onclick = () => openJobs?.('discovery'); section.append(rest);
      }
      container.append(section);
    };
    group('act-now', 'Act now', selection.act_now || []);
    const best = bestOpportunities(state, byId);
    group('discovery', 'Best opportunities', best, {brief: true});
    group('needs-checking', 'Needs checking', selection.needs_checking || [], {brief: true, checking: true, folded: true});
    group('in-progress', 'In progress', selection.in_progress || [], {folded: true});
    group('waiting', 'Waiting', selection.waiting || [], {folded: true});
    if (!best.length && !['act_now','in_progress','waiting','needs_checking'].some(key => selection[key]?.length))
      container.append(E('p', 'No current priorities are supported by the available evidence. Jobs keeps the full collection.', 'empty'));
    renderDecisions?.(container);
  }

  function revealRecord(id) {
    let card = document.querySelector(`#content [data-opportunity-id="${CSS.escape(id)}"]`);
    if (!card) {
      const index = (getState().opportunities?.discovery || []).filter(ref => ref.worth_a_look).findIndex(ref => ref.record_id === id);
      if (index >= 0 && index < 10) document.querySelector('.week-discovery .show-more:not([hidden])')?.click();
      card = document.querySelector(`#content [data-opportunity-id="${CSS.escape(id)}"]`);
    }
    if (!card) { openRecord(id); return; }
    for (let parent = card.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
    const title = card.querySelector('.opportunity-title,.approach-name');
    title?.focus({preventScroll: true}); card.scrollIntoView({block: 'nearest'});
  }
  function revealAction(id) {
    const action = [...(getState().intelligence?.actions || []), ...(getState().intelligence?.research || [])].find(item => item.id === id);
    if (action) revealRecord(action.record_id);
  }
  function revealSection(group) {
    const section = document.querySelector(`#content [data-week-group="${CSS.escape(group)}"]`);
    if (!section) return;
    if (section.tagName === 'DETAILS') section.open = true;
    const heading = section.querySelector('summary, h2');
    if (heading) {
      if (heading.tagName !== 'SUMMARY') heading.tabIndex = -1;
      heading.focus({preventScroll: true});
    }
    section.scrollIntoView({block: 'start'});
  }
  return {render, revealAction, revealRecord, revealSection};
}
