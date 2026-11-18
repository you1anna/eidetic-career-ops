"""Canonical record service shared by the browser, CLI and attended importers.

Every human mutation takes an integer revision inside one immediate transaction. Source
observations and human decisions remain separate. No function sends or schedules anything.

This module is the service's one entry point: callers use `records.<name>` (or `import records as
store`). The implementation lives in cohesive modules, each importing only from those above it:

    record_read       shared read snapshots and exclusion policy
    decision_views    read-only decision projections and batched history
    incoming_views    read-only incoming-process evidence
    record_views      batched record enrichment and timelines
    record_core       transactions, revision checks, validation, history events, observations
    record_identity   shared source upserts, organisation identity and links
    record_mutations  create, edit, transitions, working state, interactions, links, archive, jobs
    record_evidence   reviewed research findings and merges
    record_decisions  the owner's decisions, card questions and the drafts approvals bind to
    record_review     the LinkedIn / list review queue
    source_coverage   source health, channel health and the freshness line
    workspace_state   state projections and operational context caches
    state_export      complete-history JSON and private full-response file cache
    record_outcomes   reported outcomes, corrections and handover into Work
    record_week       This week's stored list
    record_export     the flat CSV export
    incoming          attended incoming-role commands and private research context

Patch a name where it is defined (for example `workspace_state.state`), not here: the modules
call each other directly, not through this facade.
"""
from __future__ import annotations

# Re-exported for callers that reach shared modules through the service (`records.db.now()`).
import db  # noqa: F401
import normalise  # noqa: F401
import record_views  # noqa: F401
import relevance  # noqa: F401
import search_config  # noqa: F401
import states  # noqa: F401

from record_views import LIVE_INTERACTIONS, outcome_view  # noqa: F401
from record_core import (  # noqa: F401
    WORKING, TYPES, EDITABLE, STATE_FIELDS, PERSON_CHOICES, RELATIONS, WRITE_LOCK_LOG_SECONDS,
    Conflict, transaction, text, safe_url, checked, event, mark_edited, bump, get, record_transitions,
    _values, _columns, add_observation,
)
from record_identity import (  # noqa: F401
    TEXT_COLUMNS, STATE_COLUMNS, IMPORT_INSERT_ONLY, upsert_record, ensure_organisation, link_organisation,
)
from record_mutations import (  # noqa: F401
    match_organisation, create, job_url_index, active_job_survivor, match_job_url, save_job, edit,
    transition, followup, interaction, link, normalise_record, archive, retype, REGISTRY, COMPANY_NUMBER,
    company_number,
)
from record_evidence import (  # noqa: F401
    evidence_links, research_observation, merge, merged_evidence_gaps, restore_merged_evidence,
)
from record_decisions import (  # noqa: F401
    DECISION_WAITING, decision_marker, decision_choices, decision_view, get_decision, create_decision,
    answer_decision, process_decision, decisions_history, PROGRESS_STAGES, note_decision, QUESTION_DAYS,
    ask_question, answer_question, get_question, questions, decisions_state, DRAFT_PURPOSES, draft,
    clear_draft,
)
from record_review import (  # noqa: F401
    person_matches, review_actions, canonical_profile, queue_linkedin_person, linkedin_card,
    advance_last_touch, import_linkedin, resolve_review,
)
from source_coverage import freshness, FAILURE_TEXT, channel_health, describe_source, coverage_limit  # noqa: F401
from workspace_state import (  # noqa: F401
    ROOT, STATE_BUILD_LOG_SECONDS, state, _STATE_CACHE, _JSON_CACHE, cached_state, cached_summary_json, summary_json,
    configured_board_scopes, state_context, cached_context,
)
from state_export import state_json_file, cached_state_file, print_state  # noqa: F401
from record_outcomes import (  # noqa: F401
    OUTCOME_REPEAT_SECONDS, OUTCOME_CHANNELS, outcome, correct, handover, next_step,
)
from record_week import (  # noqa: F401
    store_week, defer_week_item, restore_week_item, week_restore, include_in_week, week_store, week_defer,
)
from record_export import csv_rows, write_csv  # noqa: F401


def preview_incoming(conn, payload):
    import incoming
    return incoming.preview(conn,payload)


def save_incoming(conn, payload, actor='owner'):
    import incoming
    return incoming.save(conn,payload,actor)
