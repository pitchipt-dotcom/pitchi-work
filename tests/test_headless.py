import tempfile
import contextlib
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pitchi_work.headless import dispatch, run_at, view
from pitchi_work.store import RunStore, dump


class HeadlessBoundary(unittest.TestCase):
    def test_formal_json_entry_keeps_real_mission_setup_diagnostics_off_stdout(self):
        from pitchi_work import headless
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            (source/'README.md').write_text('Synthetic inspection input.\n')
            cfg=root/'gateway.json'
            cfg.write_text(json.dumps({'home':str(root/'runtime'),'projects':{'fixture':str(source)},
                'endpoint':'http://localhost/v1','model':'synthetic','process_supervisor':'systemd'}))
            cfg.chmod(0o600)
            incoming=io.TextIOWrapper(io.BytesIO(json.dumps({'action':'submit','project':'fixture',
                'request':'Inspect the synthetic source'}).encode()))
            stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(sys,'argv',['headless','--config',str(cfg)]),patch.object(sys,'stdin',incoming), \
                 patch.object(headless,'health'),patch.object(headless.shutil,'which',return_value='/usr/bin/systemd-run'), \
                 patch.object(headless.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
                 contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=headless.main()
            response=json.loads(stdout.getvalue())
            self.assertEqual(code,0);self.assertTrue(response['ok'])
            self.assertIn('Host workflow v2',stderr.getvalue())
            run=response['result']['run_id']
            self.assertTrue((root/'runtime/runs'/run/'headless_dispatch.json').exists())

    def test_service_session_cannot_be_mistaken_for_independence(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pathlib.Path.read_text', return_value='0::/user.slice/chat.service'):
            with self.assertRaisesRegex(ValueError, 'independent systemd'):
                dispatch({'home': tmp}, {'action': 'submit', 'request': 'app'})

    def test_independent_unit_and_credential_free_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(Path(tmp) / 'runs/20260101_000000_abcdef')
            dump(run.path / 'state.json', {'status': 'READY'})
            cfg = {'home': tmp, 'projects': {'demo': tmp}, 'endpoint': 'http://localhost/v1',
                   'model': 'local', 'process_supervisor': 'systemd'}
            with patch('pitchi_work.headless.shutil.which', return_value='/usr/bin/systemd-run'), \
                 patch('pitchi_work.headless.health'), patch('pitchi_work.headless.create_mission', return_value=run), \
                 patch('pitchi_work.headless.subprocess.run', return_value=SimpleNamespace(returncode=0)) as launch, \
                 patch.dict('os.environ', {'OPENAI_API_KEY': 'test-not-a-real-key'}):
                result = dispatch(cfg, {'action': 'submit', 'project': 'demo', 'request': 'app'})
            argv = launch.call_args.args[0]
            self.assertIn('--unit=pitchi-work-' + run.run_id, argv)
            self.assertIn('-i', argv)
            self.assertIn('--property=KillMode=process', argv)
            self.assertFalse(any('OPENAI_API_KEY' in value for value in argv))
            self.assertEqual(result['status'], 'RUNNING')

    def test_finite_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'home': tmp, 'projects': {'demo': tmp}}
            self.assertEqual(dispatch(cfg, {'action': 'catalog'})['projects'], ['demo'])
            for request in ({'action': 'shell'}, {'action': 'submit', 'shell': 'echo x'}, {'action': 'submit', 'request': ''}):
                with self.assertRaises(ValueError): dispatch(cfg, request)
            with self.assertRaises(ValueError): run_at(Path(tmp), '../outside')

    def test_authority_and_terminal_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(Path(tmp) / '20260101_000000_abcdef')
            for native, mapped in [('READY', 'RUNNING'), ('PARTIAL', 'FAILED'), ('NEEDS_CAPABILITY', 'NEEDS_CAPABILITY'), ('NEEDS_HUMAN', 'NEEDS_HUMAN'), ('COMPLETED', 'COMPLETED')]:
                dump(run.path / 'state.json', {'status': native, 'human_preview': {'artifact_id': 'owned', 'url': 'http://localhost/artifacts/owned/'}})
                self.assertEqual(view(run)['status'], mapped)
                self.assertEqual(view(run)['native_status'], native)
                if native == 'COMPLETED': self.assertEqual(view(run)['artifact']['artifact_id'], 'owned')
                else: self.assertIsNone(view(run)['artifact'])
                if native in run.TERMINAL: self.assertNotIn('処理中', view(run)['message'])
