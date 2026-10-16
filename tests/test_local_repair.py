"""Unused task attempt inherits evidence, never failed retry or a PASS verdict."""
import json,unittest
from unittest.mock import patch
from pitchi_work import cli
from pitchi_work.mission_controller import MissionController
from pitchi_work.recovery import manifest,recover_mission,reserve_local_repair_call
from pitchi_work.store import load
from pitchi_work.model import ModelFailure
from pitchi_work.resolution import read_verified
from test_recovery import Model
import test_reference_phase_integration as fixtures

class LocalRepairTests(unittest.TestCase):
 def failed(self):
  f=fixtures.ReferencePhaseIntegration();f.setUp();self.addCleanup(f.doCleanups)
  class Worker(Model):
   def ask(self,role,prompt,**kw):
    p=json.loads(prompt)
    if role=='worker' and p['phase'].startswith('edit_'):
     self.calls.append((p['phase'],p,prompt,kw));v=p['input']
     if p['phase'] in ('edit_0','edit_3'):
      source=next(s for s in v['sources'] if s['path']=='calc.py')
      return {'action':'edit_target','path':'calc.py','source_sha256':source['source_sha256'],'start_line':2,'end_line':2}
     if p['phase']=='edit_2':return {'action':'read','path':'forbidden.py','page':0}
     return {'action':'replace_target','target_id':v['source_view']['edit_target']['target_id'],
      'new_text':'    return a+b\n' if p['phase']=='edit_4' else '    return a+b+unknown\n'}
    return super().ask(role,prompt,**kw)
  model=Worker();controller=MissionController(f.run);controller.adapter=model;controller.execute()
  self.assertEqual(load(f.run.path/'tasks/P1.json')['attempts'][0]['state'],'FAILED')
  return f,model

 def test_official_cli_preserves_failure_and_reuses_completed_phases(self):
  f,model=self.failed();before=manifest(f.run.path);count=len(model.calls);children=[]
  def foreground(child):
   children.append(child);controller=MissionController(child);controller.adapter=model
   result=controller.execute();self.assertEqual(result['status'],'COMPLETED',result.get('stop_reason'))
   return 0
  from pitchi_work.edit_protocol import target_rules
  changed_rules=target_rules()+' New generic navigation instruction.'*20
  with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground',side_effect=foreground),patch('pitchi_work.edit_protocol.target_rules',return_value=changed_rules):
   self.assertEqual(cli.main(['--home',str(f.root/'home'),'recover',f.run.run_id,'--repair-calls','24']),0)
  child=children[0];record=load(child.path/'tasks/P1.json')
  self.assertEqual([a['state'] for a in record['attempts']],['FAILED','PASS'])
  self.assertEqual(before,manifest(f.run.path))
  self.assertFalse(any(n.startswith(('research','hypothesis')) for n,*_ in model.calls[count:]))
  self.assertFalse(any(n in ('edit_1','edit_2') for n,*_ in model.calls[count:]))
  self.assertEqual(len(list((child.path/'checkpoints/P1/1/calls/edit_2').glob('*.json'))),2)
  self.assertEqual(read_verified(child.path/'artifacts/local_repair_receipt.json')['skipped_edit_steps'],[2])
  failed=load(child.path/'checkpoints/P1/1/staged_tests/1/receipt.json')
  self.assertFalse(all(t['passed'] for t in failed['results']))

 def test_shared_budget_consumes_errors_and_does_not_reset(self):
  f,_=self.failed();child=recover_mission(f.run,repair_calls=2)
  reserve_local_repair_call(child,'worker');reserve_local_repair_call(child,'reviewer')
  with self.assertRaises(ModelFailure):reserve_local_repair_call(child,'worker')
  ledger=read_verified(f.root/'home/local_repair_budgets'/ (child.state()['local_repair_budget']+'.json'))
  self.assertEqual(len(ledger['reservations']),2)

 def test_no_grant_still_rejects_failed_retry(self):
  f,_=self.failed();before=manifest(f.run.path)
  from pitchi_work.runner import HostDefect
  with self.assertRaises(HostDefect):recover_mission(f.run)
  self.assertEqual(before,manifest(f.run.path))
