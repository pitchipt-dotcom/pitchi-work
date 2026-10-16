"""Synthetic receipts and mocked SDKs only. No subject or live API calls."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from pitchi_work.store import RunStore, dump, load
from pitchi_work.cloud import CloudControlStop, configuration, fingerprint, CloudRoleAdapter
from pitchi_work.model import ModelFailure
from pitchi_work.runner import HostDefect
from pitchi_work.resolution import (ResolutionCoordinator, CaseStore, environment, observed_problem,
                                   check_advice, finalize_resolution, read_verified, write_verified)
from pitchi_work.trial_progress import TrialProgress


ADVICE = {'can_help':True, 'suggestions':['Inspect the observed failing expression and use the required arithmetic operation.'], 'reason':'The protected assertion remains false.'}


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.run=RunStore(self.home/'runs'/'synthetic_1')
        dump(self.run.path/'state.json',{'status':'READY','active_processes':{},'resolution_policy':{'memory':True}})
        dump(self.run.path/'root_goal.json',{'request':'Repair a synthetic arithmetic module'})
        self.contract={'task_id':'P1','profile':'bug_fix','allowed_paths':['calc.py'],
                       'validators':['scope','existing_unittest'],'required_evidence':['host_checks']}
        self.base=self.run.path/'checkpoints/P1/1'
        self.c=ResolutionCoordinator(self.run,self.contract,self.base)
        self.progress=TrialProgress(self.receipt())

    def receipt(self,passed=False,error='assertion mismatch',extra=False):
        rows=[{'test':'test_sum','passed':passed,'execution':{'exit_code':0 if passed else 1,'stderr':error if not passed else '', 'stdout':''}}]
        if extra:rows.append({'test':'test_other','passed':True,'execution':{'exit_code':0}})
        return {'authority':'host','completion':False,'results':rows,'source_hashes':{'calc.py':'synthetic'}}

    def trial(self,i,passed=False,error='assertion mismatch',edit=None):
        r=self.receipt(passed,error);p=self.base/f'staged_tests/{i}/receipt.json';dump(p,r)
        ref={'reference':str(p.relative_to(self.run.path)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        edit=edit or {'action':'replace','old_text':str(i),'new_text':str(i+1)}
        self.progress.observe(r,edit)
        return self.c.observe(i,r,ref,edit,self.progress,{'path':'calc.py','text':'return a - b'})

    def cloud(self,approval=True,provider='openai'):
        config=configuration(provider,allowed=True)
        self.run.save_state(resolution_policy={'memory':True,'cloud':config})
        self.client=Mock()
        self.client.responses.create.return_value=NS(model=config['model'],status='completed',id='synthetic',output_text=json.dumps(ADVICE),usage=NS(input_tokens=20,output_tokens=10,total_tokens=30))
        self.client.messages.create.return_value=NS(model=config['model'],stop_reason='end_turn',id='synthetic',content=[NS(type='text',text=json.dumps(ADVICE))],usage=NS(input_tokens=20,output_tokens=10))
        self.c=ResolutionCoordinator(self.run,self.contract,self.base,approval=lambda p,h:approval)
        original=CloudRoleAdapter
        mock=patch('pitchi_work.cloud.CloudRoleAdapter',side_effect=lambda run,c,approval=None:original(run,c,approval=approval,client=self.client))
        mock.start();self.addCleanup(mock.stop)

    def stalled_after_review(self):
        for i in range(4):self.trial(i)
        self.c.reviewed(4,'maintain');self.progress.review()
        return self.trial(5)

    def test_local_success_never_calls_cloud(self):
        self.cloud()
        with patch.object(self.c,'consult') as consult:
            self.trial(0,True)
            consult.assert_not_called()
        self.client.responses.create.assert_not_called()

    def test_same_error_three_times_detected_without_llm_claim(self):
        for i in range(3):self.trial(i)
        self.assertEqual(self.c.data['same_error_count'],3)
        self.assertTrue(self.progress.packet()['warning'])
        self.assertFalse(self.progress.packet()['required'])

    def test_changed_diagnostics_and_regression_are_not_hidden_by_counts(self):
        self.trial(0,error='first');self.trial(1,error='second')
        self.assertEqual(self.c.data['same_error_count'],1)
        self.assertEqual(self.progress.comparison['changed_failure_ids'],['test_sum'])
        self.assertEqual(self.progress.streak,2)

    def test_cloud_waits_for_review_and_approval_denial_sends_nothing(self):
        self.cloud(False)
        for i in range(4):self.trial(i)
        self.client.responses.create.assert_not_called()
        self.c.reviewed(4,'revise');self.progress.review()
        with self.assertRaises(CloudControlStop) as raised:self.trial(5)
        self.assertEqual(raised.exception.failure_class,'EXTERNAL_CONTENT_APPROVAL_REQUIRED')
        self.client.responses.create.assert_not_called()
        self.assertFalse(self.c.budget_path().exists())

    def test_both_advisors_suggestions_not_acceptance_and_usage_logged(self):
        for provider in ('openai','anthropic'):
            with self.subTest(provider=provider):
                # Distinct mission, not implicit fallback after a failed call.
                self.run=RunStore(self.home/'runs'/('synthetic_'+provider))
                dump(self.run.path/'state.json',{'status':'READY','active_processes':{}})
                dump(self.run.path/'root_goal.json',{'request':'Synthetic repair'})
                self.base=self.run.path/'checkpoints/P1/1';self.progress=TrialProgress(self.receipt())
                self.cloud();self.c.policy['cloud']=configuration(provider,allowed=True)
                view=self.stalled_after_review()
                self.assertEqual(view['authority'],'suggestion_only')
                self.assertEqual(self.run.state()['status'],'READY')
                receipt=load(next((self.run.path/'artifacts').glob('role_call_*/receipt.json')))
                self.assertTrue(receipt['success']);self.assertEqual(receipt['usage']['total_tokens'],30)
                self.assertEqual(len(read_verified(self.c.budget_path())['calls']),1)

    def test_resume_cached_consult_does_not_resend(self):
        self.cloud();view=self.stalled_after_review()
        self.c=ResolutionCoordinator(self.run,self.contract,self.base)
        self.assertEqual(self.trial(5),view)
        self.client.responses.create.assert_called_once()

    def test_mission_budget_shared_across_child_and_extra_requires_new_grant(self):
        self.cloud();self.stalled_after_review()
        root_id=self.run.run_id
        child=RunStore(self.home/'runs'/'child_1')
        dump(child.path/'state.json',{'status':'READY','advisor_budget_id':root_id,
            'resolution_policy':self.c.policy})
        c=ResolutionCoordinator(child,self.contract,child.path/'checkpoints/P1/1',approval=lambda p,h:True)
        payload={'messages':[{'role':'user','content':'synthetic'}]};h=fingerprint(payload)
        with self.assertRaises(CloudControlStop) as raised:c.reserve(payload,h)
        self.assertEqual(raised.exception.failure_class,'CLOUD_ADVISOR_CALL_LIMIT')
        c.policy={**c.policy,'extra_call':True};self.assertTrue(c.reserve(payload,h))
        with self.assertRaises(CloudControlStop):c.reserve(payload,h)

    def test_payload_mutation_cannot_consume_grant(self):
        self.cloud()
        def mutate(p,h):p['unexpected']=True;return True
        self.c.approval=mutate;p={'messages':[]}
        with self.assertRaises(CloudControlStop):self.c.reserve(p,fingerprint(p))
        self.assertFalse(self.c.budget_path().exists())

    def memory_case(self,outcome='success'):
        return self.c.cases.save({'problem':observed_problem(self.receipt()),'environment':environment(self.contract),
                                  'outcome':outcome,'advice':ADVICE})

    def test_memory_before_cloud_current_validation_still_required(self):
        self.cloud();self.memory_case()
        for i in range(2):self.assertIsNone(self.trial(i))
        view=self.trial(2)
        self.assertEqual(view['origin'],'memory');self.client.responses.create.assert_not_called()
        self.assertEqual(self.run.state()['status'],'READY')

    def test_memory_hash_environment_and_negative_case(self):
        identifier=self.memory_case('failure')
        self.assertFalse(self.c.cases.find(observed_problem(self.receipt()),{'profile':'other'}))
        self.trial(0);self.trial(1)
        with self.assertRaises(CloudControlStop) as raised:self.trial(2)
        self.assertEqual(raised.exception.failure_class,'RESOLUTION_KNOWN_FAILURE')
        path=self.c.cases.path/(identifier+'.json');dump(path,{'data':{},'sha256':'wrong'})
        with self.assertRaises(HostDefect):self.c.cases.find(observed_problem(self.receipt()),self.c.env)

    def test_failed_advice_stops_after_four_observed_non_improvements(self):
        self.memory_case()
        for i in range(6):self.trial(i)
        with self.assertRaises(CloudControlStop) as raised:self.trial(6)
        self.assertEqual(raised.exception.failure_class,'RESOLUTION_NO_PROGRESS')

    def test_old_success_that_fails_now_escalates_only_after_review_and_consent(self):
        self.cloud();self.memory_case()
        for i in range(4):self.trial(i)
        self.client.responses.create.assert_not_called()
        self.c.reviewed(4,'revise');self.progress.review()
        self.trial(5);self.trial(6);view=self.trial(7)
        self.assertEqual(view['origin'],'cloud')
        self.assertTrue(self.c.data['memory_failed'])
        self.assertEqual(len(self.c.data['prior_advice']),1)
        self.client.responses.create.assert_called_once()

    def test_receipt_tamper_is_host_defect_not_worker_retry(self):
        p=self.base/'receipt.json';dump(p,self.receipt())
        with self.assertRaises(HostDefect):self.c.observe(0,self.receipt(),{'reference':str(p.relative_to(self.run.path)),'sha256':'wrong'},{},self.progress)

    def test_api_failure_stops_without_retry_or_provider_switch(self):
        self.cloud();self.c.consult=Mock(side_effect=ModelFailure('CLOUD_RATE_LIMITED','synthetic'))
        with self.assertRaises(CloudControlStop) as raised:self.stalled_after_review()
        self.assertEqual(raised.exception.failure_class,'CLOUD_RATE_LIMITED')
        self.c.consult.assert_called_once()
        self.assertEqual(self.run.state()['status'],'READY')

    def test_save_success_only_existing_root_gate_and_failed_cases(self):
        self.cloud();self.stalled_after_review()
        self.run.save_state(status='PARTIAL')
        finalize_resolution(self.run)
        found=self.c.cases.find(observed_problem(self.receipt()),self.c.env)
        self.assertEqual(found[0]['outcome'],'failure')
        finalize_resolution(self.run);self.assertEqual(len(list(self.c.cases.path.glob('*.json'))),1)

    def test_success_case_after_host_root_gate_not_staged_pass(self):
        self.memory_case();self.trial(0);self.trial(1);self.trial(2)
        self.run.save_state(status='COMPLETED')
        with self.assertRaises(HostDefect):finalize_resolution(self.run)
        dump(self.run.path/'artifacts/request_completion_gate.json',{'status':'COMPLETED','host_evidence_hashes_verified':True})
        finalize_resolution(self.run)
        case=next(read_verified(p) for p in self.c.cases.path.glob('*.json') if read_verified(p).get('source_run_id')==self.run.run_id)
        self.assertEqual(case['outcome'],'success');self.assertIn('validation',case)

    def test_legacy_has_no_memory_or_cloud_side_effect(self):
        self.run.save_state(resolution_policy={});self.c=ResolutionCoordinator(self.run,self.contract,self.base)
        self.assertIsNone(self.trial(0));self.assertFalse(self.c.path.exists())

    def test_invalid_advice_never_becomes_contract(self):
        for value in ({'validator':'accept_all'}, {'can_help':True,'suggestions':[],'reason':''}):
            with self.assertRaises(CloudControlStop):check_advice(value)

    def test_local_worker_receives_cloud_advice_edits_and_returns_to_host_validation(self):
        import test_reference_phase_integration as fixtures
        from pitchi_work.code_validation import validate
        f=fixtures.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        config=configuration('openai',allowed=True)
        f.run.save_state(resolution_policy={'memory':True,'cloud':config})
        client=Mock();client.responses.create.return_value=NS(model=config['model'],status='completed',id='synthetic',output_text=json.dumps(ADVICE),usage=NS(input_tokens=20,output_tokens=10,total_tokens=30))
        original_adapter=CloudRoleAdapter
        class Worker(fixtures.GenericWorker):
            interrupted=False
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);v=p['input'];name=p['phase']
                if not name.startswith('edit_'):return super().ask(role,prompt,**kw)
                self.calls.append((name,p,prompt,kw));step=int(name.split('_')[1])
                if step==0:return {'action':'read','path':'calc.py','page':0}
                if step==5:
                    assert v['trial_review']['required']
                    return {'action':'reconsider','decision':'maintain','test_reference':v['protected_staged_tests']['reference']['reference']}
                if step==7:
                    assert v['source_view']['kind']=='resolution_advice'
                    assert 'suggestion_only' in v['source_view']['text']
                    assert 'resolution_advice_access' in v
                    if not self.interrupted:
                        self.interrupted=True
                        raise RuntimeError('synthetic controller interruption')
                    return {'action':'advice','page':0}
                source=next(s for s in v['sources'] if s['path']=='calc.py')
                count=4 if step==6 else 5 if step==8 else step-1
                old='return a - b'+' + 0'*count
                return {'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
                        'old_text':old,'new_text':'return a + b' if step==8 else old+' + 0','summary':'Generic finite trial'}
        def execute(run,args,timeout):
            passed='return a + b' in (Path(run.state()['workspace'])/'calc.py').read_text()
            return {'exit_code':0 if passed else 1,'tests_run':1,'stderr':'' if passed else 'Synthetic assertion mismatch','stdout':''}
        with patch('pitchi_work.cloud.CloudRoleAdapter',side_effect=lambda run,c,approval=None:original_adapter(run,c,approval=approval,client=client)), \
             patch('pitchi_work.resolution.terminal_approval',return_value=True), \
             patch('pitchi_work.staged_validation.invocation',side_effect=execute):
            model=Worker();w=f.worker(model,legacy_edit=True)
            with self.assertRaisesRegex(RuntimeError,'synthetic controller interruption'):w.execute()
            out=f.worker(model,legacy_edit=True).execute()
        self.assertEqual(w.cached('edit_5')['decision'],'maintain')
        self.assertIn('return a + b',out['files']['calc.py'])
        client.responses.create.assert_called_once()
        self.assertEqual(w.policy['max_edit_calls'],16)
        self.assertEqual(w.policy['call_attempts'],2)
        # Formal Host validation remains independent of staged receipts/advice.
        for name,text in out['files'].items():(Path(f.run.state()['workspace'])/name).write_text(text)
        dump(f.run.path/'plan.json',{'tasks':[f.contract]})
        with patch('pitchi_work.code_validation.invocation',side_effect=execute):
            evidence=validate(f.run,f.contract,1,out)
        self.assertTrue(evidence['passed'])
        self.assertEqual(f.run.state()['status'],'READY')
        self.assertFalse(CaseStore(f.root/'home').path.exists())


if __name__=='__main__':unittest.main()
