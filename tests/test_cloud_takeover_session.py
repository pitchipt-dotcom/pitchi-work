"""Synthetic model protocol, mocked SDK only; no real network or training data."""
import copy
import json
import unittest
from unittest.mock import Mock
from types import SimpleNamespace as NS
import test_reference_phase_integration as fixture
from pitchi_work.cloud import configuration, fingerprint, CloudControlStop
from pitchi_work.cloud_takeover_live import TakeoverBudget
from pitchi_work.cloud_takeover_session import CloudTakeoverSession
from pitchi_work.edit_targets import text_hash
from pitchi_work.resolution import read_verified
from pitchi_work.staged_validation import observe_staged
from pitchi_work.store import load
from pitchi_work.runner import HostDefect


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ReferencePhaseIntegration();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.phase=self.fixture.worker(fixture.GenericWorker())
        self.receipt=observe_staged(self.phase.run,self.phase.contract,self.phase.files,self.phase.base/'session_baseline')
        store=self.phase.working
        self.references={'E1':{'kind':'evidence','ref':store.persist({'finding':'Inputs must be added, not subtracted.'})},
            'B1':{'kind':'before','ref':store.persist({'query':'synthetic arithmetic','observed':-1,'expected':5})},
            'R1':{'kind':'receipt','ref':store.persist(self.receipt)}}
        self.checks={'passed':False,'reference':self.references['R1']['ref'],'receipt':self.receipt}
        self.hypothesis={'hypothesis':'Inspect presentation only','paths':['calc.py']}
        self.config={**configuration('openai',allowed=True),'max_output_tokens':2000}
        self.budget=TakeoverBudget(self.fixture.root/'budget.json',{'max_calls':16,
            'limits_usd':{'openai':.5,'anthropic':.5},'rates':{'openai':{'gpt-6-luna':[.1,.5]}}})
        self.session=self.reopen()
        self.client=Mock()

    def reopen(self):
        return CloudTakeoverSession(self.phase,self.budget,self.config,self.references,self.hypothesis,self.checks)

    def call(self,response,approval=None):
        payload,preview=self.session.prepare()
        self.client.responses.create.return_value=NS(model='gpt-6-luna',status='completed',
            output_text=json.dumps(response),usage=NS(input_tokens=30,output_tokens=50,total_tokens=80),id='mock')
        return self.session.execute(payload,approval or preview['sha256'],client=self.client)

    def test_evidence_before_source_reconsider_target_edit_and_independent_host_tests(self):
        self.call({'action':'evidence','id':'E1','page':0})
        self.assertIn('not subtracted',self.session.state()['detail']['text'])
        self.call({'action':'evidence','id':'B1','page':0})
        self.assertIn('"observed":-1',self.session.state()['detail']['text'])
        self.session=self.reopen()  # Same reference, counters and budget on resume.
        self.call({'action':'read','path':'calc.py','page':0})
        self.assertIn('return a - b',self.session.state()['detail']['detail']['text'])
        self.call({'action':'reconsider','decision':'revise','hypothesis':'Use addition for the two inputs',
            'paths':['calc.py'],'test_reference':self.checks['reference']['reference']})
        self.assertNotEqual(self.session.state()['proposal'],self.hypothesis)
        self.call({'action':'edit_target','path':'calc.py','source_sha256':text_hash(self.phase.files['calc.py']),
            'start_line':2,'end_line':2})
        target=self.session.state()['detail']['target']['target_id']
        files,record=self.call({'action':'replace_target','target_id':target,'new_text':'    return a + b\n'})
        self.assertEqual(record['status'],'HOST_STAGE_PASS')
        self.assertEqual(record['comparison']['improved_ids'],['tests/test_calc.py'])
        self.assertIn('return a + b',files['calc.py'])
        self.assertEqual(self.phase.run.state()['status'],'READY')
        self.assertEqual(self.session.state()['steps'],6)
        self.assertEqual(len(read_verified(self.budget.path)['calls']),6)
        self.assertEqual(self.client.responses.create.call_count,6)
        for record_path in (self.phase.run.path/'private_takeovers').glob('*/record.json'):
            self.assertFalse(load(record_path)['completion'])
        with self.assertRaises(CloudControlStop):self.reopen().prepare()

    def test_no_approval_no_external_call(self):
        with self.assertRaises(CloudControlStop):self.call({'action':'evidence','id':'E1','page':0},'wrong')
        self.client.responses.create.assert_not_called()
        self.assertEqual(read_verified(self.budget.path)['calls'],[])

    def test_unknown_reference_stops(self):
        with self.assertRaises(ValueError):self.call({'action':'evidence','id':'unknown','page':0})
        with self.assertRaises(CloudControlStop):self.session.prepare()

    def test_hash_tamper_rejected(self):
        ref=self.references['E1']['ref'];path=self.phase.run.path/ref['reference']
        path.write_text('{}')
        with self.assertRaises(HostDefect):self.call({'action':'evidence','id':'E1','page':0})

    def test_repeated_request_stops_without_reset_on_resume(self):
        request={'action':'evidence','id':'E1','page':0}
        self.call(request)
        with self.assertRaisesRegex(ValueError,'NO_PROGRESS'):self.call(request)
        with self.assertRaises(CloudControlStop):self.reopen().prepare()
        self.assertEqual(self.session.state()['steps'],2)

    def test_outside_source_shell_and_stale_target_hash_rejected(self):
        for operation in ({'action':'read','path':'/etc/passwd','page':0},
                {'action':'shell','command':'echo unsafe'},
                {'action':'edit_target','path':'calc.py','source_sha256':'0'*64,'start_line':2,'end_line':2}):
            with self.assertRaises(ValueError):self.session._resolve(operation,self.phase.files,self.session.state())

    def test_navigation_and_source_search_are_host_bounded(self):
        self.call({'action':'source_search','term':'def add'})
        self.assertEqual(self.session.state()['detail']['hits'][0]['path'],'calc.py')
        self.call({'action':'navigation','path':'calc.py'})
        self.assertEqual(self.session.state()['detail']['outline'][0]['symbol'],'add')

    def test_reconsideration_wrong_receipt_and_scope_rejected(self):
        raw={'action':'reconsider','decision':'reject','hypothesis':'A different explanation',
            'paths':['calc.py'],'test_reference':'foreign'}
        with self.assertRaises(ValueError):self.session._resolve(raw,self.phase.files,self.session.state())
        raw.update(test_reference=self.checks['reference']['reference'],paths=['outside.py'])
        with self.assertRaises(ValueError):self.session._resolve(raw,self.phase.files,self.session.state())

    def test_no_test_pass_no_source_commit(self):
        self.call({'action':'edit_target','path':'calc.py','source_sha256':text_hash(self.phase.files['calc.py']),
            'start_line':2,'end_line':2})
        target=self.session.state()['detail']['target']['target_id']
        _,record=self.call({'action':'replace_target','target_id':target,'new_text':'    return a * b\n'})
        self.assertEqual(record['status'],'HOST_TEST_REJECTED')
        self.assertEqual(self.phase.working.read(self.session.state()['files']),self.phase.files)
        self.assertNotEqual(self.phase.run.state()['status'],'COMPLETED')

    def test_guard_binding_and_finite_operations_preserved(self):
        payload,_=self.session.prepare()
        self.assertLess(len(json.dumps(payload).encode())+2512,self.phase.policy['context_tokens']*.8)
        self.phase.policy=copy.deepcopy(self.phase.policy)
        self.phase.policy['context_tokens']=100
        with self.assertRaises(CloudControlStop):self.session.prepare()
        with self.assertRaises(ValueError):
            CloudTakeoverSession(self.phase,self.budget,self.config,self.references,
                {'hypothesis':'Changed initial state','paths':['calc.py']},self.checks)

    def test_existing_consumed_operations_not_reset(self):
        # Separate new connection, on a run already having consumed the ceiling.
        self.session.path.unlink()
        folder=self.phase.run.path/'private_takeovers/edit_15';folder.mkdir(parents=True)
        session=self.reopen()
        self.assertEqual(session.state()['steps'],16)
        with self.assertRaises(CloudControlStop):session.prepare()
        self.client.responses.create.assert_not_called()

    def test_failed_receipt_source_binding_required(self):
        checks=copy.deepcopy(self.checks)
        checks['receipt']['source_hashes']['calc.py']='0'*64
        references=copy.deepcopy(self.references)
        references['R1']['ref']=self.phase.working.persist(checks['receipt'])
        checks['reference']=references['R1']['ref']
        with self.assertRaisesRegex(ValueError,'source snapshot'):
            CloudTakeoverSession(self.phase,self.budget,self.config,references,self.hypothesis,checks)

    def test_stale_approval_packet_cannot_be_reused_after_retrieval(self):
        payload,preview=self.session.prepare()
        self.call({'action':'evidence','id':'E1','page':0})
        with self.assertRaisesRegex(ValueError,'Stale session packet'):
            self.session.execute(payload,preview['sha256'],client=self.client)
        self.assertEqual(self.client.responses.create.call_count,1)


if __name__=='__main__':unittest.main()
