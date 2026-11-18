import {E,A} from './dom.js';

// One presentation owner for recorded attempts; this never schedules or starts research.
export const RESEARCH_SCHEDULE = 'Usual automatic passes: 09:00 and 19:00 London time. A particular role may wait for a later pass.';
const ATTEMPT_LABELS = {
 confirmed:'Research confirmed evidence', current:'Research reused current evidence',
 prepared:'Research prepared a brief', updated:'Research updated findings', unchanged:'Research found no change',
 saved:'Research saved findings', recovered:'Research recovered a saved result', stale:'Research result was older than saved evidence',
 unverified:'Could not verify from public sources — gaps remain explicit', closed:'Research reported the advert closed',
 archived:'Research skipped an archived record', ambiguous:'Research could not establish a single match',
 access_blocked:'Research blocked — sign-in or research service needs attention', usage_limited:'Research paused; not completed',
 collection_deferred:'Research waiting — provider collection allowance',
 budget_stopped:'Research paused — spending limit', missing_context:'Research incomplete — required context missing',
 failed:'Research failed — earlier evidence kept', recovery_missing:'Research needs another source check',
 context_changed:'Research outdated — role or source changed', no_longer_active:'Research stopped — role left the maintained set',
 unknown_id:'Research could not find the record', ok:'Research completed', partial:'Research completed with gaps',
 blocked:'Research blocked — cause not recorded', empty:'Research found no additional evidence',
};
const attemptLabel=status=>Object.hasOwn(ATTEMPT_LABELS,status)?ATTEMPT_LABELS[status]:'Research outcome unavailable';
const SOURCE_REASONS = {
 robots_disallowed:'Automated check blocked by the site', login_or_challenge:'Source requires sign-in or a browser check',
 page_needs_browser:'Source needs a browser to read', unsafe_source:'Source could not be checked safely',
 source_unavailable:'Source could not be reached', 'not-found':'Source page was not found',
 'rate-limited':'Source temporarily limited automated reads', 'bad-response':'Source returned an unreadable response',
 challenged:'Source requires a browser check', refused:'Source refused the automated read',
 server:'Source returned a server error', http:'Source request failed', timeout:'Source read timed out',
 network:'Source could not be reached', error:'Source read failed',
};
const ZONED = /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const LONDON_TIME = new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/London',day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',timeZoneName:'short'});
export function researchTime(value) {
 const time=ZONED.test(value||'')?Date.parse(value):NaN;
 if(!Number.isFinite(time))return 'date not recorded';
 // Date.parse normalises impossible days such as 31 February; a receipt must not.
 const date=value.slice(0,10), calendar=Date.parse(`${date}T00:00:00Z`);
 if(!Number.isFinite(calendar)||new Date(calendar).toISOString().slice(0,10)!==date)return 'date not recorded';
 const p=Object.fromEntries(LONDON_TIME.formatToParts(time).map(part=>[part.type,part.value]));
 return `${p.day} ${p.month} ${p.year}, ${p.hour}:${p.minute} ${p.timeZoneName}`;
}
export function sourceCheckText(value) {
 const text=String(value||'');
 if(Object.hasOwn(SOURCE_REASONS,text))return SOURCE_REASONS[text];
 if(/^Research could not verify this source: [A-Za-z_][A-Za-z0-9_.]*$/.test(text))
  return 'Could not verify from public sources — earlier evidence kept.';
 return text; // Ordinary sourced prose, including quoted code, stays verbatim.
}
export function researchAttemptText(attempt) {
 if(!attempt)return 'No research attempt recorded.';
 const status=attempt.outcome || attempt.status;
 if(status==='not_attempted')return 'No research attempt recorded.';
 let label=attemptLabel(status);
 if(['unverified','partial','blocked'].includes(status) && Object.hasOwn(SOURCE_REASONS,attempt.reason)) {
  const reason=SOURCE_REASONS[attempt.reason];label=`Research incomplete — ${reason[0].toLowerCase()}${reason.slice(1)}`;
 }
 return `${label} · ${researchTime(attempt.attempted_at)}`;
}
const knownCount=value=>Number.isInteger(value)&&value>=0;
export const researchCount=value=>knownCount(value)?value:'unknown';
export function researchOutcome(status,now=Date.now()) {
 if(!status?.last_attempt)return 'Not started';
 const age=now-Date.parse(status.last_attempt);
 if(!Number.isFinite(age)||age>16*3600000)return 'Overdue — needs attention';
 if(status.status==='running')return age>50*60000?'Stalled — needs attention':'Researching';
 if(status.reason==='disabled')return 'Research paused';
 if(status.status==='failed')return 'Failed — needs attention';
 if(status.reason==='access')return 'Blocked — sign-in or research service needs attention';
 if(status.reason==='collection_limit')return 'Waiting — provider collection allowance';
 if(status.reason==='budget')return 'Paused — spending limit';
 if(status.status==='blocked'&&status.reason!=='usage_limit')return 'Blocked — research needs attention';
 if(status.quiet_attention)return `Nothing confirmed in ${status.quiet_passes} passes — needs attention`;
 // The receipt does not distinguish provider/local limits from a concurrent worker refusal.
 // Counts and pass_cap cannot establish the reason for the final call being stopped.
 if(status.reason==='usage_limit')return 'Paused — research not completed';
 if(!['checked','total','verified','unverified','backlog','contacts_verified','contacts_total'].every(key=>knownCount(status[key])))
  return 'Finished — coverage not fully recorded';
 if(status.status==='partial'||status.backlog>0||status.unverified>0||status.contacts_verified<status.contacts_total)return 'Finished with gaps';
 return status.total===0?'No active leads to check':'Checks completed';
}
const when=v=>v?researchTime(v):'Not recorded';
export function researchStatusCard(status,{records=[],openRecord}={}) {
 const card=E('section',null,'notice research-processing');card.setAttribute('aria-label','Job and contact research');
 const title=E('strong',`Job and contact research · ${researchOutcome(status)}`);title.dataset.researchStatus=JSON.stringify(status||{});
 card.append(title,E('p',`Last attempt: ${when(status?.last_attempt)} · Finished: ${when(status?.finished_at)}`));
 if(status?.last_attempt) card.append(E('p',`${status.status==='running'?'Current research pass so far':'Latest research pass'}: ${researchCount(status.checked)} of ${researchCount(status.total)} ${status.focused?'focus targets':'jobs'} checked · ${researchCount(status.verified)} adverts confirmed open · ${researchCount(status.contacts_verified)} of ${researchCount(status.contacts_total)} contact routes confirmed · ${researchCount(status.backlog)} ${status.focused?'focus targets':'jobs'} waiting.`));
 const details=A(E('details'),E('summary','Research coverage'));
 if(status?.quiet_passes)card.append(E('p',`${status.quiet_passes} completed passes in a row confirmed nothing: no advert open or closed, contact route or fully checked target${status.last_confirmed?`. Last confirmation: ${when(status.last_confirmed)}`:''}.`));
 if(status?.briefs){const b=status.briefs;details.append(E('p',`${researchCount(b.sufficient)} of ${researchCount(b.total)} work targets have sufficient recorded evidence · ${researchCount(b.blocking)} blocking gaps · ${researchCount(b.unresolved)} total gaps. Unknown facts remain visible in each work assessment.`));}
 if(status?.focused)details.append(E('p',`Maintained set: active work, shortlisted opportunities and the first ten ranked discovery items research can check. Adzuna and Reed adverts are decided by their latest listing unless you shortlist them. ${researchCount(status.outside_focus)} of ${researchCount(status.whole_queue)} eligible jobs are outside this set; they are not claimed as maintained. ${researchCount(status.current)} targets already current; ${researchCount(status.reused)} interpretations reused after fresh source reads.`));
 if(status?.allowance){const q=status.allowance;details.append(E('p',`Invocation counts recorded ${when(status.finished_at||status.last_attempt)}: research ${researchCount(q.research_day)}/${q.research_day_limit??'unknown'} that day and ${researchCount(q.research_week)}/${q.research_week_limit??'unknown'} that week; all automation ${researchCount(q.day)}/${q.day_limit??'unknown'} that day and ${researchCount(q.week)}/${q.week_limit??'unknown'} that week. London days and Monday weeks. Failed or unknown calls still count. These are invocation limits, not a credit-price guarantee.`));}
 if(status?.message)details.append(E('p',status.message));
 details.append(E('p',`Last pass without coverage gaps: ${when(status?.last_success)}`));
 details.append(E('p',`${RESEARCH_SCHEDULE} ${status?.last_attempt ? 'Your Mac is not required. An advert counts as open while its source listed it, or research confirmed it, in the last 48 hours; a page that cannot be read is not evidence either way.' : 'The VPS worker has not recorded its first run.'}`,'muted'));
 if(status?.engine==='codex_cli'){const u=status.usage||{};details.append(E('p',`Uses the VPS Codex CLI with your ChatGPT login. Last run: ${researchCount(u.completed)} completed CLI invocations; ${researchCount(u.input_tokens)} input tokens and ${researchCount(u.output_tokens)} output tokens reported. Subject to your Codex allowance; no separate API billing or API fallback.`));}
 if(status?.spend){const s=status.spend,l=status.limits||{};details.append(E('p',`Budget used or reserved at last run: $${Number(s.day_usd||0).toFixed(2)} / $${l.daily_usd??'?'} that day; $${Number(s.month_usd||0).toFixed(2)} / $${l.monthly_usd??'?'} that month (USD, UTC periods).`));if(s.reserved_usd>0)details.append(E('p',`$${Number(s.reserved_usd).toFixed(2)} remains reserved for calls whose final cost is unknown.`));}
 const issueList=E('ul');
 for(const issue of status?.issues||[]){const record=records.find(r=>r.id===issue.id);if(!record)continue;const b=E('button',record.name,'linkish');b.type='button';b.onclick=()=>openRecord?.(issue.id);issueList.append(A(E('li'),b,document.createTextNode(` — ${attemptLabel(issue.outcome)}${issue.reason?` · ${issue.reason}`:''}${issue.outcome==='failed'&&issue.where?` (raised at ${issue.where})`:''}`)));}
 if(issueList.childElementCount)details.append(issueList);
 const waiting=E('ul');
 for(const id of status?.waiting_ids||[]){const record=records.find(r=>r.id===id);if(!record)continue;const b=E('button',record.name,'linkish');b.type='button';b.onclick=()=>openRecord?.(id);waiting.append(A(E('li'),b));}
 if(waiting.childElementCount)details.append(A(E('details'),E('summary','Targets waiting for research (up to 30 shown)'),waiting));
 card.append(details);return card;
}
export function refreshResearchStatus(root=document){root.querySelectorAll('[data-research-status]').forEach(n=>{n.textContent=`Job and contact research · ${researchOutcome(JSON.parse(n.dataset.researchStatus))}`;});}
