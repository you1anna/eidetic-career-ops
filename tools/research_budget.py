"""Read/reconcile the retained pre-Codex cost ledger; the active worker makes no API calls."""
from __future__ import annotations
import json
import math
import uuid
import records

MODEL='gpt-5.4-mini-2026-03-17'
# Standard, non-regional pricing verified 17 October 2026. Cache discounts ignored conservatively.
INPUT_MICROUSD=0.75
OUTPUT_MICROUSD=4.5
SEARCH_MICROUSD=10_000
class BudgetStopped(RuntimeError): pass
class AccessBlocked(RuntimeError): pass


def spending(conn, at):
    rows=conn.execute("SELECT created_at,COALESCE(charged_microusd,reserved_microusd) cost FROM research_requests WHERE created_at>=?",(at[:7]+'-01',))
    day=month=reserved=0
    for r in rows:
        month+=r['cost']
        if r['created_at'][:10]==at[:10]: day+=r['cost']
    reserved=conn.execute("SELECT COALESCE(SUM(reserved_microusd),0) FROM research_requests WHERE status='reserved' AND created_at>=?",(at[:7]+'-01',)).fetchone()[0]
    return dict(day_usd=day/1e6,month_usd=month/1e6,reserved_usd=reserved/1e6)


def reserve(conn, run_id, amount, limits, at):
    if type(amount) is not int or amount<=0: raise ValueError('Invalid spend reservation')
    with records.transaction(conn):
        used=spending(conn,at)
        if used['day_usd']+amount/1e6>limits['daily_usd'] or used['month_usd']+amount/1e6>limits['monthly_usd']:
            raise BudgetStopped('Research budget reached')
        rid=uuid.uuid4().hex
        conn.execute('INSERT INTO research_requests(request_id,run_id,created_at,reserved_microusd) VALUES(?,?,?,?)',(rid,run_id,at,amount))
    return rid


def settle(conn,rid,usage,tools=0):
    if any(type(usage.get(k)) is not int or usage[k]<0 for k in ('input_tokens','output_tokens')) or type(tools) is not int or tools<0:
        raise ValueError('Missing model usage; reservation retained')
    cost=math.ceil(usage['input_tokens']*INPUT_MICROUSD+usage['output_tokens']*OUTPUT_MICROUSD+tools*SEARCH_MICROUSD)
    with records.transaction(conn):
        row=conn.execute('SELECT * FROM research_requests WHERE request_id=?',(rid,)).fetchone()
        if not row or row['status']!='reserved': raise ValueError('Request already settled or absent')
        over=cost>row['reserved_microusd']
        conn.execute('UPDATE research_requests SET charged_microusd=?,status=?,usage_json=? WHERE request_id=?',
                     (cost,'overrun' if over else 'settled',json.dumps(usage),rid))
    if over: raise BudgetStopped('Unexpected provider usage; further research stopped')
    return cost/1e6
