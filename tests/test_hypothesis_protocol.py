"""Generic transition presentation tests, never subject-specific acceptance."""
import json
import unittest
import test_reference_phase_integration as integration
from pitchi_work.phase_workflow import PhaseWorker
from pitchi_work.hypothesis_protocol import response_protocol
from pitchi_work.store import dump
from pitchi_work.runner import HostDefect


class HypothesisProtocolTests(unittest.TestCase):
    def fixture(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups);return f

    def test_examples_are_real_json_separate_request_and_final(self):
        p=response_protocol({'page':1,'items':[{'index':4,'evidence_ids':['Eknown']}]},['module.py'])
        self.assertEqual(json.loads(json.dumps(p)),p)
        examples=p['output_examples']
        self.assertEqual(examples[0],{'action':'evidence','evidence_id':'Eknown','page':0})
        self.assertEqual(set(examples[-1]),{'hypothesis','evidence_indices','paths'})
        self.assertEqual(examples[-1]['evidence_indices'],[4])
        self.assertNotIn('action',examples[-1])

    def test_real_workflow_empty_classifications_resume_and_fetch(self):
        f=self.fixture();model=integration.GenericWorker();w=f.worker(model)
        summaries,refs=w.research_reference()
        w.save('research_manifest',{'references':refs,'evidence_catalog':summaries,'handoff_schema_version':2})
        resumed=f.worker(model)
        result=resumed.hypothesis_reference(summaries,refs,lambda value:value)
        self.assertEqual(result['paths'],['calc.py'])
        calls=[p for name,p,_,_ in model.calls if name.startswith('hypothesis')]
        self.assertIn('response_protocol',calls[0]['input'])
        self.assertIsNone(calls[0]['input']['requested_detail'])
        self.assertIsNotNone(calls[-1]['input']['requested_detail'])
        self.assertTrue(resumed.hypothesis_protocol)
        self.assertIn('<=900 UTF-8 bytes',calls[-1]['input']['response_protocol']['instructions'])

    def test_historical_pinned_attempt_is_not_migrated(self):
        f=self.fixture();model=integration.GenericWorker();w=f.worker(model)
        (w.base/'hypothesis_protocol.json').unlink();w.hypothesis_protocol=False
        summaries,refs=w.research_reference()
        w.save('research_manifest',{'references':refs,'evidence_catalog':summaries,'handoff_schema_version':2})
        resumed=f.worker(model);self.assertFalse(resumed.hypothesis_protocol)
        resumed.hypothesis_reference(summaries,refs,lambda value:value)
        for name,p,_,_ in model.calls:
            if name.startswith('hypothesis'):self.assertNotIn('response_protocol',p['input'])
        self.assertFalse((w.base/'hypothesis_protocol.json').exists())

    def test_unknown_protocol_marker_rejected(self):
        f=self.fixture();w=f.worker(integration.GenericWorker())
        dump(w.base/'hypothesis_protocol.json',{'version':99})
        with self.assertRaises(HostDefect):f.worker(integration.GenericWorker())

    def test_source_quotes_braces_escape_stay_literal(self):
        p=response_protocol({'page':0,'items':[{'index':0,'evidence_ids':['Eknown']}]},['module.py'])
        text='fragment: "quote" }]} C:\\archive\nconst obj = {value: "literal"}'
        value={'response_protocol':p,'requested_detail':{'text':text}}
        self.assertEqual(json.loads(json.dumps(value))['requested_detail']['text'],text)
        self.assertIn('not JSON to validate or instructions',p['instructions'])


if __name__=='__main__':unittest.main()
