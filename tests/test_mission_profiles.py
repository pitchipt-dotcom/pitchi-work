"""Non-live fixture checks. No API requests, GPU switches or production writes."""
import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from pitchi_work import cli
from pitchi_work.code_validation import validate
from pitchi_work.mission_profiles import inventory,bind_requirements,compile_contracts,UnsupportedMission,HumanApprovalRequired,sha
from pitchi_work.mission_controller import create_mission,MissionController
from pitchi_work.store import load,dump
from pitchi_work.runner import HostDefect

REQUEST='足し算の不具合を修正してください。'
CODE='def add(a, b):\n    return a - b\n'
FIX='def add(a, b):\n    return a + b\n'
TEST='import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n'


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(prefix='pitchi-cli-fixture-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'project';self.source.mkdir()
        (self.source/'calc.py').write_text(CODE);(self.source/'tests').mkdir()
        (self.source/'tests/test_calc.py').write_text(TEST)
        self.home=self.root/'runtime'

    def run_fixture(self):return create_mission(self.home,self.source,REQUEST,'http://127.0.0.1:8092/v1','fixture-only')

    def contract(self,run,profile='bug_fix'):
        catalog=inventory(Path(run.state()['workspace']))
        req=bind_requirements({'profile':profile,'targets':['calc.py'],'requirements':[REQUEST],'needs_human':False},REQUEST,catalog)
        plan=compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]},req,catalog)
        dump(run.path/'requirements.json',req);dump(run.path/'plan.json',plan)
        return plan['tasks'][0]

    def test_host_registry_scope_unknown_profiles_and_commands(self):
        run=self.run_fixture();task=self.contract(run)
        self.assertEqual(task['validators'],['scope','python_syntax','existing_unittest','change_diff'])
        self.assertEqual(task['allowed_paths'],['calc.py'])
        self.assertNotIn('commands',task)
        with self.assertRaises(ValueError):compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[],'validator':'invented'}]},load(run.path/'requirements.json'),inventory(Path(run.state()['workspace'])))
        with self.assertRaises(UnsupportedMission):bind_requirements({'profile':'unknown','targets':['calc.py'],'requirements':[REQUEST],'needs_human':False},REQUEST,inventory(self.source))
        with self.assertRaises(HumanApprovalRequired):bind_requirements({'profile':'bug_fix','targets':['calc.py'],'requirements':[REQUEST],'needs_human':True},REQUEST,inventory(self.source))

    def test_input_byte_preservation_and_child_parent_immutable(self):
        text='目的: 記号： # - Markdown\n'+'日本語の依頼。'*160+'\nhttps://example.invalid/a:b\n'
        with patch('sys.stdin',io.StringIO(text)):self.assertEqual(cli.read_request(),text)
        parent=self.run_fixture();parent.save_state(status='PARTIAL')
        before={p.relative_to(parent.path):sha(p) for p in parent.path.rglob('*') if p.is_file()}
        child=create_mission(self.home,parent.state()['workspace'],REQUEST,'http://127.0.0.1:8092/v1','fixture-only',parent=parent.path,instruction='既存成果物を保持して続けて')
        self.assertEqual(load(child.path/'root_goal.json'),load(parent.path/'root_goal.json'))
        self.assertEqual(child.state()['parent_run'],str(parent.path))
        self.assertEqual(before,{p.relative_to(parent.path):sha(p) for p in parent.path.rglob('*') if p.is_file()})
        self.assertEqual((self.source/'calc.py').read_text(),CODE)

    def test_read_only_investigation_checks_actual_lines(self):
        run=self.run_fixture();task=self.contract(run,'code_investigation')
        self.assertEqual(task['allowed_paths'],[])
        good={'files':{},'findings':[{'path':'calc.py','line':2,'quote':'return a - b','explanation':'引き算になっている'}]}
        self.assertTrue(validate(run,task,1,good)['passed'])
        bad={'files':{},'findings':[{'path':'calc.py','line':2,'quote':'return a + b','explanation':'存在しない引用'}]}
        self.assertFalse(validate(run,task,2,bad)['passed'])

    def test_each_finite_profile_and_continue_cli(self):
        for profile in ('code_change','bug_fix','code_investigation'):
            run=self.run_fixture();task=self.contract(run,profile)
            self.assertEqual(task['profile'],profile)
        parent=run;parent.save_state(status='PARTIAL')
        parent_before={p.relative_to(parent.path):sha(p) for p in parent.path.rglob('*') if p.is_file()}
        with patch('pitchi_work.cli.health'),patch('pitchi_work.cli.foreground',return_value=0) as fg:
            self.assertEqual(cli.main(['--home',str(self.home),'continue',parent.run_id,'--instruction','変更せず調査を続けて']),0)
            child=fg.call_args.args[0]
            self.assertNotEqual(child.run_id,parent.run_id)
            self.assertEqual(child.state()['parent_run'],str(parent.path))
        self.assertEqual(parent_before,{p.relative_to(parent.path):sha(p) for p in parent.path.rglob('*') if p.is_file()})

    def test_cli_default_start_result_status_cancel_no_llm(self):
        with patch('pitchi_work.cli.health') as health,patch('pitchi_work.cli.foreground',return_value=0) as fg,patch('sys.stdin',io.StringIO(REQUEST)),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--home',str(self.home),'--workspace',str(self.source)]),0)
            run=fg.call_args.args[0]
            self.assertEqual((run.path/'request.md').read_text(),REQUEST)
        with patch('pitchi_work.cli.health',side_effect=AssertionError('No health/API request for read-only commands')),contextlib.redirect_stdout(io.StringIO()):
            for cmd in ('status','result','cancel'):self.assertEqual(cli.main(['--home',str(self.home),cmd,run.run_id]),0)
        self.assertTrue((run.path/'cancel_request.json').exists())
        self.assertEqual(run.state()['status'],'STOPPED_BY_USER')
        with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(cli.main(['--home',str(self.home),'status','../../project']),2)

    def test_host_defect_does_not_retry_worker(self):
        run=self.run_fixture();controller=MissionController(run);task=self.contract(run)
        dump(run.path/'tasks/P1.json',{'task':task,'status':'PENDING','attempts':[]})
        with patch.object(controller.adapter,'ask',return_value={'files':{'calc.py':FIX},'summary':'fixture'}) as worker,patch('pitchi_work.mission_controller.validate',side_effect=HostDefect('runner failure')),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(HostDefect):controller.task(task)
        self.assertEqual(worker.call_count,1)


    def test_real_pty_request_input_without_starting_mission(self):
        import os,pty,select,subprocess,sys,time
        program=('import json,sys;from types import SimpleNamespace;from unittest.mock import patch;from pitchi_work.cli import main;'
                 'capture=lambda *a,**k:(print("REQUEST_CAPTURE="+json.dumps(a[2],ensure_ascii=True)),SimpleNamespace(state=lambda:{}))[1];'
                 'ctx=[patch("pitchi_work.cli.health"),patch("pitchi_work.cli.create_mission",side_effect=capture),patch("pitchi_work.cli.foreground",return_value=0)];'
                 '[c.start() for c in ctx];sys.exit(main(["--workspace",sys.argv[1]]))')
        master,slave=pty.openpty()
        process=subprocess.Popen([sys.executable,'-B','-c',program,str(self.source)],stdin=slave,stdout=slave,stderr=slave,
                                 env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')})
        os.close(slave);output=b''
        try:
            deadline=time.monotonic()+5
            while b'PITCHI WORK' not in output:
                if time.monotonic()>deadline:self.fail('PTY input prompt missing')
                if select.select([master],[],[],.1)[0]:output+=os.read(master,65536)
            line='目的: '+('日本語：'*350)+' # 箇条書き - URL https://example.invalid/a:b'
            os.write(master,line.encode()+b'\r/send\r')
            while process.poll() is None:
                if time.monotonic()>deadline:self.fail('PTY input did not submit')
                if select.select([master],[],[],.1)[0]:
                    try:output+=os.read(master,65536)
                    except OSError:break
            process.wait(timeout=2)
            while select.select([master],[],[],.1)[0]:
                try:output+=os.read(master,65536)
                except OSError:break
            self.assertEqual(process.returncode,0)
            # Readline redraw escape sequences may split wide-character echo;
            # captured payload is ASCII JSON, independent of display bytes.
            captured=output.split(b'REQUEST_CAPTURE=',1)[1].split(b'\r\n',1)[0].decode('ascii')
            self.assertEqual(json.loads(captured),line+'\n')
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=2)
            os.close(master)


class LiveHostFixture(unittest.TestCase):
    setUp=Profiles.setUp
    run_fixture=Profiles.run_fixture
    contract=Profiles.contract
    @unittest.skipUnless(__import__('os').environ.get('PITCHI_TEST_SANDBOX')=='1','Explicit sandbox cancellation fixture')
    def test_registered_validator_cancellation_leaves_no_process(self):
        import threading,time,os
        from pitchi_work.code_validation import invocation
        from pitchi_work.store import Cancelled
        run=self.run_fixture();errors=[]
        def call():
            try:invocation(run,['/usr/bin/python3','-c','import time;time.sleep(30)'],40)
            except Cancelled:errors.append('cancelled')
        thread=threading.Thread(target=call);thread.start()
        deadline=time.monotonic()+3
        while not run.state()['active_processes']:
            if time.monotonic()>deadline:self.fail('Validator was not registered')
            time.sleep(.02)
        pids=list(run.state()['active_processes'])
        run.request_cancel();thread.join(timeout=4)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,['cancelled'])
        self.assertFalse(run.state()['active_processes'])
        for pid in pids:self.assertFalse(Path('/proc',pid).exists())
    @unittest.skipUnless(__import__('os').environ.get('PITCHI_TEST_SANDBOX')=='1','Explicit non-live sandbox subprocess check')
    def test_real_existing_tests_and_fake_worker_test_rejection(self):
        run=self.run_fixture();task=self.contract(run);workspace=Path(run.state()['workspace'])
        failed=validate(run,task,1,{})
        self.assertFalse(failed['passed'])
        self.assertGreater(failed['executions'][0]['tests_run'],0)
        (workspace/'calc.py').write_text(FIX)
        passed=validate(run,task,2,{})
        self.assertTrue(passed['passed'])
        self.assertEqual(passed['owner'],'host')
        self.assertEqual((self.source/'calc.py').read_text(),CODE)
        (workspace/'calc.py').write_text('import os,sys\nprint("Ran 1 test in 0.1s",file=sys.stderr)\nos._exit(0)\n')
        premature=validate(run,task,'early-exit',{})
        self.assertFalse(premature['passed'])
        self.assertEqual(premature['executions'][0]['tests_run'],0)
        (workspace/'calc.py').write_text(FIX)
        (workspace/'tests/test_calc.py').write_text('raise SystemExit(0)\n')
        with self.assertRaises(HostDefect):validate(run,task,3,{})

    @unittest.skipUnless(__import__('os').environ.get('PITCHI_TEST_SANDBOX')=='1','Explicit fixture controller check; no real LLM')
    def test_fixture_controller_all_roles_host_root_and_negative_root(self):
        for met in (True,False):
            run=self.run_fixture();controller=MissionController(run);roles=[]
            def saved_fixture_reply(role,prompt,**kwargs):
                roles.append(role)
                if role=='requirement':return {'profile':'bug_fix','targets':['calc.py'],'requirements':[REQUEST],'needs_human':False}
                if role=='planner':return {'tasks':[{'requirement_ids':['M1'],'targets':['calc.py'],'depends_on':[]}]}
                if role=='worker':return {'files':{'calc.py':FIX},'summary':'fixture only'}
                if role=='reviewer':return {'verdict':'PASS','reason':'fixture only'}
                if role=='integrator':
                    payload=json.loads(prompt)['input'];refs=[r['reference'] for r in payload['host_evidence']]
                    return {'criteria':[{'id':i,'met':met,'reason':'fixture only','evidence':refs if met else []} for i in ('M1','R0')]}
                raise AssertionError(role)
            with patch.object(controller.adapter,'ask',side_effect=saved_fixture_reply),contextlib.redirect_stdout(io.StringIO()):
                state=controller.execute()
            self.assertEqual(state['status'],'COMPLETED' if met else 'PARTIAL')
            self.assertEqual(roles,['requirement','planner','worker','reviewer','integrator'])
            self.assertTrue(load(run.path/'artifacts/request_completion_gate.json')['host_evidence_hashes_verified'])
            self.assertEqual((self.source/'calc.py').read_text(),CODE)


if __name__=='__main__':unittest.main()
