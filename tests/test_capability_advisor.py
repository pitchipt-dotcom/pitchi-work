"""Advisor proposals are observations, never executable authority."""
import contextlib
import io
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pitchi_work.capability_advisor import shortage, advise, accept_proposal, verify_command, approval_pending
from pitchi_work.mission_controller import create_mission, MissionController, BoundedFailure
from pitchi_work.mission_profiles import UnsupportedMission, HumanApprovalRequired
from pitchi_work.runner import HostDefect
from pitchi_work.model import ModelFailure
from pitchi_work.store import dump, load, ReadOnlyRun
from pitchi_work import cli


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); source=self.base/'source'; source.mkdir()
        self.run=create_mission(self.base/'home',source,'CSV集計CLIを新規作成してください。',
                                'http://127.0.0.1:8092/v1','non-live-test')
        self.facts={'tools':{'image.ocr':{}},'commands':{
            'python3':{'available':True,'probe':['python3','--help'],'exit_code':0,'help':'--help --version'},
            'tar':{'available':True,'probe':['tar','--help'],'exit_code':0,'help':'--help --create --file'},
            'potrace':{'available':False,'probe':['potrace','--help']}}}

    def proposal(self,possible=True):
        return {'intent':'CSV集計','missing_capabilities':[{'name':'CLI bootstrap','reason':'編集用入力なし',
            'reusable':True,'evidence_ids':['E_ENV']}],'recommendation':'保護済みHost検証付きの新規CLI profileを追加',
            'manual':{'possible':possible,'steps':['stdlib CSVで手動作成、実行前に確認'] if possible else [],
                      'commands':['python3 --help'] if possible else []},'human':'提案を確認して選択してください'}

    def test_positive_host_shortage_not_runtime_or_approval(self):
        self.assertIsNotNone(shortage(self.run,UnsupportedMission('profile unavailable')))
        for exc in (HostDefect('import failure'),ModelFailure('MODEL_TIMEOUT','timeout'),
                    HumanApprovalRequired('paid API'),BoundedFailure('WORKER_OUTPUT_INVALID','bad json')):
            self.assertIsNone(shortage(self.run,exc))
        invalid=BoundedFailure('REQUIREMENT_OUTPUT_INVALID','extra field')
        self.assertIsNone(shortage(self.run,invalid))
        self.assertFalse(approval_pending(self.run,invalid))
        dump(self.run.path/'artifacts/role_call_0001_requirement/output.txt',
             {'profile':'code_change','targets':[],'behavior':'','needs_human':False})
        self.assertEqual(shortage(self.run,invalid)['kind'],'existing_python_profile_inputs_missing')
        dump(self.run.path/'artifacts/role_call_0001_requirement/output.txt',
             {'profile':'unsupported','targets':[],'behavior':'','needs_human':False})
        self.assertEqual(shortage(self.run,invalid)['kind'],'no_registered_profile_selected')
        dump(self.run.path/'artifacts/role_call_0001_requirement/output.txt',
             {'profile':'unsupported','targets':[],'behavior':'','needs_human':True})
        self.assertIsNone(shortage(self.run,invalid))
        self.assertTrue(approval_pending(self.run,invalid))

    def test_human_stop_and_host_failure_never_call_advisor(self):
        for exception,expected in ((HumanApprovalRequired('physical work'),'NEEDS_HUMAN'),
                                   (HostDefect('runtime failure'),'STALLED')):
            run=create_mission(self.base/'home',self.base/'source','調査してください。','http://127.0.0.1:8092/v1','non-live')
            controller=MissionController(run)
            with patch.object(controller,'setup',side_effect=exception),patch.object(controller.adapter,'ask') as ask,contextlib.redirect_stdout(io.StringIO()):
                state=controller.execute()
            self.assertEqual(state['status'],expected);ask.assert_not_called()

    def test_commands_never_executed_and_no_fictitious_verified(self):
        with patch('pitchi_work.capability_advisor.subprocess.run') as execute:
            for command in ('potrace --help','nonexistent-advisor-tool --help','rm -rf data','sudo reboot',
                            'python3 -c "open(\"owned\",\"w\")"','python3 --help; touch owned','curl example.invalid'):
                proof=verify_command(command,self.facts,self.base)
                self.assertEqual(proof['verification'],'UNVERIFIED'); self.assertFalse(proof['executed'])
            self.assertEqual(verify_command('python3 --help',self.facts,self.base)['verification'],'VERIFIED')
            self.assertEqual(verify_command('tar --unknown absent',self.facts,self.base)['verification'],'PARTIALLY_VERIFIED')
            execute.assert_not_called()
        self.assertFalse((self.base/'owned').exists())

    def test_references_required_manual_no_option_and_local_analysis_failure(self):
        proposal=self.proposal(False)
        self.assertFalse(accept_proposal(proposal,{'E_ENV'})['manual']['possible'])
        proposal['missing_capabilities'][0]['evidence_ids']=['E_FAKE']
        with self.assertRaises(ValueError):accept_proposal(proposal,{'E_ENV'})
        with patch('pitchi_work.capability_advisor.collect_environment',return_value=self.facts), \
             patch('pitchi_work.model.LocalRoleAdapter.ask',side_effect=ModelFailure('MODEL_TIMEOUT','timeout')) as ask:
            controller=MissionController(self.run)
            saved=advise(self.run,{'kind':'profile','reason':'missing'},controller.adapter)
            self.assertEqual(saved['analysis_status'],'UNAVAILABLE');self.assertEqual(ask.call_count,1)
            report=load(self.run.path/saved['reference'])
            self.assertIsNone(report['options']['manual']['possible'])
            self.assertIn('まずPitchi-workで停止を再現',report['options']['codex']['request'])

    def test_invalid_analysis_is_bounded_and_no_manual_alternative_is_explicit(self):
        controller=MissionController(self.run)
        with patch('pitchi_work.capability_advisor.collect_environment',return_value=self.facts), \
             patch.object(controller.adapter,'ask',return_value={}) as ask:
            saved=advise(self.run,{'kind':'profile','reason':'missing'},controller.adapter)
        self.assertEqual(ask.call_count,2);self.assertEqual(saved['analysis_status'],'UNAVAILABLE')
        with patch('pitchi_work.capability_advisor.collect_environment',return_value=self.facts), \
             patch.object(controller.adapter,'ask',return_value=self.proposal(False)):
            saved=advise(self.run,{'kind':'profile','reason':'missing'},controller.adapter)
        report=load(self.run.path/saved['reference'])
        self.assertFalse(report['options']['manual']['possible']);self.assertEqual(report['options']['manual']['commands'],[])

    def test_controller_terminal_report_result_cli_and_immutable_parent(self):
        controller=MissionController(self.run)
        with patch.object(controller,'setup',side_effect=UnsupportedMission('No profile')), \
             patch('pitchi_work.capability_advisor.collect_environment',return_value=self.facts), \
             patch.object(controller.adapter,'ask',return_value=self.proposal()),contextlib.redirect_stdout(io.StringIO()):
            state=controller.execute()
        self.assertEqual(state['status'],'NEEDS_CAPABILITY');self.assertEqual(state['failure_class'],'CAPABILITY_GAP')
        report=load(self.run.path/'artifacts/capability_advice.json')
        for e in report['evidence']:self.assertTrue((self.run.path/e['reference']).is_file())
        before=(self.run.path/'state.json').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--home',str(self.base/'home'),'advice',self.run.run_id]),0)
        self.assertIn('missing_capabilities',output.getvalue())
        self.assertEqual(before,(self.run.path/'state.json').read_bytes())
        with self.assertRaises(ValueError):self.run.save_state(status='RUNNING')

    @unittest.skipUnless(os.environ.get('PITCHI_ADVISOR_SAVED_RUNS'),'Actual saved-run paths supplied explicitly')
    def test_actual_saved_stops_read_only_replay(self):
        for directory in os.environ['PITCHI_ADVISOR_SAVED_RUNS'].split(os.pathsep):
            run=ReadOnlyRun(directory)
            before={str(p.relative_to(run.path)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in run.path.rglob('*') if p.is_file()}
            exc=BoundedFailure('REQUIREMENT_OUTPUT_INVALID',run.state().get('stop_reason',''))
            self.assertTrue(shortage(run,exc) or approval_pending(run,exc))
            report=load(run.path/'artifacts/capability_advice.json',None)
            if report:
                for reference in report['evidence']:
                    self.assertEqual(hashlib.sha256((run.path/reference['reference']).read_bytes()).hexdigest(),reference['sha256'])
            self.assertEqual(before,{str(p.relative_to(run.path)):hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in run.path.rglob('*') if p.is_file()})


if __name__=='__main__':unittest.main()
