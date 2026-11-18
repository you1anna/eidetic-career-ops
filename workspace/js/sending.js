// One ephemeral sending session. Wording is preparation only; contact records never attach it.
import { E, A } from './dom.js';
import * as T from './template-text.js';
import { reading } from './message-format.js';
import { BATCH_LIMIT, SEND_CHANNELS, candidates, handoff, contactPayload } from './sending-model.js';

const button = (text, run, cls='') => { const b=E('button',text,cls); b.type='button'; b.onclick=run; return b; };
const field = (label, input) => { input.setAttribute('aria-label',label); return A(E('label',null,'field'),E('span',label),input); };

export function createSending({getState, getRecord, openModal, fetchTemplates, mutate, refresh, setDirty}) {
  let pack;
  return function openSending() {
    const root=E('div',null,'sending'), selected=new Set(), drafts=new Map(), saved=new Map(), unavailable=new Set();
    let available=candidates(getState()), batch=[], position=0, mode='choose', working=false;
    let search='', message='', lastUndo=null, current=null, shownId='', needsRefresh=false, savedAt=-Infinity;
    const footer=()=>root.closest('form')?.querySelector('[type=submit]');
    const dirty=()=>setDirty([...drafts.values()].some(d=>d.text || d.note));
    const capture = entry => {
      const preferred=entry.person.preferred_channel || entry.item.progress?.channel;
      return {...entry,revision:entry.person.revision,channel:SEND_CHANNELS.some(([id])=>id===preferred)?preferred:'other',
        template:T.suggest(pack,entry.person,entry.item),text:'',note:''};
    };
    function footerState() {
      const b=footer(); if(!b)return;
      b.textContent=mode==='choose'?'Start batch':mode==='done'?'Close':'Sent & next';
      b.disabled=working || (mode==='choose'&&!selected.size) || (mode==='edit'&&(!current || saved.has(current.person.id) || unavailable.has(current.person.id) || performance.now()-savedAt<600));
    }
    function showMessage() {
      const line=root.querySelector('.sending-status'); if(line){line.textContent=message;line.hidden=!message;}
    }
    function go(n) { position=n; message=''; render(); dirty(); }
    function render() {
      root.replaceChildren(); current=null;
      const status=E('p',message,'sending-status'); status.setAttribute('role','status');status.hidden=!message;
      if(lastUndo) {
        const undo=button(`Undo send to ${lastUndo.name}`,async()=>{
          if(working)return; working=true; root.inert=true; undo.disabled=true; footerState();
          const fact=lastUndo;
          try {
            await mutate(`/api/records/${encodeURIComponent(fact.id)}/correct`,
              {revision:getRecord(fact.id)?.revision,outcome_id:fact.outcome,undo:true,note:'Undone from sending session'});
            saved.delete(fact.id); lastUndo=null;
            const d=drafts.get(fact.id); if(d)d.revision=getRecord(fact.id)?.revision;
            position=batch.indexOf(fact.id); mode='edit'; message='Send record undone. Review before recording again.';
          } catch(error) {message=`Undo failed: ${error.message}`;}
          finally {working=false;root.inert=false;render();dirty();}
        });
        root.append(undo);
      }
      root.append(status);
      if(mode==='choose') {
        root.append(E('p',`Choose up to ${BATCH_LIMIT} people. Only contacts with an available contact action appear.`, 'muted'));
        const input=E('input'); input.type='search';input.value=search;
        const list=E('div',null,'sending-choices'),count=E('p','', 'muted');
        const draw=()=>{
          list.replaceChildren(); const query=search.trim().toLowerCase();
          const matches=available.filter(({person})=>`${person.name} ${person.organisation||''}`.toLowerCase().includes(query));
          count.textContent=`${selected.size} selected · ${matches.length} matching contacts${matches.length>20?' · showing first 20':''}`;
          for(const {person} of matches.slice(0,20)) {
            const check=E('input');check.type='checkbox';check.checked=selected.has(person.id);
            check.disabled=!check.checked&&selected.size>=BATCH_LIMIT;
            check.onchange=()=>{check.checked?selected.add(person.id):selected.delete(person.id);draw();footerState();};
            list.append(A(E('label',null,'sending-choice'),check,E('span',person.name)));
          }
          if(!matches.length)list.append(E('p','No matching contacts. Add or review the person in People.'));
        };
        input.oninput=()=>{search=input.value;draw();};
        root.append(field('Find people for this batch',input),count,list);draw();
      } else if(mode==='done') {
        root.append(E('h3','Batch finished'),E('p',`${saved.size} contact${saved.size===1?'':'s'} recorded. Skipped people have no new contact record.`));
        root.append(button('Review batch',()=>{mode='edit';go(0);}));
      } else {
        const id=batch[position];current=drafts.get(id);
        const d=current, person=d.person;
        const heading=E('h3',person.name);heading.tabIndex=-1;
        root.append(A(E('div',null,'sending-person'),heading,E('span',`${position+1} of ${batch.length}`,'muted')));
        const context=(d.item.progress?.points||[]).find(x=>x.startsWith('Your note:')) || person.summary;
        if(context)root.append(E('p',context,'sending-context'));
        if(person.last_touch)root.append(E('p',`Last recorded contact: ${person.last_touch}`,'muted'));
        if(d.item.progress?.next_step?.text)root.append(E('p',`Current next step: ${d.item.progress.next_step.text}`,'muted'));
        const channel=E('select');
        for(const [value,label]of SEND_CHANNELS){const o=E('option',label);o.value=value;channel.append(o);}channel.value=d.channel;
        const chooser=E('select');const blank=E('option','Choose a template');blank.value='';chooser.append(blank);
        for(const t of [...pack?.templates||[],...pack?.followups?.items||[]]){const o=E('option',t.title||t.id);o.value=t.id;chooser.append(o);}chooser.value=d.template;
        const text=E('textarea');text.rows=8;text.maxLength=8000;text.value=d.text;
        const gaps=E('p','', 'sending-gaps'),checks=E('ul',null,'sending-checks');
        const copy=button('Copy message',async()=>{
          try {await navigator.clipboard.writeText(reading(T.splitSubject(d.text).body));message='Message copied. Send in the selected app, then record it here.';}
          catch {text.focus();text.select();message='Copy unavailable. The message is selected; use Copy on your device.';}
          showMessage();
        });
        const open=E('a',null,'button sending-open');open.target='_blank';open.rel='noopener noreferrer';
        const hint=E('p','', 'muted');
        function update() {
          const missing=T.gaps(d.text);gaps.textContent=missing.length?`Personalise: ${missing.join(', ').replaceAll('_',' ')}`:'';
          checks.replaceChildren(...T.checks(pack,d.text,{id:d.template}).map(s=>E('li',s)));
          copy.disabled=!d.text.trim()||!!missing.length||unavailable.has(id);
          const route=unavailable.has(id)?{href:''}:handoff(person,d.channel,d.text);open.textContent=route.label||'Open channel';
          if(route.href){open.href=route.href;open.removeAttribute('aria-disabled');}else{open.removeAttribute('href');open.setAttribute('aria-disabled','true');}
          hint.textContent=d.channel==='instagram'?'Choose their Instagram conversation and paste. Recorded as Other with “Instagram” in the note.'
            :route.href?(route.prefilled?'Review the prefilled message and recipient before sending.':'Copy the message, then open the channel and paste it.')
            :missing.length?'Fill the placeholders before copying or opening.':!d.text.trim()?'Write a message, or record one you already sent.':'No usable route for this channel. Copy and open your app manually, or update the person’s contact details.';
        }
        text.oninput=()=>{d.text=text.value;update();dirty();};
        channel.onchange=()=>{d.channel=channel.value;update();dirty();};
        chooser.onchange=()=>{d.template=chooser.value;use.disabled=!pack||!d.template;};
        const use=button('Use template',()=>{
          if(d.text.trim()&&!confirm('Replace this person’s prepared message with the selected template?'))return;
          d.text=T.render(pack,d.template,{channel:d.channel,fill:T.values(pack,{person})});text.value=d.text;update();dirty();
          text.focus();const gap=/\{\{[^}]+\}\}/.exec(d.text);if(gap)text.setSelectionRange(gap.index,gap.index+gap[0].length);
        });
        use.disabled=!pack||!d.template;chooser.disabled=!pack;
        if(!pack)root.append(E('p','Templates unavailable. You can still write and log a message.','muted'),button('Retry templates',loadTemplates));
        root.append(A(E('div',null,'sending-pickers'),field('Channel',channel),field('Template',chooser)),use,
          field('Message to personalise',text),gaps,checks,A(E('div',null,'sending-actions'),copy,open),hint);
        const note=E('textarea');note.rows=2;note.maxLength=1900;note.value=d.note;note.oninput=()=>{d.note=note.value;dirty();};
        const details=A(E('details'),E('summary','Details & template notes'),field('Contact note',note),
          E('p','Records contact now and keeps the current next step. Wording stays only in this batch: copy it before closing. It is not stored as the sent message.','muted'));
        if(pack?.before_you_send?.items?.length)details.append(E('p','Library notes to review before reusing older wording:','muted'),
          A(E('ul',null,'sending-checks'),...pack.before_you_send.items.map(s=>E('li',s))));
        root.append(details);
        if(saved.has(id))root.append(E('p','Contact already recorded in this batch. Use Undo to correct it.','muted'));
        if(unavailable.has(id))root.append(E('p','This contact is no longer available. Skip them; no send was recorded.','muted'));
        const back=button('Previous',()=>go(position-1));back.disabled=position===0;
        const next=button(position===batch.length-1?'Finish batch':saved.has(id)?'Next':'Skip for now',()=>{
          if(position===batch.length-1){mode='done';render();}else go(position+1);
        });
        const recover=button('Refresh contact after a conflict',async()=>{
          if(working)return;working=true;root.inert=true;footerState();
          try {
            await refresh(); const latest=candidates(getState()).find(x=>x.person.id===id);
            if(latest){Object.assign(d,latest,{revision:latest.person.revision});unavailable.delete(id);needsRefresh=false;message='Contact refreshed. Your wording is preserved; review before saving again.';}
            else{unavailable.add(id);message='Contact is no longer available for this action. Your wording is preserved.';}
          }catch(error){message=error.message;}finally{working=false;root.inert=false;render();dirty();}
        },'linkish');
        recover.hidden=!needsRefresh;
        root.append(A(E('div',null,'sending-actions'),back,next),recover);
        update();
        if(shownId!==id){shownId=id;heading.focus({preventScroll:true});const dialog=root.closest('dialog');if(dialog)dialog.scrollTop=0;}
      }
      footerState();
    }
    async function loadTemplates() {
      try{pack=await fetchTemplates();message='';}catch{message='Templates could not load. Retry, or write your own message.';}
      if(root.isConnected)render();
    }
    openModal('Send messages',root,async()=>{
      if(working)return false;
      if(mode==='choose') {
        batch=[...selected].slice(0,BATCH_LIMIT);
        if(!batch.length)return false;
        available=candidates(getState());
        for(const id of batch){const entry=available.find(x=>x.person.id===id);if(!entry)throw new Error('A selected contact changed. Close and select the batch again.');drafts.set(id,capture(entry));}
        mode='edit';render();dirty();return false;
      }
      if(mode==='done')return true;
      // A fast save can finish between the two taps of a double-click. Do not let the
      // second tap record the next recipient, even when no request remains in flight.
      if(!current||saved.has(current.person.id)||unavailable.has(current.person.id)||performance.now()-savedAt<600)return false;
      const d=current;working=true;
      try {
        const result=await mutate(`/api/records/${encodeURIComponent(d.person.id)}/outcome`,contactPayload(d.action,d.revision,d.channel,d.note));
        saved.set(d.person.id,true);
        savedAt=performance.now();setTimeout(()=>{if(root.isConnected)footerState();},610);
        if(result?.outcome?.id&&!result.duplicate)lastUndo={id:d.person.id,name:d.person.name,outcome:result.outcome.id};
        message=`${result.duplicate?'Already recorded':'Recorded'}: ${d.person.name}.`;
        if(position===batch.length-1)mode='done';else position++;
      } catch(error) {message=`Not saved: ${error.message}`;needsRefresh=true;}
      finally {working=false;render();dirty();}
      return false;
    },'Start batch',{managesDirty:true,afterSubmit:footerState});
    render(); if(!pack)void loadTemplates();
  };
}
