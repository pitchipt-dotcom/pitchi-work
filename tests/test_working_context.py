"""Long-horizon synthetic workflow only; no real model or training case."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pitchi_work.working_context import WorkingContext, encoded
from pitchi_work.runner import HostDefect
from pitchi_work.phase_workflow import PhaseWorker, REFERENCE_POLICY
import test_reference_phase_integration as reference_tests
from test_reference_phase_integration import GenericWorker


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.context=WorkingContext(self.root/'history',self.root)

    def test_permanent_full_history_and_deterministic_bounded_projection(self):
        history=[{'number':i,'condition':'Retain 72 hours; do not delete without permission',
                  'text':'Generic observation '*1000} for i in range(50)]
        payload={'task_goal':'Inspect storage','rules':'Fixed schema; never change acceptance',
                 'contract':{'required_evidence':['original_hash'],'completion':'Host only'},
                 'current_evidence':{'detail':'Current source'},'host_observations':history}
        selected=self.context.select(payload,16384,2000)
        self.assertEqual(selected,self.context.select(payload,16384,2000))
        self.assertLess(len(encoded(selected))+1800+2000+512,16384*80//100)
        ref=selected['host_observations']['reference']
        self.assertEqual(self.context.read(ref),history)
        first=self.context.detail(ref,0)
        self.assertIn('72 hours',first['text'])
        self.assertEqual(first['detail_sha256'],hashlib.sha256(first['text'].encode()).hexdigest())
        self.assertEqual(selected['rules'],payload['rules'])
        self.assertEqual(selected['current_evidence'],payload['current_evidence'])
        self.assertEqual(selected['contract'],payload['contract'])

    def test_corrupt_and_unsafe_references_rejected(self):
        ref=self.context.persist({'value':'Exact original'})
        bad={**ref,'reference':'../outside.json'}
        with self.assertRaises(HostDefect):self.context.read(bad)
        target=self.root/ref['reference'];target.write_text('{}')
        with self.assertRaises(HostDefect):self.context.read(ref)

    def test_invalid_page_and_non_string_reference_are_rejected(self):
        from pitchi_work.research_handoff import HandoffContractError
        ref=self.context.persist({'value':'Original'})
        for page in (-1,True,100):
            with self.assertRaises(HandoffContractError):self.context.detail(ref,page)
        with self.assertRaises(HostDefect):self.context.read({**ref,'sha256':None})

    def test_required_information_is_not_removed_to_force_budget_pass(self):
        payload={'task_goal':'x'*16000,'rules':'Never drop the required objective',
                 'host_observations':[]}
        with self.assertRaises(HostDefect):self.context.select(payload,16384,2000)

    def catalog_payload(self, goal_bytes=0):
        return {'task_goal':'x'*goal_bytes,'rules':'Fixed acceptance and scope',
            'allowed_paths':['calc.py'], 'requested_detail':{'text':'Exact requested source'},
            'abstract_procedures':[{'procedure':{'steps':['Preserve conditions and exceptions']}}],
            'catalog_view':{'page':0,'page_count':9,'items':[
                {'index':i,'evidence_ids':['E'+str(i)],
                 'source':{'path':'calc.py','sha256':'a'*64,'start_line':i+1},
                 'finding':[],'conditions':[],'exceptions':[],'prohibitions':[],
                 'uncertainty':[],'unclassified':['E'+str(i)]} for i in range(4)]},
            'host_observations':[]}

    def test_catalog_priority_reduction_preserves_required_fields_and_receipt(self):
        payload=self.catalog_payload(7800)
        selected=self.context.select(payload,16384,2000)
        self.assertLessEqual(len(encoded(selected)),8795)
        for key in ('task_goal','rules','allowed_paths','requested_detail','abstract_procedures'):
            self.assertEqual(selected[key],payload[key])
        self.assertEqual([i['evidence_ids'] for i in selected['catalog_view']['items']],
                         [i['evidence_ids'] for i in payload['catalog_view']['items']])
        self.assertTrue(all(i['all_unclassified'] for i in selected['catalog_view']['items']))
        receipts=[json.loads(p.read_text()) for p in self.context.root.glob('*.json')]
        receipt=next(v for v in receipts if isinstance(v,dict) and v.get('operation')=='working_context_reduction')
        self.assertGreater(receipt['removed_bytes'],0)
        self.assertEqual(receipt['before_bytes']-receipt['after_bytes'],receipt['removed_bytes'])
        reopened=WorkingContext(self.context.root,self.root)
        self.assertEqual(reopened.read(receipt['original']),payload['catalog_view'])

    def test_catalog_reduction_still_rejects_mandatory_overflow(self):
        with self.assertRaises(HostDefect):self.context.select(self.catalog_payload(9000),16384,2000)

    def test_catalog_within_budget_and_semantic_classifications_unchanged(self):
        payload=self.catalog_payload()
        self.assertEqual(self.context.select(payload,16384,2000)['catalog_view'],payload['catalog_view'])
        payload=self.catalog_payload(7800)
        payload['catalog_view']['items'][0]['conditions']=['Keep 72 hours unless approval']
        selected=self.context.select(payload,16384,2000)
        self.assertEqual(selected['catalog_view']['items'][0]['conditions'],['Keep 72 hours unless approval'])

    def test_automatic_diagnostic_metadata_can_be_deferred_but_not_explicit_source(self):
        view={'kind':'diagnostic_source','path':'calc.py','source_sha256':'a'*64,
              'start_line':1,'end_line':2,'text':'Exact code and condition: retain 72 hours',
              'interpretation':'Automatic cue '*70,'test':'tests/test_calc.py'}
        payload={'task_goal':'x'*7800,'host_contract':{'scope':['calc.py'],'acceptance':'unchanged'},
                 'source_view':view,'host_observations':[]}
        selected=self.context.select(payload,16384,2000)
        self.assertLessEqual(len(encoded(selected)),8795)
        self.assertEqual(selected['host_contract'],payload['host_contract'])
        original=self.context.read(selected['source_view']['reference'])
        self.assertEqual(original,view)
        for key in ('path','source_sha256','start_line','end_line'):
            self.assertEqual(selected['source_view'][key],view[key])
        payload['source_view']={**view,'kind':'source'}
        with self.assertRaises(HostDefect):self.context.select(payload,16384,2000)

    def test_dispatch_envelope_reduces_only_redundant_navigation_prose(self):
        payload={'task_goal':'x'*9600,'host_observations':[],
            'diagnostic_access':{'read':'scoped source'},
            'operation_protocol':{'read':{'path':'exact path','page':'integer'}},
            'history_access':'Request exact immutable Host observation pages; no history was deleted. '*2,
            'catalog_access':'Read exact path/page; source_catalog pages readonly metadata. Write scope stays in host_contract.'}
        operation={'kind':'phase_execution'}
        selected,previous,ref=self.context.dispatch(payload,operation,None,None,16384,2000)
        self.assertEqual(selected['task_goal'],payload['task_goal'])
        self.assertEqual(selected['operation_protocol'],payload['operation_protocol'])
        self.assertLess(len(encoded(selected['history_access'])),len(encoded(payload['history_access'])))
        # Immutable full history remains available; no budget or rule changed.
        self.assertTrue(any(isinstance(load_value:=json.loads(p.read_text()),dict)
            and load_value.get('operation')=='working_context_reduction' for p in self.context.root.glob('*.json')))

    def test_only_empty_trial_history_can_be_evicted_with_envelope_budget(self):
        operation={'kind':'phase_execution'}
        feedback={'required':'Resume the unfinished call'}
        payload={'task_goal':'x','host_observations':[], 'diagnostic_access':{},
            'candidate_reasoning':{'recent_trials':[],'trial_access':None,'view_is_partial':True},
            'trial_review':{'warning_after':2,'force_after':4,'required':False}}
        payload['host_observations']={**self.context.observations([]),'recent':None,'view_is_partial':True}
        size=len(encoded(self.context.select(payload,16384,2000)))
        limit=16384*80//100-2000-512-256-len(encoded(
            {'operation':operation,'feedback':feedback,'previous_output':None}))
        payload['task_goal']='x'*(limit-size+21)
        selected,_,_=self.context.dispatch(payload,operation,feedback,None,16384,2000)
        self.assertIsNone(selected['candidate_reasoning'])
        self.assertEqual(selected['trial_review'],payload['trial_review'])
        payload['candidate_reasoning']['recent_trials']=[{'step':1}]
        with self.assertRaises(HostDefect):self.context.dispatch(payload,operation,feedback,None,16384,2000)

    def test_explicit_detail_retained_optional_history_evicted_not_erased(self):
        payload={'task_goal':'x'*3200,'rules':'fixed','current_evidence':{'detail':'x'*3000,'IDs':['E1']},
                 'requested_detail':{'text':'y'*3000},'prior_handoff':{'text':'z'*3000},
                 'host_observations':[{'text':'h'*8000}]}
        selected=self.context.select(payload,16384,2000)
        self.assertEqual(selected['requested_detail'],payload['requested_detail'])
        self.assertIsNone(selected['current_evidence']['detail'])
        self.assertEqual(selected['current_evidence']['IDs'],['E1'])
        self.assertIsNone(selected['prior_handoff'])
        self.assertEqual(self.context.read(selected['host_observations']['reference']),payload['host_observations'])


class LongHorizonIntegration(unittest.TestCase):
    def setUp(self):
        # Reuse the generic arithmetic workspace, NOT any stored mission/run.
        self.fixture=reference_tests.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.run=self.fixture.run;self.contract=self.fixture.contract
        self.source='def add(a, b):\n    return a - b\n# Preserve 72 hours unless approval; no removal.\n'
        material=''.join(
            '# Observation '+str(i)+': preserve 72 hours; unless approval; no removal. '+
            'generic context '*160+'\n' for i in range(40))
        (Path(self.run.state()['workspace'])/'calc.py').write_text(self.source)
        (Path(self.run.state()['workspace'])/'tests/test_calc.py').write_text(material)
        # Freeze the completed synthetic fixture before Host admission. Do not
        # authorize post-admission modification of a protected existing test.
        from pitchi_work.mission_controller import create_mission
        self.run=create_mission(self.fixture.root/'long_home',Path(self.run.state()['workspace']),
            'Inspect and repair the arithmetic module','http://127.0.0.1:1/v1','synthetic-only')
        self.feedback=[{'step':i,'a_condition':'72 hours unless approval; no removal',
                        'body':'Generic retained record '*200} for i in range(40)]

    def test_long_research_resume_fetch_first_evidence_and_hypothesis(self):
        class BrowseEarly(GenericWorker):
            def ask(self,role,prompt,**kwargs):
                packet=json.loads(prompt);name=packet['phase'];value=packet['input']
                if name=='research_37_view_0':
                    self.calls.append((name,packet,prompt,kwargs));return {'action':'catalog','page':0}
                if name=='research_37_view_1':
                    self.calls.append((name,packet,prompt,kwargs))
                    return {'action':'evidence','evidence_id':value['prior_evidence_catalog'][0]['evidence_ids'][0],'page':0}
                if name=='research_38_view_0':
                    self.calls.append((name,packet,prompt,kwargs));return {'action':'observations','page':0}
                return super().ask(role,prompt,**kwargs)
        model=BrowseEarly(interrupt='research_36_view_0')
        worker=PhaseWorker(self.run,self.contract,model,1,self.feedback)
        self.assertGreaterEqual(len(worker.source_pages),40)
        with self.assertRaises(RuntimeError):worker.execute()
        before=worker.reference('research_0')
        # Resume without supplying the huge observations again: Host reloads full state.
        resumed=PhaseWorker(self.run,self.contract,model,1)
        self.assertEqual(resumed.feedback,self.feedback)
        output=resumed.execute()
        self.assertEqual(before,resumed.reference('research_0'))
        self.assertEqual(sum(name=='research_0_view_0' for name,_,_,_ in model.calls),1)
        self.assertIn('return a + b',output['files']['calc.py'])
        late=next(p['input'] for name,p,_,_ in model.calls if name=='research_37_view_2')
        original=resumed.handoff.detail(resumed.handoff_packet('research_0'),
                     resumed.handoff_packet('research_0')['evidence_refs'][0])
        self.assertEqual(late['requested_detail']['detail'],original)
        self.assertIn('72 hours',original['text'])
        observed=next(p['input'] for name,p,_,_ in model.calls if name=='research_38_view_1')
        self.assertIn('72 hours',observed['requested_observations']['text'])
        for name,packet,prompt,kwargs in model.calls:
            self.assertLessEqual(len(prompt.encode())+512+kwargs['max_output_tokens'],16384*80//100)
            if name.startswith('research_'):
                self.assertLessEqual(len(packet['input']['prior_evidence_catalog']),4)
            self.assertEqual(packet['input']['working_context_version'],1)
        self.metrics={'research_steps':len(resumed.source_pages),
                      'worker_calls':len(model.calls),
                      'max_prompt_bytes':max(len(p.encode()) for _,_,p,_ in model.calls),
                      'max_full_guard':max(len(p.encode())+512+k['max_output_tokens'] for _,_,p,k in model.calls),
                      'guard_limit':16384*80//100,'persistent_observation_bytes':len(encoded(self.feedback)),
                      'early_evidence_refetched':True,'resume':True}
        manifest=resumed.cached('research_manifest')
        self.assertEqual(len(manifest['evidence_catalog']),len(resumed.source_pages))
        for index,page in enumerate(resumed.source_pages):
            packet=resumed.handoff_packet('research_'+str(index))
            primary=resumed.cached('research_'+str(index)).get('primary_evidence_ids',packet['evidence_refs'])[0]
            record=resumed.handoff.verify(next(r for r in packet['catalog'] if r['evidence_id']==primary))
            self.assertEqual(record['text'],page['text'])
        count=len(model.calls)
        self.assertEqual(output,PhaseWorker(self.run,self.contract,model,1).execute())
        self.assertEqual(count,len(model.calls))
        self.assertEqual((Path(self.run.state()['workspace'])/'calc.py').read_text(),self.source)

    def test_pinned_legacy_v2_dispatch_keeps_original_view(self):
        worker=PhaseWorker(self.run,self.contract,GenericWorker(),1)
        # Represent an old v2 attempt with packets but no working-view marker.
        (worker.base/'working_context_policy.json').unlink()
        (worker.base/'packets').mkdir()
        (worker.base/'packets/old.json').write_text('{}')
        resumed=PhaseWorker(self.run,self.contract,GenericWorker(),1)
        self.assertIsNone(resumed.working)
        self.assertIsNotNone(resumed.handoff)

    def test_frozen_history_mismatch_rejected_before_model(self):
        model=GenericWorker()
        worker=PhaseWorker(self.run,self.contract,model,1,self.feedback)
        ref=json.loads((worker.base/'working_observations.json').read_text())
        (self.run.path/ref['reference']).write_text('{}')
        with self.assertRaises(HostDefect):PhaseWorker(self.run,self.contract,model,1)
        self.assertEqual(model.calls,[])

    def test_historical_v2_pinned_calls_resume_without_reprojection(self):
        worker=PhaseWorker(self.run,self.contract,GenericWorker(),1)
        (worker.base/'working_context_policy.json').unlink()
        # Simulate the pre-working-context implementation's dispatch construction.
        worker.working=None
        worker.research_reference()
        refs=[worker.reference('research_'+str(i)) for i in range(len(worker.source_pages))]
        model=GenericWorker()
        resumed=PhaseWorker(self.run,self.contract,model,1)
        self.assertIsNone(resumed.working)
        resumed.research_reference()
        self.assertEqual(refs,[resumed.reference('research_'+str(i)) for i in range(len(worker.source_pages))])
        self.assertEqual(model.calls,[])


if __name__=='__main__':unittest.main()
