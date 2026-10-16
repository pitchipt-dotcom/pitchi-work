"""Read-only capability diagnosis. Suggestions never become execution authority."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .mission_profiles import PROFILES, inventory, UnsupportedMission, HumanApprovalRequired
from .model import ModelFailure
from .store import dump, load
from .tools import catalog

PROBES = {
    'python3': ['--help'], 'tar': ['--help'], 'zip': ['--help'],
    'ffmpeg': ['-h'], 'tesseract': ['--help'], 'potrace': ['--help'],
}


def approval_pending(run, exc):
    """Never grant permission: preserve unanimous explicit human-stop signals."""
    if isinstance(exc, HumanApprovalRequired):return True
    if getattr(exc,'failure_class','')!='REQUIREMENT_OUTPUT_INVALID':return False
    proposals=[]
    for path in sorted((run.path/'artifacts').glob('role_call_*_requirement/output.txt')):
        try:proposals.append(json.loads(path.read_text()))
        except (ValueError,OSError):return False
    return bool(proposals) and all(isinstance(p,dict) and p.get('needs_human') is True for p in proposals)


def shortage(run, exc):
    """Positive Host evidence only; no text matching of arbitrary runtime errors."""
    if isinstance(exc, HumanApprovalRequired):
        return None
    if getattr(exc,'output_retry_exhausted',False):
        return {'kind':'worker_output_correction_exhausted','reason':str(exc),
                'original_failure_class':exc.failure_class,
                'capability_missing_confirmed':False,
                'note':'Host finite output correction was exhausted; this alone does not prove a missing tool or environment capability.'}
    if isinstance(exc, UnsupportedMission):
        for path in (run.path/'artifacts').glob('role_call_*_requirement/output.txt'):
            try: proposal=json.loads(path.read_text())
            except (ValueError,OSError): continue
            if isinstance(proposal,dict) and proposal.get('needs_human') is True:return None
        return {'kind': 'registered_profile_boundary', 'reason': str(exc),
                'original_failure_class':exc.failure_class}
    # Actual observed case: an invalid shape hides an independently provable
    # lack of editable inputs/tests. Do not reinterpret other invalid outputs.
    if getattr(exc, 'failure_class', '') != 'REQUIREMENT_OUTPUT_INVALID':
        return None
    try:observed = inventory(Path(run.state()['workspace']))
    except UnsupportedMission:return None
    proposals = []
    for path in sorted((run.path/'artifacts').glob('role_call_*_requirement/output.txt')):
        try: proposals.append(json.loads(path.read_text()))
        except (ValueError, OSError): return None
    if proposals and all(isinstance(p,dict) and p.get('profile')=='unsupported' and p.get('needs_human') is False for p in proposals):
        return {'kind':'no_registered_profile_selected',
                'reason':'Requirement selected the explicit unsupported option on every attempt; no Host contract was compiled.',
                'original_failure_class':exc.failure_class,'original_error':str(exc)}
    if proposals and all(isinstance(p, dict) and p.get('profile') in {'code_change', 'bug_fix'}
                         and p.get('needs_human') is False for p in proposals):
        if not observed['tests'] or not any(r['path'].endswith('.py') for r in observed['files']):
            return {'kind': 'existing_python_profile_inputs_missing',
                    'reason': 'Python editing profiles require existing implementation and protected unittest inputs; these are absent.',
                    'original_failure_class': exc.failure_class, 'original_error': str(exc)}
    return None


def collect_environment(workspace):
    """Bounded, explicit probes; no credential/env dumps or generated commands."""
    workspace = Path(workspace).resolve()
    files = inventory(workspace)
    commands = {}
    for name, args in PROBES.items():
        executable = shutil.which(name)
        row = {'available': bool(executable), 'probe': [name, *args]}
        if executable:
            try:
                result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=3,
                                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
                row.update(exit_code=result.returncode,
                           help=(result.stdout+result.stderr)[:12000])
            except (OSError, subprocess.TimeoutExpired) as exc:
                row['probe_error'] = type(exc).__name__
        commands[name] = row
    packages = {}
    for name in ('Pillow', 'pytesseract', 'playwright', 'numpy', 'opencv-python'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    return {'scope': 'Only the listed commands/packages and workspace were surveyed; absence elsewhere is not asserted.',
            'profiles': PROFILES, 'tools': catalog(), 'workspace_files': files,
            'commands': commands, 'python_packages': packages,
            'python_version': list(sys.version_info[:3])}


def read_advice(run_path):
    """Read-only Python/Pitchi-chat boundary; never rerun or mutate a mission."""
    return load(Path(run_path)/'artifacts/capability_advice.json',None)


def verify_command(command, facts, workspace):
    """Never execute proposals. VERIFIED is limited to exact Host-observed probes."""
    result = {'command': command, 'verification': 'UNVERIFIED', 'executed': False,
              'requires_human_review': True, 'checks': []}
    if not isinstance(command, str) or len(command) > 2000:
        result['checks'].append('Invalid/oversized command'); return result
    try: tokens = shlex.split(command)
    except ValueError:
        result['checks'].append('Invalid quoting'); return result
    if not tokens: return result
    # Conservative: no shell syntax, shell interpreters, inline code, deletion,
    # privilege, network, installation or process/service control proposals.
    dangerous = {'sudo','su','rm','mv','chmod','chown','kill','pkill','systemctl','curl','wget',
                 'pip','pip3','apt','docker','bash','sh','eval','exec','dd','tee'}
    if (tokens[0] in dangerous or tokens[0] not in facts['commands'] or
        any(c in command for c in (';', '|', '&', '`', '$', '>', '<', '\n')) or
        any(t in {'-c', '--delete', '--remove-files', '--overwrite', '--force', '-y'} for t in tokens[1:])):
        result['safety']='NOT_AUTHORIZED'
        result['checks'].append('Outside read-only verified command policy; not executed'); return result
    proof = facts['commands'][tokens[0]]
    if not proof['available']:
        result['checks'].append('Executable not observed'); return result
    result['checks'].append('Executable observed')
    if tokens == proof['probe'] and proof.get('exit_code') == 0:
        result.update(verification='VERIFIED', requires_human_review=False)
        result['checks'].append('Exact read-only help probe succeeded; this does not prove the requested job is complete')
        return result
    unknown = []
    for token in tokens[1:]:
        if token.startswith('-'):
            if token not in proof.get('help', ''): unknown.append('Option not confirmed: '+token)
        else:
            path = (Path(workspace)/token).resolve()
            if not path.is_relative_to(Path(workspace).resolve()) or not path.exists():
                unknown.append('Path/argument not confirmed: '+token)
    result.update(verification='PARTIALLY_VERIFIED')
    result['checks'].extend(unknown or ['Options/paths observed; operation and semantic result NOT executed or verified'])
    return result


def accept_proposal(value, evidence_ids):
    fields = {'intent','missing_capabilities','recommendation','manual','human'}
    if not isinstance(value, dict) or set(value) != fields: raise ValueError('Use exact Advisor response fields')
    if any(not isinstance(value[k], str) or not value[k].strip() or len(value[k]) > 1800
           for k in ('intent','recommendation','human')): raise ValueError('Bounded nonempty explanation required')
    missing = value['missing_capabilities']
    if not isinstance(missing, list) or not 1 <= len(missing) <= 4: raise ValueError('1..4 missing capabilities required')
    for row in missing:
        if not isinstance(row, dict) or set(row) != {'name','reason','reusable','evidence_ids'}:
            raise ValueError('Invalid missing capability structure')
        if type(row['reusable']) is not bool or any(not isinstance(row[k], str) or not row[k] or len(row[k])>1200 for k in ('name','reason')):
            raise ValueError('Invalid missing capability fields')
        if not isinstance(row['evidence_ids'], list) or not row['evidence_ids'] or any(not isinstance(i,str) or i not in evidence_ids for i in row['evidence_ids']):
            raise ValueError('Missing capability must reference real Host evidence')
    manual = value['manual']
    if not isinstance(manual, dict) or set(manual) != {'possible','steps','commands'} or type(manual['possible']) is not bool:
        raise ValueError('Invalid manual option')
    if any(not isinstance(manual[k], list) or len(manual[k]) > 4 or any(not isinstance(s,str) or len(s)>2000 for s in manual[k]) for k in ('steps','commands')):
        raise ValueError('Bounded manual steps/commands required')
    if not manual['possible'] and (manual['commands'] or manual['steps']): raise ValueError('Unavailable option must not include commands')
    return value


def advise(run, blocker, adapter):
    """Called before terminalization; proposal failures never change the shortage."""
    facts = collect_environment(run.state()['workspace'])
    facts['local_role_observations']=[{'role':load(p).get('role'),'actual_model':load(p).get('actual_model'),
                                     'finish_reason':load(p).get('finish_reason'),'reference':str(p.relative_to(run.path))}
                                    for p in sorted((run.path/'artifacts').glob('role_call_*/receipt.json'))]
    facts['cli']={'entrypoint_present':(Path(__file__).resolve().parents[2]/'scripts/pitchi-work').is_file(),
                  'help_command':['pitchi-work','--help']}
    evidence = []
    for identifier, filename, content in (
        ('E_STOP','capability_stop.json',blocker), ('E_ENV','capability_environment.json',facts),
    ):
        path = run.path/'artifacts'/filename; dump(path,content)
        evidence.append({'id':identifier,'reference':'artifacts/'+filename,
                         'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    for path in sorted((run.path/'artifacts').glob('role_call_*_requirement/*')):
        if path.name not in {'receipt.json','output.txt'}:continue
        evidence.append({'id':'E_ROLE_'+str(len(evidence)), 'reference':str(path.relative_to(run.path)),
                         'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    request = load(run.path/'root_goal.json')['request']
    # Full help is retained privately for checks; model receives only bounded facts.
    summary = {**facts, 'commands':{n:{k:v for k,v in r.items() if k!='help'} for n,r in facts['commands'].items()}}
    payload = {'request':request,'stop':blocker,'facts':summary,'evidence':evidence,
               'schema':{'intent':'string','missing_capabilities':[{'name':'string','reason':'string','reusable':True,'evidence_ids':['E_ENV']}],
                         'recommendation':'minimum reusable addition or honest human-only limitation',
                         'manual':{'possible':False,'steps':[],'commands':[]},'human':'string'},
               'rules':'Advisor only. No self modification. No invented environment facts. Refer to supplied evidence IDs. '
                       'Separate one-off manual alternatives from reusable capability. Human/physical tasks need not become tools. '
                       'Commands are suggestions, never verified by you, never executed. No new acceptance conditions. '
                       'Recommend the smallest addition. Use concise Japanese. Return exactly the schema fields.'}
    proposal = None; errors = []
    for attempt in range(2):
        run.raise_if_cancelled()
        try:
            response = adapter.ask('capability_advisor',json.dumps({**payload,'format_feedback':errors[-1:]},ensure_ascii=False),max_output_tokens=2400)
            proposal = accept_proposal(response,{e['id'] for e in evidence}); break
        except (ValueError,ModelFailure) as exc:
            errors.append({'failure_class':getattr(exc,'failure_class',type(exc).__name__), 'reason':str(exc)})
            if isinstance(exc,ModelFailure) and exc.failure_class not in {'AGENT_OUTPUT_INCOMPLETE','AGENT_OUTPUT_INVALID_JSON','AGENT_OUTPUT_INVALID_SHAPE'}: break
    if proposal:
        manual = proposal['manual']; commands = [verify_command(c,facts,run.state()['workspace']) for c in manual['commands']]
        manual_option = {'assessment':'LLM proposal, not an execution guarantee','proposed_possible':manual['possible'],
                         'possible':False if not manual['possible'] else None,
                         'steps':manual['steps'],'commands':commands,
                         'verification':'PARTIALLY_VERIFIED' if manual['possible'] else 'UNVERIFIED'}
        missing = proposal['missing_capabilities']; recommendation = proposal['recommendation']; human = proposal['human']
    else:
        manual_option={'possible':None,'verification':'UNVERIFIED','steps':[],'commands':[],'reason':'Advisor unavailable; no alternative was established'}
        missing=[{'name':blocker['kind'],'reason':blocker['reason'],'reusable':None,'evidence_ids':['E_STOP']}]
        recommendation='Hostが確認した停止点とregistryを調査してください。分析LLMの提案は取得できませんでした。'
        human='不足能力の追加または依頼範囲の見直しを選択してください。自動変更は行いません。'
    prompt = ('Pitchi-workの育成依頼\n元の実案件: '+request+'\n停止点: '+blocker['reason']+
              '\nEvidence: '+json.dumps(evidence,ensure_ascii=False)+
              '\n現在利用可能: '+', '.join(PROFILES)+'; '+', '.join(facts['tools'])+
              '\n不足能力: '+json.dumps(missing,ensure_ascii=False)+'\n最小追加案: '+recommendation+
              '\n今回の案件だけを直接解決せず、まずPitchi-workで停止を再現し、同種案件でも再利用できる能力を追加し、'
              'その後、元の案件をPitchi-workから再実行して完遂を確認してください。'
              '\n成功条件: 元依頼の機械的条件をHost evidenceで確認。条件を弱めず、危険操作は承認を得る。'
              '\n回帰確認: create / repair / modify / clone、Artifact / Preview、既存Host validatorとcompletion gate。')
    report={'schema_version':1,'status':'NEEDS_CAPABILITY','run_id':run.run_id,'request':request,
            'reason':blocker['reason'],'stop':blocker,'evidence':evidence,
            'analysis_status':'AVAILABLE' if proposal else 'UNAVAILABLE','advisor_errors':errors,
            'intent':proposal['intent'] if proposal else request,
            'progress':{'stopped_stage':blocker.get('stopped_stage'),
                        'tasks':[{'task_id':p.stem,'status':load(p).get('status')} for p in sorted((run.path/'tasks').glob('*.json'))]},
            'missing_capabilities':missing,'recommendation':recommendation,
            'options':{'codex':{'request':prompt},'manual':manual_option,'human':{'action':human}},
            'verification_strength':{'host_facts':'STRICT','recommendations':'ASSISTED'},
            'authority':'Host facts and safety checks; LLM suggestions only. No commands or changes executed.'}
    dump(run.path/'artifacts/capability_advice.json',report)
    return {'reference':'artifacts/capability_advice.json','analysis_status':report['analysis_status']}
