"""One focused-maintenance boundary for the worker and visible per-role disposition."""


def latest_attempts(conn):
    """One bounded result per target; never decode the receipt archive in Python."""
    rows = conn.execute("""
        WITH attempts AS (
          SELECT json_extract(item.value,'$.id') AS record_id,
                 json_extract(item.value,'$.attempted_at') AS attempted_at,
                 json_extract(item.value,'$.outcome') AS outcome,
                 json_extract(item.value,'$.reason') AS reason,
                 json_extract(item.value,'$.context_sha') AS context_sha,
                 json_extract(item.value,'$.brief') AS brief,
                 json_extract(item.value,'$.source_checked_at') AS source_checked_at,
                 MAX(CASE WHEN json_extract(item.value,'$.outcome') NOT IN ('usage_limited','access_blocked','collection_deferred')
                          THEN json_extract(item.value,'$.attempted_at') END) OVER (
                   PARTITION BY json_extract(item.value,'$.id')
                 ) AS rotation_at,
                 ROW_NUMBER() OVER (
                   PARTITION BY json_extract(item.value,'$.id')
                   ORDER BY json_extract(item.value,'$.attempted_at') DESC, runs.run_id DESC
                 ) AS position
          FROM scan_runs AS runs, json_each(runs.receipt_json,'$.items') AS item
          WHERE runs.run_id >= 'research:' AND runs.run_id < 'research;'
            AND json_valid(runs.receipt_json)
            AND json_extract(item.value,'$.attempted_at') IS NOT NULL
        )
        SELECT record_id, attempted_at, outcome, reason, context_sha, brief, source_checked_at, rotation_at FROM attempts
        WHERE position=1 AND record_id IS NOT NULL
    """)
    return {r['record_id']: {k: r[k] or '' for k in ('attempted_at', 'outcome', 'reason', 'context_sha', 'brief', 'source_checked_at', 'rotation_at')} for r in rows}

def attach(conn,state,attempts=None):
    selected=set((state.get('opportunities') or {}).get('research_ids', []))
    queued=set((state.get('opportunities') or {}).get('research_pool', []))
    inactive={ref['record_id'] for group in ('closed','other')
              for ref in (state.get('opportunities') or {}).get(group, [])
              if ref.get('rule') not in ('qualify.advert', 'qualify.lead')}
    attempts=latest_attempts(conn) if attempts is None else attempts
    state['research_attempts']=attempts
    for r in state['records']:
        if r['type'] not in ('job','tender','opportunity','company','agency'):continue
        if state.get('intelligence_error'):
            label='Research scope unavailable';scope='unknown'
        elif r.get('exclusion') or r.get('status') in ('skip','closed','rejected'):
            label='Outside scheduled research: excluded or closed';scope='excluded'
        elif r['id'] in selected:
            label='Included in opportunity research; runs are subject to available allowance';scope='maintained'
        elif r['id'] in queued:
            label='Queued for opportunity research; candidates rotate within the available allowance';scope='outside_focus'
        elif r['id'] in inactive:
            label='Outside scheduled research: current candidate evidence does not qualify. Record an active process if one exists.';scope='ineligible'
        else:
            label='Outside the active work and top opportunity research set. Shortlist or record an existing process to include it.';scope='outside_focus'
        attempt=attempts.get(r['id'])
        r['research_disposition']=dict(scope=scope,label=label,last_attempt={k:attempt.get(k,'') for k in ('attempted_at','outcome','reason')} if attempt else None)
    plan = decision_plan(state)
    state['research_plan'] = plan[:6]
    byid={r['id']:r for r in state['records']}
    for item in plan:
        if item['kind'] == 'your_choice':
            byid[item['record_id']]['research_disposition'].update(scope='needs_input',
                label='Research waiting for your evidence or choice; public sources cannot settle this gap.')
    for item in state.get('intelligence',{}).get('actions',[])+state.get('intelligence',{}).get('research',[]):
        r=byid.get(item.get('record_id'),{})
        if item.get('progress',{}).get('status')=='research' and r.get('research_disposition'):
            item['progress']['next']=r['research_disposition']['label']


def decision_plan(state, records=None):
    """One decision-led order for the worker and Dashboard; no new admission or saved queue.

    Keep chosen/discovery alternation. Within each lane resolve work decisions before routine
    maintenance, then rotate by substantive attempts. Missing assessment is not a new blocker.
    """
    projection = state.get('opportunities') or {}
    by_id = {r['id']: r for r in (state.get('records', []) if records is None else records)}
    chosen = {r['record_id'] for group in ('active', 'shortlisted') for r in projection.get(group, [])}
    attempts = state.get('research_attempts') or {}
    lanes = ([], [])
    for position, rid in enumerate(projection.get('research_ids', [])):
        record = by_id.get(rid)
        if not record or record.get('type') == 'person' or record.get('exclusion'):
            continue
        gaps = [g for g in (record.get('evidence_brief') or {}).get('gaps', []) if g.get('blocking')]
        public = [g for g in gaps if g.get('owner') == 'research']
        personal = [g for g in gaps if g.get('owner') == 'owner']
        fit = (record.get('relevance') or {}).get('fit') or {}
        process = record.get('status') in ('applied', 'responded', 'interview', 'offer')
        question = public[0]['text'] if public else ''
        if not question and fit.get('status') == 'unconfirmed' and not fit.get('gaps') and not process:
            question = ('Which duties and essential requirements does the exact advert establish?' if record.get('type') in ('job','tender')
                        else 'What current, specific work does this organisation need help with?')
        if not public and fit.get('gaps') and not process:
            if any(requirement.get('status') == 'ambiguous' for requirement in fit.get('requirements', [])):
                question = 'Clarify the conflicting essential requirements in the exact advert.'
            else:
                personal = [dict(text=fit['gaps'][0])]
        if personal and not public:
            kind, priority = 'your_choice', 2
            question = personal[0]['text']
            consequence = 'Your evidence or preference is needed; public research cannot establish this for you.'
        elif question and not process:
            kind, priority = 'decision', 0
            consequence = ('If the evidence supports the role and your fit, review an application; ' if record.get('type') == 'job'
                           else 'If the evidence establishes relevant work, review whether to pursue it; ')
            consequence += 'if it rules the work out, Skip releases its research attention. Unresolved facts stay unknown.'
        elif question:
            kind, priority = 'process', 1
            consequence = 'Resolve this for the recorded hiring process; an unreadable advert does not end that process.'
        else:
            kind, priority = 'maintenance', 2
            question = 'Keep recorded vacancy and contact evidence current.'
            consequence = 'No application-blocking public question is recorded; routine checking does not establish suitability.'
        attempt = attempts.get(rid) or {}
        at = attempt.get('rotation_at', '' if attempt.get('outcome') in ('usage_limited', 'access_blocked', 'collection_deferred')
                         else attempt.get('attempted_at', ''))
        entry = dict(record_id=rid, kind=kind, question=question, consequence=consequence,
                     lane='chosen' if rid in chosen else 'discovery', blocking=len(gaps))
        questions = len(public) + int(fit.get('status') == 'unconfirmed' and not any(g.get('code') == 'remit' for g in public))
        lanes[0 if rid in chosen else 1].append(((priority, questions if priority == 0 else 0, at, position), entry))
    for lane in lanes:
        lane.sort(key=lambda item: item[0])
    return [lane[index][1] for index in range(max(map(len, lanes), default=0))
            for lane in lanes if index < len(lane)]
