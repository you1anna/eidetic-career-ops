#!/usr/bin/env python3
"""Whether a job advert is a candidate, and what its stated terms are.

The verification layer: pure predicates over one normalised job dict, with no network, no files and
no configuration of their own. Every rule takes its thresholds from the caller so the same code
judges a row whichever channel found it — an employer ATS board, an aggregator feed, or anything
added later. A rule returns *why* alongside its verdict, and that reason is written to the row, so a
candidate can always be traced back to the thing that admitted it.

The governing principle throughout is that an unstated fact stays unknown. A missing rate is not a
low rate, and a job is never filtered out by a threshold it cannot be compared against.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import threading
import re


@lru_cache(maxsize=4096)
def _word_pattern(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def word(term: str, text: str) -> bool:
    """Whole-word, case-insensitive match so 'CTO' does not match 'Director'. The one matcher for
    intake, ranking and CV routing; `text` is already lower-case."""
    # The escaped literal must occur before its ASCII word boundaries can match.
    return term.lower() in text and _word_pattern(term).search(text) is not None


_word = word


_TITLE_EQUIVALENTS = tuple((re.compile(pattern), replacement) for pattern, replacement in (
    (r'\bartificial intelligence\b', 'ai'),
    (r'\bquality eng\b', 'quality engineering'),
    (r'\bquality (?:&|and) release engineering\b', 'quality engineering'),
    (r'\bquality assurance\b', 'qa'),
    (r'\b(?:senior )?specialist qa\b|\bsoftware engineer (?:ml )?qa\b', 'quality engineer'),
    (r'\bmanager qa\b', 'qa manager'),
    (r'\bassurance manager (?:tech|technology)\b', 'qa manager'),
    (r'\bqa engineer lead\b', 'qa lead'),
    (r'\bqa engineers?\b', 'quality engineer'),
    (r'\bsoftware (?:dev\.?|development) engineer in test\b|\bsoftware (?:engineer|developer) in test\b',
     'software development engineer in test'),
    (r'\btest automation (manager|lead|architect)\b', r'test \1'),
    (r'\bquality and testing manager\b', 'test manager'),
    (r'\b(?:test automation engineer|automation test engineer|automation tester|test engineer|test analyst)\b',
     'quality engineer'),
    (r'\bfde\b|(?<!forward )\bdeployed engineer\b|\bforward development engineer\b', 'forward deployed engineer'),
    (r'\btechnical deployment lead\b', 'deployment engineer'),
    (r'\bfounding (?:senior |staff |principal )?(?:software |product |ai |(?:machine|robot) learning |deployments? )?engineer\b',
     'founding engineer'),
    (r'\bformer technical founders?\b', 'founding engineer'),
    (r'\b(lead|principal|staff) (?:software|backend|frontend|full stack|platform|product) engineers?\b', r'\1 engineer'),
    (r'\bengineering (?:team|site) lead\b|\bteam lead engineering\b', 'engineering lead'),
    (r'\btech lead\b|\btechnical team lead\b', 'technical lead'),
    (r'\bmanager (?:software|data|platform|ai) engineering\b|\btechnology manager\b', 'engineering manager'),
    (r'\bhead of software development\b|\bhead of product (?:&|and) engineering\b', 'head of engineering'),
    (r'\bdirector (?:of )?(?:software |ai )?engineering\b', 'director of engineering'),
    (r'\bdirector (?:of )?technology\b', 'head of technology'),
    (r'\btechnology director\b|\bhead of development\b', 'head of technology'),
    (r'\b(?:senior )?director of (?:agentic )?software development\b', 'director of engineering'),
    (r'\bprinciple engineer\b', 'principal engineer'),
    (r'\bhead of applied ai\b', 'head of ai'),
    (r'\bdirector (?:of )?(?:applications (?:and|&) )?ai\b', 'head of ai'),
    (r'\bdata (?:&|and) ai lead\b', 'applied ai lead'),
    (r'\bai(?: (?:and|&) data)? (?:validation|verification)\b', 'ai evaluation'),
    (r'\bai/ml\b', 'ai'),
    (r'\bai (senior|staff|principal) engineer\b', r'\1 ai engineer'),
    (r'\bai (?:research |native sw )engineer\b', 'ai engineer'),
    (r'\b(?:team lead ai(?: & machine learning)?|lead ai engineering)\b', 'applied ai lead'),
    (r'\bsoftware engineer leadership\b', 'engineering leader'),
))


@lru_cache(maxsize=8192)
def title_equivalent(title: str) -> str:
    """Comparison-only role wording shared by intake, ranking and CV routing.

    Preserve source titles and seniority; equivalence admits a candidate, never establishes
    duties or suitability. Bounded whole-word substitutions retain negative qualifiers.
    A pure function of the title, so each distinct title is rewritten once per process.
    """
    text = re.sub(r'[-–—,()]', ' ', title.lower())
    text = ' '.join(text.split())
    for pattern, replacement in _TITLE_EQUIVALENTS:
        text = pattern.sub(replacement, text)
    # A director title can put the software discipline after its team/area name.
    if (re.search(r'\b(?:technical )?director\b', text)
            and re.search(r'\b(?:(?:software|digital|security) engineering|full stack engineer)\b', text)):
        text += ' director of engineering'
    return text


def title_match(title: str, tf: dict) -> tuple[bool, str]:
    t = title.lower()
    equivalent = title_equivalent(title)
    negative = title_veto(title, tf.get('negative', []))
    if negative:
        return False, f'negative:{negative}'
    for pos in tf.get("positive", []):
        if _word(pos, t):
            return True, f"title:{pos}"
    for pos in tf.get('positive', []):
        if _word(pos, equivalent):
            return True, f'title-equivalent:{pos}'
    return False, "no-title-match"


def title_veto(title: str, negatives) -> str:
    """A department label must not veto an explicit software delivery occupation."""
    raw, equivalent = title.lower(), title_equivalent(title)
    for negative in negatives:
        if not (_word(negative, raw) or _word(negative, equivalent)):
            continue
        if negative.lower() == 'pricing' and re.search(r'\b(?:software engineering manager|engineering lead)\b', equivalent):
            continue
        if negative.lower() == 'sales' and _word('software development engineer in test', equivalent):
            continue
        return negative
    return ''


def _job_text(job: dict) -> tuple[str, str]:
    """Accept both scanner rows and canonical records without altering the source facts."""
    return (str(job.get("title") or job.get("name") or "").lower(),
            str(job.get("text") or job.get("summary") or "").lower())


_FIXED_TERM = re.compile(r"\bfixed[\s-]term\b|\bftc\b")
_CONTINGENT = {"contract", "interim", "ftc"}
_BODY_PERMANENT = re.compile(
    r"\bpermanent\s+(?:role|position|job|employment|contract|basis|opportunity)\b"
    r"|\b(?:role|position|job|employment|contract|type)\s*(?:is\s+|:\s*|-\s*)"
    r"(?:full[\s-]time\s*[,;/–-]?\s*)?permanent\b"
    r"|(?:^|[\n.;])\s*(?:full[\s-]time\s*[,;/–-]?\s*)?permanent\b"
    r"|[|:;–—-]\s*permanent\s*(?=$|[|:;,–—.\n-])"
    r"|\bfull[\s-]time\s*[,;/–-]?\s*permanent\b")
_BODY_CONTRACT = re.compile(
    r"\bcontract\s+(?:role|position|opportunity|basis)\b|\b(?:rolling|freelance)\s+contract\b"
    r"|\b(?:\d+|three|six|nine|twelve)[\s-]+months?\s+(?:initial\s+)?contract\b"
    r"|\bcontract\s*[:,–-]\s*(?:\d+|three|six|nine|twelve)\s*months?\b"
    r"|\b(?:day[\s-]rate|ir35|umbrella company)\b")


def _engagement_evidence(job: dict, ef: dict | None = None) -> tuple[str, list[str], list[str]]:
    """Advert wording outranks feed metadata; contradictory facts remain visible.

    Full-time describes hours, not permanence. Neither annual salaries nor feed salary amounts
    determine an engagement. Generic references to employment contracts are not contract roles.
    """
    title, body = _job_text(job)
    evidence = []

    def stated(source, text, description=False):
        claims = []
        if _FIXED_TERM.search(text):
            claims.append("ftc")
        interim = (re.search(r"\b(?:interim|fractional)\s+(?:role|position|basis|leadership|head|cto|director|manager)\b", text)
                   if description else re.search(r"\b(?:interim|fractional)\b", text))
        if interim:
            claims.append("interim")
        permanent = (_BODY_PERMANENT.search(text) or
                     (len(title.split()) >= 2 and re.search(r"\bpermanent\s+" + re.escape(title) + r"\b", text))
                     if description else _word("permanent", text))
        if permanent:
            claims.append("permanent")
        contract = (_BODY_CONTRACT.search(text) or RATE_RE.search(text) if description else
                    re.search(r"\b(?:contract|contractor|freelance|temporary)\b", text))
        # A fixed-term employment contract or a field headed "Contract: Permanent" is not a
        # second, contradictory contract claim. Configured markers only fill missing evidence.
        if contract and not any(c in _CONTINGENT for c in claims):
            claims.append("contract")
        if not claims and ef:
            markers = (ef.get("text_markers", ef.get("contract_markers", [])) if description
                       else ef.get("strong_markers", []))
            if any(_word(str(marker).lower(), text) for marker in markers):
                claims.append("contract")
        evidence.extend((source, claim) for claim in claims)

    stated("Advert text", body, description=True)
    stated("Title", title)
    stated("employment_type", str(job.get("employment_type") or "").lower())
    stated("Stored engagement", str(job.get("engagement") or "").lower())
    # Some boards put the commitment alongside the location (e.g. Lever).
    stated("Location/commitment", str(job.get("location") or "").lower())
    if not evidence:
        return "unknown", [], ["Engagement is unstated or unclear."]

    source, chosen = evidence[0]
    # A specific interim/FTC statement refines a compatible generic contract label.
    if chosen == "contract":
        source, chosen = next(((s, c) for s, c in evidence if c in ("interim", "ftc")), (source, chosen))
    conflicts = [f"{source} states {chosen}; {other_source} says {claim}."
                 for other_source, claim in evidence if
                 (chosen == "permanent" and claim in _CONTINGENT)
                 or (chosen in _CONTINGENT and claim == "permanent")]
    # Explicit alternatives in the same source do not establish one engagement.
    ambiguous = any(s == source and ((chosen == "permanent" and c in _CONTINGENT)
                                    or (chosen in _CONTINGENT and c == "permanent")) for s, c in evidence)
    reasons = [f"{s} states {claim}." for s, claim in evidence]
    return ("unknown" if ambiguous else chosen), conflicts, reasons


_SOFTWARE_DISCIPLINE = (r"software|developer|full[\s-]stack|front[\s-]end|back[\s-]end|"
                        r"ai|artificial intelligence|machine learning|llm")
# A discipline plus an occupation establishes a software title; a lone "AI" in a physical
# industry label does not. Broad QA/test titles continue to need duties when physical work appears.
_SPECIFIC_SOFTWARE_TITLE = re.compile(
    r"\b(?:software\s+(?:(?:quality|test|platform|application)\s+)?(?:engineer(?:ing)?|developer)"
    r"|(?:full[\s-]stack|front[\s-]end|back[\s-]end|ai|machine learning|llm)\s+(?:engineer(?:ing)?|developer)"
    r"|(?:head|director|vp) of software)\b")
_SOFTWARE_TITLE = re.compile(
    r"\b(?:" + _SOFTWARE_DISCIPLINE + r"|"
    r"qa|sdet|test (?:manager|lead|architect|analyst)|head of (?:test|testing))\b")
# Testing phrases name a software delivery remit (value gate, 10 October: SIT/UAT adverts were "unknown").
_SOFTWARE_WORK = re.compile(
    r"\bsoftware\s+(?:engineer(?:ing|s)?|develop(?:ment|er|ers)|test(?:ing|s)?|platform|product|application)\b"
    r"|\b(?:develop\w*|build\w*|creat\w*|design\w*|test\w*)\b[^.;\n]{0,65}\bsoftware\b"
    r"|\bsoftware\s+(?:quality engineering|release (?:readiness|reliability|gates))\b"
    r"|\b(?:saas|web applications?|test automation|ai evaluation|llm evaluation)\b"
    r"|\b(?:uat|system integration test\w*|user acceptance test\w*|regression test\w*|defect triage)\b")
# Technical nouns establish delivery only in a role-owned duty below. Merely using an LLM
# for physical work, Python alone, or another team's engineering does not establish this remit.
_AI_SOFTWARE_WORK = re.compile(
    r"\b(?:production[ -](?:grade[ -])?)?llm\s+(?:systems?|(?:agent\s+)?workflows?|orchestration)\b"
    r"|\bpython\b[^.;\n]{0,120}\b(?:ai engineering|llm|agent workflows)\b")
_ROLE_WORK = re.compile(r"^(?:the|your) work\s+(?:is|involves|includes)\s+", re.I)
_PHYSICAL_WORK = re.compile(
    r"\b(?:bms|hvac|building[\s-]services|building[\s-]management[\s-]systems?|"
    r"facilities[\s-]management|facilities[\s-]maintenance|plant[\s-]maintenance|"
    r"mechanical[\s-]engineering|electrical[\s-]engineering|civil[\s-]engineering|"
    r"structural[\s-]engineering|geotechnical[\s-]engineering|"
    r"construction[\s-](?:cost|design|project|management)|"
    r"manufacturing[\s-](?:plant|site|facility)|food[\s-]safety|haccp|brcgs)\b")

_ROLE_PREFIX = re.compile(
    r"^(?:you(?:['’]ll|\s+(?:will|would|must|are expected to|are required to))\s+"
    r"|(?:the|this|your)\s+(?:role|position)\s+(?:will|involves|includes|is to)\s+"
    r"|(?:your\s+)?(?:responsibilities|duties)\s*(?::|include)\s*"
    r"|(?:be\s+)?responsible for\s+|we need you to\s+)", re.I)
_ROLE_ACTION = re.compile(
    r'^(?:build(?:ing)?|develop(?:ing)?|design(?:ing)?|implement(?:ing)?|lead(?:ing)?|own(?:ing)?|'
    r'improve|improving|deliver(?:ing)?|manage|managing|recover(?:ing)?|restore|restoring|coach(?:ing)?|grow(?:ing)?)\b', re.I)
_NEGATED_ACTION = re.compile(r"\b(?:not|never|no|cannot|without|excluding)\b|\b\w+n['’]t\b", re.I)
# Canonicalise bounded duty equivalents once for remit qualification and proof matching. This
# changes only the comparison text: original source quotations and actor/negation guards remain.
_DUTY_VERBS = {'improving': 'improve', 'managing': 'manage', 'restoring': 'restore',
               'drive': 'lead', 'driving': 'lead', 'strengthen': 'improve', 'strengthening': 'improve'}
_DUTY_VERB = re.compile(r'^(?:' + '|'.join(_DUTY_VERBS) + r')\b', re.I)


def role_action(clause):
    """A role-owned duty, with equivalent verbs normalised for bounded remit comparisons."""
    if (_NEGATED_ACTION.search(clause) or re.search(r'\b(?:while|whereas|but|although)\b|'
                                                  r'\band\s+(?:our|their|another|other|your|the)\b', clause, re.I)):
        return ''
    action = clause.lstrip(' \t-*•')
    for _ in range(2):
        action = _ROLE_PREFIX.sub('', action, count=1)
    action = _DUTY_VERB.sub(lambda match: _DUTY_VERBS[match[0].lower()], action, count=1)
    return action if _ROLE_ACTION.match(action) else ''


def substantive_role_action(clause):
    """Specific role-owned work, independent of candidate fit or a title's inferred domain."""
    action = role_action(clause)
    # An advert's promise to develop the applicant is not an assigned duty.
    # Keep the object boundary even when a discipline intervenes ("your software
    # engineering skills") or growth is framed as/within a role rather than work.
    applicant_growth = (re.match(r'^\w+\s+(?:as|into|in|within)\s',action,re.I) or
        re.search(r'\b(?:your|an?)\s+(?:[\w/-]+\s+){0,5}'
                  r'(?:careers?|future|potential|dreams?|skills?|skillset|expertise|experience|understanding)\b',action,re.I))
    if not action or applicant_growth:
        return ''
    owned = re.split(r'\b(?:using|use|with|via)\b',action,maxsplit=1,flags=re.I)[0].casefold()
    specific = (_SOFTWARE_WORK.search(owned) or _AI_SOFTWARE_WORK.search(owned) or _PHYSICAL_WORK.search(owned)
                or re.search(r'\b(?:software|engineering)\s+(?:teams?|delivery)\b',owned,re.I))
    return action if specific else ''


def _software_delivery(title, body, physical):
    if not physical:
        stated = _SOFTWARE_TITLE.search(title) or _SOFTWARE_WORK.search(body)
        if stated:
            return stated
    # QA/testing titles alone are ambiguous in a physical sector. Actual software delivery still
    # qualifies there; using software as a tool, another team's work and negated duties do not.
    stated = _SPECIFIC_SOFTWARE_TITLE.search(title)
    if stated:
        return stated
    for clause in re.split(r'\n|(?<=[.!?])\s+|;', body):
        clause = clause.strip()
        # Explicit "your/the work is Python ..." describes the same ownership as a duty verb.
        ai_action = role_action(_ROLE_WORK.sub('build ', clause, count=1))
        ai_owned = re.split(r'\b(?:using|use|with|via)\b', ai_action, maxsplit=1, flags=re.I)[0]
        stated = _AI_SOFTWARE_WORK.search(ai_owned)
        if stated:
            return stated
        action = role_action(clause)
        owned = re.split(r'\b(?:using|use|with|via)\b', action, maxsplit=1, flags=re.I)[0]
        # Introducing an existing software tool into plant work is still physical work. Building
        # the software tool itself remains software delivery in the same industry.
        if re.search(r'\b(?:software|test automation)\b.{0,50}\btools?\b', owned, re.I) and not re.match(
                r'^(?:build|develop|design)\w*\b', owned, re.I):
            continue
        stated = _SOFTWARE_WORK.search(owned)
        if stated:
            return stated
    return None


_QUALIFIED: dict[bytes, str] = {}
_QUALIFIED_MAX = 8192
_QUALIFIED_MAX_BYTES = 8 * 1024 * 1024
_QUALIFIED_BYTES = 0
_QUALIFIED_LOCK = threading.Lock()
_REQUIREMENT_LIMIT = 32
_TEXT_LIMIT = 16000
_ESSENTIAL = re.compile(r'\b(?:must(?:[ -]have)?|essential|mandatory|required|prerequisite)\b', re.I)
# "Bonus" alone is commonly compensation, not candidate suitability. Only explicit optional
# qualification wording contributes that meaning; the original pay/source text stays untouched.
_DESIRABLE = re.compile(r'\b(?:desirable|preferred|nice[ -]to[ -]have|ideally|advantage|would be useful|'
                        r'(?:is|are|would be)\s+(?:an?\s+(?:added\s+)?)?bonus|bonus points for)\b', re.I)
_NOT_REQUIRED = re.compile(r'\bnot\s+(?:a\s+)?(?:strictly\s+)?(?:essential|required|mandatory|necessary)\b|\bno\b[^.;\n]{0,80}\brequired\b|\b(?:(?:do not|don.t)\s+need|need not)\b|\b(?:isn.t|aren.t)\s+(?:essential|required|mandatory|necessary)\b', re.I)
_APPLICATION_HEADINGS = ('application form', 'application', 'how to apply', 'apply for this job',
                         'apply for this role', 'apply now', 'submit your application')
# Flattened form headings need a delimiter or a recognisable form field/question after them;
# "Application Form Design experience" is still a qualification, with its full scope intact.
_APPLICATION_SECTION = re.compile(r'\b((?i:' + '|'.join(map(re.escape, _APPLICATION_HEADINGS))
                                  + r'))(?=\s*:|\s*\n|\s+(?i:full name|first name|last name|preferred name|'
                                    r'email(?: address)?|cv|resume|upload|attach|do you|are you|can you)\b)\s*:?\s*')
_REQUIREMENT_HEADINGS = {
    'essential': 'essential', 'essential requirements': 'essential', 'essential skills': 'essential',
    'mandatory requirements': 'essential', 'required qualifications': 'essential',
    'must have': 'essential', 'must-have': 'essential', 'minimum requirements': 'essential',
    'desirable': 'desirable', 'desirable skills': 'desirable', 'preferred': 'desirable',
    'preferred qualifications': 'desirable', 'nice to have': 'desirable', 'nice-to-have': 'desirable',
    'requirements': 'ambiguous', 'qualifications': 'ambiguous', 'skills': 'ambiguous',
    'what you bring': 'ambiguous', 'what we are looking for': 'ambiguous',
    'your skills & experience': 'ambiguous', 'about you': 'ambiguous',
    'responsibilities': None, 'duties': None, 'the role': None, 'benefits': None, 'about us': None,
    'what you will do': None, 'our offer': None, 'what they offer': None,
    **dict.fromkeys(_APPLICATION_HEADINGS),
}
_QUALIFICATION_DETAIL = re.compile(r'\b(?:experience|skills?|knowledge|proficiency|familiarity|'
                                   r'qualifications?|degrees?|certificat(?:e|ion)s?|clearance|'
                                   r'sponsorship|visas?|work permits?|right to work|'
                                   r'(?:authori[sz]ed|eligible) to work|fluency|fluent)\b', re.I)
_FORM_REQUEST = r'(?:(?:you\s+)?(?:must|are required to|need to)\s+|please\s+)?'
_FORM_FIELD = r'(?:(?:(?:full|legal|preferred|first|last|family)\s+)*name|e-?mail(?: address)?|phone number)'
_FORM_CONTEXT = r'(?:this|the) (?:application(?: form)?|form)(?: section \d+)?'
_FORM_SUFFIX = (r'(?:\s+(?:here|below|in (?:this|the) (?:required )?field|on ' + _FORM_CONTEXT
                + r'|to (?:complete|submit) ' + _FORM_CONTEXT
                + r'|when (?:completing|submitting) ' + _FORM_CONTEXT + r'))?\s*[.!?]?')
_APPLICATION_INSTRUCTIONS = tuple(re.compile(pattern, re.I) for pattern in (
    r'if you (?:have|use) (?:a )?preferred name,? (?:please )?(?:enter|use) it (?:here|below|to apply)[.!?]?',
    r'(?:(?:your|a) )?preferred name(?:\s*\((?:optional|required)\))?\s*[:*.!?]?',
    _FORM_REQUEST + r'(?:enter|provide|type|use|confirm)\s+(?:(?:your|an?)\s+)?' + _FORM_FIELD + _FORM_SUFFIX,
    _FORM_REQUEST + r'(?:upload|attach|submit)\s+(?:(?:your|a|the)\s+)?(?:cv|résumé|resume|cover letter)' + _FORM_SUFFIX,
    r'(?:(?:your|a|the)\s+)?(?:' + _FORM_FIELD + r'|cv|résumé|resume|cover letter)\s+'
    r'(?:is|are|must be)\s+(?:required|mandatory|provided|uploaded)' + _FORM_SUFFIX,
    r'(?:all\s+)?fields marked (?:with an asterisk|with \*|\*) (?:are required|must be completed)[.!?]?',
    r'required fields\s*[:*.!?]?',
))


def _application_instruction(clause: str) -> bool:
    """Ignore only complete administrative clauses, never unknown trailing conditions.

    A name/email/upload prefix cannot rule out age, residency or other eligibility wording.
    Full matches preserve mixed clauses without needing an exhaustive qualification word list.
    """
    return any(pattern.fullmatch(clause) for pattern in _APPLICATION_INSTRUCTIONS)


def requirement_phrase(text: str) -> str:
    """Remove grammatical requirement framing, never specialist scope or experience qualifiers."""
    phrase = text.strip(' \t-*•.').strip()
    phrase = re.sub(r"\b(is|are)n['’]t\b", r'\1 not', phrase, flags=re.I)
    phrase = re.sub(r'^(?:(?:you\s+)?must(?:\s+have)?\s+|(?:essential|required|mandatory)\s*:\s*)', '', phrase, flags=re.I)
    phrase = re.sub(r'^(?:experience\s+(?:with|in|of)\s+|experience\s+|knowledge\s+of\s+)', '', phrase, flags=re.I)
    phrase = re.sub(r'\s+(?:is|are)\s+(?:not\s+)?(?:essential|mandatory|required|desirable|preferred)$', '', phrase, flags=re.I)
    phrase = re.sub(r'\s+(?:experience|skills)$', '', phrase, flags=re.I)
    return re.sub(r'^either\s+', '', phrase, flags=re.I).strip()


def general_competency(text: str) -> bool:
    """Recognise only unqualified communication competencies, never establish candidate proof.

    The entire clause must fit. Specialist, language, numeric or mixed requirements remain material
    unknowns; an ordinary competency can stay explicitly unassessed without a specialist veto.
    """
    return bool(re.fullmatch(r'(?:(?:excellent|strong|good|effective)\s+)?'
                             r'(?:(?:written|verbal|oral)(?:\s+and\s+(?:written|verbal|oral))?\s+)?'
                             r'communication', requirement_phrase(text), re.I))


def _requirement_clauses(line):
    # Only split conjunctions whose two sides each state their own importance. Otherwise retain
    # the whole statement: an AND/OR mixture cannot safely be reduced to one matching OR branch.
    for sentence in re.split(r'(?<=[.!?])\s+|;\s*|\s+(?:but|however|whereas)\s+', line, flags=re.I):
        remaining = sentence.strip()
        for _ in range(_REQUIREMENT_LIMIT):
            split = None
            for match in re.finditer(r'\s+and\s+|,\s*', remaining, re.I):
                left, right = remaining[:match.start()], remaining[match.end():]
                if ((_ESSENTIAL.search(left) or _DESIRABLE.search(left) or _NOT_REQUIRED.search(left))
                        and (_ESSENTIAL.search(right) or _DESIRABLE.search(right) or _NOT_REQUIRED.search(right))):
                    split = left, right
                    break
            if split is None:
                break
            yield split[0]
            remaining = split[1]
        if remaining:
            yield remaining


def requirements(text: str) -> tuple[list[dict], bool]:
    """Bounded literal requirement clauses, without a skills assessment or guessed importance.

    Unmarked qualification lists stay ambiguous. Negated requirements and alternatives survive so
    callers cannot silently turn an optional skill or one branch of an alternative into a gate.
    """
    text = str(text or '')
    truncated = len(text) > _TEXT_LIMIT
    section, form_section, result, seen = None, False, [], set()
    # Existing scanner text flattens HTML headings. Recognise explicit heading phrases without
    # rewriting stored source text or guessing that a bare skill mention is mandatory.
    bounded = re.sub(r'\b(minimum requirements|essential requirements|essential skills|mandatory requirements|'
                     r'required qualifications|desirable skills|preferred qualifications)\b\s*:?\s*',
                     r'\n\1:\n', text[:_TEXT_LIMIT], flags=re.I)
    bounded = re.sub(r'\b(Benefits|Responsibilities|Duties|About [uU]s|What you will do|Our offer|'
                     r'The Role|Your Skills & Experience|About You|What They Offer)\b\s*:?\s*',
                     r'\n\1:\n', bounded)
    bounded = _APPLICATION_SECTION.sub(r'\n\1:\n', bounded)
    for line in bounded.splitlines():
        line = line.strip(' \t-*•')
        heading, separator, remainder = line.partition(':')
        key = heading.strip().casefold()
        if key in _REQUIREMENT_HEADINGS:
            section = _REQUIREMENT_HEADINGS[key]
            form_section = key in _APPLICATION_HEADINGS
            line = remainder.strip() if separator else ''
        elif line.endswith(':') and len(line) < 90:
            section, form_section, line = None, False, ''
        for clause in _requirement_clauses(line):
            clause = clause.strip()
            if not clause or _application_instruction(clause):
                continue
            # Collective company values are not candidate qualifications. Retain their wording
            # as ambiguous context; real applicant requirements and prohibitions still apply.
            culture = bool(re.search(r'\b(?:essential|important) that we (?:be|are|remain)\b', clause, re.I))
            waived = bool(_NOT_REQUIRED.search(clause))
            negated = waived or bool(re.search(r'\bmust not\b', clause, re.I))
            mixed = bool(_ESSENTIAL.search(clause) and _DESIRABLE.search(clause))
            uncertain_section = bool(section == 'essential' and re.search(r'\b(?:benefits|responsibilities|duties)\b', clause, re.I))
            importance = ('ambiguous' if waived or mixed or uncertain_section or culture else 'desirable' if _DESIRABLE.search(clause)
                          else 'essential' if _ESSENTIAL.search(clause) else section)
            if importance is None and re.search(r'\b(?:experience (?:with|in|of)|knowledge of|proficiency in|familiarity with)\b', clause, re.I):
                importance = 'ambiguous'
            if importance is None and form_section and clause.endswith('?') and _QUALIFICATION_DETAIL.search(clause):
                importance = 'ambiguous'
            if importance is None:
                continue
            identity = (clause.casefold(), importance, negated)
            if identity in seen:
                continue
            if len(result) >= _REQUIREMENT_LIMIT:
                return result, True
            seen.add(identity)
            phrase = requirement_phrase(clause)
            alternatives = (re.split(r'\s+or\s+', phrase, flags=re.I)
                            if re.search(r'\s+or\s+', phrase, re.I) and not re.search(r'\b(?:and|with)\b|[,/&()]', phrase, re.I) else [])
            result.append(dict(text=clause[:500], importance=importance, negated=negated, waived=waived, unresolved=(mixed or uncertain_section) and not waived,
                               alternatives=alternatives[:8], truncated=len(clause) > 500 or len(alternatives) > 8))
    return result, truncated


def qualification(record: dict) -> dict:
    """Evidence-based software remit and engagement for incoming jobs or stored records.

    Unknown remit stays eligible for qualification. Only explicit physical-engineering duties
    exclude a record; broad categories and a company operating in a physical industry do not.
    The verdict depends only on the five texts below, so it is reused for unchanged adverts across
    workspace builds. Digest keys identify byte-bounded serialized verdicts, including extracted
    requirement quotations; the cache does not retain unbounded source bodies.
    """
    global _QUALIFIED_BYTES
    # Keep original case for attributable quotations; classification lowercases its own inputs.
    title = str(record.get('title') or record.get('name') or '')
    body = str(record.get('text') or record.get('summary') or '')[:_TEXT_LIMIT + 1]
    texts = (title, body, *(str(record.get(k) or "") for k in ("employment_type", "engagement", "location")))
    digest = hashlib.blake2b(digest_size=16)
    for text in texts:
        raw = text.encode()
        digest.update(len(raw).to_bytes(8, "little") + raw)
    key = digest.digest()
    with _QUALIFIED_LOCK:
        if not _QUALIFIED:
            _QUALIFIED_BYTES = 0
        encoded = _QUALIFIED.get(key)
    if encoded is not None:
        return json.loads(encoded)
    verdict = _qualify(dict(zip(("title", "text", "employment_type", "engagement", "location"), texts)))
    encoded = json.dumps(verdict, separators=(',', ':'), ensure_ascii=True)
    size = len(encoded)  # ASCII serialization bounds retained values, including quote strings.
    with _QUALIFIED_LOCK:
        previous = _QUALIFIED.pop(key, '')
        _QUALIFIED_BYTES -= len(previous)
        if len(_QUALIFIED) >= _QUALIFIED_MAX or _QUALIFIED_BYTES + size > _QUALIFIED_MAX_BYTES:
            _QUALIFIED.clear()
            _QUALIFIED_BYTES = 0
        if size <= _QUALIFIED_MAX_BYTES:
            _QUALIFIED[key] = encoded
            _QUALIFIED_BYTES += size
    return verdict


def _qualify(record: dict) -> dict:
    title, body = _job_text(record)
    physical = _PHYSICAL_WORK.search(f"{title} {body}")
    software = _software_delivery(title, body, physical)
    if software:
        domain, reasons = "software", [f"Software remit: {software.group(0)}."]
    elif physical:
        domain, reasons = "outside", [f"Outside software remit: {physical.group(0)}."]
    else:
        domain, reasons = "unknown", ["Software remit is not established by the available text."]
    engagement, conflicts, engagement_reasons = _engagement_evidence(record)
    wanted, truncated = requirements(record.get('text') or record.get('summary') or '')
    return {"domain": domain, "engagement": engagement, "conflicts": conflicts,
            "reasons": reasons + engagement_reasons, "eligible": domain != "outside",
            'requirements': wanted, 'requirements_truncated': truncated}


def engagement_of(job: dict, ef: dict) -> str:
    return _engagement_evidence(job, ef)[0]


RATE_RE = re.compile(r"£\s?(\d{3,4})(?:\s?[-–to]+\s?£?\s?(\d{3,4}))?\s*(?:/|per|a)\s*day", re.I)


def day_rate(job: dict) -> tuple[str, int | None]:
    """The span of every sterling day rate the advert states. Agencies often give a PAYE rate before
    the umbrella or limited-company one ("£463/day PAYE, £537/day umbrella"); reading only the first
    would put a contract under the floor that its contractor rate clears."""
    blob = f"{job.get('rate','')} {job.get('text','')[:6000]}"
    found = [(int(m.group(1)), int(m.group(2) or m.group(1))) for m in RATE_RE.finditer(blob)]
    if not found:
        return (job.get("rate") or "", None)
    lo = min(a for a, _ in found)
    hi = max(b for _, b in found)
    return (f"£{lo}" + (f"–{hi}" if hi != lo else "") + "/day", hi)


_AMOUNT = r"(\d[\d,]*(?:\.\d+)?)(?![\d,])\s?([kK])?(?!\s?(?:m\b|bn\b|million|billion))"
PAY_RE = re.compile(r"([£$€])\s?" + _AMOUNT + r"(?:\s?(?:-|–|—|to)\s?[£$€]?\s?" + _AMOUNT + r")?(.{0,30})", re.S)
CURRENCY = {"£": "GBP", "$": "USD", "€": "EUR"}
PERIOD_MARKERS = [
    ("day", ("per day", "/day", "a day", "daily", "p/d", "day rate")),
    ("hour", ("per hour", "/hour", "an hour", "/hr", "p/h", "hourly")),
    ("month", ("per month", "/month", "a month", "pcm", "monthly")),
    ("year", ("per annum", "p.a", "a year", "per year", "/year", "annual", "salary", "base", "ote", "/yr")),
]
SANE = {"day": (100, 5000), "hour": (10, 1000), "month": (1000, 60000), "year": (10000, 2000000)}


def text_pay(text: str):
    """First plausible pay figure in advert text: (min, max, currency, period) or None. A day rate
    spans every day rate stated in the same currency, as ``day_rate`` does, so a PAYE figure given
    first does not stand for the whole advert."""
    # Canonical imported pay and manual captures may use ISO codes rather than symbols.
    text = re.sub(r'\b(GBP|EUR|USD)\s*(?=\d)',
                  lambda m: {'GBP':'£', 'EUR':'€', 'USD':'$'}[m[1].upper()], text or '', flags=re.I)
    first = None
    for m in PAY_RE.finditer(text or ""):
        sym, a, ak, b, bk, tail = m.groups()
        lo = float(a.replace(",", "")) * (1000 if ak else 1)
        hi = float(b.replace(",", "")) * (1000 if bk else 1) if b else lo
        if bk and not ak and lo < 1000:
            lo *= 1000
        t = re.sub(r'/\s+', '/', tail.lower())
        period = next((p for p, marks in PERIOD_MARKERS if any(k in t for k in marks)), "")
        if not period and (ak or bk or hi >= 10000):
            period = "year"
        if not period or lo > hi:
            continue
        floor, ceiling = SANE[period]
        if not (floor <= lo and hi <= ceiling):
            continue
        if first is None:
            if period != "day":
                return (_whole(lo), _whole(hi), CURRENCY[sym], period)
            first = [lo, hi, CURRENCY[sym]]
        elif period == "day" and CURRENCY[sym] == first[2]:
            first[0], first[1] = min(first[0], lo), max(first[1], hi)
    return (_whole(first[0]), _whole(first[1]), first[2], "day") if first else None


def _whole(v: float):
    return int(v) if v == int(v) else v


def ir35_of(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"outside\s+(?:of\s+)?(?:the\s+)?ir35", t):
        return "outside"
    if re.search(r"inside\s+(?:of\s+)?(?:the\s+)?ir35", t):
        return "inside"
    return ""


def _num(v) -> str:
    return "" if v is None else str(int(v)) if float(v) == int(v) else f"{v:.2f}"


def observed_terms(job: dict, engagement_filter: dict) -> dict:
    """Normalize the current source-stated terms, including deliberate empty values."""
    rate, _ = day_rate(job)
    pay, pay_source = job.get("pay"), "ats"
    if not pay:
        found = text_pay(f"{job.get('rate', '')} {(job.get('text') or '')[:6000]}")
        pay, pay_source = (found, "text") if found else (None, "")
    feed_pay = job.get("feed_pay")
    if not pay and feed_pay:
        pay_min, pay_max, currency = _num(feed_pay[0]), _num(feed_pay[1]), feed_pay[2]
        period, pay_source = "", "feed"
    else:
        pay_min = _num(pay[0]) if pay else ""
        pay_max = _num(pay[1]) if pay else ""
        currency = pay[2] if pay else ""
        period = pay[3] if pay else ""
    if not rate.endswith("/day") and pay:
        rate = f"{pay[2]} {_num(pay[0])}" + (f"–{_num(pay[1])}" if pay[1] != pay[0] else "") + f"/{pay[3]}"
    return {
        "engagement": engagement_of(job, engagement_filter),
        "rate": rate or "unstated",
        "pay_min": pay_min, "pay_max": pay_max,
        "pay_currency": currency, "pay_period": period, "pay_source": pay_source,
        "ir35": ir35_of(job.get("text") or ""),
        "workplace": job.get("workplace", "") or "",
    }


US_STATE_RE = re.compile(r"(?:,\s*|\b)(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY|DC)\b(?![a-z])")
US_WORDS = ["united states", "usa", "u.s.", "north america", "americas", "california", "texas", "washington", "sunnyvale", "palo alto", "san francisco", "seattle", "austin", "denver", "chicago", "los angeles", "san diego", "honolulu", "detroit", "milpitas", "mountain view", "menlo park", "san jose", "atlanta", "miami", "phoenix", "dallas", "houston", "portland", "salt lake", "minneapolis"]


def location_ok(location: str, text: str, lf: dict) -> tuple[bool, str]:
    loc = (location or "").lower()
    blob = f"{loc} {text[:2000].lower()}"
    for r in lf.get("reject_any_of", []):
        if r in loc:
            return False, f"location-reject:{r}"
        # Explicit eligibility requirements override broad labels such as "Remote".
        # A country mentioned as a colleague's location is not a residence requirement.
        if (r.startswith("must ") or r.endswith(" only") or r == "relocation required") and _word(r, (text or "").lower()):
            return False, f"description-reject:{r}"
    # Lever puts the commitment in the location string ("New York, NY / Internship").
    if _word("internship", loc) or _word("intern", loc):
        return False, "location-reject:internship"
    # Named US regions/cities (and multi-city US listings such as "San Francisco | New York") are
    # rejected before the accept list; an east-coast-only listing still passes via the accept list.
    if any(w in loc for w in US_WORDS):
        return False, "location-reject:us"
    for a in lf.get("accept_any_of", []):
        if _word(a, loc) if len(a) <= 3 else a in loc:
            return True, f"location:{a}"
    # Any other US state: outside the ±6h rule.
    if US_STATE_RE.search(location or ""):
        return False, "location-reject:us"
    if not loc:
        return True, "location:unstated"
    # Unlisted location: keep but flag, unless description also screams elsewhere-only
    for r in lf.get("reject_any_of", []):
        if r in blob:
            return False, f"location-reject:{r}"
    return True, "location:unlisted"


def intake_rejection(job, portals, pay, *, today=None):
    """Known contradictions only. Sparse evidence is not proof of unsuitability."""
    title, body = _job_text(job)
    _, reason = title_match(title, portals.get('title_filter', {}))
    if reason.startswith('negative:'):
        return reason
    if not qualification(job)['eligible']:
        return 'outside-domain'
    ok, reason = location_ok(job.get('location') or '', body, portals.get('location_filter', {}))
    if not ok:
        return reason
    if today and job.get('expires'):
        import datetime as dt
        try:
            if dt.date.fromisoformat(str(job['expires'])[:10]) < today:
                return 'source-expired'
        except ValueError:
            pass  # An invalid date is unknown, not proof that a role has expired.
    pay_text = f"{job.get('rate') or ''} {job.get('pay') or ''} {body[:6000]}"
    # Discovery needs proof of an incompatible ceiling, not a parsed starting salary.
    for figure in PAY_RE.finditer(pay_text):
        prefix, tail = pay_text[max(0, figure.start() - 35):figure.start()], figure.group(6)
        if (re.search(r'(?i)(?:from|starting at|minimum(?: of)?|at least)\s*$', prefix)
                or re.match(r'(?i)\s*(?:\+|plus\b|and (?:above|upwards)\b)', tail)
                or re.search(r'(?i)\bnegotiable\b', tail)):
            return ''
    stated = text_pay(f"{job.get('rate') or ''} {body[:6000]}")
    declared = job.get('pay')
    if not stated and isinstance(declared, (tuple, list)) and len(declared) == 4:
        stated = declared
    if not stated and isinstance(declared, str):
        stated = text_pay(declared)
    currency, period = job.get('pay_currency'), job.get('pay_period')
    try:
        if stated:
            _, top, currency, period = stated
        else:
            # A lower bound alone does not establish that the ceiling is too low.
            top = job.get('pay_max') or ''
        top = float(top)
    except (ValueError, TypeError):
        return ''
    import math
    if (math.isfinite(top) and top > 0 and currency == 'GBP' and period in ('day', 'year')
            and top < pay['floor_' + period + '_gbp']):
        return 'below-pay-floor'
    return ''
