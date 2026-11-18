// Decision support composes existing assessments and review actions; it never writes or runs research.
import {E, A, extLink} from './dom.js';
import {api} from './api.js';
import {researchCount, researchTime} from './research-status.js';

const DEFERRALS = {working_limit:'Outside the working set', seven_day_limit:'Seven-day additions cap',
  not_recommended:'Not recommended by current evidence', unresolved_stale:'Unresolved and stale'};
const openButton = (text, action) => { const b=E('button',text,'linkish');b.type='button';b.onclick=action;return b; };

export function researchDecisionPanel(state, openRecord) {
  const panel=A(E('section',null,'dashboard-panel research-decisions'),E('h3','Where research could change a decision'));
  const briefs=state.research_processing?.briefs;
  if(briefs)panel.append(E('p',`${researchCount(briefs.sufficient)} of ${researchCount(briefs.total)} targets have sufficient recorded context; ${researchCount(briefs.blocking)} blocking gaps remain. Context completeness does not establish suitability.`,'muted'));
  panel.append(E('p','Public questions come before routine rechecks within each research lane. Chosen work and discovery alternate under the existing allowance; a target can take more than one call.','muted'));
  const plan=state.research_plan;
  if(!plan)panel.append(E('p','Research priorities are unavailable until the current workspace assessment loads.','muted'));
  else {
    const byId=new Map((state.records||[]).map(r=>[r.id,r]));
    const questions=plan.filter(item=>item.kind!=='maintenance').slice(0,3);
    if(!questions.length)panel.append(E('p','No public question that could unlock an application is identified in the maintained set. Review the intake comparison for alternatives.','muted'));
    for(const item of questions){
      const record=byId.get(item.record_id);if(!record)continue;
      const name=[record.organisation,record.name].filter(Boolean).join(' · ');
      panel.append(A(E('article',null,'dash-decision-row'),
        A(E('h4'),openButton(name,()=>openRecord(record.id))),
        E('p',`${item.kind==='your_choice'?'Your evidence needed':'Research question'}: ${item.question}`),
        E('p',item.consequence,'muted')));
    }
    panel.append(E('p','Review a role to Shortlist or Skip. Shortlisting includes it in chosen research; skipping removes it. Neither action records an application.','muted'));
  }
  return panel;
}

export function createIntakeComparison({openRecord}) {
  let node, results, status, button, reading=false, currentVersion, comparedVersion;
  function panel(version){
    currentVersion=version;
    if(node){
      if(comparedVersion && comparedVersion!==currentVersion && results.childElementCount)
        status.textContent='The workspace changed after this comparison. Refresh comparison before choosing.';
      return node;
    }
    node=A(E('details',null,'dashboard-panel intake-comparison'),E('summary','Are better opportunities being excluded?'));
    node.dataset.persistKey='intake-comparison';
    node.append(E('p','Compare the strongest unadmitted candidates with the weakest unchosen jobs receiving research. Shortlisted and active processes are protected. Counts alone do not show missed opportunities.','muted'));
    button=E('button','Compare candidates');button.type='button';button.onclick=read;
    status=E('p','','muted');status.setAttribute('role','status');results=E('div');
    node.append(button,status,results);return node;
  }
  function candidate(row){
    const name=[row.organisation,row.name].filter(Boolean).join(' · ');
    const title=row.record_id?openButton(name,()=>openRecord(row.record_id)):E('strong',name);
    const evidence=`${row.band} · score ${row.score??'unknown'} · ${row.deferral?DEFERRALS[row.deferral]||row.deferral:'Maintained, unchosen'}`;
    const body=A(E('article',null,'dash-decision-row'),title,E('p',evidence,'muted'),
      E('p',[row.terms,row.reason,row.uncertainty].filter(Boolean).join(' · ')));
    if(row.review_url){const review=E('a','Review lead');review.href=row.review_url;body.append(review);}
    else if(!row.record_id&&row.url)body.append(extLink(row.url,'Review source advert ↗'));
    return body;
  }
  async function read(){
    if(reading)return;reading=true;button.disabled=true;
    const requestedVersion=currentVersion;
    status.textContent='Comparing current candidates…';
    try{
      const data=await api('/api/intake-review');
      const scope=E('p',`${data.scope} ${data.complete?'All eligible observations in this scope were considered.':`Partial comparison: only the first ${data.candidate_limit} of ${data.eligible_observations} eligible observations were assessed; stronger omitted candidates remain unknown.`}`,'muted');
      const columns=A(E('div',null,'dashboard-split'));
      for(const [title,rows,empty] of [['Strongest excluded candidates',data.excluded,'No excluded candidates found in the assessed scope.'],
        ['Weakest maintained jobs',data.maintained,'No unchosen maintained roles to compare.']]){
        const column=A(E('section'),E('h4',title));
        if(!rows.length)column.append(E('p',empty,'muted'));
        for(const row of rows)column.append(candidate(row));
        columns.append(column);
      }
      results.replaceChildren(scope,columns,E('p','Ranking uses the same current evidence and rules as intake. Up to three capacity exclusions and three policy exclusions are shown, strongest first in each group. A higher score is not proof of better fit. Review evidence before choosing.','muted'),
        E('p','To change attention, review an excluded email lead and Shortlist it, then review a maintained role and Skip it if unsuitable. For other sources, use Save to shortlist in Jobs. These are separate recorded choices; skipping does not refund the rolling additions allowance.','muted'));
      status.textContent=`Compared ${researchTime(data.generated_at)} · ${data.allowance_remaining} automatic additions remaining. No records changed.`;
      comparedVersion=requestedVersion;
      if(comparedVersion && comparedVersion!==currentVersion)status.textContent+=' The workspace changed; refresh comparison before choosing.';
      button.textContent='Refresh comparison';
    }catch(error){status.textContent=`Comparison unavailable. ${error.message}${results.childElementCount?' Earlier results remain below; they may be stale.':''}`;button.textContent='Retry comparison';}
    finally{reading=false;button.disabled=false;}
  }
  return {panel};
}
