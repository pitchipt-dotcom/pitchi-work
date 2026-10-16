import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from pitchi_work.procedural_memory import ProcedureStore,pinned_procedures
from pitchi_work.store import RunStore,dump,load
from pitchi_work.runner import HostDefect

def procedure():
    return {'id':'diagnose','profile':'generic','reviewed':True,
            'applicability':['Observed failed validation'],
            'checks':['Compare symptom, hypothesis, evidence and validation separately'],
            'steps':['Request missing evidence through the Host before editing'],
            'success':['Host tests and before/after observations improve without regression'],
            'avoid':['Do not treat remembered advice as acceptance']}

class ProcedureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.store=ProcedureStore(self.home)
        self.identifier=self.store.publish(procedure())
    def test_bounded_select(self):
        self.assertEqual(self.store.select('generic',[self.identifier])[0]['procedure'],procedure())
        with self.assertRaises(HostDefect):self.store.select('generic',[self.identifier]*3)
    def test_profile_and_bad_id_rejected(self):
        for profile,ids in [('other',[self.identifier]),('generic',['../x'])]:
            with self.assertRaises(HostDefect):self.store.select(profile,ids)
    def test_corrupt_memory_rejected(self):
        p=self.store.path/(self.identifier+'.json');value=load(p);value['data']['steps']=['Bad']
        dump(p,value)
        with self.assertRaises(HostDefect):self.store.select('generic',[self.identifier])
    def test_unreviewed_raw_case_cannot_be_published(self):
        value=procedure();value['reviewed']=False
        with self.assertRaises(HostDefect):self.store.publish(value)
        value=procedure();value['patch']='hidden answer'
        with self.assertRaises(HostDefect):self.store.publish(value)
    def test_checkpoint_pins_and_legacy_off(self):
        run=RunStore(self.home/'runs'/'synthetic');dump(run.path/'state.json',{'status':'READY'})
        base=run.path/'checkpoints/P1/1';contract={'profile':'generic'}
        self.assertEqual(pinned_procedures(run,contract,base),[])
        run.save_state(procedure_policy={'references':[self.identifier]})
        rows=pinned_procedures(run,contract,base)
        self.assertEqual(rows,pinned_procedures(run,contract,base))
        run.save_state(procedure_policy={'references':[]})
        with self.assertRaises(HostDefect):pinned_procedures(run,contract,base)
    def test_context_budget_finite(self):
        value=procedure();value['steps']=['x'*2000]
        with self.assertRaises(HostDefect):self.store.publish(value)

    def test_worker_receives_procedures_and_original_host_contract_is_unchanged(self):
        from test_phase_workflow import PhaseBoundaries,Model,FIX
        from pitchi_work.phase_workflow import PhaseWorker
        import json
        fixture=PhaseBoundaries();fixture.setUp();self.addCleanup(fixture.doCleanups)
        value=procedure();value['profile']=fixture.contract['profile']
        store=ProcedureStore(fixture.run.path.parent.parent)
        identifier=store.publish(value)
        fixture.run.save_state(procedure_policy={'references':[identifier]})
        contract_before=json.dumps(fixture.contract,sort_keys=True)
        received=[]
        class Instrumented(Model):
            def ask(self,role,prompt,**kwargs):
                packet=json.loads(prompt)
                if packet['phase'].startswith(('hypothesis','edit_')):
                    received.append((packet['phase'],packet['input']['abstract_procedures']))
                return super().ask(role,prompt,**kwargs)
        output=PhaseWorker(fixture.run,fixture.contract,Instrumented(),1).execute()
        self.assertEqual(output['files']['calc.py'],FIX)
        self.assertTrue(any(p=='hypothesis' for p,_ in received))
        self.assertTrue(any(p.startswith('edit_') for p,_ in received))
        self.assertTrue(all(rows[0]['reference']==identifier for _,rows in received))
        self.assertEqual(json.dumps(fixture.contract,sort_keys=True),contract_before)
        self.assertEqual(fixture.run.state()['status'],'READY')
