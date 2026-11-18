import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import refresh_views as views
import make_fixture
import json
import csv


def json_name(conn,qid):
    return json.loads(conn.execute('SELECT payload_json FROM review_queue WHERE id=?',(qid,)).fetchone()[0])['name']

class ViewTests(unittest.TestCase):
    def test_cv_choice_projection_is_consistent_before_and_after_migration(self):
        for version, choice in ((8, ''), (9, 'Manager')):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);migrations=root/'migrations';migrations.mkdir()
                for migration in views.wo.wodb.MIGRATIONS.glob('*.sql'):
                    if int(migration.name[:3])<=version:
                        (migrations/migration.name).write_text(migration.read_text())
                conn=views.wo.wodb.connect(root/'source.db');views.wo.wodb.migrate(conn,migrations)
                try:
                    conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES('J1','job','Engineering Manager','x','x')")
                    if choice:
                        conn.execute("UPDATE records SET cv_override=? WHERE id='J1'",(choice,))
                    views.refresh(conn,root)
                    exported=(root/'records/db/work-ops.json').read_bytes()
                    with (root/'career-directory.csv').open() as stream:
                        row=next(csv.DictReader(stream))
                    self.assertEqual(row['cv_override'],choice)
                    self.assertEqual(views.check(root),1)
                    self.assertEqual(views.wo.wodb.version(conn),version)
                    self.assertEqual((root/'records/db/work-ops.json').read_bytes(),exported)
                    payload=json.loads(exported)
                    self.assertEqual(payload['schema_version'],version)
                    self.assertEqual('cv_override' in payload['tables']['records'][0],version>=9)
                finally:
                    conn.close()

    def test_v6_export_parity_uses_a_migrated_copy_without_changing_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'old-migrations';old.mkdir()
            for migration in views.wo.wodb.MIGRATIONS.glob('*.sql'):
                if int(migration.name[:3])<7:
                    (old/migration.name).write_text(migration.read_text())
            conn=views.wo.wodb.connect(root/'v6.db')
            try:
                views.wo.wodb.migrate(conn,old)
                conn.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES('P1','person','Example','x','x')")
                conn.execute("INSERT INTO drafts(record_id,title,text) VALUES('P1','Note','Saved text')")
                views.refresh(conn,root)
                exported=(root/'records/db/work-ops.json').read_bytes()
                self.assertEqual(views.check(root),1)
                self.assertEqual((root/'records/db/work-ops.json').read_bytes(),exported)
                self.assertEqual(views.wo.wodb.version(conn),6)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='draft_versions'").fetchone())
            finally:
                conn.close()

    def test_edit_and_new_scan_publish_consistent_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);conn=views.wo.wodb.connect(root/'test.db');views.wo.wodb.migrate(conn)
            try:
                r=views.wo.store.create(conn,dict(type='person',name='Colleague'))['record']
                views.refresh(conn,root)
                self.assertEqual(views.check(root),1)
                views.wo.store.edit(conn,r['id'],dict(revision=1,known_context='Former colleague'))
                views.wo.store.create(conn,dict(type='job',name='New scan',status='candidate'))
                views.refresh(conn,root)
                self.assertEqual(views.check(root),2)
                text=(root/'career-directory.csv').read_text()
                self.assertIn('Former colleague',text);self.assertIn('New scan',text)
                self.assertNotIn('action_due',text)
                (root/'career-directory.csv').write_text(text+'stale\n')
                with self.assertRaises(ValueError): views.check(root)
            finally:conn.close()
    def test_the_committed_json_is_slim_and_still_proves_view_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);conn=views.wo.wodb.connect(root/'test.db');views.wo.wodb.migrate(conn)
            try:
                r=views.wo.store.create(conn,dict(type='job',name='Test Lead'))['record']
                views.wo.store.add_observation(conn,r['id'],'board:ashby:x','2026-09-27T05:30:00Z','{"kind": "job"}')
                views.refresh(conn,root)
                payload=json.loads((root/'records/db/work-ops.json').read_text())
                self.assertEqual(payload['scope'],'committed')
                self.assertEqual(set(payload['tables']),set(views.wo.COMMITTED_TABLES))
                self.assertNotIn('observations',payload['tables']);self.assertNotIn('state_events',payload['tables'])
                columns=(root/'career-directory.csv').read_text().splitlines()[0].split(',')
                self.assertFalse({'advert','linkedin','registry'} & set(columns))
                self.assertEqual(views.check(root),1)
                target=views.wo.wodb.connect(root/'restore.db');views.wo.wodb.migrate(target)
                try:
                    with self.assertRaises(ValueError): views.wo.load_dump(target,payload)  # a full restore stays strict
                    self.assertEqual(views.wo.load_dump(target,payload,partial=True)['records'],1)
                finally:target.close()
            finally:conn.close()
    def test_unaccepted_linkedin_candidates_stay_out_of_the_committed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);conn=views.wo.wodb.connect(root/'test.db');views.wo.wodb.migrate(conn)
            try:
                for first in ('Pending','Accepted','Rejected'):
                    views.wo.store.queue_linkedin_person(conn,{'First Name':first,'Last Name':'Person','URL':f'https://www.linkedin.com/in/{first.lower()}'})
                ids={json_name(conn,i):i for (i,) in conn.execute('SELECT id FROM review_queue')}
                views.wo.store.resolve_review(conn,ids['Accepted Person'],dict(resolution='accept',revision=1))
                views.wo.store.resolve_review(conn,ids['Rejected Person'],dict(resolution='reject',revision=1))
                views.refresh(conn,root)
                text=(root/'records/db/work-ops.json').read_text()
                self.assertIn('Accepted Person',text)
                self.assertNotIn('Pending Person',text);self.assertNotIn('Rejected Person',text)
                self.assertEqual(views.check(root),1)
            finally:conn.close()
    def test_synthetic_development_views_agree(self):
        # Personal exports are ignored by git; a fresh checkout must build its own test data.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'dev.db'
            counts=make_fixture.build(path)
            conn=views.wo.wodb.connect(path)
            try:
                views.refresh(conn,root)
                self.assertGreater(counts['records'],0)
                self.assertEqual(views.check(root),counts['records'])
            finally:conn.close()

if __name__=='__main__':unittest.main()
