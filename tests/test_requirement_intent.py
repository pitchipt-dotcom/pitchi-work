"""Host intent binding: alternative outputs are not alternative permissions."""
import json
import unittest
from unittest.mock import patch
from pitchi_work import cli
from pitchi_work.mission_profiles import (bind_requirement_intent,requirement_intent_schema,
    bind_requirements,compile_contracts,HumanApprovalRequired,UnsupportedMission)
from pitchi_work.mission_controller import MissionController
from pitchi_work.store import load
import test_reference_phase_integration as fixtures


class IntentTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups);return f

    def test_alternatives_preserve_entire_request_and_host_scope(self):
        f=self.fixture();request='Inspect.\nKeep the numeric limit 17 and all exceptions.\nNever deploy.'
        for proposal in ({'profile':'bug_fix','needs_human':False},
                {'profile':'code_change','needs_human':False},
                {'profile':'bug_fix','needs_human':False,'targets':['tests/test_calc.py'],
                 'requirements':['a paraphrase, not a quote']},
                {'profile':'bug_fix','needs_human':False,'targets':['../secret.py'],
                 'requirements':['unapproved deletion']}):
            with self.subTest(proposal=proposal):
                value=bind_requirement_intent(proposal,request,f.catalog)
                self.assertEqual(value['requirements'],[{'id':'M1','text':request}])
                self.assertEqual(value['targets'],['calc.py'])
                task=compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],
                    'depends_on':[]}]},value,f.catalog,research_handoff_version=2)['tasks'][0]
                self.assertEqual(task['allowed_paths'],['calc.py']);self.assertIn('scope',task['validators'])

    def test_saved_protected_php_selection_is_advisory_not_authority(self):
        from pitchi_work.search_quality import TARGETS,REGRESSION,TESTS
        catalog={'files':[{'path':p,'bytes':100} for p in TARGETS+[REGRESSION]],
                 'tests':TESTS,'search_binding':True}
        # Exact path selection from the saved Requirement; no subject solution.
        proposal={'profile':'php_search_quality','needs_human':False,
                  'targets':['src/GuidelineSectionSearch.php',REGRESSION,'tests/guideline_section_search.php'],
                  'requirements':['a summary']}
        value=bind_requirement_intent(proposal,'Inspect query processing; never deploy.',catalog)
        self.assertEqual(value['targets'],TARGETS+[REGRESSION])
        with self.assertRaises(ValueError):compile_contracts({'tasks':[{'requirement_ids':['M1'],
            'targets':['tests/guideline_section_search.php'],'depends_on':[]}]},value,catalog,research_handoff_version=2)
        with self.assertRaises(ValueError):bind_requirements(proposal,'Inspect query processing; never deploy.',catalog)

    def test_schema_authority_human_permission_and_ownership_limits(self):
        f=self.fixture()
        for proposal in ({'profile':'bug_fix','needs_human':'false'},
                {'profile':'bug_fix','needs_human':False,'validator':'invented'},
                {'profile':'php_search_quality','needs_human':False},
                {'profile':'bug_fix','needs_human':False,'behavior':'interactive_smoke'}):
            with self.assertRaises((ValueError,UnsupportedMission)):
                bind_requirement_intent(proposal,'Inspect',f.catalog)
        with self.assertRaises(HumanApprovalRequired):
            bind_requirement_intent({'profile':'bug_fix','needs_human':True},'Inspect',f.catalog)
        catalog={**f.catalog,'files':[{'path':f'm{i}.py','bytes':4000} for i in range(4)]}
        value=bind_requirement_intent({'profile':'bug_fix','needs_human':False},'Inspect',catalog)
        with self.assertRaises(ValueError):compile_contracts({'tasks':[{'requirement_ids':['M1'],
            'targets':value['targets'],'depends_on':[]}]},value,catalog,research_handoff_version=2)
        oversized={**f.catalog,'files':[{'path':'calc.py','bytes':14001}]}
        value=bind_requirement_intent({'profile':'bug_fix','needs_human':False},'Inspect',oversized)
        with self.assertRaises(ValueError):compile_contracts({'tasks':[{'requirement_ids':['M1'],
            'targets':['calc.py'],'depends_on':[]}]},value,oversized,research_handoff_version=2)
        schema=requirement_intent_schema(f.catalog)
        for variant in schema['oneOf']:
            self.assertNotIn('targets',variant['properties'])
            self.assertNotIn('requirements',variant['properties'])
            self.assertFalse(variant['additionalProperties'])

    def test_archived_v2_without_intent_marker_retains_legacy_requirement_binding(self):
        f=self.fixture()
        from pitchi_work.resolution import write_verified
        write_verified(f.run.path/'workflow_selection.json',{'research_handoff_version':2,
            'procedure_mode':'auto','procedure_references':[]})
        class Model:
            def ask(self,role,prompt,**kw):
                if role=='requirement':
                    assert 'output_schema' not in kw
                    return {'profile':'bug_fix','targets':['calc.py'],
                            'requirements':['Inspect','repair'],'needs_human':False}
                return {'tasks':[{'requirement_ids':['M1','M2'],'targets':['calc.py'],'depends_on':[]}]}
        controller=MissionController(f.run);controller.adapter=Model();controller.setup()
        requirements=load(f.run.path/'requirements.json')
        self.assertNotIn('binding_version',requirements)
        self.assertEqual([r['text'] for r in requirements['requirements']],['Inspect','repair'])
        self.assertEqual(load(f.run.path/'artifacts/workflow_receipt.json')['requirement_protocol'],1)
        self.assertEqual(load(f.run.path/'plan.json')['tasks'][0]['phase_policy']['version'],2)

    def test_official_entry_alternative_intents_planner_retry_and_real_host_validation(self):
        for profile in ('bug_fix','code_change'):
            with self.subTest(profile=profile):
                f=self.fixture()
                class Model(fixtures.GenericWorker):
                    def ask(self,role,prompt,**kw):
                        packet=json.loads(prompt);value=packet['input']
                        if role=='requirement':
                            assert 'output_schema' in kw
                            self.intent_payload=value
                            return {'profile':profile,'needs_human':False}
                        if role=='planner':
                            self.plan_calls=getattr(self,'plan_calls',0)+1
                            if self.plan_calls==2:
                                assert packet['host_feedback'][0]['required']
                                assert packet['host_feedback'][0]['allowed_response']['paths']==['calc.py']
                            return {'tasks':[{'requirement_ids':['M1'],
                                'targets':['tests/test_calc.py'] if self.plan_calls==1 else ['calc.py'],
                                'depends_on':[]}]}
                        if role=='reviewer':return {'verdict':'PASS','reason':'Independent Host checks pass'}
                        if role=='integrator':return {'criteria':[{'id':i,'met':True,'reason':'Host verified',
                            'evidence':value['allowed_evidence_references']} for i in value['required_criterion_ids']]}
                        return super().ask(role,prompt,**kw)
                model=Model();runs=[]
                def foreground(run):
                    runs.append(run);controller=MissionController(run);controller.adapter=model
                    state=controller.execute();self.assertEqual(state['status'],'COMPLETED',state.get('stop_reason'));return 0
                with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground',side_effect=foreground):
                    self.assertEqual(cli.main(['--home',str(f.root/'official'), 'start',
                        '--workspace',str(f.root/'project'),'--request','Inspect and repair the arithmetic module',
                        '--model','synthetic-only','--no-resolution-memory']),0)
                run=runs[0]
                self.assertEqual(model.plan_calls,2)
                self.assertEqual(load(run.path/'requirements.json')['requirements'][0]['text'],
                                 load(run.path/'root_goal.json')['request'])
                self.assertEqual(load(run.path/'artifacts/requirement_receipt.json')['protocol_version'],2)
                self.assertTrue(any(name.startswith('research_') for name,*_ in model.calls))
                self.assertTrue(load(run.path/'artifacts/request_completion_gate.json')['host_evidence_hashes_verified'])
                self.assertEqual((f.root/'project/calc.py').read_text(),f.original)
