"""Finite JSON gateway for trusted local UI clients; no shell or agent authority.

Configuration is Host-owned, private and separate from incoming requests.
Submitting returns immediately; the existing controller runs in its own process.
"""
import argparse
from contextlib import redirect_stdout
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from .store import RunStore, load, dump
from .mission_controller import create_mission
from .artifact_jobs import create_artifact_mission
from .model import discover_local_model, health

RUN_ID = re.compile(r'\d{8}_\d{6}_[a-f0-9]{6}')


def run_at(home, identifier):
    if not isinstance(identifier, str) or not RUN_ID.fullmatch(identifier):
        raise ValueError('Invalid run ID')
    path = home / 'runs' / identifier
    if path.is_symlink() or not (path / 'state.json').is_file():
        raise ValueError('Unknown run')
    return RunStore(path)


def view(run):
    state = run.state()
    native = state['status']
    status = native if native in {'COMPLETED', 'NEEDS_HUMAN', 'NEEDS_CAPABILITY'} else (
        'FAILED' if native in run.TERMINAL else 'RUNNING')
    preview = state.get('human_preview') or {}
    default_message = '停止しました (' + native + ')' if native in run.TERMINAL else (
        '中止処理中です' if native == 'CANCELLING' else '処理中です')
    return {'run_id': run.run_id, 'status': status, 'native_status': native,
            'original_request': load(run.path / 'root_goal.json', {}).get('request'),
            'stage': state.get('stage'), 'failure_class': state.get('failure_class'),
            'message': '完成しました' if native == 'COMPLETED' else state.get('stop_reason') or default_message,
            'artifact': {'artifact_id': preview['artifact_id'], 'type': 'static_web_app',
                         'preview_url': preview.get('url')} if native == 'COMPLETED' and preview.get('artifact_id') else None,
            'capability_advice': load(run.path / 'artifacts/capability_advice.json', None),
            'human_confirmation': {'reason': state.get('stop_reason'),
                                   'continuation': 'immutable_child',
                                   'approval_required': True} if native == 'NEEDS_HUMAN' else None}


def dispatch(config, request):
    home = Path(config['home']).resolve()
    projects = config.get('projects', {})
    action = request.get('action')
    allowed = {'catalog': {'action'}, 'status': {'action', 'run_id'},
               'result': {'action', 'run_id'}, 'cancel': {'action', 'run_id'},
               'submit': {'action', 'project', 'request', 'artifact_id', 'operation'},
               'continue': {'action', 'run_id', 'request'},
               'advice': {'action', 'run_id'}, 'advice_result': {'action', 'run_id', 'advisor_run_id'}}
    if action not in allowed or set(request) - allowed[action]:
        raise ValueError('Unknown action/fields')
    if action == 'catalog':
        return {'projects': sorted(projects), 'operations': ['create', 'repair', 'modify', 'clone', 'continue']}
    if action in {'advice', 'advice_result'}:
        from .subject_advice import request_advice, result_advice
        subject = run_at(home, request.get('run_id'))
        return (request_advice(home, subject, config) if action == 'advice' else
                result_advice(home, subject.run_id, request.get('advisor_run_id')))
    if action in {'status', 'result', 'cancel'}:
        run = run_at(home, request.get('run_id'))
        if action == 'cancel': run.request_cancel()
        return view(run)
    text = request.get('request')
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8000:
        raise ValueError('Nonempty mission within 8KB required')
    folder = home / 'artifact_data'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'headless.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        supervisor = config.get('process_supervisor', 'session')
        if supervisor not in {'session', 'systemd'}: raise ValueError('Unknown process supervisor')
        if supervisor == 'session' and '.service' in Path('/proc/self/cgroup').read_text():
            raise ValueError('Service-hosted gateway requires an independent systemd supervisor')
        if supervisor == 'systemd' and not shutil.which('systemd-run'):
            raise ValueError('Independent user systemd supervisor is unavailable')
        # Admission only, not a second task/state machine.
        for path in (home / 'runs').glob('*/headless_dispatch.json'):
            state = load(path.parent / 'state.json', {})
            if state.get('status') not in RunStore.TERMINAL:
                raise ValueError('An admitted mission is still active: ' + path.parent.name)
        endpoint = config['endpoint']
        model = config.get('model') or discover_local_model(endpoint)
        health(endpoint, model)
        if action == 'continue':
            parent = run_at(home, request.get('run_id'))
            state = parent.state()
            if state.get('llm_config',{}).get('provider','local')!='local':
                raise ValueError('Cloud continuation requires interactive CLI payload approval; no automatic model switch')
            if state['status'] not in parent.TERMINAL:
                raise ValueError('Finish/cancel the parent first')
            run = create_mission(home, state['workspace'], load(parent.path / 'root_goal.json')['request'],
                                 endpoint, model, parent=parent.path, instruction=text)
            context = load(parent.path / 'artifact_input.json', None)
            if context:
                context['resume_from_run'] = parent.run_id
                dump(run.path / 'artifact_input.json', context)
                run.save_state(input_artifact_id=context['artifact_id'], artifact_operation=context['operation'])
        elif request.get('operation', 'create') != 'create':
            run = create_artifact_mission(home, request.get('artifact_id'), request['operation'], text, endpoint, model)
        else:
            if request.get('artifact_id'): raise ValueError('Create cannot target an artifact')
            project = request.get('project')
            if project not in projects: raise ValueError('Unregistered workspace')
            run = create_mission(home, projects[project], text, endpoint, model)
        log = run.path / 'logs/headless_controller.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'HOME', 'XDG_RUNTIME_DIR') if key in os.environ}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE='1')
        try:
            command = [sys.executable, '-B', '-m', 'pitchi_work.cli', '--home', str(home), 'resume', run.run_id]
            if supervisor == 'systemd':
                unit = 'pitchi-work-' + run.run_id
                # start_new_session does NOT escape a service's cgroup. The
                # user manager owns this separate transient unit. env -i keeps
                # credentials out even if the manager itself inherited them.
                launched = subprocess.run(['systemd-run', '--user', '--quiet', '--collect', '--unit=' + unit,
                    '--property=Type=exec', '--property=KillMode=process', '--property=StandardOutput=append:' + str(log),
                    '--property=StandardError=append:' + str(log), '/usr/bin/env', '-i',
                    *[key + '=' + value for key, value in env.items()], *command],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                if launched.returncode: raise OSError('Independent supervisor rejected controller start')
                record = {'unit': unit, 'supervisor': 'systemd', 'interface': 'headless-v1'}
            else:
                with log.open('ab') as output:
                    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                             stderr=output, env=env, start_new_session=True)
                record = {'pid': child.pid, 'supervisor': 'session', 'interface': 'headless-v1'}
            dump(run.path / 'headless_dispatch.json', record)
        except (OSError, subprocess.TimeoutExpired):
            run.save_state(status='FAILED', failure_class='HOST_DISPATCH_DEFECT', stop_reason='Controller process could not start')
            raise
        return view(run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.config.stat().st_mode & 0o077:
            raise ValueError('Private gateway config requires owner-only permissions')
        incoming = sys.stdin.buffer.read(32769)
        if len(incoming) > 32768: raise ValueError('Input budget exceeded')
        # The gateway stdout is exactly one JSON document. Controller setup
        # diagnostics belong on stderr, including before dispatch completes.
        with redirect_stdout(sys.stderr):
            result = dispatch(load(args.config), json.loads(incoming))
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except Exception as error:
        # No tracebacks, environment values or credential contents in protocol.
        print(json.dumps({'ok': False, 'error': str(error), 'error_type': type(error).__name__}, ensure_ascii=False))
        return 2


if __name__ == '__main__': raise SystemExit(main())
