// Explicit source checks, independent of publication dates, record edits and scanner activity.
import { E, A, L, extLink } from './dom.js';
import {jobDecisionFacts} from './record-presentation.js';
import {sourceCheckText, researchTime} from './research-status.js';

const DAY = 24 * 60 * 60 * 1000;
const MAX_FUTURE_MS = 5 * 60 * 1000;
const ZONED = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
// Same closed-source vocabulary as config/relevance.yml; the retained test reads that list.
const GONE_LIFECYCLES = new Set(['closed', 'expired', 'removed', 'historical']);
const LONDON_DATE = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit',
});
function vacancyConflict(job, now) {
  if (GONE_LIFECYCLES.has(job?.source_lifecycle))
    return `source reports ${job.source_lifecycle}`;
  // Stored factual deadlines may include a time; existing relevance uses their leading date.
  const deadline = String(job?.published_deadline || '').slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(deadline) && deadline < LONDON_DATE.format(new Date(now)))
    return `closing date ${deadline} has passed`;
  return '';
}
// Shared by agency lists, briefs and People. An explicit work selection can override discovery
// fit; it cannot revive a closed, excluded or duplicate role. Default callers retain suitability.
export function relevantAgencyRole(job, now = Date.now(), { selected = false } = {}) {
  const rel = job?.relevance || {}, qualification = rel.qualification || {};
  const relevant = (rel.score || 0) >= 35 || (!Object.hasOwn(rel, 'score') && ['strong', 'possible'].includes(rel.signal_band || rel.band));
  return ['job', 'tender'].includes(job?.type) && !job.archived_at && !job.exclusion && !job.exclusion_contexts?.length
    && !['closed', 'rejected', 'skip', 'lost', 'declined'].includes(job.status)
    && !rel.duplicate_of
    && job.evidence_checks?.vacancy?.status !== 'closed' && job.availability?.status !== 'closed'
    && !vacancyConflict(job, now)
    && (selected || (qualification.eligible !== false && qualification.domain !== 'outside'
      && !qualification.conflicts?.length && (rel.signal_band || rel.band) !== 'low' && relevant));
}
// Call recommendations need a current explicit vacancy check. Listing sightings remain
// useful directory evidence, but do not by themselves establish a role to discuss today.
export function confirmedOpenAgencyRole(job, now = Date.now()) {
  if (!relevantAgencyRole(job, now)) return false;
  if (job.availability && job.availability.basis !== 'research') return false;
  const at = job.availability ? job.availability.at : job.evidence_checks?.vacancy?.checked_at;
  const stamp = ZONED.test(at || '') ? Date.parse(at) : NaN;
  return Number.isFinite(stamp) && stamp <= now + MAX_FUTURE_MS
    && labelStatus(job.evidence_checks?.vacancy, 'vacancy', now, job).fresh;
}
function hoursAgo(stamp, now) {
  const hours = Math.max(0, (now - stamp) / 3600000);
  return hours < 1 ? 'less than 1h ago' : `${Math.floor(hours)}h ago`;
}
export function checkStatus(check, scope = 'vacancy', now = Date.now(), job = null) {
  const advert = scope === 'vacancy', subject = advert ? 'Advert' : 'Contact route';
  const stamp = check && ZONED.test(check.checked_at || '') ? Date.parse(check.checked_at) : NaN;
  if (!check || !Number.isFinite(stamp) || stamp > now + MAX_FUTURE_MS)
    return { label: `${subject} not verified`, tone: 'unknown', fresh: false };
  const ago = hoursAgo(stamp, now);
  if (check.status === 'unverified')
    return { label: `${advert ? 'Availability' : 'Contact route'} unverified · attempted ${ago}`, tone: 'unknown', fresh: false };
  if (check.status === 'closed' && advert)
    return { label: `Advert reported closed · checked ${ago}`, tone: 'closed', fresh: false };
  if (check.status !== 'confirmed')
    return { label: `${subject} not verified`, tone: 'unknown', fresh: false };
  const conflict = advert && vacancyConflict(job, now);
  if (conflict)
    return { label: `Advert needs recheck · ${conflict}`, tone: 'stale', fresh: false };
  if (now - stamp >= DAY)
    return { label: `${subject} needs recheck · last checked ${ago}`, tone: 'stale', fresh: false };
  return { label: `${advert ? 'Advert open' : 'Contact route confirmed'} · checked ${ago}`, tone: 'fresh', fresh: true };
}

// Advert availability is decided on the server (evidence_checks.advert_availability), including
// `until`, when an open answer lapses. The page names the basis and formats only its age.
const SOURCE_NAMES = { 'feed:adzuna': 'Adzuna', 'feed:reed': 'Reed' };
const BOARD_NAMES = { greenhouse: 'Greenhouse', lever: 'Lever', ashby: 'Ashby', smartrecruiters: 'SmartRecruiters' };
const LONDON_DAY = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', day: 'numeric', month: 'short' });
function sourceName(source) {
  if (SOURCE_NAMES[source]) return SOURCE_NAMES[source];
  const [kind, provider] = String(source || '').split(':');
  if (kind === 'board') return `the employer's ${BOARD_NAMES[provider] ? BOARD_NAMES[provider] + ' ' : ''}job board`;
  return kind === 'hn' ? 'Hacker News' : kind === 'tender' ? 'the tender service' : 'its source';
}
function age(at, now) {
  const stamp = Date.parse(at || '');
  if (!Number.isFinite(stamp)) return '';
  return now - stamp < 2 * DAY ? hoursAgo(stamp, now) : `on ${LONDON_DAY.format(stamp)}`;
}
// A page left open keeps ageing the server's answer: open lapses at `until`, or when the London
// closing date passes (the only vacancyConflict that can arise after the server said open).
function aged(job, now) {
  const a = job?.availability || {};
  if (a.status !== 'open') return a;
  if (vacancyConflict(job, now)) return { ...a, status: 'closed', basis: 'deadline' };
  return now < Date.parse(a.until || '') ? a : { ...a, status: 'unconfirmed' };
}
export function availabilityStatus(job, now = Date.now()) {
  const a = aged(job, now), when = age(a.at, now), day = Number.isFinite(Date.parse(a.at || '')) ? LONDON_DAY.format(Date.parse(a.at)) : '';
  if (a.status === 'open')
    return { label: a.basis === 'listing' ? `Still listed on ${sourceName(a.source)} · seen ${when}` : `Advert open · checked ${when}`, tone: 'fresh', fresh: true };
  if (a.status === 'closed') {
    const label = a.basis === 'research' ? `Advert reported closed · checked ${when}`
      : `Advert closed · ${vacancyConflict(job, now) || (a.basis === 'deadline' ? 'closing date has passed' : 'source reports closed')}`;
    return { label, tone: 'closed', fresh: false };
  }
  if (a.basis === 'listing') return { label: `Not listed by ${sourceName(a.source)} since ${day}`, tone: 'stale', fresh: false };
  if (a.basis === 'research') return { label: `Last confirmed open ${day}`, tone: 'stale', fresh: false };
  const checked = job?.evidence_checks?.vacancy?.checked_at;
  const stamp = ZONED.test(checked || '') ? Date.parse(checked) : NaN;
  return { label: `Open unknown · ${Number.isFinite(stamp) && stamp <= now + MAX_FUTURE_MS ? 'last check ' + age(checked, now) : 'not checked yet'}`, tone: 'unknown', fresh: false };
}
// Records without a server availability (older payloads) keep the research-check label.
function labelStatus(check, scope, now, job) {
  return scope === 'vacancy' && job?.availability ? availabilityStatus(job, now) : checkStatus(check, scope, now, job);
}

export function decisionBlockers(job, now = Date.now()) {
  const status = labelStatus(job.evidence_checks?.vacancy, 'vacancy', now, job);
  return [...(!status.fresh && status.tone !== 'closed' ? ['advert open not confirmed'] : []), ...jobDecisionFacts(job).missing];
}

function refreshDecisionBlockers(node, now = Date.now()) {
  const blockers = decisionBlockers(node.checkRecord, now);
  node.textContent = blockers.length ? `Can't decide yet because: ${blockers.join('; ')}.` : '';
  node.hidden = !blockers.length;
}

export function decisionBlockerLabel(job) {
  const node = E('p', null, 'decision-blockers opportunity-uncertainty'); node.checkRecord = job;
  refreshDecisionBlockers(node); return node;
}

export function refreshCheckLabels(root = document) {
  const now = Date.now();
  // These read-only containers own their role membership; refreshing them must not replace
  // the surrounding detail, activity controls or an unsaved form.
  root.querySelectorAll('.people-agencies,.agency-roles').forEach(node => node.refreshAgencyRoles?.(now));
  root.querySelectorAll('.evidence-check').forEach(node => {
    // Dated private provenance is not a public check and has no expiry to refresh.
    if (!node.checkScope) return;
    const status = labelStatus(node.checkEvidence, node.checkScope, now, node.checkRecord);
    node.textContent = status.label;
    node.className = `evidence-check evidence-${status.tone}`;
    node.dataset.fresh = String(status.fresh);
    node.title = node.checkEvidence?.checked_at ? `Checked ${researchTime(node.checkEvidence.checked_at)}. ${sourceCheckText(node.checkEvidence.basis)}` : 'No explicit source check has been recorded.';
  });
  root.querySelectorAll('.decision-blockers').forEach(node => refreshDecisionBlockers(node, now));
}

export function checkLabel(check, scope = 'vacancy', job = null) {
  const node = E('span', null, 'evidence-check');
  node.checkEvidence = check; node.checkScope = scope; node.checkRecord = job;
  const status = labelStatus(check, scope, Date.now(), job);
  node.textContent = status.label; node.className += ` evidence-${status.tone}`;
  node.dataset.fresh = String(status.fresh);
  node.title = check?.checked_at ? `Checked ${researchTime(check.checked_at)}. ${sourceCheckText(check.basis)}` : 'No explicit source check has been recorded.';
  return node;
}

// Build once for a directory projection. Organisation adjacency includes only person links:
// many vacancies at one employer must not rescan every sibling vacancy for every route.
export function contactRouteIndex(state) {
  const records = new Map((state.records || []).map(r => [r.id, r]));
  const linksByRecord = new Map(), peopleByOrganisation = new Map();
  const add = (map, id, value) => {
    if (!map.has(id)) map.set(id, []);
    map.get(id).push(value);
  };
  for (const link of state.links || []) {
    const a = records.get(link.from_id), b = records.get(link.to_id);
    if (!a || !b) continue;
    const edge = { from_id: a.id, to_id: b.id, relation: link.relation, notes: link.notes };
    add(linksByRecord, a.id, edge);
    if (a.id !== b.id) add(linksByRecord, b.id, edge);
    for (const [person, organisation] of [[a, b], [b, a]])
      if (person.type === 'person' && ['company', 'agency'].includes(organisation.type))
        add(peopleByOrganisation, organisation.id, edge);
  }
  return { records, linksByRecord, peopleByOrganisation };
}

// Summary records and links are replaced atomically by the app. Keep the uncached builder
// above available for mutable callers; a view owns this lookup for its current snapshot.
export function createContactRouteLookup({ getState }) {
  let records, links, current;
  const index = () => {
    const state = getState();
    if (!current || records !== state.records || links !== state.links) {
      current = contactRouteIndex(state);
      records = state.records; links = state.links;
    }
    return current;
  };
  return { index, forRecord: record => contactRoutes(record, getState(), index()) };
}

export function contactRoutes(job, state, index = contactRouteIndex(state)) {
  const { records, linksByRecord, peopleByOrganisation } = index;
  const organisations = new Map();
  if (['company', 'agency'].includes(job.type)) organisations.set(job.id, job);
  const routes = new Map(), checks = new Map();
  for (const check of job.evidence_checks?.contacts || []) {
    if (!checks.has(check.person_id)) checks.set(check.person_id, []);
    checks.get(check.person_id).push(check);
  }
  const add = (person, basis, notes, organisationId = null) => {
    if (!person || person.type !== 'person' || person.archived_at || person.exclusion || person.relationship === 'do_not_contact') return;
    if (!routes.has(person.id)) {
      routes.set(person.id, { person, bases: new Set(), notes: new Set(), organisationIds: new Set(), direct: false });
    }
    const route = routes.get(person.id); route.bases.add(basis);
    if (organisationId) route.organisationIds.add(organisationId);
    else route.direct = true;
    if (notes) route.notes.add(notes);
  };
  for (const link of linksByRecord.get(job.id) || []) {
    const other = link.from_id === job.id ? link.to_id : link.to_id === job.id ? link.from_id : null;
    if (!other) continue;
    const r = records.get(other);
    if (['hiring_for', 'advertised_by'].includes(link.relation) && ['company', 'agency'].includes(r?.type)) organisations.set(r.id, r);
    if (r?.type !== 'person') continue;
    if (link.relation === 'introducer' && link.from_id !== r.id) continue;
    if (['related', 'introducer', 'works_at', 'consultant_at', 'formerly_at', 'former_colleague_at'].includes(link.relation))
      add(r, link.relation === 'introducer' ? 'Recorded introducer' : ['formerly_at', 'former_colleague_at'].includes(link.relation) ? 'Historical connection' : 'Linked to this vacancy', link.notes);
  }
  for (const org of organisations.values()) for (const link of peopleByOrganisation.get(org.id) || []) {
    const person = records.get(link.from_id === org.id ? link.to_id : link.from_id);
    if (link.relation === 'introducer' && link.from_id !== person?.id) continue;
    const basis = {
      works_at: `Recorded at ${org.name}`, consultant_at: `Recruiter at ${org.name}`,
      introducer: `Recorded introducer to ${org.name}`, formerly_at: `Formerly at ${org.name}`,
      former_colleague_at: `Former colleague at ${org.name}`,
    }[link.relation];
    if (basis) add(person, basis, link.notes, org.id);
  }
  for (const route of routes.values()) {
    route.check = (checks.get(route.person.id) || []).filter(c =>
      (c.organisation_id ? route.organisationIds.has(c.organisation_id) ||
        (route.direct && organisations.has(c.organisation_id)) : route.direct))
      .sort((a, b) => Date.parse(b.checked_at) - Date.parse(a.checked_at))[0];
  }
  return [...routes.values()].sort((a, b) => a.person.name.localeCompare(b.person.name));
}

// A general employer route is a fallback, never a named person or hiring authority.
// A later supplied route (even blank or unverified) supersedes older research.
export function employerContactRoute(job, state, { isExcluded, now = Date.now() } = {}) {
  if (!job || job.archived_at || job.exclusion || job.exclusion_contexts?.length || isExcluded?.(job)) return null;
  const companyIds = new Set((state.links || []).filter(link => link.relation === 'hiring_for' &&
    (link.from_id === job.id || link.to_id === job.id)).map(link => link.from_id === job.id ? link.to_id : link.from_id));
  const routes = [];
  for (const company of state.records || []) {
    if (!companyIds.has(company.id) || company.type !== 'company' || company.archived_at || company.exclusion ||
      company.exclusion_contexts?.length || isExcluded?.(company)) continue;
    const provenance = company.briefing?.provenance || {};
    const reviews = Object.hasOwn(provenance, 'public_contact_route')
      ? [{ ...provenance.public_contact_route, public_contact_route: company.briefing.public_contact_route }]
      : (company.research || []).filter(review => Object.hasOwn(review, 'public_contact_route'));
    const reviewTime = review => {
      const value = review.reviewed_at || '', day = value.slice(0, 10), calendar = Date.parse(day);
      return (/^\d{4}-\d{2}-\d{2}$/.test(value) || ZONED.test(value)) && Number.isFinite(calendar) &&
        new Date(calendar).toISOString().slice(0, 10) === day ? Date.parse(value) : NaN;
    };
    // An undated route cannot safely be ordered against the other findings.
    if (!reviews.length || reviews.some(review => !Number.isFinite(reviewTime(review)))) continue;
    const latest = reviews.reduce((a, b) => reviewTime(a) >= reviewTime(b) ? a : b);
    if (latest.verification !== 'primary-read' || reviewTime(latest) > now || !latest.public_contact_route?.trim()) continue;
    const sourceUrl = (latest.source_urls || []).find(url => {
      try { const parsed = new URL(url); return ['http:', 'https:'].includes(parsed.protocol) && parsed.hostname &&
        !parsed.username && !parsed.password && routeProvenance({source_url:url}) === 'public'; }
      catch { return false; }
    });
    if (sourceUrl) routes.push({ company, text: latest.public_contact_route.trim(), reviewedAt: latest.reviewed_at, sourceUrl });
  }
  return routes.sort((a, b) => Date.parse(b.reviewedAt) - Date.parse(a.reviewedAt) || a.company.id.localeCompare(b.company.id))[0] || null;
}

export function vacancyEvidence(job) {
  const check = job.evidence_checks?.vacancy;
  const section = A(E('section', null, 'vacancy-evidence'), E('h3', 'Vacancy check'), checkLabel(check, 'vacancy', job));
  section.setAttribute('aria-label', 'Vacancy freshness');
  const a = job.availability, until = a?.until ? ` · counts as open until ${new Date(a.until).toLocaleString('en-GB')}` : '';
  if (a?.basis === 'listing') {
    section.append(E('small', `Seen in the daily read of ${sourceName(a.source)}: ${new Date(a.at).toLocaleString('en-GB')}${until}`));
    if (job.url) section.append(extLink(job.url, 'Check source ↗'));
  } else if (check) {
    section.append(E('small', `Checked ${new Date(check.checked_at).toLocaleString('en-GB')}${a ? until : ' · 24-hour check window'}`), L('p', sourceCheckText(check.basis)));
    section.append(extLink(check.source_url, 'Check source ↗'));
  } else section.append(E('p', 'Availability has not been confirmed: no source has listed it and no research read confirmed it in the last 48 hours. An alert or record edit is not an availability check.', 'muted'));
  return section;
}


// How a route was established (workspace/evidence_checks.py route_class). Only a public read follows
// the 24-hour rule; the owner's confirmation or an earlier exchange stays dated, never relabelled as fresh.
export function routeProvenance(check) {
  const p = check?.provenance;
  if (p === 'owner_confirmed') return 'owner_confirmed';
  if (p === 'prior_correspondence') return 'prior_correspondence';
  if (p === 'public_read') return 'public';
  return /^https:\/\/mail\.google\.com\//i.test(check?.source_url || '') ? 'private' : 'public';
}

export function routeLine(check, organisations = new Map()) {
  const scope = check?.organisation_id ? `Via ${organisations.get(check.organisation_id) || 'their employer'}` : 'Personal route';
  const kind = routeProvenance(check), day = String(check?.checked_at || '').slice(0, 10);
  if (kind === 'public') return { scope, node: checkLabel(check, 'contact_route') };
  const text = kind === 'owner_confirmed' ? `The owner-confirmed ${day} · not a fresh public check`
    : kind === 'prior_correspondence' ? `Previously used route (${day}) · current reachability unknown`
    : check?.status === 'confirmed' ? `Confirmed from your records ${day} · not a fresh public check`
    : `Matched to your records ${day} · current reachability unknown`;
  const node = E('span', text, 'evidence-check evidence-unknown');
  node.dataset.fresh = 'false';
  return { scope, node };
}

export function personRouteEvidence(person, state = { records: [] }) {
  const section=A(E('section',null,'person-route-evidence'),E('h3','Current contact route'));
  const checks=person.evidence_checks?.contacts||[];
  const organisations=new Map((state.records||[]).filter(r=>['company','agency'].includes(r.type)).map(r=>[r.id,r.name]));
  if(!checks.length)section.append(checkLabel(null,'contact_route'));
  for(const check of checks){
    const line=routeLine(check,organisations);
    section.append(A(E('p'),E('strong',`${line.scope}:`),document.createTextNode(' '),line.node),L('p',sourceCheckText(check.basis)),extLink(check.source_url,'Route evidence ↗'));
  }
  section.append(E('small','Public checks expire after 24 hours. A personal route is not a route through their employer. A confirmed route does not establish hiring authority or willingness to introduce you.','muted'));
  return section;
}
