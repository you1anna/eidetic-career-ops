"""Contact rules taken from the user's profile (`config/profile.yml`).

One owner for the private facts that the code must act on without naming them:

- `candidate`: the user's own name and LinkedIn profile, to tell their own messages and addresses apart.
- `former_employer`: people still at a former employer carry an obligations warning instead of an
  organisation link, and are left out of outreach suggestions unless the user recorded them as a leaver.
- `private_note_topics`: a note touching one of these topics is never processed or sent to the site.
- `employers`: earlier employers, their mail domains and the months the user worked there, so mail
  headers can show who was a colleague (`tools/mail_colleagues.py`). The `former_employer` is never listed.

A profile without these keys matches nothing. Tests install synthetic rules with `override`.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re

import yaml

PROFILE = Path(__file__).resolve().parent.parent / 'config/profile.yml'
NEVER = re.compile(r'(?!x)x')


def _alternatives(words, flexible_space: bool) -> str:
    escaped = [re.escape(w.strip().casefold()) for w in words or [] if w and w.strip()]
    if flexible_space:
        escaped = [w.replace(r'\ ', r'\s*') for w in escaped]
    return '|'.join(escaped)


@dataclass(frozen=True)
class Employer:
    name: str
    domains: tuple
    start: str  # first month worked there, YYYY-MM
    end: str  # last month, YYYY-MM

    def owns(self, address) -> bool:
        domain = (address or '').rpartition('@')[2].casefold()
        return any(domain == d or domain.endswith('.' + d) for d in self.domains)

    def during(self, when) -> bool:
        return self.start <= (when or '')[:7] <= self.end


@dataclass(frozen=True)
class Rules:
    label: str
    flag: str
    warning: str
    employer: re.Pattern
    leaver: re.Pattern
    mention: re.Pattern
    private_topics: re.Pattern
    own_linkedin: str = ''
    own_name_terms: tuple = ()
    employers: tuple = ()

    def at_employer(self, text) -> bool:
        """Whether a stated company or organisation names the former employer."""
        return bool(text) and bool(self.employer.search(text))

    def is_leaver(self, context) -> bool:
        """Whether the user's own context records the person as a former colleague who has left."""
        return bool(context) and bool(self.leaver.search(context))

    def mentions_employer(self, note) -> bool:
        """Whether a free-text note refers to the former employer, by name or short form."""
        return bool(note) and bool(self.mention.search(note))

    def is_private(self, note) -> bool:
        return bool(note) and bool(self.private_topics.search(note))

    def is_own(self, address_or_sender) -> bool:
        """Whether an email address or sender line belongs to the user (it contains a part of their name)."""
        text = (address_or_sender or '').casefold()
        return any(term in text for term in self.own_name_terms)

    def flagged(self, linkedin) -> bool:
        """Whether a stored LinkedIn observation carries the former-employer flag."""
        if not isinstance(linkedin, dict):
            return False
        return bool(self.flag) and (bool(linkedin.get(self.flag)) or self.flag in (linkedin.get('flags') or []))


def rules_from(profile: dict) -> Rules:
    block = (profile or {}).get('former_employer') or {}
    names = _alternatives(block.get('names'), flexible_space=True)
    aliases = _alternatives(block.get('aliases'), flexible_space=False)
    either = '|'.join(filter(None, [names, aliases]))
    mention = '|'.join(filter(None, [
        rf'\b(?:ex[- ]?)?(?:{aliases})\b' if aliases else '',
        rf'\b(?:{names})\b' if names else '',
    ]))
    topics = '|'.join(t for t in (profile or {}).get('private_note_topics') or [] if t)
    candidate = (profile or {}).get('candidate') or {}
    return Rules(
        label=block.get('label') or '',
        flag=block.get('flag') or '',
        warning=block.get('warning') or '',
        employer=re.compile(names, re.I) if names else NEVER,
        leaver=re.compile(rf'former (?:{either}) colleague', re.I) if either else NEVER,
        mention=re.compile(mention, re.I) if mention else NEVER,
        private_topics=re.compile(rf'\b(?:{topics})\b', re.I) if topics else NEVER,
        own_linkedin=candidate.get('linkedin') or '',
        own_name_terms=tuple(t for t in (candidate.get('full_name') or '').casefold().split() if len(t) > 2),
        employers=tuple(_employer(e) for e in (profile or {}).get('employers') or []),
    )


def _employer(entry: dict) -> Employer:
    domains = tuple(str(d).strip().casefold() for d in entry.get('domains') or [] if str(d).strip())
    start, end = str(entry.get('from') or ''), str(entry.get('to') or '')
    if not entry.get('name') or not domains or not (len(start) == len(end) == 7 and start <= end):
        raise ValueError(f"profile employer {entry.get('name')!r} needs a name, mail domains and from/to months (YYYY-MM)")
    return Employer(name=str(entry['name']), domains=domains, start=start, end=end)


_cache: dict = {}
_override: list[Rules] = []


def current() -> Rules:
    """The rules from config/profile.yml, reloaded when the file changes."""
    if _override:
        return _override[-1]
    try:
        mtime = PROFILE.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = None
    if _cache.get('mtime') != mtime or 'rules' not in _cache:
        profile = yaml.safe_load(PROFILE.read_text()) if mtime is not None else {}
        _cache.update(mtime=mtime, rules=rules_from(profile or {}))
    return _cache['rules']


@contextmanager
def override(profile: dict):
    """Use rules built from `profile` instead of config/profile.yml (tests)."""
    _override.append(rules_from(profile))
    try:
        yield _override[-1]
    finally:
        _override.pop()
