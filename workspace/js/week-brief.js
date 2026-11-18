// Read-only preparation for a weekly approach. Contact resolution is shared with the
// actual contact link, so a preparation label cannot promise a route the UI cannot open.
import { firstName, gaps, splitSubject } from './template-text.js';
import { reading } from './message-format.js';

export const isKnownContact = person => ['warm', 'active', 'dormant'].includes(person.relationship);

const emailAddress = value => /^[^\s@<>"'(),;:?%&#]+@[^\s@<>"'(),;:?%&#]+\.[a-z]{2,}$/i.test(value || '') ? value : '';

// A conservative URI budget: longer messages remain available through Copy.
export function emailDraftHref(person, text) {
  const route = contactRoute(person, 'email');
  if (!route.direct) return '';
  const {subject, body} = splitSubject(text);
  if (!body.trim() || gaps(text).length || /[\r\n]/.test(subject)) return '';
  const href = `${route.href}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(reading(body))}`;
  return href.length <= 2000 ? href : '';
}

export function contactRoute(person = {}, channel = '') {
  if (person.archived_at || person.exclusion || person.exclusion_contexts?.length
    || person.relationship === 'do_not_contact' || person.relevance?.reach?.tier === 'blocked')
    return {href:'',label:'',missing:'',field:'',direct:false};
  const first = firstName(person.name) || 'them';
  const email = emailAddress(person.email);
  const digits = String(person.phone || '').replace(/[\s().-]/g, '');
  const phone = /^\+?\d{7,15}$/.test(digits) ? digits : '';
  const intl = phone.startsWith('+') ? phone.slice(1) : phone.startsWith('00') ? phone.slice(2)
    : phone.startsWith('0') ? '44' + phone.slice(1) : '';
  let profile = '';
  try {
    const u = new URL(person.url || '');
    if (u.protocol === 'https:' && /(^|\.)linkedin\.com$/.test(u.hostname)) profile = u.href;
  } catch { /* Unstated or unsafe remains unavailable. */ }
  channel ||= person.preferred_channel || (email ? 'email' : profile ? 'linkedin' : phone ? 'call' : '');
  const chosen = {
    email: email && [`Email ${first}`, `mailto:${email}`],
    linkedin: profile && [`${first} on LinkedIn ↗`, profile],
    whatsapp: intl && [`WhatsApp ${first} ↗`, `https://wa.me/${intl}`],
    call: phone && [`Call ${first}`, `tel:${phone}`],
    text: phone && [`Text ${first}`, `sms:${phone}`],
  }[channel];
  const missing = ['whatsapp', 'call', 'text'].includes(channel) && (!phone || (channel === 'whatsapp' && !intl)) ? 'phone number'
    : channel === 'email' && !email ? 'email address' : '';
  return { href: chosen?.[1] || profile, label: chosen?.[0] || (profile ? `${first} on LinkedIn ↗` : ''),
    missing, field: missing === 'phone number' ? 'phone' : missing ? 'email' : '', direct: !!chosen };
}

export function weekBrief(item, person = {}, message = item.progress?.message?.text) {
  const p = item.progress || {}, plan = p.plan || {}, brief = item.brief || {};
  const own = (p.points || []).find(x => x.startsWith('Your note:'))?.replace(/^Your note:\s*/, '');
  const connection = own || item.access?.reason || 'The relationship context is not recorded.';
  const purpose = plan.role ? `${plan.role}${plan.goal ? `: ${plan.goal}` : ''}.`
    : ({ catch_up: 'A catch-up to learn what they are working on.', introduction: 'Ask whether they can suggest a useful introduction.',
      availability: 'Ask which current roles fit your availability.' })[p.ask] || 'The purpose of this approach is not recorded.';
  const unknowns = [...new Set([p.warning, ...(plan.gaps || [plan.check]), ...brief.unknowns || [],
    ...brief.constraints || [], /still a draft/.test(plan.cv || '') ? 'The suggested CV is still a draft; review it before sharing.' : '']
    .filter(x => typeof x === 'string' && x.trim()))];
  const result = { fit: `${connection.replace(/\.?$/, '.')} ${purpose}`, gaps: unknowns,
    label: p.label || 'Needs preparation', next: p.next || 'Review the recorded context.', action: 'View progress', field: '' };
  if (p.status === 'review') return { ...result, action: 'Review the note' };
  if (p.status === 'approved') return { ...result, action: 'Review and send' };
  if (!['ready', 'follow_up'].includes(p.status)) return result;
  const route = contactRoute(person, p.channel);
  if (route.missing) return { ...result, label: 'Needs contact detail', field: route.field,
    gaps: [`No usable ${route.missing} for the chosen channel.`, ...unknowns],
    action: `Add ${route.missing}`, next: `Add the ${route.missing}, or choose another channel in the person's details.` };
  if (!route.href && p.channel !== 'in_person') return { ...result, label: 'Needs contact detail',
    gaps: ['A usable contact route has not been recorded.', ...unknowns], action: 'Choose a contact route',
    next: 'Open their details and record how you will reach them.' };
  if (['call', 'in_person'].includes(p.channel)) return { ...result, label: 'Ready to contact',
    action: p.channel === 'call' ? 'Prepare your call' : 'Prepare your conversation',
    next: 'Review the talking points, have the conversation, then record what happened.' };
  if (!message?.trim()) return { ...result, label: 'Needs preparation', action: 'Prepare your note',
    gaps: ['No note has been prepared for this step.', ...unknowns], next: 'Start from the suggested template and make the note specific to this person.' };
  const missing = gaps(message);
  if (missing.length) return { ...result, label: 'Needs preparation', action: 'Finish your note',
    gaps: [`Fill in the note: ${missing.map(x => x.replaceAll('_', ' ')).join(', ')}.`, ...unknowns],
    next: 'Fill the marked gaps before copying or recording a send.' };
  return { ...result, label: 'Ready to review', action: p.status === 'follow_up' ? 'Review your follow-up' : 'Review and send',
    next: 'Review the note and open the contact link. After sending, record that you have been in touch.' };
}
