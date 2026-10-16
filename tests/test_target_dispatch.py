"""Live target wiring, retry budgets and fail-closed recovery, synthetic only."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from pitchi_work.edit_protocol import target_dispatch, atomic_feedback, dispatch_schema, visible_targets, operation_schema
from pitchi_work.edit_targets import EditTargets
from pitchi_work.working_context import WorkingContext, encoded
from pitchi_work.phase_workflow import phase_operation, digest
from pitchi_work.recovery import pending_context_dispatch
from pitchi_work.store import dump, RunStore


class TargetDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.targets=EditTargets(self.root/'targets.json',['calc.py'],'fixed')
        self.files={'calc.py':'def add(a,b):\n    return a-b\n'}
        self.target=self.targets.issue(self.files,'calc.py',2,2)

    def test_dispatch_exact_body_no_old_text_and_original_packet_immutable(self):
        p={'rules':'Historical legacy instructions','operation_protocol':{'patch_keys':['old_text']},
            'source_view':{'kind':'edit_target','edit_target':self.target,'body_deferred':True}}
        before=json.dumps(p)
        v=target_dispatch(p,self.files,self.targets)
        self.assertEqual(json.dumps(p),before)
        self.assertEqual(v['source_view']['text'],'    return a-b\n')
        self.assertNotIn('Prefer an exact replacement',v['rules'])
        patch=self.targets.resolve(self.files,{'action':'replace_target',
            'target_id':v['source_view']['edit_target']['target_id'],'new_text':'    return a+b\n'},900)
        self.assertEqual(patch['start_line'],2)
        with self.assertRaises(ValueError):target_dispatch(p,{'calc.py':'changed'},self.targets)

    def test_atomic_feedback_keeps_failure_and_correction_values(self):
        feedback={'required':'Exact source missing','source_resolution':{'path':'calc.py','symbols':['big']},
            'response_examples':[{'action':'read','page':0}]*2,'retry_rule':'Redundant'}
        v=atomic_feedback(feedback)
        self.assertEqual(v['required'],feedback['required'])
        self.assertEqual(len(v['response_examples']),1)
        correction={**feedback,'violations':[{'previous_value':'exact','field':'x'}]}
        self.assertEqual(atomic_feedback(correction),correction)

    def test_boundary_rejection_states_exact_condition_and_uses_same_target(self):
        files={'calc.py':'def add(a,b):\n    return a-b\n# following\n'}
        target=self.targets.issue(files,'calc.py',2,2)
        raw={'action':'replace_target','target_id':target['target_id'],'new_text':'    return a+b'}
        with self.assertRaisesRegex(ValueError,'must end with a newline') as rejected:
            self.targets.resolve(files,raw,900)
        self.assertIn('line 3 remains',str(rejected.exception))
        corrected={**raw,'new_text':raw['new_text']+'\n'}
        self.assertEqual(self.targets.resolve(files,corrected,900)['replacement'],corrected['new_text'])
        self.assertIn('JSON escape \\n',target_dispatch({'source_view':{'kind':'edit_target',
            'edit_target':target}},files,self.targets)['rules'])

    def test_prompt_schema_and_visible_target_permission_are_consistent(self):
        p={'source_view':{'kind':'edit_target','edit_target':self.target,'text':'    return a-b\n'}}
        ids=visible_targets(p,self.files,self.targets)
        schema=dispatch_schema(operation_schema(self.files,['calc.py'],0,targets=True),ids)
        actions=[s['properties']['action']['enum'][0] for s in schema['oneOf']]
        self.assertNotIn('replace',actions);self.assertNotIn('patch',actions)
        target=next(s for s in schema['oneOf'] if s['properties']['action']['enum'][0]=='replace_target')
        self.assertEqual(target['properties']['target_id']['enum'],ids)
        deferred={'source_view':{'edit_target':self.target,'body_deferred':True}}
        empty=visible_targets(deferred,self.files,self.targets)
        self.assertEqual(empty,[])
        actions=[s['properties']['action']['enum'][0] for s in dispatch_schema(schema,empty)['oneOf']]
        self.assertNotIn('replace_target',actions);self.assertIn('edit_target',actions)
        with self.assertRaises(ValueError):visible_targets({'source_view':{**p['source_view'],'text':'invented'}},self.files,self.targets)

    def test_exhausted_task_official_cli_does_not_copy_or_call_health(self):
        from unittest.mock import patch
        from pitchi_work import cli
        from pitchi_work.recovery import manifest
        root=self.root/'home/runs/20260101_000000_abcdef'
        dump(root/'state.json',{'status':'STALLED','llm_config':{'provider':'local'}})
        dump(root/'tasks/P1.json',{'attempts':[{'attempt':1,'state':'FAILED'}]})
        before=manifest(root)
        with patch('pitchi_work.cli.health') as health, patch('pitchi_work.recovery.recover_mission') as recover:
            self.assertEqual(cli.main(['--home',str(self.root/'home'),'recover',root.name]),2)
            health.assert_not_called();recover.assert_not_called()
        self.assertEqual(manifest(root),before)

    def test_valid_id_without_visible_body_is_rejected_before_application(self):
        import test_reference_phase_integration as fixtures
        from pitchi_work.model import ModelFailure
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups)
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);v=p['input'];phase=p['phase']
                if phase=='edit_0':
                    source=next(s for s in v['sources'] if s['path']=='calc.py')
                    return {'action':'edit_target','path':'calc.py','source_sha256':source['source_sha256'],
                        'start_line':3,'end_line':70}
                if phase=='edit_1':
                    self.calls.append((phase,p,prompt,kw))
                    assert v['source_view']['body_deferred'] and 'text' not in v['source_view']
                    actions=[s['properties']['action']['enum'][0] for s in kw['output_schema']['oneOf']]
                    assert 'replace_target' not in actions and 'replace' not in actions
                    # Deliberately bypass grammar: Host must independently refuse.
                    return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                        'new_text':'# invented unseen block\n'}
                return super().ask(role,prompt,**kw)
        model=Model();worker=f.worker(model)
        with self.assertRaisesRegex(ModelFailure,'Target body not visible'):worker.execute()
        self.assertIsNone(worker.cached('edit_1'))
        self.assertEqual(len([n for n,*_ in model.calls if n=='edit_1']),2)
        self.assertEqual((Path(f.run.state()['workspace'])/'calc.py').read_text(),f.original)

    def test_common_composer_budgets_target_retry_without_cutting_source(self):
        p={'task_goal':'x'*6500,'host_contract':{'scope':['calc.py']},
            'source_view':{'kind':'edit_target','edit_target':self.target},'rules':'legacy '*120}
        p=target_dispatch(p,self.files,self.targets);p['working_context_composition']=1
        ctx=WorkingContext(self.root/'history',self.root)
        feedback=atomic_feedback({'required':'Read exact source','response_examples':[{'action':'read','page':0}]*2})
        operation=phase_operation(p,feedback,{'action':'replace'})
        v,previous,_=ctx.dispatch(p,operation,feedback,{'action':'replace','old_text':'x'*3000},16384,2000)
        prompt={'phase':'edit_1','operation':operation,'input':v,'feedback':feedback,'previous_output':previous}
        self.assertLessEqual(len(encoded(prompt))+512+2000,16384*80//100)
        self.assertEqual(v['task_goal'],p['task_goal'])
        self.assertEqual(v['source_view']['text'],'    return a-b\n')

    def test_pending_guard_requires_matching_audit_and_no_later_invocation(self):
        root=self.root/'run';base=root/'checkpoints/P1/1';folder=base/'calls/edit_1';folder.mkdir(parents=True)
        dump(root/'state.json',{'status':'STALLED','current_phase':'edit_1','stop_reason':'PHASE_CONTEXT_DEFECT: cannot fit'})
        p={'task_goal':'x'*20000};ctx=WorkingContext(base/'working_history',root)
        from pitchi_work.runner import HostDefect
        with self.assertRaises(HostDefect):ctx.compose(p,16384,2000)
        dump(folder/'2.json',{'status':'STARTED','at':'2026-01-01','input_sha256':digest(p)})
        self.assertEqual(len(pending_context_dispatch(RunStore(root))),1)
        role=root/'artifacts/role_call_1_worker';role.mkdir(parents=True)
        self.assertEqual(pending_context_dispatch(RunStore(root)),[])
        dump(role/'receipt.json',{'started_at':'2026-01-02'})
        self.assertEqual(pending_context_dispatch(RunStore(root)),[])

    def test_official_recover_reuses_only_proven_unsent_retry_slot(self):
        from unittest.mock import patch
        import test_reference_phase_integration as fixtures
        from test_recovery import Model
        from pitchi_work.mission_controller import MissionController
        from pitchi_work.recovery import recover_mission, manifest
        from pitchi_work.runner import HostDefect
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups)
        class TargetModel(Model):
            reject_edit=True
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt)
                if role=='worker' and p['phase'].startswith('edit_'):
                    self.calls.append((p['phase'],p,prompt,kw));v=p['input']
                    if p['phase']=='edit_0':
                        s=next(s for s in v['sources'] if s['path']=='calc.py')
                        return {'action':'edit_target','path':'calc.py','source_sha256':s['source_sha256'],
                            'start_line':2,'end_line':2}
                    if p['phase']=='edit_1':
                        if self.reject_edit:return {'action':'replace','old_text':'guessed'}
                        self.assert_schema=kw['output_schema']
                        return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                            'new_text':'    return a + b\n'}
                    return {'action':'done','summary':'Host must validate'}
                return super().ask(role,prompt,**kw)
        model=TargetModel();controller=MissionController(f.run);controller.adapter=model
        original=WorkingContext.dispatch
        def guarded(ctx,payload,operation,feedback,previous,*args,**kwargs):
            if previous and previous.get('action')=='replace':
                ctx.persist({'operation':'working_context_composition','status':'INFEASIBLE',
                    'original':ctx.persist(payload),'authority':'host'})
                raise HostDefect('PHASE_CONTEXT_DEFECT: synthetic retry envelope')
            return original(ctx,payload,operation,feedback,previous,*args,**kwargs)
        with patch.object(WorkingContext,'dispatch',guarded):
            self.assertEqual(controller.execute()['status'],'STALLED')
        self.assertIn('synthetic retry envelope',f.run.state()['stop_reason'])
        before=manifest(f.run.path);count=len(model.calls)
        child=recover_mission(f.run);model.reject_edit=False
        controller=MissionController(child);controller.adapter=model
        self.assertEqual(controller.execute()['status'],'COMPLETED')
        self.assertEqual(before,manifest(f.run.path))
        self.assertFalse(any(n.startswith(('research','hypothesis')) for n,*_ in model.calls[count:]))
        self.assertEqual(len(list((child.path/'checkpoints/P1/1/calls/edit_1').glob('*.json'))),2)
        self.assertTrue((child.path/'checkpoints/P1/1/pre_dispatch_archive/edit_1/2.json').exists())
        actions=[s['properties']['action']['enum'][0] for s in model.assert_schema['oneOf']]
        self.assertNotIn('replace',actions);self.assertNotIn('patch',actions)
