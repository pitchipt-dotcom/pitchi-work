"""Official CLI -> Controller -> compiler -> real Host checks, no live model."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from pitchi_work import cli
from pitchi_work.mission_controller import MissionController,configure_workflow
from pitchi_work.procedural_memory import ProcedureStore
from pitchi_work.store import load
import test_reference_phase_integration as fixtures
from test_procedural_memory import procedure

class EntryTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups);return f

    def test_official_cli_controller_reference_knowledge_review_host(self):
        f=self.fixture();store=ProcedureStore(f.root/'official')
        value=procedure();value['profile']='bug_fix';ref=store.publish(value)
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);v=p['input']
                if role=='requirement':
                    if not p['host_feedback']:
                        return {'profile':'bug_fix','needs_human':False,'validator':'not model authority'}
                    feedback=p['host_feedback'][0]
                    assert feedback['authority']=='host' and feedback['required']
                    assert 'calc.py' in feedback['allowed_response']['paths']
                    assert 'outside_scope.py' not in feedback['allowed_response']['paths']
                    return {'profile':'bug_fix','needs_human':False}
                if role=='planner':return {'tasks':[{'requirement_ids':[r['id'] for r in v['requirements']['requirements']],'targets':['calc.py'],'depends_on':[]}]}
                if role=='reviewer':return {'verdict':'PASS','reason':'Independent Host tests pass'}
                if role=='integrator':return {'criteria':[{'id':i,'met':True,'reason':'Host verified','evidence':v['allowed_evidence_references']} for i in v['required_criterion_ids']]}
                if p['phase'].startswith('edit_'):
                    self.calls.append((p['phase'],p,prompt,kw));step=int(p['phase'].split('_')[1])
                    actions={s['properties']['action']['enum'][0] for s in kw['output_schema']['oneOf']}
                    assert not actions.intersection({'replace','patch'})
                    if step==8:
                        assert v['trial_review']['required'] and v['trial_review']['force_after']==4
                        return {'action':'reconsider','decision':'maintain','test_reference':v['protected_staged_tests']['reference']['reference']}
                    if step in (0,2,4,6,9):
                        s=next(s for s in v['sources'] if s['path']=='calc.py')
                        return {'action':'edit_target','path':'calc.py','source_sha256':s['source_sha256'],
                                'start_line':2,'end_line':2}
                    count=(step+1)//2
                    new='    return a + b\n' if step==10 else '    return a - b'+' + 0'*count+'\n'
                    return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],'new_text':new}
                return super().ask(role,prompt,**kw)
        model=Model();captured=[]
        def foreground(run):
            captured.append(run);controller=MissionController(run);controller.adapter=model
            state=controller.execute();self.assertEqual(state['status'],'COMPLETED',state.get('stop_reason'));return 0
        with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground',side_effect=foreground):
            code=cli.main(['--home',str(f.root/'official'),'start','--workspace',str(f.root/'project'),
                '--request','Inspect and repair the arithmetic module','--model','synthetic-only',
                '--no-resolution-memory'])
        self.assertEqual(code,0);run=captured[0];base=run.path/'checkpoints/P1/1'
        self.assertEqual(load(run.path/'plan.json')['tasks'][0]['phase_policy']['version'],2)
        for name in ('working_context_policy','reference_protocol','hypothesis_protocol','hypothesis_grounding','edit_context','edit_protocol','trial_review_protocol','execution_navigation','retry_context','source_navigation','staged_candidate_gate','edit_targets_policy'):
            self.assertTrue((base/(name+'.json')).exists(),name)
        self.assertGreater(len(list((base/'reference_handoff/evidence').glob('*'))),0)
        for prefix in ('hypothesis_view_','edit_'):
            calls=[p for name,p,_,_ in model.calls if name.startswith(prefix)]
            self.assertTrue(calls);self.assertTrue(all(p['input']['abstract_procedures'][0]['procedure']==value for p in calls))
        self.assertTrue(all(p['input'].get('working_context_composition')==1 for _,p,_,_ in model.calls))
        compositions=[load(p) for p in (base/'working_history').glob('*.json')]
        self.assertTrue(any(isinstance(v,dict) and v.get('operation')=='working_context_composition'
                            and v['status']=='READY' for v in compositions))
        self.assertEqual(load(base/'edit_8.json')['decision'],'maintain')
        self.assertEqual(load(base/'edit_protocol.json'),{'version':2})
        self.assertTrue(load(run.path/'artifacts/request_completion_gate.json')['host_evidence_hashes_verified'])
        self.assertEqual((f.root/'project/calc.py').read_text(),f.original)
        self.assertFalse(load(base/'reasoning_edit.json')['enabled'])

    def test_default_cli_and_legacy_selection_unchanged(self):
        f=self.fixture()
        with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground',return_value=0) as fg:
            self.assertEqual(cli.main(['--home',str(f.root/'default'),'start','--workspace',str(f.root/'project'),'--request','Inspect and repair the arithmetic module','--model','synthetic-only']),0)
        run=fg.call_args.args[0]
        from pitchi_work.resolution import read_verified
        self.assertEqual(read_verified(run.path/'workflow_selection.json')['research_handoff_version'],2)
        configure_workflow(run,1,[])
        class PlanningModel:
            def ask(self,role,prompt,**kw):
                if role=='requirement':return {'profile':'bug_fix','targets':['calc.py'],'requirements':['Inspect','repair'],'needs_human':False}
                return {'tasks':[{'requirement_ids':['M1','M2'],'targets':['calc.py'],'depends_on':[]}]}
        controller=MissionController(run);controller.adapter=PlanningModel();controller.setup()
        self.assertEqual(load(run.path/'plan.json')['tasks'][0]['phase_policy']['version'],1)
        with self.assertRaises(ValueError):configure_workflow(run,2)

    def test_over_two_and_duplicate_procedures_rejected(self):
        f=self.fixture()
        before=(f.run.path/'workflow_selection.json').read_bytes()
        for refs in (['a'*64]*2,['a'*64,'b'*64,'c'*64]):
            with self.assertRaises(ValueError):configure_workflow(f.run,2,refs)
        self.assertEqual((f.run.path/'workflow_selection.json').read_bytes(),before)

    def test_default_api_automatic_stable_knowledge_and_corruption_stops(self):
        f=self.fixture();store=ProcedureStore(f.root/'home')
        from pitchi_work.resolution import read_verified
        self.assertEqual(read_verified(f.run.path/'workflow_selection.json')['research_handoff_version'],2)
        refs=store.automatic('php_search_quality');self.assertEqual(len(refs),2)
        rows=store.select('php_search_quality',refs)
        text=json.dumps(rows)
        for banned in ('old_text','new_text','private_validation','target_id'):
            self.assertNotIn(banned,text)
        self.assertEqual(store.automatic('unrelated'),[])
        path=store.path/(refs[0]+'.json');path.write_text('{}')
        with self.assertRaises(Exception):store.automatic('php_search_quality')
