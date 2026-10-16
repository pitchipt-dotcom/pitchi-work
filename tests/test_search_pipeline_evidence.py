"""Synthetic search observations; no training concepts/results or live searches."""
import copy
import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pitchi_work.search_evidence import pipeline, compare, record, compare_tool
from pitchi_work.tools import execute_tool, catalog
from pitchi_work.store import dump, load
from pitchi_work.runner import HostDefect
from pitchi_work import cli

def observation():
    return {'queries':[{'query':'topic_a action_b','normalized_query':'topic_a action_b',
        'normalization':{'interpretations':[]},'concepts':[{'id':'a','aliases':['topic_a','alias_a']},{'id':'b','aliases':['action_b']}],
        'metadata_expansion':[],'guideline_concept_groups':[],
        'retrieval_stages':[{'stage':'exact','candidate_count':4,'admitted_new':2,'fts':'group_a AND group_b','relaxation_level':0},
                            {'stage':'related','candidate_count':3,'admitted_new':1,'fts':'group_a AND related_b','relaxation_level':1}],
        'retrieval_counts':{'documents':2},
        'provider_candidates':{'documents':[{'id':'x','title':'topic_a action_b','source_url':'https://example.invalid/x','summary':'source excerpt'},
                                           {'id':'y','title':'topic_a','source_url':'https://example.invalid/y'}]},
        'provider_top':{'documents':[{'id':'y'},{'id':'x'}]},
        'final_ranking':[{'id':'y','rank':1},{'id':'x','rank':2}]}]}

class PipelineEvidence(unittest.TestCase):
    def test_stages_groups_candidates_and_rank_are_separate(self):
        p=pipeline(observation());q=p['queries'][0]
        self.assertEqual(q['stages'][0]['candidate_count'],4)
        self.assertEqual(q['sources']['documents']['observed_provider_count'],2)
        self.assertEqual(q['stages'][1]['relaxation_level'],1)
        self.assertEqual(q['sources']['documents']['display_ids'],['y','x'])
        self.assertEqual(q['concepts'][0]['aliases'],['topic_a','alias_a'])
        self.assertFalse(p['completion_authority'])

    def test_before_after_control_quality_not_self_certified(self):
        before=pipeline(observation());raw=observation();raw['queries'][0]['provider_top']['documents'].reverse()
        after=pipeline(raw);result=compare(before,after,['topic_a action_b'])
        source=result['observations'][0]['sources'][0]
        self.assertEqual(source['before_display_ids'],['y','x']);self.assertEqual(source['after_display_ids'],['x','y'])
        self.assertEqual(source['before_candidates'][0]['title'],'topic_a action_b')
        self.assertEqual(source['after_candidates'][0]['source_url'],'https://example.invalid/x')
        self.assertTrue(result['observations'][0]['normalization_observed_both'])
        self.assertFalse(result['is_validator_pass']);self.assertFalse(result['is_mission_completed'])
        with self.assertRaises(ValueError):compare(before,after,['unknown'])
        duplicate=copy.deepcopy(before);duplicate['queries'].append(duplicate['queries'][0])
        with self.assertRaises(ValueError):compare(duplicate,after)

    def test_hash_provenance_matches_saved_host_observations(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);run=SimpleNamespace(path=root)
            dump(root/'artifacts/source.json',{'observations':observation()})
            (root/'collector.py').write_text('# Host collector');(root/'data.bin').write_bytes(b'generic-source')
            ref=record(run,'before',observation(),'artifacts/source.json',root/'collector.py',root/'data.bin')
            result=load(root/ref['reference']);self.assertEqual(result['provenance']['authority'],'host')
            self.assertTrue(result['provenance']['data_sha256'])
            wrong=observation();wrong['queries'][0]['query']='different'
            with self.assertRaises(HostDefect):record(run,'after',wrong,'artifacts/source.json',root/'collector.py',root/'data.bin')

    def test_missing_counts_and_bad_files_are_not_pass(self):
        bad=observation();bad['queries'][0]['retrieval_counts']['documents']=9
        with self.assertRaises(ValueError):pipeline(bad)
        bad=observation();bad['queries'][0]['retrieval_stages'][0]['candidate_count']=-1
        with self.assertRaises(ValueError):pipeline(bad)
        self.assertFalse(compare_tool({'before':'/missing.json','after':'/missing.json'})['ok'])
        unknown=observation();del unknown['queries'][0]['normalized_query']
        projected=pipeline(unknown)
        self.assertFalse(compare(projected,projected)['observations'][0]['normalization_observed_both'])

    def test_cli_and_registry_use_same_readonly_comparison(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);a=root/'before.json';b=root/'after.json';dump(a,observation());dump(b,observation())
            result=execute_tool('search.pipeline_compare',{'before':str(a),'after':str(b)})
            self.assertTrue(result['ok']);self.assertFalse(catalog()['search.pipeline_compare']['network'])
            with patch('pitchi_work.cli.foreground_tool',return_value=0) as fg,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['tool','search.pipeline_compare','--before',str(a),'--after',str(b),'--control','topic_a action_b']),0)
            self.assertEqual(fg.call_args.args[1],'search.pipeline_compare')
            self.assertEqual(fg.call_args.args[2]['controls'],['topic_a action_b'])

if __name__=='__main__':unittest.main()
