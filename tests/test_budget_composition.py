"""Generic reservation, lossless references and ordinary-entry integration."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pitchi_work.working_context import WorkingContext, encoded
from pitchi_work.runner import HostDefect
import test_reference_phase_integration as reference_tests


class BudgetCompositionTests(unittest.TestCase):
    def test_existing_navigation_projections_reserve_scope_before_admission(self):
        scope=[{'path':'calc.py','write_allowed':True,'source_sha256':'a'*64}]
        payload={'task_goal':'x'*7300,'host_contract':{'scope':['calc.py']},'rules':'Keep acceptance',
            'diagnostic_access':{'test_result':{'action':'test_result','page':0}},
            'sources':scope+[{'path':'readonly'+str(i),'write_allowed':False} for i in range(100)],
            'catalog_access':'Redundant navigation prose '*100,
            'history_access':'Redundant history prose '*100,
            'candidate_reasoning':{'recent_trials':[],'trial_access':None,'rules':'Review policy defined in trial_review'},
            'trial_review':{'force_after':4,'warn_after':2,'required':False},
            'current_objective':{'phase':'implement_task','rules':'Original Host navigation narration '*40}}
        selected=self.context.compose(payload,16384,2000)
        self.assertEqual(selected['sources'],scope)
        for key in ('task_goal','host_contract','rules','trial_review'):
            self.assertEqual(selected[key],payload[key])
        self.assertLessEqual(len(encoded(selected)),8795)
        audit=self.audits()[-1]
        self.assertEqual(self.context.read(audit['original']),payload)

    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.context=WorkingContext(self.root/'history',self.root)

    def test_reserves_fixed_envelope_output_and_preserves_source(self):
        payload={'task_goal':'Inspect generic code','host_contract':{'scope':['calc.py']},
            'rules':'Host alone accepts','source_view':{'path':'calc.py','kind':'edit_target',
                'edit_target':{'target_id':'T1'},'text':'def calculate():\n    return 1\n'},
            'source_navigation':{'symbols':{'calc.py':['x'*10000]},'remaining_operations':15}}
        selected=self.context.compose(payload,16384,2000,envelope_reserve=600)
        self.assertLessEqual(len(encoded(selected))+600+2000+512,16384*80//100)
        for field in ('task_goal','host_contract','rules','source_view'):
            self.assertEqual(selected[field],payload[field])
        nav=selected['source_navigation']
        self.assertTrue(nav['body_deferred'])
        self.assertEqual(self.original('source_navigation'),payload['source_navigation'])
        audit=self.audits()[-1]
        self.assertEqual(audit['envelope_reserved_bytes'],600)

    def audits(self):
        return [v for p in self.context.root.glob('*.json')
            if isinstance(v:=json.loads(p.read_text()),dict) and
            v.get('operation')=='working_context_composition']

    def original(self,field):
        return self.context.read(self.audits()[-1]['materials'][field]['reference'])

    def test_large_target_is_referenced_not_cut_and_requires_reselection(self):
        source='def calculate():\n    return 1\n'*2000
        payload={'rules':'Never relax scope','source_view':{'path':'calc.py',
            'edit_target':{'target_id':'T1','source_sha256':'a'*64},'text':source}}
        selected=self.context.compose(payload,16384,2000)
        view=selected['source_view']
        self.assertNotIn('text',view)
        self.assertTrue(view['requires_source_read_and_target_selection'])
        self.assertEqual(view['request'],{'action':'read','path':'calc.py','page':0})
        reopened=WorkingContext(self.context.root,self.root)
        ref=self.audits()[-1]['materials']['source_view']['reference']
        self.assertEqual(reopened.read(ref)['text'],source)
        self.assertIn('calculate',reopened.detail(ref,0)['text'])

    def test_new_material_kind_needs_no_new_overflow_branch(self):
        payload={'rules':'Fixed','new_inspection':{'text':'Original detail '*3000}}
        selected=self.context.compose(payload,16384,2000,materials={
            'new_inspection':{'priority':5,'fallback':{'count':3000},
                'request':{'action':'observations','page':0}}})
        self.assertLessEqual(len(encoded(selected)),8795)
        self.assertEqual(self.original('new_inspection'),payload['new_inspection'])

    def test_impossible_fixed_input_is_not_cut_and_records_components(self):
        payload={'task_goal':'x'*20000,'rules':'Must remain intact'}
        with self.assertRaises(HostDefect):self.context.compose(payload,16384,2000)
        audit=self.audits()[-1]
        self.assertEqual(audit['status'],'INFEASIBLE')
        self.assertEqual(audit['field_bytes']['task_goal'],20002)
        self.assertEqual(payload['task_goal'],'x'*20000)

    def test_catalog_projection_keeps_ids_and_nonempty_classification(self):
        items=[{'index':i,'evidence_ids':[str(i)],'conditions':['only with approval'],
            'source':{'path':'metadata '*100},'finding':[]} for i in range(4)]
        payload={'task_goal':'x'*7200,'context_catalog_version':1,
            'catalog_view':{'page':0,'page_count':1,'items':items}}
        selected=self.context.compose(payload,16384,2000)
        self.assertTrue(selected['catalog_view']['body_deferred'])
        for item in selected['catalog_view']['items']:
            self.assertEqual(item['conditions'],['only with approval'])
            self.assertEqual(item['evidence_ids'],[str(item['index'])])
        self.assertEqual(self.original('catalog_view'),payload['catalog_view'])

    def test_evidence_paging_is_exact_complete_and_hash_bound(self):
        from pitchi_work.research_handoff import HandoffStore,CATEGORIES
        store=HandoffStore(self.root/'handoff');text=('条件72時間、例外は事前承認。\n'*100)
        ref=store.register('synthetic',text)
        packet=store.compile({**{f:[] for f in CATEGORIES},'evidence_refs':[ref['evidence_id']]},[ref])
        first=store.detail(packet,ref['evidence_id'],0,page_bytes=1200)
        details=[store.detail(packet,ref['evidence_id'],i,page_bytes=1200) for i in range(first['page_count'])]
        self.assertEqual(''.join(d['text'] for d in details),text)
        self.assertTrue(all(d['source_sha256']==ref['source_sha256'] and len(d['text'].encode())<=1200 for d in details))
        with self.assertRaises(HostDefect):store.detail(packet,ref['evidence_id'],page_bytes=3001)

    def test_requested_body_hashes_and_scope_survive_metadata_deferral(self):
        body={'evidence_id':'Eknown','page':0,'text':'exact requested condition',
            'source_sha256':'a'*64,'detail_sha256':'b'*64}
        detail={'source':{'path':'calc.py','sha256':'a'*64},'detail':body,
            'handoff':{'evidence_refs':['Eknown'],'interpretation':'ASSISTED','catalog':['metadata'*1000]}}
        payload={'task_goal':'x'*7500,'context_catalog_version':1,'requested_detail':detail}
        selected=self.context.compose(payload,16384,2000)
        self.assertEqual(selected['requested_detail']['detail'],body)
        self.assertEqual(selected['requested_detail']['source'],detail['source'])
        self.assertEqual(selected['requested_detail']['handoff']['evidence_refs'],['Eknown'])
        self.assertEqual(self.original('requested_detail'),detail)

    def test_initial_and_retry_reserve_the_same_actual_outer_envelope(self):
        operation={'kind':'phase_execution','instructions':'Execute','response_schema':None}
        payload={'task_goal':'x'*10300,'working_context_composition':1,'context_envelope_version':1}
        selected,previous,_=self.context.dispatch(payload,operation,None,None,16384,2000,phase_name='hypothesis_view_1')
        prompt={'phase':'hypothesis_view_1','operation':operation,'input':selected,'feedback':None,'previous_output':previous}
        self.assertLessEqual(len(encoded(prompt))+2000+512,16384*80//100)
        self.assertEqual(selected['task_goal'],payload['task_goal'])
        with self.assertRaises(HostDefect):self.context.dispatch(payload,operation,{'required':'x'*1000},
            None,16384,2000,phase_name='hypothesis_view_1')

    def test_material_permission_exact_pages_resume_and_tamper_rejection(self):
        from pitchi_work.research_handoff import HandoffContractError
        original={'conditions':['72 hours; exception only before deadline']*40}
        identity=self.context.authorize_material(original)
        resumed=WorkingContext(self.context.root,self.root)
        first=resumed.material(identity,0)
        text=''.join(resumed.material(identity,i)['text'] for i in range(first['page_count']))
        self.assertEqual(json.loads(text),original)
        unregistered=self.context.persist({'not_authorized':True})
        with self.assertRaises(HandoffContractError):resumed.material(unregistered['content_sha256'],0)
        with self.assertRaises(HandoffContractError):resumed.material(identity,999)
        (self.context.root/(identity+'.json')).write_text('{}')
        with self.assertRaises(HostDefect):resumed.material(identity,0)

    def test_current_goal_precedes_automatic_source_and_legacy_order_replays(self):
        payload={'task_goal':'Exact immutable task condition. '*170,
            'host_contract':{'scope':['calc.py'],'validators':['scope','tests']},
            'source_view':{'path':'calc.py','text':'# exact source line\n'*300}}
        legacy=self.context.compose({**payload,'material_protocol_version':1},16384,2000)
        current=self.context.compose({**payload,'material_protocol_version':2},16384,2000)
        self.assertTrue(legacy['task_goal']['body_deferred'])
        self.assertEqual(current['task_goal'],payload['task_goal'])
        self.assertEqual(current['host_contract'],payload['host_contract'])
        self.assertTrue(current['source_view']['requires_source_read_and_target_selection'])
        self.assertLessEqual(len(encoded(current)),8795)

    def test_compact_material_cards_keep_authorized_identity_and_old_shape(self):
        goal=['Exact generic condition '+str(i)+' '+('x'*80) for i in range(100)]
        payload={'task_goal':goal,'host_contract':{'scope':['calc.py']},
            'material_protocol':'Request context_material(context_id,page); Host verifies scope/hash.',
            'diagnostic_access':{'test_result':{'action':'test_result','page':0},'rules':'Repeated narration '*30},
            'tool_state':{'last_action':{'action':'read'},'changed_paths':[], 'read_rule':'Repeated narration '*30}}
        old=self.context.compose({**payload,'material_protocol_version':2},16384,2000)
        new=self.context.compose({**payload,'material_protocol_version':3,'context_navigation_version':1},16384,2000)
        self.assertIn('request',old['task_goal'])
        self.assertEqual(set(new['task_goal']),{'context_id','body_deferred'})
        identity=new['task_goal']['context_id'];detail=self.context.material(identity,0)
        reconstructed=''.join(self.context.material(identity,p)['text'] for p in range(detail['page_count']))
        self.assertEqual(json.loads(reconstructed),goal)
        self.assertEqual(new['host_contract'],payload['host_contract'])
        self.assertEqual(new['tool_state']['last_action'],payload['tool_state']['last_action'])
        self.assertIn('test_result',new['diagnostic_access'])



class BudgetEntryIntegration(unittest.TestCase):
    def test_edit_reads_deferred_material_then_targets_and_host_tests(self):
        fixture=reference_tests.ReferencePhaseIntegration()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.contract['goal']=['Generic immutable objective '+ 'condition '*300]
        from pitchi_work.phase_workflow import digest
        fixture.contract['sha256']=digest({k:v for k,v in fixture.contract.items() if k!='sha256'})
        from pitchi_work.procedural_memory import ProcedureStore
        store=ProcedureStore(fixture.run.path.parent.parent);refs=[]
        for i in range(2):
            refs.append(store.publish({'id':'generic-'+str(i),'profile':'bug_fix','reviewed':True,
                'applicability':['Observed bug'],'checks':['Use Host evidence'],
                'steps':['Keep conditions and exceptions. '*40],
                'success':['Independent tests pass'],'avoid':['Do not change acceptance']}))
        fixture.run.save_state(procedure_policy={'references':refs})
        class Model(reference_tests.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);v=p['input'];phase=p['phase']
                if not phase.startswith('edit_'):return super().ask(role,prompt,**kwargs)
                self.calls.append((phase,p,prompt,kwargs));step=int(phase.split('_')[1])
                if step==0:
                    assert 'context_material' in [s['properties']['action']['enum'][0] for s in kwargs['output_schema']['oneOf']]
                    assert v['abstract_procedures']['body_deferred']
                    identity=v['abstract_procedures']['context_id']
                    return {'action':'context_material','context_id':identity,'page':0}
                if step==1:
                    assert v['requested_material']['text']
                    s=next(s for s in v['sources'] if s['path']=='calc.py')
                    return {'action':'edit_target','path':'calc.py','source_sha256':s['source_sha256'],
                        'start_line':2,'end_line':2}
                if step==2:return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                    'new_text':'    return a + b\n'}
                return {'action':'done','summary':'Host alone verifies'}
        model=Model();fixture.worker_instance=fixture.worker(model)
        output=fixture.worker_instance.execute()
        self.assertIn('return a + b',output['files']['calc.py'])
        self.assertTrue(any(name.startswith('edit_') for name,*_ in model.calls))
        for _,_,prompt,_ in model.calls:self.assertLessEqual(len(prompt.encode())+2000+512,16384*80//100)
    def test_research_reserves_actual_envelope_and_replays_pinned_packets(self):
        fixture=reference_tests.ReferencePhaseIntegration()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        from pitchi_work.phase_workflow import digest
        from pitchi_work.store import load
        fixture.contract['goal']=['Inspect the exact evidence. '+ 'objective '*390]
        fixture.contract['sha256']=digest({k:v for k,v in fixture.contract.items() if k!='sha256'})
        model=reference_tests.GenericWorker();worker=fixture.worker(model)
        summaries,refs=worker.research_reference()
        self.assertGreaterEqual(len(refs),3)
        self.assertTrue(all(p['input']['context_envelope_version']==1 for _,p,_,_ in model.calls))
        for _,p,prompt,_ in model.calls:
            self.assertLessEqual(len(prompt.encode())+512+2000,16384*80//100)
            self.assertEqual(p['input']['task_goal'],fixture.contract['goal'])
        packets={p.name:p.read_bytes() for p in (worker.base/'packets').glob('*.json')}
        # Recovery uses the same Host reader and honors pinned policy; no calls.
        resumed_model=reference_tests.GenericWorker();resumed=fixture.worker(resumed_model)
        self.assertEqual(resumed.research_reference(),(summaries,refs))
        self.assertEqual(resumed_model.calls,[])
        self.assertEqual(packets,{p.name:p.read_bytes() for p in (worker.base/'packets').glob('*.json')})
        audits=[load(p) for p in worker.working.root.glob('*.json')]
        self.assertTrue(any(isinstance(a,dict) and a.get('operation')=='working_context_composition'
            and a.get('original_bytes',0)>8795 and a.get('status')=='READY' for a in audits))

    def test_large_target_existing_read_reselection_and_host_tests(self):
        fixture=reference_tests.ReferencePhaseIntegration()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        class Model(reference_tests.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);v=p['input'];phase=p['phase']
                if not phase.startswith('edit_'):return super().ask(role,prompt,**kwargs)
                self.calls.append((phase,p,prompt,kwargs))
                step=int(phase.split('_')[1])
                source=next(s for s in v['sources'] if s['path']=='calc.py')
                if step in (0,2):
                    if step==2:
                        assert v['source_view']['body_deferred']
                        assert 'text' not in v['source_view']
                    return {'action':'read','path':'calc.py','page':0}
                if step in (1,3):return {'action':'edit_target','path':'calc.py',
                    'source_sha256':source['source_sha256'],'start_line':3 if step==1 else 2,
                    'end_line':70 if step==1 else 2}
                if step==4:return {'action':'replace_target',
                    'target_id':v['source_view']['edit_target']['target_id'],
                    'new_text':'    return a + b\n'}
                return {'action':'done','summary':'Independent Host validation required'}
        # Long complete lines, never an incomplete code fragment.
        source=Path(fixture.run.state()['workspace'])/'calc.py'
        source.write_text('def add(a, b):\n    return a - b\n'+('# exact observation '*6+'\n')*90)
        # Recompile from the actual synthetic source, before any phase is pinned.
        from pitchi_work.mission_profiles import inventory, bind_requirements, compile_contracts
        catalog=inventory(source.parent)
        req=bind_requirements({'profile':'bug_fix','targets':['calc.py'],
            'requirements':['Inspect','repair'],'needs_human':False},'Inspect and repair the arithmetic module',catalog)
        fixture.contract=compile_contracts(fixture.coarse,req,catalog,research_handoff_version=2)['tasks'][0]
        worker=fixture.worker(Model());result=worker.execute()
        self.assertIn('return a + b',result['files']['calc.py'])
        receipts=[json.loads(p.read_text()) for p in (worker.base/'staged_tests').glob('*/receipt.json')]
        self.assertTrue(any(all(r['passed'] for r in receipt['results']) for receipt in receipts))

    def test_normal_v2_worker_calls_use_shared_composer(self):
        fixture=reference_tests.ReferencePhaseIntegration()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        worker=reference_tests.GenericWorker()
        # Inherited setup uses real compiler/controller contracts, not packet fabrication.
        result=fixture.worker(worker).execute()
        self.assertIsNotNone(result)
        self.assertTrue(worker.calls)
        self.assertTrue(all(p['input'].get('working_context_composition')==1 for _,p,_,_ in worker.calls))
