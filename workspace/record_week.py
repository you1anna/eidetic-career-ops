"""This week's stored list: fixed on first visit, then changed only by "Not this week", "Put back",
"Refresh from your list" or an explicit include.
"""
from __future__ import annotations

import db
from record_core import Conflict, get, text, transaction
from workspace_state import cached_context


def store_week(conn, week, items, actor='agent'):
    """Fix the week's list once. Later calls for the same week change nothing."""
    with transaction(conn):
        if conn.execute('SELECT 1 FROM week_items WHERE week=? LIMIT 1',(week,)).fetchone():
            return False
        at=db.now()
        conn.executemany('INSERT INTO week_items(week,action_id,record_id,slot,position,state,reason,at,actor) VALUES(?,?,?,?,?,?,?,?,?)',
                         [(week,i['action_id'],i['record_id'],i['slot'],n,'planned',i.get('reason',''),at,actor) for n,i in enumerate(items)])
    return True


def defer_week_item(conn, week, action_id, reason, replacement, actor='owner'):
    """'Not this week': keep the deferred item visible and put the named replacement in its place."""
    with transaction(conn):
        row=conn.execute("SELECT * FROM week_items WHERE week=? AND action_id=? AND state='planned'",(week,action_id)).fetchone()
        if not row:
            raise ValueError("This item is not in this week's list")
        at=db.now()
        conn.execute("UPDATE week_items SET state='deferred',reason=?,at=?,actor=? WHERE week=? AND action_id=?",
                     (text(reason,240) or 'Not this week',at,actor,week,action_id))
        if replacement:
            if conn.execute('SELECT 1 FROM week_items WHERE week=? AND action_id=?',(week,replacement['action_id'])).fetchone():
                raise Conflict("The replacement is already in this week's list. Reload and try again.")
            name=conn.execute('SELECT name FROM records WHERE id=?',(row['record_id'],)).fetchone()
            conn.execute('INSERT INTO week_items(week,action_id,record_id,slot,position,state,reason,at,actor) VALUES(?,?,?,?,?,?,?,?,?)',
                         (week,replacement['action_id'],replacement['record_id'],replacement['slot'],row['position'],'planned',
                          f"Replaces {name['name'] if name else 'an earlier item'} ({text(reason,240) or 'not this week'})",at,actor))
    return True


def restore_week_item(conn, week, action_id, reason, keep, actor='owner'):
    """Undo 'Not this week': the item returns to its place, and the replacement the deferral put there
    (same position, written at the same moment) leaves unless it is in `keep` (he has acted on it). After a
    refresh or a later deferral at that place the pair is no longer known, so nothing is removed."""
    with transaction(conn):
        row=conn.execute("SELECT * FROM week_items WHERE week=? AND action_id=? AND state='deferred'",(week,action_id)).fetchone()
        if not row:
            raise ValueError('This item is not set aside this week')
        swap=conn.execute("SELECT action_id FROM week_items WHERE week=? AND state='planned' AND position=? AND at=? AND action_id<>?",
                          (week,row['position'],row['at'],action_id)).fetchone()
        removed=swap['action_id'] if swap and swap['action_id'] not in keep else None
        if removed:
            conn.execute('DELETE FROM week_items WHERE week=? AND action_id=?',(week,removed))
        conn.execute("UPDATE week_items SET state='planned',reason=?,at=?,actor=? WHERE week=? AND action_id=?",
                     (text(reason,240),db.now(),actor,week,action_id))
    return removed


def week_restore(conn, payload, actor='owner'):
    """Put back an item set aside this week ("Put back", or Undo on the notice), while it still qualifies."""
    import approaches
    action_id = text(payload.get('action_id'), 240)
    view = cached_context(conn)
    intel = view['intelligence']
    week = intel.get('week') or {}
    entry = next((i for i in week.get('deferred') or [] if i['action_id'] == action_id), None)
    if not entry:
        raise ValueError('This item is not set aside this week')
    if not entry.get('actions'):
        raise ValueError('This item no longer qualifies, so it cannot come back this week')
    acted = {i['action_id'] for i in week['items'] if i.get('done')}
    removed = restore_week_item(conn, week['week'], action_id, approaches.reason_for(intel, view, action_id, entry['slot']),
                                acted, actor)
    return {'saved': True, 'removed': removed}


def include_in_week(conn, record_id, actor='agent'):
    """Put one ready person at the top of the stored week (moving them up if already saved lower).
    Everyone else stays on it."""
    import approaches
    view = cached_context(conn)
    week = (view.get('intelligence') or {}).get('week') or {}
    if not week:
        raise ValueError("This week's list is unavailable; reload and try again")
    if not week.get('stored'):
        store_week(conn, week['week'], week['items'], actor)
        view = cached_context(conn)
        week = view['intelligence']['week']
    person = get(conn, record_id)
    if person['type'] != 'person':
        raise ValueError('Choose a person')
    actions = [a for a in view['intelligence']['actions']
               if record_id in (a.get('person_id'), a.get('record_id'))
               and a.get('kind') in ('ask', 'catch_up', 'recruiter')
               and (a.get('progress') or {}).get('status') in ('ready', 'follow_up')]
    actions.sort(key=lambda a: a.get('kind') != 'catch_up')
    if not actions:
        raise ValueError('This person has no approach ready to contact')
    action = actions[0]
    existing = conn.execute('SELECT state, position FROM week_items WHERE week=? AND action_id=?',
                            (week['week'], action['id'])).fetchone()
    top = conn.execute('SELECT MIN(position) FROM week_items WHERE week=?', (week['week'],)).fetchone()[0]
    if existing and existing['state'] == 'planned':
        # Asking for someone already saved lower down moves them to the top of the week.
        if existing['position'] <= top:
            return {'saved': False, 'week': week['week'], 'id': record_id, 'action_id': action['id']}
        with transaction(conn):
            conn.execute('UPDATE week_items SET position=?, at=?, actor=? WHERE week=? AND action_id=?',
                         (top - 1, db.now(), actor, week['week'], action['id']))
        return {'saved': True, 'moved': True, 'week': week['week'], 'id': record_id, 'action_id': action['id']}
    aside = next((i for i in week['deferred'] if i['record_id'] == record_id), None)
    if aside:
        # Set aside with "Not this week": put that item back rather than adding a second one for them.
        week_restore(conn, {'action_id': aside['action_id']}, actor)
        return {'saved': True, 'week': week['week'], 'id': record_id, 'action_id': aside['action_id'], 'restored': True}
    with transaction(conn):
        # An explicit request goes to the top of the week, ahead of the automatic choice.
        position = (top if top is not None else 1) - 1
        conn.execute('INSERT INTO week_items(week,action_id,record_id,slot,position,state,reason,at,actor) '
                     'VALUES(?,?,?,?,?,?,?,?,?)',
                     (week['week'], action['id'], record_id, 'known', position, 'planned',
                      'Your list · contact first', db.now(), actor))
    return {'saved': True, 'week': week['week'], 'id': record_id, 'action_id': action['id']}


def week_store(conn, payload=None, actor='owner'):
    """Fix this week's list as currently proposed. Idempotent: an existing list is kept, unless
    `refresh` asks for it to be chosen again from his list (items acted on and deferred ones stay)."""
    view = cached_context(conn)
    week = (view['intelligence'] or {}).get('week')
    if not week:
        raise ValueError("This week's list is unavailable; reload and try again")
    if (payload or {}).get('refresh'):
        import approaches
        chosen = approaches.refreshed_week(view['intelligence'], view)
        with transaction(conn):
            at = db.now()
            conn.execute("DELETE FROM week_items WHERE week=? AND state='planned'", (week['week'],))
            # A set-aside item he acted on anyway is kept as done, so its deferred row gives way to it.
            conn.executemany('DELETE FROM week_items WHERE week=? AND action_id=?', [(week['week'], c['action_id']) for c in chosen])
            conn.executemany('INSERT INTO week_items(week,action_id,record_id,slot,position,state,reason,at,actor) VALUES(?,?,?,?,?,?,?,?,?)',
                             [(week['week'], c['action_id'], c['record_id'], c['slot'], n, 'planned', c['reason'], at, actor)
                              for n, c in enumerate(chosen)])
        return {'saved': True, 'week': week['week']}
    if week['stored']:
        return {'saved': False, 'week': week['week']}
    return {'saved': store_week(conn, week['week'], week['items'], actor), 'week': week['week']}


def week_defer(conn, payload, actor='owner'):
    """'Not this week' (or replacing an item that no longer qualifies): the next eligible approach
    for the same slot takes its place, and the reason stays visible."""
    import approaches
    action_id = text(payload.get('action_id'), 240)
    week_store(conn, actor=actor)
    view = cached_context(conn)
    intel = view['intelligence']
    week = intel['week']
    row = next((i for i in week['items'] if i['action_id'] == action_id and not i.get('done')), None)
    if not row:
        raise ValueError("This item is not in this week's list")
    chosen = {i['action_id'] for i in week['items']} | {i['action_id'] for i in week['deferred']}
    swap = approaches.replacement(intel, row['slot'], chosen, state=view)
    reason = text(payload.get('reason', ''), 240) or ('Not this week' if row['state'] == 'planned' else 'No longer suitable')
    defer_week_item(conn, week['week'], action_id, reason, swap, actor)
    return {'saved': True, 'replacement': swap}
