"""Synthetic arithmetic only; no real provider, subject solution or saved edit."""
import json
import unittest
from unittest.mock import patch

import test_reference_phase_integration as reference_fixture
from pitchi_work.cloud_takeover import MockCloudWorker, MockEditTakeover, run_record
from pitchi_work.model import ModelFailure
from pitchi_work.runner import HostDefect
from pitchi_work.store import Cancelled, load, dump
from pitchi_work.mission_profiles import inventory, compile_contracts


class StuckWorker(reference_fixture.GenericWorker):
    def ask(self, role, prompt, **kwargs):
        packet=json.loads(prompt)
        if packet['phase']=='edit_1':
            self.calls.append((packet['phase'],packet,prompt,kwargs))
            # Same already-visible read: finite local retry cannot make an edit.
            return {'action':'read','path':'calc.py','page':0}
        return super().ask(role,prompt,**kwargs)


def proposed(packet):
    source=packet['input']['source_view']
    return {'action':'replace','path':'calc.py','source_sha256':source['source_sha256'],
            'old_text':'return a - b','new_text':'return a + b','summary':'Correct arithmetic'}


class CloudTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.fixture=reference_fixture.ReferencePhaseIntegration()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.run=self.fixture.run
        from pathlib import Path
        workspace=Path(self.run.state()['workspace'])
        (workspace/'tests/test_identity.py').write_text(
            'import unittest\nfrom calc import add\nclass Identity(unittest.TestCase):\n'
            '    def test_zero(self): self.assertEqual(add(7,0),7)\n')
        from pitchi_work.code_validation import snapshot
        dump(self.run.path/'artifacts/host_baseline.json',snapshot(workspace))
        self.fixture.catalog=inventory(workspace)
        self.fixture.contract=compile_contracts(self.fixture.coarse,self.fixture.req,
            self.fixture.catalog,research_handoff_version=2)['tasks'][0]
        self.local=StuckWorker();self.phase=self.fixture.worker(self.local,legacy_edit=True)
        self.mock=MockCloudWorker(proposed)
        self.phase.edit_takeover=MockEditTakeover(self.mock)

    def record(self):
        return run_record(self.run)['takeovers'][0]

    def test_stalled_local_edit_replaced_then_host_tests_and_resume(self):
        output=self.phase.execute()
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertEqual(self.mock.calls,1)
        self.assertEqual(sum(n=='edit_1' for n,*_ in self.local.calls),2)
        record=self.record()
        self.assertEqual(record['status'],'HOST_STAGE_PASS')
        self.assertEqual(record['improved_test_ids'],['tests/test_calc.py'])
        self.assertEqual(record['regressed_test_ids'],[])
        self.assertTrue(load(self.run.path/record['host_validation']['reference'])['results'][0]['passed'])
        self.assertEqual((self.fixture.root/'project/calc.py').read_text(),self.fixture.original)
        # Default controller did not mark the mission completed.
        export=run_record(self.run)
        self.assertEqual(export['completion_classification'],'NOT_COMPLETED')
        self.assertEqual(export['api_calls'],0)
        self.assertEqual(export['actual_api_cost_usd'],0)
        self.assertEqual(export['external_stages'],1)
        self.assertEqual(export['host_verified_takeover_stages'],1)
        self.assertEqual(export['external_stage_ratio'],1/export['total_stages'])
        resumed=self.fixture.worker(self.local)
        resumed.edit_takeover=MockEditTakeover(self.mock)
        calls=len(self.local.calls)
        self.assertEqual(resumed.execute(),output)
        self.assertEqual(len(self.local.calls),calls)
        self.assertEqual(self.mock.calls,1)

    def rejected(self, mutate):
        self.mock.respond=lambda packet:mutate(proposed(packet))
        with self.assertRaises(ModelFailure) as failed:self.phase.execute()
        self.assertEqual(failed.exception.failure_class,'TAKEOVER_EDIT_REJECTED')
        self.assertEqual(self.record()['status'],'REJECTED')
        self.assertIsNone(self.phase.cached('edit_1'))
        self.assertIsNone(self.phase.cached('output'))
        self.assertEqual(run_record(self.run)['completion_classification'],'NOT_COMPLETED')

    def test_reject_contract_extra_field(self):
        self.rejected(lambda p:{**p,'validator':'trust me'})

    def test_reject_scope(self):
        self.rejected(lambda p:{**p,'path':'tests/test_calc.py'})

    def test_reject_stale_hash(self):
        self.rejected(lambda p:{**p,'source_sha256':'0'*64})

    def test_reject_inapplicable_patch(self):
        self.rejected(lambda p:{**p,'old_text':'fragment absent'})

    def test_host_serializes_valid_line_patch(self):
        self.mock.respond=lambda packet:{'action':'patch','path':'calc.py',
            'source_sha256':packet['input']['source_view']['source_sha256'],
            'start_line':2,'end_line':2,'replacement_lines':['    return a + b'],
            'summary':'Correct arithmetic'}
        self.assertIn('return a + b',self.phase.execute()['files']['calc.py'])
        self.assertEqual(self.record()['status'],'HOST_STAGE_PASS')

    def test_reject_invalid_patch_serialization(self):
        self.mock.respond=lambda packet:{'action':'patch','path':'calc.py',
            'source_sha256':packet['input']['source_view']['source_sha256'],
            'start_line':2,'end_line':2,'replacement_lines':['one\ntwo'],
            'summary':'invalid multiline item'}
        with self.assertRaises(ModelFailure) as failed:self.phase.execute()
        self.assertEqual(failed.exception.failure_class,'TAKEOVER_EDIT_REJECTED')
        self.assertIsNone(self.phase.cached('output'))

    def test_local_success_never_uses_mock(self):
        phase=self.fixture.worker(reference_fixture.GenericWorker())
        phase.edit_takeover=MockEditTakeover(self.mock)
        phase.execute()
        self.assertEqual(self.mock.calls,0)
        self.assertEqual(run_record(self.run)['external_stages'],0)

    def test_mock_does_not_reset_consumed_api_budget(self):
        self.run.save_state(cloud_call_count=2,advisor_budget_id='existing_grant')
        ledger=self.fixture.root/'home/recovery_memory/advisor_budgets/existing_grant.json'
        dump(ledger,{'calls':[{'provider':'openai'},{'provider':'anthropic'}]})
        original=ledger.read_bytes()
        self.phase.execute()
        self.assertEqual(ledger.read_bytes(),original)
        self.assertEqual(self.run.state()['cloud_call_count'],2)
        self.assertEqual(run_record(self.run)['existing_run_cloud_call_count'],2)
        self.assertEqual(run_record(self.run)['api_calls'],0)

    def test_cancellation_does_not_accept_mock_edit(self):
        def cancel(packet):
            self.run.request_cancel()
            return proposed(packet)
        self.mock.respond=cancel
        with self.assertRaises(Cancelled):self.phase.execute()
        self.assertEqual(self.record()['status'],'CANCELLED')
        self.assertIsNone(self.phase.cached('edit_1'))
        self.assertIsNone(self.phase.cached('output'))

    def test_reject_oversize_output(self):
        self.rejected(lambda p:{**p,'new_text':'x'*2000})

    def test_reject_non_edit_operation(self):
        self.rejected(lambda p:{'action':'done','summary':'completed'})

    def test_host_tests_reject_regression(self):
        # Pre-existing passing behavior must not be lost by the proposed edit.
        self.mock.respond=lambda packet:{**proposed(packet),'new_text':'return 5'}
        with self.assertRaises(ModelFailure) as failed:self.phase.execute()
        self.assertEqual(failed.exception.failure_class,'TAKEOVER_HOST_TEST_FAILED')
        record=self.record()
        self.assertEqual(record['status'],'HOST_TEST_REJECTED')
        self.assertTrue(record['failed_test_ids'])
        self.assertEqual(record['improved_test_ids'],['tests/test_calc.py'])
        self.assertEqual(record['regressed_test_ids'],['tests/test_identity.py'])
        self.assertIsNone(self.phase.cached('output'))
        self.assertEqual(run_record(self.run)['completion_classification'],'NOT_COMPLETED')

    def test_default_off_and_no_external_adapter(self):
        self.assertIsNone(self.fixture.worker(reference_fixture.GenericWorker()).edit_takeover)
        with self.assertRaises(ValueError):MockEditTakeover(object())

    def test_limit_survives_new_coordinator(self):
        self.rejected(lambda p:{'action':'done','summary':'no edit'})
        retry=self.fixture.worker(self.local);retry.edit_takeover=MockEditTakeover(self.mock)
        with self.assertRaises(ModelFailure) as failed:retry.execute()
        self.assertEqual(failed.exception.failure_class,'TAKEOVER_LIMIT')
        self.assertEqual(self.mock.calls,1)

    def test_root_gate_cannot_be_replaced_by_stage_pass(self):
        self.phase.execute()
        with patch.object(self.run,'state',return_value={'status':'COMPLETED'}):
            with self.assertRaises(HostDefect):run_record(self.run)

    def test_host_context_defect_does_not_invoke_takeover(self):
        self.phase.policy={**self.phase.policy,'context_tokens':100}
        with self.assertRaises(HostDefect):self.phase.execute()
        self.assertEqual(self.mock.calls,0)


if __name__=='__main__':unittest.main()
