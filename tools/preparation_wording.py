"""Closed wording contract for unattended notes; no semantic truth claim from a regex or model.

Public source text can check routes or cause a decline, but cannot create a shared memory.
Explicit relationship clauses can undergo the finite pronoun/tense conversions below. Every
option retains the exact input clause. Subjective assessments, uncertain notes and general
third-person descriptions are never used. This trades coverage for traceability; usefulness
still needs human evaluation.
"""
import re


def personal_lines(raw):
    raw = raw.strip()
    if not raw or '?' in raw or re.search(r"\b(?:not|never|no|but|maybe|might|perhaps|think|unsure|apparently|check|unknown|nearly|dont|don.t|didn.t|wasn.t|weren.t|can.t)\b", raw, re.I):
        return []
    if '\n' in raw or '{{' in raw or '}}' in raw:
        return []
    lines = []
    if re.match(r'^(?:We|You)\s', raw) or re.match(r'^I\b.*\byou\b', raw):
        if len(raw.split()) <= 45 and not re.search(r'\b(?:him|her|them|his|their)\b', raw):
            lines.append((raw.rstrip('.!') + '.', raw, 'verbatim sentence; terminal punctuation only'))
    if re.match(r'^Worked together ', raw):
        lines.append(('We w' + raw[1:].rstrip('.!') + '.', raw, 'explicit shared work; pronoun only'))
    patterns = [
        (r'worked (?:with|wtih) (?:him|her|you)(?: closely| years ago)? at ', 'I worked with you at {org}.'),
        (r'used to work for (?:him|her|you) at ', 'I used to work for you at {org}.'),
        (r'worked for (?:him|her|you) at ', 'I worked for you at {org}.'),
        (r'worked in (?:his|her|your) team(?: for years)? at ', 'I worked in your team at {org}.'),
        (r'(?:he |she |you )?recruited me into ', 'You recruited me into {org}.'),
        (r'(?:he |she |you )?hired me into ', 'You hired me into {org}.'),
    ]
    # Only known subjectless annotations may be omitted. A comma alone is NOT permission
    # to reset the subject: "Sam led the project, worked with him …" still refers to Sam.
    annotations = {'good colleague', 'strong contact', 'good contact', 'excellent performer',
                   'good guy', 'friendly', 'good connection', 'good connection to use',
                   'high performer', 'good relationship', 'definite potential', 'good potential',
                   'good leader', 'very knowledgable', 'knowledgable', 'knowledgeable',
                   'competant', 'very competant', 'competent', 'good qa', 'good attitude',
                   'lost touch', 'used to keep in touch', 'want to re-connect', 'want to reconnect',
                   'a great first conversation', 'my number 1'}
    clauses = [c.strip().rstrip('.') for c in re.split(r'[,;]| - ', raw) if c.strip()]
    converted = []
    for clause in clauses:
        if clause.casefold() in annotations:
            continue
        found = False
        for prefix, wording in patterns:
            match = re.fullmatch(r'(?:i\s+)?' + prefix + r'([^,;.!?\n]+)', clause, re.I)
            if not match:
                continue
            org = match[1].strip()
            if not org or len(org.split()) > 8 or not re.fullmatch(r"[\w &'’-]+", org):
                continue
            if re.search(r'\b(?:want|good|strong|because|although|who|where|since|before|after|now|instead|if|remember|possibly)\b', org, re.I):
                continue
            converted.append((wording.format(org=org), clause, 'explicit relationship clause; pronoun and tense conversion'))
            found = True
            break
        if not found:
            converted = []
            break
    lines.extend(converted)
    return list(dict.fromkeys(lines))[:3]


def options(packet, pack):
    config = pack.get('preparation') or {}
    raw = (packet.get('relationship') or {}).get('your_note', '').strip()
    openings = personal_lines(raw)
    if not openings:
        return []
    ask_kind = packet.get('ask', {}).get('kind')
    ask = (config.get('asks') or {}).get(ask_kind)
    if not ask:
        return []
    lines = {line['id']: line['text'] for line in pack.get('core', {}).get('lines', [])}
    shapes = list(pack.get('shapes') or [])
    lead = packet.get('cv', {}).get('lead_with')
    shapes.sort(key=lambda s: s.get('wording') != lead)
    if len(shapes) < 3 or not all(shape.get('wording') for shape in shapes[:3]):
        return []
    out = []
    for opening, quote, transformation in openings:
        values = dict(first_name=packet['person']['first_name'], personal_line=opening,
                      exit=lines.get('exit', ''), ask=ask,
                      shape_1=shapes[0]['wording'], shape_2=shapes[1]['wording'], shape_3=shapes[2]['wording'])
        body = config.get('body', '')
        for key, value in values.items():
            body = body.replace('{' + key + '}', value)
        if not body or re.search(r'\{[a-z_]+\}', body) or not all(values.values()):
            continue
        out.append(dict(message=body, ask=ask, ask_kind=ask_kind,
                 reason=f"Uses your recorded context and your chosen {ask_kind.replace('_', '-')} ask.",
                 evidence=dict(path='relationship.your_note', quote=quote, opening=opening, transformation=transformation),
                 wording_sources=['config/templates.yml:preparation', 'config/templates.yml:core']))
    return out


def matched(message, packet):
    # Normalising line endings/outer whitespace does not grant paraphrasing or extra sentences.
    text = (message or '').replace('\r\n', '\n').strip()
    return next((o for o in packet.get('draft_options', []) if o['message'] == text), None)
