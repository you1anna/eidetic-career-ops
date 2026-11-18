#!/usr/bin/env python3
"""Attended evidence evaluation; synthetic sources, at most five calls, no canonical imports."""
import argparse
import copy
import hashlib
import html
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'workspace'), str(ROOT / 'tools')]
import db
import automation_usage as quota
import brief_research as brief
import evidence_briefs
import research_model as model
import research_worker as worker

SHARED_USAGE_DB = Path('/srv/work-ops/data/work-ops.db')


def cases():
    for label, text, employer, expected in (
        ('current employer', 'Alex Example works at Example Labs.', 'Example Labs', True),
        ('conflicting employer', 'Alex Example works at New Labs.', 'New Labs', True),
        ('negated employment', 'Alex Example no longer works at Example Labs.', 'Example Labs', False),
        ('different person', 'Bailey Example works at Example Labs.', 'Example Labs', False),
        ('missing private context', 'Alex Example works at Example Labs.', 'Example Labs', True),
    ):
        p = dict(id='P-evaluation', type='person', name='Alex Example', organisation='Example Labs',
                 known_context='' if label=='missing private context' else 'We worked together.',
                 preferred_ask='catch_up', preferred_channel='linkedin', url='https://example.org/alex', revision=1)
        p['contacts'] = worker.person_contacts(p, [])
        source = dict(url=p['url'], ok=True, checked_at=worker.sources.stamp(), text=text, reason='read')
        candidate = dict(subject_id=p['id'], kind='employer', value=employer, source=0, quote=text)
        response = dict(vacancy=dict(status='unverified', source=0, quote='', basis=''), contacts=[], candidates=[candidate], unknowns=[])
        yield dict(label=label, record=p, source=source, response=response, expected=expected)


def run(output, mode='synthetic'):
    output = Path(output)
    if mode == 'socket' and not SHARED_USAGE_DB.is_file():
        raise ValueError('Socket evaluation requires the canonical VPS shared usage database; no copied or alternative ledger is accepted.')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    conn = db.connect(SHARED_USAGE_DB if mode == 'socket' else output / 'synthetic-usage.db')
    if mode == 'synthetic': db.migrate(conn)
    limits = dict(max_calls=5, _usage=dict(calls=0, completed=0, input_tokens=0, output_tokens=0))
    report = dict(mode=mode, source_mode='Synthetic public-source fixtures; live coverage untested',
                  usage_measurement='Real shared-ledger calls' if mode=='socket' else 'Synthetic accounting only; token use is not measured',
                  policy=brief.memo.policy(), evaluator=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  human_review='pending', cases=[], status='running')
    try:
        for case in cases():
            prompt = brief.packet(case['record'], [case['source']])
            request = dict(operation='brief', prompt=prompt)
            if mode == 'synthetic':
                with patch.object(model, 'exchange', return_value=dict(ok=True, result=case['response'], usage={})):
                    result = model.call(conn, 'brief-evaluation:'+worker.sources.stamp(), None, limits, worker.sources.stamp(), prompt, model.BRIEF)
            else:
                result = model.call(conn, 'brief-evaluation:'+worker.sources.stamp(), None, limits, worker.sources.stamp(), prompt, model.BRIEF)
            finding = brief.checked_result(case['record'], copy.deepcopy(result), [case['source']])
            view = evidence_briefs.build(case['record'], researched=finding['evidence_brief'])
            accepted = bool(finding['evidence_brief']['items'])
            report['cases'].append(dict(label=case['label'], request=request, response=result, finding=finding, brief=view,
                                        expected_accept=case['expected'], control_pass=accepted==case['expected']))
        report['status'] = 'completed'
    except Exception as error:
        report.update(status='blocked' if isinstance(error, (model.UsageLimited, model.AccessBlocked)) else 'failed', reason=str(error))
    finally:
        report['usage'] = limits['_usage']
        report['allowance_after'] = quota.summary(conn, worker.sources.stamp())
        conn.close()
        digest = worker.private_json(output / 'report.json', report)
        escaped = html.escape(json.dumps(report, indent=2, ensure_ascii=False))
        page = ('<!doctype html><meta charset="utf-8"><title>Evidence brief evaluation</title>'
                '<h1>Evidence brief evaluation</h1><p>Review source attribution, useful gaps and preserved unknowns. '
                'No canonical records were imported. Synthetic sources do not prove live coverage. '
                'Passing controls is not human acceptance.</p><pre>'+escaped+'</pre>')
        with (output / 'review.html').open('x') as out: out.write(page)
        (output / 'review.html').chmod(0o600)
        worker.private_json(output / 'manifest.json', dict(report_sha256=digest))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New private report directory')
    parser.add_argument('--mode', choices=('synthetic','socket'), default='synthetic')
    args = parser.parse_args(argv)
    try: report = run(args.output, args.mode)
    except ValueError as error:
        print('BLOCKED: '+str(error)); return 2
    print(json.dumps({k:report[k] for k in ('mode','status','usage','human_review')}))
    return 0 if report['status']=='completed' and (args.mode=='socket' or all(c['control_pass'] for c in report['cases'])) else 2


if __name__ == '__main__': raise SystemExit(main())
