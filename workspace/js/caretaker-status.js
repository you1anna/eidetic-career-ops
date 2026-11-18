import {E,A} from './dom.js';
export function caretakerOutcome(s,now=Date.now()) {
 if(!s?.last_attempt)return 'Not started';
 const age=now-Date.parse(s.last_attempt);
 if(!Number.isFinite(age)||age< -300000||age>45*60000)return 'Overdue — needs attention';
 if(s.status==='paused')return 'Paused';
 if(s.status==='failed')return 'Needs attention';
 if(s.status==='partial')return 'Checks have gaps';
 return 'Checks completed';
}
export function caretakerStatusCard(s){
 const card=E('section',null,'notice caretaker-processing');card.setAttribute('aria-label','Site caretaker');
 const title=E('strong',`Site caretaker · ${caretakerOutcome(s)}`);title.dataset.caretakerStatus=JSON.stringify(s||{});
 card.append(title,E('p','Checks site health, services, receipts, email backlog and backup evidence every 15 minutes when enabled. No model calls or automatic repairs.'));
 if(s?.last_attempt)card.append(E('p',`Last check: ${new Date(s.last_attempt).toLocaleString('en-GB')}. Reload for new results.`));
 else card.append(E('p','No caretaker receipt yet. An operator must activate the VPS timer after deployment.'));
 const details=A(E('details'),E('summary','Caretaker checks'));
 for(const c of s?.checks||[])details.append(A(E('p'),E('strong',`${c.label} · ${c.status}`),document.createTextNode(` — ${c.detail}`)));
 card.append(details);return card;
}
export function refreshCaretakerStatus(root=document){root.querySelectorAll('[data-caretaker-status]').forEach(n=>{n.textContent=`Site caretaker · ${caretakerOutcome(JSON.parse(n.dataset.caretakerStatus))}`;});}
