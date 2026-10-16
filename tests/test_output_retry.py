"""Generic output rejection boundary: never expand permissions to repair output."""
import json
import unittest
from unittest.mock import patch
from pitchi_work.mission_controller import MissionController
from pitchi_work.mission_profiles import bind_requirements, HumanApprovalRequired
from pitchi_work.capability_advisor import shortage
from pitchi_work.edit_protocol import target_rules
from pitchi_work.driver import BoundedFailure
from pitchi_work.store import load
import test_reference_phase_integration as fixtures


class OutputRetryTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups);return f

    def test_instructions_cover_limit_split_complete_source_and_fresh_target(self):
        text=target_rules()
        for phrase in ('900 UTF-8 bytes','multiple small replace_target',
                       'syntactically complete valid source','updated source hash','fresh target'):
            self.assertIn(phrase,text)

    def test_all_requirement_rejections_share_finite_feedback(self):
        valid={'profile':'bug_fix','targets':['calc.py'],'requirements':['Inspect'],'needs_human':False}
        invalid=[{**valid,'targets':['outside_scope.py']},{**valid,'extra':True},
                 {**valid,'profile':'unknown-profile'},{**valid,'requirements':['not in original request']}]
        for proposal in invalid:
            with self.subTest(proposal=proposal):
                f=self.fixture();controller=MissionController(f.run);calls=[]
                def ask(role,prompt,**kw):
                    value=json.loads(prompt);calls.append(value)
                    return proposal if len(calls)==1 else valid
                payload={'response_schema':{'targets':['Host paths']},
                         'inventory':{'candidate_paths':['calc.py']},'profiles':{'bug_fix':{}}}
                with patch.object(controller.adapter,'ask',side_effect=ask):
                    _,accepted=controller.ask('requirement',payload,
                        lambda response:bind_requirements(response,'Inspect',f.catalog))
                self.assertEqual(accepted['targets'],['calc.py']);self.assertEqual(len(calls),2)
                feedback=calls[1]['host_feedback'][0]
                self.assertTrue(feedback['required']);self.assertEqual(feedback['allowed_response']['paths'],['calc.py'])
                self.assertEqual(feedback['previous_output'],proposal)
                self.assertIn('Preserve fields',feedback['retry_rule'])
                self.assertEqual(calls[0]['input'],calls[1]['input'])

    def test_exhaustion_is_host_marked_and_human_permission_is_not_retried(self):
        f=self.fixture();controller=MissionController(f.run)
        with patch.object(controller.adapter,'ask',return_value={}) as ask:
            with self.assertRaises(BoundedFailure) as stopped:
                controller.ask('requirement',{},lambda _:(_ for _ in ()).throw(ValueError('schema rejected')))
            self.assertEqual(ask.call_count,2)
        self.assertTrue(stopped.exception.output_retry_exhausted)
        gap=shortage(f.run,stopped.exception)
        self.assertEqual(gap['kind'],'worker_output_correction_exhausted')
        self.assertFalse(gap['capability_missing_confirmed'])
        with patch.object(controller.adapter,'ask',return_value={}) as ask:
            with self.assertRaises(HumanApprovalRequired):
                controller.ask('requirement',{},lambda _:(_ for _ in ()).throw(HumanApprovalRequired('approval required')))
            self.assertEqual(ask.call_count,1)

    def test_protected_php_test_selection_uses_same_retry_as_other_rejections(self):
        # Exact selected paths from the saved failed Requirement; request text
        # is synthetic. No search answer, clinical query or solution is copied.
        from pitchi_work.search_quality import TARGETS, REGRESSION
        bad={'profile':'php_search_quality',
             'targets':['src/GuidelineSectionSearch.php',REGRESSION,
                        'tests/guideline_section_search.php'],
             'requirements':['Inspect'],'needs_human':False}
        good={**bad,'targets':bad['targets'][:2]}
        catalog={'files':[{'path':p,'bytes':0} for p in TARGETS+[REGRESSION]],
                 'tests':['tests/guideline_section_search.php'], 'search_binding':True}
        for exhausted in (False,True):
            with self.subTest(exhausted=exhausted):
                f=self.fixture();controller=MissionController(f.run);inputs=[]
                def respond(role,prompt,**kw):
                    inputs.append(json.loads(prompt))
                    return bad if len(inputs)==1 or exhausted else good
                payload={'inventory':{'candidate_paths':TARGETS+[REGRESSION]},
                         'profiles':{'php_search_quality':{}},'response_schema':{}}
                with patch.object(controller.adapter,'ask',side_effect=respond):
                    if exhausted:
                        with self.assertRaises(BoundedFailure) as stop:
                            controller.ask('requirement',payload,
                                lambda v:bind_requirements(v,'Inspect',catalog))
                        self.assertEqual(shortage(f.run,stop.exception)['kind'],
                                         'worker_output_correction_exhausted')
                    else:
                        _,value=controller.ask('requirement',payload,
                            lambda v:bind_requirements(v,'Inspect',catalog))
                        self.assertEqual(value['targets'],good['targets'])
                self.assertEqual(len(inputs),2)
                feedback=inputs[1]['host_feedback'][0]
                self.assertTrue(feedback['required'])
                self.assertEqual(feedback['previous_output'],bad)
                self.assertNotIn('tests/guideline_section_search.php',
                                 feedback['allowed_response']['paths'])

    def test_controller_calls_advisor_only_after_scope_retry_exhaustion(self):
        f=self.fixture();controller=MissionController(f.run)
        # Archived v1 file-selection contract still rejects invalid selections.
        from pitchi_work.mission_controller import configure_workflow
        configure_workflow(f.run,1,[])
        proposal={'profile':'bug_fix','targets':['outside_scope.py'],
                  'requirements':[load(f.run.path/'root_goal.json')['request']],'needs_human':False}
        with patch.object(controller.adapter,'ask',return_value=proposal) as ask:
            def advise(run,blocker,adapter):
                self.assertEqual(ask.call_count,2)
                self.assertEqual(blocker['kind'],'worker_output_correction_exhausted')
                self.assertFalse(blocker['capability_missing_confirmed'])
                return {'analysis_status':'AVAILABLE'}
            with patch('pitchi_work.capability_advisor.advise',side_effect=advise) as advisor:
                state=controller.execute()
                advisor.assert_called_once()
        self.assertEqual(state['status'],'NEEDS_CAPABILITY')
        self.assertFalse((f.run.path/'plan.json').exists())

    def test_edit_rejections_return_reason_and_permissions_without_new_dispatch_types(self):
        for invalid in ('scope','schema','bytes'):
            with self.subTest(invalid=invalid):
                f=self.fixture()
                class Model(fixtures.GenericWorker):
                    def ask(self,role,prompt,**kw):
                        packet=json.loads(prompt)
                        if packet.get('phase')=='edit_1':
                            self.edit_calls=getattr(self,'edit_calls',0)+1
                            output=super().ask(role,prompt,**kw)
                            if self.edit_calls==1:
                                if invalid=='scope':output['target_id']='unissued-id'
                                elif invalid=='schema':output['extra']='forbidden'
                                else:output['new_text']='x'*901
                            else:
                                feedback=packet['feedback'];self.retry_feedback=feedback
                                assert feedback['required']
                                assert feedback['allowed_response']['paths']==['calc.py']
                                assert feedback['allowed_response']['new_text_utf8_bytes']==900
                                assert output['target_id'] in feedback['allowed_response']['visible_target_ids']
                            return output
                        return super().ask(role,prompt,**kw)
                model=Model();worker=f.worker(model);output=worker.execute()
                self.assertEqual(model.edit_calls,2)
                self.assertIn('return a + b',output['files']['calc.py'])
                self.assertEqual((f.root/'project/calc.py').read_text(),f.original)

    def test_large_total_change_uses_multiple_valid_target_operations(self):
        f=self.fixture()
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**kw):
                packet=json.loads(prompt);phase=packet['phase'];value=packet['input']
                if phase in ('edit_0','edit_2'):
                    source=next(s for s in value['sources'] if s['path']=='calc.py')
                    return {'action':'edit_target','path':'calc.py','source_sha256':source['source_sha256'],
                            'start_line':2,'end_line':2}
                if phase in ('edit_1','edit_3'):
                    text='    return a '+('-' if phase=='edit_1' else '+')+' b # '+'x'*620+'\n'
                    self.replacements=getattr(self,'replacements',[])+[text]
                    self.ids=getattr(self,'ids',[])+[value['source_view']['edit_target']['target_id']]
                    return {'action':'replace_target','target_id':self.ids[-1],'new_text':text}
                return super().ask(role,prompt,**kw)
        model=Model();worker=f.worker(model);output=worker.execute()
        self.assertGreater(sum(len(s.encode()) for s in model.replacements),900)
        self.assertTrue(all(len(s.encode())<=900 for s in model.replacements))
        self.assertEqual(len(set(model.ids)),2)
        compile(output['files']['calc.py'],'calc.py','exec')
        self.assertIn('return a + b',output['files']['calc.py'])
