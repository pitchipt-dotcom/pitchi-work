"""Generic early-proposal reproduction; Host fetches, never invents a hypothesis."""
import json
import unittest
import test_reference_phase_integration as integration
from pitchi_work.research_handoff import HandoffContractError


class GroundingTests(unittest.TestCase):
    def fixture(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups);return f

    def test_early_proposal_is_discarded_then_primary_fetched_and_reproposed(self):
        f=self.fixture()
        class Early(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt)
                if p['phase'].startswith('hypothesis'):
                    self.calls.append((p['phase'],p,prompt,kwargs))
                    return {'hypothesis':'Read the original condition before changing behavior','evidence_indices':[0,1],'paths':['calc.py']}
                return super().ask(role,prompt,**kwargs)
        model=Early();w=f.worker(model);summaries,refs=w.research_reference()
        w.save('research_manifest',{'references':refs,'evidence_catalog':summaries,'handoff_schema_version':2})
        result=w.hypothesis_reference(summaries,refs,lambda v:v)
        self.assertEqual(result['evidence_indices'],[0,1])
        self.assertTrue(w.cached('hypothesis_view_0')['host_grounding_request'])
        self.assertTrue(w.cached('hypothesis_view_1')['host_grounding_request'])
        self.assertEqual(w.cached('hypothesis_view_1')['evidence_id'],w.cached('research_1')['primary_evidence_ids'][0])
        self.assertNotIn('hypothesis',w.cached('hypothesis_view_0'))
        calls=[p for n,p,_,_ in model.calls if n.startswith('hypothesis')]
        self.assertEqual(len(calls),3)
        self.assertIsNone(calls[0]['input']['requested_detail'])
        self.assertIsNotNone(calls[1]['input']['requested_detail'])
        self.assertIsNotNone(calls[2]['input']['requested_detail'])
        self.assertFalse(w.cached('output'))

    def test_legacy_early_proposal_is_still_rejected(self):
        f=self.fixture();w=f.worker(integration.GenericWorker());summaries,refs=w.research_reference()
        w.save('research_manifest',{'references':refs,'evidence_catalog':summaries,'handoff_schema_version':2})
        w.hypothesis_grounding=False
        class Early:
            def ask(self,*args,**kwargs):return {'hypothesis':'Premature','evidence_indices':[0],'paths':['calc.py']}
        w.adapter=Early()
        from pitchi_work.model import ModelFailure
        with self.assertRaises(ModelFailure):w.hypothesis_reference(summaries,refs,lambda v:v)
        self.assertIsNone(w.cached('hypothesis'))


if __name__=='__main__':unittest.main()
