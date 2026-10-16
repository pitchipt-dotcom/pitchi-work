"""Host-owned observations for finite Python profiles, never Worker tests."""
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .store import dump,load
from .runner import HostDefect
from .mission_profiles import sha


def snapshot(workspace):
    return {p.relative_to(workspace).as_posix():sha(p) for p in Path(workspace).rglob('*')
            if p.is_file() and '__pycache__' not in p.parts}


def invocation(run,args,timeout,host_files=()):
    """No network, no writable origin, no inherited secrets or service access."""
    workspace=Path(run.state()['workspace']).resolve()
    command=['bwrap','--ro-bind','/','/','--unshare-net','--unshare-pid','--die-with-parent',
             '--tmpfs','/home','--tmpfs','/tmp','--tmpfs','/run','--tmpfs','/mnt','--tmpfs','/media','--tmpfs','/root',
             '--proc','/proc','--dev','/dev',
             '--dir',str(workspace.parent),'--ro-bind',str(workspace),str(workspace),
             '--chdir',str(workspace)]
    for source,destination in host_files:command+=['--ro-bind',str(source),destination]
    command+=['--',*args]
    env={'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    try:process=subprocess.Popen(command,cwd=workspace,env=env,start_new_session=True,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    except OSError as exc:raise HostDefect('Profile validator runner unavailable: '+str(exc)) from exc
    started=time.monotonic()
    try:
        run.register_process(process.pid,'host_profile_validator')
        while True:
            run.raise_if_cancelled()
            if time.monotonic()-started>timeout:raise HostDefect('Profile validator timeout')
            try:stdout,stderr=process.communicate(timeout=.2);break
            except subprocess.TimeoutExpired:continue
    except BaseException:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.communicate(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.communicate()
        raise
    finally:run.unregister_process(process.pid)
    run.raise_if_cancelled()  # A cancel racing with process exit is not a test result.
    if 'bwrap:' in stderr:raise HostDefect('Sandbox execution failure: '+stderr.strip())
    # Require the fixed Host bootstrap's structured completion report; a
    # project import that exits zero early or prints unittest-like text is
    # not an observed test run. Never parse Worker self-report as acceptance.
    reports=[line.split('PITCHI_HOST_TEST_RESULT=',1)[1] for line in stdout.splitlines()
             if line.startswith('PITCHI_HOST_TEST_RESULT=')]
    observation={}
    if len(reports)==1:
        try:observation=json.loads(reports[0])
        except ValueError:pass
    count=observation.get('tests_run',0);skipped=observation.get('skipped',0)
    valid=type(count) is int and type(skipped) is int and 0<=skipped<=count and observation.get('completed') is True
    return {'argv':args,'cwd':str(workspace),'exit_code':process.returncode,'stdout':stdout,'stderr':stderr,
            'elapsed_seconds':round(time.monotonic()-started,3),'tests_run':count-skipped if valid else 0,
            'host_test_observation':observation if valid else None,
            'sandbox':'read-only workspace; isolated network/pid/home; no secrets in environment'}


def validate(run,contract,attempt,report):
    check=dict(contract);saved_hash=check.pop('sha256')
    if hashlib.sha256(json.dumps(check,sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=saved_hash:
        raise HostDefect('Profile contract hash changed')
    workspace=Path(run.state()['workspace']);current=snapshot(workspace)
    baseline=load(run.path/'artifacts/host_baseline.json');allowed=set(contract['allowed_paths'])
    # Other completed tasks may own edits; all declared scope remains Host-owned.
    plan=load(run.path/'plan.json');all_allowed={p for t in plan['tasks'] for p in t['allowed_paths']}
    scope=all(current.get(n)==h for n,h in baseline.items() if n not in all_allowed) and set(current)<=set(baseline)|all_allowed
    checks=[{'validator':'scope','pass':scope}];executions=[];diffs={}
    if contract['profile']=='static_web_app':
        from .web_validation import observe
        web_checks,executions,diffs=observe(run,contract,attempt)
        checks.extend(web_checks)
        artifact_input=load(run.path/'artifact_input.json',{})
        if artifact_input.get('operation')=='clone':
            from html.parser import HTMLParser
            class Title(HTMLParser):
                def __init__(self):super().__init__();self.inside=False;self.text=''
                def handle_starttag(self,tag,attrs):
                    if tag=='title':self.inside=True
                def handle_endtag(self,tag):
                    if tag=='title':self.inside=False
                def handle_data(self,text):
                    if self.inside:self.text+=text
            before_title=Title();after_title=Title()
            before_title.feed((run.path/'baseline_files/index.html').read_text());after_title.feed((workspace/'index.html').read_text())
            checks.append({'validator':'clone_visible_identity','pass':bool(after_title.text.strip()) and before_title.text!=after_title.text,
                           'before':before_title.text,'after':after_title.text})
    elif contract['profile']=='php_search_quality':
        from .search_quality import validate as search_validate
        more,executions,diffs,comparison=search_validate(run,contract,attempt)
        checks.extend(more)
    elif contract['profile']=='code_investigation':
        findings=report.get('findings',[])
        valid=isinstance(findings,list) and bool(findings)
        if valid:
            for finding in findings:
                if not isinstance(finding,dict):valid=False;break
                name=finding.get('path');line=finding.get('line');quote=finding.get('quote')
                if name not in contract['input_files'] or type(line)!=int or not isinstance(quote,str) or not quote:
                    valid=False;break
                lines=(workspace/name).read_text().splitlines()
                if not 1<=line<=len(lines) or quote not in lines[line-1]:valid=False;break
        checks.append({'validator':'source_references','pass':valid})
    else:
        syntax=True
        for name in contract['expected_outputs']:
            try:ast.parse((workspace/name).read_text(),filename=name)
            except (SyntaxError,OSError,UnicodeError):syntax=False
            original=(run.path/'baseline_files'/name).read_text()
            changed=(workspace/name).read_text()
            diffs[name]=''.join(difflib.unified_diff(original.splitlines(True),changed.splitlines(True),fromfile=name,tofile=name))
        checks.append({'validator':'python_syntax','pass':syntax})
        for name in contract['existing_tests']:
            if current.get(name)!=baseline.get(name):raise HostDefect('Protected existing test changed')
            test=Path(name)
            # Fixed Host bootstrap imports stdlib unittest BEFORE exposing the
            # project import path. Supports a tests directory without __init__.
            bootstrap=('import sys,unittest,json;sys.path.insert(0,sys.argv[1]);'
                       'suite=unittest.defaultTestLoader.discover(sys.argv[2],pattern=sys.argv[3]);'
                       'result=unittest.TextTestRunner(verbosity=2).run(suite);'
                       'print("PITCHI_HOST_TEST_RESULT="+json.dumps({"completed":True,"tests_run":result.testsRun,"skipped":len(result.skipped)}));'
                       'sys.exit(0 if result.wasSuccessful() else 1)')
            executions.append(invocation(run,['/usr/bin/python3','-I','-B','-c',bootstrap,
                str(workspace),str(workspace/test.parent),test.name],contract['timeout']))
        checks.append({'validator':'existing_unittest','pass':bool(executions) and all(
            x['exit_code']==0 and x['tests_run']>0 for x in executions)})
        # Diff is evidence of observed changes, not itself proof of correctness.
        checks.append({'validator':'change_diff','pass':bool(diffs)})
    receipt={'owner':'host','contract_sha256':saved_hash,'active_workspace':str(workspace),
             'checks':checks,'passed':all(c['pass'] for c in checks),'executions':executions,
             'artifact_hashes':{n:current.get(n) for n in (contract['expected_outputs'] or contract['input_files'])},'diff':diffs,
             'report':report if contract['profile']=='code_investigation' else None}
    if contract['profile']=='php_search_quality':receipt['search_comparison']=comparison
    path=run.path/'artifacts'/f"{contract['task_id']}_host_{attempt}.json"
    dump(path,receipt)
    return {'reference':path.relative_to(run.path).as_posix(),'sha256':sha(path),**receipt}
