"""Pure source-logo rules shared by intake and read projections; never fetch an image."""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit


def linkedin_company_logo(value) -> str:
    """Keep the signed URL verbatim, only for LinkedIn's company-logo image path."""
    if (not isinstance(value, str) or len(value) > 4096
            or re.search(r'[\s\x00-\x1f\x7f\\]', value)):
        return ''
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname != 'media.licdn.com'
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or not parsed.path.startswith('/dms/image/')
                or not re.search(r'/company-logo(?:_|/|$)', parsed.path)):
            return ''
    except ValueError:
        return ''
    return value


def logo_expiry(value) -> dt.datetime | None:
    """Unknown until retained evidence validates a source's expiry field semantics.

    A numeric LinkedIn `e` resembles a timestamp, but shape alone does not establish
    its meaning. Keep receipt counters without inventing an expiry or vacancy date.
    """
    return None


def logo_counts(jobs, now: dt.datetime) -> dict:
    """Per parsed card, before cross-email job deduplication; no source URLs in receipts."""
    counts = dict(cards=0, logo_cards=0, logo_expired=0, logo_expiring_soon=0, logo_expiry_unknown=0)
    for job in jobs:
        counts['cards'] += 1
        url = linkedin_company_logo(job.get('company_logo_url'))
        if not url:
            continue
        counts['logo_cards'] += 1
        expiry = logo_expiry(url)
        if expiry is None:
            counts['logo_expiry_unknown'] += 1
        elif expiry <= now:
            counts['logo_expired'] += 1
        elif expiry <= now + dt.timedelta(days=7):
            counts['logo_expiring_soon'] += 1
    return counts
