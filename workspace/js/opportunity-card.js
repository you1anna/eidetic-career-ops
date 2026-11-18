// A decision brief renders server-owned ranking and source evidence, never a client-side score.
import {E, A, extLink} from './dom.js';
import {statedDate, isExcluded, engagementLabel, cardRemit, jobDecisionFacts, jobLocation} from './record-presentation.js';
import {checkLabel} from './job-evidence.js';
import {workAssessment} from './approach-brief.js';
import {jobStatusButton} from './job-actions.js';
import {createCompanyLogos} from './company-logo.js';
import {researchAttemptText} from './research-status.js';

export function cardExplanation(record, ref) {
  const factors = record.relevance?.reasons || [];
  const reasons = [...new Set([ref.reason, ...factors.filter(r => r.fit && r.weight > 0).map(r => r.text),
    ...factors.filter(r => r.weight > 0).map(r => r.text)].filter(Boolean))].slice(0, 2);
  const uncertainty = ref.uncertainty || record.relevance?.fit?.gaps?.[0]
    || record.relevance?.qualification?.conflicts?.[0] || record.briefing?.decisive_unknowns?.[0] || '';
  return {reasons, uncertainty};
}

export {cardRemit} from './record-presentation.js';

export function sourcePresentation(record, ref) {
  if (ref.source) return {label: ref.source.label || 'Recorded source', at: ref.source.at || '',
    url: ref.source.url || '', basis: ref.source.basis || ''};
  const source = record.source_file || '', availability = record.availability || {};
  const label = source.includes('alert:linkedin') ? 'LinkedIn email'
    : source.includes('alert:') ? 'Job alert email'
    : source.includes('feed:adzuna') ? 'Adzuna' : source.includes('feed:reed') ? 'Reed'
    : source.includes('board:') ? 'Employer job board' : record.incoming ? 'Recorded incoming role' : 'Recorded source';
  return {label, url: record.detail_url || record.url || '', at: availability.at || '', basis: availability.basis || ''};
}


export {researchAttemptText as researchStatus} from './research-status.js';

export function createOpportunityCards({getState, openRecord, mutate, canLeave, jobStatusSaved, showAlert}) {
  const logos = createCompanyLogos(), expanded = new Set();
  function identityBlock(record, existingTitle = null) {
    const identity = E('div', null, 'opportunity-identity'), heading = E('div', null, 'opportunity-heading');
    const company = record.organisation || (['company','agency'].includes(record.type) ? record.name : 'Company not stated');
    const title = existingTitle || E('button', record.name || 'Untitled', 'linkish opportunity-title'); title.type = 'button';
    if (!existingTitle) title.onclick = () => openRecord(record.id);
    title.textContent = record.name || 'Untitled';
    heading.append(A(E('h3'), title), E('p', company, 'opportunity-company'));
    const image = getState().company_logos?.[record.logo_company_id];
    identity.append(logos.logo(company, image), heading);
    return identity;
  }
  function factBlock(record, ref) {
    const box = E('div', null, 'opportunity-facts');
    const remit = cardRemit(record);
    if (remit.text) box.append(remit.url
      ? A(E('p', null, 'opportunity-remit'), extLink(remit.url, `“${remit.text}”`))
      : E('p', remit.text, 'opportunity-remit'));
    box.append(E('p', record.pay || 'Pay not stated', 'opportunity-pay'),
      E('p', [engagementLabel(record),
        jobLocation(record)].join(' · '), 'opportunity-terms'));
    const origin = sourcePresentation(record, ref), source = E('div', null, 'opportunity-source');
    const wording = origin.at ? `${origin.label} · ${['receipt','email'].includes(origin.basis) ? 'received ' : ''}${statedDate(origin.at)}` : origin.label;
    source.append(origin.url ? extLink(origin.url, `${wording} ↗`) : E('span', wording));
    if (record.type === 'job') source.append(checkLabel(record.evidence_checks?.vacancy, 'vacancy', record));
    box.append(source);
    return box;
  }
  function enrichProgress(box, record, ref, {decision = false, item = null} = {}) {
    const heading = box.querySelector(':scope > h3'), title = heading?.querySelector('button');
    if (heading) heading.replaceWith(identityBlock(record, title));
    box.classList.add('week-progress-brief');
    const facts = factBlock(record, ref), next = box.querySelector('.approach-next');
    if (decision && record.type === 'job') {
      const values = jobDecisionFacts(record), progress = item?.progress;
      const uncertainty = values.uncertainty || ref.uncertainty;
      const fit = `Fit: ${values.fit}${uncertainty ? ` · Still unknown: ${uncertainty}` : ''}`;
      // A human commitment or reply always wins, even if an older payload carries a default flag.
      const defaultStep = progress?.next_is_default && !progress.reply_to_record
        && !['mine','waiting'].includes(progress.next_step?.kind);
      if (defaultStep && next) next.textContent = fit;
      facts.querySelector('.opportunity-pay').textContent = [values.pay, values.engagement, values.location].join(' · ');
      facts.querySelector('.opportunity-terms').remove();
      if (!values.remit.text) facts.prepend(E('p', 'Work and requirements not established.', 'opportunity-remit'));
      if (!defaultStep) facts.querySelector('.opportunity-remit').append(E('span', ` ${fit}`, 'opportunity-fit'));
    } else if (ref.uncertainty) facts.append(E('p', `Still unknown: ${ref.uncertainty}`, 'opportunity-uncertainty'));
    (next || box.querySelector('.opportunity-identity')).after(facts);
    return box;
  }
  function card(record, ref, {checking = false, position = null, item = null} = {}) {
    const box = E('article', null, 'opportunity-card' + (checking ? ' needs-checking-card' : ''));
    box.dataset.recordId = record.id; box.dataset.opportunityId = record.id; box.dataset.opportunityLane = ref.lane || '';
    if (ref.action_id) box.dataset.actionId = ref.action_id;
    const fit = record.relevance?.fit?.status;
    const badge = checking ? 'Promising · needs checking' : fit === 'supported' ? 'Supported fit' : 'Worth considering';
    box.append(E('span', `${position ? position + ' · ' : ''}${badge}`, 'opportunity-label'), identityBlock(record));
    box.append(factBlock(record, ref));
    const origin = sourcePresentation(record, ref);
    const explanation = cardExplanation(record, ref);
    const why = A(E('div', null, 'opportunity-why'), E('h4', checking ? 'Why check this' : 'Why this is near the top'));
    if (explanation.reasons.length) why.append(A(E('ul'), explanation.reasons.map(reason => E('li', reason))));
    if (explanation.uncertainty) why.append(E('p', `Still unknown: ${explanation.uncertainty}`, 'opportunity-uncertainty'));
    if (checking) why.append(E('p', researchAttemptText(ref.research_status), 'opportunity-research muted'));
    box.append(why);
    const evidence = A(E('details', null, 'opportunity-evidence'), E('summary', checking ? 'Source details' : 'Evidence and comparisons'));
    evidence.dataset.persistKey = `evidence:${record.id}`;
    let built = false;
    const build = () => { if (!built) { built = true; evidence.append(workAssessment(record, item)); } };
    evidence.open = expanded.has(record.id); if (evidence.open) build();
    evidence.addEventListener('toggle', () => {
      if (evidence.open) { expanded.add(record.id); build(); } else expanded.delete(record.id);
    });
    box.append(evidence);
    const actions = E('div', null, 'opportunity-actions');
    const status = E('p', '', 'opportunity-status'); status.setAttribute('role', 'status');
    const transitions = record.type === 'job' ? record.transitions?.status || [] : [];
    const options = {mutate, canLeave, saved: jobStatusSaved,
      failed: message => { status.textContent = message; if (!status.isConnected) showAlert(message); }};
    if (!isExcluded(record) && transitions.includes('pursue'))
      actions.append(jobStatusButton(record, 'pursue', options, 'Shortlist', 'primary'));
    if (origin.url) actions.append(extLink(origin.url, origin.label === 'LinkedIn email' ? 'Open LinkedIn ↗' : record.type === 'job' ? 'Open advert ↗' : 'Open source ↗'));
    if (transitions.includes('skip')) actions.append(jobStatusButton(record, 'skip', options, 'Skip', 'linkish'));
    if (!actions.childElementCount) {
      const open = E('button', 'Open brief'); open.type = 'button'; open.onclick = () => openRecord(record.id); actions.append(open);
    }
    box.append(actions, status);
    return box;
  }
  return {card, enrichProgress, reset: logos.reset};
}
