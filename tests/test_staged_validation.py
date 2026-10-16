import tempfile,unittest,hashlib
from pathlib import Path
from unittest.mock import patch
from pitchi_work.staged_validation import observe_staged,ensure_query_input
from pitchi_work.store import dump,load
from pitchi_work.runner import HostDefect


class StagedTests(unittest.TestCase):
    def test_execution_copies_baseline_assets_outside_research_read_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);origin=root/'origin';origin.mkdir();(origin/'assets').mkdir()
            (origin/'module.py').write_text('# original\n');(origin/'assets/config.bin').write_bytes(b'\x00\xff\x01')
            (origin/'test_example.py').write_text('# protected\n')
            class Run:
                path=root/'run'
                def state(self):return {'workspace':str(origin)}
            run=Run();dump(run.path/'artifacts/host_baseline.json',{'module.py':hashlib.sha256(b'# original\n').hexdigest(),
                'assets/config.bin':hashlib.sha256(b'\x00\xff\x01').hexdigest(),
                'test_example.py':hashlib.sha256(b'# protected\n').hexdigest()})
            contract={'profile':'bug_fix','existing_tests':['test_example.py'],'allowed_paths':['module.py'],'timeout':120}
            def execute(proxy,args,timeout):
                staged=Path(proxy.state()['workspace'])
                self.assertEqual((staged/'assets/config.bin').read_bytes(),b'\x00\xff\x01')
                self.assertEqual((staged/'module.py').read_text(),'# changed\n')
                return {'exit_code':0,'tests_run':1,'stderr':''}
            with patch('pitchi_work.staged_validation.invocation',side_effect=execute):
                receipt=observe_staged(run,contract,{'module.py':'# changed\n'},root/'probe')
            self.assertIn('assets/config.bin',receipt['workspace_hashes'])
            self.assertEqual((origin/'module.py').read_text(),'# original\n')

    def test_bound_execution_input_produced_without_fabricating_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            class Run:path=Path(d)
            run=Run();dump(run.path/'search_binding.json',{'queries':['alpha beta','gamma']})
            ref=ensure_query_input(run)
            self.assertEqual(load(run.path/ref['reference']),['alpha beta','gamma'])
            self.assertEqual(ref['kind'],'execution_input_not_evidence')
            self.assertEqual(ensure_query_input(run),ref)
            dump(run.path/ref['reference'],['mismatched'])
            with self.assertRaises(HostDefect):ensure_query_input(run)

    def test_protected_test_observes_staged_not_origin_and_is_not_completion(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            class Run:
                path=root
                def state(self):return {'workspace':str(root/'origin')}
            run=Run();contract={'profile':'bug_fix','existing_tests':['tests/test_x.py'],'timeout':120}
            def execute(proxy,args,timeout):
                workspace=Path(proxy.state()['workspace'])
                self.assertEqual((workspace/'module.py').read_text(),'# changed\n')
                self.assertEqual(proxy.path,run.path)
                return {'exit_code':1,'tests_run':1,'stderr':'assertion failed'}
            with patch('pitchi_work.staged_validation.invocation',side_effect=execute):
                receipt=observe_staged(run,contract,{'module.py':'# changed\n','tests/test_x.py':'# protected\n'},root/'probe')
            self.assertFalse(receipt['completion']);self.assertFalse(receipt['results'][0]['passed'])
            self.assertEqual(receipt['authority'],'host');self.assertTrue((root/'probe/receipt.json').exists())

    def test_escaping_path_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            class Run:
                path=Path(d)
            with self.assertRaises(ValueError):observe_staged(Run(),{'profile':'bug_fix','existing_tests':[]},{'../../escape':'secret'},Path(d)/'probe')
