"""Provider parity, immutable email age and bounded source health, on synthetic records."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import db
import records
import record_views
import relevance
from test_work_selection import job, project


class EmailSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(Path(self.tmp.name) / 'email.db')
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def test_source_is_email_not_listing_and_repeat_does_not_renew_age(self):
        row = records.create(self.conn, dict(type='job', name='Engineering Lead', organisation='Example',
                    url='https://app.jackandjill.ai/jobs/11111111-1111-4111-8111-111111111111/post'))['record']
        for source in ('alert:linkedin', 'alert:jackandjill'):
            with self.subTest(source=source):
                for at in ('2026-11-02T09:00:00Z', '2026-11-05T09:00:00Z'):
                    records.add_observation(self.conn, row['id'], source, at, json.dumps(dict(
                        first_seen_at='2026-11-02T09:00:00Z', current=dict(url=row['url'], received_at=at,
                        email_summary='A strong match. Lead software teams and own release readiness.'))))
                projected = record_views.load_records(self.conn, row['id'])[0]
                self.assertEqual(projected['source_context'], dict(source=source, url=row['url'],
                                 at='2026-11-02T09:00:00Z', basis='email'))
                self.assertNotEqual(projected['availability']['status'], 'open')
                self.assertFalse(projected['advert'])
                self.assertNotIn('A strong match', json.dumps(projected))

    def test_same_facts_have_fixed_score_and_section_for_both_sources(self):
        for source, label in (('alert:linkedin','LinkedIn email'), ('alert:jackandjill','Jack and Jill email')):
            with self.subTest(source=source):
                row=job('J','unknown',source_file='source:'+source,date='',summary='',location='London',
                        source_context=dict(source=source,url='https://example.test/role',at='2026-11-05T09:00:00Z',basis='email'))
                row['advert']={'email_summary':'A strong match. Lead software engineering teams and improve release readiness.'}
                state=dict(records=[row],links=[])
                row['relevance']=relevance.score(state,today=dt.date(2026,11,5))['J']
                self.assertEqual((row['relevance']['score'],row['relevance']['band']), (40,'possible'))
                self.assertEqual(row['relevance']['fit']['status'],'unconfirmed')
                result=project([row],intelligence={'actions':[],'research':[]})
                self.assertEqual(result['discovery'],[])
                self.assertEqual([r['record_id'] for r in result['needs_checking']],['J'])
                self.assertEqual(result['needs_checking'][0]['source']['label'],label)
                # A source-independent, confirmed recommendation has the same ranking input.
                row['advert']={}
                row['summary']='Lead software engineering teams and improve release readiness.'
                row['availability']={'status':'open','basis':'research','at':'2026-11-05T09:30:00Z'}
                row['relevance']=relevance.score(state,today=dt.date(2026,11,5))['J']
                self.assertEqual((row['relevance']['score'],row['relevance']['band']), (140,'strong'))
                self.assertEqual([r['record_id'] for r in project([row],intelligence={'actions':[],'research':[]})['discovery']],['J'])

    def test_jack_alert_without_fit_fields_is_still_a_research_candidate(self):
        row=job('J',source_file='source:alert:jackandjill')
        row['relevance'].update(score=40,band='possible')
        self.assertEqual(project([row],intelligence={'actions':[],'research':[]})['research_ids'],['J'])

    def test_per_source_failure_and_last_processed_survive_empty_polls_without_private_fields(self):
        def add(at,sources,status='ok'):
            receipt=dict(started_at=at,finished_at=at,status=status,sources=sources,processing={'messages':1})
            with self.conn:
                self.conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                                  ('email:'+at,at[:10],status,json.dumps(receipt)))
        add('2026-11-05T09:00:00Z',{'alert:jackandjill':{'state':'ok','messages':1,'jobs':5,'raw':'private body'}})
        add('2026-11-05T09:15:00Z',{'alert:jackandjill':{'state':'error','messages':0,'quarantined':1,'last_alert_processed':'2026-11-05T09:00:00Z'},
                                 'alert:linkedin':{'state':'ok','messages':1,'jobs':3}},'partial')
        result=record_views.email_status(self.conn)
        self.assertEqual(result['sources']['alert:jackandjill']['state'],'error')
        self.assertEqual(result['sources']['alert:jackandjill']['last_alert_processed'],'2026-11-05T09:00:00Z')
        self.assertNotIn('private body',json.dumps(result))


if __name__ == '__main__':
    unittest.main()
