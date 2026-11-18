#!/usr/bin/env python3
"""Compare ranking and This week before and after a code change, on a copy of the data.

    .venv/bin/python tools/rank_diff.py snapshot --db data/work-ops-<date>.db --today <date> --rev <old> --out data/measure/before.json
    .venv/bin/python tools/rank_diff.py snapshot --db data/work-ops-<date>.db --today <date> --out data/measure/after.json
    .venv/bin/python tools/rank_diff.py compare data/measure/before.json data/measure/after.json

`--rev` builds the snapshot with the code at that commit (extracted to a temporary folder), so the
before snapshot needs no checkout; without it the working tree is used. Each snapshot recomputes
everything (relevance, intelligence, availability and the This week projection) from the database.
Advert scores use the fixed `--today`; the projection uses the clock, so build both in one sitting.
It reads only; exit 1 means a QE contract advert fell to Low (archive/plans/2026-09-28-problem-first-search.md).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
GROUPS = ('discovery', 'shortlisted', 'active', 'closed', 'other')
REF_KEYS = ('record_id', 'rule', 'lane', 'worth_a_look', 'attention')
LISTED = 15  # records listed per changed section


def extract(rev: str, into: Path, repo: Path = ROOT) -> str:
    """Write the tracked files at `rev` into `into`; return the full commit id."""
    git = ['git', '-C', str(repo)]
    sha = subprocess.run([*git, 'rev-parse', '--verify', rev + '^{commit}'], check=True,
                         capture_output=True, text=True).stdout.strip()
    archive = subprocess.run([*git, 'archive', '--format=tar', sha], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter='data')
    return sha


def _code(code_root: Path):
    """The modules of one code version. A process can hold only one, so another root needs a fresh run."""
    workspace = (Path(code_root) / 'workspace').resolve()
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    import db
    import records
    import relevance
    if Path(records.__file__).resolve().parent != workspace:
        raise RuntimeError(f'records is already loaded from {Path(records.__file__).parent}; '
                           'snapshot another code root in a fresh process')
    return db, records, relevance


def _selection(state: dict) -> dict:
    """The projection's placements, weekly sections and research targets, with names to report them."""
    projection = state.get('opportunities') or {}
    lists = {key: [{k: ref[k] for k in REF_KEYS if k in ref} for ref in projection.get(key) or []]
             for key in (*GROUPS, 'act_now', 'waiting', 'in_progress', 'needs_checking')}
    ids = {ref['record_id'] for refs in lists.values() for ref in refs}
    names = {r['id']: r.get('name') or '' for r in state['records'] if r['id'] in ids}
    return dict(projection=dict(lists, research_ids=list(projection.get('research_ids') or [])),
                names=names, error=state.get('intelligence_error') or state.get('relevance_error') or '')


def snapshot(db_path: Path, today: dt.date, code_root: Path = ROOT) -> dict:
    """Scores as of one fixed date, so snapshots taken on different days compare only the code change."""
    db, records, relevance = _code(code_root)
    conn = db.connect(db_path)
    try:
        state = records.state_context(conn)
    finally:
        conn.close()
    built = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    scores = relevance.score(state, relevance.load_config(), today)
    out = {}
    for r in state['records']:
        rel = scores.get(r['id']) or {}
        if r['type'] in relevance.ADVERT_TYPES and rel:
            title = next((x['text'] for x in rel['reasons'] if '"' in x['text'] or 'Title matches none' in x['text']), '')
            out[r['id']] = dict(name=r.get('name') or '', type=r['type'], score=rel['score'], band=rel['band'],
                                lane=rel.get('lane') or '', family=(rel.get('route') or {}).get('family') or '-', title_reason=title)
    return {'adverts': out, 'selection': _selection(state), 'built_at': built}


def _placements(selection: dict) -> dict:
    projection = selection['projection']
    return {ref['record_id']: (group, ref.get('rule') or '-') for group in GROUPS for ref in projection.get(group, [])}


def compare_selection(before: dict, after: dict) -> dict:
    """This week section by section, under the current page definition, and every changed placement."""
    sys.path.insert(0, str(ROOT / 'workspace'))
    import work_selection
    snaps = (before['selection'], after['selection'])
    pages = [work_selection.week_page(s['projection']) for s in snaps]
    sections = []
    for key, title in (*work_selection.WEEK_PAGE, ('research_ids', 'Research targets')):
        b, a = ([ref['record_id'] for ref in page[key]] if key in page else list(s['projection'][key])
                for page, s in zip(pages, snaps))
        sections.append(dict(title=title, before=len(b), after=len(a),
                             entered=[i for i in a if i not in set(b)], left=[i for i in b if i not in set(a)]))
    places = [_placements(s) for s in snaps]
    moved = [(rid, places[0].get(rid), places[1].get(rid)) for rid in sorted(set(places[0]) | set(places[1]))
             if places[0].get(rid) != places[1].get(rid)]
    note = ''
    if before.get('built_at') and after.get('built_at'):
        hours = abs((dt.datetime.fromisoformat(after['built_at']) - dt.datetime.fromisoformat(before['built_at'])).total_seconds()) / 3600
        if hours >= 1:
            note = (f'Snapshots were built {hours:.1f} h apart: advert availability (48-hour windows) and dated '
                    'evidence follow the clock, so some differences may not come from the code.')
    return dict(sections=sections, moved=moved, places=places, names={**snaps[0]['names'], **snaps[1]['names']},
                errors=[s['error'] for s in snaps], note=note)


def compare(before: dict, after: dict) -> dict:
    b, a = before['adverts'], after['adverts']
    ids = sorted(set(b) & set(a))
    table = collections.Counter()
    for i in ids:
        table[(a[i]['family'], 'before', b[i]['band'])] += 1
        table[(a[i]['family'], 'after', a[i]['band'])] += 1
    movers = sorted((a[i]['score'] - b[i]['score'], i, a[i]['name']) for i in ids if abs(a[i]['score'] - b[i]['score']) >= 10)
    fell = [(i, a[i]['name']) for i in ids if a[i]['family'] == 'quality' and a[i]['lane'] == 'contract'
            and b[i]['band'] != 'low' and a[i]['band'] == 'low']
    top = lambda s: collections.Counter(a[i]['family'] for i in sorted(ids, key=lambda i: -s[i]['score'])[:20])
    selection = compare_selection(before, after) if 'selection' in before and 'selection' in after else None
    return dict(table=table, movers=movers, fell=fell, top_before=top(b), top_after=top(a), selection=selection)


def selection_report(out: dict) -> list[str]:
    where = lambda place: ' · '.join(place) if place else 'not placed'
    name = lambda rid: (out['names'].get(rid) or '')[:60]
    def change(rid):
        before, after = out['places'][0].get(rid), out['places'][1].get(rid)
        return f'{where(before)} → {where(after)}' if before != after else f'still {where(after)}'
    def listed(prefix, ids):
        rows = [f'  {prefix} {rid}  {name(rid)}  ({change(rid)})' for rid in ids[:LISTED]]
        return rows + ([f'    … {len(ids) - LISTED} more'] if len(ids) > LISTED else [])
    lines = [f'note: {out["note"]}'] if out['note'] else []
    lines += [f'note: projection unavailable in the {side} snapshot: {error}'
              for side, error in zip(('before', 'after'), out['errors']) if error]
    lines.append('This week              before  after')
    lines += [f"{s['title']:22} {s['before']:6} {s['after']:6}" for s in out['sections']]
    for s in out['sections']:
        if s['entered'] or s['left']:
            lines.append(f"{s['title']}: {len(s['entered'])} entered, {len(s['left'])} left")
            lines += listed('+', s['entered']) + listed('-', s['left'])
    lines.append(f"Placement changes (group · rule): {len(out['moved'])}")
    transitions = collections.Counter((b, a) for _, b, a in out['moved'])
    for (b, a), count in transitions.most_common():
        examples = [rid for rid, x, y in out['moved'] if (x, y) == (b, a)][:3]
        lines.append(f"  {count:5}  {where(b)} → {where(a)}  e.g. {' '.join(examples)}")
    return lines


def report(out: dict) -> str:
    fams = sorted({k[0] for k in out['table']})
    lines = ['family       band      before  after']
    for f in fams:
        for band in ('strong', 'possible', 'low'):
            lines.append(f"{f:12} {band:9} {out['table'][(f, 'before', band)]:6} {out['table'][(f, 'after', band)]:6}")
    lines.append(f"top 20 before: {dict(out['top_before'])}")
    lines.append(f"top 20 after:  {dict(out['top_after'])}")
    lines.append(f"moved by 10 or more: {len(out['movers'])}")
    lines += [f"  {d:+4} {i} {n[:70]}" for d, i, n in out['movers']]
    lines.append(f"QE contracts that fell to Low: {len(out['fell'])}")
    lines += [f"  {i} {n[:70]}" for i, n in out['fell']]
    if out.get('selection'):
        lines += selection_report(out['selection'])
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('--db', type=Path, required=True)
    s.add_argument('--out', type=Path, required=True)
    s.add_argument('--today', type=dt.date.fromisoformat, required=True, help='score as of this date; use one date for every snapshot compared')
    s.add_argument('--rev', help='build with the code at this git revision instead of the working tree')
    c = sub.add_parser('compare')
    c.add_argument('before', type=Path)
    c.add_argument('after', type=Path)
    a = ap.parse_args(argv)
    if a.command == 'snapshot':
        if a.rev:
            with tempfile.TemporaryDirectory(prefix='rank-diff-') as code:
                sha = extract(a.rev, Path(code))
                snap = dict(snapshot(a.db.resolve(), a.today, Path(code)), code=sha)
        else:
            snap = dict(snapshot(a.db, a.today), code='working tree')
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
        print(f"{a.out}: {len(snap['adverts'])} adverts, This week built by {snap['code']} at {snap['built_at']}")
        return 0
    out = compare(json.loads(a.before.read_text()), json.loads(a.after.read_text()))
    print(report(out))
    return 1 if out['fell'] else 0


if __name__ == '__main__':
    sys.exit(main())
