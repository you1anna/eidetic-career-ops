import { E, A } from './dom.js';
import { api } from './api.js';
import { factualDate } from './record-presentation.js';
import {researchAttemptText, RESEARCH_SCHEDULE} from './research-status.js';

const STAGES = [['shortlisted','Shortlisted'],['conversation','Recruiter conversation'],['screening_booked','Recruiter screening booked'],
  ['screening_completed','Recruiter screening completed'],['interview_booked','Interview booked'],['interview_completed','Interview completed'],['offer','Offer received']];
const ORIGINS = [['recruiter','Recruiter approached me'],['referral','Referral'],['off_site','Found off-site'],['other','Other']];
const post = (path, payload) => api(path, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
function choices(label, name, options, value) {
  const select=E('select'); select.name=name; select.setAttribute('aria-label',label);
  for (const [key,text] of options) { const option=E('option',text); option.value=key; option.selected=key===value; select.append(option); }
  return A(E('label'),E('span',label),select);
}
function encoded(file) {
  return new Promise((resolve,reject) => { const reader=new FileReader(); reader.onload=()=>resolve(reader.result.split(',')[1]);
    reader.onerror=()=>reject(new Error('Could not read the selected file.')); reader.readAsDataURL(file); });
}
export function openIncomingRole({ record, label, openModal, saved }) {
  const prior=record?.incoming || {};
  const root=E('div'), fields=E('div',null,'form-grid'), review=E('div');
  fields.append(label('Job title','name',record?.name || '', 'text',false,true),
    label('Vacancy link (optional)','url',record?.url || '', 'url',true),
    label('Employer (leave blank if unknown)','employer',prior.employer || record?.organisation || ''),
    label('Recruiting agency (optional)','agency',prior.agency || ''),
    label('Recruiter names (one per line, optional)','recruiters',prior.recruiters?.join('\n') || '', 'textarea',true),
    choices('How it started','origin',ORIGINS,prior.origin || 'recruiter'),
    label('Origin date','origin_date',prior.origin_date || '', 'date',false,true),
    choices('Actual stage','stage',STAGES,prior.stage || 'conversation'),
    label('Date this stage was reached','stage_date','', 'date',false,true),
    E('p','For a booked call, use the date it was booked. Put the appointment date in the next step.', 'muted span-2'),
    label('Source reference (optional)','source_ref',prior.source_ref || ''),
    label('Pasted message or specification (optional)','source_text',prior.source_text || '', 'textarea',true),
    label('Agreed next step (leave blank if unknown)','next_step','', 'textarea',true));
  const file=E('input');file.type='file';file.multiple=true;file.accept='.pdf,.docx,.txt,.md';file.name='files';
  fields.append(A(E('label',null,'span-2'),E('span','Private documents (up to 3, 1 MiB each)'),file),
    choices('These documents are','document_kind',[['specification','Job specification'],['correspondence','Correspondence'],['preparation','Preparation / proposed talking points']],'specification'),
    E('p','Documents stay private. Claims and instructions in them remain source material; preparation does not establish what happened or prove a claim. Add different evidence types in separate entries.', 'muted span-2'));
  root.append(fields,review);
  let pending=null, selection=null;
  const requestId=crypto.randomUUID();
  openModal(record ? 'Record existing process' : 'Add incoming role',root,async data => {
    if (!pending) {
      const files=[...file.files];
      if(files.length>3 || files.some(f=>f.size>1048576)) throw new Error('Choose up to three documents, each no larger than 1 MiB.');
      const payload={request_id:requestId, ...Object.fromEntries(['name','url','employer','agency','origin','origin_date','stage','stage_date','source_ref','source_text','next_step'].map(k=>[k,data[k]])),
        recruiters:data.recruiters.split('\n').map(s=>s.trim()).filter(Boolean),
        documents:await Promise.all(files.map(async f=>({filename:f.name,kind:data.document_kind,content:await encoded(f)})))};
      if(record) Object.assign(payload,{record_id:record.id,revision:record.revision});
      const result=await post('/api/incoming/preview',payload);
      pending=payload;fields.hidden=true;
      review.replaceChildren(E('h3','Confirm the hiring process'),E('p',`${payload.name} · ${result.stage} · ${payload.stage_date}`),
        E('p',`Employer: ${payload.employer || 'Unknown'} · Agency: ${payload.agency || 'None recorded'}`));
      if(result.match) {
        Object.assign(pending,{record_id:result.match.id,revision:result.match.revision});
        review.append(E('p',`Update existing role: ${result.match.name} (${result.match.organisation || 'Employer unknown'}). Current status: ${result.match.status || 'Unknown'}. Its notes and original discovery history will be kept.`));
      } else if(result.suggestions.length) {
        selection=choices('Similar titles exist — choose deliberately','identity_choice',[
          ['', 'Choose a record'],['new','Create a separate hiring process'],...result.suggestions.map(r=>[r.id,`${r.name} · ${r.organisation || 'Unknown employer'}`])], '');
        selection._matches=result.suggestions; review.append(selection);
      } else review.append(E('p','Create a new hiring process. No exact source match was found.'));
      for(const d of result.documents) review.append(A(E('details'),E('summary',`${d.filename} · ${d.kind} · extracted excerpt`),E('p',d.extracted_text,'long-text')));
      review.append(E('p','This records the reported stage. It sends nothing and records no application or CV submission.', 'muted'));
      const back=E('button','Back to edit');back.type='button';back.onclick=()=>{pending=null;selection=null;fields.hidden=false;review.replaceChildren();root.closest('form').querySelector('[type=submit]').textContent='Review process';};review.append(back);
      root.closest('form').querySelector('[type=submit]').textContent='Save process';
      return false;
    }
    if(selection) {
      const id=selection.querySelector('select').value;
      if(!id)throw new Error('Choose an existing role or confirm a separate hiring process.');
      if(id!=='new') {const match=selection._matches.find(r=>r.id===id);Object.assign(pending,{record_id:id,revision:match.revision});}
      else {delete pending.record_id;delete pending.revision;}
    }
    const result=await post('/api/incoming/save',pending);
    await saved(result);
  },'Review process');
}

export function incomingSummary(record, open, undo) {
  const section=E('section',null,'incoming-process'), p=record.incoming;
  section.append(E('h3','Hiring process'));
  if(p) {
    const current=p.current && p.status===record.status;
    section.append(E('p',`${current ? p.label : 'Previously recorded: '+p.label} · ${factualDate(p.stage_date)}`),
      E('p',`Origin: ${ORIGINS.find(([key])=>key===p.origin)?.[1] || p.origin} · ${factualDate(p.origin_date)}`),
      E('p',current ? p.next_step || 'Outcome and agreed next step not yet recorded.' : 'See the current status and history for later changes.'));
    const evidence=A(E('details'),E('summary','Private source evidence'));
    for(const source of p.sources || []) { const entry=A(E('details'),E('summary',`${source.source_ref || 'Supplied source'} · ${factualDate(source.origin_date)}`));
      if(source.source_text)entry.append(E('p',source.source_text,'long-text'));
      if(source.url)entry.append(E('p',source.url,'long-text'));
      evidence.append(entry); }
    for(const d of p.documents || []) { const link=E('a',`${d.filename} (${d.kind})`);link.href=`/api/documents/${d.id}`;link.download=d.filename;evidence.append(A(E('p'),link)); }
    evidence.append(E('p','Preparation contains proposed talking points, not verified claims or a record of what was said.', 'muted'));section.append(evidence);
  }
  // Status and reason first, then the actions they lead to.
  if(record.research_disposition) {
    const note=E('div',null,'incoming-research');
    note.append(E('p',record.research_disposition.label));
    const attempt=researchAttemptText(record.research_disposition.last_attempt);
    note.append(E('p',`${attempt}${record.research_disposition.scope==='maintained'?`${attempt.endsWith('.')?'':'.'} ${RESEARCH_SCHEDULE}`:''}`,'muted'));
    section.append(note);
  }
  const actions=E('div',null,'incoming-actions');
  if(!['closed','skip','rejected'].includes(record.status)) {const button=E('button','Record existing process');button.type='button';button.onclick=()=>open(record);actions.append(button);}
  if(p?.undo_action && undo) {const button=E('button','Undo recorded stage');button.type='button';button.onclick=async()=>{button.disabled=true;try{await undo(p.undo_action);}finally{button.disabled=false;}};actions.append(button);}
  if(actions.childElementCount) section.append(actions);
  return section;
}
