"""Declared state vocabularies for career records, and the only writer of `state_events`.

Every enum and every allowed transition is a literal at the top of this module, in the shape of
career-ops `templates/states.yml`: one place to read what a value may be. Nothing infers a state.
A follow-up reaching `done` is a task state and never touches `stage` or `status`.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

# --- vocabularies -----------------------------------------------------------------------------

ENUMS: dict[str, dict[str, tuple[str, ...]]] = {
    "person": {
        "kind": ("recruiter", "in_house_talent", "hiring_manager", "peer", "former_colleague", "former_manager",
                 "friend", "founder", "introducer", "other"),
        "relationship": ("none", "cold", "warm", "active", "dormant", "do_not_contact"),
        # The owner's list (migration 012): how he reaches someone, what he asks, and his own order.
        # Plain fields set with records.edit, not transitions; blank means not stated.
        "preferred_channel": ("call", "whatsapp", "text", "email", "linkedin", "in_person"),
        "preferred_ask": ("catch_up", "introduction", "availability"),
        "priority": ("first", "next"),
    },
    # Companies carry no status; their state is their opportunities and jobs.
    "org": {
        "status": ("not_contacted", "called_no_answer", "called_brief_received", "on_books",
                   "no_relevant_desk", "declined", "joined"),
        "kind": ("company", "agency", "community", "network"),
    },
    "opportunity": {
        "stage": ("lead", "contacted", "conversation", "proposal", "won", "lost"),
        "owner": ("owner", "agent"),
    },
    "job": {
        "status": ("candidate", "pursue", "skip", "rejected", "applied", "responded",
                   "interview", "offer", "closed"),
    },
    "interaction": {
        "channel": ("linkedin", "email", "call", "whatsapp", "text", "meeting", "event", "form", "other"),
        "direction": ("out", "in", "note"),
    },
    "followup": {
        "status": ("needs_review", "ready", "in_progress", "waiting", "blocked", "paused", "done"),
        "owner": ("owner", "agent"),
    },
    "record": {
        "verification": ("primary-read", "search-verified", "certain", "likely", "unconfirmed", "guessing"),
    },
}

# Decisions the owner answers on the site (archive/plans/2026-09-25-decisions-signals-consultancies.md, D6). Each
# kind is a fixed set of choices: (id, button label, what the agent does next, note prompt). A note
# prompt makes the note required for that choice. `order` puts kinds in front of the owner by urgency.
DECISION_KINDS: dict[str, dict] = {
    "draft": {"label": "Approve a draft", "order": 0, "choices": (
        ("approve", "Approve as is", "This exact note becomes ready to copy. You choose where and when to send it.", ""),
        ("edit", "Approve with edits", "Claude revises the draft with your edits and brings it back for approval.", "The edits you want"),
        ("hold", "Hold", "Parked with the draft kept. Claude raises it again only if the role changes.", ""),
        ("drop", "Drop", "Closed. The draft is kept for the record but not used.", ""))},
    "approve": {"label": "Approve a step", "order": 1, "choices": (
        ("approve", "Approve", "Claude carries out the approved step and reports back here.", ""),
        ("not_yet", "Not yet", "Claude works on what you name, then asks again.", "What is missing"))},
    "join": {"label": "Join or register", "order": 2, "choices": (
        ("registered", "I've registered", "Claude records the channel as yours and starts using it.", ""),
        ("help", "Help me register", "Claude prepares the profile text, rates and links for you to use.", ""),
        ("later", "Not now", "Parked. Claude raises it again only if something changes.", ""),
        ("no", "No", "Closed and ranked down. Claude will not raise it again.", ""))},
    # Raised by the daily ingest and the nightly backup (tools/failure_tasks.py), withdrawn when it works again.
    "failure": {"label": "Something failed", "order": 3, "choices": (
        ("fix", "Fix it", "Claude finds the cause and fixes it; for a blocked source that means finding another route "
                          "(an API, feed or email alert). Claude reports back here.", ""),
        ("wait", "Leave it for now", "Nothing changes. It is not raised again until it has worked once in between.", ""),
        ("drop", "Stop this source", "Claude turns the source off in the configuration. Records it already found stay.", ""))},
}
# A record with a live decision waits on it (records.py sets this; relevance.py reads it).
DECISION_WAITING = "The owner's answer"
# Every decision also offers a question back to the agent; it stays open until the owner chooses.
ASK_CHOICE = ("ask", "Ask Claude…", "Claude answers your question here and the decision stays open for your choice.", "Your question")
DECISION_STATUS = ("open", "answered", "done", "withdrawn")
# What a decision is about, in the order "Waiting on you" shows them: the site and its data (a
# workspace:<slug> subject, or a source) first, because those answers enable the rest (1 October),
# then the owner's job search (a person, company, role, draft or platform).
DECISION_AREAS = {"operations": "The site and its data", "search": "Your job search"}
OPERATIONS_RECORD_TYPES = ("source",)

OUTCOME_KINDS = {
    'sent': 'Sent', 'followed_up': 'Followed up', 'replied': 'Replied', 'introduction': 'Introduction offered',
    'referral': 'Referral given', 'submitted': 'CV submitted',
    'conversation': 'Relevant conversation', 'proposal': 'Proposal', 'won': 'Engagement won',
    'no_fit': 'No fit', 'no_budget': 'No current budget', 'permanent_only': 'Permanent only',
    'no_response': 'No response', 'handed_over': 'Handed over to Work',
}
# Kinds a record type can report. A referral comes from a person; a CV goes to a desk, a role or a
# consultant. Handover is recorded only by the handover step, which creates the opportunity.
OUTCOME_SCOPE = {'referral': ('person',), 'submitted': ('person', 'agency', 'job')}
OUTCOME_INTERNAL = ('handed_over',)
OUTCOME_ACTION_LABELS = {'sent': 'I sent this', 'followed_up': 'I followed up', 'replied': 'They replied'}


def outcome_actions(record):
    typ = record.get('type')
    if typ not in ('person','company','job','opportunity','agency') or record.get('archived_at'):
        return []
    return [dict(kind=kind,label=OUTCOME_ACTION_LABELS.get(kind,label),
                 **({'asks': 'company'} if kind == 'submitted' else {}))
            for kind,label in OUTCOME_KINDS.items()
            if kind not in OUTCOME_INTERNAL and typ in OUTCOME_SCOPE.get(kind, (typ,))]

# Transitions a write may make. A value absent from the map may only be set by import.
TRANSITIONS: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {
    ("opportunity", "stage"): {
        "lead": ("contacted", "lost"),
        "contacted": ("conversation", "lost"),
        "conversation": ("proposal", "lost"),
        "proposal": ("won", "lost"),
        "won": (),
        "lost": ("lead",),
    },
    ("org", "status"): {
        "not_contacted": ("called_no_answer", "called_brief_received", "no_relevant_desk", "declined", "joined"),
        "called_no_answer": ("called_no_answer", "called_brief_received", "no_relevant_desk", "declined", "joined"),
        "called_brief_received": ("on_books", "no_relevant_desk", "declined", "joined"),
        "on_books": ("no_relevant_desk", "declined"),
        "no_relevant_desk": ("not_contacted", "called_no_answer"),
        "declined": ("not_contacted",),
        "joined": ("declined",),
    },
    ("job", "status"): {
        "candidate": ("pursue", "applied", "skip"),
        "pursue": ("applied", "skip", "closed"),
        "skip": ("candidate", "pursue"),
        "applied": ("rejected", "responded", "closed"),
        "responded": ("interview", "rejected", "closed"),
        "interview": ("offer", "rejected", "closed"),
        "offer": ("closed", "rejected"),
        "rejected": (),
        "closed": (),
    },
    ("person", "relationship"): {
        "none": ("cold", "warm", "active", "dormant", "do_not_contact"),
        "cold": ("warm", "active", "dormant", "do_not_contact"),
        "warm": ("active", "dormant", "do_not_contact"),
        "active": ("warm", "dormant", "do_not_contact"),
        "dormant": ("cold", "warm", "active", "do_not_contact"),
        "do_not_contact": ("none", "cold", "warm", "active", "dormant"),
    },
}

# The entity a record id belongs to, for validation. Companies and agencies are both `org`.
TYPE_ENTITY = {"person": "person", "company": "org", "agency": "org", "opportunity": "opportunity",
               "job": "job", "event": "record", "source": "record"}

# Fields a transition may write, and where they live on `records`.
STATE_FIELDS = ("stage", "status", "relationship", "kind", "sub_state", "owner", "blocked_on")


class StateError(ValueError):
    """A value or a move that the declared model does not allow."""


def validate(entity_type: str, field: str, value: str | None) -> str | None:
    """Return the value when the model declares it, else raise. NULL is always allowed: an
    unknown state is a fact, not a default."""
    if value is None or value == "":
        return None
    allowed = ENUMS.get(entity_type, {}).get(field)
    if allowed is None:
        raise StateError(f"No declared vocabulary for {entity_type}.{field}")
    if value not in allowed:
        raise StateError(f"{value!r} is not a {entity_type}.{field}; allowed: {', '.join(allowed)}")
    return value


def allowed_from(entity_type: str, field: str, current: str | None) -> tuple[str, ...]:
    table = TRANSITIONS.get((entity_type, field))
    if table is None:
        return ENUMS.get(entity_type, {}).get(field, ())
    if current in (None, ""):
        return tuple(table)
    return table.get(current, ())


def transition_refusal(entity_type: str, field: str, current: str | None, to: str,
                       allowed: tuple[str, ...] | list[str]) -> str:
    """Name the current state, the refused move and the moves that are allowed."""
    shown = "unset" if current in (None, "") else str(current)
    choices = ", ".join(allowed) if allowed else "none"
    return f"Cannot transition {entity_type}.{field} from {shown!r} to {to!r}. Allowed: {choices}."


def transition(conn: sqlite3.Connection, record_id: str, field: str, to: str,
               actor: str = "agent", note: str = "") -> dict:
    """Move one state field, recording what moved. Raises before any write when the move is not
    declared. Never touches a follow-up."""
    # Compatibility for older Python callers; all writes use the revision-safe service.
    import records
    row = records.get(conn, record_id)
    try:
        result = records.transition(conn, record_id, dict(field=field, to=to, note=note, revision=row['revision']), actor=actor)
    except ValueError as error:
        raise StateError(str(error)) from error
    return dict(id=record_id, field=field, **{'from':row.get(field), 'to':result['record'].get(field)})
