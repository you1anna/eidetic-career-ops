#!/usr/bin/env python3
"""Build a synthetic development database: made-up records that exercise every path the checks use.

The owner's data is never committed (decision #28), so `ops/check.sh`, cloud sessions and fresh clones use
this instead. Everything goes through the record service, so the database is valid by construction and
follows the current migrations. Names are invented and every email is on example.com.

Usage: .venv/bin/python tools/make_fixture.py <new.db>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / 'workspace')]
import db  # noqa: E402
import records  # noqa: E402
import record_imports  # noqa: E402
from email_worker import save_receipt  # noqa: E402

AGENT = 'agent'


def build(path: Path) -> dict:
    if path.exists():
        raise SystemExit(f'refusing to overwrite {path}')
    conn = db.connect(path)
    db.migrate(conn)
    today = dt.date.today()
    recent = (today - dt.timedelta(days=3)).isoformat()

    def new(**values) -> str:
        return records.create(conn, values, actor=AGENT)['record']['id']

    def rev(rid: str) -> int:
        return records.get(conn, rid)['revision']

    def link(a: str, b: str, relation: str) -> None:
        records.link(conn, dict(from_id=a, to_id=b, relation=relation, revision=rev(a)), actor=AGENT)

    def research(rid: str, signal: str, url: str) -> None:
        records.add_observation(conn, rid, 'research:development-fixture', db.now(), json.dumps(dict(
            signal=signal, reviewed_at=today.isoformat(), source_date=recent, source_urls=[url],
            facts=['The source describes an existing software service.'],
            assumptions=['A bounded engagement may be useful.'],
            suggested_task='Confirm the delivery owner and remit',
            verification='primary-read', decisive_unknowns=['Whether an interim or bounded engagement is useful']), sort_keys=True))

    def advert(rid: str, text: str, url: str, observed_at: str | None = None) -> None:
        row = records.get(conn, rid)
        records.add_observation(conn, rid, 'board:fixture:example', observed_at or db.now(), json.dumps(dict(
            title=row['name'], text=text, url=url, employment_type=row.get('engagement') or 'contract', date=recent), sort_keys=True))

    rail = new(type='company', name='Example Rail Ltd', url='https://jobs.example.com/example-rail',
               summary='Invented train-ticketing company used only for development data.')
    fintech = new(type='company', name='Northwind Fintech', url='https://northwind.example.com',
                  summary='Invented payments company.')
    research(fintech, 'Building a Quality Engineering department for software release readiness, incident prevention and SLO coverage.',
             'https://northwind.example.com/careers')
    cedar = new(type='company', name='Cedar Example Software', url='https://cedar.example.com',
                summary='Invented software company with a separate integration remit.')
    research(cedar, 'An inherited application needs data integration and recovery improvements with a maintainable team handover.',
             'https://cedar.example.com/integration')
    agency = new(type='agency', kind='agency', name='Acme Tech Recruitment', status='not_contacted',
                 subtype='Engineering leadership contract desk', url='https://acme-recruitment.example.com',
                 summary='Invented contract technology recruiter with software engineering leadership briefs.')
    record_imports.import_research_entries(conn, [dict(id=agency, reviewed_at=today.isoformat(),
        desk_summary='Engineering leadership and AI product contracts. Current briefs need confirming.',
        public_contact_route='https://acme-recruitment.example.com/contact',
        source_urls=['https://acme-recruitment.example.com/desks'], verification='primary-read')],
        'research:fixture-agency-desk')
    # The owner's list (Release 2): who to contact first, how, and what he asks.
    alex = new(type='person', name='Alex Example', role='Head of Engineering', organisation='Northwind Fintech',
               email='alex@example.com', phone='07700 900123', relationship='warm', kind='former_colleague', priority='first',
               preferred_channel='whatsapp', preferred_ask='introduction',
               known_context='Your note (14 Oct 2026): Worked together on an invented payments platform.')
    riley = new(type='person', name='Riley Recruiter', role='Senior Consultant, Engineering Leadership', relationship='active',
        email='riley@example.com', kind='recruiter', priority='next', preferred_channel='email', preferred_ask='availability')
    # Enough deliberately listed, wholly synthetic people to exercise the five-person
    # pilot and its replacement/restore behavior without padding it with organisations.
    for n, name in enumerate(['Bailey Pilot', 'Drew Pilot', 'Ellis Pilot', 'Frankie Pilot', 'Georgie Pilot'], start=2):
        rid = new(type='person', name=name, relationship='warm', kind='former_colleague', priority='next',
                  email=f'pilot{n}@example.com', preferred_channel='email', preferred_ask='catch_up',
                  known_context='Your note: Worked together on a synthetic software delivery team.')
        record_imports.import_research_entries(conn, [dict(id=rid, reviewed_at=today.isoformat(),
            source_urls=['https://pilot.example.com/context'], decisive_unknowns=['Their current priorities'],
            approach=dict(role='connector', points=['Ask what they are working on.'], ask='Would a short catch-up be useful?',
                          cv='Manager', goal='A useful catch-up', wave=n))], 'research:fixture-pilot')
    sam = new(type='person', name='Sam Sample-Jones', role='Recruitment consultant', organisation='Acme Tech Recruitment',
              email='sam@example.com', kind='recruiter', relationship='cold')
    taylor = new(type='person', name='Taylor Fixture', role='VP Engineering', organisation='Northwind Fintech',
                 email='taylor@example.com', relationship='active', kind='peer')
    new(type='person', name='Jo Placeholder', role='QA Lead', email='jo@example.com')
    job = new(type='job', name='Interim Engineering Manager', organisation='Example Rail Ltd', status='candidate',
              url='https://jobs.example.com/example-rail/1001', pay='£750/day', engagement='contract', ir35='Outside',
              detail_url='https://jobs.example.com/example-rail/1001/details', apply_url='https://jobs.example.com/example-rail/1001/apply',
              date=recent, location='London', summary='Lead software engineering teams through platform modernisation and improve delivery quality.')
    job2 = new(type='job', name='Interim Engineering Manager', organisation='Acme Tech Recruitment', status='candidate',
               url='https://acme-recruitment.example.com/jobs/2002', engagement='contract', date=recent,
               summary='Interim software engineering leadership for a cloud platform and developer productivity team.')
    opp = new(type='opportunity', name='Acme Tech Recruitment / Sam Sample-Jones', stage='lead',
              summary='Invented contract route through a recruiter.')
    event = new(type='event', name='Example QA Meetup', date=(today + dt.timedelta(days=11)).isoformat(), url='https://events.example.com/qa')
    tender = new(type='tender', name='Example Council test services', published_deadline=(today + dt.timedelta(days=26)).isoformat(),
                 date=recent, url='https://tenders.example.com/1', summary='Software test automation and release readiness for an existing public-service application.')
    source = new(type='source', name='Example jobs API', summary='Invented keyed source.')
    # A real exclusion label with entirely invented role/person data, solely to verify policy UI.
    excluded_company = new(type='company', name='Excludo', url='https://excluded.example.com',
                           summary='Synthetic exclusion fixture; no real company evidence or contact data.')
    excluded_job = new(type='job', name='Synthetic excluded engineering role', organisation='Excludo', status='candidate',
                       engagement='contract', url='https://excluded.example.com/jobs/synthetic',
                       summary='Synthetic software engineering role for exclusion checks only.')
    excluded_person = new(type='person', name='Exclusion Fixture Contact', organisation='Excludo',
                          relationship='warm', kind='former_colleague')
    link(excluded_job, excluded_company, 'hiring_for')
    link(excluded_person, excluded_company, 'works_at')

    link(job, rail, 'hiring_for')
    link(job2, agency, 'hiring_for')
    link(alex, fintech, 'works_at')
    link(alex, cedar, 'related')  # one person, two different company problems and outcome contexts
    link(taylor, fintech, 'works_at')
    link(sam, agency, 'consultant_at')
    link(sam, agency, 'related')      # the same pair twice: the detail view shows it once
    link(agency, sam, 'related')      # and in the other direction
    link(opp, agency, 'related')
    link(opp, sam, 'related')
    link(tender, fintech, 'buyer')
    link(event, alex, 'related')
    link(job2, job, 'possible_match')

    records.followup(conn, dict(id=job, action='Read the advert and decide whether to pursue', status='ready',
                                owner='owner', revision=rev(job)), actor=AGENT)
    records.interaction(conn, dict(record_id=alex, at=recent, channel='email', direction='in',
                                   summary='Invented note: asked about fractional work.', revision=rev(alex)), actor=AGENT)
    # Companies to approach: one just funded with a named CTO, one advertising a leadership role itself.
    funded = new(type='company', name='Brightpath Robotics', url='https://brightpath.example.com',
                 summary='Invented robotics start-up used only for development data.')
    casey = new(type='person', name='Casey Placeholder', role='CTO', organisation='Brightpath Robotics',
                verification_level='primary-read', evidence_urls='https://brightpath.example.com/team')
    link(casey, funded, 'works_at')
    research(funded, 'The software team is building model evaluation and guardrails for an existing agentic workflow before wider customer deployments.',
             'https://brightpath.example.com/software')
    published = (today - dt.timedelta(days=7)).isoformat()  # inside the 120-day funding window
    records.add_observation(conn, funded, 'news:uktn', published, json.dumps(dict(
        amount='£4m', company='Brightpath Robotics', feed='uktn', headline='Brightpath Robotics raises £4m (invented)',
        published=published, region='UK', url='https://news.example.com/brightpath'), sort_keys=True))
    harbour = new(type='company', name='Harbour Analytics', url='https://harbour.example.com',
                  summary='Invented analytics company used only for development data.')
    lead = new(type='job', name='Interim Head of Engineering', organisation='Harbour Analytics', engagement='interim',
               location='London', date=recent, url='https://harbour.example.com/jobs/head-of-engineering',
               summary='Interim software engineering leadership for a legacy data integration and recovery programme.')
    link(lead, harbour, 'hiring_for')
    morgan = new(type='person', name='Morgan Example', role='Head of Engineering', organisation='Harbour Analytics',
                 verification_level='primary-read', evidence_urls='https://harbour.example.com/team')
    link(morgan, harbour, 'works_at')
    research(harbour, 'An inherited software application needs data integration and recovery improvements with a maintainable team handover.',
             'https://harbour.example.com/engineering')
    for rid in (job, job2, lead, tender):
        row = records.get(conn, rid)
        # Keep the later explicit check authoritative even when fixture creation takes <1 second.
        board_at = (dt.datetime.fromisoformat(db.now()) - dt.timedelta(seconds=1)).isoformat() if rid == job2 else None
        advert(rid, row['summary'], row['url'], board_at)
    # Agency recommendations need an explicit current vacancy check, not a board sighting.
    record_imports.import_research_entries(conn, [dict(id=job2, reviewed_at=db.now(),
        source_urls=['https://acme-recruitment.example.com/jobs/2002'], freshness_checks=[dict(
            scope='vacancy', status='confirmed', checked_at=db.now(), provenance='public_read',
            source_url='https://acme-recruitment.example.com/jobs/2002',
            basis='Synthetic agency advert explicitly accepts applications for this role.')])],
        'research:fixture-agency-open-role')
    # A promising role with an explicitly unresolved prerequisite and a real-shaped attempt.
    # This exercises Needs checking without pretending that an alert confirms availability.
    checking = new(type='job', name='Quality Engineering Lead — payments verification',
                   organisation='Northwind Fintech', status='candidate', engagement='permanent',
                   location='London', pay='£100,000/year', date=recent,
                   url='https://jobs.example.com/linkedin-alert/999000123',
                   summary='Lead software quality engineering teams. Essential: experience with synthetic payments protocols.')
    link(checking, fintech, 'hiring_for')
    records.add_observation(conn, checking, 'alert:linkedin', db.now(), json.dumps(dict(current=dict(
        title='Quality Engineering Lead — payments verification', company='Northwind Fintech',
        url='https://jobs.example.com/linkedin-alert/999000123', received_at=db.now()))))
    save_receipt(conn, 'research:synthetic-week-preview', dict(started_at=recent, finished_at=recent,
        status='complete', items=[dict(id=checking, attempted_at=recent+'T10:00:00Z', outcome='unverified',
        reason='Synthetic source refused the read; the payments requirement remains unestablished.')]))
    records.draft(conn, dict(record_id=opp, title='Draft to Sam',
                            text='Hi Sam,\n\nAn invented draft for development data.\n\nMorgan', revision=rev(opp)), actor=AGENT)
    records.draft(conn, dict(record_id=casey, title='Note to Casey Placeholder',
                            text='Hi Casey,\n\nWhich existing AI workflow needs clearer release evidence before its next customer deployment?\n\nMorgan',
                            revision=rev(casey)), actor=AGENT)
    conn.commit()

    for d in (dict(subject=agency, kind='join', question='Register with Acme Tech Recruitment?',
                   context='Invented.', recommendation='help', reason='Development data.'),
              dict(subject=opp, kind='draft', question='Approve the draft to Sam?', recommendation='approve',
                   reason='Development data.'),
              dict(subject='workspace:example-setting', kind='approve', question='Turn on an example setting?',
                   recommendation='approve', reason='Development data.'),
              dict(subject=source, kind='join', question='Register for the example API key?',
                   recommendation='later', reason='Development data.'),
              dict(subject='workspace:source-board-lever-example', kind='failure',
                   question='The daily run cannot read Example Board: address not found (HTTP 404); the source may have moved.',
                   recommendation='fix', reason='Development data.')):
        records.create_decision(conn, d)

    approved = records.create_decision(conn, dict(subject=casey, kind='draft', question='Use this note to Casey Placeholder?',
                                                 recommendation='approve', reason='The invented source identifies a specific existing AI workflow.'))['decision']
    records.answer_decision(conn, approved['id'], dict(choice='approve', revision=approved['revision']))
    history = records.create_decision(conn, dict(subject=opp, kind='approve', question='Research the example recruiter route?',
                                                recommendation='approve', reason='Synthetic decision history.'))['decision']
    history = records.answer_decision(conn, history['id'], dict(choice='approve', revision=history['revision']))['decision']
    records.process_decision(conn, history['id'], 'done', 'The example remit is researched.', revision=history['revision'])
    records.create_decision(conn, dict(subject=opp, kind='approve', question='Confirm the example engagement route?',
                                      recommendation='approve', reason='A second live decision on the same record.'))
    records.outcome(conn, morgan, dict(kind='conversation', note='Invented conversation confirmed a data-recovery problem.',
                                     revision=rev(morgan)), actor=AGENT)

    # Less-crowded routes use the real importer and existing lifecycle, with wholly made-up evidence.
    for slug, route, contact in [('cover', 'interim_cover', 'Harper Cover'),
                                  ('leader', 'new_leader', 'Jamie Leader'),
                                  ('partner', 'delivery_partner', 'Avery Partner')]:
        url = f'https://{slug}.example.com/route'
        co = new(type='company', name=f'Example {slug.title()} Systems', url=f'https://{slug}.example.com')
        owner = new(type='person', name=contact, role='CTO', verification_level='primary-read',
                    evidence_urls=url, date=today.isoformat())
        link(owner, co, 'works_at')
        if route == 'interim_cover':
            vacancy = new(type='job', name='Head of Quality Engineering', organisation=f'Example {slug.title()} Systems',
                          engagement='permanent', date=recent, url=url, status='candidate',
                          summary='Permanent software quality engineering leadership and release readiness.')
            advert(vacancy, 'Permanent Head of Quality Engineering. Lead software release readiness and incident prevention.', url)
            link(vacancy, co, 'hiring_for')
        else:
            signal = dict(text=('The new CTO started on the stated date and leads software engineering and release readiness.'
                                if route == 'new_leader' else
                                'Our associate programme uses external software specialists for release readiness and integration delivery.'), url=url)
            signal.update(dict(kind='leadership_change', person_id=owner, started_on=recent) if route == 'new_leader' else
                          dict(kind='delivery_partner', accepts_specialists=True))
            record_imports.import_research_entries(conn, [dict(id=co, reviewed_at=today.isoformat(), source_date=recent,
                source_urls=[url], verification='primary-read', outreach_signals=[signal])], 'research:fixture-routes')
        if route == 'new_leader':
            introducer = new(type='person', name='Rory Introducer', relationship='dormant', kind='former_colleague')
            link(introducer, owner, 'introducer')
            # A separate current company remit supports the introducer's qualification question.
            research(co, 'The platform team is improving software release readiness and incident prevention.', url)

    # Prepared first-batch approaches use the same validated import as attended research.
    record_imports.import_research_entries(conn, [dict(id=rid, reviewed_at=today.isoformat(),
        source_urls=['https://northwind.example.com/careers'], summary='Synthetic preparation for the user-guide walkthrough.',
        approach=dict(role=role, points=['Ask how the engineering team is doing.', 'Discuss interim leadership needs.'],
                      ask=ask, cv=cv, goal='Agree one useful next conversation', wave=1, **({'last': last} if last else {})))
        for rid, role, cv, ask, last in [
            (alex, 'connector', 'Director', 'Who would be useful to speak with?',
             'Aug 2026, email: you swapped notes on the payments platform launch.'),
            (riley, 'recruiter', 'QE', "I'm available now for contract or interim work. Which role and client should we discuss?", '')]],
        'research:fixture-prepared')

    csv = ('First Name,Last Name,URL,Email Address,Company,Position,Connected On\n'
           'Alex,Example,https://www.linkedin.com/in/example-alex/,,Northwind Fintech,Head of Engineering,01 Jan 2020\n'
           'Riley,Test,https://www.linkedin.com/in/example-riley/,,Example Rail Ltd,Engineering Manager,02 Feb 2021\n'
           'Morgan,Dummy,https://www.linkedin.com/in/example-morgan/,,Acme Tech Recruitment,Consultant,03 Mar 2022\n'
           'Casey,Fixture,https://www.linkedin.com/in/example-casey/,,Northwind Fintech,CTO,04 Apr 2023\n')
    records.import_linkedin(conn, csv)
    counts = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('records', 'links', 'decisions', 'review_queue', 'drafts', 'draft_versions', 'outcomes', 'interactions', 'followups')}
    conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('db', type=Path)
    counts = build(parser.parse_args().db)
    print(', '.join(f'{k}={v}' for k, v in counts.items()))


if __name__ == '__main__':
    main()
