"""Generic retry envelope accounting and exact diagnostic references."""
import json,tempfile,unittest
from pathlib import Path
from pitchi_work.working_context import WorkingContext,encoded
from pitchi_work.staged_validation import diagnostics
from pitchi_work.edit_protocol import operation_schema
from pitchi_work.runner import HostDefect
import test_reference_phase_integration as fixtures


class RetryDispatchTests(unittest.TestCase):
    def test_derived_navigation_narration_not_contract_or_requested_source(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            payload={'host_contract':{'fixed':'c'*7900},'task_goal':'Original task',
                     'source_view':{'kind':'requested_source','text':'s'*1200},
                     'current_objective':{'phase':'stabilize_tested_candidate','rules':'Derived narration '*25},
                     'catalog_access':'Derived catalog explanation '*10,'diagnostic_access':{'test_result':True}}
            selected=context.select(payload,16384,2000,envelope_reserve=700)
            for k in ('host_contract','task_goal','source_view','diagnostic_access'):
                self.assertEqual(selected[k],payload[k])
            self.assertEqual(selected['current_objective']['phase'],payload['current_objective']['phase'])
            self.assertEqual(selected,context.select(selected,16384,2000,envelope_reserve=700))
            with self.assertRaises(HostDefect):context.select({**payload,'host_contract':{'fixed':'c'*12000}},16384,2000,envelope_reserve=700)
    def test_optional_trial_counts_project_to_ids_not_lost_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            history={'recent_trials':[{'step':1,'passed':2,'failed':3},{'step':3,'passed':2,'failed':3}],
                     'trial_access':{'action':'trial_result','step':3,'page':0},
                     'rules':'Review policy is defined once in trial_review; exact receipts via trial_result.'}
            payload={'host_contract':{'fixed':'c'*9600},'candidate_reasoning':history,
                     'trial_review':{'required':False,'warning':True,'force_after':4},'host_observations':[]}
            selected=context.select(payload,16384,2000,envelope_reserve=400)
            self.assertEqual(selected['host_contract'],payload['host_contract'])
            self.assertEqual(selected['trial_review'],payload['trial_review'])
            self.assertEqual(selected['candidate_reasoning']['trial_access'],history['trial_access'])
            self.assertEqual(selected['candidate_reasoning']['recent_trials'],[{'step':1},{'step':3}])
            self.assertEqual(selected,context.select(selected,16384,2000,envelope_reserve=400))
            with self.assertRaises(HostDefect):context.select({**payload,'host_contract':{'fixed':'c'*12000}},16384,2000,envelope_reserve=400)
    def test_automatic_source_cue_can_be_deferred_but_requested_code_cannot(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            cue={'kind':'diagnostic_source','text':'exact source line\n'*50,
                 'path':'module.py','source_sha256':'a'*64,'diagnostic_line':17,
                 'request_example':{'action':'read','path':'module.py','page':1}}
            payload={'task_goal':'Fixed original goal','host_contract':{'fixed':'c'*7700},
                     'source_view':cue,'host_observations':[]}
            operation={'kind':'phase_execution'};feedback={'required':'e'*800}
            selected,shown,ref=context.dispatch(payload,operation,feedback,
                {'action':'replace','old_text':'Rejected fragment'},16384,2000)
            self.assertEqual(selected['source_view']['text'],'')
            self.assertEqual(context.read(selected['source_view']['reference']),cue)
            self.assertEqual(selected['source_view']['source_sha256'],cue['source_sha256'])
            self.assertEqual(selected['source_view']['request_example'],cue['request_example'])
            self.assertEqual(selected['host_contract'],payload['host_contract'])
            self.assertLessEqual(len(encoded({'phase':'edit_0','operation':operation,'input':selected,
                'feedback':feedback,'previous_output':shown}))+2512,16384*80//100)
            repeat=context.select(selected,16384,2000,envelope_reserve=1000)
            self.assertEqual(repeat['source_view'],selected['source_view'])
            requested={**payload,'source_view':{**cue,'kind':'requested_source'}}
            with self.assertRaises(HostDefect):context.dispatch(requested,operation,feedback,
                {'action':'replace','old_text':'Rejected fragment'},16384,2000)

    def test_retry_projection_preserves_original_observations_not_reference_wrappers(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            observations=[{'message':'Original exception with condition 72 hours and no deletion'}]
            selected=context.select({'task_goal':'Fixed task','host_observations':observations},16384,2000)
            repeated=context.select(selected,16384,2000)
            self.assertEqual(repeated,selected)
            self.assertEqual(context.read(repeated['host_observations']['reference']),observations)
            bad={**selected,'host_observations':{**selected['host_observations'],'page_count':99}}
            with self.assertRaises(HostDefect):context.select(bad,16384,2000)

    def test_initial_dense_call_uses_actual_envelope_and_keeps_mandatory_material(self):
        fixture=fixtures.ReferencePhaseIntegration(methodName='test_multiple_research_steps_handoff_to_hypothesis_and_staged_edit')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        class Model:
            def ask(self,role,prompt,**options):
                self.prompt=json.loads(prompt);self.bytes=len(prompt.encode())
                return {'action':'done','summary':'Candidate only; Host acceptance not implied'}
        model=Model();worker=fixture.worker(model)
        payload={'task_goal':'g'*1200,'rules':'r'*1200,'host_contract':{'fixed':'c'*600},
            'source_view':{'text':'s'*2000},'mandatory_material':'m'*3700,
            'diagnostic_access':{'test_result':True},'host_observations':[]}
        result=worker.call('edit_initial_dense',payload,lambda value:value)
        self.assertEqual(result['action'],'done');self.assertLessEqual(model.bytes+2512,16384*80//100)
        self.assertEqual(model.prompt['input']['mandatory_material'],payload['mandatory_material'])
        self.assertEqual(model.prompt['input']['host_contract'],payload['host_contract'])
        self.assertEqual(model.prompt['input']['source_view'],payload['source_view'])

    def test_measured_envelope_is_not_double_counted_as_estimated_reserve(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            payload={'task_goal':'g'*1200,'rules':'r'*800,'host_contract':{'fixed':'c'*600},
                'source_view':{'text':'s'*1000},'mandatory_material':'m'*3800,
                'diagnostic_access':{'test_result':True},'host_observations':[]}
            feedback={'required':'q'*900}
            previous={'action':'replace','path':'module.py','old_text':'a'*1800,'new_text':'b'*900}
            selected,shown,ref=context.dispatch(payload,{'kind':'phase_execution'},feedback,previous,16384,2000)
            envelope={'phase':'edit_12','operation':{'kind':'phase_execution'},'input':selected,
                'feedback':feedback,'previous_output':shown}
            self.assertLessEqual(len(encoded(envelope))+2512,16384*80//100)
            self.assertEqual(selected['mandatory_material'],payload['mandatory_material'])
            self.assertEqual(context.read(ref),previous)
    def test_workflow_can_page_rejected_output_and_resume_without_changing_saved_input(self):
        fixture=fixtures.ReferencePhaseIntegration(methodName='test_multiple_research_steps_handoff_to_hypothesis_and_staged_edit')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**options):
                packet=json.loads(prompt);phase=packet['phase'];value=packet['input']
                if phase=='edit_1':
                    self.calls.append((phase,packet,prompt,options))
                    if packet['feedback']:
                        self.previous_ref=packet['previous_output']['reference']
                        return {'action':'previous_output','page':0}
                    source=next(v for v in value['sources'] if v['path']=='calc.py')
                    return {'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
                        'old_text':'absent exact fragment','new_text':'corrected fragment','summary':'Synthetic rejected edit'}
                if phase=='edit_2':return {'action':'read','path':'calc.py','page':0}
                if phase=='edit_3':
                    source=next(v for v in value['sources'] if v['path']=='calc.py')
                    return {'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
                        'old_text':'return a - b','new_text':'return a + b','summary':'Fix generic arithmetic'}
                return super().ask(role,prompt,**options)
        model=Model();worker=fixture.worker(model,legacy_edit=True);output=worker.execute()
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertEqual(worker.working.read(model.previous_ref)['old_text'],'absent exact fragment')
        first=worker.reference('edit_1')
        from pitchi_work.phase_workflow import PhaseWorker
        resumed=PhaseWorker(fixture.run,fixture.contract,model,1)
        self.assertEqual(resumed.execute(),output);self.assertEqual(first,resumed.reference('edit_1'))

    def test_rejected_patch_reference_and_full_retry_within_unchanged_guard(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            payload={'task_goal':'g'*1200,'rules':'r'*800,'host_contract':{'fixed':'c'*600},
                'source_view':{'text':'s'*1800},'source_navigation':{'symbols':{'m':[]},'read_history':[]},
                'sources':[{'path':'module.py','write_allowed':True},{'path':'protected.py','write_allowed':False}],
                'protected_staged_tests':{'reference':{'reference':'receipt.json','sha256':'0'*64},
                    'passed':False,'failures':['f'*1700]},'diagnostic_access':{'test_result':True}}
            previous={'action':'replace','path':'module.py','source_sha256':'0'*64,
                'old_text':'a'*1800,'new_text':'b'*900,'summary':'Rejected proposal'}
            operation={'kind':'phase_execution','instructions':'Read exact source'}
            feedback={'required':'Read exact source; previous old_text absent','examples':'e'*1000}
            selected,shown,ref=context.dispatch(payload,operation,feedback,previous,16384,2000)
            envelope={'phase':'edit_2','operation':operation,'input':selected,
                      'feedback':feedback,'previous_output':shown}
            self.assertLessEqual(len(encoded(envelope))+2512,16384*80//100)
            self.assertEqual(context.read(ref),previous)
            self.assertEqual(selected['source_view'],payload['source_view'])
            self.assertEqual(selected['host_contract'],payload['host_contract'])
            self.assertEqual(selected['task_goal'],payload['task_goal'])
            self.assertIn('Rejected proposal',context.detail(ref,0)['text']+
                ''.join(context.detail(ref,i)['text'] for i in range(1,context.detail(ref,0)['page_count'])))

    def test_true_mandatory_overflow_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            with self.assertRaises(HostDefect):context.dispatch({'rules':'x'*16000},
                {'kind':'phase_execution'},None,None,16384,2000)

    def test_correction_keeps_declared_values_not_partial_reference(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root);previous={'description':'Original'}
            selected,shown,ref=context.dispatch({'rules':'Fixed'}, {'kind':'contract_correction'},
                {'violations':[{'field':'description','previous_value':'Original'}]},previous,16384,2000)
            self.assertEqual(shown,previous);self.assertEqual(context.read(ref),previous)

    def test_dense_atomic_correction_defers_duplicate_edit_not_required_context(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            previous={'action':'replace','path':'module.py','source_sha256':'0'*64,
                'old_text':'original source '*40,'new_text':'original source '*40,
                'summary':'A proposal, not acceptance'}
            feedback={'violations':[{'field':'new_text','required_action':'Return an actual change',
                'previous_value':{'exists':True,'value':previous['new_text']}}]}
            operation={'kind':'contract_correction','instructions':'Correct declared fields only'}
            envelope={'operation':operation,'feedback':feedback,'previous_output':previous}
            old_limit=13107-2000-512-256-len(encoded(envelope))
            payload={'host_contract':{'acceptance':'Fixed; Host judges'},
                'source_view':{'text':'Exact requested source'},'rules':'Never weaken scope',
                'host_observations':[]}
            base=context.select(payload,16384,2000)
            payload['task_goal']='g'*(old_limit+120-len(encoded(base))-len(encoded('task_goal'))-4)
            selected,shown,ref=context.dispatch(payload,operation,feedback,previous,16384,2000)
            for key in ('host_contract','source_view','rules','task_goal'):
                self.assertEqual(selected[key],payload[key])
            self.assertEqual(context.read(ref),previous)
            self.assertEqual(shown['reference'],ref)
            self.assertNotIn('old_text',shown)
            self.assertEqual(feedback['violations'][0]['previous_value']['value'],previous['new_text'])
            prompt={'phase':'edit_3','operation':operation,'input':selected,
                'feedback':feedback,'previous_output':shown}
            self.assertLessEqual(len(encoded(prompt))+2512,13107)

    def test_correction_missing_declared_value_keeps_full_previous(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            previous={'action':'replace','old_text':'Original','new_text':'Original'}
            _,shown,_=context.dispatch({'rules':'Fixed'}, {'kind':'contract_correction'},
                {'violations':[{'field':'new_text'}]},previous,16384,2000)
            self.assertEqual(shown,previous)

    def test_target_correction_uses_same_budget_and_retains_exact_value(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            previous={'action':'replace_target','target_id':'host-issued-id',
                'new_text':'synthetic code line\n'*45}
            feedback={'violations':[{'field':'new_text','previous_value':
                {'exists':True,'value':previous['new_text']}}]}
            payload={'working_context_composition':1,'host_contract':{'fixed':'c'*8200},
                'source_view':{'text':'Exact requested source'},'rules':'Host scope unchanged'}
            selected,shown,ref=context.dispatch(payload,{'kind':'contract_correction'},
                feedback,previous,16384,2000)
            self.assertEqual(context.read(ref),previous)
            self.assertEqual(shown['reference'],ref)
            self.assertNotIn('new_text',shown)
            self.assertEqual(selected['host_contract'],payload['host_contract'])
            self.assertEqual(feedback['violations'][0]['previous_value']['value'],previous['new_text'])
            self.assertLessEqual(len(encoded({'phase':'edit_1','operation':{'kind':'contract_correction'},
                'input':selected,'feedback':feedback,'previous_output':shown}))+2512,13107)

    def test_diagnostic_first_cause_not_only_stack_tail(self):
        text='Fatal error: synthetic arithmetic failure\n'+'stack frame\n'*100+'end trace\n'
        receipt={'results':[{'test':'tests/example.php','passed':False,
            'execution':{'exit_code':255,'stderr':text,'stdout':''}}]}
        first=diagnostics(receipt)[0]
        self.assertIn('Fatal error',first['stderr']['first']);self.assertIn('end trace',first['stderr']['last'])
        self.assertTrue(first['stderr']['partial']);self.assertEqual(first['stderr']['utf8_bytes'],len(text.encode()))
        self.assertEqual(receipt['results'][0]['execution']['stderr'],text)
        schema=operation_schema({'module.py':'original'},['module.py'],0,diagnostics=True)
        actions=[s['properties']['action']['enum'][0] for s in schema['oneOf']]
        self.assertIn('test_result',actions);self.assertIn('previous_output',actions)
