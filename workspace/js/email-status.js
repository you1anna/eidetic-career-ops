import { E } from './dom.js';
const deferredCount = status => Object.values(status?.last_import?.deferred||{}).reduce((sum,n)=>sum+(Number.isSafeInteger(n)&&n>0?n:0),0);
export function emailSourceOutcome(source, now=Date.now()) {
 if(!['ok','empty'].includes(source.state) || source.quarantined>0) return 'Incomplete — needs attention';
 if(!source.last_alert_processed) return 'Waiting for first alert';
 if(now-Date.parse(source.last_alert_processed)>36*60*60*1000) return 'No recent alerts — check delivery';
 return 'Working';
}
export function emailOutcome(status, now=Date.now()) {
 if(!status?.last_attempt) return 'Not started';
 const age=now-Date.parse(status.last_attempt);
 if(!Number.isFinite(age) || age>45*60*1000) return 'Overdue — needs attention';
 if(status.status==='running') return age>10*60*1000?'Processing stalled — needs attention':'Processing';
 if(status.status==='failed') return 'Failed — retained mail will retry';
 if(status.status==='partial') return 'Incomplete — needs attention';
 if(status.capacity_warning) return 'Storage nearly full — needs attention';
 const sourceStates=Object.values(status.sources||{}).map(source=>({source,outcome:emailSourceOutcome(source,now)}));
 const problem=sourceStates.find(row=>row.outcome.startsWith('Incomplete')) || sourceStates.find(row=>row.outcome!=='Working');
 if(problem) return `${problem.outcome} — ${problem.source.label||'Email source'}`;
 if(status.last_alert_processed && now-Date.parse(status.last_alert_processed)>36*60*60*1000) return 'No recent alerts — check delivery';
 const deferred=deferredCount(status);
 if(deferred) return `Alerts processed — ${deferred} advert${deferred===1?'':'s'} deferred`;
 return status.last_alert_processed?'Working':'Waiting for first job alert';
}
const when = value => value ? new Date(value).toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}) : 'Never';
export function emailStatusCard(status) {
 const card=E('section',null,'notice email-processing');
 card.setAttribute('aria-label','Job alert email processing');
 card.dataset.emailOutcome=emailOutcome(status)==='Working'?'working':'attention';
 const title=E('strong',`Job alert email processing · ${emailOutcome(status)}`);
 title.dataset.emailStatus=JSON.stringify(status||{});
 card.append(title);
 const times=E('p',`Last attempt: ${when(status?.last_attempt)} · Last successful check: ${when(status?.last_success)}`);
 card.append(times);
 const details=E('details'); details.append(E('summary','Email delivery details'));
 const p=status?.processing||{},c=status?.collection||{};
 details.append(E('p',`Last email received: ${when(status?.last_received_at)} · Last job alert processed: ${when(status?.last_alert_processed)}`));
 const sources=Object.values(status?.sources||{});
 for(const source of sources) details.append(E('p',`${source.label}: ${emailSourceOutcome(source)}. Last alert processed: ${when(source.last_alert_processed)}. Latest run: ${source.messages??'unknown'} emails, ${source.jobs??'unknown'} distinct adverts, ${source.eligible??'unknown'} matched intake filters; ${source.quarantined??'unknown'} retained for investigation.`));
 if(status?.last_attempt) details.append(E('p',`${sources.length?'':`Latest run: ${p.messages??'unknown'} alert emails processed, ${p.jobs??'unknown'} distinct adverts parsed, ${p.eligible??'unknown'} matched your filters. `}${c.pending??'Unknown'} emails waiting to be collected; ${p.quarantined??'unknown'} retained for investigation.`));
 const imported=status?.last_import;
 if(imported) {
  const reasons={working_limit:'working limit',seven_day_limit:'seven-day limit',unresolved_stale:'unresolved and stale',not_recommended:'not recommended'};
  const held=Object.entries(reasons).filter(([key])=>imported.deferred?.[key]>0).map(([key,label])=>`${imported.deferred[key]} ${label}`).join(', ');
  details.append(E('p',`Last alert import (${when(imported.finished_at)}): ${imported.jobs??'unknown'} distinct adverts parsed, ${imported.eligible??'unknown'} matched your filters, ${imported.admitted??'unknown'} added; ${imported.deferred?deferredCount(status):'unknown'} deferred${held?` (${held})`:''}. Automatic addition allowance remaining at that import: ${imported.allowance_remaining??'unknown'}.`));
 }
 if(status?.message) details.append(E('p',status.message));
 details.append(E('p','Runs on the server every 15 minutes. Your Mac is not required. Processing an alert does not verify that its vacancy or contact route is current.','muted'));
 if(!status?.last_attempt) details.append(E('p','Server processing has not recorded a run yet. Forwarding is not verified.'));
 card.append(details);return card;
}
export function refreshEmailStatus(root=document) {
 root.querySelectorAll('[data-email-status]').forEach(node=>{const outcome=emailOutcome(JSON.parse(node.dataset.emailStatus)); node.textContent=`Job alert email processing · ${outcome}`; node.closest('.email-processing').dataset.emailOutcome=outcome==='Working'?'working':'attention';});
}
