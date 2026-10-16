import copy
import unittest
from pathlib import Path
from pitchi_work.search_link_coverage import check_gold, validate_links
from pitchi_work.mission_profiles import UnsupportedMission
from pitchi_work.store import load
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class LinkGoldTests(unittest.TestCase):
    def setUp(self):
        self.gold=load(ROOT/'tests/fixtures/metadata/aortic-gold.json')
        self.request=' '.join(q['query'] for q in self.gold['queries'])

    def test_unfrozen_or_missing_official_expectation_rejected(self):
        for field,value in [('frozen_before_implementation',False),('required_mode','full_text')]:
            gold=copy.deepcopy(self.gold);gold[field]=value
            with self.assertRaises(UnsupportedMission):check_gold(gold,self.request)
        gold=copy.deepcopy(self.gold);gold['queries'][0]['expected']['official_url']=''
        with self.assertRaises(UnsupportedMission):check_gold(gold,self.request)

    def test_nonacceptance_queries_need_no_fabricated_expectation(self):
        self.assertIs(check_gold(self.gold,self.request),self.gold)

    def test_hit_count_without_bibliography_never_passes(self):
        from types import SimpleNamespace
        rows=[{'query':q['query'],'provider_top':{'guidelines':[{'id':'metadata:jcs-2020-aortic','guideline_title':'wrong'}]},'final_ranking':[]} for q in self.gold['queries'] if q['acceptance']]
        rows+=[{'query':q,'provider_top':{'guidelines':[]},'final_ranking':[]} for q in self.gold['holdout']]
        run=SimpleNamespace(path=ROOT)
        with patch('pitchi_work.search_link_coverage.protected_gold',return_value=self.gold),patch('pitchi_work.search_link_coverage.canonical_metadata',return_value={}),patch('pitchi_work.search_link_coverage.sha',return_value='hash'):
            checks,_=validate_links(run,{'queries':rows},{'queries':rows})
        self.assertTrue(all(not c['pass'] for c in checks if c['validator'].startswith('gold_link:')))

if __name__=='__main__':unittest.main()
