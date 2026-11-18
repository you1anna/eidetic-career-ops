"""Read-only collection policy and fixed transport identities; no scheduling or mutations."""
from pathlib import Path
from urllib.parse import urlsplit, unquote
import posixpath
import hashlib
import math
import yaml

PATH = Path(__file__).resolve().parents[1] / 'config/collection.yml'
# Endpoint identity is code, not a configurable means of escaping a provider allowance.
ENDPOINTS = {
    'api.adzuna.com': ('adzuna', '/'), 'www.reed.co.uk': ('reed', '/api/'),
    'boards-api.greenhouse.io': ('greenhouse', '/'), 'api.greenhouse.io': ('greenhouse', '/'),
    'api.lever.co': ('lever', '/'), 'api.ashbyhq.com': ('ashby', '/'),
    'api.smartrecruiters.com': ('smartrecruiters', '/'), 'ats.rippling.com': ('rippling', '/'),
    'api.company-information.service.gov.uk': ('companies-house', '/'),
    'hn.algolia.com': ('hn_algolia', '/'),
    'www.contractsfinder.service.gov.uk': ('contracts_finder', '/Published/'),
    'www.find-tender.service.gov.uk': ('find_a_tender', '/api/'),
    'www.uktech.news': ('uktn', '/funding/feed/'), 'uktech.news': ('uktn', '/funding/feed/'),
    'reed.co.uk': ('reed', '/api/'),
}
PROVIDERS = frozenset(p for p,_ in ENDPOINTS.values())
DAYS = ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
_cache = {}

def provider_for(url):
    parts = urlsplit(url)
    entry = ENDPOINTS.get((parts.hostname or '').lower())
    path=parts.path
    for _ in range(3):path=unquote(path)
    path=posixpath.normpath('/'+path.lstrip('/')).casefold()+'/'
    return entry[0] if entry and path.startswith(entry[1].casefold()) else None

def validate(value):
    if not isinstance(value,dict) or set(value) != {'version','timezone','background_days','refresh_after_hours','providers'}:
        raise ValueError('Collection policy fields are invalid')
    if value['version'] != 1 or type(value['version']) is not int or value['timezone'] != 'Europe/London':
        raise ValueError('Unsupported collection policy version/timezone')
    days=value['background_days']
    if not isinstance(days,list) or not days or any(x not in DAYS for x in days) or len(set(days)) != len(days):
        raise ValueError('Invalid collection background days')
    if type(value['refresh_after_hours']) is not int or not 1 <= value['refresh_after_hours'] <= 24:
        raise ValueError('Refresh must precede the 48-hour evidence expiry')
    if not isinstance(value['providers'],dict) or set(value['providers']) != PROVIDERS:
        raise ValueError('Every collection provider needs explicit policy')
    common={'daily_limit','scheduled_target','min_interval_seconds','account'}
    boards={'adzuna','reed','greenhouse','lever','ashby','smartrecruiters','rippling'}
    for provider,p in value['providers'].items():
        extra=({'full_discovery_limit'} if provider in boards else set())
        if provider=='reed':extra|={'search_limit','detail_limit'}
        if provider=='companies-house':extra|={'refresh_days','company_limit'}
        required=(common-{'account'})|extra
        if not isinstance(p,dict) or set(p)-(common|extra) or not required<=set(p):
            raise ValueError('Invalid provider policy fields')
        for key in set(p)-{'account','min_interval_seconds'}:
            if type(p[key]) is not int or p[key]<0:raise ValueError('Provider limits must be nonnegative integers')
        if not p['scheduled_target']<=p['daily_limit']<=10:
            raise ValueError('Collection daily ceiling is ten; target must fit')
        if p.get('full_discovery_limit',0)>p['scheduled_target']:
            raise ValueError('Discovery allocation exceeds scheduled target')
        interval=p['min_interval_seconds']
        if type(interval) not in (int,float) or not math.isfinite(interval) or not 0<=interval<=60:
            raise ValueError('Invalid provider pacing')
        if provider=='adzuna' and interval<4.1:raise ValueError('Adzuna requires at least 4.1-second pacing')
        if provider=='reed' and p['search_limit']+p['detail_limit']>p['scheduled_target']:
            raise ValueError('Reed search/detail allocation exceeds target')
        if provider=='companies-house' and (not 1<=p['refresh_days']<=365 or not 0<=p['company_limit']<=3 or p['company_limit']*3>p['scheduled_target']):
            raise ValueError('Invalid company refresh policy')
        if 'account' in p:
            account=p['account']
            if not isinstance(account,dict) or set(account)-{'confirmed','minute','day','week','month','observed'}:
                raise ValueError('Invalid observed account allowance')
            for k in ('minute','day','week','month'):
                if k in account and (type(account[k]) is not int or account[k]<1):raise ValueError('Invalid account limit')
            if 'confirmed' in account and type(account['confirmed']) is not bool:raise ValueError('Invalid account confirmation')
            if 'observed' in account:
                import datetime
                try:datetime.date.fromisoformat(account['observed'])
                except (ValueError,TypeError):raise ValueError('Invalid account observation date') from None
    return value

def load(path=PATH):
    raw=Path(path).read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if _cache.get('digest') != digest:
        try:value=validate(yaml.safe_load(raw))
        except (yaml.YAMLError,TypeError,KeyError,AttributeError) as e:raise ValueError('Invalid collection policy') from e
        _cache.update(digest=digest,value=value)
    # Callers cannot mutate the shared policy accidentally.
    import copy
    return copy.deepcopy(_cache['value'])
