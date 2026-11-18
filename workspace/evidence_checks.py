"""Typed, timestamped checks of a vacancy or one person's route to it.

These are source observations, not a record's workflow or an overall freshness claim.
Both research intake and read projections use the same conservative validation.
"""
import datetime as dt
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


FIELDS = {'scope', 'checked_at', 'status', 'source_url', 'basis', 'person_id', 'organisation_id', 'provenance'}
# How a contact route was established. A public read, the owner's own confirmation and a route matched to
# past one-to-one correspondence are different claims (archive/plans/2026-10-18-weekly-approach-preparation.md).
PROVENANCE = ('public_read', 'owner_confirmed', 'prior_correspondence')
# Links into the owner's own mailbox are provenance for him, never a public source anyone can read.
PRIVATE_HOSTS = ('mail.google.com',)
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z')
CLOCK_SKEW = dt.timedelta(minutes=5)
# An advert counts as open while its source listed it, or a research read confirmed it, within this
# window. Sources are read once a day, so 48 hours tolerates one missed daily run.
AVAILABILITY_WINDOW = dt.timedelta(hours=48)
# Same closed-source vocabulary as config/relevance.yml gone_lifecycles (test_evidence_checks reads it).
GONE_LIFECYCLES = ('closed', 'expired', 'removed', 'historical')
LISTING_KINDS = ('board', 'feed', 'hn', 'tender')
ADVERT_TYPES = ('job', 'tender')
LONDON = ZoneInfo('Europe/London')


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _text(value, field, limit=4000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or
            any(ord(char) < 32 and char not in '\n\t' for char in value)):
        raise ValueError(f'A freshness check needs readable {field} text (up to {limit} characters)')


def checked_time(value, now=None):
    """Require seconds and a timezone; tolerate only five minutes of clock skew."""
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError('A freshness check needs a full ISO checked_at timestamp with timezone')
    try:
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError('A freshness check needs a valid checked_at timestamp') from None
    if stamp > (now or utc_now()) + CLOCK_SKEW:
        raise ValueError('A freshness check cannot be more than five minutes in the future')
    return stamp


def validate_check(check, source_urls, now=None):
    """Return its UTC instant without modifying the check or its original timestamp."""
    if not isinstance(check, dict):
        raise ValueError('Each freshness check must be an object')
    if set(check) - FIELDS:
        raise ValueError('Unsupported freshness check fields: ' + ', '.join(sorted(set(check) - FIELDS)))
    if check.get('scope') not in ('vacancy', 'contact_route'):
        raise ValueError('A freshness check scope must be vacancy or contact_route')
    allowed = ('confirmed', 'unverified', 'closed') if check['scope'] == 'vacancy' else ('confirmed', 'unverified')
    if check.get('status') not in allowed:
        raise ValueError('A freshness check needs a valid status; closed applies only to a vacancy')
    stamp = checked_time(check.get('checked_at'), now)
    _text(check.get('basis'), 'basis')
    url = check.get('source_url')
    _text(url, 'source_url')
    try:
        parsed = urlsplit(url)
        safe = (parsed.scheme in ('http', 'https') and parsed.hostname and
                not (parsed.username or parsed.password) and not any(char.isspace() for char in url))
        parsed.port  # Malformed ports are not usable evidence links.
    except ValueError:
        safe = False
    if not safe:
        raise ValueError('A freshness check needs a complete, safe http or https source URL')
    if not isinstance(source_urls, list) or url not in source_urls:
        raise ValueError('A freshness check source_url must be present in the finding source_urls')
    if 'provenance' in check:
        if check['provenance'] not in PROVENANCE:
            raise ValueError('A check provenance must be public_read, owner_confirmed or prior_correspondence')
        if check['provenance'] == 'public_read' and private_source(url):
            raise ValueError('A public read needs a publicly readable source, not a private mailbox link')
    if check['scope'] == 'contact_route':
        _text(check.get('person_id'), 'person_id', 200)
        if 'organisation_id' in check:
            _text(check['organisation_id'], 'organisation_id', 200)
    elif 'person_id' in check or 'organisation_id' in check:
        raise ValueError('Only a contact_route check may name a person or organisation')
    return stamp


def private_source(url):
    try:
        return (urlsplit(url or '').hostname or '').lower() in PRIVATE_HOSTS
    except ValueError:
        return False


def route_class(check):
    """Which kind of evidence a check belongs to. A later failed public read supersedes an earlier
    public read, never a confirmation the owner gave or a route taken from his own correspondence. A legacy
    check without provenance whose source is a private mailbox link was not a public read either."""
    provenance = check.get('provenance')
    if provenance in ('owner_confirmed', 'prior_correspondence'):
        return provenance
    if provenance == 'public_read':
        return 'public'
    return 'private' if private_source(check.get('source_url')) else 'public'


def validate_finding(finding):
    if 'freshness_checks' not in finding:
        return
    if not isinstance(finding['freshness_checks'], list):
        raise ValueError('freshness_checks must be a list')
    now = utc_now()
    for check in finding['freshness_checks']:
        validate_check(check, finding.get('source_urls'), now)


def valid_targets(check, active_types):
    """A historic or malformed identity cannot establish a current contact route."""
    if check['scope'] != 'contact_route':
        return True
    return (active_types.get(check['person_id']) == 'person' and
            ('organisation_id' not in check or active_types.get(check['organisation_id']) in ('company', 'agency')))


def _instant(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=dt.timezone.utc) if stamp.tzinfo is None else stamp.astimezone(dt.timezone.utc)


def _london_date(now):
    return now.astimezone(LONDON).date().isoformat()


def advert_availability(record, now=None):
    """Whether an advert still looks open, from fields the record already carries.

    The latest scanner listing (record['advert'], from the daily board/feed read) and a confirmed
    research read are positive evidence. A refused, blocked or failed read is never evidence that
    an advert closed and never outranks a listing. Closure comes only from the source lifecycle, an
    explicit closed read or a passed closing date. Returns short codes for every advert record;
    `until` is when an open answer stops being open, so a page left open can age it without its own rule.
    """
    now = now or utc_now()
    advert = record.get('advert') or {}
    scope = str(advert.get('source') or '').split(':')
    listing = (_instant(advert.get('observed_at')) if scope[0] in LISTING_KINDS else None)
    source = ':'.join(scope[:2]) if listing else None
    vacancy = (record.get('evidence_checks') or {}).get('vacancy') or {}
    if record.get('source_lifecycle') in GONE_LIFECYCLES:
        return dict(status='closed', basis='listing', at=advert.get('observed_at') if listing else None, source=source, until=None)
    if vacancy.get('status') == 'closed':
        return dict(status='closed', basis='research', at=vacancy.get('checked_at'), source=source, until=None)
    deadline = str(record.get('published_deadline') or '')[:10]
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', deadline) and deadline < _london_date(now):
        return dict(status='closed', basis='deadline', at=None, source=source, until=None)
    evidence = []
    if listing:
        evidence.append((listing, 'listing', advert['observed_at']))
    confirmed = _instant(vacancy.get('checked_at')) if vacancy.get('status') == 'confirmed' else None
    if confirmed:
        evidence.append((confirmed, 'research', vacancy['checked_at']))
    usable = [e for e in evidence if e[0] <= now + CLOCK_SKEW]
    if not usable:
        return dict(status='unconfirmed', basis=None, at=None, source=source, until=None)
    stamp, basis, at = max(usable, key=lambda e: e[0])
    until = stamp + AVAILABILITY_WINDOW
    if now >= until:
        return dict(status='unconfirmed', basis=basis, at=at, source=source, until=None)
    return dict(status='open', basis=basis, at=at, source=source, until=until.isoformat())
