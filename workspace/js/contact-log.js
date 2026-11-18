// A contact is a reported factual event with an optional note and human next step.
// The caller owns persistence, receipts and Undo; this module only builds the form.
import { E, A } from './dom.js';
import { CHANNEL_NAMES } from './labels.js';
import { nextStepFields } from './next-step.js';

export const CONTACT_CHANNELS = ['call', 'whatsapp', 'text', 'email', 'linkedin', 'in_person', 'other'];
const field = (label, input, span = false) => {
  input.setAttribute('aria-label', label);
  return A(E('label', null, 'field' + (span ? ' span-2' : '')), E('span', label), input);
};

export function contactAction(item) {
  return item.progress?.actions?.find(a => a.method === 'outcome' && ['sent', 'followed_up'].includes(a.kind));
}

export function createContactLog({ openModal, setDirty, save }) {
  return function openContactLog({ item, action, revision }) {
    const fields = E('div', null, 'form-grid contact-log');
    const channel = E('select'); channel.name = 'channel';
    const preferred = action.channel || item.progress?.channel || 'other';
    [...new Set([preferred, ...CONTACT_CHANNELS])].forEach(value => {
      const option = E('option', CHANNEL_NAMES[value] || value); option.value = value; channel.append(option);
    });
    const status = E('p', '', 'approach-status span-2'); status.setAttribute('role', 'status');
    const next = item.progress?.actions?.some(a => a.method === 'next_step')
      ? nextStepFields(item.progress.next_step, { keep:true }) : null;
    const note=E('textarea');note.name='note';note.rows=2;note.maxLength=2000;
    fields.append(E('p', `Record contact with ${item.person || item.company || 'this contact'} today. This saves a record; it sends nothing.`, 'span-2'),
      field('How you got in touch', channel), ...(next ? [next.box] : []), field('Contact note (optional)',note,true), status);
    fields.addEventListener('input', () => setDirty(true));
    channel.onchange = () => setDirty(true);
    openModal(action.kind === 'followed_up' ? 'Log follow-up' : 'Log contact', fields,
      data => save(item, action, {
        revision, channel: data.channel, note:data.note || '',
        ...(next?.read() !== undefined ? { next_step:next.read() } : {}),
      }, status, fields), 'Save contact');
  };
}
