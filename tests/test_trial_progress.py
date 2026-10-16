"""Generic thresholds and receipt comparisons, no task-specific solutions."""
import unittest,json
from pathlib import Path
from unittest.mock import patch
import test_reference_phase_integration as fixtures
from pitchi_work.phase_workflow import PhaseWorker
from pitchi_work.phase_workflow import correct_fields
from pitchi_work.trial_progress import TrialProgress
from pitchi_work.trial_progress import review_instructions
from pitchi_work.working_context import WorkingContext,encoded
from pitchi_work.runner import HostDefect
from tempfile import TemporaryDirectory
from pitchi_work.candidate_reasoning import check_reconsideration
from pitchi_work.edit_protocol import operation_schema


def receipt(passes=(), error='assertion'):
    return {'results':[{'test':k,'passed':k in passes,
                        'execution':{'exit_code':0 if k in passes else 1,
                                     'stderr':'' if k in passes else error}}
                       for k in ('test_alpha','test_beta','test_gamma')]}


class TrialProgressTests(unittest.TestCase):
    def test_noop_replace_requests_only_new_text_and_host_revalidates(self):
        f=fixtures.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);v=p['input']
                if p['phase']!='edit_1':return super().ask(role,prompt,**kw)
                if not p['feedback']:
                    source=next(s for s in v['sources'] if s['path']=='calc.py')
                    return {'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
                            'old_text':'return a - b','new_text':'return a - b','summary':'Candidate'}
                assert p['operation']['kind']=='contract_correction'
                assert [x['field'] for x in p['feedback']['violations']]==['new_text']
                return {'corrections':[{'field':'new_text','value':'return a + b'},
                                      {'field':'summary','value':'Unrequested change'}]}
        def execution(run,args,timeout):
            passed='return a + b' in (Path(run.state()['workspace'])/'calc.py').read_text()
            return {'exit_code':0 if passed else 1,'tests_run':1,'stderr':'' if passed else 'Assertion mismatch','stdout':''}
        with patch('pitchi_work.staged_validation.invocation',side_effect=execution):
            w=f.worker(Model(),legacy_edit=True);out=w.execute()
        self.assertIn('return a + b',out['files']['calc.py'])
        self.assertEqual(w.cached('edit_1')['summary'],'Candidate')
        audit=json.loads((w.base/'calls/edit_1/2.json').read_text())['correction_audit']
        self.assertEqual(audit['changed_fields'],['new_text'])
        self.assertEqual(audit['ignored_fields'],['summary'])
        self.assertEqual(w.policy['max_edit_calls'],16)
    def test_review_receipt_identity_bound_and_targeted_correction(self):
        previous={'hypothesis':'Inspect argument order','paths':['app.py']}
        checks={'passed':False,'reference':{'reference':'staged/4/receipt.json'}}
        bad={'action':'reconsider','decision':'revise',**previous,'test_reference':'hypothesis.json'}
        with self.assertRaises(ValueError) as failure:
            check_reconsideration(bad,['app.py'],checks,previous,True)
        violations=failure.exception.detail['violations']
        self.assertEqual({v['field'] for v in violations},{'decision','hypothesis','test_reference'})
        fixed=correct_fields(bad,{'corrections':[{'field':'decision','value':'maintain'},
                              {'field':'test_reference','value':checks['reference']['reference']}]},violations)
        self.assertEqual(check_reconsideration(fixed,['app.py'],checks,previous,True),fixed)
        self.assertEqual(fixed['hypothesis'],previous['hypothesis'])
        compact={'action':'reconsider','decision':'maintain','test_reference':checks['reference']['reference']}
        self.assertEqual(check_reconsideration(compact,['app.py'],checks,previous,True),fixed)
        self.assertNotIn('hypothesis',compact)
        schemas=operation_schema({'app.py':'x'},['app.py'],0,reconsider=True,review_decision=True,
                                 current_test_reference=checks['reference']['reference'])['oneOf']
        refs=[s['properties']['test_reference'] for s in schemas if s['properties']['action']['enum']==['reconsider']]
        self.assertTrue(refs)
        self.assertTrue(all(r['enum']==[checks['reference']['reference']] for r in refs))
    def test_dense_retry_has_one_review_policy_without_losing_contract(self):
        with TemporaryDirectory() as root:
            context=WorkingContext(Path(root)/'history',root)
            packet=TrialProgress(receipt()).packet()
            payload={'task_goal':'Fixed task','host_contract':{'immutable':'c'*7600},
                     'trial_review':{**packet,'rules':review_instructions()},
                     'candidate_reasoning':{'rules':'Review policy is defined once in trial_review; exact receipts via trial_result.'}}
            operation={'kind':'phase_execution'};feedback={'required':'e'*850}
            previous={'action':'replace','old_text':'not present'}
            selected,shown,ref=context.dispatch(payload,operation,feedback,previous,16384,2000)
            self.assertEqual(selected['host_contract'],payload['host_contract'])
            self.assertEqual(selected['trial_review'],payload['trial_review'])
            self.assertEqual(context.read(ref),previous)
            self.assertLessEqual(len(encoded({'phase':'edit_2','operation':operation,'input':selected,
                'feedback':feedback,'previous_output':shown}))+2512,16384*80//100)
            bloated={**payload,'candidate_reasoning':{'rules':'Repeated policy '*70}}
            projected,_,_=context.dispatch(bloated,operation,feedback,previous,16384,2000)
            self.assertEqual(projected['host_contract'],payload['host_contract'])
            self.assertEqual(projected['trial_review'],payload['trial_review'])
            self.assertTrue(projected['candidate_reasoning']['view_is_partial'])
            with self.assertRaises(HostDefect):context.dispatch(
                {**bloated,'host_contract':{'immutable':'c'*12000}},operation,feedback,previous,16384,2000)

    def test_four_trial_workflow_warning_maintain_and_resume(self):
        f=fixtures.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups);interrupted=[]
        class Model(fixtures.GenericWorker):
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt);v=p['input'];name=p['phase']
                if not name.startswith('edit_'):return super().ask(role,prompt,**kw)
                step=int(name.split('_')[1])
                if step==0:return {'action':'read','path':'calc.py','page':0}
                if step==5:
                    assert v['trial_review']['required'] and v['trial_review']['force_after']==4
                    if not interrupted:interrupted.append(True);raise RuntimeError('Generic checkpoint interruption')
                    return {'action':'reconsider','decision':'maintain',
                            'hypothesis':v['hypothesis']['hypothesis'],'paths':v['hypothesis']['paths'],
                            'test_reference':v['protected_staged_tests']['reference']['reference']}
                if step==3:
                    assert v['trial_review']['warning'] and not v['trial_review']['required']
                    assert p['operation']['kind']=='phase_execution'
                source=next(s for s in v['sources'] if s['path']=='calc.py')
                if step==6:
                    assert not v['trial_review']['required']
                    old='return a - b'+' + 0'*4;new='return a + b'
                else:
                    old='return a - b'+' + 0'*(step-1);new=old+' + 0'
                return {'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
                        'old_text':old,'new_text':new,'summary':'Generic experiment'}
        def execute(run,args,timeout):
            passed='return a + b' in (Path(run.state()['workspace'])/'calc.py').read_text()
            return {'exit_code':0 if passed else 1,'tests_run':1,'stderr':'' if passed else 'Assertion mismatch','stdout':''}
        model=Model()
        with patch('pitchi_work.staged_validation.invocation',side_effect=execute):
            w=f.worker(model,legacy_edit=True)
            with self.assertRaisesRegex(RuntimeError,'checkpoint interruption'):w.execute()
            original=w.cached('hypothesis')
            output=PhaseWorker(f.run,f.contract,model,1).execute()
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertEqual(w.cached('hypothesis'),original)
        self.assertEqual(w.cached('edit_5')['decision'],'maintain')
        self.assertEqual(w.policy['max_edit_calls'],16)
        self.assertEqual(w.policy['call_attempts'],2)
        comparison=json.loads((w.base/'staged_tests/4/comparison.json').read_text())
        self.assertEqual(comparison['review']['force_after'],4)
        self.assertEqual(f.run.state()['status'],'READY')

    def test_two_warns_four_forces_and_two_candidate_compared(self):
        two=TrialProgress(receipt(),2);four=TrialProgress(receipt(),4)
        for i in range(1,5):
            for tracker in (two,four):tracker.observe(receipt(),{'action':'replace','new_text':str(i)})
            self.assertEqual(two.packet()['required'],i>=2)
            self.assertEqual(four.packet()['required'],i>=4)
            self.assertEqual(four.packet()['warning'],i>=2)
        four.review();self.assertFalse(four.packet()['required'])

    def test_same_count_different_ids_is_regression_not_progress(self):
        p=TrialProgress(receipt(('test_alpha',)))
        p.observe(receipt(('test_beta',)),{'action':'patch'})
        self.assertEqual(p.comparison['improved_ids'],['test_beta'])
        self.assertEqual(p.comparison['regressed_ids'],['test_alpha'])
        self.assertEqual(p.streak,1)
        p.observe(receipt(('test_alpha','test_beta')),{'action':'patch','replacement':'new'})
        self.assertEqual(p.streak,0)

    def test_changed_failure_and_identical_edit_are_distinct(self):
        p=TrialProgress(receipt())
        edit={'action':'replace','old_text':'x','new_text':'y','source_sha256':'a','summary':'one'}
        p.observe(receipt(error='type failure'),edit)
        self.assertEqual(len(p.comparison['changed_failure_ids']),3)
        self.assertFalse(p.comparison['repeated_edit'])
        p.observe(receipt(error='type failure'),{**edit,'summary':'two','source_sha256':'b'})
        self.assertTrue(p.comparison['repeated_edit'])
        self.assertEqual(p.comparison['changed_failure_ids'],[])
        self.assertFalse(p.packet()['required'])
        with self.assertRaises(ValueError):p.observe({'results':[]},edit)

    def test_timing_change_is_not_failure_cause_change(self):
        p=TrialProgress(receipt());next_receipt=receipt()
        for row in next_receipt['results']:row['execution']['elapsed_seconds']=12
        p.observe(next_receipt,{'action':'patch','replacement':'other'})
        self.assertEqual(p.comparison['changed_failure_ids'],[])

    def test_review_can_keep_revise_or_reject_without_contract_change(self):
        previous={'hypothesis':'Inspect argument order','paths':['app.py']}
        checks={'passed':False,'reference':{'reference':'receipt.json'}}
        for decision,text in [('maintain',previous['hypothesis']),('revise','Inspect conversion'),
                              ('reject','Investigate a different execution path')]:
            v={'action':'reconsider','decision':decision,'hypothesis':text,
               'paths':['app.py'],'test_reference':'receipt.json'}
            self.assertEqual(check_reconsideration(v,['app.py'],checks,previous,True),v)
        self.assertEqual(previous,{'hypothesis':'Inspect argument order','paths':['app.py']})
        schemas=operation_schema({'app.py':'x'},['app.py'],0,reconsider=True,review_decision=True)['oneOf']
        self.assertTrue(any('decision' in s['properties'] for s in schemas))
        with self.assertRaises(ValueError):
            check_reconsideration(v,['app.py'],checks,previous,False)


if __name__=='__main__':unittest.main()
