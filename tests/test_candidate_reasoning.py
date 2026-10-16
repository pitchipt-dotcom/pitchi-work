"""Generic failed-experiment reconsideration, immutable contract and resume."""
import json,unittest
from unittest.mock import patch
import test_reference_phase_integration as integration
from pitchi_work.candidate_reasoning import check_reconsideration,trial_summary
from pitchi_work.phase_workflow import PhaseWorker
from pitchi_work.runner import HostDefect


class ReconsiderationTests(unittest.TestCase):
    def test_trial_detail_refuses_unknown_step_and_tampered_host_receipt(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);name=p['phase'];v=p['input']
                if name=='edit_1':return {'action':'replace','path':'calc.py',
                    'source_sha256':v['source_view']['source_sha256'],
                    'old_text':'return a - b','new_text':'return a - b + 0','summary':'Synthetic failed trial'}
                if name=='edit_2':
                    if not p['feedback']:return {'action':'trial_result','step':99,'page':0}
                    assert 'existing Host trial step' in p['feedback']['required']
                    path=f.run.path/'checkpoints/P1/1/staged_tests/1/receipt.json'
                    path.write_text(path.read_text()+' ')
                    return {'action':'trial_result','step':1,'page':0}
                return super().ask(role,prompt,**kw)
        with patch('pitchi_work.staged_validation.invocation',return_value={'exit_code':1,'tests_run':1,'stderr':'Generic failure','stdout':''}):
            with self.assertRaisesRegex(HostDefect,'trial receipt hash mismatch'):f.worker(Model(),legacy_edit=True).execute()
        self.assertEqual(f.run.state()['status'],'READY')

    def test_history_summary_does_not_duplicate_test_identities_or_change_receipt(self):
        receipt={'results':[{'test':'tests/'+('long_name_'*20)+str(i),'passed':i<2} for i in range(8)]}
        original=json.dumps(receipt,sort_keys=True)
        ref={'reference':'trial/3.json','sha256':'a'*64}
        summary=trial_summary(3,ref,receipt)
        self.assertEqual(summary,{'step':3,'reference':ref,'passed':2,'failed':6})
        self.assertLess(len(json.dumps(summary)),300)
        self.assertEqual(json.dumps(receipt,sort_keys=True),original)

    def test_scope_reference_and_no_progress_are_strict(self):
        checks={'passed':False,'reference':{'reference':'trials/1.json','sha256':'a'*64}}
        old={'hypothesis':'Inspect input parsing'}
        good={'action':'reconsider','hypothesis':'Inspect the arithmetic return expression',
              'paths':['calc.py'],'test_reference':'trials/1.json'}
        self.assertEqual(check_reconsideration(good,['calc.py'],checks,old),good)
        for changes in ({'paths':['outside.py']},{'test_reference':'trials/0.json'},
                        {'hypothesis':old['hypothesis']},{'hypothesis':'x'*901},
                        {'paths':['calc.py','calc.py']},{'validator':'always-pass'}):
            with self.assertRaises(ValueError):check_reconsideration({**good,**changes},['calc.py'],checks,old)
        with self.assertRaises(ValueError):check_reconsideration(good,['calc.py'],{**checks,'passed':True},old)

    def test_failed_trial_revises_only_proposal_and_resumes_immutable_history(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        contract=f.contract.copy();interrupted=[]
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);name=p['phase'];v=p['input']
                if not name.startswith('edit_'):return super().ask(role,prompt,**kw)
                self.calls.append((name,p,prompt,kw))
                if name=='edit_0':return {'action':'read','path':'calc.py','page':0}
                if name=='edit_1':return {'action':'replace','path':'calc.py',
                    'source_sha256':v['source_view']['source_sha256'],
                    'old_text':'return a - b','new_text':'return a - b + 0','summary':'Trial'}
                if name=='edit_2':
                    return {'action':'trial_result','step':1,'page':0}
                if name=='edit_3':
                    assert v['source_view']['kind']=='trial_result'
                    assert 'Observed assertion mismatch' in v['source_view']['text']
                    assert not v['protected_staged_tests']['passed']
                    assert len(v['candidate_reasoning']['recent_trials'])==1
                    assert 'reference' not in v['candidate_reasoning']['recent_trials'][0]
                    return {'action':'reconsider','hypothesis':'The observed assertion contradicts the first trial; inspect the return expression',
                        'paths':['calc.py'],'test_reference':v['protected_staged_tests']['reference']['reference']}
                if name=='edit_4':
                    assert v['hypothesis']['hypothesis'].startswith('The observed assertion')
                    if not interrupted:interrupted.append(True);raise RuntimeError('Synthetic interruption')
                    return {'action':'read','path':'calc.py','page':0}
                return {'action':'replace','path':'calc.py','source_sha256':v['source_view']['source_sha256'],
                        'old_text':'return a - b + 0','new_text':'return a + b','summary':'Revise trial after observed assertion'}
        def execute(proxy,args,timeout):
            from pathlib import Path
            passed='return a + b' in (Path(proxy.state()['workspace'])/'calc.py').read_text()
            return {'exit_code':0 if passed else 1,'tests_run':1,'stderr':'' if passed else 'Observed assertion mismatch','stdout':''}
        m=Model()
        with patch('pitchi_work.staged_validation.invocation',side_effect=execute):
            w=f.worker(m,legacy_edit=True)
            with self.assertRaisesRegex(RuntimeError,'Synthetic interruption'):w.execute()
            original_hypothesis=w.cached('hypothesis');trial=w.cached('edit_3')
            out=PhaseWorker(f.run,f.contract,m,1).execute()
        self.assertIn('return a + b',out['files']['calc.py'])
        self.assertEqual(w.cached('hypothesis'),original_hypothesis)
        self.assertEqual(w.cached('edit_3'),trial)
        self.assertEqual(f.contract,contract)
        self.assertEqual(f.run.state()['status'],'READY')
        self.assertEqual(sum(n=='edit_3' for n,_,_,_ in m.calls),1)


if __name__=='__main__':unittest.main()
