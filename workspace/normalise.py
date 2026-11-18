"""The one set of normalisation rules for facts entering or already in the workspace.

Every import (hub, scans, observations, research, LinkedIn) and the `wo repair` backfill use these
functions, so a fact is stored the same way whichever route it arrived by.
"""
from __future__ import annotations

import html
import datetime as dt
import re
from urllib.parse import urlsplit, urlunsplit

import profile_rules

# Factual dates are dates or empty. Unknown is empty; free text belongs in notes.
DATE_FIELDS = ('date', 'published_deadline', 'last_touch')
FACTUAL_DATE = re.compile(r'^\d{4}(?:-\d{2}(?:-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?)?)?$')
LEADING_DATE = re.compile(r'^(\d{4}-\d{2}(?:-\d{2})?)\b\s*[,;(]?\s*(.*?)\)?\s*$')
EMPTY_WORDS = {'unknown', 'n/a', 'na', 'none', 'tbc', 'tba', '-', '—', '?'}
TEXT_FIELDS = ('name', 'organisation', 'role', 'summary', 'notes', 'known_context', 'location', 'workplace',
               'engagement', 'pay', 'contact', 'subtype', 'next_action')


HASHLESS_HEX = re.compile(r'&x([0-9a-f]{1,6});', re.I)  # '&xa;' from a feed that dropped the '#'


def decode(value: str) -> str:
    """Decode HTML entities, including a hex entity a feed sent without its '#'."""
    return html.unescape(HASHLESS_HEX.sub(r'&#x\1;', value))


def clean(value):
    """Decode HTML entities and collapse whitespace."""
    return ' '.join(decode(value).split()) if isinstance(value, str) else value


def org_key(name) -> str:
    """How two organisation (or title, or location) strings are compared."""
    return clean(name or '').casefold()


# A stated employer that names no organisation to link. People at the former employer carry the
# obligations flag instead of a link (`profile_rules`, `tools/linkedin_export.py`).
NOT_AN_EMPLOYER = {'mostly retired', 'retired', 'self employed', 'self-employed', 'freelance', 'independent consultant'}
# LinkedIn placeholders that start a stated employer: "Stealth (Fintech)", "Freelance (Self employed)", "none".
PLACEHOLDER_EMPLOYER = re.compile(r'^(?:stealth|freelance|self[- ]employed|confidential|none|n/a)\b')


def placeholder_employer(name) -> bool:
    """Whether a stated employer is a placeholder, not an organisation ("Stealth", "Freelance")."""
    key = org_key(name)
    return key in NOT_AN_EMPLOYER or bool(PLACEHOLDER_EMPLOYER.match(key))


def employer_key(name) -> str:
    """The org_key of a person's stated employer, or '' when it names no organisation to link."""
    key = org_key(name)
    return '' if placeholder_employer(key) or profile_rules.current().at_employer(key) else key


def job_url(value: str) -> str:
    """Validate an advert URL and give LinkedIn numeric, slug and email links one identity.

    Parse the actual hostname before recognising LinkedIn. Other providers' query strings can
    identify the advert, so preserve them. Fragments are page positions, not advert identities.
    """
    if not isinstance(value, str) or not value or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        raise ValueError('Use a complete http or https job link without spaces or credentials')
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme not in ('http', 'https') or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if ':' in host:
            import ipaddress
            ipaddress.IPv6Address(host)
            authority = '[' + host.lower() + ']'
        else:
            host = host.encode('idna').decode('ascii').lower().rstrip('.')
            if len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                      for label in host.split('.')):
                raise ValueError
            authority = host
    except (ValueError, UnicodeError):
        raise ValueError('Use a complete http or https job link without spaces or credentials') from None
    if port is not None and not (parsed.scheme == 'http' and port == 80 or parsed.scheme == 'https' and port == 443):
        authority += ':' + str(port)
    if host == 'linkedin.com' or host.endswith('.linkedin.com'):
        match = re.fullmatch(r'/(?:comm/)?jobs/view/(?:[^/]*-)?([0-9]+)/?', parsed.path)
        if match:
            return f'https://www.linkedin.com/jobs/view/{match[1]}/'
    if host == 'app.jackandjill.ai' and port in (None, 443) and parsed.scheme == 'https':
        match = re.fullmatch(r'/jobs/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/post/?', parsed.path, re.I)
        if match:
            return f'https://app.jackandjill.ai/jobs/{match[1].lower()}/post'
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ''))


def compatible_adverts(stored: dict, incoming: dict) -> bool:
    """A title/company match cannot override different stated locations or engagement terms.

    Missing facts stay neutral. Disagreement needs an attended review, rather than automatically
    attaching an advert and combining two different roles' lifecycle evidence.
    """
    for field in ('location', 'engagement'):
        a, b = (org_key(record.get(field)) for record in (stored, incoming))
        if a in EMPTY_WORDS:
            a = ''
        if b in EMPTY_WORDS:
            b = ''
        if a and b and a != b:
            return False
    return True


def observation_time(value) -> dt.datetime:
    """Comparable UTC time for source evidence, including date-only observations."""
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=dt.timezone.utc) if stamp.tzinfo is None else stamp.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def board_identity(careers_url: str):
    """Return (provider, slug) from a careers URL, or (None, None)."""
    m = re.match(r'https://ats\.rippling\.com/([a-zA-Z0-9_-]+)/jobs(?:[/?#]|$)', careers_url)
    if m:
        return 'rippling', m.group(1)
    m = re.match(r"https?://(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)", careers_url)
    if m:
        return "greenhouse", m.group(1)
    m = re.match(r"https?://jobs\.lever\.co/([^/?#]+)", careers_url)
    if m:
        return "lever", m.group(1)
    m = re.match(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", careers_url)
    if m:
        return "ashby", m.group(1)
    m = re.match(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", careers_url)
    if m:
        return "smartrecruiters", m.group(1)
    return None, None


POSTING_PATHS = {
    'greenhouse': re.compile(r"https?://(?:job-boards|boards)\.greenhouse\.io/[^/?#]+/jobs/(\d+)(?:[/?#]|$)"),
    'lever': re.compile(r"https?://jobs\.lever\.co/[^/?#]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)", re.I),
    'ashby': re.compile(r"https?://jobs\.ashbyhq\.com/[^/?#]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)", re.I),
}


def board_posting(url: str) -> tuple[str, str, str] | None:
    """Return (provider, slug, posting_id) for one advert's page on an employer job board, or None."""
    provider, slug = board_identity(url or '')
    pattern = POSTING_PATHS.get(provider)
    match = pattern.match(url) if pattern else None
    return (provider, slug, match.group(1)) if match else None


PERSON_JOINERS = re.compile(r"[-‐‑–.]")
PERSON_DROPPED = re.compile(r"['’`]")


def person_key(name) -> str:
    """How two person names are compared: case, hyphens, dots, apostrophes and spacing are ignored, so
    'Alex Sample-Harris' and 'Alex Sample Harris' are the same name. Spelling differences are not."""
    return ' '.join(PERSON_JOINERS.sub(' ', PERSON_DROPPED.sub('', clean(name or ''))).casefold().split())


def is_factual_date(value) -> bool:
    return not value or bool(FACTUAL_DATE.match(value))


def split_date(value: str) -> tuple[str, str]:
    """A stored date and the text that belongs in notes: '2026-10-23 (CE-001)' -> ('2026-10-23', 'CE-001');
    'Unknown' -> ('', ''); 'Check meetup.com' -> ('', 'Check meetup.com')."""
    value = clean(value or '')
    if is_factual_date(value):
        return value, ''
    if value.casefold() in EMPTY_WORDS:
        return '', ''
    m = LEADING_DATE.match(value)
    if m:
        return m.group(1), m.group(2).strip(' ;,()')
    return '', value


def normalise_values(values: dict) -> dict:
    """Clean text fields and make date fields factual; displaced date text is appended to notes once."""
    out = dict(values)
    for k in TEXT_FIELDS:
        if isinstance(out.get(k), str):
            out[k] = clean(out[k]) if k not in ('summary', 'notes', 'known_context') else decode(out[k]).strip()
    moved = []
    for k in DATE_FIELDS:
        if isinstance(out.get(k), str) and out[k]:
            kept, text = split_date(out[k])
            out[k] = kept
            if text:
                moved.append(f"{k.replace('_', ' ').capitalize()} note: {text}")
    if moved:
        notes = out.get('notes') or ''
        extra = ' '.join(m + '.' if not m.endswith('.') else m for m in moved if m not in notes)
        if extra:
            out['notes'] = (notes + ' ' + extra).strip()
    return out
