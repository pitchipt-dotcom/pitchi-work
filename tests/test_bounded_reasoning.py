import unittest
from unittest.mock import patch
import test_reference_phase_integration as integration


class ReasoningTests(unittest.TestCase):
    def fixture(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups);return f

    def test_opt_in_only_hypothesis_edit_and_pin_across_resume(self):
        f=self.fixture();f.run.save_state(worker_reasoning=True)
        model=integration.GenericWorker();worker=f.worker(model)
        with patch('pitchi_work.staged_validation.invocation',return_value={'exit_code':0,'tests_run':1,'stderr':''}):worker.execute()
        for name,packet,prompt,options in model.calls:
            self.assertEqual(options.get('enable_thinking',False),name.startswith(('hypothesis_','edit_')))
            self.assertEqual(options['max_output_tokens'],worker.policy['max_output_tokens'])
        f.run.save_state(worker_reasoning=False)
        resumed=f.worker(model)
        self.assertTrue(resumed.reasoning_modes['edit']);self.assertTrue(resumed.reasoning_modes['hypothesis'])
        self.assertEqual(f.run.state()['status'],'READY')

    def test_default_and_historical_pin_remain_nonthinking(self):
        f=self.fixture();worker=f.worker(integration.GenericWorker())
        self.assertFalse(any(worker.reasoning_modes.values()))
        f.run.save_state(worker_reasoning=True)
        self.assertFalse(any(f.worker(integration.GenericWorker()).reasoning_modes.values()))
