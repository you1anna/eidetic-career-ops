"""Bounded public-page evidence for unattended research; no cookies or private-network reads."""
from __future__ import annotations
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import web_read
import collection_budget
import normalise
import evidence_checks


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def public_url(url):
    # The private/loopback/metadata classification lives once in web_read; research adds its own
    # stricter rules on top: standard ports only, bounded length, no control characters.
    if len(url)>2048 or any(ord(c)<33 for c in url):
        raise ValueError('Invalid public source')
    web_read.validate_public_url(url)
    p = urllib.parse.urlsplit(url)
    if p.password or p.port not in (None,80,443):
        raise ValueError('Public HTTP(S) source required')
    return url


# The DNS-rebinding-safe connection and direct-egress handlers are web_read's; research re-exports
# them so research_worker's imports and the connection logic stay in one place.
public_connection = web_read.public_connection
HTTPHandler = web_read.PublicHTTPHandler
HTTPSHandler = web_read.PublicHTTPSHandler


class Redirect(web_read.SafeRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        public_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPHandler(), HTTPSHandler(), Redirect()).open


BOARD_NAMES = {'greenhouse': 'Greenhouse', 'lever': 'Lever', 'ashby': 'Ashby'}
MAX_POSTING_SOURCES = 30
POSTING_TEXT_LIMIT = 4000


def _posting_identity(job):
    pages = (job.get('url'), job.get('detail_url'), job.get('apply_url'))
    return next(((u, p) for u in pages if u for p in [normalise.board_posting(u)] if p), (None, None))


def prepare_posting_sources(jobs, cache):
    """Register the pass's exact targets before its first board read. Keep at most 30 excerpts,
    never whole board bodies; other listed IDs remain available for vacancy checks."""
    import scan_boards
    targets = cache.setdefault('posting_targets', {})
    remaining = MAX_POSTING_SOURCES - sum(len(v) for v in targets.values())
    for job in jobs:
        if remaining <= 0: break
        page, identity = _posting_identity(job)
        if not identity: continue
        api = scan_boards.posting_api_url(*identity)
        postings = targets.setdefault(api, {})
        if identity[2] not in postings:
            postings[identity[2]] = page
            remaining -= 1


def _posting_excerpt(provider, item, page, api, at):
    import scan_boards
    payload = [item] if provider == 'lever' else {'jobs': [item]}
    parsed = scan_boards.normalise(provider, payload)[0]
    identity = normalise.board_posting(page)
    if (parsed['source_id'] != identity[2] or
            parsed.get('url') and normalise.board_posting(parsed['url']) != identity):
        return None
    if not parsed.get('text', '').strip(): return None
    terms = [parsed.get('title', '')]
    terms.extend(f'{key.replace("_", " ").title()}: {parsed[key]}'
                 for key in ('location','workplace','employment_type') if parsed.get(key))
    if parsed.get('pay'):
        lo, hi, currency, period = parsed['pay']
        terms.append(f'Pay: {currency} {lo:g}–{hi:g}/{period}')
    elif parsed.get('rate'): terms.append('Pay: '+parsed['rate'])
    text = '\n'.join([*terms, parsed['text']])[:POSTING_TEXT_LIMIT]
    return dict(url=page, final_url=page, checked_at=at, ok=True, reason='read', text=text, board_api=api)


def posting_source(posting, cache):
    """The bounded body of this exact listed posting, read with its retained availability check."""
    if not posting or posting[0]['status'] != 'confirmed': return None
    check, api = posting
    identity = normalise.board_posting(check['source_url'])
    return (cache.get(('board_sources',api)) or {}).get(identity[2])


def _listed_ids(provider, api, cache):
    """Read a board once per pass. Retain IDs and bounded excerpts only for registered targets."""
    key = ('board_api', api)
    if cache is not None and key in cache:
        return cache[key]
    try:
        with web_read.single_attempt():
            payload = json.loads(web_read.request(api, opener=_opener(), accept='application/json')['body'].decode('utf-8'))
        items = (payload.get('jobs') if provider == 'ashby' else [payload]) if isinstance(payload, dict) else None
        ids = frozenset(str(j.get('id')) for j in items if isinstance(j, dict)) if isinstance(items, list) else None
        if cache is not None:
            at = stamp()
            cache[('board_checked_at',api)] = at
            wanted = (cache.get('posting_targets') or {}).get(api, {})
            excerpts = cache.setdefault(('board_sources',api), {})
            for item in items or []:
                posting_id = str(item.get('id')) if isinstance(item,dict) else ''
                if posting_id not in wanted: continue
                try: excerpt = _posting_excerpt(provider,item,wanted[posting_id],api,at)
                except (ValueError,TypeError,KeyError,IndexError,AttributeError): excerpt = None
                if excerpt: excerpts[posting_id] = excerpt
    except collection_budget.Deferred:
        raise
    except urllib.error.HTTPError as error:
        ids = error.code
    except Exception:
        ids = None
    if cache is not None:
        if ('board_checked_at',api) not in cache:
            cache[('board_checked_at',api)] = stamp()
        cache[key] = ids
    return ids


def observed_posting(job, cache):
    """A recent exact canonical listing can answer availability without renewing its clock."""
    advert=job.get('advert') or {}
    page, identity=_posting_identity(job)
    if not identity or not advert.get('observed_at') or normalise.board_posting(advert.get('url',''))!=identity:
        return None
    if advert.get('source') != f'board:{identity[0]}:{identity[1]}':
        return None
    try:
        at=collection_budget.instant(advert['observed_at'])
        age=collection_budget.instant(stamp())-at
        if not dt.timedelta(0)<=age<dt.timedelta(hours=24):return None
        availability=evidence_checks.advert_availability(job)
        if availability.get('status')!='open':return None
    except (ValueError,TypeError):return None
    import scan_boards
    api=scan_boards.posting_api_url(*identity)
    checked=at.isoformat()
    if advert.get('text'):
        excerpt='\n'.join(str(advert.get(k,'')) for k in ('title','location','text'))[:POSTING_TEXT_LIMIT]
        cache.setdefault(('board_sources',api),{})[identity[2]]=dict(url=page,final_url=page,checked_at=checked,
            ok=True,reason='retained-listing',text=excerpt,board_api=api)
    return dict(scope='vacancy',status='confirmed',checked_at=checked,source_url=page,
                basis='Exact advert retained from the employer board at the recorded observation time.',provenance='public_read'),api


def posting_check(job, cache=None):
    """Whether an advert on an employer job board is still listed, read from that board's public API
    (the endpoints scan_boards uses) instead of a page that needs a browser. Returns (vacancy check,
    API URL), or None when the advert is not on a supported board or the API could not be read. An
    advert missing from the board stays unverified: absence alone never closes it."""
    import scan_boards
    cache = {} if cache is None else cache
    retained=observed_posting(job,cache)
    if retained:return retained
    page, posting = _posting_identity(job)
    if not posting:
        return None
    provider, slug, posting_id = posting
    api = scan_boards.posting_api_url(provider, slug, posting_id)
    try:
        public_url(page); public_url(api)
    except ValueError:
        return None
    prepare_posting_sources([job],cache)
    ids = _listed_ids(provider, api, cache)
    # A missing Greenhouse or Lever posting answers 404/410; an Ashby board always answers.
    if ids is None or isinstance(ids, int) and (ids not in (404, 410) or provider == 'ashby'):
        return None
    listed = not isinstance(ids, int) and posting_id in ids
    board = BOARD_NAMES[provider]
    basis = (f"Listed on the employer's {board} job board (posting {posting_id}) when checked." if listed else
             f"Not listed on the employer's {board} job board when checked. That alone does not close the advert.")
    return dict(scope='vacancy', status='confirmed' if listed else 'unverified',
                checked_at=cache.get(('board_checked_at',api)) or stamp(),
                source_url=page, basis=basis, provenance='public_read'), api


def read(url, cache=None):
    if cache is not None and url in cache: return cache[url]
    result=dict(url=url, checked_at=stamp(), ok=False, text='', reason='source_unavailable')
    deferred=False
    try:
        public_url(url)
        opener=_opener()
        p=urllib.parse.urlsplit(url);robots=f'{p.scheme}://{p.netloc}/robots.txt'
        with web_read.single_attempt():
            try: rules=web_read.request(robots,opener=opener,accept='text/plain',logged=False)
            except urllib.error.HTTPError as e:
                if e.code not in (404,410): raise
            else:
                parser=urllib.robotparser.RobotFileParser();parser.parse(rules['body'].decode('utf-8',errors='replace').splitlines())
                if not parser.can_fetch('work-ops-scan',url):
                    result['reason']='robots_disallowed';return result
            response=web_read.request(url,opener=opener,accept='text/html,application/json')
        public_url(response['url'])
        html=response['body'].decode(response['charset'] or 'utf-8',errors='replace')
        text=web_read.strip_html(html)
        result['final_url']=response['url']
        # Link targets prove which exact profile or address a page points to; visible text drops them.
        # Used only for deterministic route validation, never sent to the model.
        result['links']=links_in(html)
        login_path = re.search(r'(?:^|/)(?:sign[-_]?in|log[-_]?in|login|signin)(?:/|$)', urllib.parse.urlsplit(response['url']).path, re.I)
        if login_path or response['headers'].get('cf-mitigated')=='challenge' or re.search(r'captcha|verify you are human|just a moment|sign in to (?:view|continue)',text[:1500],re.I):
            result['reason']='login_or_challenge'
        elif web_read.looks_script_rendered(html) or len(text.strip())<120:
            result['reason']='page_needs_browser'
        else: result.update(ok=True,text=text[:24000],reason='read')
    except collection_budget.Deferred:
        deferred=True
        raise
    except Exception as e: result['reason']=web_read.failure_kind(e) if not isinstance(e,ValueError) else 'unsafe_source'
    finally:
        result['checked_at']=stamp()
        if cache is not None and not deferred: cache[url]=result
    return result


def normalized(text): return re.sub(r'\s+',' ',str(text)).strip().casefold()


def links_in(html,limit=300):
    out=[]
    for raw in re.findall(r'href\s*=\s*["\']([^"\'<>\s]{1,500})["\']',html or '',re.I):
        link=raw.strip()
        if re.match(r'(?:https?:|mailto:)',link,re.I) and link not in out:
            out.append(link)
            if len(out)>=limit:break
    return out


def route_key(value):
    """One comparable form for a profile URL or email address: no scheme, www, country subdomain,
    query or trailing slash. Unknown or empty values never match anything."""
    value=str(value or '').strip()
    if not value:return ''
    if value.lower().startswith('mailto:'):value=value[7:].split('?')[0]
    if '@' in value and '/' not in value:return value.casefold()
    try:p=urllib.parse.urlsplit(value if '://' in value else 'https://'+value)
    except ValueError:return ''
    host=(p.hostname or '').lower().removeprefix('www.')
    if host.endswith('.linkedin.com'):host='linkedin.com'
    path=urllib.parse.unquote(p.path).rstrip('/').casefold()
    return host+path if host else ''


def route_supported(person,source):
    """Whether an actually read source carries this person's exact recorded route: the page is their
    recorded profile/site, or its text or links contain their recorded profile URL or email address."""
    wanted={k for k in (route_key(person.get('url')),route_key(person.get('email'))) if k}
    if not wanted or not source.get('ok'):return False
    if route_key(source.get('final_url') or source.get('url')) in wanted:return True
    found={route_key(link) for link in source.get('links') or []}
    text=normalized(source.get('text',''))
    return bool(wanted&found) or any(w in text for w in wanted)


def check_claim(job, claim, sources, person=None, *, fallback_source=None):
    index=claim.get('source',-1)
    source=sources[index] if type(index) is int and 0<=index<len(sources) else (fallback_source or {})
    url=source.get('url') or (person or job).get('url') or job.get('url')
    check=dict(scope='contact_route' if person else 'vacancy', status='unverified',
               source_url=url, checked_at=source.get('checked_at') or stamp(),
               basis=source.get('reason') or 'No usable source evidence')
    check['provenance']='public_read'
    if person:
        check['person_id']=person['id']
        if person.get('organisation_id'): check['organisation_id']=person['organisation_id']
    if not source.get('ok'): return check
    quote=normalized(claim.get('quote',''));text=normalized(source.get('text',''))
    name=normalized((person or job).get('name',''));company=normalized(job.get('organisation',''))
    status=claim.get('status')
    if person and not person.get('organisation_id'):
        # A personal route: the person and their exact recorded profile or address, with no employer claim.
        supported=(status=='confirmed' and len(quote)>=18 and quote in text and bool(name) and name in quote
                   and route_supported(person,source) and not re.search(r'\b(former|formerly|previously|left|until \d{4})\b',quote))
        if supported:
            check.update(status='confirmed',basis=(str(claim.get('basis',''))[:700]+' Evidence: '+str(claim.get('quote',''))[:650]).strip())
        else: check['basis']='Source read, but this exact personal route could not be established.'
        return check
    supported=(len(quote)>=18 and quote in text and name and name in text and company and company in text)
    if not person:
        known={normalise.job_url(u) for u in (job.get('url'),job.get('detail_url'),job.get('apply_url')) if u}
        final=source.get('final_url') or url
        exact=normalise.job_url(url) in known and normalise.job_url(final)==normalise.job_url(url)
        # A new URL must carry the role + employer in the cited excerpt, not just elsewhere in a listing.
        supported=supported and (exact or (name in quote and company in quote))
    if person:
        # A bare name or historic tie cannot establish a current route to this employer.
        supported=supported and name in quote and company in quote and not re.search(r'\b(former|formerly|previously|left|until \d{4})\b',quote)
        supported=supported and status=='confirmed'
    else:
        if status=='confirmed': supported=supported and bool(re.search(r'\b(apply|application|hiring|join our|recruiting)\b',quote))
        elif status=='closed': supported=supported and bool(re.search(r'no longer|closed|filled|not accepting|expired',quote))
        else: supported=False
    if supported:
        check.update(status=status,basis=(str(claim.get('basis',''))[:700]+' Evidence: '+str(claim.get('quote',''))[:650]).strip())
    else: check['basis']='Source read, but exact vacancy/current contact route could not be established.'
    return check
