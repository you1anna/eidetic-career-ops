"""Extract the digest's own role paragraphs, never quoted replies or generated fit claims.

The summary is retained as email_summary, not advert text: research must read a primary source
before its wording can establish remit, requirements or current vacancy availability.
"""
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from normalise import clean, job_url
from job_rules import PAY_RE, text_pay

JOB = re.compile(r'/jobs/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/post', re.I)
PLACE = r'London(?:\s*\([^)]*\))?|United Kingdom|UK|United States|USA?|Singapore|Berlin|Paris|New York'
PAY_PREFIX = re.compile(r'\b(?:salary|pay|rate|compensation|remuneration|listed range)\b'
                        r'(?:\s+(?:is|of|from|between|up to|range|band|package|starts? at))*'
                        r'\s*[:=–—-]?\s*(?=[£$€]\s?\d)', re.I)


def _pay(summary):
    clauses = []
    for line in re.split(r'(?<=[.!?])\s+', summary):
        prefix = PAY_PREFIX.search(line)
        start = prefix.start() if prefix else next((m.start() for m in PAY_RE.finditer(line)
                    if re.match(r'\s*(?:/|per|a)\s*(?:day|year|annum|hour|month)\b', m[6], re.I)), None)
        if start is not None and text_pay(line[start:]):
            clauses.append(line[start:])
    return '\n'.join(clauses)


def _identity(value):
    try:
        url = job_url(value)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'app.jackandjill.ai':
            if parsed.path.startswith('/jobs/'):
                raise ValueError('Unsupported Jack and Jill job URL')
            return None
        match = JOB.fullmatch(parsed.path)
        if not match:
            if parsed.path.startswith('/jobs/'):
                raise ValueError('Unsupported Jack and Jill job URL')
            return None
        return match[1].lower(), url
    except ValueError:
        raise ValueError('Unsupported Jack and Jill job URL') from None


class _RadarCard:
    """One explicitly marked jj-card; repeated advert/action links are not new roles."""
    def __init__(self):
        self.identities, self.paragraphs = set(), []
        self.title, self.title_parts, self.paragraph = '', None, None
        self.paragraph_has_job = False

    def start(self, tag, attrs):
        if tag == 'p':
            if self.paragraph is not None:
                raise ValueError('Malformed Jack and Jill radar paragraph')
            self.paragraph, self.paragraph_has_job = [], False
        if tag == 'a':
            href = attrs.get('href') or ''
            identity = _identity(href) if href.startswith(('http://', 'https://')) else None
            if identity:
                self.identities.add(identity)
                if self.paragraph is not None:
                    self.paragraph_has_job = True
            if 'jj-title' in (attrs.get('class') or '').split():
                if not identity or self.title or self.title_parts is not None:
                    raise ValueError('Ambiguous Jack and Jill radar title')
                self.title_parts = []
        if tag == 'br':
            self.data(' ')

    def end(self, tag):
        if tag == 'a' and self.title_parts is not None:
            self.title = clean(''.join(self.title_parts))
            self.title_parts = None
        if tag == 'p' and self.paragraph is not None:
            if not self.paragraph_has_job:
                self.paragraphs.append(clean(''.join(self.paragraph)))
            self.paragraph = None

    def data(self, text):
        if self.title_parts is not None:
            self.title_parts.append(text)
        if self.paragraph is not None:
            self.paragraph.append(text)

    def finish(self):
        if len(self.identities) != 1 or not self.title or not self.paragraphs:
            raise ValueError('Incomplete or ambiguous Jack and Jill radar card')
        company, separator, location = self.paragraphs[0].partition('·')
        company, location = clean(company), clean(location)
        if not separator or not company:
            raise ValueError('Jack and Jill radar employer missing')
        role = self.title
        if ' at ' in role:
            role, stated_company = role.rsplit(' at ', 1)
            if clean(stated_company).casefold() != company.casefold():
                raise ValueError('Conflicting Jack and Jill radar employer')
        summary = clean(' '.join(self.paragraphs[1:]))[:8000]
        # A pay range in the role heading is explicit remuneration; company prose is not.
        pay = [match[1] for match in re.finditer(r'\(([£$€]\s?\d[^()]*)\)', role) if text_pay(match[1])]
        summary_pay = _pay(summary)
        if summary_pay:
            pay.append(summary_pay)
        identity = next(iter(self.identities))
        return dict(source_id=identity[0], url=identity[1], title=clean(role), company=company,
                    location=location, email_summary=summary, pay_text='\n'.join(pay))


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards, self.stack = [], []
        self.parts, self.links, self.anchor = [], [], None
        self.radar, self.radar_depth = None, 0

    def blocked(self):
        return any(blocked for _, blocked in self.stack)

    def finish(self):
        if self.links:
            identities = {identity for identity, _, _ in self.links}
            if len(identities) != 1:
                raise ValueError('Ambiguous Jack and Jill role paragraph')
            identity, title, end = next((link for link in self.links if link[1]), self.links[0])
            if ' at ' not in title:
                raise ValueError('Jack and Jill role title or employer missing')
            role, company = (clean(s) for s in title.rsplit(' at ', 1))
            if not role or not company:
                raise ValueError('Jack and Jill role title or employer missing')
            summary = clean(''.join(self.parts[end:])).lstrip(': *')[:8000]
            # Only a stated location prefix or explicit location suffix is a location fact.
            # Do not infer London from the campaign or an unrelated employer description.
            explicit = re.match(r'((?:Hybrid|Remote|On[- ]site|Office[- ]based)(?:\s+in)?\s+[^,.;:]+|(?:Based in|Location:)\s*[^,.;:]+)(?:[,.;]|$)', summary, re.I)
            place = re.match(r'([^,.;:]{2,80})(?:,\s*(?:in[- ]office|hybrid|remote|on[- ]site)\b|\s+(?:hybrid|remote)\s*,)', summary, re.I)
            prefix = re.match(rf'({PLACE})(?:[,.;]|$)', summary, re.I)
            suffix = re.search(rf'\(((?:{PLACE})(?:,?\s*(?:United Kingdom|hybrid|remote))?)\)\s*$', role, re.I)
            location = next((match[1] for match in (explicit,place,prefix,suffix) if match), '')
            # A remuneration cue must introduce the amount, not just occur in a funding sentence.
            self.cards.append(dict(source_id=identity[0], url=identity[1], title=role, company=company,
                                   location=location, email_summary=summary, pay_text=_pay(summary)))
        self.parts, self.links, self.anchor = [], [], None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set((values.get('class') or '').split())
        blocked = tag in ('blockquote', 'script', 'style') or bool(classes & {'gmail_quote', 'yahoo_quoted'})
        radar_start = tag == 'table' and 'jj-card' in classes
        if not self.blocked() and self.radar is None and (tag in ('p', 'li') or blocked or radar_start):
            self.finish()
        if tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'):
            self.stack.append((tag, blocked))
        if self.blocked():
            return
        if radar_start:
            if self.radar is not None:
                raise ValueError('Nested Jack and Jill radar cards')
            self.radar, self.radar_depth = _RadarCard(), len(self.stack)
        if self.radar is not None:
            self.radar.start(tag, values)
            return
        if tag == 'a':
            href = values.get('href', '')
            identity = _identity(href) if href.startswith(('http://', 'https://')) else None
            self.anchor = (identity, len(self.parts)) if identity else None
        if tag == 'br':
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if not self.blocked():
            if self.radar is not None:
                self.radar.end(tag)
                if tag == 'table' and len(self.stack) == self.radar_depth:
                    self.cards.append(self.radar.finish())
                    self.radar = None
            elif tag == 'a' and self.anchor:
                identity, start = self.anchor
                self.links.append((identity, clean(''.join(self.parts[start:])), len(self.parts)))
                self.anchor = None
            elif tag in ('p', 'li'):
                self.finish()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.blocked():
            if self.radar is not None:
                self.radar.data(data)
            else:
                self.parts.append(data)


def parse(html):
    parser = _Paragraphs()
    parser.feed(html)
    parser.close()
    if parser.radar is not None:
        raise ValueError('Truncated Jack and Jill radar card')
    parser.finish()
    seen = {}
    for card in parser.cards:
        if card['source_id'] in seen and seen[card['source_id']] != card:
            raise ValueError('Conflicting Jack and Jill job identity')
        seen[card['source_id']] = card
    return parser.cards


def plain_html(text):
    """Only Markdown links become anchors; raw text cannot introduce HTML or quoted cards."""
    text = re.split(r'(?m)^On [\s\S]{0,400}?wrote:\s*$', text, maxsplit=1)[0]
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
    paragraphs = []
    for paragraph in re.split(r'\n\s*\n', text):
        if re.match(r'\s*(?:On .+wrote:|[-]+\s*Original Message)', paragraph):
            break
        if paragraph.lstrip().startswith('>'):
            continue
        paragraph = escape(paragraph)
        paragraph = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<a href="\2">\1</a>', paragraph)
        paragraphs.append('<p>' + paragraph + '</p>')
    return ''.join(paragraphs)
