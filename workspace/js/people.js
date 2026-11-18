// People presentation consumes one read-only action/role projection. Saved relationships stay factual.
// LinkedIn imports and their review queue live here too.
import { E, A } from './dom.js';
import { confirmedOpenAgencyRole, checkLabel } from './job-evidence.js';
import { contactRoute } from './week-brief.js';
import { peopleContext } from './people-context.js';
import { firstSentence, factualDate } from './record-presentation.js';

export const PEOPLE_GROUPS = [
  ['first', 'First priority', 'Your saved first priority contacts.'],
  ['next', 'Next', 'Also on your list, after the first group.'],
  ['current', 'Current contacts', 'People you know well, are in touch with, or know from before.'],
  ['recruiters', 'Recruiters', 'Saved recruiter details and agency links. Current roles may need checking.'],
  ['meet', 'To meet', "Decision-makers whose role the assistant confirmed on their company's own site."],
  ['others', 'Everyone else', 'Connections and contacts with no relationship recorded.'],
  ['never', 'Not contacting', 'Set aside. Excluded from contact routes, agency desks, outreach and research unless you promote them to current contacts.'],
];
const FOLDED = new Set(['others', 'never']);
const SHOWN = 12;
const tierOf = r => r?.relevance?.reach?.tier || 'cold';
const warmthOf = r => ({ close: 3, in_touch: 2, known: 1 })[tierOf(r)]
  || ({ active: 3, warm: 2, dormant: 1 })[r?.relationship] || 0;

export function groupPerson(r, recruitment = { people: new Set() }) {
  if (r.relationship === 'do_not_contact' || tierOf(r) === 'blocked') return 'never';
  if (r.priority === 'first' || r.priority === 'next') return r.priority;
  if (['close', 'in_touch', 'known'].includes(tierOf(r)) || ['warm', 'active', 'dormant'].includes(r.relationship)) return 'current';
  if (recruitment?.people?.has(r.id)) return 'recruiters';
  if (r.verification_level === 'primary-read') return 'meet';
  return 'others';
}

// Relationship context only: this does not admit work to Jobs or scheduled research.
// Existing desk research owns qualification; unrelated newer research cannot renew it.
export function recruitmentContext(state, now = new Date()) {
  const records = new Map((state.records || []).filter(r => !r.archived_at).map(r => [r.id, r]));
  const isAgency = r => r?.type === 'agency' && r.kind === 'agency';
  const allowed = r => !r.exclusion && !r.exclusion_contexts?.length
    && r.relationship !== 'do_not_contact' && r.relevance?.reach?.tier !== 'blocked';
  const people = new Set(), organisations = new Map(), consultants = new Map(), roles = new Map();
  const isWarm = p => ['warm', 'active'].includes(p?.relationship) || ['close', 'in_touch', 'known'].includes(p?.relevance?.reach?.tier);
  for (const r of records.values())
    if (r.type === 'person' && allowed(r) && ['recruiter', 'in_house_talent'].includes(r.kind)) people.add(r.id);
  for (const link of state.links || []) {
    const a = records.get(link.from_id), b = records.get(link.to_id);
    if (link.relation === 'consultant_at') {
      const [person, agency] = a?.type === 'person' ? [a, b] : [b, a];
      if (person?.type !== 'person' || !isAgency(agency) || !allowed(person)) continue;
      people.add(person.id);
      if (!organisations.has(person.id)) organisations.set(person.id, agency.name);
      const list = consultants.get(agency.id) || [];
      if (!list.some(p => p.id === person.id)) { list.push(person); consultants.set(agency.id, list); }
    } else if (['hiring_for', 'advertised_by'].includes(link.relation)) {
      const [job, agency] = isAgency(a) ? [b, a] : [a, b];
      if (!isAgency(agency) || !confirmedOpenAgencyRole(job, now.getTime())) continue;
      const list = roles.get(agency.id) || [];
      if (!list.some(j => j.id === job.id)) { list.push(job); roles.set(agency.id, list); }
    }
  }
  const agencies = [];
  const reviewTime = value => {
    const date = String(value || ''), time = Date.parse(date);
    const day = date.slice(0, 10), calendarTime = Date.parse(day);
    return /^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(date) && Number.isFinite(time)
      && Number.isFinite(calendarTime) && new Date(calendarTime).toISOString().slice(0, 10) === day ? time : NaN;
  };
  for (const r of records.values()) {
    if (!isAgency(r) || !allowed(r) || ['declined', 'no_relevant_desk'].includes(r.status)) continue;
    const deskReviews = (r.research || []).filter(x => Object.hasOwn(x, 'desk_summary') && Number.isFinite(reviewTime(x.reviewed_at)));
    const latestDesk = deskReviews.reduce((a, b) => !a || reviewTime(b.reviewed_at) > reviewTime(a.reviewed_at) ? b : a, null);
    const deskAt = latestDesk ? reviewTime(latestDesk.reviewed_at) : NaN, deskAge = now.getTime() - deskAt;
    const validDesk = !!(latestDesk && Number.isFinite(deskAge) && deskAge >= 0 && deskAge <= 90 * 86400000
      && latestDesk.verification === 'primary-read' && latestDesk.desk_summary?.trim()
      && latestDesk.public_contact_route?.trim() && latestDesk.source_urls?.length);
    if ((latestDesk && !validDesk) || (!latestDesk && (r.research || []).some(x => Object.hasOwn(x, 'desk_summary')))) continue;
    const agencyConsultants = (consultants.get(r.id) || []).slice().sort((a, b) => Number(isWarm(b)) - Number(isWarm(a)) || a.name.localeCompare(b.name, 'en-GB'));
    const warmConsultant = agencyConsultants.find(isWarm) || null;
    const agencyRoles = (roles.get(r.id) || []).slice().sort((a, b) =>
      Number(['contract', 'interim', 'ftc'].includes(b.engagement)) - Number(['contract', 'interim', 'ftc'].includes(a.engagement))
      || (b.relevance?.score || 0) - (a.relevance?.score || 0) || a.id.localeCompare(b.id));
    if (!agencyRoles.length) continue;
    const primaryReviews = (r.research || []).filter(x => x.verification === 'primary-read' && x.source_urls?.length && Number.isFinite(reviewTime(x.reviewed_at)));
    const latestPrimary = primaryReviews.reduce((a, b) => !a || reviewTime(b.reviewed_at) > reviewTime(a.reviewed_at) ? b : a, null);
    const primaryAt = latestPrimary ? reviewTime(latestPrimary.reviewed_at) : NaN, primaryAge = now.getTime() - primaryAt;
    const validPrimary = !!(latestPrimary && Number.isFinite(primaryAge) && primaryAge >= 0 && primaryAge <= 90 * 86400000);
    const review = validDesk ? latestDesk : validPrimary ? latestPrimary : null;
    const at = review ? reviewTime(review.reviewed_at) : 0;
    const consultant = warmConsultant || agencyConsultants[0] || null;
    const summary = validDesk ? latestDesk.desk_summary.trim()
      : (validPrimary && (latestPrimary.desk_summary || latestPrimary.summary)?.trim())
      || (agencyRoles.length ? `${agencyRoles.length} relevant role${agencyRoles.length > 1 ? 's' : ''}: ${agencyRoles.slice(0, 2).map(j => j.name).join('; ')}.` : r.summary || '');
    agencies.push({ record: r, review, consultant, roles: agencyRoles, summary, at });
  }
  agencies.sort((a, b) => b.at - a.at || a.record.name.localeCompare(b.record.name, 'en-GB') || a.record.id.localeCompare(b.record.id));
  return { people, organisations, agencies: agencies.slice(0, 5), allAgencies: agencies };
}

export function createPeopleView({ getState, getContactIndex, openRecord, openJob = openRecord, recordProgress, addPerson, importLinkedIn, importReview, demotePerson, promotePerson, openSending }) {
  let viewFilter = 'all', recruitment, context, container;
  const openFolded = new Set();
  const role = r => r.role || r.linkedin?.observed_role || '';
  const company = r => r.organisation
    || (r.linkedin?.observed_company ? `${r.linkedin.observed_company} (per LinkedIn, Sep 2026)` : '')
    || recruitment.organisations.get(r.id) || '';
  const group = r => groupPerson(r, recruitment);
  const filters = [
    ['Everyone', 'all'], ['For Act now', 'act_now'], ['Needs reply', 'needs_reply'], ['Waiting', 'waiting'],
    ['Current contacts', 'current'], ['Recruiters & agencies', 'recruiters'], ['Not contacting', 'never'],
  ];
  function matches(r, key) {
    const entry = context.byId.get(r.id);
    if (key === 'act_now') return entry?.roles.some(role => role.actNow);
    if (['needs_reply', 'waiting'].includes(key)) return entry?.action.key === key;
    if (key === 'recruiters') return recruitment.people.has(r.id);
    if (key === 'current') return ['first', 'next', 'current'].includes(group(r));
    if (key === 'never') return group(r) === 'never';
    return true;
  }
  function selectFilter(key) {
    viewFilter = key;
    container.replaceChildren();
    render(container);
  }
  function roleLinks(roles) {
    if (!roles?.length) return null;
    const line = E('div', null, 'person-roles');
    for (const {job, direct, actNow} of roles.slice(0, 2)) {
      const link = E('a', `${job.name}${direct ? '' : ' · connection'}`, 'person-role-link');
      link.href = `#work/${encodeURIComponent(job.id)}`;
      link.dataset.jobId = job.id;
      link.title = direct ? 'Recorded link to this role' : 'Recorded connection at the employer or agency; hiring responsibility is not confirmed';
      link.onclick = event => { event.preventDefault(); openJob(job.id); };
      line.append(link);
      if (actNow) line.append(E('span', 'Act now', 'muted'));
    }
    if (roles.length > 2) line.append(E('span', `+${roles.length - 2} more in details`, 'muted'));
    return line;
  }
  function row(r) {
    const entry = context.byId.get(r.id), action = entry?.action;
    const wrap = E('div', null, 'person-row-wrap'); wrap.dataset.personId = r.id;
    const button = E('button', null, 'record-card person-row'); button.type = 'button'; button.dataset.id = r.id;
    button.onclick = () => openRecord(r.id, true, entry?.item?.id || null);
    const about = [role(r), company(r)].filter(Boolean).join(' · ');
    button.append(E('strong', r.name || 'Untitled'));
    if (about) button.append(E('small', about));
    if (action?.next) button.append(E('small', firstSentence(action.next), 'record-context'));
    wrap.append(button);
    const controls = E('div', null, 'person-row-controls');
    const route = contactRoute(r, entry?.item?.progress?.channel);
    if (route.href) {
      const contact = E('a', 'Contact', 'person-contact'); contact.href = route.href;
      contact.title = route.label; contact.setAttribute('aria-label', `Contact ${r.name || 'person'} · ${route.label}`);
      if (route.href.startsWith('https:')) { contact.target = '_blank'; contact.rel = 'noopener noreferrer'; }
      controls.append(contact);
    }
    const chipKey = ['needs_reply','waiting'].includes(action?.key) ? action.key
      : entry?.roles.some(role => role.actNow) ? 'act_now' : null;
    if (chipKey) {
      const chip = E('button', chipKey === 'act_now' ? 'For Act now' : action.label, 'chip person-filter-chip');
      chip.type = 'button'; chip.dataset.peopleChip = chipKey;
      chip.setAttribute('aria-label', `Filter people: ${filters.find(([,key])=>key===chipKey)[0]}`);
      chip.onclick = () => selectFilter(chipKey); controls.append(chip);
    } else if (action?.label) controls.append(E('span', action.label, 'muted'));
    const isDemoted = group(r) === 'never';
    if ((isDemoted && promotePerson) || (!isDemoted && demotePerson)) {
      const actionButton = E('button', isDemoted ? 'Promote' : 'Demote to Not contacting', 'person-row-action');
      actionButton.type = 'button'; actionButton.dataset.personAction = isDemoted ? 'promote' : 'demote';
      actionButton.dataset.id = r.id;
      actionButton.setAttribute('aria-label', isDemoted ? `Promote ${r.name || 'person'} to current contacts` : `Demote ${r.name || 'person'} to Not contacting`);
      actionButton.onclick = () => isDemoted ? promotePerson(r) : demotePerson(r);
      if (isDemoted) controls.append(actionButton);
      else controls.append(A(E('details', null, 'person-more'), E('summary', 'More'), actionButton));
    }
    wrap.append(controls);
    const links = roleLinks(entry?.roles); if (links) wrap.append(links);
    return wrap;
  }
  function list(target, items) {
    const box = E('div', null, 'group-list people-list');
    items.slice(0, SHOWN).forEach(r => box.append(row(r))); target.append(box);
    if (items.length <= SHOWN) return;
    let shown = SHOWN;
    const more = E('button', `Show more (${items.length - shown})`, 'show-more'); more.type = 'button';
    more.onclick = () => {
      items.slice(shown, shown + SHOWN).forEach(r => box.append(row(r))); shown += SHOWN;
      if (shown >= items.length) more.remove(); else more.textContent = `Show more (${items.length - shown})`;
    };
    target.append(more);
  }
  function agencyRow({record, review, consultant, roles, summary}) {
    const wrap = E('div', null, 'person-row-wrap agency-row-wrap');
    const button = E('button', null, 'record-card agency-row'); button.type = 'button'; button.dataset.id = record.id;
    button.onclick = () => openRecord(record.id);
    const p = recordProgress?.(record);
    const who = consultant ? `Desk contact: ${consultant.name}${role(consultant) ? ` (${role(consultant)})` : ''}` : '';
    const roleContact = roles[0]?.briefing?.public_contact_route;
    const sub = roleContact ? `${roles[0].name}: ${firstSentence(roleContact)}`
      : [who, firstSentence(summary || review?.desk_summary)].filter(Boolean).join(' · ');
    button.append(A(E('span'), E('strong', record.name), sub ? E('small', sub) : null));
    if (p) button.append(E('span', p.label, 'chip'));
    else if (review?.reviewed_at) button.append(E('small', `Desk researched ${factualDate(review.reviewed_at)}`, 'muted'));
    wrap.append(button);
    const links = E('div', null, 'person-roles');
    for (const job of roles.slice(0, 2)) {
      const link = E('a', job.name, 'person-role-link'); link.href = `#work/${encodeURIComponent(job.id)}`;
      link.dataset.jobId = job.id; link.onclick = event => { event.preventDefault(); openJob(job.id); };
      links.append(link, checkLabel(job.evidence_checks?.vacancy, 'vacancy', job));
    }
    if (roles.length > 2) links.append(E('span', `+${roles.length - 2} more in details`, 'muted'));
    wrap.append(links);
    return wrap;
  }
  function renderAgencies(target) {
    if (!['all','recruiters'].includes(viewFilter) || !recruitment.agencies.length) return;
    const original = recruitment.allAgencies;
    const section = E('section', null, 'people-agencies');
    let shown = recruitment.agencies.length, signature;
    const renderCurrent = (now = Date.now(), force = false) => {
      const all = original.map(item => ({...item, roles:item.roles.filter(job => confirmedOpenAgencyRole(job,now))}))
        .filter(item => item.roles.length);
      const next = JSON.stringify(all.map(item => [item.record.id,...item.roles.map(job => job.id)]));
      if (!force && next === signature) return;
      signature = next; section.hidden = !all.length; section.replaceChildren();
      section.append(A(E('div', null, 'section-head'), E('h2', 'Agencies to approach'), E('span', `${all.length} agencies`, 'count')),
        E('p', 'Relevant roles with current open evidence. Agency counts are separate from people.', 'muted section-note'));
      const box = E('div', null, 'group-list people-list');
      all.slice(0,shown).forEach(item => box.append(agencyRow(item))); section.append(box);
      if (all.length > shown) {
        const more = E('button', `Show more (${all.length-shown})`, 'show-more'); more.type='button';
        more.onclick = () => { shown += SHOWN; renderCurrent(Date.now(),true); };
        section.append(more);
      }
    };
    section.refreshAgencyRoles = renderCurrent;
    renderCurrent();
    target.append(section);
  }
  function render(target) {
    container = target;
    const state = getState(); recruitment = recruitmentContext(state); context = peopleContext(state, new Date(), getContactIndex?.());
    const allPeople = state.records.filter(r => r.type === 'person' && !r.archived_at);
    const people = allPeople.filter(r => matches(r, viewFilter));
    const toolbar = E('div', null, 'toolbar people-tools');
    if (openSending) {
      const send=E('button','Send messages','primary');send.type='button';send.onclick=openSending;toolbar.append(send);
    }
    for (const [label, handler] of [['Add a person',addPerson],['Import LinkedIn CSV',importLinkedIn]]) {
      const button = E('button', label); button.type = 'button'; button.onclick = handler; toolbar.append(button);
    }
    target.append(toolbar);
    const filterBar = E('div', null, 'toolbar people-filters'); filterBar.setAttribute('aria-label','Filter people');
    for (const [label,key] of filters) {
      const selected = viewFilter === key, count = allPeople.filter(r => matches(r,key)).length;
      const button = E('button', null, selected ? 'active' : ''); button.type='button';
      button.append(E('span',label), E('span',count,'filter-count'));
      button.dataset.peopleFilter = key; button.setAttribute('aria-label',label);
      button.setAttribute('aria-pressed',String(selected)); button.onclick=()=>selectFilter(key); filterBar.append(button);
    }
    target.append(filterBar, E('p', `${people.length} unique ${people.length === 1 ? 'person' : 'people'}`, 'muted people-count'));
    const queue = context.queue.filter(r => matches(r,viewFilter));
    const queueIds = new Set(queue.map(r=>r.id));
    if(queue.length || viewFilter==='all') {
      const head=A(E('div',null,'section-head'),E('h2','Needs attention'),E('span',queue.length,'count')); head.id='people-attention';
      target.append(head,E('p','Replies and next steps first, then oldest waits, role connections and priorities.','muted section-note'));
      if(queue.length) list(target,queue);
      else target.append(E('p','No replies, next steps or priority contacts need attention. Your directory is below.','notice'));
    }
    const byGroup=new Map(PEOPLE_GROUPS.map(([key])=>[key,[]]));
    people.filter(r=>!queueIds.has(r.id)).forEach(r=>byGroup.get(group(r)).push(r));
    const order=(a,b)=>warmthOf(b)-warmthOf(a)||(b.relevance?.score||0)-(a.relevance?.score||0)||String(a.name).localeCompare(String(b.name),'en-GB');
    for (const [key,title,note] of PEOPLE_GROUPS) {
      const items=byGroup.get(key).sort(order); if(!items.length) continue;
      if(FOLDED.has(key) && viewFilter!==key) {
        const details=A(E('details',null,'lower people-folded'),E('summary',`${title} (${items.length})`),E('p',note,'muted')); details.id=`people-${key}`;
        details.addEventListener('toggle',()=>{
          if(details.open){openFolded.add(key);if(details.childNodes.length===2)list(details,items);}else openFolded.delete(key);
        });
        if(openFolded.has(key)){details.open=true;list(details,items);} target.append(details);
      } else {
        const head=A(E('div',null,'section-head'),E('h2',title),E('span',items.length,'count'));head.id=`people-${key}`;
        target.append(head,E('p',note,'muted section-note'));list(target,items);
      }
    }
    renderAgencies(target);
    if(!people.length && viewFilter!=='all') target.append(E('p','No people match this filter. Choose Everyone to see the directory.','notice'));
    if(viewFilter==='all') importReview.render(target);
  }
  return {render,count:()=>peopleContext(getState(), new Date(), getContactIndex?.()).queue.length};
}
