#!/usr/bin/env python3
"""work-ops CLI: one SQLite store for career records, and the views taken from it.

    wo migrate                       apply pending migrations
    wo import-scans <pending_dir>    read daily-scan TSVs into the store
    wo import-report                 print the last import report
    wo ingest [--dry-run|--probe]    the daily run: every source, one import, one receipt (tools/ingest.py)
    wo export-csv [path]             write career-directory.csv from the store
    wo export-json [path] | import-json <path>    whole-store backup as JSON
    wo backup <dest>                 consistent .db copy (no sqlite3 binary needed)
    wo fetch <url> | fetch-log       read one URL through the scanners' fetcher; recent reads and failures
    wo show <id> | brief [--md]
    wo followup <id> --action … | transition <id> <field> <to> | interaction <id> --channel …

The Markdown hub import was retired on 27 September (history: tag `pre-cleanup-2026-09`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "workspace"))

import row_shape as ed  # noqa: E402
import db as wodb  # noqa: E402
import states  # noqa: E402
import records as store  # noqa: E402
import normalise  # noqa: E402

# Preserve importer entry points used by scanners and older integrations.
from record_imports import (  # noqa: E402
    TEXT_COLUMNS, STATE_COLUMNS, IMPORT_INSERT_ONLY, RECENT_SIGHTING,
    derive, upsert_record, ensure_organisation, link_organisation, write_scans,
    _import_scans, observation_time, record_lifecycle, lifecycle_time,
    import_observations, import_signal, import_research, cmd_import_scans,
)
DEFAULT_DB = ROOT / 'data/work-ops.db'


def csv_rows(conn):
    return store.csv_rows(conn)


# The readable snapshot committed to Git: the records and the work on them. Observations, history,
# edits and run receipts stay in the database and its encrypted off-host backups (ops/backup-offsite.sh).
COMMITTED_TABLES = ("records", "links", "followups", "drafts", "draft_versions", "decisions", "questions", "outcomes", "interactions", "review_queue")


def committed_dump(conn: sqlite3.Connection) -> dict:
    """The export that may be committed to Git. LinkedIn candidates the owner has not accepted or merged stay
    in the local database only (archive/plans/2026-09-22-linkedin-export.md, decision 3)."""
    payload = dump(conn)
    # A read-only export of a pre-migration database must retain its actual schema version/tables.
    payload['tables'] = {t: payload['tables'][t] for t in COMMITTED_TABLES if t in payload['tables']}
    payload['tables']['review_queue'] = [r for r in payload['tables']['review_queue']
                                         if r['kind'] != 'linkedin_person' or (r['resolution'] or '').startswith(('accept', 'merge'))]
    payload['scope'] = 'committed'
    return payload


def dump(conn: sqlite3.Connection) -> dict:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {"exported_at": wodb.now(), "schema_version": wodb.version(conn),
            "tables": {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in sorted(tables)}}


def load_dump(conn: sqlite3.Connection, payload: dict, *, replace: bool = False,
              allow_count_reduction: bool = False, partial: bool = False) -> dict:
    """Validated, atomic replacement from an export of the current schema. A full export must carry every
    table; `partial` accepts the committed snapshot, whose missing tables load empty."""
    if payload.get('schema_version') != wodb.version(conn):
        raise ValueError('Export schema differs; restore the original database and migrate it first')
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}-{'schema_version'}
    supplied=payload.get('tables',{})
    if not isinstance(supplied,dict):
        raise ValueError('Export must contain the database tables')
    given=set(supplied)-{'schema_version'}
    if (given != tables) if not partial else not (given <= tables and 'records' in given):
        raise ValueError('Export must contain exactly the current database tables' if not partial else
                         'Export contains tables this database does not have, or no records')
    supplied={t: supplied.get(t, []) for t in tables}
    for table in tables:
        columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
        if not isinstance(supplied[table],list) or any(not isinstance(r,dict) or set(r)!=columns for r in supplied[table]):
            raise ValueError('Invalid columns in '+table)
    record_ids={r['id'] for r in supplied['records']}
    if not record_ids:
        raise ValueError('Export contains no records; refusing destructive import')
    references = {
        'links': ('from_id', 'to_id'),
        'followups': ('record_id',),
        'interactions': ('person_id', 'org_id', 'record_id'),
        'state_events': ('entity_id',),
        'drafts': ('record_id',),
        'draft_versions': ('record_id',),
        'outcomes': ('record_id',),
        'record_edits': ('record_id',),
        'source_records': ('record_id',),
        'observations': ('record_id',),
        'decisions': ('record_id',),
        'questions': ('record_id',),
    }
    for table, columns in references.items():
        for row in supplied.get(table,[]):
            for column in columns:
                value=row[column]
                if value not in (None, '') and value not in record_ids:
                    raise ValueError(f'Export contains broken relationship: {table}.{column}={value}')
    current_count=conn.execute('SELECT COUNT(*) FROM records').fetchone()[0]
    incoming_count=len(supplied['records'])
    if current_count and not replace:
        raise ValueError('Database already contains records; pass --replace to replace it')
    if current_count and incoming_count < current_count and not allow_count_reduction:
        raise ValueError(f'Export has fewer records ({incoming_count}) than the database ({current_count}); pass --allow-count-reduction')
    counts={}
    with store.transaction(conn):
        conn.execute('PRAGMA defer_foreign_keys = ON')
        # Child tables first, so cascade deletes cannot erase rows already restored.
        order=sorted(tables-{'records'})+['records']
        for table in order: conn.execute('DELETE FROM '+table)
        for table in reversed(order):
            rows=supplied[table]
            for r in rows:
                cols=list(r)
                conn.execute('INSERT INTO '+table+' ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',list(r.values()))
            counts[table]=len(rows)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Export contains broken relationships')
    return counts


def cmd_brief(conn, markdown):
    today=dt.date.today().isoformat()
    rows=conn.execute("SELECT r.id,r.name,r.working_state,r.stage,r.status,f.action,f.reason,f.waiting_on FROM records r LEFT JOIN followups f ON f.record_id=r.id WHERE r.archived_at IS NULL AND f.record_id IS NOT NULL ORDER BY r.working_state,r.name").fetchall()
    counts=conn.execute('SELECT type,COUNT(*) FROM records WHERE archived_at IS NULL GROUP BY type').fetchall()
    queue=conn.execute('SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL').fetchone()[0]
    last=conn.execute("SELECT date,receipt_json FROM scan_runs WHERE run_id LIKE 'ingest:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    if last:
        receipt=json.loads(last['receipt_json'])
        keyed=', '.join(receipt.get('keyed') or [])
        which=('saved observations' if receipt.get('reused_pending') else f'free sources plus {keyed}' if keyed
               else 'free sources only' if receipt.get('keyless') else 'every source')
        read=f'sources last read {last["date"]} ({which})'
    else:
        read='no daily run recorded'
    lines=['# Career brief' if markdown else 'Career brief', '',f'Generated {today} from work-ops; {read}.','',
           'Records: '+', '.join(f'{t}: {n}' for t,n in counts),f'Import review: {queue} unresolved.','']
    for r in rows:
        lines.append(f"- {r['name']} ({r['id']}): {r['working_state'].replace('_',' ')}; stage {r['stage'] or r['status'] or 'not recorded'}. {r['action'] or ''}"+
                     (f" Waiting on {r['waiting_on']}: {r['reason']}." if r['waiting_on'] else ''))
    return '\n'.join(lines)+'\n'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("WO_DB", DEFAULT_DB)))
    ap.add_argument("--actor", default="owner", choices=["owner", "agent"],
                    help="who makes a mutation; agent corrections stay distinguishable from the owner's edits")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser('collection-policy', help='read collection policy and accounting; no network').add_argument('--json',action='store_true')
    p = sub.add_parser("ingest", help="the daily run: every source, one import, one receipt")
    p.add_argument("--data", type=Path, default=ROOT / "data")
    p.add_argument("--env-file", type=Path, default=ROOT / ".env", help="KEY=value credentials; names only are ever reported")
    p.add_argument("--dry-run", action="store_true", help="run on a copy of the database and pending store")
    p.add_argument("--reuse-pending", type=Path, help="import a verified pending snapshot without source requests; retain its read dates")
    p.add_argument("--probe", action="store_true", help="check the credentialled sources' live schemas and exit")
    p.add_argument("--keyless", action="store_true", help="free sources only: no Adzuna, Reed or mailbox calls")
    import ingest
    p.add_argument("--keyed", action="append", default=[], choices=ingest.KEYED,
                   help="with --keyless, also run this approved keyed source (repeatable)")
    sub.add_parser("import-scans").add_argument("pending", type=Path)
    sub.add_parser("import-report")
    p=sub.add_parser('research-review',help='read the immutable VPS research acceptance report; no research or model calls')
    p.add_argument('--date',type=dt.date.fromisoformat,default=dt.date(2026,10,19))
    sub.add_parser("import-research").add_argument("path", type=Path)
    sub.add_parser("export-csv").add_argument("path", type=Path, nargs="?", default=ROOT / "career-directory.csv")
    exj = sub.add_parser("export-json", help="slim export by default (generated, never committed: #28); --full for a complete backup")
    exj.add_argument("path", type=Path, nargs="?", default=ROOT / "records/db/work-ops.json")
    exj.add_argument("--full", action="store_true", help="include unaccepted LinkedIn candidates (backups only, never commit)")
    import_json=sub.add_parser("import-json")
    import_json.add_argument("path", type=Path)
    import_json.add_argument("--replace", action="store_true", help="replace a non-empty database after making a backup")
    import_json.add_argument("--allow-count-reduction", action="store_true", help="allow replacement with fewer records")
    import_json.add_argument("--partial", action="store_true",
                             help="load the committed snapshot (records and the work on them; no observations or history)")
    sub.add_parser("backup").add_argument("dest", type=Path)
    prune = sub.add_parser("prune-backups", help="list backups past retention (--apply deletes them)")
    prune.add_argument("dir", type=Path, nargs="?", default=ROOT / "data/backups")
    prune.add_argument("--apply", action="store_true")
    incoming=sub.add_parser('incoming-role',help='Preview or save a confirmed incoming hiring process (private evidence; no mail access)')
    incoming.add_argument('--json',required=True,help='JSON payload, or - for stdin')
    incoming.add_argument('--preview',action='store_true')
    sub.add_parser("show").add_argument("id")
    brief = sub.add_parser("brief")
    brief.add_argument("--md", action="store_true")
    follow = sub.add_parser("followup")
    follow.add_argument("id")
    follow.add_argument("--action", required=True)
    follow.add_argument("--owner", default="owner", choices=["owner", "agent"])
    follow.add_argument("--status", default="needs_review", choices=store.WORKING)
    follow.add_argument("--reason", default="")
    follow.add_argument("--waiting-on", default="")
    follow.add_argument("--notes", default="")
    move = sub.add_parser("transition")
    move.add_argument("id")
    move.add_argument("field")
    move.add_argument("to")
    move.add_argument("--note", default="")
    log = sub.add_parser("interaction")
    log.add_argument("id")
    log.add_argument("--channel", required=True, choices=list(states.ENUMS["interaction"]["channel"]))
    log.add_argument("--direction", required=True, choices=list(states.ENUMS["interaction"]["direction"]))
    log.add_argument("--at", default=dt.date.today().isoformat())
    log.add_argument("--summary", required=True)
    log.add_argument("--evidence", default="")
    outcome = sub.add_parser('outcome', help='record a factual outcome; never sends or changes commercial status')
    outcome.add_argument('id'); outcome.add_argument('kind',choices=list(states.OUTCOME_KINDS))
    outcome.add_argument('--note',default=''); outcome.add_argument('--draft-version',type=int); outcome.add_argument('--action-id',default='')
    outcome.add_argument('--revision',type=int,required=True)
    outcome.add_argument('--company',default='',help='required for submitted: the company your CV went to')
    fix = sub.add_parser('correct', help='undo a mistaken outcome (its contact and last touch go with it) or the latest change of a state field; both stay in the history')
    fix.add_argument('id'); fix.add_argument('--revision', type=int, required=True); fix.add_argument('--note', default='')
    target = fix.add_mutually_exclusive_group(required=True)
    target.add_argument('--outcome', type=int, help='the outcome id, from `show` or `state`')
    target.add_argument('--field', choices=sorted(store.STATE_FIELDS))
    dr = sub.add_parser('draft', help='save text for the owner to send himself (nothing is sent); --text-file - reads stdin')
    dr.add_argument('id'); dr.add_argument('--title', default=''); dr.add_argument('--text-file', default='')
    dr.add_argument('--purpose', default='approval', choices=['approval', 'message'],
                    help='approval: a note that needs a decision; message: the note on a card')
    dr.add_argument('--clear', action='store_true'); dr.add_argument('--reason', default='')
    wk = sub.add_parser('week', help="change this week's stored list")
    wk.add_argument('action', choices=['include'])
    wk.add_argument('id')
    create = sub.add_parser('create'); create.add_argument('--json',required=True,help='JSON record object; - reads it from stdin')
    edit = sub.add_parser('edit'); edit.add_argument('id'); edit.add_argument('--json',required=True,help='JSON fields; - reads them from stdin')
    find = sub.add_parser('find', help='active records whose name, role or organisation contains the text')
    find.add_argument('text'); find.add_argument('--type', default='')
    link = sub.add_parser('link'); link.add_argument('from_id'); link.add_argument('to_id'); link.add_argument('relation'); link.add_argument('--notes',default='')
    imp = sub.add_parser('import-linkedin'); imp.add_argument('csv',type=Path)
    lix = sub.add_parser('import-linkedin-export', help='queue review cards from a LinkedIn export snapshot (tools/linkedin_export.py)')
    lix.add_argument('snapshot', type=Path)
    lix.add_argument('--tranche', default='first', choices=['first', 'all', 'none'], help="'none' with --company selects one organisation")
    lix.add_argument('--company', default='')
    lst = sub.add_parser('import-list', help="apply the owner's list from tools/network_list.py check, all or nothing")
    lst.add_argument('file', type=Path)
    lst.add_argument('--dry-run', action='store_true', help='report what would change, then roll back')
    dec = sub.add_parser('decision', help="ask the owner on the site, and process his answers (he answers only on the site)")
    dsub = dec.add_subparsers(dest='decision_action', required=True)
    dadd = dsub.add_parser('add', help='ask a question with a declared kind of answers; a repeat returns the live one')
    dadd.add_argument('subject', help='a record id, or workspace:<slug>')
    dadd.add_argument('--kind', required=True, choices=sorted(store.states.DECISION_KINDS))
    dadd.add_argument('--question', required=True)
    dadd.add_argument('--context', default='')
    dadd.add_argument('--recommend', default='', help='one of the kind\'s choice ids')
    dadd.add_argument('--reason', default='', help='why that recommendation')
    dadd.add_argument('--evidence', default='', help='URLs separated by "; "')
    dlist = dsub.add_parser('list', help='decisions to process first: the owner\'s answers waiting for the agent')
    dlist.add_argument('--status', choices=['answered', 'open', 'live', 'all'], default='answered')
    dsub.add_parser('show', help='one decision in full: context, recommendation, answer and thread').add_argument('id', type=int)
    for name, text in (('done', 'what was done with the answer'), ('reply', 'the reply to the owner; reopens the decision'),
                       ('withdraw', 'why the question no longer applies'), ('claim', 'the work being started'), ('block', 'what prevents completion')):
        dp = dsub.add_parser(name); dp.add_argument('id', type=int); dp.add_argument('message', help=text)
        dp.add_argument('--revision',type=int,help='required: the decision revision shown by list/show; stale work is refused')

    qs = sub.add_parser('question', help="The owner's \"Ask Claude…\" questions from the site's cards; an agent answers them")
    qsub = qs.add_subparsers(dest='question_action', required=True)
    qlist = qsub.add_parser('list', help='unanswered questions, oldest first')
    qlist.add_argument('--all', action='store_true', help=f'also those asked in the last {store.QUESTION_DAYS} days')
    qsub.add_parser('show', help='one question and its answer').add_argument('id', type=int)
    qa = qsub.add_parser('answer', help='answer once; it shows on the card at his next page load')
    qa.add_argument('id', type=int)
    qa.add_argument('--text-file', required=True, help='- reads stdin')
    dn = dsub.add_parser('note', help='append failure progress without changing the owner’s answer')
    dn.add_argument('id', type=int); dn.add_argument('message')
    dn.add_argument('--stage', choices=store.PROGRESS_STAGES, default='investigating')
    dn.add_argument('--owner', choices=['agent', 'owner', 'provider'], default='agent')
    dn.add_argument('--next-step', default='')
    review = sub.add_parser('review'); review.add_argument('id',type=int); review.add_argument('resolution',choices=['accept','merge','reject']); review.add_argument('--target')
    arch = sub.add_parser('archive'); arch.add_argument('id'); arch.add_argument('--reason', required=True)
    retype = sub.add_parser('retype', help='correct an organisation between company and agency, in place')
    retype.add_argument('id'); retype.add_argument('to', choices=['company', 'agency']); retype.add_argument('--reason', required=True)
    sub.add_parser('approach', help='companies to approach and what each still lacks (the agent work queue)').add_argument('--limit', type=int, default=20)
    cn = sub.add_parser('company-number', help='record which Companies House company an organisation is, on evidence that states it')
    cn.add_argument('id'); cn.add_argument('number', help="8 digits, or 2 letters and 6 digits; 'none' removes a wrong one")
    cn.add_argument('--evidence', required=True, help="the page that states the number, e.g. the company's own terms or footer")
    merge = sub.add_parser('merge', help='fold a duplicate into the record that stays')
    merge.add_argument('duplicate'); merge.add_argument('keep'); merge.add_argument('--reason', required=True)
    quality = sub.add_parser('quality', help='list invalid data and enrichment gaps')
    quality.add_argument('--strict', action='store_true', help='exit 1 when any defect (not gap) is present')
    quality.add_argument('--json', action='store_true')
    activity = sub.add_parser('activity', help='measured use from stored history: outreach, preparation, research (read-only)')
    window = activity.add_mutually_exclusive_group()
    window.add_argument('--since', type=dt.date.fromisoformat, help='first London day, YYYY-MM-DD')
    window.add_argument('--weeks', type=int, default=2, help='the last N weeks (default 2)')
    activity.add_argument('--json', action='store_true')
    fix = sub.add_parser('repair', help='apply the deterministic quality fixes through the record service')
    fix.add_argument('--dry-run', action='store_true')
    sub.add_parser('state')
    watch = sub.add_parser('watch', help='raise a task on the site when the daily ingest or nightly backup is overdue')
    watch.add_argument('--data', type=Path, help='the data folder holding backups/ (default: the database\'s folder)')
    fetch = sub.add_parser('fetch', help='read one URL with the scanners\' identity, retries and log (tools/fetch.py)')
    fetch.add_argument('url')
    fetch.add_argument('--as', dest='format', choices=['text', 'json', 'raw'], default='text')
    fetch.add_argument('--browser', action='store_true', help='read the page in headless Chromium first')
    fetch.add_argument('--via', choices=['auto', 'local', 'vps'], default='auto',
                       help='auto: this host, then the VPS from the Mac if the source was unreachable')
    fetch.add_argument('--max-chars', type=int, default=0)
    flog = sub.add_parser('fetch-log', help='recent reads from the fetch log, scheduled and attended')
    flog.add_argument('--limit', type=int, default=30)
    flog.add_argument('--failures', action='store_true')
    flog.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if a.command == 'collection-policy':
        import collection_reads
        try:result=collection_reads.summary(a.db.parent/'collection.db')
        except (OSError,ValueError) as error:result=dict(state='unknown',reason=type(error).__name__)
        print(json.dumps(result,indent=2))
        return 0 if result['state']=='ok' else 1
    if a.command in ('fetch', 'fetch-log'):
        import fetch
        return fetch.main(a)
    if a.command=='research-review':
        import research_acceptance
        try:print(research_acceptance.read_report(a.db.parent,a.date))
        except FileNotFoundError:
            print('No completed acceptance report for '+a.date.isoformat()+'. The one-time VPS check is scheduled for 19 October at 10:05 Europe/London.');return 1
        except (OSError,ValueError,KeyError):
            print('Acceptance report could not be verified; inspect the acceptance service journal.');return 1
        return 0
    if a.command!='migrate' and (not a.db.is_file() or not a.db.stat().st_size):
        print('Database is missing; use migrate explicitly to create it.',file=sys.stderr);return 2

    if a.command == "ingest":
        import ingest
        env = ingest.load_env(a.env_file)
        if a.probe:
            return ingest.probe(env)
        try:
            with wodb.failures_noted(a.data, "wo-scan.service"):
                receipt = ingest.run(a.db, a.data, env, dry_run=a.dry_run, lock_wait=600, reuse_pending=a.reuse_pending,
                                     keyless=a.keyless, keyed=a.keyed)
        except ingest.Busy as error:
            print(error, file=sys.stderr)
            return 3
        print(ingest.format_receipt(receipt))
        return int(receipt.get('status') == 'failed')
    if a.command == "prune-backups":
        removed = wodb.prune_backups(a.dir, apply=a.apply)
        size = sum(p.stat().st_size for p in removed) if not a.apply else 0
        for p in removed:
            print(("deleted " if a.apply else "would delete ") + p.name)
        print(f"{len(removed)} file(s)" + ("" if a.apply else f", {size / 1e6:.0f} MB; pass --apply to delete"))
        return 0
    conn = wodb.connect(a.db)
    try:
        if a.command == "migrate":
            applied = wodb.migrate(conn)
            print(f"{a.db}: schema version {wodb.version(conn)}" + (f", applied {', '.join(applied)}" if applied else ", already current"))
            return 0
        # A backup is most needed just before migrating, so it is allowed while pending; the read-only
        # activity report also runs on a pulled backup whose schema is behind this checkout.
        if wodb.pending(conn) and a.command not in ("backup", "activity"):
            print(f"{a.db}: run `wo migrate` first", file=sys.stderr)
            return 2
        if a.command == "import-scans":
            report = cmd_import_scans(conn, a.pending)
            print(format_report(report))
        elif a.command == "import-research":
            print(json.dumps(import_research(conn,a.path),ensure_ascii=False))
        elif a.command == "import-report":
            row = conn.execute("SELECT report_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
            print(format_report(json.loads(row[0])) if row else "No import has been run.")
        elif a.command == "export-csv":
            count = store.write_csv(conn,a.path)
            print(f"{a.path}: {count} records")
        elif a.command == "export-json":
            a.path.parent.mkdir(parents=True, exist_ok=True)
            payload = dump(conn) if a.full else committed_dump(conn)
            a.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            print(f"{a.path}: {sum(len(v) for v in payload['tables'].values())} rows" + ("" if a.full else " (committed-safe)"))
        elif a.command == "import-json":
            payload=json.loads(a.path.read_text(encoding="utf-8"))
            if conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] and a.replace:
                stamp=dt.datetime.now().strftime('%Y%m%dT%H%M%S%f')
                backup_path=a.db.with_name(a.db.name+'.pre-import-'+stamp+'.db')
                wodb.backup(conn,backup_path)
                print(f'Pre-import backup: {backup_path}')
            counts = load_dump(conn, payload, replace=a.replace,
                               allow_count_reduction=a.allow_count_reduction, partial=a.partial)
            print(", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        elif a.command == "backup":
            print(f"{wodb.backup(conn, a.dest)}: schema version {wodb.version(conn)}")
        elif a.command == "show":
            row = conn.execute("SELECT * FROM records WHERE id = ?", (a.id,)).fetchone()
            if not row:
                print(f"No record {a.id}", file=sys.stderr)
                return 1
            out = {k: v for k, v in store.get(conn,a.id).items() if v not in (None, "")}
            out["links"] = [f"{r[1]} ({r[0]})" for r in conn.execute(
                "SELECT relation, to_id FROM links WHERE from_id = ? UNION SELECT relation, from_id FROM links WHERE to_id = ?",
                (a.id, a.id))]
            out["state_events"] = [dict(r) for r in conn.execute("SELECT * FROM state_events WHERE entity_id = ? ORDER BY id", (a.id,))]
            out["interactions"] = [dict(r) for r in conn.execute(store.LIVE_INTERACTIONS + " AND (record_id = ? OR person_id = ?) ORDER BY at", (a.id, a.id))]
            d = conn.execute("SELECT d.title, d.text, d.purpose, d.version_id, v.actor FROM drafts d "
                             "LEFT JOIN draft_versions v ON v.id=d.version_id WHERE d.record_id=?", (a.id,)).fetchone()
            if d:
                out["draft"] = dict(d)
            print(json.dumps(out, ensure_ascii=False, indent=1))
        elif a.command == "brief":
            print(cmd_brief(conn, a.md))
        elif a.command == "followup":
            saved = store.followup(conn, dict(id=a.id, revision=store.get(conn,a.id)["revision"], action=a.action, owner=a.owner,
                                              status=a.status, reason=a.reason, waiting_on=a.waiting_on, notes=a.notes), actor=a.actor)
            print(json.dumps({k: v for k, v in saved.items() if k != "followups"}))
        elif a.command == "transition":
            print(json.dumps(store.transition(conn,a.id,dict(field=a.field,to=a.to,note=a.note,revision=store.get(conn,a.id)["revision"]),actor=a.actor)))
        elif a.command == "interaction":
            print(json.dumps(log_interaction(conn, a.id, a.channel, a.direction, a.at, a.summary, a.evidence, actor=a.actor)))
        elif a.command == 'outcome':
            if a.draft_version is not None:
                raise ValueError('Saving outgoing wording is retired; record contact without --draft-version.')
            print(json.dumps(store.outcome(conn,a.id,dict(kind=a.kind,note=a.note,draft_version_id=a.draft_version,revision=a.revision,action_id=a.action_id,company=a.company),actor=a.actor)))
        elif a.command=='correct':
            print(json.dumps(store.correct(conn,a.id,dict(revision=a.revision,note=a.note,
                **({'outcome_id':a.outcome} if a.outcome is not None else {'field':a.field})),actor=a.actor)))
        elif a.command=='draft':
            raise ValueError('Outgoing draft editing is retired. Saved wording remains available through show and history.')
        elif a.command == 'week' and a.action == 'include':
            raise ValueError('Weekly person selection is retired. Manage relationships in People.')
        elif a.command=='incoming-role':
            payload=json.loads(sys.stdin.read() if a.json=='-' else a.json)
            print(json.dumps(store.preview_incoming(conn,payload) if a.preview else store.save_incoming(conn,payload,actor=a.actor)))
        elif a.command=='create': print(json.dumps(store.create(conn,json.loads(sys.stdin.read() if a.json=='-' else a.json),actor=a.actor)))
        elif a.command=='edit':
            payload=json.loads(sys.stdin.read() if a.json=='-' else a.json);payload.setdefault('revision',store.get(conn,a.id)['revision'])
            print(json.dumps(store.edit(conn,a.id,payload,actor=a.actor)))
        elif a.command=='link': print(json.dumps(store.link(conn,dict(from_id=a.from_id,to_id=a.to_id,relation=a.relation,notes=a.notes,revision=store.get(conn,a.from_id)['revision']),actor=a.actor)))
        elif a.command=='archive': print(json.dumps(store.archive(conn,a.id,dict(reason=a.reason,revision=store.get(conn,a.id)['revision']),actor=a.actor)))
        elif a.command=='retype': print(json.dumps(store.retype(conn,a.id,dict(type=a.to,reason=a.reason,revision=store.get(conn,a.id)['revision']),actor=a.actor)))
        elif a.command=='approach': print(approach_queue(store.state_context(conn),a.limit))
        elif a.command=='company-number':
            number=None if a.number.lower()=='none' else a.number
            print(json.dumps(store.company_number(conn,a.id,dict(number=number,evidence=a.evidence,revision=store.get(conn,a.id)['revision']),actor=a.actor)))
        elif a.command=='merge': print(json.dumps(store.merge(conn,a.duplicate,a.keep,dict(reason=a.reason,revision=store.get(conn,a.duplicate)['revision']),actor=a.actor)))
        elif a.command=='repair':
            import quality as dq
            print(json.dumps(dq.repair(conn,actor='agent' if a.actor=='owner' else a.actor,dry_run=a.dry_run),ensure_ascii=False,indent=1))
        elif a.command=='activity':
            import activity_report
            since=a.since or dt.datetime.now(activity_report.LONDON).date()-dt.timedelta(weeks=a.weeks)
            result=activity_report.report(conn,since)
            print(json.dumps(result,ensure_ascii=False,indent=1) if a.json else activity_report.format_report(result))
        elif a.command=='quality':
            import quality as dq
            report=dq.check(conn)
            print(json.dumps(report,ensure_ascii=False,indent=1) if a.json else dq.format_report(report))
            if a.strict and report['defect_total']: return 1
        elif a.command=='import-linkedin': print(json.dumps(store.import_linkedin(conn,a.csv.read_text(encoding='utf-8-sig'))))
        elif a.command=='import-linkedin-export':
            import linkedin_export
            print(json.dumps(linkedin_export.import_snapshot(conn,json.loads(a.snapshot.read_text(encoding='utf-8')),a.tranche,a.company),ensure_ascii=False,indent=1))
        elif a.command=='import-list':
            import network_list
            report=network_list.import_list(conn,json.loads(a.file.read_text(encoding='utf-8')),actor=a.actor,dry_run=a.dry_run)
            print(json.dumps(report,ensure_ascii=False,indent=1))
            if report['problems']: return 1
        elif a.command=='review':
            q=conn.execute('SELECT revision FROM review_queue WHERE id=?',(a.id,)).fetchone()
            if not q: raise ValueError('Unknown review item')
            print(json.dumps(store.resolve_review(conn,a.id,dict(resolution=a.resolution,target_id=a.target,revision=q[0]))))
        elif a.command=='state': store.print_state(conn, sys.stdout)
        elif a.command=='find': print(find_records(conn,a.text,a.type))
        elif a.command=='watch':
            import failure_tasks
            print(json.dumps(failure_tasks.watch(conn, a.data or a.db.parent)))
        elif a.command=='question':
            if a.question_action=='list':
                print(format_questions(store.questions(conn, unanswered=not a.all)))
            elif a.question_action=='show':
                print(format_questions([store.get_question(conn,a.id)]))
            else:
                answer=sys.stdin.read() if a.text_file=='-' else Path(a.text_file).read_text(encoding='utf-8')
                result=store.answer_question(conn,a.id,answer,actor=a.actor)
                print(f"question {a.id} answered on {result['question']['record_name']}")
        elif a.command=='decision':
            if a.decision_action not in ('add','list','show') and store.get_decision(conn,a.id)['kind']=='draft':
                raise ValueError('Outgoing draft decisions are retired and remain read-only history.')
            if a.decision_action=='add':
                if a.kind=='draft':raise ValueError('Outgoing draft approval is retired. Existing decisions are preserved.')
                result=store.create_decision(conn,dict(subject=a.subject,kind=a.kind,question=a.question,context=a.context,
                    recommendation=a.recommend,reason=a.reason,evidence_urls=a.evidence),actor=a.actor)
                print(('asked' if result['saved'] else 'already live')+f" #{result['decision']['id']}: {result['decision']['question']}")
            elif a.decision_action=='note':
                result=store.note_decision(conn,a.id,a.message,stage=a.stage,owner=a.owner,next_step=a.next_step,actor=a.actor)
                print(f"#{a.id} progress recorded; {result['decision']['status']}")
            elif a.decision_action=='list':
                print(format_decisions(conn,a.status))
            elif a.decision_action=='show':
                print('\n'.join(format_decision(store.get_decision(conn,a.id),full=True)))
            else:
                result=store.process_decision(conn,a.id,a.decision_action,a.message,actor=a.actor,revision=a.revision)
                print(f"#{a.id} {result['decision']['execution_status'] if a.decision_action in ('claim','block') else result['decision']['status']}")
    except (ValueError,store.Conflict) as error:
        print(str(error),file=sys.stderr);return 2
    finally:
        conn.close()
    return 0


def log_interaction(conn, record_id, channel, direction, at, summary, evidence='', source='manual', actor='owner'):
    return store.interaction(conn,dict(record_id=record_id,revision=store.get(conn,record_id)['revision'],channel=channel,direction=direction,at=at,summary=summary,evidence_ref=evidence),actor=actor)


def format_questions(rows: list[dict]) -> str:
    """Each question with the record it is about and, once answered, the answer."""
    if not rows:
        return 'No unanswered questions.'
    lines = []
    for q in rows:
        lines.append(f"question {q['id']} · {q['record_name']} ({q['record_id']}) · asked {q['asked_at'][:16]}: {q['question']}")
        if q['answered_at']:
            lines.append(f"  answered {q['answered_at'][:16]} by {q['answered_by']}: {q['answer']}")
    return '\n'.join(lines)


def format_decisions(conn, status='answered') -> str:
    """The agent's inbox: each decision with the owner's answer, his note and the thread so far."""
    where={'answered':"status='answered'",'open':"status='open'",'live':"status IN ('open','answered')",'all':'1'}[status]
    rows=[store.decision_view(conn,r) for r in conn.execute(f'SELECT * FROM decisions WHERE {where} ORDER BY answered_at,id')]
    return '\n'.join(line for d in rows for line in format_decision(d)) if rows else f'No {status} decisions.'


def format_decision(d: dict, full: bool = False) -> list[str]:
    """One decision's lines; `full` adds what was asked with it: context, recommendation, reason, evidence."""
    label=lambda cid,key='label': next((c[key] for c in d['choices'] if c['id']==cid),cid if key=='label' else '')
    lines=[f"#{d['id']} [{d['status']}] {d['subject']}{' ('+d['record_name']+')' if d['record_name'] else ''} · {d['kind']}",
           f"  Q: {d['question']}", f"  Revision: {d['revision']}" + (f" · execution: {d['execution_status']}" if d['execution_status'] else '')]
    if d.get('draft_version_id'):
        lines.append(f"  Draft version: {d['draft_version_id']} · {d['draft_binding']}" + (' · current' if d['draft_is_current'] else ' · superseded'))
        if full:
            lines.append('  Draft text: '+d['draft']['text'])
    if full:
        lines+=[f"  {name}: {value}" for name,value in (('Context',d['context']),('Recommended',d['recommendation'] and label(d['recommendation'])),
                                                        ('Why',d['reason']),('Evidence',d['evidence_urls'])) if value]
    if d['answer']:
        lines.append(f"  The owner: {label(d['answer'])}" + (f" — {d['note']}" if d['note'] else '') + f"  ({d['answered_at'][:16]})")
        lines.append(f"  Next: {label(d['answer'],'next')}")
    # Earlier exchanges only: the current answer is summarised above and the outcome below.
    last=max((i for i,t in enumerate(d['thread']) if t['by']=='owner'),default=len(d['thread'])) if d['answer'] else len(d['thread'])
    for t in d['thread'][:last]:
        lines.append(f"    · {t['by']}: {t.get('label') or t.get('action','')} {t.get('text','')}".rstrip())
    if d['outcome']:
        lines.append(f"  Outcome: {d['outcome']}")
    return lines


def find_records(conn, text: str, typ: str = '') -> str:
    """Active records whose name, role or organisation contains `text`, ignoring case: one line each."""
    like=f'%{text.lower()}%'
    rows=conn.execute("SELECT id,type,name,role,organisation FROM records WHERE archived_at IS NULL AND (lower(name) LIKE ? OR lower(role) LIKE ? "
                      "OR lower(organisation) LIKE ?)"+(' AND type=?' if typ else '')+' ORDER BY type,name,id',(like,like,like)+((typ,) if typ else ())).fetchall()
    return '\n'.join(f"{r['id']}  {r['type']}  "+' · '.join(x for x in (r['name'],r['role'],r['organisation']) if x) for r in rows) or f'No records match {text!r}.'


def approach_queue(state: dict, limit: int = 20) -> str:
    """This week as a page loaded now shows it, from the projection the site and research share.

    Sections follow `work_selection.WEEK_PAGE`. Each line names the rule that placed it
    (`work_selection.RULES`) and an active item's cause, so a surprising placement can be traced
    from the live gateway. Discovery items the page leaves to Jobs follow with their availability.
    `limit` bounds each section; 0 prints only the summary line the session hook reads."""
    import work_selection
    projection=state.get('opportunities') or {}
    by_id={r['id']:r for r in state['records']}
    total=sum(len(projection.get(group,[])) for group in ('active','shortlisted','discovery'))
    lines=[f"Opportunities: {total} active, shortlisted or recommended work records"]
    if limit<=0:return lines[0]
    def label(ref,*extra):
        r=by_id[ref['record_id']]
        trace=' · '.join(x for x in (ref['lane'],ref.get('rule'),ref.get('attention'),*extra) if x)
        return f"  {r['id']}  {r['name']}  [{trace}]  "
    def detail(ref):
        gaps=[g['text'] for g in (by_id[ref['record_id']].get('evidence_brief') or {}).get('gaps',[]) if g.get('blocking')]
        return label(ref)+('; '.join(gaps) if gaps else ref['reason'])
    def checking(ref):
        status=ref.get('research_status') or {}
        attempt=' '.join(x for x in (status.get('status'),status.get('attempted_at')) if x)
        return label(ref)+f"{ref.get('uncertainty','')} · research: {attempt or 'none'}"
    def held(ref):
        availability=(by_id[ref['record_id']].get('availability') or {}).get('status') or 'unknown'
        return label(ref,'availability '+availability)+ref['reason']
    def section(title,refs,line):
        if not refs:return  # The page shows no empty section.
        lines.append(f"{title}: {len(refs)}")
        lines.extend(line(ref) for ref in refs[:limit])
        if len(refs)>limit:lines.append(f"  … {len(refs)-limit} more (--limit)")
    page=work_selection.week_page(projection)
    for key,title in work_selection.WEEK_PAGE:
        section(title,page[key],checking if key=='needs_checking' else detail)
    section('Jobs → Discovery only, advert not open now',
            [ref for ref in projection.get('discovery',[]) if not ref.get('worth_a_look')],held)
    if 'research_ids' in projection:
        lines.append(f"Research targets: {len(projection['research_ids'])}  "+' '.join(projection['research_ids']))
    return '\n'.join(lines)


def format_report(r: dict) -> str:
    lines = [f"Import {r.get('command', '')} {r['source']} at {r['at']}".strip(),
             "  accepted: " + ", ".join(f"{k}={v}" for k, v in sorted(r["accepted"].items())) + f", total={r['total']}"]
    for key in ("followups", "drafts", "consultant_links", "person_candidates", "new", "existing", "scan_files"):
        if key in r:
            lines.append(f"  {key}: {r[key]}")
    lines.append(f"  malformed rows: {len(r['malformed'])}")
    lines += [f"    {m['file']}:{m['line']} — {m['reason']}" for m in r["malformed"][:25]]
    lines.append(f"  duplicate ids: {len(r['duplicate_ids'])}" + (" — " + ", ".join(r["duplicate_ids"][:10]) if r["duplicate_ids"] else ""))
    lines.append(f"  unresolved links: {len(r['unresolved_links'])}" + (" — " + ", ".join(r["unresolved_links"][:10]) if r["unresolved_links"] else ""))
    for key in ('observed','new_observed','filtered_observations','signals','signal_organisations_created',
                'commercial_signals','commercial_organisations_created'):
        if key in r: lines.append(f"  {key}: {r[key]}")
    if r.get('conflicts'):
        lines.append('  conflicts retained for review: '+str(len(r['conflicts'])))
        lines.extend('    '+json.dumps(x,ensure_ascii=False) for x in r['conflicts'])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
