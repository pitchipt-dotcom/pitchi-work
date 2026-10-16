"""Synthetic permission protocol; no training query or subject algorithm."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pitchi_work.research_handoff import HandoffStore, ResearchReferenceScope, CATEGORIES, HandoffContractError
from pitchi_work.runner import HostDefect
from pitchi_work.phase_workflow import PhaseWorker
import test_reference_phase_integration as integration


def proposal(ids):
    return {**{f:[] for f in CATEGORIES},'evidence_refs':ids}


class ReferenceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=HandoffStore(Path(self.tmp.name)/'handoff')
        self.a=self.store.register('current','Use the archive condition before selecting a window.')
        self.b=self.store.register('archive','Window is 24 hours; approval extends it to 48 hours. Never delete records.')
        self.scope=ResearchReferenceScope(self.store,[self.a],[self.b])

    def test_discovery_is_not_permission_scope_feedback_and_retrieval(self):
        ids=[self.a['evidence_id'],self.b['evidence_id']]
        with self.assertRaises(HandoffContractError) as error:self.scope.validate(proposal(ids))
        feedback=error.exception.detail
        self.assertEqual(feedback['allowed_evidence_ids'],ids[:1])
        self.assertEqual(feedback['rejected_evidence_ids'],ids[1:])
        self.assertEqual(feedback['response_examples']['retrieval']['evidence_id'],ids[1])
        packet=self.store.compile(proposal(ids[1:]),[self.b])
        view=self.store.context(packet,ids[1])
        self.scope.authorize(ids[1],view)
        final=self.scope.validate(proposal(ids))
        self.assertEqual(set(final['evidence_refs']),set(ids))
        self.assertEqual(view['detail']['text'],self.store.verify(self.b)['text'])
        self.assertIn('Never delete',view['detail']['text'])
        self.assertTrue(final['unclassified'])  # no semantic classification change

    def test_unknown_or_unretrieved_category_id_is_rejected(self):
        value=proposal([self.a['evidence_id']]);value['conditions']=[self.b['evidence_id']]
        with self.assertRaises(HandoffContractError):self.scope.validate(value)
        value['conditions']=['E'+'0'*24]
        with self.assertRaises(HandoffContractError) as error:self.scope.validate(value)
        self.assertEqual(error.exception.detail['retrievable_rejected_ids'],[])
        with self.assertRaises(HandoffContractError):self.scope.authorize('E'+'0'*24,{})

    def test_forged_detail_and_tampered_hash_never_authorize(self):
        view=self.store.context(self.store.compile(proposal([self.b['evidence_id']]),[self.b]),self.b['evidence_id'])
        view['detail']['text']='Unauthorized rewrite'
        with self.assertRaises(HostDefect):self.scope.authorize(self.b['evidence_id'],view)
        self.assertEqual(list(self.scope.allowed),[self.a['evidence_id']])
        self.store.path(self.b['reference']).write_text('{}')
        with self.assertRaises(HostDefect):ResearchReferenceScope(self.store,[self.a],[self.b])

    def test_schema_cardinality_retention_and_budget_are_not_relaxed(self):
        with self.assertRaises(HandoffContractError):self.scope.validate(proposal([]))
        value=proposal([self.a['evidence_id']]);value['finding']=[self.a['evidence_id']]*9
        with self.assertRaises(HandoffContractError):self.scope.validate(value)
        packet=self.store.compile(proposal([self.b['evidence_id']]),[self.b])
        with self.assertRaises(HandoffContractError):self.store.detail(packet,self.b['evidence_id'],True)
        with self.assertRaises(HostDefect):self.store.context(packet,self.b['evidence_id'],context_tokens=20)


class ScopeWorkflowIntegration(unittest.TestCase):
    def test_supporting_import_cannot_replace_primary_hypothesis_fetch(self):
        fixture=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        class Worker(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                data=json.loads(prompt)
                if data['phase']=='hypothesis_view_1':
                    self.calls.append((data['phase'],data,prompt,kwargs))
                    return {'hypothesis':'A bounded proposal','evidence_indices':[1],'paths':['calc.py']}
                return super().ask(role,prompt,**kwargs)
        worker=fixture.worker(Worker());summaries,refs=worker.research_reference()
        # Historical protocol without automatic Host grounding still rejects
        # a proposal backed only by a supporting (not primary) import.
        worker.hypothesis_grounding=False
        self.assertEqual(len(worker.handoff_packet('research_1')['evidence_refs']),2)
        worker.save('research_manifest',{'references':refs,'evidence_catalog':summaries,'handoff_schema_version':2})
        def accept(value):return value
        from pitchi_work.model import ModelFailure
        with self.assertRaises(ModelFailure) as failure:worker.hypothesis_reference(summaries,refs,accept)
        self.assertEqual(failure.exception.failure_class,'PHASE_CONTRACT_UNSATISFIED')
        self.assertIsNone(worker.cached('hypothesis'))

    def test_rejected_prior_id_can_request_then_use_and_resume(self):
        fixture=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        class Worker(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                data=json.loads(prompt);value=data['input'];phase=data['phase']
                if phase=='research_1_view_0':
                    self.calls.append((phase,data,prompt,kwargs))
                    b=value['prior_evidence_catalog'][0]['evidence_ids'][0]
                    if data['feedback']:
                        assert data['operation']['kind']=='reference_resolution'
                        assert data['feedback']['rejected_evidence_ids']==[b]
                        return {'action':'evidence','evidence_id':b,'page':0}
                    return proposal(value['reference_protocol']['allowed_evidence_ids']+[b])
                return super().ask(role,prompt,**kwargs)
        model=Worker(interrupt='research_1_view_1')
        worker=fixture.worker(model)
        with self.assertRaises(RuntimeError):worker.execute()
        cached=worker.reference('research_1_view_0')
        resumed=fixture.worker(model);resumed.execute()
        packet=resumed.handoff_packet('research_1')
        self.assertEqual(len(packet['evidence_refs']),2)
        wrapper=resumed.cached('research_1')
        self.assertEqual(wrapper['authorization_calls'],[cached])
        self.assertEqual(cached,resumed.reference('research_1_view_0'))
        self.assertEqual(sum(name=='research_1_view_0' for name,_,_,_ in model.calls),2)
        for _,_,prompt,kwargs in model.calls:
            self.assertLessEqual(len(prompt.encode())+512+kwargs['max_output_tokens'],13107)

    def test_old_working_context_attempt_keeps_original_protocol(self):
        fixture=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        worker=fixture.worker(integration.GenericWorker())
        (worker.base/'reference_protocol.json').unlink();worker.reference_protocol=False
        worker.research_reference()
        model=integration.GenericWorker();resumed=fixture.worker(model)
        self.assertFalse(resumed.reference_protocol)
        resumed.research_reference();self.assertEqual(model.calls,[])


if __name__=='__main__':unittest.main()
