"""Bounded calls to the VPS's ChatGPT-authenticated Codex CLI; no API-key fallback."""
from __future__ import annotations
import json
import socket
import automation_usage as quota
from research_schemas import DISCOVERY,FINDING,PREPARATION,BRIEF

SOCKET='/run/wo-research-codex.sock'
class AccessBlocked(RuntimeError):pass
UsageLimited=quota.Limited

def exchange(request):
    raw=json.dumps(request,ensure_ascii=False).encode()+b'\n'
    if len(raw)>50_000:raise ValueError('Research request too large')
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(330);client.connect(SOCKET);client.sendall(raw);client.shutdown(socket.SHUT_WR)
            parts=[];size=0
            while block:=client.recv(65536):
                size+=len(block)
                if size>200_000:raise ValueError('Research result too large')
                parts.append(block)
    except (FileNotFoundError,ConnectionRefusedError,PermissionError):raise AccessBlocked('Codex research service unavailable') from None
    return json.loads(b''.join(parts))

OPERATIONS={'discovery':(DISCOVERY,True,''),'finding':(FINDING,False,''),
            'brief':(BRIEF,False,'')}

def call(conn,run_id,key,limits,at,prompt,schema,search=False,purpose=''):
    operation=next((name for name,spec in OPERATIONS.items() if spec==(schema,search,purpose)),None)
    if not operation:raise ValueError('Unknown research schema')
    # The worker persists usage counts in its run receipt; never estimate subscription usage in dollars.
    usage=limits.setdefault('_usage',{'calls':0,'completed':0,'input_tokens':0,'output_tokens':0})
    if usage['calls']>=min(limits.get('max_calls',6),6):raise UsageLimited('Research pass invocation cap reached')
    with quota.serial(conn):
        reservation=quota.reserve(conn,run_id,'research',at,purpose=purpose)
        usage['calls']+=1
        response=exchange({'operation':operation,'prompt':prompt})
        quota.finish(conn,reservation,response.get('usage'),False)
        if response.get('ok'):
            from research_codex import validate_output
            validate_output(response['result'],schema)
            quota.finish(conn,reservation,response.get('usage'),True)
    if not response.get('ok'):
        reason=response.get('reason')
        if reason=='access':raise AccessBlocked('Codex sign-in required')
        if reason=='usage_limit':raise UsageLimited('Codex usage limit reached')
        raise RuntimeError('Codex research '+str(reason))
    from research_codex import validate_output
    validate_output(response['result'],schema)
    usage['completed']+=1
    for name in ('input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens'):
        n=response.get('usage',{}).get(name,0)
        if type(n) is int and n>=0:usage[name]=usage.get(name,0)+n
    return response['result']
