"""Regression inputs are saved real assessments, never invented responses.

Transport is replaced by a read-only saved-response adapter. This is not live
Acceptance evidence and cannot establish that a model will repair its output.
"""
import copy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from pitchi_work.assessment_prompt import assessment_errors
from pitchi_work.driver import Driver,BoundedFailure
from pitchi_work.policy import ASSESSMENT_ATTEMPTS,SAME_FAILURE_LIMIT
from pitchi_work.runtime import HostCore
from pitchi_work.store import ReadOnlyRun,load
from pitchi_work.replay import fingerprints


PARTIAL=Path(os.environ.get('PITCHI_PARTIAL_RUN','/path/to/private-partial-run'))
COMPLETED=Path(os.environ.get('PITCHI_SAVED_RUN','/path/to/private-completed-run'))


def saved_evidence(path):
    packet=load(path/'artifacts/integration_packet.json')
    state=load(path/'state.json')
    return {'root_goal':load(path/'root_goal.json'),'root_goal_sha256':state['root_goal_sha256'],
            'execution_context':load(path/'execution_context.json'),'obligations':packet['obligations'],
            'tasks':[load(f) for f in sorted((path/'tasks').glob('*.json'))],
            'final_tests':state['final_tests'],'artifacts':packet['artifacts'],
            'input_artifacts':packet['input_artifacts']}


class SavedResponseAdapter:
    def __init__(self,response):self.response=response;self.prompts=[]
    def ask(self,role,prompt,**kwargs):
        self.prompts.append(prompt)
        return copy.deepcopy(self.response)


@unittest.skipUnless(os.environ.get('PITCHI_PARTIAL_RUN') and os.environ.get('PITCHI_SAVED_RUN'), 'Private saved-run fixtures must be explicitly supplied; not distributed')
class SavedIntegratorBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources=[PARTIAL,COMPLETED]
        cls.before={str(p):fingerprints(p) for p in cls.sources}

    @classmethod
    def tearDownClass(cls):
        for p in cls.sources:
            if fingerprints(p)!=cls.before[str(p)]:raise AssertionError('Saved run changed: '+str(p))

    def test_actual_M12_missing_fields_rejected(self):
        packet=load(PARTIAL/'artifacts/integration_packet.json')
        result=load(PARTIAL/'artifacts/integrator_batch_1.json')
        errors=assessment_errors(result,packet,{r['id'] for r in result['criteria']})
        self.assertEqual(len(errors),2)
        self.assertTrue(all(e.startswith('M12:') for e in errors))
        self.assertTrue(any('artifacts' in e for e in errors))
        self.assertTrue(any('commands' in e for e in errors))

    def test_rejected_saved_response_has_bounded_feedback_no_worker(self):
        evidence=saved_evidence(PARTIAL)
        core=HostCore(ReadOnlyRun(PARTIAL))
        _,batches=core.integrator_inputs(evidence)
        payload,prompt,_=batches[1]
        adapter=SavedResponseAdapter(load(PARTIAL/'artifacts/integrator_batch_1.json'))
        events=[]
        with TemporaryDirectory(prefix='pitchi-assessment-replay-') as folder:
            driver=Driver.__new__(Driver)
            driver.core=core;driver.adapter=adapter
            driver.run=SimpleNamespace(path=Path(folder),raise_if_cancelled=lambda:None,
                                       event=lambda *args,**kwargs:events.append((args,kwargs)))
            with self.assertRaises(BoundedFailure) as raised:
                driver.integrator_batch(evidence,payload,prompt,1)
            self.assertEqual(raised.exception.failure_class,'INTEGRATOR_ASSESSMENT_INVALID')
            self.assertEqual(len(adapter.prompts),min(ASSESSMENT_ATTEMPTS,SAME_FAILURE_LIMIT))
            self.assertIn('M12:',adapter.prompts[1])
            self.assertIn('search/library.sqlite',adapter.prompts[1])
            self.assertTrue(all(a[0]=='integrator' for a,_ in events))
            self.assertFalse((Path(folder)/'artifacts/integrator_batch_1.json').exists())
            self.assertEqual(len(list((Path(folder)/'artifacts').glob('*_rejected_*.json'))),len(adapter.prompts))

    def test_real_negative_R0_is_not_overridden_or_regenerated(self):
        packet=load(PARTIAL/'artifacts/integration_packet.json')
        result=load(PARTIAL/'artifacts/integrator_batch_2.json')
        self.assertEqual(assessment_errors(result,packet,{r['id'] for r in result['criteria']}),[])
        self.assertIs(next(r for r in result['criteria'] if r['id']=='R0')['met'],False)
        decision=HostCore(ReadOnlyRun(PARTIAL)).completion(
            load(PARTIAL/'artifacts/integrator_assessment.json'),saved_evidence(PARTIAL))
        self.assertEqual(decision,load(PARTIAL/'artifacts/request_completion_gate.json'))
        self.assertEqual(decision['status'],'PARTIAL')

    def test_completed_saved_assessments_keep_protected_input_refs(self):
        packet=load(COMPLETED/'artifacts/integration_packet.json')
        result=load(COMPLETED/'artifacts/integrator_assessment.json')
        self.assertEqual(assessment_errors(result,packet,{r['id'] for r in result['criteria']}),[])
        evidence=saved_evidence(COMPLETED)
        core=HostCore(ReadOnlyRun(COMPLETED))
        _,batches=core.integrator_inputs(evidence)
        for payload,prompt,_ in batches:
            ids={o['id'] for o in payload['obligations']}
            # Select actual saved rows; no repaired or fabricated answer.
            response={**result,'criteria':[r for r in result['criteria'] if r['id'] in ids]}
            adapter=SavedResponseAdapter(response)
            with TemporaryDirectory(prefix='pitchi-assessment-replay-') as folder:
                driver=Driver.__new__(Driver);driver.core=core;driver.adapter=adapter
                driver.run=SimpleNamespace(path=Path(folder),raise_if_cancelled=lambda:None,
                                           event=lambda *args,**kwargs:self.fail('Valid saved response was rejected'))
                self.assertEqual(driver.integrator_batch(evidence,payload,prompt,0),response)
                self.assertEqual(len(adapter.prompts),1)


if __name__=='__main__':unittest.main()
