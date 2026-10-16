"""Host-verified recovery forks; no checkpoint synthesis or terminal rewrites."""
import fcntl
import hashlib
from pathlib import Path
import shutil
import uuid
from datetime import datetime

from .store import RunStore, dump, load, now
from .runner import HostDefect
from .mission_profiles import sha


def reserve_local_repair_call(run, role):
    """Shared fail-closed grant; failed/time-out calls remain consumed."""
    identifier=run.state().get('local_repair_budget')
    if not identifier:return
    from .resolution import read_verified,write_verified
    from .model import ModelFailure
    import re
    if not re.fullmatch('[0-9]{8}_[0-9]{6}_[a-f0-9]{6}',identifier):raise HostDefect('REPAIR_BUDGET_ID_INVALID')
    folder=run.path.parent.parent/'local_repair_budgets';folder.mkdir(exist_ok=True)
    with (folder/(identifier+'.lock')).open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        path=folder/(identifier+'.json');ledger=read_verified(path)
        if len(ledger['reservations'])>=ledger['limit']:
            raise ModelFailure('LOCAL_REPAIR_CALL_LIMIT','Shared explicitly granted local call budget exhausted')
        ledger['reservations'].append({'run_id':run.run_id,'role':role,'at':now()})
        write_verified(path,ledger)


def manifest(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise HostDefect('RECOVERY_UNSAFE_PATH: symlink in saved state')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def assert_recoverable_journal(parent):
    """Existing exhausted-attempt guard, before copying or model preflight."""
    repair=None
    if (parent.path/'artifacts/local_repair_receipt.json').exists():
        from .resolution import read_verified
        repair=read_verified(parent.path/'artifacts/local_repair_receipt.json')
    for path in (parent.path/'tasks').glob('*.json'):
        record=load(path)
        if any(a.get('state')=='FAILED' and not (repair and record['task']['task_id']==repair['task_id'] and
            a['attempt']<repair['new_attempt']) for a in record.get('attempts',[])):
            raise HostDefect('RECOVERY_RETRY_EXHAUSTED: failed Worker attempt cannot be reset; '
                'history and limits preserved; a separately authorized new task or verified migration is required')


def pending_context_dispatch(parent):
    """Prove a local guard stopped BEFORE a model call; never reset a retry."""
    from .phase_workflow import digest
    state=parent.state()
    if not state.get('stop_reason','').startswith('PHASE_CONTEXT_DEFECT:'):return []
    phase=state.get('current_phase')
    records=[]
    for folder in (parent.path/'checkpoints').glob('*/*/calls/'+str(phase)):
        journals=sorted(folder.glob('*.json'),key=lambda p:int(p.stem))
        if not journals:continue
        path=journals[-1];value=load(path)
        if value.get('status') not in ('STARTED','PRE_DISPATCH_FAILED'):continue
        base=folder.parent.parent
        proof=None
        for audit in (base/'working_history').glob('*.json'):
            item=load(audit)
            if not isinstance(item,dict) or item.get('status')!='INFEASIBLE':continue
            from .working_context import WorkingContext
            original=WorkingContext(base/'working_history',parent.path).read(item['original'])
            if digest(original)==value.get('input_sha256'):
                proof={'reference':str(audit.relative_to(parent.path)),'sha256':sha(audit)};break
        if proof is None:continue
        # Every actual model invocation is receipted and predates this pending
        # slot. An interrupted/unreceipted invocation is NEVER reclaimable.
        roles=list((parent.path/'artifacts').glob('role_call_*'))
        if any(not (r/'receipt.json').is_file() or
               load(r/'receipt.json').get('started_at','9999')>=value['at'] for r in roles):continue
        records.append({'journal':str(path.relative_to(parent.path)),'sha256':sha(path),
            'slot':int(path.stem),'phase':phase,'guard_proof':proof,
            'model_dispatched':False,'retry_limit_unchanged':True})
    return records


def transfer_targets(parent,child):
    """Keep historical IDs immutable; reissue current blocks using Host.issue."""
    from .edit_targets import EditTargets,text_hash
    from .resolution import read_verified,write_verified
    receipts=[]
    for task in load(child.path/'plan.json')['tasks']:
        for base in sorted((child.path/'checkpoints'/task['task_id']).glob('*')):
            registry=base/'edit_targets.json'
            if not registry.exists():continue
            origin=parent.path/registry.relative_to(child.path)
            old=EditTargets(origin,task['allowed_paths'],task['sha256'])
            data=old._read()
            archive=base/('target_archive_'+sha(origin)+'.json')
            shutil.copyfile(origin,archive)
            registry.unlink()  # Exact copied registry only, not parent/history.
            live=EditTargets(registry,task['allowed_paths'],task['sha256'])
            write_verified(registry,{'identity':live.identity,'targets':{}})
            files=load(base/'input.json')['files'];issued=[];deferred=[]
            for identifier,record in data['targets'].items():
                name=record['path'];source=files.get(name)
                if name not in task['allowed_paths']:raise HostDefect('RECOVERY_TARGET_SCOPE_MISMATCH')
                if source is None or text_hash(source)!=record['source_sha256']:
                    deferred.append(identifier);continue # Recheck at exact staged replay boundary.
                lines=source.splitlines(True)
                exact=''.join(lines[record['start_line']-1:record['end_line']])
                if exact!=record['original_text']:raise HostDefect('RECOVERY_TARGET_RANGE_MISMATCH')
                target=live.issue(files,name,record['start_line'],record['end_line'])
                if target['original_sha256']!=record['original_sha256']:raise HostDefect('RECOVERY_TARGET_HASH_MISMATCH')
                issued.append({'old_target_id':identifier,'new_target':target})
            archives=[]
            for item in sorted(base.glob('target_archive_*.json')):
                archived=read_verified(item)
                if archived['identity']['allowed_paths']!=sorted(task['allowed_paths']) or archived['identity']['binding']!=task['sha256']:
                    raise HostDefect('RECOVERY_TARGET_ARCHIVE_SCOPE_MISMATCH')
                EditTargets.replay(item,task['allowed_paths'],task['sha256'],archived['identity']['namespace'])
                archives.append({'name':item.name,'sha256':sha(item),'namespace':archived['identity']['namespace']})
            value={'version':1,'authority':'host','archives':archives,'binding':task['sha256'],
                'allowed_paths':task['allowed_paths'],'issued':issued,'deferred_staged_targets':deferred}
            write_verified(base/'target_recovery.json',value)
            receipts.append({'registry':str(registry.relative_to(child.path)),
                'transfer':str((base/'target_recovery.json').relative_to(child.path)),
                'transfer_sha256':sha(base/'target_recovery.json'),'issued':len(issued),'deferred':len(deferred)})
    return receipts


def verify_saved(run, *, allow_failed=False):
    """Use existing validators on a private copy, never mutate the parent."""
    from .phase_workflow import PhaseWorker, digest
    from .mission_controller import MissionController
    from .resolution import read_verified
    from .procedural_memory import ProcedureStore
    MissionController(run).assert_immutable()
    selection = read_verified(run.path/'workflow_selection.json')
    version = selection['research_handoff_version']
    if version not in (1, 2):
        raise HostDefect('RECOVERY_VERSION_MISMATCH: unsupported saved generation')
    plan = load(run.path/'plan.json')
    contracts = {}
    boundaries = []
    for task in plan['tasks']:
        body = dict(task); expected = body.pop('sha256')
        if digest(body) != expected:
            raise HostDefect('RECOVERY_CONTRACT_MISMATCH: contract hash')
        if task.get('phase_policy', {}).get('version') != version:
            raise HostDefect('RECOVERY_VERSION_MISMATCH: selection/contract')
        record = load(run.path/'tasks'/f"{task['task_id']}.json")
        if record['task'] != task or len(record['attempts']) > task['max_attempts']:
            raise HostDefect('RECOVERY_CONTRACT_MISMATCH: task journal')
        contracts[task['task_id']] = expected
        for attempt in record['attempts']:
            repair=None
            if (run.path/'artifacts/local_repair_receipt.json').exists():repair=read_verified(run.path/'artifacts/local_repair_receipt.json')
            if attempt.get('inherited_checkpoint'):
                old=(repair or {}).get('checkpoint_attempts',{}).get(str(attempt['attempt']))
                expected=f"checkpoints/{task['task_id']}/{old}"
                if not old or attempt['inherited_checkpoint']!=expected:raise HostDefect('REPAIR_CHECKPOINT_BINDING_MISMATCH')
                base=run.path/expected
            else:base=run.path/'checkpoints'/task['task_id']/str(attempt['attempt'])
            if not base.exists():
                if attempt['state'] == 'STARTED':
                    boundaries.append({'task':task['task_id'], 'attempt':attempt['attempt'],
                                       'boundary':'before_worker_dispatch'})
                    continue
                raise HostDefect('RECOVERY_CHECKPOINT_MISSING: task has no saved phase state')
            archived_failure=repair and repair['task_id']==task['task_id'] and attempt['attempt']<repair['new_attempt']
            if attempt['state'] == 'FAILED' and not allow_failed and not archived_failure:
                raise HostDefect('RECOVERY_RETRY_EXHAUSTED: failed Worker attempt cannot be reset')
            # This is the production reader. It verifies input identity/source,
            # frozen observations, policy and knowledge; no adapter is called.
            phase = PhaseWorker(run, task, None, attempt['attempt'], None)
            completed = []
            for receipt in sorted(base.glob('*.receipt.json')):
                name = receipt.name[:-len('.receipt.json')]
                phase.cached(name)
                completed.append(name)
            for packet in sorted((base/'packets').glob('*.json')):
                value = load(packet)
                if digest(value['payload']) != value['sha256']:
                    raise HostDefect('RECOVERY_PACKET_MISMATCH: pinned input changed')
            for reference in sorted((base/'reference_handoff/checkpoints').glob('*.json')):
                if not reference.name.endswith('.receipt.json'):
                    phase.handoff.resume(reference.stem)
            if phase.working:
                for path in (base/'working_history').glob('*.json'):
                    content = load(path)
                    from .working_context import encoded
                    if hashlib.sha256(encoded(content)).hexdigest() != path.stem:
                        raise HostDefect('RECOVERY_HISTORY_MISMATCH: persistent content hash')
            boundaries.append({'task':task['task_id'], 'attempt':attempt['attempt'],
                'completed_checkpoints':completed,
                'research_complete':phase.cached('research_manifest') is not None,
                'boundary':'replay_confirmed_checkpoints_then_next_unfinished_call',
                'calls_consumed':len(list((base/'calls').glob('*/*.json')))})
    if run.state().get('procedure_policy'):
        ProcedureStore(run.path.parent.parent).select(run.state()['profile'],
            run.state()['procedure_policy']['references'])
    binding = load(run.path/'search_binding.json', None)
    if binding:
        if sha(Path(binding['database'])) != binding['database_sha256']:
            raise HostDefect('RECOVERY_DATABASE_MISMATCH: fixed snapshot changed')
        if any(sha(Path(binding['source'])/name) != value
               for name,value in binding['source_hashes'].items()):
            raise HostDefect('RECOVERY_SOURCE_MISMATCH: search origin changed')
        if not Path(binding['runtime']).is_dir():
            raise HostDefect('RECOVERY_ENVIRONMENT_MISMATCH: private runtime missing')
    return {'workflow_version':version, 'contracts':contracts, 'boundaries':boundaries}


def recover_mission(parent, *, repair_calls=None):
    """Create one recovery successor through the ordinary Host lifecycle.

    Cloud-role runs cannot recover until a shared role-call ledger is supported;
    fail closed rather than resetting costs or copying an external approval.
    Existing local advisor ledgers retain their mission identity, but recovery
    requires a local-only saved policy and never grants cloud permission.
    """
    parent = RunStore(parent.path)
    home = parent.path.parent.parent
    if parent.path.parent != (home/'runs').resolve():
        raise HostDefect('RECOVERY_UNSAFE_PATH: direct run required')
    directory = home/'recoveries';directory.mkdir(exist_ok=True)
    with (directory/'host.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        claim = directory/(parent.run_id+'.json')
        if claim.exists():
            raise HostDefect('RECOVERY_SUCCESSOR_EXISTS: use saved child '+load(claim)['child_run_id'])
        # Read-only lock: preserve every byte and timestamp of parent files.
        with (parent.path/'controller.lock').open('rb') as handle:
            try:fcntl.flock(handle, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise HostDefect('RECOVERY_PARENT_ACTIVE: controller lock held')
            state = parent.state()
            if state['status'] not in (('STALLED','NEEDS_CAPABILITY') if repair_calls is not None else ('STALLED',)) or state.get('orchestration_version') != 'pitchi-profiles-v1':
                raise HostDefect('RECOVERY_UNSUPPORTED_STATE: saved profile STALLED run required')
            if state.get('active_processes'):
                raise HostDefect('RECOVERY_PARENT_ACTIVE: registered processes remain')
            if (parent.path/'cancel_request.json').exists():
                raise HostDefect('RECOVERY_CANCELLED: cancellation requires a fresh human-directed mission')
            if (state.get('llm_config',{}).get('provider','local') != 'local' or
                    state.get('resolution_policy',{}).get('cloud')):
                raise HostDefect('RECOVERY_EXTERNAL_POLICY: cloud recovery requires separately renewed policy')
            if not (parent.path/'plan.json').is_file():
                raise HostDefect('RECOVERY_CHECKPOINT_MISSING: no confirmed plan')
            repair=None
            if repair_calls is None:assert_recoverable_journal(parent)
            else:
                if type(repair_calls) is not int or not 1<=repair_calls<=24:
                    raise HostDefect('REPAIR_BUDGET_INVALID: explicit local grant must be 1..24 calls')
                plan=load(parent.path/'plan.json')
                candidates=[]
                for task in plan['tasks']:
                    record=load(parent.path/'tasks'/f"{task['task_id']}.json")
                    if record['attempts'] and record['attempts'][-1]['state']=='FAILED':
                        candidates.append((task,record))
                if len(candidates)!=1:raise HostDefect('REPAIR_BOUNDARY_INVALID: one failed phased task required')
                task,record=candidates[0];old=record['attempts'][-1]['attempt'];next_attempt=old+1
                if next_attempt>task['max_attempts']:raise HostDefect('REPAIR_TASK_ATTEMPTS_EXHAUSTED')
                phase=state.get('current_phase','')
                import re
                match=re.fullmatch('edit_([0-9]+)',phase)
                if not match or task.get('phase_policy',{}).get('version')!=2:
                    raise HostDefect('REPAIR_BOUNDARY_INVALID: confirmed v2 edit failure required')
                from .resolution import read_verified
                inherited=read_verified(parent.path/'artifacts/local_repair_receipt.json') if (parent.path/'artifacts/local_repair_receipt.json').exists() else None
                checkpoint_attempt=inherited['checkpoint_attempt'] if inherited and inherited['task_id']==task['task_id'] else old
                base=parent.path/'checkpoints'/task['task_id']/str(checkpoint_attempt)
                slot=int(match.group(1))
                calls=sorted((base/'calls'/phase).glob('*.json'))
                if ((base/(phase+'.json')).exists() or len(calls)!=task['phase_policy']['call_attempts'] or
                    any(load(p)['status']!='FAILED' for p in calls) or
                    any(load(p).get('failure_class') not in {'PHASE_OUTPUT_INVALID','PHASE_OUTPUT_LIMIT','PHASE_CONTRACT_UNSATISFIED','CORRECTION_NO_PROGRESS'} for p in calls)):
                    raise HostDefect('REPAIR_BOUNDARY_INVALID: unconfirmed or non-correctable failure')
                if slot+1>=task['phase_policy']['max_edit_calls']:raise HostDefect('REPAIR_EDIT_OPERATIONS_EXHAUSTED')
                if task.get('guided_edits'):
                    from .resolution import read_verified
                    if read_verified(parent.path/'guided_edit_plan.json')!=task['guided_edits']:
                        raise HostDefect('REPAIR_BOUNDARY_INVALID: immutable guided input required')
                elif not (base/'research_manifest.receipt.json').exists() or not (base/'hypothesis.receipt.json').exists():
                    raise HostDefect('REPAIR_BOUNDARY_INVALID: confirmed research/hypothesis required')
                repair={'task_id':task['task_id'],'contract_sha256':task['sha256'],
                    'checkpoint_attempt':checkpoint_attempt,'new_attempt':next_attempt,
                    'skipped_edit_steps':sorted(set((inherited or {}).get('skipped_edit_steps',[])+[slot])),
                    'checkpoint_attempts':{**(inherited or {}).get('checkpoint_attempts',{}),str(next_attempt):checkpoint_attempt},
                    'attempt_skip_steps':{**(inherited or {}).get('attempt_skip_steps',{}),str(next_attempt):sorted(set((inherited or {}).get('skipped_edit_steps',[])+[slot]))},
                    'failed_call_hashes':{**(inherited or {}).get('failed_call_hashes',{}),**{str(p.relative_to(parent.path)):sha(p) for p in calls}},
                    'call_limit':repair_calls,'parent_run_id':parent.run_id,'authority':'host',
                    'classification':'inherited_failed_candidate_not_validated_success'}
            saved = manifest(parent.path)
            workspace = Path(state['workspace'])
            source = manifest(workspace)
            identifier = datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
            child_path = home/'runs'/identifier
            child_workspace = home/'workspaces'/identifier
            shutil.copytree(workspace, child_workspace)
            shutil.copytree(parent.path, child_path,
                ignore=shutil.ignore_patterns('controller.lock','cancel_request.json'))
            child = RunStore(child_path)
            # All counters/configuration are inherited, not reconstructed from
            # a whitelist. Only lifecycle/process/path identity changes.
            changes = {**state, 'run_id':identifier,'workspace':str(child_workspace),
                'status':'RECOVERING','stage':'recovery','parent_run':str(parent.path),
                'created_at':now(),'active_processes':{},'recovery_pending':True}
            for key in ('terminal_record','failure_class','failure_owner','stop_reason','recovery_parent_run_id'):
                changes.pop(key,None)
            dump(child_path/'state.json',changes)
            context = load(child_path/'execution_context.json')
            context.update(active_workspace=str(child_workspace), cwd=str(child_workspace),
                           artifact_base=str(child_workspace))
            dump(child_path/'execution_context.json',context)
            binding = load(child_path/'search_binding.json',None)
            if binding:
                database = Path(binding['database'])
                if not database.resolve().is_relative_to(parent.path):
                    raise HostDefect('RECOVERY_DATABASE_MISMATCH: snapshot outside saved run')
                binding['database'] = str(child_path/database.relative_to(parent.path))
                dump(child_path/'search_binding.json',binding)
            try:
                if manifest(parent.path) != saved or manifest(workspace) != source:
                    raise HostDefect('RECOVERY_PARENT_CHANGED: parent changed during copy')
                for name,value in saved.items():
                    if name in {'state.json','execution_context.json','search_binding.json','cancel_request.json','controller.lock'}:continue
                    if sha(child_path/name) != value:
                        raise HostDefect('RECOVERY_COPY_MISMATCH: inherited file changed')
                if manifest(child_workspace) != source:
                    raise HostDefect('RECOVERY_SOURCE_MISMATCH: isolated copy changed')
                transfers=transfer_targets(parent,child)
                receipt = verify_saved(child,allow_failed=repair is not None)
                if repair:
                    from .resolution import write_verified,read_verified
                    budget=state.get('local_repair_budget')
                    if budget:
                        ledger=read_verified(home/'local_repair_budgets'/(budget+'.json'))
                        if ledger['limit']!=repair_calls:raise HostDefect('REPAIR_BUDGET_CHANGE_FORBIDDEN')
                    else:
                        budget=identifier
                        write_verified(home/'local_repair_budgets'/(budget+'.json'),
                            {'limit':repair_calls,'reservations':[],'origin_run':parent.run_id})
                    write_verified(child_path/'artifacts/local_repair_receipt.json',repair)
                    child.save_state(local_repair_budget=budget)
                    record=load(child_path/'tasks'/f"{repair['task_id']}.json")
                    record['attempts'].append({'attempt':repair['new_attempt'],'state':'STARTED',
                        'worker_output_ref':f"artifacts/{repair['task_id']}_worker_{repair['new_attempt']}.json",
                        'inherited_checkpoint':f"checkpoints/{repair['task_id']}/{repair['checkpoint_attempt']}"})
                    dump(child_path/'tasks'/f"{repair['task_id']}.json",record)
                    receipt['local_repair']=repair
                receipt.update(schema_version=1, authority='host', parent_run_id=parent.run_id,
                    child_run_id=identifier, at=now(), inherited_files=saved,
                    inherited_source_hashes=source,
                    inherited_counters={k:v for k,v in state.items() if
                        'budget' in k or 'count' in k or 'policy' in k},
                    original_current_phase=state.get('current_phase'),
                    replay='confirmed checkpoint reads only; unfinished calls retain retry journals',
                    root_acceptance_unchanged=True,target_transfers=transfers,
                    pending_dispatches=pending_context_dispatch(parent))
                dump(child_path/'artifacts/recovery_receipt.json',receipt)
                dump(claim,{'child_run_id':identifier,'receipt_sha256':sha(child_path/'artifacts/recovery_receipt.json')})
                child.save_state(status='READY',recovery_pending=False,recovery_parent_run_id=parent.run_id)
                child.event('host','recovery_created',parent_run_id=parent.run_id,
                            receipt='artifacts/recovery_receipt.json')
            except Exception as exc:
                child.save_state(status='STALLED',failure_class='RECOVERY_INTEGRITY_DEFECT',
                                 stop_reason=str(exc),failure_owner='host')
                dump(claim,{'child_run_id':identifier,'failed_recovery':True})
                raise
            return child
