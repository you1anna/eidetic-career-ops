import test from 'node:test';
import assert from 'node:assert/strict';
import { contactRoute, weekBrief, isKnownContact, emailDraftHref } from '../../workspace/js/week-brief.js';

test('email drafts encode exact readable wording and have a bounded safe fallback', () => {
  const who = {email:'alex+work@example.com'};
  const href = emailDraftHref(who, 'Subject: Catch up & introductions\n\nHi Alex,\n\n**Two examples** & a question?');
  const uri = new URL(href);
  assert.equal(uri.pathname, who.email);
  assert.equal(uri.searchParams.get('subject'), 'Catch up & introductions');
  assert.equal(uri.searchParams.get('body'), 'Hi Alex,\n\nTwo examples & a question?');
  for (const email of ['a@example.com?bcc=other@example.com','a@example.com\nbcc:x','a@example.com#x',''])
    assert.equal(emailDraftHref({email}, 'Hello'), '');
  assert.equal(emailDraftHref(who, 'Fill {{reason}}'), '');
  assert.equal(emailDraftHref(who, 'x'.repeat(2100)), '');
  assert.equal(emailDraftHref(who, ''), '');
});

test('known contact presentation uses an explicit relationship, never a guess from prose', () => {
  for (const relationship of ['warm', 'active', 'dormant']) assert.ok(isKnownContact({ relationship }));
  for (const relationship of ['cold', 'none', 'do_not_contact', undefined]) assert.equal(isKnownContact({ relationship }), false);
  assert.equal(isKnownContact({ known_context: 'A friendly colleague', kind: 'friend' }), false);
});

const person = { type: 'person', name: 'Alex Example', email: 'alex@example.com' };
test('contact handoff defaults to a recorded email then LinkedIn and respects explicit channels and exclusions', () => {
  const p = {...person, url:'https://www.linkedin.com/in/synthetic-alex', phone:'+44 7700 900123'};
  assert.equal(contactRoute(p).href, 'mailto:alex@example.com');
  assert.equal(contactRoute({...p,email:''}).href, 'https://www.linkedin.com/in/synthetic-alex');
  assert.equal(contactRoute(p,'linkedin').href, 'https://www.linkedin.com/in/synthetic-alex');
  assert.equal(contactRoute(p,'call').href, 'tel:+447700900123');
  for (const fields of [{relationship:'do_not_contact'}, {archived_at:'2026-10-01'},
    {exclusion:{reason:'Excluded'}}, {exclusion_contexts:['C']}, {relevance:{reach:{tier:'blocked'}}}]) {
    const route = contactRoute({...p,...fields}, 'email');
    assert.equal(route.href, '', JSON.stringify(fields));
    assert.equal(route.missing, '');
  }
  for (const email of ['alex@example.com?bcc=other@example.com','alex@example.com\nBcc: other@example.com'])
    assert.equal(contactRoute({...p,email,url:''}).href, 'tel:+447700900123');
});
const make = (progress = {}, rest = {}) => ({ kind: 'ask', access: { reason: 'You recorded a warm relationship' },
  brief: { unknowns: ['Whether they can offer an introduction'] },
  progress: { status: 'ready', channel: 'email', ask: 'introduction', points: ['Your note: Worked on the payments team together.'],
    message: { text: 'Hi Alex, could we catch up?' }, plan: { role: 'Connector', goal: 'A useful introduction', gaps: ['Their current remit'] },
    next: 'Send your ask.', ...progress }, ...rest });

test('fit is the recorded relationship and purpose; readiness does not invent a vacancy', () => {
  const b = weekBrief(make(), person);
  assert.match(b.fit, /payments team/);
  assert.match(b.fit, /Connector/);
  assert.equal(b.label, 'Ready to review');
  assert.equal(b.action, 'Review and send');
  assert.deepEqual(b.gaps, ['Their current remit', 'Whether they can offer an introduction']);
  assert.match(b.next, /review/i);
});

test('missing preferred contact details remain a gap even with a LinkedIn fallback', () => {
  const p = { ...person, email: '', url: 'https://www.linkedin.com/in/synthetic' };
  assert.equal(contactRoute(p, 'email').missing, 'email address');
  const b = weekBrief(make(), p);
  assert.equal(b.label, 'Needs contact detail');
  assert.equal(b.action, 'Add email address');
  assert.equal(b.field, 'email');
  assert.ok(b.gaps.some(g => /email address/.test(g)));
});

test('no note and unresolved template placeholders cannot appear ready', () => {
  assert.equal(weekBrief(make({ message: null }), person).action, 'Prepare your note');
  const b = weekBrief(make({ message: { text: 'Hello {{first_name}}, {{specific_ask}}' } }), person);
  assert.equal(b.label, 'Needs preparation');
  assert.equal(b.action, 'Finish your note');
  assert.ok(b.gaps.some(g => g.includes('first name') && g.includes('specific ask')));
});

test('calls need a usable phone but do not require a message or a finished CV', () => {
  const b = weekBrief(make({ channel: 'call', message: null,
    plan: { cv: 'Director (still a draft)', role: 'Connector', goal: 'Catch up' } }), { ...person, phone: '07700 900123' });
  assert.equal(b.label, 'Ready to contact');
  assert.equal(b.action, 'Prepare your call');
  assert.ok(b.gaps.some(g => /CV.*draft/.test(g)));
  assert.equal(contactRoute({ ...person, phone: '07700 900123' }, 'whatsapp').href, 'https://wa.me/447700900123');
});

test('a reply or recorded send keeps its real next step and does not demand another note', () => {
  const b = weekBrief(make({ status: 'waiting', label: 'Waiting for a reply', message: null,
    next: 'Sent 17 Oct. A follow-up is suggested from 24 Oct if nothing comes back.' }), person);
  assert.equal(b.label, 'Waiting for a reply');
  assert.equal(b.action, 'View progress');
  assert.match(b.next, /24 Oct/);
  assert.ok(!b.gaps.some(g => /No note/.test(g)));
});

test('approval and blocked states are never converted into ready-to-send advice', () => {
  assert.equal(weekBrief(make({ status: 'review', label: 'Note to review' }), person).action, 'Review the note');
  const b = weekBrief(make({ status: 'held', label: 'On hold', next: 'Held by your decision.' }), person);
  assert.equal(b.label, 'On hold');
  assert.equal(b.next, 'Held by your decision.');
});

test('sparse records and untrusted links stay unknown', () => {
  const b = weekBrief(make({ plan: null, points: [] }, { access: null, brief: {} }), { type: 'person', url: 'javascript:alert(1)' });
  assert.match(b.fit, /not recorded/);
  assert.equal(contactRoute({ url: 'https://evil.example/linkedin.com' }, 'linkedin').href, '');
  assert.equal(b.label, 'Needs contact detail');
});


test('working limit explains automatic additions and personal exceptions', async () => {
  const {capacityText} = await import('../../workspace/js/week.js');
  assert.equal(capacityText({working_limit:10, additions_per_seven_days:10}),
    'Up to 10 opportunities to consider. Saved jobs and active processes stay separate. Everything else is searchable reference, with no review required. At most 10 new opportunities are added automatically over seven days.');
  assert.equal(capacityText(null), '');
});


test('research capacity is distinguished from visible working places', async () => {
 const {capacityText} = await import('../../workspace/js/week.js');
 assert.equal(capacityText({working_limit:10,research_pool_limit:30,additions_per_seven_days:60}), 'Up to 10 opportunities; 30 candidates available for research. At most 60 new records per seven days. Saved work stays separate.');
});
