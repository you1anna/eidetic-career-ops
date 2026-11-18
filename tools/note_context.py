#!/usr/bin/env python3
"""Everything gathered about one person, in one place, for writing the note on their card
(.claude/commands/notes.md). Local only: it prints to the terminal and writes nothing.

  ops/wo-live show <person-id> | .venv/bin/python tools/note_context.py -

It prints the hosted card facts from `show`, then what stays on this Mac: LinkedIn threads between
the user and that person alone (a group thread is not opened, and a message that touches a private
topic from the profile is held, never printed), and every section of data/research/*.md that names them.
Use it to choose what the note says; never copy a message body into the note, research JSON or git.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "workspace"))
from linkedin_export import _profile  # noqa: E402
import profile_rules  # noqa: E402  the one private-topic screen and the user's own identity
import search_config  # noqa: E402  the template pack, read the way the site reads it


def own_profile() -> str:
    """The user's LinkedIn profile URL (config/profile.yml candidate.linkedin)."""
    return _profile(profile_rules.current().own_linkedin)


def _url(value):
    return (value or '').strip().rstrip('/').lower()


def threads(messages_csv: Path, profile: str) -> list[list[dict]]:
    """Two-person LinkedIn threads with this profile, oldest message first."""
    profile = _url(profile)
    if not profile or not messages_csv.exists():
        return []
    convs = {}
    with messages_csv.open(encoding='utf-8') as f:
        for m in csv.DictReader(f):
            convs.setdefault(m['CONVERSATION ID'], []).append(m)
    out = []
    for ms in convs.values():
        people = {_url(m['SENDER PROFILE URL']) for m in ms}
        people |= {_url(u) for m in ms for u in (m['RECIPIENT PROFILE URLS'] or '').split(',')}
        people -= {'', _url(own_profile())}
        if people == {profile}:
            out.append(sorted(ms, key=lambda m: m['DATE']))
    return sorted(out, key=lambda ms: ms[-1]['DATE'])


def sections(research: Path, name: str) -> list[tuple[str, str]]:
    """(file, section) for each heading block in the local research notes that names the person."""
    found = []
    if not research.exists():
        return found
    for path in sorted(research.rglob('*.md')):
        blocks = re.split(r'(?m)^(?=#{2,3} )', path.read_text(encoding='utf-8'))
        for b in blocks:
            if not b.startswith('#') or name.lower() not in b.lower():
                continue
            lines = b.strip().splitlines()
            # A long section is usually a table about everyone: keep its heading and their lines.
            if len(lines) > 30:
                lines = [lines[0], *(x for x in lines[1:] if name.lower() in x.lower())]
            found.append((str(path.relative_to(research)), '\n'.join(lines)))
    return found


def _iter_items(data, key: str) -> list[dict]:
    if isinstance(data, dict):
        return [x for x in (data.get(key) or []) if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def correspondence(research: Path, show: dict) -> list[str]:
    """Safe correspondence facts (emails, 1:1 dates, shared group domains/colleagues) from local mining."""
    corr_root = research / 'correspondence'
    if not corr_root.exists():
        return []
    pid = show.get('id') or ''
    name = (show.get('name') or '').strip()
    if not name:
        return []
    rules = profile_rules.current()
    legal_domains = set()
    ld_file = corr_root / 'legal-domains.json'
    if ld_file.exists():
        legal_domains = {d.lower() for d in (json.loads(ld_file.read_text(encoding='utf-8')).get('domains') or [])}
    held_threads = set()
    for lf in sorted(corr_root.glob('*/raw/legal-*.json')):
        for item in _iter_items(json.loads(lf.read_text(encoding='utf-8')), 'threads'):
            tid = item.get('id') or item.get('threadId')
            if tid:
                held_threads.add(tid)

    emails = {show['email'].lower()} if show.get('email') else set()
    for pf in sorted(corr_root.glob('*/people.json')):
        for row in _iter_items(json.loads(pf.read_text(encoding='utf-8')), 'people'):
            if (pid and row.get('id') == pid) or (row.get('name') or '').lower() == name.lower():
                for e in (row.get('emails') or []) + (row.get('aliases') or []) + (row.get('existing') or []):
                    if isinstance(e, str) and '@' in e:
                        emails.add(e.lower())

    direct, groups, co_people, co_domains = [], [], set(), set()
    name_pat = re.compile(re.escape(name), re.I)
    name_tokens = [t.lower() for t in name.split() if len(t) > 2]
    for mf in sorted(corr_root.glob('*/raw/mail-*.json')):
        raw = json.loads(mf.read_text(encoding='utf-8'))
        msgs = []
        for t in _iter_items(raw, 'threads'):
            if (t.get('id') or t.get('threadId')) in held_threads:
                continue
            if 'messages' in t:
                msgs.extend(m for m in (t.get('messages') or []) if isinstance(m, dict))
            else:
                msgs.append(t)
        for m in msgs:
            if (m.get('threadId') or m.get('id')) in held_threads:
                continue
            hs = m.get('headers') or {}
            sender = str(m.get('sender') or hs.get('From') or '')
            to_rec = ', '.join(m.get('toRecipients') or []) if 'toRecipients' in m else str(hs.get('To') or '')
            cc_rec = ', '.join(m.get('ccRecipients') or []) if 'ccRecipients' in m else str(hs.get('Cc') or '')
            subj = ' '.join(str(m.get('subject') or hs.get('Subject') or '').split())[:90]
            blob = ' '.join([sender, to_rec, cc_rec, subj])
            if rules.is_private(blob) or any(d in blob.lower() for d in legal_domains):
                continue
            addrs = [a.lower() for a in re.findall(r'[\w.+-]+@[\w.-]+', ' '.join([sender, to_rec, cc_rec]))]
            matched = bool(name_pat.search(blob) or (emails and set(addrs) & emails))
            if not matched and name_tokens and len(name_tokens) >= 2:
                matched = any(all(tok in a for tok in name_tokens) for a in addrs)
            if not matched:
                continue
            for a in addrs:
                if name_tokens and all(tok in a for tok in name_tokens):
                    emails.add(a)
            others = [a for a in set(addrs) if not rules.is_own(a)]
            date = (m.get('date') or '')[:10]
            direction = m.get('direction') or ('out' if rules.is_own(sender) else 'in')
            if len(others) <= 1:
                label = f"  {date} ({direction})" + (f": {subj}" if subj else "")
                direct.append(label)
            else:
                groups.append(date)
                for a in others:
                    if a not in emails:
                        co_people.add(a)
                        dom = a.split('@', 1)[-1]
                        if dom not in {'gmail.com', 'hotmail.com', 'yahoo.co.uk', 'yahoo.com', 'outlook.com', 'icloud.com'}:
                            co_domains.add(dom)

    out = []
    if emails:
        out.append('  Emails: ' + ', '.join(sorted(emails)))
    if direct:
        s_direct = sorted(set(direct))
        out.append(f"  Direct emails ({len(s_direct)}):")
        out.extend(s_direct[-6:])
    if groups:
        s_dates = sorted(d for d in groups if d)
        span = f"{s_dates[0]} to {s_dates[-1]}" if s_dates else 'unknown dates'
        dom_txt = f"; shared domains: {', '.join(sorted(co_domains)[:6])}" if co_domains else ''
        peer_txt = f"; co-recipients: {', '.join(sorted(co_people)[:6])}" if co_people else ''
        out.append(f"  Group threads ({len(groups)}, {span}){dom_txt}{peer_txt}")
    return out


def own_voice(found_threads: list[list[dict]]) -> list[str]:
    """The user's own sent messages in two-person LinkedIn threads with this person."""
    rules, own, sent = profile_rules.current(), _url(own_profile()), []
    for ms in found_threads:
        for m in ms:
            if _url(m.get('SENDER PROFILE URL')) != own:
                continue
            body = ' '.join((m.get('CONTENT') or '').split())
            if body and not rules.is_private(body):
                sent.append(f"  {m['DATE'][:10]}: {body[:300]}")
    return sent[-6:]


def cv_positioning(cvs_path: Path = ROOT / 'config/cvs.yml') -> list[str]:
    """Concise summary of the owner's six CV lanes so outreach never pigeonholes him as QE-only."""
    lines = [
        '  Anti-pigeonholing rule: Do NOT box the owner into "QE / Quality Engineering / Test Manager" only.',
        '  Across his 6 CVs, he leads engineering functions, recovers/builds production & AI systems, and leads quality:',
    ]
    if cvs_path.exists():
        for key, cv in (yaml.safe_load(cvs_path.read_text(encoding='utf-8')) or {}).items():
            if isinstance(cv, dict) and cv.get('title'):
                lines.append(f"  - {key} ({cv['title']}): {cv.get('summary') or cv['title']}")
    return lines


# The owner's template pack (config/templates.yml): the same templates and checks the site's note box uses
# (workspace/js/template-text.js applies them in the browser).
GAP = re.compile(r'\{\{\s*([a-z0-9_]+)\s*\}\}')
LINK = re.compile(r'\b(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|co\.uk|org\.uk|uk|io|ai|engineering|dev|org|net|app|co|me)\b'
                  r'(?:/[^\s)]*)?', re.I)
REDIRECT = re.compile(r'\bgoogle\.[a-z.]+/url\?', re.I)


def template_pack() -> dict:
    return search_config.templates()


def _template(pack: dict, template_id: str) -> dict:
    items = [*(pack.get('templates') or []), *((pack.get('followups') or {}).get('items') or [])]
    return next((t for t in items if t.get('id') == template_id), {})


def suggest_template(show: dict, pack: dict | None = None) -> str:
    """The template the site's note box starts from for this person (template-text.js `suggest`)."""
    pack = pack if pack is not None else template_pack()
    rules = pack.get('suggest') or {}
    kind = show.get('kind') or ''
    tid = (rules.get('by_kind') or {}).get(kind, '')
    # The recorded ask comes first: a catch-up (also the default) never turns into a referral request.
    if tid and (show.get('preferred_ask') or 'catch_up') == 'catch_up' and (rules.get('when_catch_up') or {}).get(kind):
        tid = rules['when_catch_up'][kind]
    if not tid:
        tid = 'warm' if show.get('relationship') in (rules.get('known_relationships') or []) else rules.get('otherwise', 'weak_tie')
    return tid if _template(pack, tid) else ''


def _pattern(rule: dict, key: str = 'match'):
    flags = (0 if rule.get('case') == 'sensitive' else re.I) | (re.M if rule.get('multiline') else 0)
    return re.compile(rule[key], flags)


def _words(text: str) -> int:
    """Words in the message itself: not the subject line, nor the forwardable paragraph under the sign-off."""
    body = re.sub(r'^Subject:[ \t]*.*\r?\n(?:[ \t]*\r?\n)?', '', text, count=1)
    return len(re.split(r'\n-{3,}[ \t]*\n', body)[0].split())


def is_followup(pack: dict, template_id: str) -> bool:
    return any(t.get('id') == template_id for t in (pack.get('followups') or {}).get('items') or [])


def lint_note(text: str, *, role: str = '', kind: str = '', template: str = '', pack: dict | None = None,
              first: bool | None = None) -> list[str]:
    """Every way a note departs from the owner's pack, worded as the site's note box shows it: gaps left,
    his don't-say list, his phrasing and positioning rules, length, and links. A first approach (any
    note but a follow-up, unless `first` says otherwise) must link to his site."""
    if not (text or '').strip():
        return []
    pack = pack if pack is not None else template_pack()
    first = not is_followup(pack, template) if first is None else first
    t = _template(pack, template)
    waived = set(t.get('waive') or [])
    out = []
    left = list(dict.fromkeys(GAP.findall(text)))
    if left:
        out.append('Gaps still to fill: ' + ', '.join(left))
    for rule in pack.get('dont_say') or []:
        hits = list(dict.fromkeys(m.group(0).strip() for m in _pattern(rule).finditer(text)))
        if hits:
            quoted = ' or '.join(f'"{h}"' for h in hits)
            out.append(f"Don't say {quoted}: {rule['why']}.")
    for rule in pack.get('checks') or []:
        if rule['id'] in waived or not _pattern(rule).search(text):
            continue
        if rule.get('unless') and _pattern(rule, 'unless').search(text):
            continue
        out.append(rule['say'])
    count = _words(text)
    if t.get('words') and count > t['words']:
        out.append(f"About {count} words: the pack keeps these under {t['words']}.")
    hosts = {re.sub(r'^https?://', '', link.lower()).removeprefix('www.').split('/')[0] for link in LINK.findall(text)}
    site = (pack.get('first_approach') or {}).get('link')
    if first and site and site not in text.lower():
        out.append(pack['first_approach']['missing'])
    if len(hosts) > 1:
        out.append(f'{len(hosts)} links: the pack sends one, portfolio.example.com.')
    if REDIRECT.search(text):
        out.append('A link is wrapped in a Google redirect: write the bare domain.')
    return list(dict.fromkeys(out))


def card(show: dict) -> list[str]:
    li = show.get('linkedin') or {}
    plan = next((r.get('approach') for r in show.get('research') or [] if r.get('approach')), {}) or {}
    draft = show.get('draft') or {}
    pack = template_pack()
    tid = suggest_template(show, pack)
    lines = [f"# {show.get('name')} ({show.get('id')})",
             f"relationship {show.get('relationship')} · kind {show.get('kind')} · channel {show.get('preferred_channel')}"
             f" · ask {show.get('preferred_ask')} · {li.get('observed_role') or ''} at {li.get('observed_company') or ''}"
             f" · connected {li.get('connected_on') or 'unknown'}",
             '', 'Known context (his notes; check-first warnings):', show.get('known_context') or '(none)']
    if plan:
        lines += ['', 'Prepared approach:', *(f"  {k}: {v}" for k, v in plan.items())]
    lines += ['', 'Recorded contact:', *(f"  {i['at'][:10]} {i['channel']} {i['direction']}: {i['summary']}"
                                         for i in show.get('interactions') or [])]
    if tid:
        t = _template(pack, tid)
        shape = next((s for s in pack.get('shapes') or [] if plan.get('cv') in (s.get('cvs') or [])), (pack.get('shapes') or [{}])[0])
        lines += ['', f"Template the site starts from (config/templates.yml): {t['title']} ({tid});"
                      f" lead with {shape.get('label', '')}: {shape.get('wording', '')}", t.get('for', ''), t.get('body', '')]
    if draft:
        lines += ['', f"Current note ({draft.get('purpose')}, version {draft.get('version_id')}, by {draft.get('actor')}):",
                  draft.get('text') or '']
        issues = lint_note(draft.get('text') or '', role=li.get('observed_role') or '', kind=show.get('kind') or '',
                           template=tid, pack=pack)
        if issues:
            lines += ['', 'Checks against the template pack (the site shows the same):', *(f"  ! {w}" for w in issues)]
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('show', help='`show` JSON for one person, or - for stdin')
    p.add_argument('--export', default='', help='LinkedIn export folder (default: the newest in data/raw-imports/linkedin)')
    p.add_argument('--research', default=str(ROOT / 'data/research'))
    a = p.parse_args(argv)
    show = json.loads(sys.stdin.read() if a.show == '-' else Path(a.show).read_text(encoding='utf-8'))
    exports = sorted((ROOT / 'data/raw-imports/linkedin').glob('*')) if not a.export else [Path(a.export)]
    out = card(show)
    out += ['', 'Positioning & CV breadth (config/cvs.yml):', *cv_positioning()]
    found = threads(exports[-1] / 'messages.csv', show.get('url') or '') if exports else []
    voice = own_voice(found)
    if voice:
        out += ['', "Your own voice to them in LinkedIn messages (match this register):"] + voice
    out += ['', f"LinkedIn threads with them alone ({len(found)}; local only):"]
    for ms in found:
        out.append(f"--- {ms[0]['DATE'][:10]} to {ms[-1]['DATE'][:10]}, {len(ms)} messages")
        for m in ms:
            body = ' '.join((m['CONTENT'] or '').split())
            out.append(f"  {m['DATE'][:10]} {m['FROM']}: " + ('[held: private topic]' if profile_rules.current().is_private(body) else body[:600]))
    corr = correspondence(Path(a.research), show)
    if corr:
        out += ['', 'Correspondence headers & group overlap (local only):', *corr]
    notes = sections(Path(a.research), show.get('name') or '\0')
    out += ['', f"Local research that names them ({len(notes)} sections; local only):"]
    for name, text in notes:
        out += [f"--- {name}", text]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
