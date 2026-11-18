"""Working attention is bounded without rewriting personal records or losing alternatives."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import work_selection

POLICY = {'working_limit': 10, 'additions_per_seven_days': 10, 'qualification_days': 14}
TODAY = dt.date(2026, 10, 29)


def row(n, score=80, **changes):
    r = dict(id=f'J{n:03}', type='job', name='Engineering Manager', status='candidate',
             source_file='source:board:example', created_at='2026-10-28T10:00:00Z',
             date='2026-10-28', availability={'status':'open'},
             relevance={'score':score, 'band':'strong', 'qualification':{'eligible':True}})
    r.update(changes)
    return r


def project(rows, **kwargs):
    state = dict(records=rows, intelligence={'actions':[], 'research':[]}, discovery_capacity=POLICY)
    before = copy.deepcopy(state)
    result = work_selection.build(state, today=TODAY, **kwargs)
    assert state == before
    return result


class CapacityTests(unittest.TestCase):
    def test_strongest_ten_win_regardless_of_input_order_and_the_rest_remain(self):
        rows = [row(i, 80+i) for i in range(30)]
        result = project(rows)
        self.assertEqual([x['record_id'] for x in result['discovery']], [f'J{i:03}' for i in range(29,19,-1)])
        self.assertEqual(len(result['other']),20)
        self.assertTrue(all(x['rule']=='other.capacity' for x in result['other']))
        self.assertEqual(result, project(list(reversed(rows))))

    def test_qualification_and_recommendations_share_ten_places_but_saved_work_does_not(self):
        rows = [row(i) for i in range(4)] + [row(i, source_file='source:alert:linkedin',
            relevance={'score':40,'band':'possible','fit':{'status':'unconfirmed'}}, availability={}) for i in range(4,20)]
        rows += [row(100,status='pursue'),row(101,status='interview')]
        result=project(rows)
        self.assertEqual(len(result['discovery']),4)
        self.assertEqual(len([x for x in result['other'] if x['rule']=='qualify.advert']),6)
        self.assertEqual([x['record_id'] for x in result['shortlisted']],['J100'])
        self.assertEqual([x['record_id'] for x in result['active']],['J101'])
        self.assertEqual(set(result['research_ids']),{f'J{i:03}' for i in range(10)} | {'J100','J101'})

    def test_old_unselected_uncertainty_expires_but_new_evidence_and_personal_work_survive(self):
        unknown=dict(source_file='source:alert:linkedin',created_at='2026-10-01T00:00:00Z',
                     relevance={'score':40,'band':'possible','fit':{'status':'unconfirmed'}},availability={})
        rows=[row(1,**unknown),row(2,status='pursue',**unknown),row(3,created_at='2026-10-01T00:00:00Z')]
        result=project(rows)
        self.assertEqual(next(x for x in result['other'] if x['record_id']=='J001')['rule'],'other.capacity_stale')
        self.assertEqual(result['needs_checking'],[])
        self.assertEqual([x['record_id'] for x in result['shortlisted']],['J002'])
        self.assertEqual([x['record_id'] for x in result['discovery']],['J003'])

    def test_reference_returns_only_when_current_evidence_supports_it(self):
        old=row(1,created_at='2026-09-01',source_file='source:research:manual',
                relevance={'score':40,'band':'possible','fit':{'status':'unconfirmed'}},availability={})
        self.assertEqual(project([old])['research_ids'],[])
        old['updated_at']='2026-10-29';old['last_seen_at']='2026-10-29'
        self.assertEqual(project([old])['research_ids'],[])
        supported=dict(old,relevance={'score':80,'band':'strong','qualification':{'eligible':True}},availability={'status':'open'})
        self.assertEqual([r['record_id'] for r in project([supported])['discovery']],['J001'])


class ResearchPoolTests(unittest.TestCase):
    def test_research_can_reach_candidates_outside_ten_visible_places(self):
        rows=[row(i,source_file='source:alert:linkedin',relevance={'score':40,'band':'possible','fit':{'status':'unconfirmed'}},availability={}) for i in range(50)]
        state=dict(records=rows,intelligence={'actions':[], 'research':[]},discovery_capacity={**POLICY,'research_pool_limit':30},research_attempts={f'J{i:03}':{'attempted_at':'2026-10-28T10:00:00Z'} for i in range(10)})
        result=work_selection.build(state,today=TODAY)
        self.assertEqual(len(result['research_pool']),30)
        self.assertIn('J010',result['research_ids'])
        self.assertLessEqual(len(result['discovery'])+len(result['needs_checking']),10)
        self.assertLessEqual(len(result['research_ids']),10)
        self.assertEqual(len(result['other']),50)

    def test_current_open_source_keeps_old_unresolved_role_researchable(self):
        r=row(1,created_at='2026-09-01',source_file='source:board:example',relevance={'score':40,'band':'possible','fit':{'status':'unconfirmed'}},availability={'status':'open'})
        result=project([r])
        self.assertIn('J001',result['research_ids'])
