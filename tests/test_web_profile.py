"""Finite profile/binding boundaries, not acceptance evidence for an app."""
import unittest
from pitchi_work.mission_profiles import bind_requirements,compile_contracts


class WebProfile(unittest.TestCase):
    def test_saved_integrator_omission_is_not_filled_or_accepted(self):
        from pathlib import Path
        import json
        from pitchi_work.mission_controller import check_integration_assessment
        path=Path(__file__).resolve().parents[1]/'runs/20261008_165157_236595'
        if not path.is_dir():self.skipTest('Private real Integrator evidence unavailable')
        value=json.loads((path/'artifacts/role_call_0005_integrator/output.txt').read_text())
        before=json.dumps(value,sort_keys=True)
        with self.assertRaisesRegex(ValueError,'Missing criterion IDs.*R0'):
            check_integration_assessment(value,{'M1','M2','I0','R0'},{'artifacts/P1_host_root.json'})
        self.assertEqual(json.dumps(value,sort_keys=True),before)

    @unittest.skipUnless(__import__('os').getenv('PITCHI_TEST_SANDBOX')=='1','Explicit isolated browser test')
    def test_real_saved_worker_app_pass_and_wrong_grading_fail(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import shutil
        from pitchi_work.web_tools import verify_web
        from pitchi_work.mission_profiles import sha
        source=Path(__file__).resolve().parents[1]/'workspaces/20261008_164656_d0558a'
        if not source.is_dir():self.skipTest('Private real Worker artifacts unavailable')
        hashes={p.name:sha(p) for p in source.iterdir() if p.is_file()}
        with TemporaryDirectory(prefix='pitchi-real-web-regression-') as temporary:
            base=Path(temporary);copy=base/'app';shutil.copytree(source,copy)
            good=verify_web({'root':str(copy),'behavior':'arithmetic_practice'},home=base/'private')
            self.assertTrue(good['ok'],good);self.assertTrue(good['passed'],good)
            code=(copy/'app.js').read_text()
            self.assertIn('value === expected',code)
            (copy/'app.js').write_text(code.replace('value === expected','true'))
            bad=verify_web({'root':str(copy),'behavior':'arithmetic_practice'},home=base/'private')
            self.assertTrue(bad['ok'],bad);self.assertFalse(bad['passed'],bad)
            self.assertTrue(any(c['validator']=='arithmetic_practice' and not c['pass'] for c in bad['checks']))
        self.assertEqual(hashes,{p.name:sha(p) for p in source.iterdir() if p.is_file()})

    def test_saved_requirement_missing_behavior_is_actionable(self):
        from pathlib import Path
        import json
        path=Path(__file__).resolve().parents[1]/'runs/20261008_164203_1874ba'
        if not path.is_dir():self.skipTest('Private real-run evidence unavailable')
        value=json.loads((path/'artifacts/role_call_0001_requirement/output.txt').read_text())
        with self.assertRaisesRegex(ValueError,'missing: behavior'):
            bind_requirements(value,(path/'request.md').read_text(),{'files':[],'tests':[]})

    def test_empty_workspace_compiles_only_host_candidates(self):
        request='Make an arithmetic practice app.'
        catalog={'files':[],'tests':[]}
        requirement=bind_requirements({'profile':'static_web_app','targets':['index.html','app.js','style.css'],
            'requirements':[request],'needs_human':False,'behavior':'arithmetic_practice'},request,catalog)
        plan=compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':requirement['targets'],'depends_on':[]}]},requirement,catalog)
        contract=plan['tasks'][0]
        self.assertEqual(contract['input_files'],[])
        self.assertEqual(contract['expected_outputs'],requirement['targets'])
        self.assertEqual(contract['verification_modes']['enjoyment'],'SUBJECTIVE')
        self.assertNotIn('commands',contract)
        with self.assertRaises(ValueError):
            bind_requirements({**{'profile':'static_web_app','targets':requirement['targets'],'requirements':[request],
                'needs_human':False},'behavior':'invented_validator'},request,catalog)
