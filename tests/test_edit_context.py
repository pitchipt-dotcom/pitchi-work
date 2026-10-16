"""Large generic read scope does not expand every edit call's working context."""
import json
import unittest
from unittest.mock import patch
from pathlib import Path
import test_reference_phase_integration as integration
from pitchi_work.phase_workflow import PhaseWorker,digest


class EditContextTests(unittest.TestCase):
    def test_literal_search_read_replace_and_host_candidate_not_completion(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);v=p['input'];name=p['phase']
                if name.startswith('edit_'):
                    self.calls.append((name,p,prompt,kwargs))
                    if name=='edit_0':return {'action':'source_search','term':'return'}
                    if name=='edit_1':
                        assert v['source_view']['kind']=='literal_source_search'
                        return {'action':'read','path':'calc.py','page':0}
                    assert v['source_navigation']['read_history']==[{'path':'calc.py','page':0}]
                    return {'action':'replace','path':'calc.py','source_sha256':v['source_view']['source_sha256'],
                        'old_text':'return a - b','new_text':'return a + b','summary':'Generic edit'}
                return super().ask(role,prompt,**kwargs)
        with patch('pitchi_work.staged_validation.invocation',return_value={'exit_code':0,'tests_run':1,'stderr':''}):
            w=f.worker(Model(),legacy_edit=True);out=w.execute()
        self.assertIn('requires independent formal validation',out['summary'])
        self.assertIn('return a + b',out['files']['calc.py'])
        self.assertFalse(json.loads((w.base/'staged_tests/2/receipt.json').read_text())['completion'])
        self.assertIsNone(w.cached('edit_3'))

    def test_host_serializes_line_boundary_without_rewriting_contents(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);v=p['input'];name=p['phase']
                if name=='edit_1':
                    self.calls.append((name,p,prompt,kwargs))
                    return {'action':'patch','path':'calc.py','source_sha256':v['source_view']['source_sha256'],
                            'start_line':2,'end_line':2,'replacement':'    return a + b','summary':'Generic arithmetic edit'}
                if name=='edit_2':
                    self.calls.append((name,p,prompt,kwargs));return {'action':'done','summary':'Staged'}
                return super().ask(role,prompt,**kwargs)
        with patch('pitchi_work.staged_validation.invocation',return_value={'exit_code':0,'tests_run':1,'stderr':''}):
            w=f.worker(Model(),legacy_edit=True);out=w.execute()
        self.assertEqual(w.cached('edit_1')['replacement'],'    return a + b\n')
        self.assertEqual(out['files']['calc.py'],f.original.replace('    return a - b\n','    return a + b\n',1))

    def test_identical_patch_is_not_progress(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);v=p['input'];name=p['phase']
                if name=='edit_1':
                    self.calls.append((name,p,prompt,kwargs))
                    if p['feedback']:
                        assert p['feedback']['failure_class']=='TOOL_REQUEST_NO_PROGRESS'
                        return {'action':'done','summary':'Diagnostic staging only'}
                    return {'action':'patch','path':'calc.py','source_sha256':v['source_view']['source_sha256'],
                            'start_line':2,'end_line':2,'replacement':'    return a - b\n','summary':'No byte change'}
                return super().ask(role,prompt,**kwargs)
        model=Model();w=f.worker(model,legacy_edit=True);out=w.execute()
        self.assertEqual(out['files'],{})

    def test_repeated_visible_read_rejected_with_specific_feedback(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);name=p['phase'];v=p['input']
                if name.startswith('edit'):
                    self.calls.append((name,p,prompt,kwargs))
                    self.assertions=v
                    if name=='edit_1' and p['feedback']:
                        assert p['feedback']['failure_class']=='TOOL_REQUEST_NO_PROGRESS'
                        assert p['feedback']['already_visible']=={'path':'calc.py','page':0}
                        assert v['task_goal']==f.contract['goal']
                        assert v['host_contract']['validators']==f.contract['validators']
                        return {'action':'done','summary':'No self-PASS'}
                    return {'action':'read','path':'calc.py','page':0}
                return super().ask(role,prompt,**kwargs)
        model=Model();w=f.worker(model);out=w.execute()
        self.assertEqual(out['files'],{})
        self.assertEqual(w.cached('edit_1')['action'],'done')
        self.assertEqual(model.assertions['tool_state']['last_action'],{'action':'read','path':'calc.py','page':0})

    def test_large_catalog_is_paged_readable_and_budgeted(self):
        f=integration.ReferencePhaseIntegration(methodName='test_empty_source_snapshot_is_retained_not_invented')
        f.setUp();self.addCleanup(f.doCleanups)
        workspace=Path(f.run.state()['workspace'])
        names=['notes/record_'+str(i).zfill(3)+'.py' for i in range(60)]
        for name in names:
            p=workspace/name;p.parent.mkdir(exist_ok=True);p.write_text('# Retain records unless explicitly approved.\n'*50)
        c={**f.contract,'research_inputs':names};c.pop('sha256');c['sha256']=digest(c)
        class Model(integration.GenericWorker):
            def ask(self,role,prompt,**kwargs):
                p=json.loads(prompt);name=p['phase'];v=p['input']
                if name=='edit_0':
                    self.calls.append((name,p,prompt,kwargs));return {'action':'source_catalog','page':1}
                if name=='edit_1':
                    self.calls.append((name,p,prompt,kwargs));return {'action':'read','path':names[-1],'page':0}
                if name=='edit_2':
                    self.calls.append((name,p,prompt,kwargs));return {'action':'done','summary':'Generic diagnostic only; no self-PASS'}
                return super().ask(role,prompt,**kwargs)
        model=Model();w=PhaseWorker(f.run,c,model,1);result=w.execute()
        self.assertEqual(result['files'],{})
        calls=[(p,prompt,k) for name,p,prompt,k in model.calls if name.startswith('edit')]
        for p,prompt,k in calls:
            self.assertLessEqual(len(p['input']['sources']),len(c['allowed_paths'])+4)
            self.assertNotIn('handoff_calls',p['input']['hypothesis'])
            self.assertTrue(p['input']['hypothesis_reference']['sha256'])
            self.assertLessEqual(len(prompt.encode())+512+k['max_output_tokens'],13107)
        self.assertEqual(calls[1][0]['input']['source_catalog']['page'],1)
        self.assertIn('Retain records',calls[2][0]['input']['source_view']['text'])
        restored=PhaseWorker(f.run,c,model,1)
        self.assertEqual(restored.execute(),result)
        self.assertEqual((workspace/names[-1]).read_text(),'# Retain records unless explicitly approved.\n'*50)


if __name__=='__main__':unittest.main()
