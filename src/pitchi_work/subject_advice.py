"""On-demand Advisor sidecar; never modifies the subject mission or its evidence."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from .store import RunStore, load, dump
from .model import LocalRoleAdapter, discover_local_model
from .capability_advisor import advise


def request_advice(home, subject, config):
    identifier = uuid4().hex
    folder = home / 'artifact_data' / 'advisor_requests' / identifier
    folder.mkdir(parents=True, mode=0o700)
    state = subject.state()
    if state['status'] not in RunStore.TERMINAL:
        raise ValueError('Advisor requires a terminal subject')
    dump(folder / 'state.json', {'status': 'RUNNING', 'workspace': state['workspace']})
    dump(folder / 'root_goal.json', load(subject.path / 'root_goal.json'))
    dump(folder / 'subject.json', {'subject_run_id': subject.run_id, 'advisor_run_id': identifier,
         'failure_class': state.get('failure_class'), 'reason': state.get('stop_reason'),
         'stopped_stage': state.get('stage')})
    endpoint = config['endpoint']
    model = config.get('model') or discover_local_model(endpoint)
    command = [sys.executable, '-B', '-m', 'pitchi_work.subject_advice', str(folder), endpoint, model]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'HOME', 'XDG_RUNTIME_DIR') if k in os.environ}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE='1')
    if config.get('process_supervisor') == 'systemd':
        command = ['systemd-run', '--user', '--quiet', '--collect', '--unit=pitchi-advisor-'+identifier,
                   '--property=Type=exec', '/usr/bin/env', '-i',
                   *[k+'='+v for k,v in env.items()], *command]
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=8)
        if result.returncode:
            dump(folder / 'state.json', {'status': 'FAILED', 'failure_class': 'ADVISOR_DISPATCH_FAILURE'})
    else:
        subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return result_advice(home, subject.run_id, identifier)


def result_advice(home, subject_id, identifier):
    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('Invalid Advisor ID')
    folder = home / 'artifact_data' / 'advisor_requests' / identifier
    subject = load(folder / 'subject.json', {})
    if subject.get('subject_run_id') != subject_id:
        raise ValueError('Advisor subject mismatch')
    state = load(folder / 'state.json')
    return {**subject, 'status': state['status'],
            'subject_failure_class': subject.get('failure_class'), 'subject_reason': subject.get('reason'),
            'advisor_failure_class': state.get('failure_class'), 'advisor_error': state.get('stop_reason'),
            'capability_advice': load(folder / 'artifacts/capability_advice.json', None)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path); parser.add_argument('endpoint'); parser.add_argument('model')
    args = parser.parse_args()
    record = RunStore(args.folder)
    subject = load(args.folder / 'subject.json')
    try:
        advise(record, {'kind': 'existing_run_failure', **subject},
               LocalRoleAdapter(record, args.endpoint, args.model))
        report = load(args.folder / 'artifacts/capability_advice.json')
        report.update(subject_run_id=subject['subject_run_id'], advisor_run_id=subject['advisor_run_id'])
        dump(args.folder / 'artifacts/capability_advice.json', report)
        record.save_state(status='COMPLETED')
    except Exception as exc:
        record.save_state(status='FAILED', failure_class='ADVISOR_FAILURE', stop_reason=type(exc).__name__)


if __name__ == '__main__': main()
