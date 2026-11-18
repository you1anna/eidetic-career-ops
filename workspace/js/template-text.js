// The owner's outreach templates (config/templates.yml, sent as state.templates): which one suits a person,
// what the record fills in, the gaps left for him, and the checks every note gets. No page code here, so
// tests/ui/templates.test.mjs runs it without a browser; tools/note_context.py applies the same checks.

const GAP = /\{\{\s*([a-z0-9_]+)\s*\}\}/g;
// Link-like text: a scheme, or a domain under a common ending (portfolio.example.com, example.co.uk).
const LINK = /\b(?:https?:\/\/)?(?:[a-z0-9-]+\.)+(?:com|co\.uk|org\.uk|uk|io|ai|engineering|dev|org|net|app|co|me)\b(?:\/[^\s)]*)?/gi;
const REDIRECT = /\bgoogle\.[a-z.]+\/url\?/i;
const SHORT_CHANNELS = new Set(['whatsapp', 'text']);

export function firstName(name) {
  return String(name || '').trim().replace(/^(dr|mr|mrs|ms|miss|prof)\.?\s+/i, '').split(/\s+/)[0] || '';
}

export function gaps(text) {
  return [...new Set([...String(text || '').matchAll(GAP)].map(m => m[1]))];
}

export const template = (pack, id) => (pack?.templates || []).find(t => t.id === id) ||
  (pack?.followups?.items || []).find(t => t.id === id) || null;

// A follow-up or thank-you comes after the first note, which already carried the link and the CV.
export const isFollowup = (pack, id) => (pack?.followups?.items || []).some(t => t.id === id);

// The CV prepared for someone is shown by its title; find its key in config/cvs.yml.
export function cvKey(cvs, title) {
  const shown = String(title || '');
  return Object.entries(cvs || {}).find(([, cv]) => cv.title && shown.startsWith(cv.title))?.[0] || '';
}

// Which template the note box starts from. The recorded ask comes first: a catch-up ask (also the
// default when none is set) never turns into a referral request.
export function suggest(pack, person = {}, item = {}) {
  const rules = pack?.suggest || {};
  const kind = person.kind || '';
  let id = item.kind === 'recruiter' ? 'recruiter' : rules.by_kind?.[kind] || '';
  const ask = item.progress?.ask || person.preferred_ask || 'catch_up';
  if (id && ask === 'catch_up' && rules.when_catch_up?.[kind]) id = rules.when_catch_up[kind];
  if (!id) id = (rules.known_relationships || []).includes(person.relationship) ? 'warm' : rules.otherwise || 'weak_tie';
  return template(pack, id) ? id : pack?.templates?.[0]?.id || '';
}

export function leadShape(pack, cv) {
  const shapes = pack?.shapes || [];
  return (shapes.find(s => (s.cvs || []).includes(cv)) || shapes[0])?.id || '';
}

export function deskFor(pack, cv) {
  const desks = pack?.desks || [];
  return (desks.find(d => (d.cvs || []).includes(cv)) || desks[0])?.id || '';
}

function shapeOrder(pack, lead) {
  const shapes = pack?.shapes || [];
  const first = shapes.find(s => s.id === lead) || shapes[0];
  return first ? [first, ...shapes.filter(s => s !== first)] : [];
}

const withStop = text => (text && !/[.!?]$/.test(text) ? `${text}.` : text || '');

// What the record and the pack fill in. Anything not here stays a {{gap}}.
export function values(pack, { person = {}, lead = '', desk = '' } = {}) {
  const shapes = shapeOrder(pack, lead);
  const d = (pack?.desks || []).find(x => x.id === desk) || pack?.desks?.[0] || {};
  const proof = n => withStop((pack?.proofs?.items || []).find(p => p.id === (d.proofs || [])[n])?.text);
  const out = {
    first_name: firstName(person.name),
    company: String(person.organisation || '').trim(),
    blurb_paragraph: pack?.blurbs?.paragraph || '',
    role_headline: d.role_headline || '', role_targets: d.role_targets || '',
    proof_1: proof(0), proof_2: proof(1),
  };
  shapes.forEach((s, n) => { out[`shape_${n + 1}`] = s.wording; });
  return out;
}

// The text for a channel: email keeps the subject line and the forwardable paragraph; LinkedIn and
// WhatsApp start at the greeting, and WhatsApp or a text uses the short version where there is one.
export function render(pack, id, { channel = '', fill = {} } = {}) {
  const t = template(pack, id);
  if (!t) return '';
  const short = SHORT_CHANNELS.has(channel) && t.short;
  let text = short || t.body || '';
  if (channel === 'email' && !short) {
    if (t.blurb) text += '\n\n---\n{{blurb_paragraph}}';
    if (t.subject) text = `Subject: ${t.subject}\n\n${text}`;
  }
  return text.replace(GAP, (whole, name) => fill[name] || whole);
}

// Put `value` in place of every {{name}} gap.
export function fillGap(text, name, value) {
  return String(text || '').replace(new RegExp(`\\{\\{\\s*${name}\\s*\\}\\}`, 'g'), () => value);
}

export function splitSubject(text) {
  const m = /^Subject:[ \t]*(.*)\r?\n(?:[ \t]*\r?\n)?/.exec(String(text || ''));
  return m ? { subject: m[1].trim(), body: text.slice(m[0].length) } : { subject: '', body: String(text || '') };
}

// Words in the message itself: not the subject line, and not the forwardable paragraph under the sign-off.
export function words(text) {
  const main = splitSubject(text).body.split(/\n-{3,}[ \t]*\n/)[0];
  return (main.match(/\S+/g) || []).length;
}

const pattern = (rule, extra = '') => new RegExp(rule.match, (rule.case === 'sensitive' ? '' : 'i') + (rule.multiline ? 'm' : '') + extra);

// Every way this note departs from the pack, worded for the owner. Gaps are reported separately: they block
// copying and recording the send. A first approach (anything but a follow-up) must link to the site.
export function checks(pack, text, { id = '', first = !isFollowup(pack, id) } = {}) {
  const note = String(text || '');
  if (!note.trim() || !pack) return [];
  const t = template(pack, id) || {};
  const waived = new Set(t.waive || []);
  const out = [];
  for (const rule of pack.dont_say || []) {
    const hits = [...new Set([...note.matchAll(pattern(rule, 'g'))].map(m => m[0].trim()))];
    if (hits.length) out.push(`Don't say ${hits.map(h => `"${h}"`).join(' or ')}: ${rule.why}.`);
  }
  for (const rule of pack.checks || []) {
    if (waived.has(rule.id) || !pattern(rule).test(note)) continue;
    if (rule.unless && pattern({ ...rule, match: rule.unless }).test(note)) continue;
    out.push(rule.say);
  }
  const count = words(note);
  if (t.words && count > t.words) out.push(`About ${count} words: the pack keeps these under ${t.words}.`);
  const hosts = new Set((note.match(LINK) || []).map(link =>
    link.toLowerCase().replace(/^https?:\/\//, '').replace(/^www\./, '').split('/')[0]));
  const site = pack.first_approach?.link;
  if (first && site && !note.toLowerCase().includes(site)) out.push(pack.first_approach.missing);
  if (hosts.size > 1) out.push(`${hosts.size} links: the pack sends one, portfolio.example.com.`);
  if (REDIRECT.test(note)) out.push('A link is wrapped in a Google redirect: write the bare domain.');
  return [...new Set(out)];
}

// The CV space in a first approach. The CV goes as an attachment; the note says so in its own words
// or with the pack's line, which the note box adds before the sign-off and takes out again.
// A closing phrase, or a first line that is a single capitalised word: the sender's name.
const CLOSING = /^(?:thanks|many thanks|cheers|best|all the best)\b/i;
const NAME_LINE = /^\p{Lu}[\p{Ll}'’-]+(?:\n|$)/u;
const isSignOff = para => CLOSING.test(para) || NAME_LINE.test(para);

export function cvAttached(pack, text) {
  const rule = pack?.first_approach?.cv_attached;
  return !!rule && new RegExp(rule, 'i').test(String(text || ''));
}

export function addCvLine(pack, text) {
  const line = pack?.first_approach?.cv_line;
  const note = String(text || '');
  if (!line || cvAttached(pack, note)) return note;
  // The forwardable paragraph stays under the sign-off; the line goes in the message above it.
  const cut = note.search(/\n-{3,}[ \t]*\n/);
  const main = cut < 0 ? note : note.slice(0, cut), rest = cut < 0 ? '' : note.slice(cut);
  const paras = main.replace(/\s+$/, '').split(/\n[ \t]*\n/);
  const at = paras.length > 1 && isSignOff(paras.at(-1).trim()) ? paras.length - 1 : paras.length;
  paras.splice(at, 0, line);
  return paras.join('\n\n') + (rest ? `\n${rest}` : '');
}

const paragraphs = text => String(text || '').split(/\n[ \t]*\n/);

// The pack's own line, as a paragraph of its own: the one form the note box can take out again.
export const hasCvLine = (pack, text) => !!pack?.first_approach?.cv_line &&
  paragraphs(text).some(p => p.trim() === pack.first_approach.cv_line);

export function removeCvLine(pack, text) {
  if (!hasCvLine(pack, text)) return String(text || '');
  return paragraphs(text).filter(p => p.trim() !== pack.first_approach.cv_line).join('\n\n');
}

// The pack's sending window, in London time; empty when now is inside it.
export function sendHint(pack, now = new Date()) {
  const w = pack?.send_window;
  if (!w) return '';
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', weekday: 'short', hour: 'numeric', hourCycle: 'h23' })
    .formatToParts(now).map(p => [p.type, p.value]));
  const day = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(parts.weekday);
  const hour = Number(parts.hour);
  return (w.days || []).includes(day) && hour >= w.from_hour && hour < w.to_hour ? '' : w.say || '';
}

// Choices worked out on the page for a gap: the next months, the other desks, the founder problems and
// the company LinkedIn showed in September. Each is [label, value, also] where `also` fills further gaps.
export function choices(pack, name, { person = {}, desk = '', now = new Date() } = {}) {
  const spec = pack?.placeholders?.[name] || {};
  const out = (spec.examples || []).map(text => [text, text]);
  if (spec.suggest === 'months') {
    for (let n = 1; n <= 3; n++) {
      const month = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + n, 1)).toLocaleString('en-GB', { month: 'long', timeZone: 'UTC' });
      out.push([month, month]);
    }
  }
  if (spec.suggest === 'other_desks') {
    // Mid-sentence: "engineering leadership roles", but "AI roles".
    const words = label => label.split(' ').map(w => (w === w.toUpperCase() ? w : w.toLowerCase())).join(' ');
    (pack.desks || []).filter(d => d.id !== desk).forEach(d => out.push([`${words(d.label)} roles`, `${words(d.label)} roles`]));
  }
  if (spec.suggest === 'problems')
    (pack.problems || []).forEach(p => out.push([p[name], p[name], p]));
  if (spec.suggest === 'linkedin_company') {
    const seen = String(person.linkedin?.observed_company || '').trim();
    if (seen) out.push([`${seen} (per LinkedIn, Sep 2026)`, seen]);
  }
  return out;
}
