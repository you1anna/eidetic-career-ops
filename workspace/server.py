#!/usr/bin/env python3
"""Private loopback SQLite workspace: the page, `/api/state` and the mutation routes."""
import argparse
import gzip
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import db as wodb
import records as store
import dashboard_views
import record_views
import request_timings
import email_leads
import intake_review
import search_config

HERE = Path(__file__).resolve().parent
# The only files served: the page, its stylesheet and icon, and every browser module in js/ (the folder
# holds nothing else and is allowlisted whole), so a new module needs no entry here.
ASSETS = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/style.css': ('style.css', 'text/css'), '/icon.svg': ('icon.svg', 'image/svg+xml'),
          **{f'/js/{p.name}': (f'js/{p.name}', 'text/javascript') for p in sorted((HERE / 'js').glob('*.js'))}}
# Specific explanatory assets only: never expose the docs directory or arbitrary repository paths.
ASSETS.update({
    '/system-map': ('../docs/system-map.html', 'text/html'),
    '/system-map/system-map.css': ('../docs/system-map.css', 'text/css'),
    **{f'/system-map/{name}.{ext}': (f'../docs/{name}.{ext}', kind)
       for name in ('system-map', 'email-processing', 'research-and-freshness', 'software-lifecycle')
       for ext, kind in (('svg', 'image/svg+xml'), ('png', 'image/png'))},
})

# Public pitch: exact files only, with absolute namespaced URLs in the HTML.
CAREER_OPS_ASSETS = {
    '/career-ops': ('career-ops/index.html', 'text/html'),
    '/career-ops/': ('career-ops/index.html', 'text/html'),
    **{f'/career-ops/{name}': (f'career-ops/{name}', kind) for name, kind in (
        ('style.css', 'text/css'), ('journey.js', 'text/javascript'),
        ('icon.svg', 'image/svg+xml'), ('career-ops-discussion-brief.txt', 'text/plain'))},
}
ASSETS.update(CAREER_OPS_ASSETS)

REFRESH_PURPOSES = {'save': 'refresh_after_save', 'background': 'refresh_background'}


def timing_kind(method, path, status, purpose=None):
    """Which responsiveness figure a reply counts toward: saves and page summaries; None otherwise.
    A summary read says why it was made (X-WO-Refresh); an unknown or missing reason is "other"."""
    if status >= 500:
        return None
    if method == 'GET' and path.startswith('/api/state?view=summary'):
        if purpose in REFRESH_PURPOSES:
            return REFRESH_PURPOSES[purpose]
        return 'refresh_other' if '&since=' in path else 'summary_full'
    if method in ('POST', 'PATCH') and path.startswith('/api/') and not path.startswith('/api/seen/'):
        return 'save'
    return None


def load_state_db(conn, compact=False):
    return store.cached_state(conn, compact=compact)

def save_followup_db(conn, payload):
    return store.followup(conn, payload)

def parse_public_origin(value):
    parts=urlsplit(value)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.path or parts.query or parts.fragment:raise ValueError('Public origin must be a bare https origin, e.g. https://work.example.com')
    return f'https://{parts.netloc}'

class WorkspaceHTTPServer(ThreadingHTTPServer):
    # Parallel module loads and overlapping navigations can overrun the stdlib's five-slot
    # listen queue, resetting sockets before a handler starts. This is a bounded pending
    # connection queue, not a change to database locking or the state-cache build guard.
    request_queue_size = 32


def accepts_gzip(value):
    """An explicit gzip preference overrides '*'; malformed quality values opt out safely."""
    qualities={}
    for part in value.lower().split(','):
        name,*parameters=part.strip().split(';')
        if name.strip() not in ('gzip','*'):
            continue
        quality=1.0
        for parameter in parameters:
            key,_,raw=parameter.strip().partition('=')
            if key.strip()=='q':
                try: quality=float(raw.strip())
                except ValueError: quality=0
                if not 0<=quality<=1: quality=0
        name=name.strip()
        qualities[name]=min(qualities.get(name,1),quality)
    return qualities.get('gzip',qualities.get('*',0))>0


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
    def parse_request(self):
        self.started=time.monotonic()
        return super().parse_request()
    def reply(self,status,body,kind='application/json',headers=None,gzipped=None):
        if kind=='application/json' and not isinstance(body,bytes):body=json.dumps(body).encode()
        headers=dict(headers or {})
        compressible=kind in ('application/json','text/html','text/javascript','application/javascript','text/css')
        if compressible and len(body)>1024 and not any(k.lower()=='content-encoding' for k in headers):
            vary=next((k for k in headers if k.lower()=='vary'),'Vary')
            values=[v.strip() for v in headers.get(vary,'').split(',') if v.strip()]
            if not any(v.lower() in ('*','accept-encoding') for v in values):values.append('Accept-Encoding')
            headers[vary]=', '.join(values)
            if accepts_gzip(self.headers.get('Accept-Encoding','')):
                compressed=gzipped if gzipped is not None else gzip.compress(body,compresslevel=5,mtime=0)
                if len(compressed)<len(body):
                    body=compressed
                    headers['Content-Encoding']='gzip'
        self.reply_headers(status,len(body),kind,headers)
        self.wfile.write(body)
        # One line per save, refusal, page load or section view, never the request or response content;
        # other successful reads stay quiet. A page load names only the device class.
        page=self.command=='GET' and self.path.split('?')[0]=='/'
        elapsed=(time.monotonic()-getattr(self,'started',time.monotonic()))*1000
        # The Dashboard's responsiveness figures: kind and duration only, in this process's memory.
        kind=timing_kind(self.command,self.path,status,self.headers.get('X-WO-Refresh'))
        if kind:request_timings.record(kind,elapsed)
        if self.command!='GET' or status>=400 or page:
            ms=round(elapsed)
            ua=self.headers.get('User-Agent','')
            device=(' iphone' if 'iPhone' in ua else ' desktop' if re.search('Macintosh|Windows|X11',ua) else ' other') if page else ''
            logging.info('%s %s %d %dms%s',self.command,self.path.split('?')[0][:200],status,ms,device)
    def reply_headers(self,status,length,kind='application/json',headers=None):
        self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cross-Origin-Resource-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' https://media.licdn.com; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Referrer-Policy','same-origin')
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.end_headers()
    def reply_full_state(self,conn):
        with store.cached_state_file(conn,compress=accepts_gzip(self.headers.get('Accept-Encoding',''))) as stream:
            headers={'Vary':'Accept-Encoding'} if stream.varies else {}
            if stream.encoding:headers['Content-Encoding']=stream.encoding
            self.reply_headers(200,stream.length,headers=headers)
            while chunk:=stream.read(64*1024):
                self.wfile.write(chunk)
    def with_db(self,fn,*args):
        if not self.server.db.is_file(): raise ValueError('Database is missing; restore it before starting')
        conn=wodb.connect(self.server.db)
        try:return fn(conn,*args)
        finally:conn.close()
    def allowed(self):
        hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'};origins={f'http://{host}' for host in hosts}
        public=getattr(self.server,'public_origin',None)
        if public:hosts.add(urlsplit(public).netloc);origins.add(public)
        return hosts,origins
    def host_ok(self):
        return self.headers.get('Host','').lower() in self.allowed()[0]
    def do_GET(self):
        if not self.host_ok():return self.reply(403,{'error':'Use the configured workspace address'})
        try:
            pitch_path = self.path.split('?', 1)[0]
            if pitch_path in CAREER_OPS_ASSETS:
                name, kind = CAREER_OPS_ASSETS[pitch_path]
                return self.reply(200, (HERE / name).read_bytes(), kind)
            if self.path=='/api/health':return self.reply(200,{'service':'work-ops','status':'ok','pid':os.getpid(),'mode':'database','database':str(self.server.db),'revision':os.environ.get('WO_REVISION','')})
            if self.path=='/api/state':return self.with_db(self.reply_full_state)
            if self.path=='/api/message-templates':return self.reply(200,search_config.templates())
            if self.path=='/api/status':return self.reply(200,self.with_db(record_views.run_statuses))
            # Server-only Dashboard figures: bounded reads, no state build and no build lock.
            if self.path=='/api/dashboard':return self.reply(200,self.with_db(dashboard_views.overview,self.server.db.parent))
            if self.path=='/api/intake-review':return self.reply(200,self.with_db(intake_review.compare,self.server.db.parent))
            if urlsplit(self.path).path == '/api/email-leads':
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) - {'q', 'source'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Unsupported lead search')
                return self.reply(200, self.with_db(email_leads.search, self.server.db.parent,
                                  query.get('q', [''])[0], query.get('source', [''])[0]))
            lead = re.fullmatch(r'/api/email-leads/([a-f0-9]{64})(/original)?', self.path)
            if lead:
                if lead[2]:
                    return self.reply(200, email_leads.original_email(self.server.db.parent, lead[1]),
                                      'application/octet-stream', {'Content-Disposition': 'attachment; filename="original-alert.eml"'})
                return self.reply(200, self.with_db(email_leads.detail, self.server.db.parent, lead[1]))
            # A page holding an earlier summary version asks only for what changed since then.
            summary=re.fullmatch(r'/api/state\?view=summary(?:&since=([0-9a-f]{8}-[0-9]{1,12}))?(&encoding=strings-v1)?',self.path)
            if summary:
                raw,gz=self.with_db(store.summary_json,summary[1], 'strings-v1' if summary[2] else None)
                return self.reply(200,raw,gzipped=gz)
            timeline=re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)/timeline',self.path)
            if timeline:
                try:return self.reply(200,self.with_db(record_views.timeline,timeline[1]))
                except ValueError:return self.reply(404,{'error':'Unknown record'})
            lead_record = re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)/email-lead', self.path)
            if lead_record:
                return self.reply(200, self.with_db(email_leads.record_review, lead_record[1]))

            document=re.fullmatch(r'/api/documents/([a-f0-9]{32})',self.path)
            if document:
                import base64
                row=self.with_db(lambda conn: conn.execute('SELECT content_base64 FROM record_documents WHERE id=?',(document[1],)).fetchone())
                if not row:return self.reply(404,{'error':'Document not found'})
                return self.reply(200,base64.b64decode(row[0]),'application/octet-stream',{'Content-Disposition':'attachment; filename="private-document"'})
            if self.path not in ASSETS:return self.reply(404,{'error':'Not found'})
            name,kind=ASSETS[self.path];self.reply(200,(HERE/name).read_bytes(),kind)
        except (ValueError,TypeError) as error:self.reply(400,{'error':str(error)})
        except (BrokenPipeError,ConnectionResetError,socket.timeout):pass
        except Exception as error:
            logging.exception('Workspace read failed')
            wodb.note_failure(self.server.db.parent,'work-ops-workspace.service',error)
            self.reply(500,{'error':'Could not read the records. Check the local service log.'})
    def do_PATCH(self):
        self.mutate('PATCH')
    def do_POST(self):
        self.mutate('POST')
    def mutate(self, method):
        if not self.host_ok() or self.headers.get('Origin') not in self.allowed()[1]:
            return self.reply(403,{'error':'Same-origin request required'})
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
            return self.reply(415,{'error':'JSON required'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            limit=4_300_000 if self.path in ('/api/incoming/preview','/api/incoming/save') else 2_100_000 if self.path=='/api/import/linkedin' else 65536
            if not 0<length<=limit:raise ValueError('Invalid request size')
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):raise ValueError('JSON object required')
            lead = re.fullmatch(r'/api/email-leads/([a-f0-9]{64})/shortlist', self.path)
            if method == 'POST' and lead:
                if set(payload) != {'confirm'} or payload['confirm'] is not True:
                    raise ValueError('Confirm manual shortlisting of this unassessed lead')
                return self.reply(200, self.with_db(email_leads.shortlist, self.server.db.parent, lead[1]))
            match=re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)(/transition)?',self.path)
            review=re.fullmatch(r'/api/review/(\d+)',self.path)
            decision=re.fullmatch(r'/api/decisions/(\d+)',self.path)
            outcome=re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)/outcome',self.path)
            draft_save=re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)/draft',self.path)
            step=re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)/(correct|handover|next-step)',self.path)
            ask=re.fullmatch(r'/api/records/([A-Za-z0-9_-]+)/question',self.path)
            if method=='PATCH' and match and not match[2]:
                result=self.with_db(store.edit,match[1],payload)
            elif method=='POST' and match and match[2]:
                result=self.with_db(store.transition,match[1],payload)
            elif method=='POST' and review:
                result=self.with_db(store.resolve_review,int(review[1]),payload)
            elif method=='POST' and decision:
                # The only way to answer a decision; the server records it as the owner's.
                if self.with_db(store.get_decision,int(decision[1]))['kind']=='draft':
                    return self.reply(410,{'error':'Outgoing draft approval is retired. Saved decisions remain in history.'})
                result=self.with_db(store.answer_decision,int(decision[1]),payload)
            elif method=='POST' and outcome:
                if payload.get('sent_message') is not None or payload.get('draft_version_id') is not None:
                    return self.reply(410,{'error':'Saving outgoing wording is retired. Record contact without message text.'})
                result=self.with_db(store.outcome,outcome[1],payload)
            elif method=='POST' and draft_save:
                return self.reply(410,{'error':'Outgoing draft editing is retired. Saved wording remains in history.'})
            elif method=='POST' and ask:
                # The site only asks, as the owner; an agent answers through wo.
                result=self.with_db(store.ask_question,ask[1],payload)
            elif method=='POST' and step:
                result=self.with_db({'correct':store.correct,'handover':store.handover,'next-step':store.next_step}[step[2]],step[1],payload)
            elif method=='POST' and re.fullmatch(r'/api/seen/[a-z-]{1,24}(/record|/decision)?',self.path):
                result={}  # counted by the log line alone: which section, never which record
            elif method=='POST' and self.path in ('/api/week/store','/api/week/defer','/api/week/restore'):
                return self.reply(410,{'error':'Weekly person selection is retired. Manage relationships in People.'})
            elif method=='POST' and self.path=='/api/import/linkedin':
                result=self.with_db(store.import_linkedin,payload.get('csv'))
            elif method=='POST' and self.path in ('/api/incoming/preview','/api/incoming/save'):
                result=self.with_db(store.preview_incoming if self.path.endswith('/preview') else store.save_incoming,payload)
            elif method=='POST' and self.path=='/api/jobs/save':
                result=self.with_db(store.save_job,payload)
            elif method=='POST' and self.path in ('/api/records','/api/followup','/api/interactions','/api/links'):
                fn={'/api/records':store.create,'/api/followup':store.followup,
                    '/api/interactions':store.interaction,'/api/links':store.link}[self.path]
                result=self.with_db(fn,payload)
            else:return self.reply(404,{'error':'Not found'})
            self.reply(200,result)
        except store.Conflict as error:self.reply(409,{'error':str(error)})
        except (ValueError,TypeError,KeyError) as error:self.reply(400,{'error':str(error)})
        except (BrokenPipeError,ConnectionResetError,socket.timeout):pass
        except Exception as error:
            logging.exception('Workspace save failed')
            wodb.note_failure(self.server.db.parent,'work-ops-workspace.service',error)
            self.reply(500,{'error':'Save was not confirmed. Keep your changes and retry.'})

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Private SQLite career workspace; loopback only')
    parser.add_argument('--port',type=int,default=8769)
    parser.add_argument('--db',type=Path,default=HERE.parent/'data/work-ops.db')
    parser.add_argument('--public-origin',type=parse_public_origin,help='HTTPS origin behind a token-validating Access tunnel')
    parser.add_argument('--log-file',type=Path)
    args=parser.parse_args()
    if not args.db.is_file() or not args.db.stat().st_size:parser.error('Database is missing; run the documented local setup first')
    conn=wodb.connect(args.db)
    if wodb.pending(conn):parser.error('Database needs migration; run tools/wo.py migrate first')
    conn.close()
    if args.log_file:
        logging.basicConfig(level=logging.INFO,handlers=[RotatingFileHandler(args.log_file,maxBytes=1024*1024,backupCount=2)],format='%(asctime)s %(levelname)s %(message)s')
    else:  # under systemd, stderr goes to the journal, which adds the time
        logging.basicConfig(level=logging.INFO,stream=sys.stderr,format='%(levelname)s %(message)s')
    os.environ.setdefault('WO_UNIT','work-ops-workspace.service')
    server=WorkspaceHTTPServer(('127.0.0.1',args.port),Handler)
    server.db=args.db.resolve();server.public_origin=args.public_origin
    print(f'Career workspace: http://127.0.0.1:{server.server_port} — database {server.db}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
