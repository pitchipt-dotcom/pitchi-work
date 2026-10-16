"""Same PhaseWorker/Host tests for local and mocked Cloud target edits."""
import json
import unittest
import test_reference_phase_integration as fixture
from pitchi_work.cloud_takeover import MockCloudWorker, MockEditTakeover
from pitchi_work.model import ModelFailure
from pitchi_work.store import load


class TargetWorker(fixture.GenericWorker):
    def __init__(self,fail=False,interrupt=None):
        super().__init__(interrupt=interrupt);self.fail=fail

    def ask(self,role,prompt,**kwargs):
        packet=json.loads(prompt);phase=packet['phase'];value=packet['input']
        if phase=='edit_1' and phase==self.interrupt:
            self.interrupt=None;raise RuntimeError('synthetic interrupted edit')
        if phase=='edit_0':
            self.calls.append((phase,packet,prompt,kwargs))
            source=next(s for s in value['sources'] if s['path']=='calc.py')
            return {'action':'edit_target','path':'calc.py','source_sha256':source['source_sha256'],'start_line':2,'end_line':2}
        if phase=='edit_1':
            self.calls.append((phase,packet,prompt,kwargs))
            return {'action':'replace_target','target_id':'invalid' if self.fail else value['source_view']['edit_target']['target_id'],
                    'new_text':'    return a + b\n'}
        return super().ask(role,prompt,**kwargs)


class TargetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ReferencePhaseIntegration();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)

    def test_local_target_edit_independent_tests_and_context(self):
        model=TargetWorker();phase=self.fixture.worker(model);output=phase.execute()
        self.assertIn('return a + b',output['files']['calc.py'])
        receipt=load(phase.base/'staged_tests/1/receipt.json')
        self.assertTrue(all(r['passed'] for r in receipt['results']))
        self.assertEqual(self.fixture.run.state()['status'],'READY')
        for _,_,prompt,kwargs in model.calls:
            self.assertLessEqual(len(prompt.encode())+512+kwargs['max_output_tokens'],phase.policy['context_tokens']*80//100)

    def test_target_registry_and_checkpoints_resume(self):
        model=TargetWorker(interrupt='edit_1');phase=self.fixture.worker(model)
        with self.assertRaises(RuntimeError):phase.execute()
        ref=phase.reference('edit_0');registry=phase.edit_targets.path.read_bytes()
        restarted=self.fixture.worker(model);output=restarted.execute()
        self.assertEqual(ref,restarted.reference('edit_0'))
        self.assertEqual(registry,restarted.edit_targets.path.read_bytes())
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertEqual(sum(n=='edit_0' for n,_,_,_ in model.calls),1)

    def test_mock_takeover_uses_same_target_contract_and_host_validator(self):
        phase=self.fixture.worker(TargetWorker(fail=True))
        def respond(packet):
            return {'action':'replace_target','target_id':packet['input']['source_view']['edit_target']['target_id'],
                    'new_text':'    return a + b\n'}
        phase.edit_takeover=MockEditTakeover(MockCloudWorker(respond))
        output=phase.execute()
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertEqual(load(self.fixture.run.path/'private_takeovers/P1_edit_1.json')['status'],'HOST_STAGE_PASS')

    def test_mock_takeover_regression_rejected_not_completed(self):
        phase=self.fixture.worker(TargetWorker(fail=True))
        phase.edit_takeover=MockEditTakeover(MockCloudWorker(lambda packet:{'action':'replace_target',
            'target_id':packet['input']['source_view']['edit_target']['target_id'],'new_text':'    return a * b\n'}))
        with self.assertRaises(ModelFailure):phase.execute()
        self.assertNotEqual(self.fixture.run.state()['status'],'COMPLETED')
        self.assertEqual(load(self.fixture.run.path/'private_takeovers/P1_edit_1.json')['status'],'HOST_TEST_REJECTED')

    def test_pinned_legacy_workflow_never_auto_migrates(self):
        from pitchi_work.store import dump
        phase=self.fixture.worker(fixture.GenericWorker())
        # Synthetic historical attempt: packets exist but no target-policy marker.
        (phase.base/'edit_targets_policy.json').unlink()
        dump(phase.base/'packets/edit_0.json',{'synthetic_historical_packet':True})
        restarted=self.fixture.worker(fixture.GenericWorker())
        self.assertIsNone(restarted.edit_targets)
        self.assertFalse((phase.base/'edit_targets_policy.json').exists())


if __name__=='__main__':unittest.main()
