"""Declarative lifecycles that were previously spread through progress and decision code.

Stored record fields stay in `states.TRANSITIONS` (job status, opportunity stage, organisation
status, person relationship). This module owns the derived approach progress, the actions and
sections that follow from it, and the decision status graph. It does not read the database.
An outcome recorded without an approach id remains a fact report; a named approach may record
only an outcome its card offers.
"""
from __future__ import annotations

from typing import NamedTuple


class MachineError(ValueError):
    """A move the declared machine does not allow."""


class Offer(NamedTuple):
    method: str
    event: str
    label: str
    kinds: tuple[str, ...] = ()
    actor: str = 'owner'
    guard: str = ''


class Attention(NamedTuple):
    section: str
    text: str


class DecisionMove(NamedTuple):
    to: str
    actor: str
    guard: str = ''


# Progress statuses shared by applications and other approaches. Not every status occurs on both.
PROGRESS = (
    'research', 'ready', 'review', 'approved', 'waiting', 'follow_up', 'replied', 'submitted',
    'conversation', 'proposal', 'won', 'handed_over', 'held', 'no_response', 'interview', 'offer',
    'closed', 'skipped',
)
PRE_CONTACT = ('ready', 'review', 'approved')
ACTIVE = ('waiting', 'follow_up', 'replied', 'submitted', 'conversation', 'proposal', 'interview', 'offer')
TERMINAL = ('held', 'no_response', 'closed', 'skipped', 'won', 'handed_over')
# A research approach is not a person-queue action. The browser list is workspace/js/machines.js.
PERSON_TERMINAL = ('research', *TERMINAL)
NEEDS_REPLY = ('replied', 'conversation')
ACTIVE_JOBS = ('applied', 'responded', 'interview', 'offer')
# Job statuses that map straight onto progress. `applied` is timed; candidate and pursue are not here.
JOB_PROGRESS = {
    'skip': ('skipped', False),
    'rejected': ('closed', False),
    'closed': ('closed', False),
    'responded': ('replied', True),
    'interview': ('interview', True),
    'offer': ('offer', True),
}

SECTION_OF_STATUS = {
    'research': 'research',
    'held': 'parked', 'no_response': 'parked', 'closed': 'parked', 'skipped': 'parked',
    'won': 'done', 'handed_over': 'done',
    **{status: 'in_progress' for status in ACTIVE},
}
KIND_SECTION = {
    'catch_up': 'people', 'ask': 'people', 'desk': 'desks', 'recruiter': 'desks',
    'note': 'decision_makers', 'opportunity': 'opportunities', 'application': 'adverts', 'tender': 'adverts',
}

# Weekly attention for active and shortlisted work. `work_selection` places the record; the cause
# chooses Act now, Waiting or In progress.
ATTENTION = {
    'reply': Attention('act_now', 'They replied; a response is yours.'),
    'their_step': Attention('waiting', 'The recorded next step is theirs.'),
    'your_step': Attention('act_now', 'The recorded next step is yours.'),
    'incoming': Attention('in_progress', 'An incoming hiring process with only a free-text step.'),
    'stage': Attention('act_now', 'An offer, interview, reply or follow-up stage.'),
    'shortlisted': Attention('act_now', 'A shortlisted role with no recorded step.'),
    'submitted': Attention('waiting', 'Applied, submitted or proposed; awaiting them.'),
    'unassigned': Attention('in_progress', 'Active work with no recorded responsibility.'),
}
STAGE_RANK = {'offer': 0, 'interview': 2, 'replied': 3, 'responded': 3, 'follow_up': 4}
AWAITING = ('waiting', 'submitted', 'proposal', 'applied')

# Application card, primary first. Job-status moves are still limited by states.TRANSITIONS.
APPLICATION_OFFERS = {
    'ready': (Offer('transition', 'applied', 'I applied'),),
    'waiting': (
        Offer('transition', 'responded', 'They responded'),
        Offer('outcome', 'followed_up', 'I followed up'),
        Offer('transition', 'rejected', 'Rejected'),
        Offer('transition', 'closed', 'Close the application'),
    ),
    'follow_up': (
        Offer('outcome', 'followed_up', 'I followed up'),
        Offer('transition', 'responded', 'They responded'),
        Offer('transition', 'rejected', 'Rejected'),
        Offer('transition', 'closed', 'Close the application'),
    ),
    'no_response': (
        Offer('transition', 'responded', 'They responded'),
        Offer('transition', 'closed', 'Close the application'),
    ),
    'replied': (
        Offer('transition', 'interview', 'Interview arranged'),
        Offer('transition', 'rejected', 'Rejected'),
    ),
    'interview': (
        Offer('transition', 'offer', 'Offer received'),
        Offer('transition', 'rejected', 'Rejected'),
    ),
    'offer': (
        Offer('transition', 'closed', 'Close the application'),
        Offer('transition', 'rejected', 'Rejected'),
    ),
}
APPLICATION_CORRECT = ('waiting', 'follow_up', 'no_response', 'replied', 'interview', 'offer', 'closed')

_SENT = (Offer('outcome', 'sent', '', guard='label comes from the approach kind'),)

# Outreach card. Empty `kinds` means every approach kind. Handover is removed for an opportunity.
OUTREACH_OFFERS = {
    **{status: _SENT for status in PRE_CONTACT},
    'waiting': (
        Offer('outcome', 'replied', 'They replied'),
        Offer('outcome', 'followed_up', 'I followed up'),
        Offer('outcome', 'no_response', 'Park: no response'),
    ),
    'follow_up': (
        Offer('outcome', 'followed_up', 'I followed up'),
        Offer('outcome', 'replied', 'They replied'),
        Offer('outcome', 'no_response', 'Park: no response'),
    ),
    'no_response': (Offer('outcome', 'replied', 'They replied after all'),),
    'replied': (
        Offer('outcome', 'conversation', 'We spoke'),
        Offer('outcome', 'referral', 'They gave a referral', kinds=('ask', 'catch_up')),
        Offer('outcome', 'introduction', 'They offered an introduction', kinds=('ask', 'catch_up')),
        Offer('outcome', 'submitted', 'CV submitted', kinds=('desk', 'recruiter')),
        Offer('outcome', 'no_fit', 'Not a fit'),
        Offer('outcome', 'no_budget', 'No current budget'),
    ),
    'submitted': (
        Offer('outcome', 'conversation', 'Interview or call arranged'),
        Offer('outcome', 'no_fit', 'Not a fit'),
    ),
    'conversation': (
        Offer('handover', 'handover', 'Hand over to Work', guard='not an opportunity'),
        Offer('outcome', 'proposal', 'Proposal sent'),
        Offer('outcome', 'no_fit', 'Not a fit'),
        Offer('outcome', 'no_budget', 'No current budget'),
        Offer('outcome', 'permanent_only', 'Permanent only'),
    ),
    'proposal': (
        Offer('handover', 'handover', 'Hand over to Work', guard='not an opportunity'),
        Offer('outcome', 'won', 'Engagement won'),
        Offer('outcome', 'no_budget', 'No current budget'),
    ),
    'won': (Offer('handover', 'handover', 'Hand over to Work', guard='no earlier handover, and not an opportunity'),),
}

# Decision status. `supersede` retires a draft approval when the draft changes. Claim and block
# keep the status and move execution state in the decision service.
DECISION_MOVES = {
    ('open', 'answer'): DecisionMove('answered', 'owner'),
    ('answered', 'answer'): DecisionMove('answered', 'owner', 'until the agent has claimed the answer'),
    ('open', 'withdraw'): DecisionMove('withdrawn', 'agent'),
    ('answered', 'withdraw'): DecisionMove('withdrawn', 'agent'),
    ('answered', 'done'): DecisionMove('done', 'agent'),
    ('answered', 'reply'): DecisionMove('open', 'agent'),
    ('answered', 'claim'): DecisionMove('answered', 'agent', 'execution becomes working'),
    ('answered', 'block'): DecisionMove('answered', 'agent', 'only after a claim'),
    ('open', 'supersede'): DecisionMove('withdrawn', 'agent', 'the draft was replaced or removed'),
    ('answered', 'supersede'): DecisionMove('withdrawn', 'agent', 'the draft was replaced or removed'),
    ('done', 'supersede'): DecisionMove('withdrawn', 'agent', 'an approved draft was replaced or removed'),
}


def offers(approach_kind, status):
    """Offers for this progress status, already limited to the approach kind."""
    table = APPLICATION_OFFERS if approach_kind == 'application' else OUTREACH_OFFERS
    return tuple(offer for offer in table.get(status, ()) if not offer.kinds or approach_kind in offer.kinds)


def progress_after_contact(stage, *, person, elapsed):
    """Where a sent or followed-up approach sits. People never leave waiting on elapsed time."""
    if stage not in ('sent', 'followed_up'):
        raise MachineError(
            f"Cannot time a contact from stage {stage!r}. Allowed stages: sent, followed_up.")
    if person or not elapsed:
        return 'waiting'
    return 'follow_up' if stage == 'sent' else 'no_response'


def progress_section(status, kind):
    if status in SECTION_OF_STATUS:
        return SECTION_OF_STATUS[status]
    try:
        return KIND_SECTION[kind]
    except KeyError:
        raise MachineError(f"No section for progress {status!r} on a {kind} approach.") from None


def person_queue_key(progress):
    """People filter key. Labels and waiting-day counts stay in the browser."""
    progress = progress or {}
    step = progress.get('next_step') or {}
    text = step.get('text') if isinstance(step.get('text'), str) else ''
    if progress.get('reply_to_record'):
        return 'needs_reply'
    if progress.get('status') in PERSON_TERMINAL or step.get('kind') == 'done':
        return ''
    if step.get('kind') in ('mine', 'waiting') and text.strip():
        return 'next_step' if step['kind'] == 'mine' else 'waiting'
    if progress.get('status') in NEEDS_REPLY:
        return 'needs_reply'
    if progress.get('status') == 'waiting':
        return 'waiting'
    return ''


def _allowed_text(allowed):
    return ', '.join(allowed) if allowed else 'none'


def require_progress_event(approach_kind, status, event):
    """Refuse an outcome the named approach's card does not offer. Unknown statuses stay open:
    research and held approaches have no offer list, and a fact with no approach id is not checked."""
    if not status:
        return
    table = APPLICATION_OFFERS if approach_kind == 'application' else OUTREACH_OFFERS
    if status not in table:
        return
    allowed = [offer.event for offer in offers(approach_kind, status) if offer.method == 'outcome']
    if event not in allowed:
        raise MachineError(
            f"Cannot record outcome {event!r} on a {approach_kind} approach in {status!r}. "
            f"Allowed: {_allowed_text(allowed)}.")


def decision_move(status, action):
    """Return the decision status after `action`, or raise naming the current status and the allowed actions."""
    move = DECISION_MOVES.get((status, action))
    if move is None:
        allowed = [name for (current, name) in DECISION_MOVES if current == status]
        raise MachineError(
            f"Cannot apply {action!r} to a decision in {status!r}. Allowed: {_allowed_text(allowed)}.")
    return move.to
