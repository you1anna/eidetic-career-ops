"""The ranking diff used before a relevance change ships; synthetic snapshots and a synthetic database."""
import datetime as dt
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'workspace'))
import db
import rank_diff
import records


def advert(score, band, family='-', lane='contract', name='Role'):
    return dict(name=name, type='job', score=score, band=band, lane=lane, family=family, title_reason='')


def week(built_at, research_ids, discovery=(), other=(), needs_checking=()):
    projection = {key: [] for key in ('discovery', 'shortlisted', 'active', 'closed', 'other',
                                      'act_now', 'waiting', 'in_progress', 'needs_checking')}
    projection.update(discovery=list(discovery), other=list(other), needs_checking=list(needs_checking),
                      research_ids=research_ids)
    names = {rid: f'Record {rid}' for rid in 'ABCD'}
    return {'adverts': {}, 'built_at': built_at, 'selection': dict(projection=projection, names=names, error='')}


class RankDiffTests(unittest.TestCase):
    def test_compare_reports_movers_and_a_qe_contract_that_fell_to_low(self):
        before = {'adverts': {'Q': advert(47, 'possible'), 'L': advert(55, 'strong'), 'S': advert(60, 'strong')}}
        after = {'adverts': {'Q': advert(10, 'low', 'quality'), 'L': advert(55, 'strong', 'leadership'),
                             'S': advert(45, 'possible', 'leadership', name='EM - Embedded')}}
        out = rank_diff.compare(before, after)
        self.assertEqual(out['fell'], [('Q', 'Role')])
        self.assertEqual([m[1] for m in out['movers']], ['Q', 'S'])
        self.assertEqual(out['table'][('quality', 'after', 'low')], 1)
        self.assertEqual(out['top_after']['leadership'], 2)

    def test_compare_reports_this_week_section_by_section_and_every_changed_placement(self):
        ref = lambda rid, rule, **extra: dict(record_id=rid, rule=rule, lane='contract', **extra)
        before = week('2026-10-29T09:00:00+00:00', ['A', 'B', 'C'],
                      discovery=[ref('A', 'discovery.advert', worth_a_look=True), ref('B', 'discovery.lead', worth_a_look=True)],
                      other=[ref('C', 'qualify.advert')], needs_checking=[ref('C', 'qualify.advert')])
        after = week('2026-10-29T11:30:00+00:00', ['C', 'A', 'D'],
                     discovery=[ref('A', 'discovery.advert', worth_a_look=False), ref('D', 'discovery.lead', worth_a_look=True)],
                     other=[ref('B', 'other.lead'), ref('C', 'qualify.advert')], needs_checking=[ref('C', 'qualify.advert')])
        out = rank_diff.compare(before, after)['selection']
        self.assertEqual([(s['title'], s['before'], s['after'], s['entered'], s['left']) for s in out['sections']], [
            ('Act now', 0, 0, [], []), ('Best opportunities', 2, 1, ['D'], ['A', 'B']),
            ('Needs checking', 1, 1, [], []), ('In progress', 0, 0, [], []), ('Waiting', 0, 0, [], []),
            ('Research targets', 3, 3, ['D'], ['B'])])
        self.assertEqual(out['moved'], [('B', ('discovery', 'discovery.lead'), ('other', 'other.lead')),
                                        ('D', None, ('discovery', 'discovery.lead'))])
        text = rank_diff.report(rank_diff.compare(before, after))
        self.assertIn('Snapshots were built 2.5 h apart', text)
        self.assertIn('Best opportunities: 1 entered, 2 left', text)
        self.assertIn('  - A  Record A  (still discovery · discovery.advert)', text)
        self.assertIn('  - B  Record B  (discovery · discovery.lead → other · other.lead)', text)
        self.assertIn('  + D  Record D  (not placed → discovery · discovery.lead)', text)
        self.assertIn('      1  discovery · discovery.lead → other · other.lead  e.g. B', text)
        self.assertNotIn('This week', rank_diff.report(rank_diff.compare({'adverts': {}}, {'adverts': {}})))

    def test_a_revision_is_extracted_without_touching_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, into = Path(tmp) / 'repo', Path(tmp) / 'code'
            (repo / 'workspace').mkdir(parents=True)
            (repo / 'workspace' / 'records.py').write_text('OLD = True\n')
            git = lambda *args: subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout
            git('init', '-q')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', 'old')
            (repo / 'workspace' / 'records.py').write_text('OLD = False\n')
            sha = rank_diff.extract('HEAD', into, repo)
            self.assertEqual(sha, git('rev-parse', 'HEAD').strip())
            self.assertEqual((into / 'workspace' / 'records.py').read_text(), 'OLD = True\n')
            self.assertEqual((repo / 'workspace' / 'records.py').read_text(), 'OLD = False\n')

    def test_one_process_snapshots_only_one_code_version(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, 'fresh process'):
            rank_diff.snapshot(Path(tmp) / 't.db', dt.date(2026, 9, 28), Path(tmp))

    def test_snapshot_reads_active_adverts_from_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 't.db'
            conn = db.connect(path)
            db.migrate(conn)
            records.create(conn, dict(type='job', name='Interim Head of Engineering', engagement='interim',
                                      location='London', date='2026-09-20'))
            records.create(conn, dict(type='person', name='Someone'))
            conn.close()
            day = dt.date(2026, 9, 28)
            snap, again = rank_diff.snapshot(path, day), rank_diff.snapshot(path, day)
            later = rank_diff.snapshot(path, dt.date(2026, 12, 1))
        self.assertEqual(len(snap['adverts']), 1)
        self.assertEqual(snap['adverts'], again['adverts'])  # the same date gives the same scores
        projection = snap['selection']['projection']
        placed = [ref['record_id'] for group in rank_diff.GROUPS for ref in projection[group]]
        self.assertEqual(placed, list(snap['adverts']))
        self.assertEqual(snap['selection']['names'][placed[0]], 'Interim Head of Engineering')
        self.assertTrue(dt.datetime.fromisoformat(snap['built_at']).tzinfo)
        row = next(iter(snap['adverts'].values()))
        self.assertEqual((row['lane'], row['band'] in ('strong', 'possible', 'low')), ('contract', True))
        self.assertLess(next(iter(later['adverts'].values()))['score'], row['score'])  # freshness moved with the date


if __name__ == '__main__':
    unittest.main()
