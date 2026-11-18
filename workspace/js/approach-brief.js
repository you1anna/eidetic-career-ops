// One usable brief, assembled from existing records, evidence and declared progress.
import { E, A, L, extLink, linkify } from './dom.js';
import { checkLabel, checkStatus, contactRouteIndex, contactRoutes, confirmedOpenAgencyRole, decisionBlockerLabel, routeLine, employerContactRoute } from './job-evidence.js';
import { statedDate, engagementLabel, jobDecisionFacts, jobLocation } from './record-presentation.js';
import {sourceCheckText, researchTime} from './research-status.js';
import { contactRoute } from './week-brief.js';
import { peopleContext, personAction } from './people-context.js';

const blocked = (record, isExcluded) => !record || record.archived_at || record.exclusion ||
  record.exclusion_contexts?.length || record.relevance?.reach?.tier === 'blocked' ||
  record.relationship === 'do_not_contact' || (isExcluded && isExcluded(record));
const bounded = (text, limit = 420) => {
  const value = String(text || '').trim().replace(/\s+/g, ' ');
  if (value.length <= limit) return value;
  const end = value.lastIndexOf(' ', limit);
  return value.slice(0, end > limit / 2 ? end : limit).trimEnd() + '…';
};
const button = (text, fn, cls = 'linkish') => {
  const node = E('button', text, cls); node.type = 'button'; node.onclick = fn; return node;
};
// Evidence completeness uses the same colours as working states elsewhere on the site.
const actions = state => [...(state.intelligence?.actions || []), ...(state.intelligence?.research || [])];

function personApproach(person, state, job) {
  const companyIds = new Set((state.links || []).filter(link =>
    ['hiring_for', 'advertised_by'].includes(link.relation) &&
    (link.from_id === job?.id || link.to_id === job?.id))
    .map(link => link.from_id === job?.id ? link.to_id : link.from_id));
  const available = actions(state).filter(item => item.progress && !item.exclusion &&
    (item.person_id === person.id || item.record_id === person.id));
  return available.find(item => companyIds.has(item.company_id)) ||
    available.find(item => item.progress.section === 'in_progress') || available[0] ||
    { record_id: person.id, person_id: person.id, progress: { channel: person.preferred_channel || '' } };
}

// This is a contact suggestion, never an inference that someone is the hiring manager.
// Fresh evidence comes first; within that group prefer an existing relationship over a cold route.
export function suggestedContact(job, state, isExcluded, routeIndex) {
  if (blocked(job, isExcluded)) return null;
  const rank = route => [
    Number(checkStatus(route.check, 'contact_route').fresh),
    ({ active: 3, warm: 2, dormant: 1 })[route.person.relationship] || 0,
    ({ first: 2, next: 1 })[route.person.priority] || 0,
    Number(route.direct),
    Number([...route.bases].some(basis => /^(Recorded at|Recruiter at)/.test(basis))),
    Number.isFinite(Date.parse(route.check?.checked_at)) ? Date.parse(route.check.checked_at) : 0,
  ];
  return contactRoutes(job, state, routeIndex).filter(route => !blocked(route.person, isExcluded)).sort((a, b) => {
    const left = rank(a), right = rank(b);
    for (let i = 0; i < left.length; i++) if (left[i] !== right[i]) return right[i] - left[i];
    return a.person.id.localeCompare(b.person.id);
  })[0] || null;
}

function routeCard(route, state, job, { progress, openRecord }, suggested = false) {
  const { person, check } = route;
  const card = E('div', null, 'contact-route' + (suggested ? ' suggested-contact' : ''));
  if (suggested) card.append(E('h4', checkStatus(check, 'contact_route').fresh ? 'Suggested contact' : 'Contact to check'));
  const name = button(person.name, () => openRecord(person.id), 'linkish contact-name');
  name.dataset.contactId = person.id;
  card.append(name, routeLine(check).node);
  card.append(E('p', 'Confirm their current employer and connection to this role before approaching.', 'brief-contact-basis'));
  const profile = contactRoute(person, 'linkedin');
  if (profile.href) card.append(extLink(profile.href, 'Review profile ↗'));
  if (suggested) {
    const item = personApproach(person, state, job);
    if (person.known_context) card.append(L('p', bounded(person.known_context, 360), 'brief-known-context'));
    if (item.progress.plan?.last) card.append(A(E('p', null, 'approach-last'), E('b', 'Last talked '),
      E('span', bounded(item.progress.plan.last, 200))));
    if (item.progress.warning || item.progress.plan?.check) card.append(L('p',
      bounded(item.progress.warning || item.progress.plan.check, 300), 'approach-warning'));
    const reach = progress.contact(item);
    if (reach) card.append(reach);
    else card.append(E('p', 'Contact details have not been recorded yet.', 'muted'));
  }
  const evidence = A(E('details', null, 'brief-contact-evidence'), E('summary', 'Source and context'));
  evidence.append(E('p', [...route.bases].join(' · '), 'brief-contact-basis'));
  const role = [person.role || person.linkedin?.observed_role, person.organisation || person.linkedin?.observed_company].filter(Boolean).join(' · ');
  if (role) evidence.append(E('p', `Recorded role: ${role}`));
  if (check) evidence.append(E('small', `${check.status === 'unverified' ? 'Attempted' : 'Recorded check'} ${researchTime(check.checked_at)}`, 'muted'),
    L('p', sourceCheckText(check.basis)), extLink(check.source_url, 'Contact source ↗'));
  else evidence.append(E('p', 'Current employer and route have not been verified.', 'muted'));
  if (person.known_context) evidence.append(L('p', person.known_context));
  if (route.notes.size) evidence.append(L('p', [...route.notes].join(' ')));
  evidence.append(E('small', 'The recorded connection does not establish hiring authority or an agreement to introduce you.', 'muted'));
  card.append(evidence);
  return card;
}

export function requirementPresentation(item) {
  const importance = ({essential:'Essential', desirable:'Desirable', ambiguous:'Unspecified importance'})[item.importance] || 'Unspecified importance';
  const status = ({supported:'Supported by recorded experience', not_established:'Not established from recorded experience',
    conflicting:'Source wording conflicts', not_required:'Not required by this source'})[item.status] || 'Comparison uncertain';
  return {importance, status, quote:item.quote || item.text || ''};
}

// One assessment replaces duplicated generated approach, fit and prepared-context blocks.
export function workAssessment(record, current = null, {decision = false} = {}) {
  const section = E('section', null, 'evidence-brief work-assessment');
  section.setAttribute('aria-label', 'Work assessment');
  section.append(E('h3', 'Work assessment'));
  const brief = record.evidence_brief, briefing = record.briefing || {};
  const seen = new Set();
  const key = value => String(value || '').trim().replace(/\s+/g, ' ').toLocaleLowerCase('en-GB');
  const unique = value => {
    if (!key(value) || seen.has(key(value))) return false;
    seen.add(key(value)); return true;
  };
  const row = (label, value) => {
    if (value && unique(value)) section.append(A(E('div', null, 'brief-part'), E('h4', label), L('p', value)));
  };
  // The canonical factual projection survives routine reads. Never substitute generated
  // approach/message text for its fields or refresh their original dates from a later check.
  const values = decision ? jobDecisionFacts(record) : null;
  if (decision) {
    section.append(checkLabel(record.evidence_checks?.vacancy, 'vacancy', record));
    const terms = E('dl', null, 'job-terms');
    for (const [label,value] of [['Pay',values.pay],['Engagement',values.engagement],['Location',values.location],['IR35',record.ir35]])
      if(value)terms.append(A(E('div'),E('dt',label),E('dd',value)));
    section.append(terms);
    const remit = A(E('div', null, 'brief-part decision-remit'), E('h4', 'Remit'));
    remit.append(values.remit.url ? A(E('p'), extLink(values.remit.url, `“${values.remit.text}”`))
      : E('p', values.remit.text || 'Work and requirements not established.'));
    section.append(remit); unique(values.remit.text);
    row('Fit', values.fit);
    section.append(decisionBlockerLabel(record));
  } else {
    row('Remit', record.summary || (briefing.facts?.length ? 'Recorded requirements are below.' : 'Specific work and requirements are not established.'));
    const factors = (record.relevance?.reasons || []).filter(r => r.weight > 0);
    row('Fit', [...factors.filter(r => r.fit), ...factors.filter(r => !r.fit)].slice(0, 2).map(r => r.text).join(' · ') || current?.fit?.reason);
    row('Terms', [engagementLabel(record), record.pay || 'Pay not stated',
      jobLocation(record) || 'Location not stated', record.ir35].filter(Boolean).join(' · '));
    if (record.type === 'job') section.append(checkLabel(record.evidence_checks?.vacancy, 'vacancy', record));
  }
  const sources = A(E('details', null, 'work-evidence-sources'), E('summary', 'Demand evidence and sources'));
  if(decision && record.summary && record.summary !== values.remit.text)
    sources.append(A(E('div',null,'brief-part'),E('h4','Full recorded remit'),L('p',record.summary)));
  const provenance = (field, target) => {
    const value = briefing.provenance?.[field];
    const box = E('div', null, 'brief-field-provenance'); box.dataset.field = field;
    if (!value) box.append(E('small', 'Original source and review dates not recorded.', 'muted'));
    else {
      box.dataset.sourceDate = value.source_date || ''; box.dataset.reviewedAt = value.reviewed_at || '';
      box.append(E('small', `Source date: ${value.source_date ? statedDate(value.source_date) : 'not recorded'} · Reviewed: ${value.reviewed_at ? statedDate(value.reviewed_at) : 'not recorded'} · Review date does not verify current availability.`, 'muted'));
      for (const url of [...new Set(value.source_urls || [])]) box.append(extLink(url, 'Source ↗'));
    }
    target.append(box);
  };
  const field = (name, label, values) => {
    const valuesToShow = values.filter(unique);
    if (!values.length) return;
    const box = A(E('section', null, 'briefing-field'), E('h4', label)); box.dataset.field = name;
    valuesToShow.forEach(value => box.append(L('p', value)));
    if (!valuesToShow.length) box.append(E('p', 'This evidence is already shown above.', 'muted'));
    provenance(name, box); sources.append(box);
  };
  field('facts', 'Recorded role evidence', briefing.facts || []);
  field('public_contact_route', 'Advert contact', briefing.public_contact_route ? [briefing.public_contact_route] : []);
  if (record.relevance?.fit?.requirements?.length) {
    const comparison = A(E('section', null, 'requirement-comparison'), E('h4', 'Requirements against recorded experience'));
    for (const item of record.relevance.fit.requirements.slice(0, 8)) {
      const presentation = requirementPresentation(item);
      const entry = A(E('div', null, 'brief-evidence-item'), E('strong', `${presentation.importance} · ${presentation.status}`),
        E('blockquote', presentation.quote));
      if (item.alternatives?.length > 1) entry.append(E('p', `Alternatives stated: ${item.alternatives.join(' or ')}`, 'muted'));
      if (item.url) entry.append(extLink(item.url, 'Requirement source ↗'));
      if (item.date) entry.append(E('small', ` · ${statedDate(item.date)}`, 'muted'));
      if (item.proof_url) entry.append(extLink(item.proof_url, 'Sourced experience ↗'));
      comparison.append(entry);
    }
    sources.append(comparison);
  }
  for (const evidence of record.relevance?.fit?.evidence || []) {
    const entry = A(E('div', null, 'brief-evidence-item'), E('h4', evidence.label || 'Experience comparison'));
    if (evidence.quote) entry.append(E('blockquote', evidence.quote));
    if (evidence.url) entry.append(extLink(evidence.url, 'Advert evidence ↗'));
    if (evidence.date) entry.append(E('small', ` · Source date: ${statedDate(evidence.date)}`, 'muted'));
    if (evidence.proof) entry.append(L('p', evidence.proof));
    if (evidence.proof_url) entry.append(extLink(evidence.proof_url, 'Sourced experience ↗'));
    sources.append(entry);
  }
  for (const item of brief?.items || []) {
    if (['summary', 'engagement', 'pay', 'location', 'ir35'].includes(item.kind) || !unique(item.value)) continue;
    const entry = A(E('div', null, 'brief-evidence-item'), E('strong', item.kind?.replaceAll('_', ' ') || 'Evidence'), L('p', item.value),
      E('small', `${item.provenance === 'public_source' ? 'Public source' : 'Recorded'} · ${item.at ? statedDate(item.at) : 'Date not recorded'}`, 'muted'));
    if (item.source_url) entry.append(extLink(item.source_url, 'Evidence source ↗'));
    sources.append(entry);
  }
  for (const fact of current?.brief?.facts || []) {
    if (!unique(fact.text)) continue;
    const entry = A(E('div', null, 'brief-evidence-item'), L('p', fact.text), E('small', fact.date ? statedDate(fact.date) : 'Date not recorded', 'muted'));
    if (fact.url) entry.append(extLink(fact.url, 'Evidence source ↗'));
    sources.append(entry);
  }
  for (const review of record.research || []) {
    const entry = E('div', null, 'research-note');
    for (const [name, label] of [['facts', 'Facts'], ['assumptions', 'Assumptions']]) {
      const values = (review[name] || []).filter(unique);
      if (values.length) entry.append(L('p', `${label}: ${values.join('; ')}`));
    }
    if (!entry.childElementCount) continue;
    entry.prepend(E('small', `Reviewed ${statedDate(review.reviewed_at || review.observed_at)}`));
    for (const url of [...new Set(review.source_urls || [])]) entry.append(extLink(url, 'Research source ↗'));
    sources.append(entry);
  }
  if (sources.childElementCount === 1) sources.append(E('p', 'No specific public demand evidence is recorded.', 'muted'));
  section.append(sources);
  const reviews = record.type === 'agency' && (record.research || []).some(r => r.verification === 'primary-read')
    ? (record.research || []).filter(r => r.verification === 'primary-read')
    : (record.research || []);
  const unknowns = [...(record.relevance?.fit?.gaps || []), ...(briefing.decisive_unknowns || []), ...(brief?.gaps || []).map(g => g.text),
    ...(record.relevance?.qualification?.conflicts || []), ...reviews.flatMap(review => review.decisive_unknowns || [])].filter(unique);
  if (unknowns.length) {
    const box = A(E('div', null, 'brief-part work-unknowns'), E('h4', 'What is still missing for a decision'), A(E('ul'), unknowns.map(value => L('li', value))));
    if (briefing.decisive_unknowns?.length) provenance('decisive_unknowns', box);
    section.append(box);
  }
  return section;
}
function agencyContext(agency, state, isExcluded) {
  const records = new Map((state.records || []).filter(r => !r.archived_at).map(r => [r.id, r]));
  const consultants = [], roles = [];
  for (const link of state.links || []) {
    const otherId = link.from_id === agency.id ? link.to_id : link.to_id === agency.id ? link.from_id : null;
    const other = otherId ? records.get(otherId) : null;
    if (!other || blocked(other, isExcluded)) continue;
    if (other.type === 'person' && ['consultant_at', 'works_at', 'related'].includes(link.relation)) {
      if (!consultants.some(p => p.id === other.id)) consultants.push(other);
    } else if (['job', 'tender'].includes(other.type) && ['hiring_for', 'advertised_by'].includes(link.relation)) {
      if (confirmedOpenAgencyRole(other) && !roles.some(j => j.id === other.id)) roles.push(other);
    }
  }
  roles.sort((a, b) => Number(['contract', 'interim', 'ftc'].includes(b.engagement)) - Number(['contract', 'interim', 'ftc'].includes(a.engagement))
    || (b.relevance?.score || 0) - (a.relevance?.score || 0) || a.id.localeCompare(b.id));
  const primary = (agency.research || []).filter(r => r.verification === 'primary-read');
  const review = primary.find(r => r.desk_summary || r.summary) || (agency.research || [])[0] || null;
  const unknowns = [...new Set((primary.length ? primary : agency.research || []).flatMap(r => r.decisive_unknowns || []).filter(Boolean))];
  return { consultants, roles, review, unknowns };
}
export function agencyApproachBrief(agency, state, { progress, current, openRecord, openJob = openRecord, isExcluded }) {
  const section = E('section', null, 'approach-brief agency-approach-brief');
  section.setAttribute('aria-label', 'Agency desk and approach');
  section.append(E('h3', 'Agency desk and approach'));
  const { consultants, roles, review, unknowns } = agencyContext(agency, state, isExcluded);
  const deskText = review?.desk_summary || review?.summary || agency.summary || '';
  if (deskText) {
    const part = A(E('div', null, 'brief-part'), E('h4', 'Specialist desk'), L('p', deskText));
    if (review?.reviewed_at || review?.observed_at)
      part.append(E('small', `Reviewed ${statedDate(review.reviewed_at || review.observed_at)}`, 'muted'));
    for (const url of [...new Set(review?.source_urls || [])]) part.append(extLink(url, 'Desk source ↗'));
    section.append(part);
  }
  const routeText = review?.public_contact_route || agency.briefing?.public_contact_route || '';
  if (consultants.length || routeText) {
    const routeBox = A(E('div', null, 'brief-part agency-contacts'),
      E('h4', consultants.length ? 'Consultant and contact route' : 'Contact route'));
    for (const person of consultants) {
      const item = personApproach(person, state);
      const card = E('div', null, 'contact-route');
      const name = button(person.name, () => openRecord(person.id), 'linkish contact-name');
      name.dataset.contactId = person.id;
      const meta = [person.role || person.linkedin?.observed_role,
        person.relationship && person.relationship !== 'cold' ? `Relationship: ${person.relationship}` : ''].filter(Boolean).join(' · ');
      card.append(name, meta ? E('span', ` · ${meta}`, 'muted') : null);
      if (person.known_context) card.append(L('p', bounded(person.known_context, 360), 'brief-known-context'));
      const reach = progress.contact(item);
      if (reach) card.append(reach);
      routeBox.append(card);
    }
    if (routeText) routeBox.append(L('p', routeText, 'brief-contact-basis'));
    section.append(routeBox);
  }
  if (roles.length) {
    const roleBox = E('div', null, 'brief-part agency-roles');
    let signature;
    roleBox.refreshAgencyRoles = (now = Date.now()) => {
      const currentRoles = roles.filter(job => confirmedOpenAgencyRole(job,now));
      const next = JSON.stringify(currentRoles.map(job => job.id));
      if (next === signature) return;
      signature = next; roleBox.hidden = !currentRoles.length; roleBox.replaceChildren();
      roleBox.append(E('h4', `Confirmed open roles (${currentRoles.length})`));
      for (const job of currentRoles.slice(0, 3)) {
        const terms = [engagementLabel(job), job.pay, job.ir35].filter(Boolean).join(' · ');
        const link = button(job.name, () => openJob(job.id), 'linkish'); link.dataset.roleId = job.id;
        roleBox.append(A(E('p'), link, checkLabel(job.evidence_checks?.vacancy, 'vacancy', job), terms ? E('small', ` · ${terms}`, 'muted') : null));
      }
    };
    roleBox.refreshAgencyRoles();
    section.append(roleBox);
  }
  if (unknowns.length) {
    section.append(A(E('div', null, 'brief-part work-unknowns'), E('h4', 'What to confirm with this desk'),
      A(E('ul'), unknowns.map(u => L('li', u)))));
  }
  if (blocked(agency, isExcluded)) {
    section.append(E('p', 'Excluded from new work.', 'muted'));
    return section;
  }
  if (current?.progress) section.append(progress.block(current));
  return section;
}
export function jobApproachBrief(job,state,options) {
  const section=E('section',null,'approach-brief job-approach-brief');section.setAttribute('aria-label','Job assessment and progress');
  section.append(workAssessment(job,options.current,{decision:true}));
  // Keep the freshness source route alongside its one canonical status.
  const vacancy=E('div',null,'vacancy-evidence');vacancy.append(checkLabel(job.evidence_checks?.vacancy,'vacancy',job));
  if(job.evidence_checks?.vacancy?.source_url)vacancy.append(extLink(job.evidence_checks.vacancy.source_url,'Source ↗'));
  section.querySelector('.evidence-check')?.replaceWith(vacancy);
  if(options.incoming)section.append(options.incoming);
  section.append(options.jobControls(job));
  const contacts=E('div',null,'job-contacts');contacts.setAttribute('aria-label','Connections');
  const unavailable=blocked(job,options.isExcluded);
  const routeIndex=unavailable?null:options.routeIndex || contactRouteIndex(state);
  const routes=unavailable?[]:contactRoutes(job,state,routeIndex).filter(route=>!blocked(route.person,options.isExcluded));
  if(routes.length){const more=A(E('details'),E('summary',`Connections (${routes.length})`));for(const route of routes)more.append(routeCard(route,state,job,options));contacts.append(more);}
  else if(unavailable) contacts.append(E('p',`Contact routes are hidden for this ${job.archived_at?'archived':'excluded'} role.`,'muted'));
  else {
    const employerRoute=employerContactRoute(job,state,{isExcluded:options.isExcluded});
    if(employerRoute) contacts.append(E('h4','General employer route'),
      E('p',`No named contact is shown. ${employerRoute.company.name} publishes this general contact route; ask who handles this vacancy.`),
      L('p',bounded(employerRoute.text)),
      E('small',`Reviewed ${statedDate(employerRoute.reviewedAt)} · General employer route, not a named hiring contact. `,'muted'),
      extLink(employerRoute.sourceUrl,'Employer source ↗'));
    else contacts.append(E('p',`No named contact is shown for this role. ${extLink(job.url,'Advert').tagName==='A'
      ? 'Check the advert for a hiring contact.' : 'Add an advert or a sourced hiring contact to continue.'}`,'muted'));
  }
  if (!unavailable) {
    const employer = job.organisation || (routeIndex.linksByRecord.get(job.id) || [])
      .filter(link => link.relation === 'hiring_for')
      .map(link => routeIndex.records.get(link.from_id === job.id ? link.to_id : link.from_id))
      .find(record => record?.type === 'company' && !blocked(record,options.isExcluded))?.name;
    const search = new URL('https://www.linkedin.com/search/results/people/');
    search.searchParams.set('keywords', [employer, job.name, 'hiring'].filter(Boolean).join(' '));
    const find = extLink(search.href, 'Find the hiring manager'); find.className = 'find-hiring-contact';
    contacts.append(E('p', 'A connection or confirmed contact route alone does not establish who handles this hire.', 'muted'), find);
  }
  section.append(contacts);return section;
}
export function personApproachBrief(person,state,{progress,current,openRecord,openJob = openRecord,isExcluded,personListControls,context}) {
  const section=E('section',null,'approach-brief person-approach-brief');section.setAttribute('aria-label','Relationship and contact history');
  section.append(E('h3','Do this next'));
  if(blocked(person,isExcluded)){
    section.append(E('p',person.relationship==='do_not_contact'
      ? 'On your Not contacting list. Excluded from contact routes, agency desks, outreach and research unless you promote them to current contacts.'
      : 'Excluded from new work.','muted'));
    if (personListControls) section.append(personListControls(person));
    return section;
  }
  const entry = context || peopleContext(state).byId.get(person.id);
  const action = current?.progress && current.id !== entry?.item?.id
    ? personAction(person,current.progress) : entry?.action;
  if(current?.progress) section.append(progress.block(current,{next:action?.next || 'No action is queued for this contact.'}));
  else {
    section.append(E('p',action?.next || 'Choose a role or record the next step when you agree one.','approach-next'));
    const reach=progress.contact(personApproach(person,state));if(reach)section.append(reach);
  }
  if (entry?.roles.length) {
    const roles = A(E('div',null,'brief-part person-roles'),E('h4','Roles you could discuss'));
    const renderRole = ({job,bases,actNow}) => {
      const link=button(job.name,()=>openJob(job.id),'linkish');link.dataset.roleId=job.id;
      return A(E('p'),link,E('small',`${actNow ? ' · Act now' : ''} · ${bases.join(' · ')}`,'muted'));
    };
    entry.roles.slice(0,3).forEach(role=>roles.append(renderRole(role)));
    if(entry.roles.length>3) {
      const more=A(E('details',null,'person-more-roles'),E('summary',`More roles (${entry.roles.length-3})`));
      const list=E('div'); let shown=3;
      const next=button('Show more',()=>{
        entry.roles.slice(shown,shown+12).forEach(role=>list.append(renderRole(role)));
        shown=Math.min(shown+12,entry.roles.length);
        next.hidden=shown===entry.roles.length;
        next.textContent=`Show more (${entry.roles.length-shown})`;
      });
      more.append(list,next);
      more.addEventListener('toggle',()=>{if(more.open && shown===3)next.onclick();});
      roles.append(more);
    }
    roles.append(E('small','A recorded connection does not establish hiring authority. Confirm their connection to the role.','muted'));
    section.append(roles);
  }
  const background=A(E('details',null,'person-relationship-context'),E('summary','Relationship and source context'));
  if(person.known_context || person.summary)background.append(L('p',person.known_context || person.summary,'brief-known-context'));
  const agencies = (state.links || []).filter(l => l.relation === 'consultant_at' && (l.from_id === person.id || l.to_id === person.id))
    .map(l => (state.records || []).find(r => r.id === (l.from_id === person.id ? l.to_id : l.from_id) && r.type === 'agency' && !r.archived_at))
    .filter(Boolean);
  for (const agency of agencies.slice(0, 1)) {
    const { review } = agencyContext(agency, state, isExcluded);
    const box = A(E('div', null, 'brief-part person-agency-context'),
      A(E('p'), E('strong', 'Agency desk: '), button(agency.name, () => openRecord(agency.id), 'linkish')));
    const desk = review?.desk_summary || review?.summary || '';
    if (desk && desk !== person.summary) box.append(L('p', desk));
    if (review?.public_contact_route) box.append(L('p', review.public_contact_route, 'brief-contact-basis'));
    background.append(box);
  }
  if(background.childNodes.length>1) section.append(background);
  if (personListControls) section.append(personListControls(person));
  return section;
}
