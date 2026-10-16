"""Generic experiment feedback gates; no subject algorithm or fixture."""
import json,unittest
from pathlib import Path
from unittest.mock import patch
import test_reference_phase_integration as fixtures
from pitchi_work.phase_workflow import phase_operation
from pitchi_work.model import ModelFailure


class TrialReviewTests(unittest.TestCase):
    def experiment(self,refuses=False):
        f=fixtures.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        original_worker=f.worker
        def legacy_worker(model,**kwargs):
            w=original_worker(model,**kwargs)
            w.trial_review_version=1
            return w
        f.worker=legacy_worker
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);v=p['input'];name=p['phase']
                if not name.startswith('edit_'):return super().ask(role,prompt,**kw)
                self.calls.append((name,p,prompt,kw))
                if name in ('edit_0','edit_4'):return {'action':'read','path':'calc.py','page':0}
                source=next(x for x in v['sources'] if x['path']=='calc.py')
                proposal={'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
                          'old_text':'return a - b'+(' + 0' if name=='edit_2' else ''),
                          'new_text':'return a - b + 0'+(' + 0' if name=='edit_2' else ''),'summary':'Synthetic trial'}
                if name=='edit_3':
                    assert p['operation']['kind']=='candidate_review'
                    assert v['trial_review']=={'required':True,'non_improving_trials':2,'best_passed':0}
                    actions={s['properties']['action']['enum'][0] for s in kw['output_schema']['oneOf']}
                    assert not actions.intersection({'patch','replace'})
                    if not p['feedback'] or refuses:return proposal
                    assert p['feedback']['failure_class']=='TRIAL_REVIEW_REQUIRED'
                    return {'action':'reconsider','hypothesis':'The unchanged assertion contradicts both trials; inspect the return expression',
                            'paths':['calc.py'],'test_reference':v['protected_staged_tests']['reference']['reference']}
                if name=='edit_5':
                    assert not v['trial_review']['required']
                    return {**proposal,'old_text':'return a - b + 0 + 0','new_text':'return a + b'}
                return proposal
        def execution(run,args,timeout):
            passed='return a + b' in (Path(run.state()['workspace'])/'calc.py').read_text()
            return {'exit_code':0 if passed else 1,'tests_run':1,'stderr':'' if passed else 'Synthetic assertion mismatch','stdout':''}
        return f,Model(),execution

    def test_non_improving_trials_require_review_then_resume_normal_edit(self):
        f,m,execution=self.experiment()
        with patch('pitchi_work.staged_validation.invocation',side_effect=execution):
            w=f.worker(m,legacy_edit=True);out=w.execute()
        self.assertIn('return a + b',out['files']['calc.py'])
        self.assertEqual(w.cached('edit_3')['action'],'reconsider')
        self.assertFalse(json.loads((w.base/'output.receipt.json').read_text())['is_task_pass'])
        self.assertEqual(w.policy['max_edit_calls'],16);self.assertEqual(w.policy['call_attempts'],2)
        self.assertEqual(f.run.state()['status'],'READY')

    def test_refusing_review_is_bounded_not_unlimited_patch_retry(self):
        f,m,execution=self.experiment(refuses=True)
        with patch('pitchi_work.staged_validation.invocation',side_effect=execution):
            w=f.worker(m,legacy_edit=True)
            with self.assertRaises(ModelFailure):w.execute()
        self.assertEqual(sum(name=='edit_3' for name,_,_,_ in m.calls),2)
        self.assertIsNone(w.cached('edit_4'))
        self.assertEqual((Path(f.run.state()['workspace'])/'calc.py').read_text(),f.original)

    def test_legacy_operation_unchanged_and_review_does_not_override_correction(self):
        self.assertEqual(phase_operation({}),{'kind':'phase_execution','instructions':'Execute the bounded phase contract','response_schema':None})
        payload={'trial_review':{'required':True}}
        self.assertEqual(phase_operation(payload)['kind'],'candidate_review')
        self.assertEqual(phase_operation(payload,{'violations':[{'field':'description'}]},{'description':'text'})['kind'],'contract_correction')


if __name__=='__main__':unittest.main()
