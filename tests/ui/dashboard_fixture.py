"""Synthetic pending adverts shared by Dashboard browser and visual journeys."""
import json
from pathlib import Path
import sys


def write(data_dir):
    root=Path(__file__).resolve().parents[2]
    sys.path[:0]=[str(root/'workspace'),str(root/'tools')]
    import collection_budget,collection_policy
    ledger=Path(data_dir)/'collection.db'
    if not ledger.exists():
        import datetime as dt
        now=dt.datetime.now(dt.timezone.utc)
        collection_budget.initialize(ledger,at=now,hold_today=False)
        policy=collection_policy.load();policy['providers']['adzuna']['min_interval_seconds']=0
        budget=collection_budget.Budget(ledger,policy,now=lambda:now)
        for _ in range(6):budget.finish(budget.reserve('adzuna','scan'),'200')
        budget.reserve('adzuna','research')
        budget.defer('adzuna','daily-limit')
    folder=Path(data_dir)/'pending';folder.mkdir(exist_ok=True)
    items={}
    for number in range(45):
        scope='alert:linkedin';sid=str(910000+number)
        items[scope+'\x1f'+sid]=dict(kind='job',source_scope=scope,source_id=sid,
            first_seen_at='2026-11-06T10:00:00Z',last_seen_at='2026-11-06T10:00:00Z',source_lifecycle='active',
            current=dict(title=f'Engineering Manager {number:02}',company=f'Example Candidate {number:02}',
                eligible=True,location='London',url=f'https://www.linkedin.com/jobs/view/{sid}/',
                text='Lead software engineering teams and improve release readiness.' if number>=15 else 'Role details to follow.'))
    (folder/'observations.json').write_text(json.dumps(dict(items=items,sources={})))


if __name__=='__main__':write(sys.argv[1])
