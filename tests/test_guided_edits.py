import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_reference_phase_integration as fixture
from pitchi_work.phase_workflow import install_edit_steps,PhaseWorker
from pitchi_work.mission_profiles import compile_contracts
from pitchi_work.store import dump,load
from pitchi_work.model import ModelFailure


class GuidedTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ReferencePhaseIntegration();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.plan={'schema_version':1,'classification':'CODEX_DESIGNED_STEPS','source_hashes':{
            'calc.py':__import__('hashlib').sha256(self.f.original.encode()).hexdigest()},
            'parent_run_id':'historical-run-not-imported','max_local_calls':24,
            'steps':[{'id':'S1','path':'calc.py','original_text':'    return a - b\n',
                      'new_text':'    return a + b\n','instruction':'Add, do not subtract','local_check':'Arithmetic test'}]}
        self.path=self.f.root/'steps.json';dump(self.path,self.plan)

    def worker(self,model):
        install_edit_steps(self.f.run,self.path)
        catalog={**self.f.catalog,'guided_edits':self.plan}
        contract=compile_contracts(self.f.coarse,self.f.req,catalog,research_handoff_version=2)['tasks'][0]
        return PhaseWorker(self.f.run,contract,model,1)

    def test_real_host_application_without_research_and_resume(self):
        class Worker:
            calls=0
            def ask(s,role,prompt,**kw):
                s.calls+=1;v=json.loads(prompt)['input']
                self.assertIn('guided_step',v)
                self.assertIn('boundary line',v['rules'])
                return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],'new_text':'    return a + b\n'}
        model=Worker();w=self.worker(model);out=w.execute()
        self.assertIn('return a + b',out['files']['calc.py'])
        self.assertEqual(model.calls,1);self.assertFalse(list(w.base.glob('research_*.json')))
        self.assertTrue(w.cached('guided_S1')['accepted'])
        self.assertEqual(w.execute(),out);self.assertEqual(model.calls,1)
        self.assertEqual((Path(self.f.run.state()['workspace'])/'calc.py').read_text(),self.f.original)

    def test_syntax_failure_is_finite_retry_with_actual_error(self):
        class Worker:
            calls=0
            def ask(s,role,prompt,**kw):
                s.calls+=1;p=json.loads(prompt);v=p['input']
                if s.calls==2:self.assertIn('Syntax',json.dumps(p['feedback']))
                return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                        'new_text':'    return (\n' if s.calls==1 else '    return a + b\n'}
        model=Worker();w=self.worker(model);w.execute();self.assertEqual(model.calls,2)
        self.assertEqual(w.cached('guided_S1')['calls'],2)

    def test_nonidentical_valid_output_accepted(self):
        class Worker:
            def ask(s,role,prompt,**kw):
                v=json.loads(prompt)['input'];return {'action':'replace_target',
                    'target_id':v['source_view']['edit_target']['target_id'],'new_text':'    return (a + b)\n'}
        w=self.worker(Worker());w.execute();self.assertFalse(w.cached('guided_S1')['matches_taught_text'])

    def test_bad_target_exhausts_existing_retry(self):
        class Worker:
            calls=0
            def ask(s,*a,**kw):s.calls+=1;return {'action':'replace_target','target_id':'outside','new_text':'    return a + b\n'}
        m=Worker();w=self.worker(m)
        with self.assertRaises(ModelFailure):w.execute()
        self.assertEqual(m.calls,2);self.assertIsNone(w.cached('output'))

    def test_ordinary_cli_controller_runs_guided_steps_to_root(self):
        from pitchi_work import cli
        class Worker:
            def __init__(s,*a):pass
            def ask(s,role,prompt,**kw):
                v=json.loads(prompt)['input']
                if role=='requirement':return {'profile':'bug_fix','needs_human':False}
                if role=='planner':return {'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]}
                if role=='worker':return {'action':'replace_target',
                    'target_id':v['source_view']['edit_target']['target_id'],'new_text':v['guided_step']['new_text']}
                if role=='reviewer':return {'verdict':'PASS','reason':'Synthetic measured result'}
                if role=='integrator':return {'criteria':[{'id':i,'met':True,'reason':'Measured',
                    'evidence':v['allowed_evidence_references']} for i in v['required_criterion_ids']]}
                raise AssertionError(role)
        home=self.f.root/'official_home'
        with patch('pitchi_work.cli.health'),patch('pitchi_work.mission_controller.LocalRoleAdapter',Worker):
            rc=cli.main(['--home',str(home),'start','--workspace',str(self.f.root/'project'),
                '--request','Execute the supplied arithmetic repair steps','--edit-steps',str(self.path),
                '--model','synthetic-no-network','--no-resolution-memory'])
        self.assertEqual(rc,0)
        runs=list((home/'runs').glob('*/state.json'));self.assertEqual(len(runs),1)
        self.assertEqual(load(runs[0])['status'],'COMPLETED')
        self.assertFalse(list((runs[0].parent/'checkpoints').glob('*/1/research_*.json')))

    def test_repeated_install_never_resets_call_reservations(self):
        from pitchi_work.recovery import reserve_local_repair_call
        from pitchi_work.resolution import read_verified
        install_edit_steps(self.f.run,self.path);reserve_local_repair_call(self.f.run,'worker')
        install_edit_steps(self.f.run,self.path)
        p=self.f.run.path.parent.parent/'local_repair_budgets'/(self.f.run.run_id+'.json')
        self.assertEqual(len(read_verified(p)['reservations']),1)

    def test_resume_replays_successful_step_without_llm_and_preserves_validation(self):
        self.plan['steps'].append({'id':'S2','path':'calc.py','original_text':'def add(a, b):\n',
            'new_text':'def add(a, b): # addition\n','instruction':'Annotate this declaration','local_check':'Arithmetic remains valid'})
        dump(self.path,self.plan)
        class Worker:
            calls=[];paused=False
            def ask(s,role,prompt,**kw):
                v=json.loads(prompt)['input'];n=v['guided_step']['id']
                if n=='S2' and not s.paused:s.paused=True;raise RuntimeError('pause at safe boundary')
                s.calls.append(n)
                return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                        'new_text':v['guided_step']['new_text']}
        m=Worker();w=self.worker(m)
        with self.assertRaises(RuntimeError):w.execute()
        saved=w.reference('guided_S1')
        restarted=PhaseWorker(self.f.run,w.contract,m,1);restarted.execute()
        self.assertEqual(restarted.reference('guided_S1'),saved)
        self.assertEqual(m.calls,['S1','S2'])

    def test_ambiguous_target_and_excess_operation_budget_rejected(self):
        self.plan['steps']*=9;dump(self.path,self.plan)
        with self.assertRaises(ValueError):install_edit_steps(self.f.run,self.path)
        self.plan['steps']=self.plan['steps'][:1];self.plan['steps'][0]['original_text']='# missing\n';dump(self.path,self.plan)
        with self.assertRaises(ValueError):install_edit_steps(self.f.run,self.path)

    def test_formal_repair_reuses_guided_success_and_archives_failed_slot(self):
        from pitchi_work.mission_controller import MissionController
        from pitchi_work.recovery import manifest,recover_mission
        from pitchi_work.resolution import read_verified
        self.plan['steps'].append({'id':'S2','path':'calc.py','original_text':'def add(a, b):\n',
            'new_text':'def add(a, b): # addition\n','instruction':'Annotate declaration','local_check':'Arithmetic'})
        dump(self.path,self.plan);install_edit_steps(self.f.run,self.path)
        class Worker:
            calls=[];fail=True
            def ask(s,role,prompt,**kw):
                packet=json.loads(prompt);v=packet['input']
                if role=='requirement':return {'profile':'bug_fix','needs_human':False}
                if role=='planner':return {'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]}
                if role=='worker':
                    s.calls.append(v['guided_step']['id'])
                    return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                        'new_text':'x'*901 if s.fail and v['guided_step']['id']=='S2' else v['guided_step']['new_text']}
                if role=='reviewer':return {'verdict':'PASS','reason':'Host verified'}
                if role=='integrator':return {'criteria':[{'id':i,'met':True,'reason':'Host verified',
                    'evidence':v['allowed_evidence_references']} for i in v['required_criterion_ids']]}
                raise RuntimeError('Synthetic advisor unavailable')
        model=Worker();controller=MissionController(self.f.run);controller.adapter=model;controller.execute()
        before=manifest(self.f.run.path);middle=recover_mission(self.f.run,repair_calls=24)
        controller=MissionController(middle);controller.adapter=model;controller.execute()
        middle_before=manifest(middle.path);model.fail=False;child=recover_mission(middle,repair_calls=24)
        controller=MissionController(child);controller.adapter=model;result=controller.execute()
        self.assertEqual(result['status'],'COMPLETED',result.get('stop_reason'))
        self.assertEqual(model.calls,['S1','S2','S2','S2','S2','S2'])
        self.assertEqual(before,manifest(self.f.run.path))
        self.assertEqual(middle_before,manifest(middle.path))
        self.assertEqual([x['state'] for x in load(child.path/'tasks/P1.json')['attempts']],['FAILED','FAILED','PASS'])
        self.assertEqual(len(list((child.path/'checkpoints/P1/1/calls/edit_1').glob('*.json'))),2)
        self.assertEqual(read_verified(child.path/'artifacts/local_repair_receipt.json')['skipped_edit_steps'],[1,2])

    def test_multi_recover_independent_of_archive_order(self):
        from pitchi_work.edit_targets import HistoricalTargets
        original=HistoricalTargets.__init__
        def other_generation_first(reader,archives):
            if archives:
                saved=load(archives[0].path.parent/'guided_target_0.json')
                if saved:
                    archives=sorted(archives,key=lambda r:saved['target_id'] in r._read()['targets'])
            original(reader,archives)
        # Exercise the existing full formal recover test and all of its strict
        # history, retry, completion and budget assertions with adversarial order.
        with patch.object(HistoricalTargets,'__init__',other_generation_first):
            self.test_formal_repair_reuses_guided_success_and_archives_failed_slot()


if __name__=='__main__':unittest.main()
