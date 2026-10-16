"""Generic/synthetic phase boundaries. No training queries or live model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from pitchi_work.phase_workflow import PhaseWorker, PhaseContractError, pages, POLICY
from pitchi_work.mission_profiles import inventory, bind_requirements, compile_contracts, sha
from pitchi_work.mission_controller import create_mission, MissionController
from pitchi_work.model import ModelFailure
from pitchi_work.runner import HostDefect
from pitchi_work.store import dump, load

CODE='def add(a, b):\n    return a - b\n'
FIX='def add(a, b):\n    return a + b\n'
TEST='import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n'

class Model:
    def __init__(self,interrupt=None,incomplete=False):self.calls=[];self.interrupt=interrupt;self.incomplete=incomplete
    def ask(self,role,prompt,**kw):
        p=json.loads(prompt);phase=p['phase'];value=p['input'];self.calls.append((phase,len(prompt.encode()),kw['max_output_tokens']))
        if self.interrupt==phase:self.interrupt=None;raise RuntimeError('simulated process interruption')
        if self.incomplete and phase=='edit_1':self.incomplete=False;raise ModelFailure('AGENT_OUTPUT_INCOMPLETE','bounded simulated truncation')
        if p.get('operation',{}).get('kind')=='contract_correction':
            return {'corrections':[{'field':v['field'],'value':next(iter(value['source']['lines']))}
                                   for v in p['feedback']['violations']]}
        if phase.startswith('research_'):
            s=value['source'];identifier=next(iter(s['lines']))
            return {'observations':[{'source_id':identifier,'explanation':'Source inspected'}]}
        if phase=='hypothesis':return {'hypothesis':'Correct the arithmetic operation','evidence_indices':[0],'paths':['calc.py']}
        if phase=='edit_0':return {'action':'read','path':'calc.py','page':0}
        if phase=='edit_1':
            source=next(s for s in value['sources'] if s['path']=='calc.py')
            return {'action':'patch','path':'calc.py','source_sha256':source['source_sha256'],
                    'start_line':2,'end_line':2,'replacement':'    return a + b\n','summary':'Replace operator'}
        return {'action':'done','summary':'Staged for Host validation, not PASS'}

class PhaseBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        source=self.root/'project';source.mkdir();(source/'calc.py').write_text(CODE)
        (source/'tests').mkdir();(source/'tests/test_calc.py').write_text(TEST)
        self.run=create_mission(self.root/'home',source,'Inspect and repair','http://127.0.0.1:1/v1','synthetic-only')
        catalog=inventory(Path(self.run.state()['workspace']))
        req=bind_requirements({'profile':'bug_fix','targets':['calc.py'],'requirements':['Inspect','repair'],'needs_human':False},'Inspect and repair',catalog)
        self.contract=compile_contracts({'tasks':[{'requirement_ids':['M1','M2'],'targets':['calc.py'],'depends_on':[]}]},req,catalog)['tasks'][0]
        dump(self.run.path/'tasks/P1.json',{'task':self.contract,'status':'PENDING','attempts':[]})
        dump(self.run.path/'plan.json',{'tasks':[self.contract]})

    def test_split_evidence_small_outputs_no_self_pass(self):
        model=Model();worker=PhaseWorker(self.run,self.contract,model,1)
        output=worker.execute();self.assertEqual(output['files']['calc.py'],FIX)
        self.assertEqual((Path(self.run.state()['workspace'])/'calc.py').read_text(),CODE)
        self.assertEqual(self.run.state()['status'],'READY')
        self.assertTrue(any(c[0]=='hypothesis' for c in model.calls))
        self.assertTrue(all(n<15000 and t==2000 for _,n,t in model.calls))
        self.assertTrue(worker.reference('research_manifest')['sha256'])
        self.assertFalse(load(worker.base/'output.receipt.json')['is_task_pass'])

    def test_checkpoint_resumes_staged_patch_without_repeating_completed_calls(self):
        model=Model(interrupt='edit_2');worker=PhaseWorker(self.run,self.contract,model,1)
        with self.assertRaises(RuntimeError):worker.execute()
        before={p.name:sha(p) for p in worker.base.glob('*.receipt.json')}
        self.assertEqual(sum(c[0]=='edit_1' for c in model.calls),1)
        restored=PhaseWorker(self.run,self.contract,model,1);out=restored.execute()
        self.assertEqual(out['files']['calc.py'],FIX)
        self.assertEqual(sum(c[0]=='edit_1' for c in model.calls),1)
        self.assertTrue(all(sha(worker.base/name)==h for name,h in before.items()))
        # A controller interruption after publication is also idempotent.
        (Path(self.run.state()['workspace'])/'calc.py').write_text(FIX)
        call_count=len(model.calls)
        self.assertEqual(PhaseWorker(self.run,self.contract,model,1).execute(),out)
        self.assertEqual(len(model.calls),call_count)

    def test_incomplete_small_call_is_journalled_and_repaired_only_locally(self):
        model=Model(incomplete=True);worker=PhaseWorker(self.run,self.contract,model,1)
        self.assertEqual(worker.execute()['files']['calc.py'],FIX)
        failures=sorted((worker.base/'calls/edit_1').glob('*.json'))
        self.assertEqual(len(failures),2)
        self.assertEqual(load(failures[0])['failure_class'],'AGENT_OUTPUT_INCOMPLETE')
        self.assertEqual(sum(c[0].startswith('research') for c in model.calls),len(worker.source_pages))

    def test_two_incomplete_calls_stop_without_publishing_or_research_retry(self):
        class Bad(Model):
            def ask(self,role,prompt,**kw):
                if json.loads(prompt)['phase']=='edit_1':raise ModelFailure('AGENT_OUTPUT_INCOMPLETE','synthetic truncation')
                return super().ask(role,prompt,**kw)
        worker=PhaseWorker(self.run,self.contract,Bad(),1)
        with self.assertRaisesRegex(ModelFailure,'Bounded phase failed'):worker.execute()
        self.assertEqual(len(list((worker.base/'calls/edit_1').glob('*.json'))),2)
        self.assertEqual((Path(self.run.state()['workspace'])/'calc.py').read_text(),CODE)

    def test_malformed_agent_objects_are_local_output_errors(self):
        for invalid in ([], {'observations':[False]}, {'observations':[{'line':1,'quote':[], 'explanation':''}]}):
            with self.subTest(output=invalid):
                # Each subcase has an independent Host attempt, not a rewritten checkpoint.
                attempt=([], {'observations':[False]}, {'observations':[{'line':1,'quote':[], 'explanation':''}]}).index(invalid)+1
                worker=PhaseWorker(self.run,self.contract,Model(),attempt)
                with patch.object(worker.adapter,'ask',return_value=invalid):
                    with self.assertRaisesRegex(ModelFailure,'Bounded phase failed'):worker.execute()
                self.assertEqual(load(worker.base/'calls/research_0/1.json')['failure_class'],'PHASE_OUTPUT_INVALID')
        self.assertEqual((Path(self.run.state()['workspace'])/'calc.py').read_text(),CODE)

    def test_tampered_hash_or_changed_source_is_host_defect(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1);worker.execute()
        file=worker.base/'research_0.json';data=load(file);data['observations']=[];dump(file,data)
        with self.assertRaises(HostDefect):PhaseWorker(self.run,self.contract,Model(),1).execute()

    def test_completed_call_cannot_be_reused_for_different_input(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1);worker.research()
        before=sha(worker.base/'research_0.json')
        with self.assertRaisesRegex(HostDefect,'dispatch packet changed'):
            worker.call('research_0',{'different_input':True},lambda value:value)
        self.assertEqual(sha(worker.base/'research_0.json'),before)

    def test_changed_contract_and_readonly_test_write_are_rejected(self):
        bad={**self.contract,'allowed_paths':['tests/test_calc.py']}
        with self.assertRaises(HostDefect):PhaseWorker(self.run,bad,Model(),1)
        class OutOfScope(Model):
            def ask(self,role,prompt,**kw):
                if json.loads(prompt)['phase']=='edit_1':
                    return {'action':'patch','path':'tests/test_calc.py','source_sha256':sha(Path(self.run.state()['workspace'])/'tests/test_calc.py'),
                            'start_line':1,'end_line':1,'replacement':'','summary':'bad'}
                return super().ask(role,prompt,**kw)
        model=OutOfScope();model.run=self.run
        with self.assertRaises(ModelFailure):PhaseWorker(self.run,self.contract,model,1).execute()
        self.assertEqual((Path(self.run.state()['workspace'])/'tests/test_calc.py').read_text(),TEST)

    def test_long_unicode_line_is_paginated_without_content_loss(self):
        text='字'*6000+'\nnext\n';view=pages(text,3500)
        self.assertEqual(''.join(p['text'] for p in view),text)
        self.assertTrue(all(len(p['text'].encode())<=3500 for p in view))
        self.assertTrue(view[0]['partial_line'])

    def test_host_source_selection_attaches_exact_quote_and_hash(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        findings,refs=worker.research()
        self.assertEqual(findings[0]['line'],1)
        self.assertEqual(findings[0]['quote'],CODE.splitlines()[0])
        self.assertEqual(findings[0]['source_sha256'],sha(Path(self.run.state()['workspace'])/'calc.py'))
        self.assertEqual(len(refs),len(worker.source_pages))

    def test_retry_repairs_one_field_preserving_context_evidence_and_valid_observation(self):
        class Repair(Model):
            def ask(self,role,prompt,**kw):
                packet=json.loads(prompt);self.packets=getattr(self,'packets',[])+[packet]
                if packet['phase']=='research_0':
                    ids=list(packet['input']['source']['lines'])
                    if packet['feedback'] is None:
                        return {'observations':[{'source_id':ids[0],'explanation':'First observation'},
                                                {'source_id':'not-a-source','explanation':'Second observation'}]}
                    return {'corrections':[{'field':'observations[1].source_id','value':ids[1]}]}
                return super().ask(role,prompt,**kw)
        model=Repair();worker=PhaseWorker(self.run,self.contract,model,1)
        worker.save('seed',{'observations':['Synthetic Host evidence']})
        worker.feedback=[worker.reference('seed')]
        findings,_=worker.research()
        first,retry=model.packets[:2]
        self.assertEqual(first['input'],retry['input'])
        self.assertEqual(retry['input']['host_observations'],[worker.reference('seed')])
        self.assertEqual(retry['feedback']['field'],'observations[1].source_id')
        self.assertIn('input.source.lines',retry['feedback']['required'])
        self.assertEqual(retry['previous_output']['observations'][0]['explanation'],'First observation')
        self.assertEqual(len(worker.cached('research_0')['observations']),2)
        self.assertEqual(findings[1]['quote'],CODE.splitlines()[1])
        self.assertEqual(findings[0]['explanation'],'First observation')

    def test_failed_call_resume_keeps_detailed_feedback_and_previous_output(self):
        model=Model();worker=PhaseWorker(self.run,self.contract,model,1)
        invalid={'observations':[{'source_id':'unknown','explanation':'Inspect'}]}
        count=0
        def interrupt_between_calls():
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('interrupt between calls')
        with patch.object(self.run,'raise_if_cancelled',side_effect=interrupt_between_calls),patch.object(model,'ask',return_value=invalid):
            with self.assertRaises(RuntimeError):worker.research()
        packets=[]
        restored=PhaseWorker(self.run,self.contract,model,1)
        original=model.ask
        def resumed(role,prompt,**kw):
            packets.append(json.loads(prompt));return original(role,prompt,**kw)
        with patch.object(model,'ask',side_effect=resumed):restored.research()
        self.assertEqual(packets[0]['previous_output'],invalid)
        self.assertEqual(packets[0]['feedback']['field'],'observations[0].source_id')
        self.assertEqual(len(list((worker.base/'calls/research_0').glob('*.json'))),2)

    def test_empty_initial_observation_is_valid_but_retry_cannot_erase_proposal(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        with patch.object(worker.adapter,'ask',return_value={'observations':[]}):
            self.assertEqual(worker.research()[0],[])
        retry=PhaseWorker(self.run,self.contract,Model(),2)
        with patch.object(retry.adapter,'ask',side_effect=[
                {'observations':[{'source_id':'unknown','explanation':'Inspect'}]}, {'observations':[]}]):
            with self.assertRaises(ModelFailure) as stopped:retry.research()
        self.assertEqual(stopped.exception.failure_class,'PHASE_CONTRACT_UNSATISFIED')
        self.assertIsNone(retry.cached('research_0'))

    def test_only_actual_size_or_truncation_exhaustion_is_output_limit(self):
        cases=[({'observations':[{'source_id':'unknown','explanation':'Inspect'}]},'CORRECTION_NO_PROGRESS'),
               ({'observations':'wrong type'},'CORRECTION_NO_PROGRESS'),
               ({'observations':[],'extra':'x'*2000},'PHASE_OUTPUT_LIMIT')]
        for attempt,(raw,expected) in enumerate(cases,1):
            worker=PhaseWorker(self.run,self.contract,Model(),attempt)
            with patch.object(worker.adapter,'ask',return_value=raw) as calls:
                with self.assertRaises(ModelFailure) as stopped:worker.research()
            self.assertEqual(stopped.exception.failure_class,expected)
            self.assertEqual(calls.call_count,POLICY['call_attempts'])

    def test_many_short_lines_keep_numbered_source_packets_bounded(self):
        workspace=Path(self.run.state()['workspace']);(workspace/'calc.py').write_text('#\n'*1000)
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        worker.research()
        self.assertTrue(all(size<15000 for _,size,_ in worker.adapter.calls))
        self.assertEqual(''.join(p['text'] for p in worker.source_pages if p['path']=='calc.py'),'#\n'*1000)

    def test_source_id_protocol_handles_long_unicode_line_fragments(self):
        workspace=Path(self.run.state()['workspace']);text='字'*3000+'\n'
        (workspace/'calc.py').write_text(text)
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        findings,_=worker.research()
        parts=[x for x in findings if x['path']=='calc.py']
        self.assertGreater(len(parts),1)
        self.assertTrue(all(x['line']==1 and x['quote'] in text for x in parts))
        self.assertTrue(all(size<15000 for _,size,_ in worker.adapter.calls))

    def correction_case(self,initial,response,check):
        model=Model();worker=PhaseWorker(self.run,self.contract,model,1);packets=[]
        payload={'contract':{'owner':'host'},'evidence':[{'reference':'synthetic-evidence','sha256':'fixed'}]}
        answers=iter([initial,response])
        def ask(role,prompt,**kw):packets.append(json.loads(prompt));return next(answers)
        with patch.object(model,'ask',side_effect=ask):
            result=worker.call('synthetic_correction',payload,check)
        self.assertEqual(packets[0]['operation']['kind'],'phase_execution')
        self.assertEqual(packets[1]['operation']['kind'],'contract_correction')
        self.assertEqual(packets[0]['input'],packets[1]['input'])
        self.assertEqual(packets[1]['previous_output'],initial)
        return result,packets[1]

    def test_correction_shortens_500_bytes_to_200_without_changing_normal_field(self):
        initial={'explanation':'界'*166+'ab','stable':{'value':42}}
        def check(value):
            n=len(value['explanation'].encode())
            if n>200:raise PhaseContractError('explanation','Shorten to <=200 UTF-8 bytes',violation='max_utf8_bytes',limit=200,actual=n,unit='utf8_bytes')
            self.assertEqual(value['stable'],initial['stable']);return value
        result,packet=self.correction_case(initial,{'corrections':[{'field':'explanation','value':'界'*60}]},check)
        self.assertEqual(len(result['explanation'].encode()),180)
        v=packet['feedback']['violations'][0]
        self.assertEqual((v['actual'],v['limit'],v['unit']),(500,200,'utf8_bytes'))
        self.assertEqual(v['previous_value'],{'exists':True,'value':initial['explanation']})

    def test_correction_adds_only_host_declared_missing_field(self):
        initial={'stable':'keep'}
        def check(value):
            if 'required' not in value:raise PhaseContractError('required','Add required field',violation='missing_required_field')
            self.assertEqual(value,{'stable':'keep','required':'supplied'});return value
        result,packet=self.correction_case(initial,{'corrections':[{'field':'required','value':'supplied'}]},check)
        self.assertEqual(result['stable'],'keep')
        self.assertEqual(packet['feedback']['violations'][0]['previous_value'],{'exists':False})

    def test_correction_reduces_five_items_to_allowed_three(self):
        initial={'items':[1,2,3,4,5],'stable':'keep'}
        def check(value):
            if len(value['items'])>3:raise PhaseContractError('items','Select at most three',violation='max_items',limit=3,actual=len(value['items']),unit='items')
            return value
        result,packet=self.correction_case(initial,{'corrections':[{'field':'items','value':[1,2,3]}]},check)
        self.assertEqual(result,{'items':[1,2,3],'stable':'keep'})
        self.assertEqual(packet['feedback']['violations'][0]['limit'],3)

    def test_correction_ignores_normal_and_unknown_fields(self):
        for attempt,field in enumerate(['stable','new_rule'],1):
            worker=PhaseWorker(self.run,self.contract,Model(),attempt)
            def check(value):
                if len(value['explanation'].encode())>200:raise PhaseContractError('explanation','Shorten',limit=200,unit='utf8_bytes')
                return value
            with patch.object(worker.adapter,'ask',side_effect=[{'explanation':'x'*500,'stable':'keep'},
                    {'corrections':[{'field':field,'value':'changed'},{'field':'explanation','value':'short'}]}]) as calls:
                result=worker.call('synthetic_correction',{},check)
            self.assertEqual(result,{'explanation':'short','stable':'keep'})
            self.assertEqual(calls.call_count,2)
            audit=load(worker.base/'calls/synthetic_correction/2.json')['correction_audit']
            self.assertEqual(audit['ignored_fields'],[field])
            self.assertEqual(audit['changed_fields'],['explanation'])

    def test_correction_still_invalid_stops_after_two_dispatches(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        def check(value):
            n=len(value['explanation'].encode())
            if n>200:raise PhaseContractError('explanation','Shorten',limit=200,actual=n,unit='utf8_bytes')
            return value
        with patch.object(worker.adapter,'ask',side_effect=[{'explanation':'x'*500},
                {'corrections':[{'field':'explanation','value':'x'*300}]}]) as calls:
            with self.assertRaises(ModelFailure) as stopped:worker.call('synthetic_correction',{},check)
        self.assertEqual(stopped.exception.failure_class,'PHASE_CONTRACT_UNSATISFIED')
        self.assertEqual(calls.call_count,2)

    def test_identical_full_response_is_not_accepted_as_correction(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        def check(value):
            if len(value['explanation'].encode())>200:
                raise PhaseContractError('explanation','Shorten to <=200 UTF-8 bytes',limit=200,unit='utf8_bytes')
            return value
        initial={'explanation':'x'*500,'stable':'keep'}
        with patch.object(worker.adapter,'ask',return_value=initial) as calls:
            with self.assertRaises(ModelFailure) as stopped:worker.call('synthetic_correction',{},check)
        self.assertEqual(stopped.exception.failure_class,'CORRECTION_NO_PROGRESS')
        self.assertEqual(calls.call_count,2)
        self.assertIsNone(worker.cached('synthetic_correction'))

    def test_byte_correction_soft_target_keeps_hard_acceptance(self):
        for attempt,(initial_text,corrected) in enumerate([('界'*133+'ab','字'*100),('界'*300,'字'*120)],1):
            worker=PhaseWorker(self.run,self.contract,Model(),attempt);packets=[]
            def check(value):
                n=len(value['explanation'].encode())
                if n>400:raise PhaseContractError('explanation','Shorten',violation='max_utf8_bytes',limit=400,actual=n,unit='utf8_bytes')
                self.assertEqual(value['stable'],'keep');return value
            replies=iter([{'explanation':initial_text,'stable':'keep'},
                          {'corrections':[{'field':'explanation','value':corrected},{'field':'stable','value':'ignored'}]}])
            def ask(role,prompt,**kw):packets.append(json.loads(prompt));return next(replies)
            with patch.object(worker.adapter,'ask',side_effect=ask):result=worker.call('synthetic_correction',{},check)
            v=packets[1]['feedback']['violations'][0]
            self.assertEqual((v['actual'],v['limit'],v['soft_target']),(len(initial_text.encode()),400,320))
            self.assertIn('preserve meaning',v['required_action'])
            self.assertEqual(result,{'explanation':corrected,'stable':'keep'})
            audit=load(worker.base/'calls/synthetic_correction/2.json')['correction_audit']
            self.assertEqual(audit['measurements'][0]['corrected_utf8_bytes'],len(corrected.encode()))
            # 360 bytes is above the advisory 320 target but below hard 400: PASS.
            self.assertLessEqual(len(result['explanation'].encode()),400)

    def test_identical_declared_values_detect_no_progress_despite_scope_noise(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        def check(value):
            n=len(value['explanation'].encode())
            if n>400:raise PhaseContractError('explanation','Shorten',limit=400,actual=n,unit='utf8_bytes')
            return value
        initial={'explanation':'x'*401,'stable':'keep'}
        with patch.object(worker.adapter,'ask',side_effect=[initial,{'corrections':[
                {'field':'explanation','value':initial['explanation']},{'field':'stable','value':'ignored'}]}]) as calls:
            with self.assertRaises(ModelFailure) as stopped:worker.call('synthetic_correction',{},check)
        self.assertEqual(stopped.exception.failure_class,'CORRECTION_NO_PROGRESS')
        self.assertEqual(calls.call_count,2)
        record=load(worker.base/'calls/synthetic_correction/2.json')
        self.assertEqual(record['correction_audit']['changed_fields'],[])
        self.assertEqual(record['correction_audit']['ignored_fields'],['stable'])
        self.assertEqual(record['feedback']['violations'][0]['field'],'explanation')
        self.assertIsNone(worker.cached('synthetic_correction'))

    def test_host_remeasures_corrected_utf8_bytes_and_rejects_over_hard_limit(self):
        worker=PhaseWorker(self.run,self.contract,Model(),1)
        def check(value):
            n=len(value['explanation'].encode())
            if n>400:raise PhaseContractError('explanation','Shorten',limit=400,actual=n,unit='utf8_bytes')
            return value
        with patch.object(worker.adapter,'ask',side_effect=[{'explanation':'x'*600},
                {'corrections':[{'field':'explanation','value':'界'*140}]}]):
            with self.assertRaises(ModelFailure) as stopped:worker.call('synthetic_correction',{},check)
        self.assertEqual(stopped.exception.failure_class,'PHASE_CONTRACT_UNSATISFIED')
        record=load(worker.base/'calls/synthetic_correction/2.json')
        self.assertEqual(record['feedback']['violations'][0]['actual'],420)
        self.assertEqual(record['correction_audit']['measurements'][0]['corrected_utf8_bytes'],420)
        self.assertIsNone(worker.cached('synthetic_correction'))

    def test_research_correction_collects_all_oversized_explanations_at_once(self):
        model=Model();worker=PhaseWorker(self.run,self.contract,model,1);packets=[]
        def ask(role,prompt,**kw):
            p=json.loads(prompt);packets.append(p)
            if p['phase']=='research_0':
                if p['operation']['kind']=='phase_execution':
                    ids=list(p['input']['source']['lines'])
                    return {'observations':[{'source_id':ids[0],'explanation':'界'*150},
                                            {'source_id':ids[1],'explanation':'字'*150}]}
                return {'corrections':[{'field':v['field'],'value':'Brief factual observation'} for v in p['feedback']['violations']]}
            return {'observations':[]}
        with patch.object(model,'ask',side_effect=ask):worker.research()
        violations=packets[1]['feedback']['violations']
        self.assertEqual([v['field'] for v in violations],['observations[0].explanation','observations[1].explanation'])
        self.assertTrue(all(v['limit']==400 and v['actual']==450 for v in violations))
        self.assertEqual(len(worker.cached('research_0')['observations']),2)

    def test_controller_records_attempt_before_incomplete_output(self):
        controller=MissionController(self.run)
        with patch.object(controller.adapter,'ask',side_effect=ModelFailure('AGENT_OUTPUT_INCOMPLETE','synthetic')),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ModelFailure):controller.task(self.contract)
        record=load(self.run.path/'tasks/P1.json')
        self.assertEqual(len(record['attempts']),1);self.assertEqual(record['attempts'][0]['state'],'FAILED')
        self.assertEqual(record['attempts'][0]['failure_class'],'PHASE_OUTPUT_LIMIT')

    def test_short_contract_has_no_phase_policy_or_acceptance_changes(self):
        catalog=inventory(Path(self.run.state()['workspace']))
        req=bind_requirements({'profile':'bug_fix','targets':['calc.py'],'requirements':['repair'],'needs_human':False},'repair',catalog)
        contract=compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]},req,catalog)['tasks'][0]
        self.assertNotIn('phase_policy',contract)
        for field in ['validators','completion_conditions','allowed_paths','max_attempts']:
            self.assertEqual(contract[field],self.contract[field])

    def test_large_source_selects_phases_without_changing_validation(self):
        workspace=Path(self.run.state()['workspace'])
        (workspace/'calc.py').write_text(CODE+'# explanatory comment\n'*400)
        catalog=inventory(workspace)
        req=bind_requirements({'profile':'bug_fix','targets':['calc.py'],'requirements':['repair'],'needs_human':False},'repair',catalog)
        contract=compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]},req,catalog)['tasks'][0]
        self.assertIn('phase_policy',contract)
        worker=PhaseWorker(self.run,contract,Model(),1)
        self.assertGreater(len(worker.source_pages),2)
        self.assertTrue(worker.execute()['files']['calc.py'].startswith(FIX))

    @unittest.skipUnless(os.getenv('PITCHI_TEST_SANDBOX')=='1','Explicit Host runner fixture; no real LLM')
    def test_phased_worker_reaches_real_host_validator_and_reviewer(self):
        controller=MissionController(self.run);model=Model(incomplete=True)
        def reply(role,prompt,**kw):
            if role=='reviewer':return {'verdict':'PASS','reason':'Synthetic Reviewer; Host checks are independent'}
            return model.ask(role,prompt,**kw)
        with patch.object(controller.adapter,'ask',side_effect=reply),contextlib.redirect_stdout(io.StringIO()):
            controller.task(self.contract)
        record=load(self.run.path/'tasks/P1.json')
        self.assertEqual(record['status'],'PASS')
        evidence=load(self.run.path/record['attempts'][0]['host_evidence']['reference'])
        self.assertTrue(evidence['passed']);self.assertGreater(evidence['executions'][0]['tests_run'],0)
        self.assertEqual((self.root/'project/calc.py').read_text(),CODE)
        self.assertEqual(self.run.state()['status'],'RUNNING') # Task PASS is NOT COMPLETED.

    @unittest.skipUnless(os.getenv('PITCHI_TEST_SANDBOX')=='1','Explicit Host runner fixture; no real LLM')
    def test_controller_resumes_interrupted_same_attempt_and_then_validates(self):
        controller=MissionController(self.run);model=Model(interrupt='edit_2')
        def reply(role,prompt,**kw):
            if role=='reviewer':return {'verdict':'PASS','reason':'Host evidence checked'}
            return model.ask(role,prompt,**kw)
        with patch.object(controller.adapter,'ask',side_effect=reply),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'process interruption'):controller.task(self.contract)
        record=load(self.run.path/'tasks/P1.json')
        self.assertEqual(record['attempts'][0]['state'],'STARTED')
        restarted=MissionController(self.run)
        with patch.object(restarted.adapter,'ask',side_effect=reply),contextlib.redirect_stdout(io.StringIO()):
            restarted.task(self.contract)
        record=load(self.run.path/'tasks/P1.json')
        self.assertEqual(record['status'],'PASS');self.assertEqual(len(record['attempts']),1)
        self.assertEqual(sum(c[0]=='edit_1' for c in model.calls),1)
        self.assertTrue(load(self.run.path/record['attempts'][0]['host_evidence']['reference'])['passed'])

if __name__=='__main__':unittest.main()
