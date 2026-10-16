"""Generic integration of real PhaseWorker/Host compiler; synthetic adapter only."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pitchi_work.mission_controller import create_mission
from pitchi_work.mission_profiles import inventory, bind_requirements, compile_contracts, sha
from pitchi_work.phase_workflow import PhaseWorker, POLICY, REFERENCE_POLICY, digest
from pitchi_work.research_handoff import CATEGORIES, POLICY as HANDOFF_POLICY, encoded
from pitchi_work.model import ModelFailure
from pitchi_work.runner import HostDefect


class GenericWorker:
    def __init__(self, interrupt=None, invalid=False, endless=False):
        self.calls=[];self.interrupt=interrupt;self.invalid=invalid;self.endless=endless

    def ask(self, role, prompt, **kwargs):
        packet=json.loads(prompt);phase=packet['phase'];value=packet['input']
        self.calls.append((phase,packet,prompt,kwargs))
        if phase==self.interrupt:
            self.interrupt=None
            raise RuntimeError('synthetic process interruption')
        if phase.startswith('research_'):
            if phase=='research_1_view_0':
                return {'action':'evidence','evidence_id':value['prior_evidence_catalog'][0]['evidence_ids'][0],'page':0}
            proposal={f:[] for f in CATEGORIES}
            proposal['evidence_refs']=value.get('reference_protocol',{}).get(
                'allowed_evidence_ids',value['current_evidence']['handoff']['evidence_refs'])
            # Intentionally classify nothing: retention must not depend on completeness.
            return proposal
        if phase.startswith('hypothesis_view_'):
            if self.invalid:return {'action':'evidence','evidence_id':'E'+'0'*24,'page':0}
            if value['requested_detail'] is None or self.endless:
                return {'action':'evidence','evidence_id':value['catalog_view']['items'][0]['evidence_ids'][0],'page':0}
            return {'hypothesis':'Correct the arithmetic operator','evidence_indices':[0],'paths':['calc.py']}
        if phase=='edit_0':
            if getattr(self,'legacy_edit',False):return {'action':'read','path':'calc.py','page':0}
            source=next(s for s in value['sources'] if s['path']=='calc.py')
            return {'action':'edit_target','path':'calc.py','source_sha256':source['source_sha256'],
                    'start_line':2,'end_line':2}
        if phase=='edit_1':
            if getattr(self,'legacy_edit',False):
                source=next(s for s in value['sources'] if s['path']=='calc.py')
                return {'action':'patch','path':'calc.py','source_sha256':source['source_sha256'],
                    'start_line':2,'end_line':2,'replacement':'    return a + b\n','summary':'Correct operator'}
            return {'action':'replace_target','target_id':value['source_view']['edit_target']['target_id'],
                    'new_text':'    return a + b\n'}
        return {'action':'done','summary':'Staged only; Host acceptance still required'}


class ReferencePhaseIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);source=self.root/'project';source.mkdir()
        self.original='def add(a, b):\n    return a - b\n'+(
            '# Storage: three hours. Extension only before deadline and without next reservation. No unauthorized removal.\n'*90)
        (source/'calc.py').write_text(self.original)
        (source/'tests').mkdir()
        (source/'tests/test_calc.py').write_text(
            'import unittest\nfrom calc import add\nclass AddTest(unittest.TestCase):\n'
            '    def test_add(self): self.assertEqual(add(2,3),5)\n')
        self.run=create_mission(self.root/'home',source,'Inspect and repair the arithmetic module',
            'http://127.0.0.1:1/v1','synthetic-only')
        self.catalog=inventory(Path(self.run.state()['workspace']))
        self.req=bind_requirements({'profile':'bug_fix','targets':['calc.py'],
            'requirements':['Inspect','repair'],'needs_human':False},
            'Inspect and repair the arithmetic module',self.catalog)
        self.coarse={'tasks':[{'requirement_ids':['M1','M2'],'targets':['calc.py'],'depends_on':[]}]}
        self.contract=compile_contracts(self.coarse,self.req,self.catalog,research_handoff_version=2)['tasks'][0]

    def worker(self, model, *, legacy_edit=False):
        if legacy_edit:
            # Explicitly exercise the saved pre-target-only contract generation.
            from pitchi_work.store import dump
            base=self.run.path/'checkpoints/P1/1';base.mkdir(parents=True,exist_ok=True)
            dump(base/'edit_protocol.json',{'version':1})
            model.legacy_edit=True
        return PhaseWorker(self.run,self.contract,model,1)

    def test_multiple_research_steps_handoff_to_hypothesis_and_staged_edit(self):
        model=GenericWorker();worker=self.worker(model);output=worker.execute()
        self.assertGreaterEqual(len(worker.source_pages),3)
        manifest=worker.cached('research_manifest')
        self.assertNotIn('observations',manifest)
        self.assertEqual(len(manifest['evidence_catalog']),len(worker.source_pages))
        for i,page in enumerate(worker.source_pages):
            packet=worker.handoff_packet('research_'+str(i))
            self.assertEqual(set(packet['unclassified']),set(packet['evidence_refs']))
            detail=worker.handoff_detail(['research_'+str(i)],packet['evidence_refs'][0],0)
            expected=worker.handoff.detail(packet,packet['evidence_refs'][0])
            self.assertEqual(detail['detail']['text'],expected['text'])
            self.assertEqual(detail['source']['sha256'],page['sha256'])
            self.assertEqual(detail['detail']['source_sha256'],sha_text(page['text']))
            self.assertNotIn(page['text'],encoded(packet).decode())
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertEqual((Path(self.run.state()['workspace'])/'calc.py').read_text(),self.original)
        self.assertEqual(self.run.state()['status'],'READY')
        self.assertFalse(worker.handoff.read('checkpoints/research_0.receipt.json')['is_task_pass'])
        prior=next(p['input']['requested_detail'] for name,p,_,_ in model.calls if name=='research_1_view_1')
        self.assertIn('three hours',prior['detail']['text'])
        hypothesis=next(p['input'] for name,p,_,_ in model.calls if name=='hypothesis_view_1')
        self.assertIn('without next reservation',hypothesis['requested_detail']['detail']['text'])
        self.assertIn('No unauthorized removal',hypothesis['requested_detail']['detail']['text'])
        self.assertEqual(worker.cached('hypothesis')['evidence_indices'],[0])

    def test_resume_preserves_checkpoints_and_does_not_repeat_research(self):
        model=GenericWorker(interrupt='research_2_view_0');worker=self.worker(model)
        with self.assertRaises(RuntimeError):worker.execute()
        original_refs=[worker.reference('research_'+str(i)) for i in range(2)]
        before=sum(name.startswith('research_0') or name.startswith('research_1') for name,_,_,_ in model.calls)
        restarted=self.worker(model);restarted.execute()
        after=sum(name.startswith('research_0') or name.startswith('research_1') for name,_,_,_ in model.calls)
        self.assertEqual(before,after)
        self.assertEqual(original_refs,[restarted.reference('research_'+str(i)) for i in range(2)])
        count=len(model.calls)
        self.assertEqual(restarted.execute(),self.worker(model).execute())
        self.assertEqual(count,len(model.calls))

    def test_hypothesis_resume_replays_fetch_without_losing_context(self):
        model=GenericWorker(interrupt='hypothesis_view_1');worker=self.worker(model)
        with self.assertRaises(RuntimeError):worker.execute()
        checkpoint=worker.reference('hypothesis_view_0')
        output=self.worker(model).execute()
        self.assertEqual(checkpoint,worker.reference('hypothesis_view_0'))
        self.assertEqual(sum(name=='hypothesis_view_0' for name,_,_,_ in model.calls),1)
        self.assertIn('return a + b',output['files']['calc.py'])

    def test_context_and_packets_are_finite_and_description_free(self):
        model=GenericWorker();worker=self.worker(model);worker.execute()
        for name,packet,prompt,kwargs in model.calls:
            self.assertLessEqual(len(prompt.encode())+512+kwargs['max_output_tokens'],REFERENCE_POLICY['context_tokens']*80//100)
            if name.startswith('research_'):
                self.assertNotIn('400',packet['input']['rules'])
                self.assertNotIn('explanation',packet['input']['rules'])
                if packet['input']['prior_handoff'] is not None:
                    self.assertIsNone(packet['input']['prior_handoff']['detail'])
            if name.startswith('hypothesis_view_'):
                self.assertLessEqual(len(packet['input']['catalog_view']['items']),4)
                self.assertNotIn('observations',packet['input'])
        for i in range(len(worker.source_pages)):
            self.assertLessEqual(len(encoded(worker.handoff_packet('research_'+str(i)))),HANDOFF_POLICY['max_packet_bytes'])

    def test_unknown_worker_reference_fails_contract_finitely(self):
        model=GenericWorker(invalid=True);worker=self.worker(model)
        with self.assertRaises(ModelFailure) as failure:worker.execute()
        self.assertEqual(failure.exception.failure_class,'PHASE_CONTRACT_UNSATISFIED')
        self.assertEqual(sum(name=='hypothesis_view_0' for name,_,_,_ in model.calls),POLICY['call_attempts'])
        self.assertIsNone(worker.cached('hypothesis'))

    def test_hash_mismatch_is_host_defect_before_any_resumed_worker_request(self):
        model=GenericWorker();worker=self.worker(model);worker.execute()
        packet=worker.handoff_packet('research_0')
        target=worker.handoff.path(packet['catalog'][0]['reference'])
        target.write_text('{}')
        count=len(model.calls)
        with self.assertRaises(HostDefect):self.worker(model).execute()
        self.assertEqual(len(model.calls),count)

    def test_detail_requests_have_finite_limit_not_worker_repair(self):
        model=GenericWorker(endless=True);worker=self.worker(model)
        with self.assertRaises(ModelFailure) as failure:worker.execute()
        self.assertEqual(failure.exception.failure_class,'PHASE_HANDOFF_LIMIT')
        self.assertEqual(sum(name.startswith('hypothesis_view_') for name,_,_,_ in model.calls),REFERENCE_POLICY['max_hypothesis_handoff_calls'])

    def test_legacy_contract_remains_identical_except_explicit_new_policy(self):
        legacy=compile_contracts(self.coarse,self.req,self.catalog)['tasks'][0]
        self.assertEqual(legacy['phase_policy'],POLICY)
        self.assertEqual(self.contract['phase_policy'],REFERENCE_POLICY)
        old=copy.deepcopy(legacy);new=copy.deepcopy(self.contract)
        for item in (old,new):item.pop('sha256');item.pop('phase_policy')
        self.assertEqual(old,new)
        from test_phase_workflow import Model
        worker=PhaseWorker(self.run,legacy,Model(),1)
        output=worker.execute()
        self.assertIsNone(worker.handoff)
        self.assertIn('observations',worker.cached('research_manifest'))
        self.assertIn('return a + b',output['files']['calc.py'])

    def test_same_checkpoint_cannot_be_reinterpreted_under_another_policy(self):
        self.worker(GenericWorker()).execute()
        legacy=compile_contracts(self.coarse,self.req,self.catalog)['tasks'][0]
        with self.assertRaises(HostDefect):PhaseWorker(self.run,legacy,GenericWorker(),1)

    def test_research_can_fetch_current_fragment_without_duplicate_body(self):
        path=Path(self.run.state()['workspace'])/'calc.py'
        path.write_text('def add(a, b):\n    return a - b\n# '+'x'*3400+'\n'+self.original.split('\n',2)[2])
        class ReadCurrent(GenericWorker):
            def ask(self, role, prompt, **kwargs):
                data=json.loads(prompt)
                if data['phase']=='research_1_view_0':
                    self.calls.append((data['phase'],data,prompt,kwargs))
                    current=data['input']['current_evidence']
                    assert current['detail']['page_count']>1
                    return {'action':'evidence','evidence_id':current['handoff']['evidence_refs'][0],'page':1}
                return super().ask(role,prompt,**kwargs)
        model=ReadCurrent();worker=self.worker(model);worker.execute()
        data=next(p['input'] for name,p,_,_ in model.calls if name=='research_1_view_1')
        self.assertIsNone(data['current_evidence']['detail'])
        packet=worker.handoff_packet('research_1')
        expected=worker.handoff.detail(packet,packet['evidence_refs'][0],1)
        self.assertEqual(data['requested_detail']['detail'],expected)

    def test_hypothesis_can_browse_bounded_catalog_pages(self):
        path=Path(self.run.state()['workspace'])/'calc.py'
        path.write_text(self.original+'# Extra generic observation.\n'*200)
        class Browse(GenericWorker):
            def ask(self,role,prompt,**kwargs):
                data=json.loads(prompt)
                if data['phase']=='hypothesis_view_0':
                    self.calls.append((data['phase'],data,prompt,kwargs))
                    assert data['input']['catalog_view']['page_count']>1
                    return {'action':'catalog','page':1}
                if data['phase']=='hypothesis_view_2':
                    self.calls.append((data['phase'],data,prompt,kwargs))
                    return {'hypothesis':'A bounded proposal','evidence_indices':[4],'paths':['calc.py']}
                return super().ask(role,prompt,**kwargs)
        model=Browse();worker=self.worker(model);worker.execute()
        data=next(p['input'] for name,p,_,_ in model.calls if name=='hypothesis_view_1')
        self.assertEqual(data['catalog_view']['page'],1)
        self.assertLessEqual(len(data['catalog_view']['items']),4)
        self.assertEqual(worker.cached('hypothesis')['evidence_indices'],[4])

    def test_empty_source_snapshot_is_retained_not_invented(self):
        (Path(self.run.state()['workspace'])/'tests/test_calc.py').write_text('')
        # This is fixture authoring, not a Worker edit of a protected test.
        # Admit a fresh mission after constructing the synthetic empty input.
        self.run=create_mission(self.root/'empty_home',Path(self.run.state()['workspace']),
            'Inspect and repair the arithmetic module','http://127.0.0.1:1/v1','synthetic-only')
        worker=self.worker(GenericWorker());worker.execute()
        index=next(i for i,p in enumerate(worker.source_pages) if p['text']=='')
        packet=worker.handoff_packet('research_'+str(index))
        detail=worker.handoff.detail(packet,packet['evidence_refs'][0])
        self.assertEqual(detail['text'],'')
        self.assertEqual(detail['source_sha256'],sha_text(''))

    def test_unknown_version_and_read_only_profile_do_not_silently_change_acceptance(self):
        with self.assertRaises(HostDefect):
            compile_contracts(self.coarse,self.req,self.catalog,research_handoff_version=3)
        req=bind_requirements({'profile':'code_investigation','targets':['calc.py'],
            'requirements':['Inspect'],'needs_human':False},'Inspect',self.catalog)
        coarse={'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]}
        legacy=compile_contracts(coarse,req,self.catalog)['tasks'][0]
        self.assertEqual(legacy['phase_policy'],POLICY)
        with self.assertRaises(HostDefect):
            compile_contracts(coarse,req,self.catalog,research_handoff_version=2)

    def test_full_context_budget_defect_stops_before_model_dispatch(self):
        model=GenericWorker()
        contract=copy.deepcopy(self.contract)
        contract['goal']='x'*16000
        contract['sha256']=digest(
            {k:v for k,v in contract.items() if k!='sha256'})
        worker=PhaseWorker(self.run,contract,model,1)
        with self.assertRaises(HostDefect) as failure:worker.execute()
        self.assertIn('PHASE_CONTEXT_DEFECT',str(failure.exception))
        self.assertEqual(model.calls,[])


def sha_text(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


if __name__=='__main__':unittest.main()
