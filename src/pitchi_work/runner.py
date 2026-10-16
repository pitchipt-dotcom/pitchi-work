"""Registered Host validators only; no model-written commands or test routing."""
import os
import signal
import shlex
import subprocess
from pathlib import Path
from .store import load,Cancelled,terminate_run_processes
from .execution_context import context,artifact_snapshot,assert_workspace
from .host_contracts import authority,TEMPLATES
from .host_validator_runner import command,TIMEOUT_SECONDS


class HostDefect(RuntimeError):
    failure_class='HOST_VALIDATOR_DEFECT'


def execute_registered(run, task):
    workspace=context(run)['active_workspace'];assert_workspace(run,workspace)
    if task['host_contract_sha256']!=authority(task):raise HostDefect('Host contract hash mismatch')
    run.raise_if_cancelled()
    previous=set((run.path/'artifacts').glob('host_evidence_'+task['template_id']+'_*.json'))
    invocation=command(workspace,run.path,task['template_id'])
    env={k:v for k,v in os.environ.items() if k!='OPENAI_API_KEY'}
    state=run.state()
    env.update(BOOK_AI_QA_LLM_URL=state['local_model_endpoint'].rstrip('/')+'/chat/completions',
               BOOK_AI_QA_LLM_MODEL=state['local_model'],PYTHONDONTWRITEBYTECODE='1')
    # Workspace code is untrusted. Existing projects/runs are read-only mounts;
    # only this working copy, this run and private /tmp can be written.
    isolated=['bwrap','--ro-bind','/','/','--dev-bind','/dev','/dev',
              '--bind',str(run.path),str(run.path),'--bind',workspace,workspace,'--tmpfs','/tmp']
    baseline=load(run.path/'artifacts/host_baseline.json',{})
    allowed={p for t in TEMPLATES.values() for p in t['outputs']}
    for name in baseline:
        path=Path(workspace)/name
        if name not in allowed and path.is_file():isolated+=['--ro-bind',str(path),str(path)]
    isolated+=['--chdir',workspace,'--',*shlex.split(invocation)]
    try:
        process=subprocess.Popen(isolated,cwd=workspace,env=env,start_new_session=True,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    except (OSError,ValueError) as exc:
        raise HostDefect('Host validator process could not start: '+str(exc)) from exc
    try:
        run.register_process(process.pid,'host_validator')
        import time
        deadline=time.monotonic()+TIMEOUT_SECONDS
        while True:
            run.raise_if_cancelled()
            if time.monotonic()>deadline:raise HostDefect('Host validator timeout')
            try:stdout,stderr=process.communicate(timeout=min(0.2,max(0.01,deadline-time.monotonic())));break
            except subprocess.TimeoutExpired:continue
    except BaseException:
        # Stop only this registered child group; cancellation cannot spawn retries.
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.communicate(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.communicate()
        raise
    finally:run.unregister_process(process.pid)
    receipts=sorted(set((run.path/'artifacts').glob('host_evidence_'+task['template_id']+'_*.json'))-previous)
    registry=load(receipts[-1],{}) if receipts else {}
    if not receipts or registry.get('failure_owner')=='host' or registry.get('mode')!='live':
        raise HostDefect(registry.get('error') or stderr or 'Host receipt missing')
    passed=process.returncode==0 and registry.get('passed') is True
    return {'command':'host-validator:'+task['template_id'],'task_id':task['task_id'],
            'exit_code':0 if passed else 1,'stdout':stdout,'stderr':stderr,'registry_result':registry,
            'execution_context':context(run),'artifact_snapshot':artifact_snapshot(__import__('pathlib').Path(workspace),task),
            'host_execution_evidence':[{'source':'host_validator','kind':kind,
                'validator_ids':TEMPLATES[task['template_id']]['validators'],
                'evidence_artifact':str(receipts[-1].relative_to(run.path)),
                'host_contract_sha256':task['host_contract_sha256']}
                for kind in registry.get('proof_kinds',[]) if passed]}
