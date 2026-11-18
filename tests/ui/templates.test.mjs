// The owner's template pack as the note box uses it: no browser, external data or database. The pack is read the
// way the server reads it, and the browser's checks are compared with tools/note_context.py's on the same notes.
import assert from 'node:assert/strict';
import test from 'node:test';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as T from '../../workspace/js/template-text.js';
import * as F from '../../workspace/js/message-format.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = join(root, '.venv/bin/python');
const pack = JSON.parse(execFileSync(python, ['-c',
  'import json,sys; sys.path.insert(0, "workspace"); import search_config; print(json.dumps(search_config.templates()))'], { cwd: root }));
const person = { name: 'Dr Sam Sample-Jones', kind: 'former_colleague', relationship: 'active', preferred_ask: 'catch_up',
  linkedin: { observed_company: 'Acme Payments' } };

test('the note box starts from the template that fits, and a catch-up never becomes a referral request', () => {
  const cases = [
    [person, { kind: 'ask', progress: { ask: 'catch_up' } }, 'warm'],
    [{ ...person, preferred_ask: '' }, { kind: 'ask', progress: {} }, 'warm'],
    [{ ...person, preferred_ask: 'introduction' }, { kind: 'ask', progress: { ask: 'introduction' } }, 'former_colleague'],
    [{ kind: 'introducer', preferred_ask: 'introduction' }, { kind: 'ask' }, 'connector'],
    [{ kind: 'introducer', preferred_ask: 'catch_up' }, { kind: 'ask' }, 'warm'],
    [{ kind: 'former_manager', preferred_ask: 'catch_up' }, { kind: 'catch_up' }, 'former_manager'],
    [{ kind: 'friend' }, { kind: 'ask' }, 'friend'],
    [{ kind: 'peer' }, { kind: 'recruiter' }, 'recruiter'],
    [{ relationship: 'dormant' }, { kind: 'ask' }, 'warm'],
    [{ relationship: 'cold' }, { kind: 'ask' }, 'weak_tie'],
  ];
  for (const [who, item, expected] of cases) assert.equal(T.suggest(pack, who, item), expected, JSON.stringify(who));
});

test('the record fills names, shapes, proof points and the blurb; the rest stays a gap', () => {
  const fill = T.values(pack, { person, lead: 'fix', desk: 'qe' });
  assert.equal(fill.first_name, 'Sam');
  assert.equal(fill.company, '', 'LinkedIn\'s September company is offered, never written in');
  assert.deepEqual([fill.shape_1, fill.shape_2, fill.shape_3], [
    "coming into a system that's struggling and fixing how it ships", 'leading an engineering team', 'building AI products hands-on']);
  assert.match(fill.proof_1, /first customer-facing AI assistant.*personal data\.$/);
  assert.match(fill.proof_2, /about 1,000 end-to-end tests.*ownership\.$/);
  assert.equal(T.values(pack, { person: { ...person, organisation: 'Northwind' } }).company, 'Northwind');
  assert.equal(T.leadShape(pack, 'FDE'), 'build');
  assert.equal(T.leadShape(pack, ''), 'lead');
  assert.equal(T.deskFor(pack, 'QELead'), 'qe');
  assert.equal(T.cvKey({ Director: { title: 'Director of Engineering / CTO' } }, 'Director of Engineering / CTO (still a draft)'), 'Director');
  const text = T.render(pack, 'former_colleague', { channel: 'linkedin', fill });
  assert.deepEqual(T.gaps(text), ['personal_line', 'company', 'reason']);
  assert.ok(text.startsWith('Hi Sam,\n\n{{personal_line}}'));
  assert.equal(T.gaps(T.fillGap(text, 'company', 'Acme')).includes('company'), false);
});

test('each channel gets its own form: subject and blurb on email, the short version on WhatsApp', () => {
  const fill = T.values(pack, { person });
  const email = T.render(pack, 'friend', { channel: 'email', fill });
  assert.equal(T.splitSubject(email).subject, 'A small favour');
  assert.ok(T.splitSubject(email).body.startsWith('Hey Sam,'));
  assert.ok(email.endsWith(`---\n${pack.blurbs.paragraph}`));
  assert.ok(!T.render(pack, 'friend', { channel: 'linkedin', fill }).includes('Subject:'));
  assert.ok(!T.render(pack, 'friend', { channel: 'linkedin', fill }).includes('---'));
  assert.ok(T.render(pack, 'friend', { channel: 'whatsapp', fill }).startsWith('Hey Sam, quick favour.'));
  assert.ok(T.render(pack, 'warm', { channel: 'whatsapp', fill }).startsWith('Hi Sam,'), 'no short version: the body without a subject');
  // The blurb under the sign-off is not counted against the pack's length.
  assert.equal(T.words(email), T.words(T.render(pack, 'friend', { channel: 'linkedin', fill })));
});

test('choices for a gap: next months, other desks, founder problems and LinkedIn\'s company', () => {
  const now = new Date('2026-12-15T12:00:00Z');
  assert.deepEqual(T.choices(pack, 'month', { now }).map(c => c[1]), ['January', 'February', 'March']);
  assert.deepEqual(T.choices(pack, 'other_desk', { desk: 'leadership' }).map(c => c[1]), ['AI roles', 'quality engineering roles']);
  const [label, value, also] = T.choices(pack, 'problem_hypothesis')[1];
  assert.equal(value, 'releases have started to slip as the team grows');
  assert.equal(also.area, 'your release process');
  assert.deepEqual(T.choices(pack, 'company', { person }).map(c => c[1]), ['Acme Payments']);
  assert.ok(T.choices(pack, 'give_line').length >= 2);
  assert.deepEqual(T.choices(pack, 'day_rate'), [], 'the rate is never suggested');
});

test('the pack\'s sending window is read in London time, across the clock change', () => {
  const hint = pack.send_window.say;
  assert.equal(T.sendHint(pack, new Date('2026-10-20T07:00:00Z')), '', 'Tuesday 08:00 BST');
  assert.equal(T.sendHint(pack, new Date('2026-10-20T06:59:00Z')), hint, 'Tuesday 07:59 BST');
  assert.equal(T.sendHint(pack, new Date('2026-10-22T08:59:00Z')), '', 'Thursday 09:59 BST');
  assert.equal(T.sendHint(pack, new Date('2026-10-22T09:00:00Z')), hint, 'Thursday 10:00 BST');
  assert.equal(T.sendHint(pack, new Date('2026-10-19T08:00:00Z')), hint, 'Monday');
  assert.equal(T.sendHint(pack, new Date('2026-11-03T08:00:00Z')), '', 'Tuesday 08:00 GMT');
});

test('the site and the notes tool give the same checks for the same note', () => {
  const notes = [
    ['warm', "Subject: Catching up\n\nHi Sam,\n\nHope you're well.\n\nI'm passionate about robust QA, see https://www.google.com/url?q=x and portfolio.example.com — thanks.\n\nMorgan"],
    ['warm', `Hi Sam,\n\n${'word '.repeat(170)}\n\nMorgan`],
    ['recruiter', 'Hi Riley,\n\nQuick summary:\n- Interim Head of QE, ideally on an AI product.\n- About 200 RAG tests.\n\nMorgan'],
    ['friend', 'Hey Sam, News from me: I was made redundant as Head of Quality Engineering. 31.4% and 0.5% flake rate, 1:20.'],
    ['weak_tie', 'Hi Sam, we met at the meetup. Looking for QE or Test Manager roles.\n\n- one\n- two'],
    ['former_colleague', 'Hi Sam, leading an engineering team or a Head of QE role. portfolio.example.com'],
  ];
  const python_ = JSON.parse(execFileSync(python, ['-c', [
    'import json,sys', 'sys.path.insert(0, "tools")', 'import note_context',
    'notes = json.load(sys.stdin)',
    'print(json.dumps([[w for w in note_context.lint_note(text, template=tid) if not w.startswith("Gaps still")] for tid, text in notes]))',
  ].join('\n')], { cwd: root, input: JSON.stringify(notes) }));
  notes.forEach(([id, text], n) => assert.deepEqual(T.checks(pack, text, { id }), python_[n], `note ${n + 1} (${id})`));
  assert.ok(python_.every((warnings, n) => n === notes.length - 1 ? warnings.length === 0 : warnings.length > 0));
});

test('every first approach links to the site; follow-ups and thank-yous need not', () => {
  const missing = pack.first_approach.missing;
  const bare = 'Hi Sam,\n\nWe last spoke at the roundtable.\n\nWould you be up for a call?\n\nThanks,\nMorgan';
  for (const id of ['warm', 'recruiter', 'founder', '']) assert.ok(T.checks(pack, bare, { id }).includes(missing), id);
  for (const item of pack.followups.items) assert.ok(!T.checks(pack, bare, { id: item.id }).includes(missing), item.id);
  assert.ok(!T.checks(pack, bare, { id: '', first: false }).includes(missing), 'a blank follow-up');
  assert.ok(!T.checks(pack, bare.replace('call?', 'call? More at portfolio.example.com'), { id: 'warm' }).includes(missing));
  for (const t of pack.templates) for (const channel of ['email', 'linkedin', 'whatsapp'])
    assert.ok(!T.checks(pack, T.render(pack, t.id, { channel }), { id: t.id }).includes(missing), `${t.id} on ${channel}`);
});

test('the CV space adds the pack line before the sign-off, keeps the blurb, and takes it out again', () => {
  const line = pack.first_approach.cv_line;
  const warm = T.render(pack, 'warm', { channel: 'linkedin' });
  assert.equal(T.cvAttached(pack, warm), false);
  const added = T.addCvLine(pack, warm);
  assert.ok(added.endsWith(`${line}\n\nThanks,\nMorgan`), added.slice(-80));
  assert.equal(T.cvAttached(pack, added), true);
  assert.equal(T.hasCvLine(pack, added), true);
  assert.equal(T.addCvLine(pack, added), added, 'never twice');
  assert.equal(T.removeCvLine(pack, added), warm);
  const friend = T.render(pack, 'friend', { channel: 'email' });
  const withBlurb = T.addCvLine(pack, friend);
  assert.ok(withBlurb.includes(`${line}\n\nMorgan\n\n---\n`), 'above the sign-off, the forwardable paragraph still under it');
  assert.equal(T.removeCvLine(pack, withBlurb), friend);
  assert.equal(T.addCvLine(pack, 'Hi Sam,\n\nOne line.\n\nAlex'), `Hi Sam,\n\nOne line.\n\n${line}\n\nAlex`, 'any one-word name signs off');
  assert.equal(T.addCvLine(pack, 'Hi Sam,\n\nOne line.\n\nSee you soon.'), `Hi Sam,\n\nOne line.\n\nSee you soon.\n\n${line}`, 'a closing sentence is not a sign-off');
  const founder = T.addCvLine(pack, T.render(pack, 'founder', { channel: 'email' }));
  assert.ok(founder.endsWith(`${line}\n\nMorgan\nportfolio.example.com`), 'the site link stays in the signature');
  const recruiter = T.render(pack, 'recruiter', { channel: 'email' });
  assert.equal(T.cvAttached(pack, recruiter), true, "the recruiter email's own \"CV attached\" is its CV space");
  assert.equal(T.addCvLine(pack, recruiter), recruiter);
  assert.equal(T.hasCvLine(pack, recruiter), false, 'its own wording is not taken out');
  assert.equal(T.cvAttached(pack, 'Ask me first before my CV goes to anyone, please.'), false);
  assert.deepEqual(T.checks(pack, added, { id: 'warm' }), T.checks(pack, warm, { id: 'warm' }), 'the line trips no check');
});

test('a note reads as emphasis and lists, and the pack\'s own notes do not trip the bullet warning', () => {
  const plain = 'Hi Sam,\n\nOne line.\n\nThanks,\nMorgan';
  assert.equal(F.structured(plain), false);
  assert.equal(F.reading(plain), plain);
  const note = [
    'Hi Sam,', '',
    '**Would you be up for a call?** I can start with:',
    '1. leading an engineering team',
    '2. building AI products hands-on', '',
    'Quick summary:',
    '- 15 years across defence, retail and SaaS.',
    '- Now independent.', '',
    'Thanks,', 'Morgan',
  ].join('\n');
  assert.equal(F.reading(note).includes('**'), false);
  assert.match(F.reading(note), /Would you be up for a call\?/);
  const parsed = F.blocks(note);
  assert.equal(parsed[1].text, '**Would you be up for a call?** I can start with:');
  assert.deepEqual(parsed[2], { type: 'ol', items: ['leading an engineering team', 'building AI products hands-on'] });
  assert.deepEqual(parsed[4], { type: 'ul', items: ['15 years across defence, retail and SaaS.', 'Now independent.'] });
  assert.equal(parsed.at(-1).text, 'Thanks,\nMorgan');
  const html = F.readingHtml(note);
  assert.match(html, /<strong>Would you be up for a call\?<\/strong>/);
  assert.match(html, /<ol><li>leading an engineering team<\/li><li>building AI products hands-on<\/li><\/ol>/);
  assert.match(html, /<ul><li>15 years across defence, retail and SaaS\.<\/li><li>Now independent\.<\/li><\/ul>/);
  assert.match(F.readingHtml('**See <portfolio> & "quotes".**'), /<strong>See &lt;portfolio&gt; &amp; &quot;quotes&quot;\.<\/strong>/);
  assert.doesNotMatch(html, /<script|onerror/);
  for (const id of ['warm', 'former_colleague', 'former_manager', 'connector', 'friend', 'weak_tie', 'founder', 'recruiter', 'recruiter_wrong_role', 'bump']) {
    const text = T.render(pack, id, { channel: 'linkedin', fill: T.values(pack, { person }) });
    assert.equal(F.structured(text), true, id);
    assert.equal(T.checks(pack, text, { id }).some(w => w.startsWith('Formatting')), false, id);
  }
});
