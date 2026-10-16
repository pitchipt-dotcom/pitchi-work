"""Real adapter routing with mocked official SDK; synthetic inputs only."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
import test_reference_phase_integration as fixture

from pitchi_work.cloud import configuration, fingerprint, CloudControlStop
from pitchi_work.cloud_takeover_live import TakeoverBudget, LiveEditTakeover, apply_edit
from pitchi_work.resolution import read_verified
from pitchi_work.store import RunStore,load,dump


class LiveTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ReferencePhaseIntegration();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.run=self.fixture.run;self.contract=self.fixture.contract
        self.policy={'max_calls':2,'limits_usd':{'openai':0.5,'anthropic':0.5},
                     'rates':{'openai':{'gpt-6-luna':[0.1,0.5]}}}
        self.budget=TakeoverBudget(self.fixture.root/'private_budget/ledger.json',self.policy)
        self.payload={'provider':'openai','model':'gpt-6-luna','max_output_tokens':2000,
                      'messages':[{'role':'user','content':'Synthetic atomic edit'}]}
        self.config={**configuration('openai',allowed=True),'max_output_tokens':2000}
        text=self.fixture.original
        import hashlib
        self.edit={'action':'replace','path':'calc.py','source_sha256':hashlib.sha256(text.encode()).hexdigest(),
                   'old_text':'return a - b','new_text':'return a + b','summary':'Repair arithmetic'}
        self.client=Mock();self.client.responses.create.return_value=NS(model='gpt-6-luna',status='completed',
            output_text=json.dumps(self.edit),usage=NS(input_tokens=20,output_tokens=30,total_tokens=50),id='synthetic')
        self.phase=self.fixture.worker(fixture.GenericWorker())

    def test_live_adapter_patch_host_tests_and_durable_usage(self):
        session=LiveEditTakeover(self.run,self.contract,self.budget,self.config)
        files,record=session.execute(self.payload,fingerprint(self.payload),self.phase.files,'edit_1',client=self.client)
        self.assertIn('return a + b',files['calc.py'])
        self.assertEqual(record['status'],'HOST_STAGE_PASS')
        self.assertEqual(record['comparison']['improved_ids'],['tests/test_calc.py'])
        ledger=read_verified(self.budget.path)
        self.assertEqual(len(ledger['calls']),1);self.assertEqual(ledger['calls'][0]['usage']['completion_tokens'],30)
        self.assertEqual(self.run.state()['status'],'READY')
        from pitchi_work.cloud_takeover import run_record
        exported=run_record(self.run)
        self.assertEqual(exported['completion_classification'],'NOT_COMPLETED')
        self.assertEqual(exported['external_stages'],1)
        self.assertEqual(exported['api_calls'],1)
        reopened=TakeoverBudget(self.budget.path,self.policy)
        with self.assertRaises(CloudControlStop):reopened.reserve(self.payload,fingerprint(self.payload),'other_child')

    def test_no_send_without_exact_approval(self):
        session=LiveEditTakeover(self.run,self.contract,self.budget,self.config)
        with self.assertRaises(CloudControlStop):session.execute(self.payload,'wrong',self.phase.files,'edit_1',client=self.client)
        self.client.responses.create.assert_not_called()
        self.assertEqual(read_verified(self.budget.path)['calls'],[])

    def test_scope_hash_and_invalid_patch_rejected(self):
        for field,value in [('path','other.py'),('source_sha256','0'*64),('old_text','not present')]:
            with self.subTest(field=field),self.assertRaises(ValueError):apply_edit({**self.edit,field:value},self.phase.files,self.contract)

    def test_unknown_cost_and_budget_limit_stop_before_send(self):
        with self.assertRaises(CloudControlStop):self.budget.preview({**self.payload,'provider':'anthropic'})
        huge={**self.payload,'max_output_tokens':2_000_000}
        with self.assertRaises(CloudControlStop):self.budget.reserve(huge,fingerprint(huge),'child')

    def test_unconfirmed_call_keeps_entire_reservation(self):
        index=self.budget.reserve(self.payload,fingerprint(self.payload),'child')
        before=read_verified(self.budget.path)['calls'][index]['charged_bound_usd']
        self.budget.settle(index,{})
        self.assertEqual(read_verified(self.budget.path)['calls'][index]['charged_bound_usd'],before)
        with self.assertRaises(CloudControlStop):TakeoverBudget(self.budget.path,{**self.policy,'max_calls':16})

    def test_existing_operation_limit_preserved(self):
        session=LiveEditTakeover(self.run,self.contract,self.budget,self.config)
        with self.assertRaises(CloudControlStop):session.execute(self.payload,fingerprint(self.payload),self.phase.files,'edit_16',client=self.client)
        self.client.responses.create.assert_not_called()

    def test_child_cannot_reset_grant(self):
        LiveEditTakeover(self.run,self.contract,self.budget,self.config)
        child=RunStore(self.fixture.root/'child')
        dump(child.path/'state.json',{'status':'READY','parent_run':str(self.run.path)})
        other=TakeoverBudget(self.fixture.root/'other_budget/ledger.json',self.policy)
        with self.assertRaises(CloudControlStop):LiveEditTakeover(child,self.contract,other,self.config)
        LiveEditTakeover(child,self.contract,self.budget,self.config)
        self.assertEqual(child.state()['takeover_budget_path'],str(self.budget.path.resolve()))

    def test_mocked_sdk_target_edit_uses_host_block_and_independent_tests(self):
        from pitchi_work.edit_targets import EditTargets
        targets=EditTargets(self.phase.base/'cloud_targets.json',self.contract['allowed_paths'],self.contract['sha256'])
        target=targets.issue(self.phase.files,'calc.py',2,2)
        response={'action':'replace_target','target_id':target['target_id'],'new_text':'    return a + b\n'}
        self.client.responses.create.return_value.output_text=json.dumps(response)
        session=LiveEditTakeover(self.run,self.contract,self.budget,self.config)
        files,record=session.execute(self.payload,fingerprint(self.payload),self.phase.files,'edit_1',client=self.client,targets=targets)
        self.assertIn('return a + b',files['calc.py'])
        self.assertEqual(record['status'],'HOST_STAGE_PASS')
        self.assertEqual(record['edited_paths'],['calc.py'])
        self.assertNotIn('old_text',load(self.run.path/'private_takeovers/edit_1/output.json'))


if __name__=='__main__':unittest.main()
