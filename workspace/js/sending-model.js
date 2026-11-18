// Pure batch selection and handoff. The existing outcome service owns all recorded state.
import { contactAction } from './contact-log.js';
import { contactRoute } from './week-brief.js';
import { gaps, splitSubject } from './template-text.js';
import { reading } from './message-format.js';

export const BATCH_LIMIT = 10;
export const SEND_CHANNELS = [['email','Gmail'],['linkedin','LinkedIn'],['whatsapp','WhatsApp'],
  ['instagram','Instagram'],['text','Text'],['other','Other']];
const allowed = person => person?.type === 'person' && !person.archived_at && !person.exclusion
  && !person.exclusion_contexts?.length && person.relationship !== 'do_not_contact'
  && person.relevance?.reach?.tier !== 'blocked';

export function candidates(state) {
  const records = new Map((state.records || []).map(r => [r.id,r])), found = new Map();
  for (const item of state.intelligence?.actions || []) {
    const person = records.get(item.person_id || item.record_id);
    if (!allowed(person) || item.exclusion || found.has(person.id)) continue;
    const action = contactAction(item);
    if (action?.record_id === person.id) found.set(person.id,{person,item,action});
  }
  return [...found.values()];
}

export function handoff(person, channel, text) {
  const none = {href:'',label:'',prefilled:false};
  if (!allowed(person) || !text.trim() || gaps(text).length) return none;
  if (channel === 'instagram') return {href:'https://www.instagram.com/direct/inbox/',label:'Open Instagram · choose their conversation',prefilled:false};
  const route = contactRoute(person,channel);
  if (!route.direct) return none; // never open LinkedIn while recording Email/WhatsApp
  const {subject,body} = splitSubject(text);
  if (channel === 'email') {
    const base = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(person.email)}`;
    const titled = `${base}&su=${encodeURIComponent(subject)}`;
    const full = `${titled}&body=${encodeURIComponent(reading(body))}`;
    return {href:full.length <= 2000 ? full : titled.length <= 2000 ? titled : base,label:'Open Gmail',prefilled:full.length <= 2000};
  }
  if (channel === 'whatsapp') {
    const full = `${route.href}?text=${encodeURIComponent(reading(body))}`;
    return {href:full.length <= 2000 ? full : route.href,label:'Open WhatsApp',prefilled:full.length <= 2000};
  }
  return {href:route.href,label:channel === 'linkedin' ? 'Open LinkedIn profile' : 'Open Text',prefilled:false};
}

export function contactPayload(action, revision, channel, note) {
  if (!SEND_CHANNELS.some(([key])=>key===channel)) throw new Error('Choose a message channel');
  return {revision,kind:action.kind,action_id:action.action_id,channel:channel==='instagram'?'other':channel,
    note:channel==='instagram'?`Instagram${note.trim()?`: ${note}`:''}`:note};
}
