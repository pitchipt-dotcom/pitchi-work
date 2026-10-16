"""Official terminal recovery; generic arithmetic only, no live model."""
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from pitchi_work import cli
from pitchi_work.mission_controller import MissionController
from pitchi_work.recovery import manifest, recover_mission
from pitchi_work.runner import HostDefect
from pitchi_work.store import load
import test_reference_phase_integration as fixtures


class Model(fixtures.GenericWorker):
    def ask(self,role,prompt,**kw):
        p=json.loads(prompt);v=p['input']
        if role=='requirement':return {'profile':'bug_fix','targets':['calc.py'],'requirements':['Inspect','repair'],'needs_human':False}
        if role=='planner':return {'tasks':[{'requirement_ids':[r['id'] for r in json.loads(prompt)['input']['requirements']['requirements']],'targets':['calc.py'],'depends_on':[]}]}
        if role=='reviewer':return {'verdict':'PASS','reason':'Host verified'}
        if role=='integrator':return {'criteria':[{'id':i,'met':True,'reason':'Host verified','evidence':v['allowed_evidence_references']} for i in v['required_criterion_ids']]}
        return super().ask(role,prompt,**kw)


class RecoveryTests(unittest.TestCase):
    def test_official_recover_reissues_target_and_preserves_history(self):
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups)
        class TargetModel(Model):
            fail=True
            def ask(self,role,prompt,**kw):
                p=json.loads(prompt)
                if role=='worker' and p['phase'].startswith('edit_'):
                    self.calls.append((p['phase'],p,prompt,kw));v=p['input']
                    if p['phase']=='edit_0':
                        s=next(s for s in v['sources'] if s['path']=='calc.py')
                        return {'action':'edit_target','path':'calc.py','source_sha256':s['source_sha256'],
                            'start_line':2,'end_line':2}
                    if self.fail:self.fail=False;raise RuntimeError('Synthetic interruption after target selection')
                    return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
                        'new_text':'    return a + b\n'}
                return super().ask(role,prompt,**kw)
        model=TargetModel();controller=MissionController(f.run);controller.adapter=model
        self.assertEqual(controller.execute()['status'],'STALLED')
        before=manifest(f.run.path);count=len(model.calls)
        intermediate=recover_mission(f.run)
        (intermediate.path/'controller.lock').touch()
        intermediate.save_state(status='STALLED',failure_class='PHASE_CONTEXT_DEFECT',
            stop_reason='Synthetic pre-dispatch Host stop; no extra Worker call')
        middle_before=manifest(intermediate.path)
        child=recover_mission(intermediate);controller=MissionController(child);controller.adapter=model
        result=controller.execute()
        self.assertEqual(result['status'],'COMPLETED',result.get('stop_reason'))
        self.assertEqual(before,manifest(f.run.path))
        self.assertEqual(middle_before,manifest(intermediate.path))
        self.assertFalse(any(n.startswith(('research','hypothesis')) for n,*_ in model.calls[count:]))
        from pitchi_work.resolution import read_verified
        old=read_verified(f.run.path/'checkpoints/P1/1/edit_targets.json')
        new=read_verified(child.path/'checkpoints/P1/1/edit_targets.json')
        self.assertFalse(set(old['targets']) & set(new['targets']))
        self.assertEqual(len(list((child.path/'checkpoints/P1/1/calls/edit_1').glob('*.json'))),2)
        self.assertTrue(load(child.path/'artifacts/recovery_receipt.json')['target_transfers'])
        with self.assertRaises(ValueError):
            from pitchi_work.edit_targets import EditTargets
            registry=EditTargets(child.path/'checkpoints/P1/1/edit_targets.json',['calc.py'],
                load(child.path/'plan.json')['tasks'][0]['sha256'])
            registry.resolve({'calc.py':f.original},{'action':'replace_target',
                'target_id':next(iter(old['targets'])),'new_text':'x'},900)

    def stopped(self):
        f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups)
        model=Model(interrupt='hypothesis_view_1')
        controller=MissionController(f.run);controller.adapter=model
        result=controller.execute()
        self.assertEqual(result['status'],'STALLED')
        self.assertTrue((f.run.path/'checkpoints/P1/1/research_manifest.json').exists())
        f.run.save_state(cloud_call_count=2)
        return f,model

    def test_official_recovery_checkpoint_evidence_budget_parent_immutable(self):
        f,model=self.stopped();before=manifest(f.run.path);children=[]
        def foreground(child):
            children.append(child)
            controller=MissionController(child);controller.adapter=model
            result=controller.execute()
            self.assertEqual(result['status'],'COMPLETED',result.get('stop_reason'))
            return 0
        count=len(model.calls)
        with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground',side_effect=foreground):
            rc=cli.main(['--home',str(f.root/'home'),'recover',f.run.run_id])
        self.assertEqual(rc,0)
        child=children[0]
        self.assertNotEqual(child.run_id,f.run.run_id)
        self.assertEqual(manifest(f.run.path),before)
        self.assertFalse(any(n.startswith('research_') for n,*_ in model.calls[count:]))
        self.assertEqual(child.state()['cloud_call_count'],2)
        self.assertEqual(child.state()['advisor_budget_id'],f.run.state()['advisor_budget_id'])
        call=child.path/'checkpoints/P1/1/calls/hypothesis_view_1'
        self.assertTrue((call/'1.json').exists())
        self.assertTrue((call/'2.json').exists())
        receipt=load(child.path/'artifacts/recovery_receipt.json')
        self.assertTrue(receipt['boundaries'][0]['research_complete'])
        self.assertEqual(receipt['original_current_phase'],'hypothesis_view_1')
        old=f.run.path/'checkpoints/P1/1/research_0.json'
        self.assertEqual(old.read_bytes(),(child.path/'checkpoints/P1/1/research_0.json').read_bytes())
        self.assertEqual((f.root/'project/calc.py').read_text(),f.original)
        with self.assertRaises(HostDefect):recover_mission(f.run)

    def test_invalid_saved_source_and_hash_stop_before_dispatch(self):
        for kind in ('source','checkpoint','packet','version','scope'):
            with self.subTest(kind=kind):
                f,_=self.stopped()
                if kind=='source':(Path(f.run.state()['workspace'])/'calc.py').write_text('changed')
                elif kind=='checkpoint':(f.run.path/'checkpoints/P1/1/research_0.json').write_text('{}')
                elif kind=='packet':(f.run.path/'checkpoints/P1/1/packets/research_0_view_0.json').write_text('{}')
                elif kind=='version':
                    from pitchi_work.resolution import write_verified
                    write_verified(f.run.path/'workflow_selection.json',{'research_handoff_version':1})
                else:
                    p=f.run.path/'plan.json';v=load(p);v['tasks'][0]['allowed_paths']=['outside.py']
                    from pitchi_work.store import dump
                    dump(p,v)
                before=manifest(f.run.path)
                with self.assertRaises((HostDefect,KeyError)):recover_mission(f.run)
                self.assertEqual(manifest(f.run.path),before)

    def test_resume_continue_unchanged_and_missing_plan_rejected(self):
        f,_=self.stopped()
        with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground') as foreground:
            self.assertEqual(cli.main(['--home',str(f.root/'home'),'resume',f.run.run_id]),2)
            foreground.assert_not_called()
        (f.run.path/'plan.json').unlink()
        with self.assertRaises(HostDefect):recover_mission(f.run)

    def test_database_mismatch_cancel_and_external_policy_rejected(self):
        from pitchi_work.store import dump
        from pitchi_work.mission_profiles import sha
        for kind in ('database','cancel','external'):
            with self.subTest(kind=kind):
                f,_=self.stopped()
                if kind=='cancel':dump(f.run.path/'cancel_request.json',{'at':'synthetic'})
                elif kind=='external':f.run.save_state(llm_config={'provider':'openai'})
                else:
                    database=f.run.path/'artifacts/data.sqlite';database.write_bytes(b'generic snapshot')
                    dump(f.run.path/'search_binding.json',{'database':str(database),'database_sha256':'0'*64,
                        'source':str(f.root/'project'),'source_hashes':{'calc.py':sha(f.root/'project/calc.py')},
                        'runtime':str(f.root)})
                before=manifest(f.run.path)
                with self.assertRaises(HostDefect):recover_mission(f.run)
                self.assertEqual(manifest(f.run.path),before)
