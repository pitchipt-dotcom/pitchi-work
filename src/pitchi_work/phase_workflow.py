"""Host-owned, bounded development phases. Phase completion is NOT task PASS.

No generated commands, validators, contracts or completion conditions accepted.
Only uncompleted calls are retried; complete evidence and staged edits are hashed.
"""
import hashlib
import json
import re
from pathlib import Path
from .model import ModelFailure
from .runner import HostDefect
from .store import dump, load, now
from .mission_profiles import visible, sha

POLICY = {'version':1, 'phases':['research','hypothesis','edit','host_validation'],
          'source_page_bytes':3500, 'max_source_pages':64, 'max_edit_calls':16,
          'max_response_bytes':1800, 'max_replacement_bytes':900,
          'max_output_tokens':2000, 'call_attempts':2}

# Opt-in Host policy. Old pinned contracts keep the exact version-1 dictionary.
REFERENCE_POLICY = {**POLICY, 'version':2, 'research_handoff_version':2,
                    'max_research_handoff_calls':4, 'max_hypothesis_handoff_calls':8,
                    'context_tokens':16384}

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def install_edit_steps(run,path):
    """Explicit instructional input, never historical checkpoint migration."""
    from .resolution import write_verified
    value=load(path)
    if (run.path/'guided_edit_plan.json').exists():
        from .resolution import read_verified
        if read_verified(run.path/'guided_edit_plan.json')!=value:raise HostDefect('Guided plan is immutable')
        return  # Never reset reservations on a repeated setup.
    if set(value)!={'schema_version','classification','source_hashes','steps','parent_run_id','max_local_calls'} or value['schema_version']!=1 or value['classification']!='CODEX_DESIGNED_STEPS':
        raise ValueError('Explicit guided plan schema required')
    if type(value['max_local_calls']) is not int or not 1<=value['max_local_calls']<=24:
        raise ValueError('Explicit local grant must be 1..24 calls')
    steps=value['steps'];files={}
    if not isinstance(steps,list) or not 1<=len(steps)<=POLICY['max_edit_calls']//2:
        raise ValueError('Target issuance plus replacements exceed unchanged operation limit')
    for name,expected in value['source_hashes'].items():
        if not visible(name) or Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('Unsafe guided path')
        source=Path(run.state()['workspace'])/name
        if source.is_symlink() or not source.is_file() or sha(source)!=expected:raise ValueError('Guided source hash mismatch')
        files[name]=source.read_text()
    for i,step in enumerate(steps):
        if set(step)!={'id','path','original_text','instruction','new_text','local_check'} or step['id']!='S'+str(i+1):raise ValueError('Ordered explicit step fields required')
        if step['path'] not in files or not all(isinstance(step[k],str) and step[k] for k in ('original_text','instruction','new_text','local_check')):
            raise ValueError('Guided step outside scope or missing instruction')
        if len(step['new_text'].encode())>POLICY['max_replacement_bytes']:raise ValueError('Replacement exceeds unchanged bound')
        text=files[step['path']];old=step['original_text']
        if text.count(old)!=1 or not old.endswith('\n') or not step['new_text'].endswith('\n'):
            raise ValueError('Target must be unique complete lines with preserved boundaries')
        a=text.index(old)
        if a and text[a-1]!='\n':raise ValueError('Target begins inside a line')
        if len(old.splitlines())>81:raise ValueError('Target exceeds unchanged range limit')
        files[step['path']]=text.replace(old,step['new_text'],1)
    write_verified(run.path/'guided_edit_plan.json',value)
    write_verified(run.path.parent.parent/'local_repair_budgets'/(run.run_id+'.json'),
                   {'limit':value['max_local_calls'],'reservations':[],'origin_run':run.run_id})
    run.save_state(local_repair_budget=run.run_id,classification='CODEX_DESIGNED_STEPS',related_run_id=value['parent_run_id'])
    dump(run.path/'artifacts/guided_budget_receipt.json',{'target_issuance':len(steps),'replacement_operations':len(steps),
        'maximum_operations':POLICY['max_edit_calls'],'maximum_local_calls':value['max_local_calls'],
        'reserved_retry_operation_slots':POLICY['max_edit_calls']-2*len(steps),
        'host_local_checks':len(steps),'verification_charged_as_llm_operation':False,
        'ordinary_role_calls_minimum':4,'ordinary_role_calls_with_retry_maximum':8,
        'call_attempts':POLICY['call_attempts'],'source':'explicit new instructional mission; old budgets unchanged',
        'plan_sha256':sha(run.path/'guided_edit_plan.json')})

def phase_operation(payload,feedback=None,previous=None):
    correction=previous is not None and bool((feedback or {}).get('violations'))
    operation={'kind':'contract_correction' if correction else 'phase_execution',
        'instructions':'Do not rethink the phase or repeat the full output. input.rules describes the final merged object, NOT your correction response. Return only corrections for Host-declared violations. Host merges and revalidates; preserve all other fields. Byte limits count UTF-8 encoded bytes, NOT characters. Shorten substantially below the limit.' if correction else 'Execute the bounded phase contract',
        'response_schema':{'corrections':[{'field':'exact field from violations','value':'corrected value'}]} if correction else None}
    if not correction and payload.get('trial_review',{}).get('required'):
        operation={'kind':'candidate_review','instructions':'Compare Host trial receipts. Maintain: return only action=reconsider, decision=maintain, test_reference. Host retains proposal. Revise/reject: include new hypothesis and paths. Review is not acceptance.','response_schema':None}
    if (feedback or {}).get('scope_violation'):
        operation={'kind':'reference_resolution','instructions':
            'Resolve the reference permission failure. Discovery is NOT permission. '
            'Use feedback.allowed_evidence_ids for a final response, or request a needed '
            'retrievable_rejected_id using feedback.response_examples.retrieval. '
            'Do NOT resend the previous invalid reference list. Host authorizes after retrieval.',
            'response_schema':None}
    return operation

class PhaseContractError(ValueError):
    def __init__(self, field, message, *, violation='contract_violation', limit=None,
                 actual=None, unit=None, violations=None):
        super().__init__(message)
        self.detail={'field':field,'required':message,'violations':violations or [
            {'field':field,'violation':violation,'limit':limit,'actual':actual,
             'unit':unit,'required_action':message}]}

class PhaseOutputLimit(ValueError):
    failure_class='PHASE_OUTPUT_LIMIT'

class CorrectionNoProgress(PhaseContractError):
    failure_class='CORRECTION_NO_PROGRESS'

def repair_fields(previous, patch, path='$'):
    """A bounded retry may replace existing fields only, never invent a schema."""
    if isinstance(previous,dict) and isinstance(patch,dict):
        result=dict(previous)
        for key,value in patch.items():
            if key not in previous:raise PhaseContractError(path+'.'+key,'Repair must name an existing field')
            result[key]=repair_fields(previous[key],value,path+'.'+key)
        return result
    if isinstance(previous,list) and isinstance(patch,dict):
        result=list(previous)
        for key,value in patch.items():
            if not re.fullmatch(r'0|[1-9][0-9]*',key) or int(key)>=len(previous):
                raise PhaseContractError(path,'Repair must name an existing array index')
            i=int(key);result[i]=repair_fields(previous[i],value,path+'['+key+']')
        return result
    return patch

def field_tokens(field):
    if not isinstance(field,str) or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\[[0-9]+\]|\.[A-Za-z_][A-Za-z_0-9]*)*',field) is None:
        raise PhaseContractError('corrections','Use an exact Host-declared field path')
    return [int(index) if index else key for key,index in re.findall(r'([A-Za-z_][A-Za-z_0-9]*)|\[([0-9]+)\]',field)]

def field_value(value,field):
    try:
        for token in field_tokens(field):value=value[token]
        return {'exists':True,'value':value}
    except (KeyError,IndexError,TypeError,ValueError):return {'exists':False}

def correct_fields(previous, response, violations, audit=None):
    """Host merges only declared violations; unchanged fields cannot be regenerated."""
    audit={} if audit is None else audit
    audit.update(ignored_fields=[],changed_fields=[],measurements=[])
    allowed={v['field'] for v in violations}
    if set(response)!={'corrections'} or not isinstance(response['corrections'],list) or not response['corrections']:
        raise PhaseContractError('corrections','Return only {corrections:[{field:Host-declared field,value:corrected value}]}')
    result=json.loads(json.dumps(previous));seen=set()
    for item in response['corrections']:
        if not isinstance(item,dict) or set(item)!={'field','value'} or not isinstance(item['field'],str):
            raise PhaseContractError('corrections','Return {field:string,value:corrected value} objects')
        if item['field'] not in allowed:
            audit['ignored_fields'].append(item['field']);continue
        if item['field'] in seen:
            raise PhaseContractError('corrections','Correct each Host-declared field at most once')
        seen.add(item['field']);tokens=field_tokens(item['field']);parent=result
        try:
            for token in tokens[:-1]:parent=parent[token]
            last=tokens[-1]
            if isinstance(parent,list):
                if type(last) is not int or not 0<=last<len(parent):raise IndexError()
            elif not isinstance(parent,dict) or not isinstance(last,str):raise TypeError()
            parent[last]=item['value']
        except (KeyError,IndexError,TypeError):
            raise PhaseContractError('corrections','Parent field must already exist; missing leaf fields may be added only when declared by Host')
        old=field_value(previous,item['field'])
        if not old['exists'] or digest(old['value'])!=digest(item['value']):audit['changed_fields'].append(item['field'])
        audit['measurements'].append({'field':item['field'],
            'previous_utf8_bytes':len(old['value'].encode()) if old['exists'] and isinstance(old['value'],str) else None,
            'corrected_utf8_bytes':len(item['value'].encode()) if isinstance(item['value'],str) else None})
    if not audit['changed_fields']:
        raise CorrectionNoProgress('corrections','No Host-declared value changed; shorten or correct the declared fields rather than resending identical values')
    return result

def policy_for(profile, byte_count, requirement_count, *, research_handoff_version=1):
    if type(research_handoff_version) is not int or research_handoff_version not in {1,2}:
        raise HostDefect('PHASE_POLICY_DEFECT: unsupported handoff version')
    if research_handoff_version==2 and profile=='code_investigation':
        raise HostDefect('PHASE_POLICY_DEFECT: read-only final report remains on legacy schema')
    if profile in {'code_change','bug_fix','code_investigation','php_search_quality'} and (
            byte_count>6000 or requirement_count>1 or profile=='php_search_quality'):
        return dict(REFERENCE_POLICY if research_handoff_version==2 else POLICY)
    return None

def pages(text, limit, line_overhead=0):
    """Line-preserving pages; never truncate a source line or pretend completeness."""
    result=[];lines=[];start=1;size=0
    for i,line in enumerate(text.splitlines(True),1):
        n=len(line.encode())+line_overhead
        if n>limit:
            if lines:
                result.append({'start_line':start,'text':''.join(lines)});lines=[];size=0
            # Minified/generated lines are explicit fragments, not omitted code.
            part='';offset=0
            for character in line:
                if len((part+character).encode())>limit-line_overhead:
                    result.append({'start_line':i,'text':part,'partial_line':True,'column':offset})
                    offset+=len(part);part=''
                part+=character
            if part:result.append({'start_line':i,'text':part,'partial_line':True,'column':offset})
            start=i+1;continue
        if lines and size+n>limit:
            result.append({'start_line':start,'text':''.join(lines)});lines=[];start=i;size=0
        lines.append(line);size+=n
    if lines or not result:result.append({'start_line':start,'text':''.join(lines)})
    return result

class PhaseWorker:
    def __init__(self,run,contract,adapter,attempt,feedback=None):
        self.run=run;self.contract=contract;self.adapter=adapter;self.policy=contract['phase_policy']
        check=dict(contract);expected=check.pop('sha256')
        if digest(check)!=expected:raise HostDefect('PHASE_CONTRACT_DEFECT: formal contract hash changed')
        if self.policy not in (POLICY,REFERENCE_POLICY):raise HostDefect('PHASE_POLICY_DEFECT: unknown policy')
        if self.policy==REFERENCE_POLICY and not contract['allowed_paths']:
            raise HostDefect('PHASE_POLICY_DEFECT: reference workflow requires an editing contract')
        if re.fullmatch(r'P[1-9][0-9]*',contract['task_id']) is None or type(attempt) is not int or not 1<=attempt<=contract['max_attempts']:
            raise HostDefect('PHASE_CHECKPOINT_DEFECT: unsafe checkpoint identity')
        self.base=run.path/'checkpoints'/contract['task_id']/str(attempt)
        self.repair_skipped_steps=[]
        if (run.path/'artifacts/local_repair_receipt.json').exists():
            from .resolution import read_verified
            repair=read_verified(run.path/'artifacts/local_repair_receipt.json')
            inherited_attempt=repair.get('checkpoint_attempts',{}).get(str(attempt))
            if repair['task_id']==contract['task_id'] and inherited_attempt is not None:
                if repair['contract_sha256']!=contract['sha256'] or not 1<=inherited_attempt<attempt:
                    raise HostDefect('REPAIR_CHECKPOINT_BINDING_MISMATCH')
                self.base=run.path/'checkpoints'/contract['task_id']/str(inherited_attempt)
                for ref,expected in repair['failed_call_hashes'].items():
                    p=run.path/ref
                    if not p.resolve().is_relative_to(self.base.resolve()) or p.is_symlink() or sha(p)!=expected:
                        raise HostDefect('REPAIR_FAILURE_HISTORY_CHANGED')
                self.repair_skipped_steps=repair['attempt_skip_steps'][str(attempt)]
        self.base.mkdir(parents=True,exist_ok=True)
        self.handoff=None
        if self.policy==REFERENCE_POLICY:
            from .research_handoff import HandoffStore
            self.handoff=HandoffStore(self.base/'reference_handoff')
        self.feedback=feedback or []
        self.working=None
        self.reference_protocol=False
        self.hypothesis_protocol=False
        self.hypothesis_grounding=False
        self.edit_context=False
        self.edit_protocol=False
        self.edit_protocol_version=0
        self.edit_targets=None
        self.source_navigation=False
        self.staged_candidate_gate=False
        self.serialized_source_pages=False
        self.retry_context=False
        self.retry_previous_ref=None
        self.material_view=None
        self.execution_navigation=False
        self.reconsideration=False
        self.trial_review_protocol=False
        self.trial_review_version=0
        self.reasoning_modes={}
        # Explicit injection for mock-only local edit takeover. Default stays OFF.
        self.edit_takeover=None
        if self.handoff is not None:
            from .working_context import WorkingContext
            marker=self.base/'working_context_policy.json'
            # Historical pinned inputs must retain their original construction.
            if marker.exists():
                if load(marker)!={'version':WorkingContext.VERSION}:
                    raise HostDefect('PHASE_CONTEXT_DEFECT: unknown working context policy')
                self.working=WorkingContext(self.base/'working_history',run.path)
            elif not any((self.base/'packets').glob('*.json')):
                dump(marker,{'version':WorkingContext.VERSION})
                self.working=WorkingContext(self.base/'working_history',run.path)
            if self.working is not None:
                observations=self.base/'working_observations.json'
                if observations.exists():
                    saved_feedback=self.working.read(load(observations))
                    if feedback is not None and saved_feedback!=feedback:
                        raise HostDefect('PHASE_CONTEXT_DEFECT: frozen observations changed')
                    self.feedback=saved_feedback
                else:
                    dump(observations,self.working.persist(self.feedback))
                marker=self.base/'reference_protocol.json'
                if marker.exists():
                    if load(marker)!={'version':1}:raise HostDefect('PHASE_POLICY_DEFECT: unknown reference protocol')
                    self.reference_protocol=True
                elif not any((self.base/'packets').glob('*.json')):
                    dump(marker,{'version':1});self.reference_protocol=True
                marker=self.base/'hypothesis_protocol.json'
                if marker.exists():
                    if load(marker)!={'version':1}:raise HostDefect('PHASE_POLICY_DEFECT: unknown hypothesis protocol')
                    self.hypothesis_protocol=True
                elif not any((self.base/'packets').glob('*.json')):
                    dump(marker,{'version':1});self.hypothesis_protocol=True
                marker=self.base/'hypothesis_grounding.json'
                if marker.exists():
                    if load(marker)!={'version':1}:raise HostDefect('PHASE_POLICY_DEFECT: unknown hypothesis grounding')
                    self.hypothesis_grounding=True
                elif not any((self.base/'packets').glob('*.json')):
                    dump(marker,{'version':1});self.hypothesis_grounding=True
                marker=self.base/'edit_context.json'
                if marker.exists():
                    if load(marker)!={'version':1}:raise HostDefect('PHASE_POLICY_DEFECT: unknown edit context')
                    self.edit_context=True
                elif not any((self.base/'packets').glob('*.json')):
                    dump(marker,{'version':1});self.edit_context=True
                marker=self.base/'edit_protocol.json'
                if marker.exists():
                    if load(marker) not in ({'version':1},{'version':2}):raise HostDefect('PHASE_POLICY_DEFECT: unknown edit protocol')
                    self.edit_protocol=True
                    self.edit_protocol_version=load(marker)['version']
                elif not any((self.base/'packets').glob('*.json')):
                    dump(marker,{'version':2});self.edit_protocol=True;self.edit_protocol_version=2
        workspace=Path(run.state()['workspace'])
        if self.edit_protocol:
            from .edit_targets import EditTargets
            marker=self.base/'edit_targets_policy.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CHECKPOINT_DEFECT: unknown edit target policy')
                self.edit_targets=EditTargets(self.base/'edit_targets.json',contract['allowed_paths'],contract['sha256'])
                transfer=self.base/'target_recovery.json'
                if transfer.exists():
                    from .resolution import read_verified
                    value=read_verified(transfer)
                    if (value['binding']!=contract['sha256'] or value['allowed_paths']!=contract['allowed_paths']):
                        raise HostDefect('RECOVERY_TARGET_BINDING_MISMATCH')
                    from .edit_targets import HistoricalTargets
                    readers=[]
                    for item in value['archives']:
                        archive=self.base/item['name']
                        if archive.parent!=self.base or not item['name'].startswith('target_archive_') or sha(archive)!=item['sha256']:
                            raise HostDefect('RECOVERY_TARGET_ARCHIVE_MISMATCH')
                        readers.append(EditTargets.replay(archive,contract['allowed_paths'],contract['sha256'],item['namespace']))
                    self.live_edit_targets=self.edit_targets
                    self.edit_targets=HistoricalTargets(readers)
            elif not list((self.base/'packets').glob('edit_*.json')):
                from .edit_targets import EditTargets
                dump(marker,{'version':1})
                self.edit_targets=EditTargets(self.base/'edit_targets.json',contract['allowed_paths'],contract['sha256'])
            marker=self.base/'trial_review_protocol.json'
            if marker.exists():
                if load(marker) not in ({'version':1},{'version':2}):raise HostDefect('PHASE_CHECKPOINT_DEFECT: trial review protocol version')
                self.trial_review_protocol=True
                self.trial_review_version=load(marker)['version']
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':2});self.trial_review_protocol=True;self.trial_review_version=2
            marker=self.base/'reconsideration.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CHECKPOINT_DEFECT: reconsideration version')
                self.reconsideration=True
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':1});self.reconsideration=True
            marker=self.base/'execution_navigation.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CHECKPOINT_DEFECT: execution navigation version')
                self.execution_navigation=True
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':1});self.execution_navigation=True
            marker=self.base/'retry_context.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CONTEXT_DEFECT: unknown retry context protocol')
                self.retry_context=True
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':1});self.retry_context=True
            marker=self.base/'source_page_serialization.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CHECKPOINT_DEFECT: source page serialization version')
                self.serialized_source_pages=True
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':1});self.serialized_source_pages=True
            marker=self.base/'source_navigation.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CHECKPOINT_DEFECT: source navigation version')
                self.source_navigation=True
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':1});self.source_navigation=True
            marker=self.base/'staged_candidate_gate.json'
            if marker.exists():
                if load(marker)!={'version':1}:raise HostDefect('PHASE_CHECKPOINT_DEFECT: staged candidate gate version')
                self.staged_candidate_gate=True
            elif not list((self.base/'packets').glob('edit_*.json')):
                dump(marker,{'version':1});self.staged_candidate_gate=True
        for stage in ('hypothesis','edit'):
            marker=self.base/('reasoning_'+stage+'.json')
            if marker.exists():
                mode=load(marker)
                if set(mode)!={'version','enabled'} or mode['version']!=1 or type(mode['enabled']) is not bool:
                    raise HostDefect('PHASE_CHECKPOINT_DEFECT: bounded reasoning policy')
            else:
                enabled=run.state().get('worker_reasoning',False)
                if type(enabled) is not bool:raise HostDefect('PHASE_POLICY_DEFECT: reasoning mode must be boolean')
                mode={'version':1,'enabled':enabled and not list((self.base/'packets').glob(stage+'_*.json'))}
                dump(marker,mode)
            self.reasoning_modes[stage]=mode['enabled']
        names=sorted(set(contract['input_files']+contract['allowed_paths']+contract.get('research_inputs',[])))
        current={}
        for name in names:
            p=workspace/name
            if not visible(name) or p.is_symlink() or not p.resolve().is_relative_to(workspace.resolve()):
                raise HostDefect('PHASE_INPUT_DEFECT: unsafe source')
            if p.exists(): current[name]=p.read_text()
        identity={'contract':contract['sha256'],'root_goal':sha(run.path/'root_goal.json'),
                  'instruction':sha(run.path/'instruction.md')}
        path=self.base/'input.json'
        if path.exists():
            saved=load(path)
            if saved['identity']!=identity or digest(saved)!=load(self.base/'input_hash.json'):
                raise HostDefect('PHASE_CHECKPOINT_DEFECT: input identity/hash changed')
            self.files=saved['files']
            result=self.cached('output')
            allowed=self.contract['allowed_paths']
            # Publication may have been interrupted after some file writes.
            # Permit only exact original or exact staged-final bytes, never a third version.
            for name in set(current)|set(self.files):
                old=self.files.get(name);actual=current.get(name)
                final=result['files'].get(name,old) if result and name in allowed else old
                if actual!=old and actual!=final:
                    raise HostDefect('PHASE_CHECKPOINT_DEFECT: workspace source changed')
        else:
            self.files=current;dump(path,{'identity':identity,'files':current})
            dump(self.base/'input_hash.json',digest(load(path)))
        initial=set(contract['input_files']+contract['existing_tests'])
        self.source_pages=[{'path':name,'sha256':hashlib.sha256(text.encode()).hexdigest(),**p}
                           for name,text in self.files.items() if name in initial
                           for p in pages(text,self.policy['source_page_bytes'],line_overhead=60)]
        if len(self.source_pages)>self.policy['max_source_pages']:
            raise HostDefect('PHASE_INPUT_DEFECT: source page limit reached; no truncation')

    def cached(self,name):
        path=self.base/(name+'.json');receipt=self.base/(name+'.receipt.json')
        if not path.exists():return None
        if not receipt.exists() or sha(path)!=load(receipt)['sha256']:
            raise HostDefect('PHASE_CHECKPOINT_DEFECT: phase evidence hash mismatch')
        value=load(path)
        def verify(obj):
            if isinstance(obj,dict):
                if obj.get('research_handoff_version')==2 and 'checkpoint' in obj:
                    if self.handoff is None:raise HostDefect('PHASE_CHECKPOINT_DEFECT: handoff policy mismatch')
                    self.handoff.resume(obj['checkpoint'])
                if 'reference' in obj and 'sha256' in obj:
                    target=(self.run.path/obj['reference']).resolve()
                    if not target.is_relative_to(self.run.path.resolve()) or not target.is_file() or sha(target)!=obj['sha256']:
                        raise HostDefect('PHASE_CHECKPOINT_DEFECT: linked evidence changed')
                    if target.parent==self.base and target!=path:self.cached(target.stem)
                for item in obj.values():verify(item)
            elif isinstance(obj,list):
                for item in obj:verify(item)
        verify(value)
        return value

    def save(self,name,value):
        if self.cached(name) is not None:raise HostDefect('PHASE_CHECKPOINT_DEFECT: completed phase is immutable')
        path=self.base/(name+'.json');dump(path,value)
        dump(self.base/(name+'.receipt.json'),{'sha256':sha(path),'at':now(),
             'contract_sha256':self.contract['sha256'],'authority':'host','is_task_pass':False})
        self.run.event('worker','phase_checkpoint',phase=name,reference=str(path.relative_to(self.run.path)),sha256=sha(path))

    def reference(self,name):
        if self.cached(name) is None:raise HostDefect('PHASE_CHECKPOINT_DEFECT: missing evidence')
        p=self.base/(name+'.json')
        return {'reference':str(p.relative_to(self.run.path)),'sha256':sha(p)}

    def call(self,name,payload,check):
        packet=self.base/'packets'/(name+'.json')
        if not packet.exists() and hasattr(self,'live_edit_targets'):
            self.edit_targets=self.live_edit_targets
            del self.live_edit_targets
            view=payload.get('source_view') or {}
            if view.get('edit_target'):
                old=view['edit_target']
                current=self.edit_targets.issue(getattr(self,'current_staged_files',self.files),
                    old['path'],old['start_line'],old['end_line'])
                if any(current[k]!=old[k] for k in current if k!='target_id'):
                    raise HostDefect('RECOVERY_TARGET_REISSUE_MISMATCH')
                payload={**payload,'source_view':{**view,'edit_target':current}}
        if packet.exists() and hasattr(self,'live_edit_targets'):
            view=payload.get('source_view');saved_view=load(packet)['payload'].get('source_view')
            if isinstance(view,dict) and isinstance(saved_view,dict) and saved_view.get('edit_target'):
                old=saved_view['edit_target'];verified=self.edit_targets.target(old['target_id'])
                if verified!=old or any(view.get('edit_target',{}).get(k)!=old[k]
                        for k in ('path','source_sha256','original_sha256','start_line','end_line')):
                    raise HostDefect('RECOVERY_TARGET_PACKET_MISMATCH')
                payload={**payload,'source_view':{**view,'edit_target':verified}}
        composed = self.handoff is not None and self.working is not None and (
            not packet.exists() or load(packet)['payload'].get('working_context_composition')==1)
        if self.run.state().get('procedure_policy') and name.startswith(('hypothesis','edit_')):
            from .procedural_memory import pinned_procedures
            payload={**payload,'abstract_procedures':pinned_procedures(self.run,self.contract,self.base)}
        if composed and name.startswith('edit_') and self.edit_targets is not None and (
                not packet.exists() or load(packet)['payload'].get('edit_dispatch_version')==1):
            from .edit_protocol import target_dispatch
            payload={**target_dispatch(payload,getattr(self,'current_staged_files',self.files),self.edit_targets),'edit_dispatch_version':1}
            if packet.exists():
                # Replay the instruction that produced the pinned working set.
                # Live unfinished dispatch is upgraded only after hash checks.
                payload['rules']=load(packet)['payload']['rules']
        if self.working is not None:
            metadata={'working_context_version':self.working.VERSION,
                'history_access':'Request {action:observations,page:integer} for exact Host observation pages; no history was deleted.'}
            if composed:
                payload={**payload,**metadata,'working_context_composition':1}
                if name.startswith(('hypothesis','edit_')) and (not packet.exists() or
                        load(packet)['payload'].get('material_protocol_version') in (1,2,3)):
                    payload['material_protocol_version']=(load(packet)['payload']['material_protocol_version'] if packet.exists() else 3)
                    payload['material_protocol']='Deferred input cards are exact Host snapshots. Request {action:context_material,context_id:Host-issued card ID,page:integer}; respect page_count. No material or mission condition was discarded.'
                    payload['requested_material']=self.material_view
                if not packet.exists() or load(packet)['payload'].get('context_catalog_version')==1:
                    payload['context_catalog_version']=1
                if not packet.exists() or load(packet)['payload'].get('context_navigation_version')==1:
                    payload['context_navigation_version']=1
                reserve=None
                # New calls reserve their actual envelope in every phase, not
                # only Edit. The legacy 1800-byte estimate can reject a Research
                # body that fits the independently measured full-dispatch guard.
                # Pin this choice so old packets remain exactly reproducible.
                measured_envelope=(not packet.exists() or
                    load(packet)['payload'].get('context_envelope_version')==1)
                if measured_envelope:
                    payload['context_envelope_version']=1
                if measured_envelope or (name.startswith('edit_') and 'diagnostic_access' in payload):
                    envelope={'phase':name,'operation':phase_operation(payload),
                        'input':{},'feedback':None,'previous_output':None}
                    reserve=len(json.dumps(envelope,ensure_ascii=False,separators=(',',':')).encode())-2
                payload=self.working.compose(payload,self.policy['context_tokens'],
                    self.policy['max_output_tokens'],envelope_reserve=reserve)
            elif self.retry_context and name.startswith('edit_') and 'diagnostic_access' in payload:
                # New edit calls use measured initial envelope accounting as
                # well as measured retry accounting. Do not reject a mandatory
                # set using the old feedback estimate before a retry exists.
                envelope={'phase':name,'operation':phase_operation(payload),
                    'input':{},'feedback':None,'previous_output':None}
                reserve=len(json.dumps(envelope,ensure_ascii=False,separators=(',',':')).encode())-2
                payload=self.working.select({**payload,**metadata},self.policy['context_tokens'],self.policy['max_output_tokens'],envelope_reserve=reserve)
            else:
                payload=self.working.select(payload,self.policy['context_tokens'],self.policy['max_output_tokens'])
                payload.update(metadata)
        result=self.cached(name)
        if result is not None and not packet.exists():
            raise HostDefect('PHASE_CHECKPOINT_DEFECT: completed call has no pinned input')
        if packet.exists():
            saved=load(packet)
            if saved['sha256']!=digest(saved['payload']) or saved['sha256']!=digest(payload):
                fields=sorted(k for k in set(saved['payload'])|set(payload) if saved['payload'].get(k)!=payload.get(k))
                raise HostDefect('PHASE_CHECKPOINT_DEFECT: dispatch packet changed; phase='+name+'; fields='+','.join(fields))
        else:dump(packet,{'payload':payload,'sha256':digest(payload),'authority':'host'})
        if result is not None:return result
        if name.startswith('edit_') and hasattr(self,'live_edit_targets'):
            # Historical packet/hash remains unchanged. Only the new dispatch
            # view is rebound through Host issuance against actual staged bytes.
            self.edit_targets=self.live_edit_targets
            del self.live_edit_targets
            view=payload.get('source_view')
            if isinstance(view,dict) and view.get('edit_target'):
                target=view['edit_target']
                rebound=self.edit_targets.issue(self.current_staged_files,target['path'],
                    target['start_line'],target['end_line'])
                if any(rebound[k]!=target[k] for k in ('source_sha256','original_sha256','start_line','end_line','path')):
                    raise HostDefect('RECOVERY_TARGET_REISSUE_MISMATCH')
                payload={**payload,'source_view':{**view,'edit_target':rebound}}
                self.working.persist({'operation':'target_reissued_at_dispatch',
                    'old_target':target,'new_target':rebound,'authority':'host','is_task_pass':False})
        folder=self.base/'calls'/name;folder.mkdir(parents=True,exist_ok=True)
        existing=sorted(folder.glob('*.json'));failures=[load(p) for p in existing]
        start_attempt=len(existing)+1
        recovery=load(self.run.path/'artifacts/recovery_receipt.json',{})
        for pending in recovery.get('pending_dispatches',[]):
            if pending['phase']!=name or not existing:continue
            journal=existing[-1]
            if str(journal.relative_to(self.run.path))!=pending['journal']:continue
            if sha(journal)!=pending['sha256']:continue  # Already dispatched in this child.
            proof=self.run.path/pending['guard_proof']['reference']
            if sha(proof)!=pending['guard_proof']['sha256'] or pending['model_dispatched'] is not False:
                raise HostDefect('RECOVERY_PENDING_DISPATCH_MISMATCH')
            archive=self.base/'pre_dispatch_archive'/name/journal.name
            archive.parent.mkdir(parents=True,exist_ok=True)
            dump(archive,load(journal))
            start_attempt=pending['slot']
            failures=failures[:-1]
            self.run.event('host','pending_dispatch_recovered',phase=name,slot=start_attempt,
                archive=str(archive.relative_to(self.run.path)),retry_limit_unchanged=True)
        last=failures[-1] if failures else {}
        feedback=last.get('feedback') or ({'failure_class':last.get('failure_class'),
                   'required':last.get('error','Interrupted call; return the required phase object')} if failures else None)
        previous=last.get('previous_output')
        kind=last.get('failure_class','PHASE_CONTRACT_UNSATISFIED')
        live_target_protocol=bool(composed and name.startswith('edit_') and self.edit_targets is not None)
        explicit_target=live_target_protocol and (payload.get('source_view') or {}).get('kind')=='edit_target'
        target_only=live_target_protocol and self.edit_protocol_version==2
        if live_target_protocol:
            from .edit_protocol import target_dispatch, atomic_feedback
            payload=target_dispatch(payload,getattr(self,'current_staged_files',self.files),self.edit_targets)
            feedback=atomic_feedback(feedback)
        for attempt in range(start_attempt,self.policy['call_attempts']+1):
            self.run.raise_if_cancelled();self.run.save_state(current_phase=name)
            call_path=folder/(str(attempt)+'.json')
            dump(call_path,{'status':'STARTED','at':now(),'input_sha256':digest(payload)})
            correction_audit={}
            model_dispatched=False
            try:
                violations=(feedback or {}).get('violations',[])
                correction=previous is not None and bool(violations)
                operation=phase_operation(payload,feedback,previous)
                dispatch_input=payload;shown_previous=previous
                if composed or (self.retry_context and name.startswith('edit_') and 'diagnostic_access' in payload):
                    dispatch_input,shown_previous,self.retry_previous_ref=self.working.dispatch(
                        payload,operation,feedback,previous,self.policy['context_tokens'],self.policy['max_output_tokens'],phase_name=name)
                prompt=json.dumps({'phase':name,'operation':operation,'input':dispatch_input,'feedback':feedback,
                                  'previous_output':shown_previous},ensure_ascii=False,separators=(',',':'))
                if len(prompt.encode())>15000:raise HostDefect('PHASE_CONTEXT_DEFECT: bounded packet exceeded')
                if self.handoff is not None and len(prompt.encode())+512+self.policy['max_output_tokens']>self.policy['context_tokens']*80//100:
                    raise HostDefect('PHASE_CONTEXT_DEFECT: full dispatch exceeds 80% budget')
                options={}
                if target_only and name.startswith('edit_'):
                    from .edit_protocol import visible_targets
                    self.live_target_scope=visible_targets(dispatch_input,getattr(self,'current_staged_files',self.files),self.edit_targets)
                if self.edit_protocol and name.startswith('edit_') and not correction:
                    options={'output_schema':getattr(self,'edit_schema',None)}
                    if (target_only or explicit_target) and options['output_schema']:
                        from .edit_protocol import dispatch_schema, visible_targets
                        ids=visible_targets(dispatch_input,getattr(self,'current_staged_files',self.files),self.edit_targets)
                        options['output_schema']=dispatch_schema(options['output_schema'],ids)
                    if payload.get('material_protocol_version') in (1,2,3) and options.get('output_schema'):
                        options['output_schema']={'oneOf':options['output_schema']['oneOf']+[{
                            'type':'object','properties':{'action':{'type':'string','enum':['context_material']},
                            'context_id':{'type':'string','enum':self.working.material_ids()},
                            'page':{'type':'integer','minimum':0}},
                            'required':['action','context_id','page'],'additionalProperties':False}]}
                if any(name.startswith(stage+'_') and enabled for stage,enabled in self.reasoning_modes.items()):
                    options['enable_thinking']=True
                if self.contract.get('guided_edits') and name.startswith('edit_'):
                    spent=sum(len(list(p.glob('*.json'))) for p in (self.base/'calls').glob('edit_*'))
                    issued=sum(bool(re.fullmatch(r'guided_target_[0-9]+\.json',p.name)) for p in self.base.glob('guided_target_*.json'))
                    if spent+issued>=self.policy['max_edit_calls']:
                        raise ModelFailure('PHASE_EDIT_LIMIT','Guided targets plus edit/retry calls exhausted the unchanged operation limit')
                model_dispatched=True
                raw=self.adapter.ask('worker',prompt,max_output_tokens=self.policy['max_output_tokens'],**options)
                if not isinstance(raw,dict):raise ValueError('Phase response must be an object')
                if len(json.dumps(raw,ensure_ascii=False).encode())>self.policy['max_response_bytes']:
                    raise PhaseOutputLimit('Response exceeds bounded phase size; return a smaller atomic result')
                if correction:
                    if digest(raw)==digest(previous):
                        correction_audit['unchanged_full_response']=True
                        raise CorrectionNoProgress('corrections','Identical full response; no declared field was corrected')
                    raw=correct_fields(previous,raw,violations,correction_audit)
                    if len(json.dumps(raw,ensure_ascii=False).encode())>self.policy['max_response_bytes']:
                        raise PhaseOutputLimit('Merged response exceeds bounded phase size')
                elif set(raw)=={'repair'}:
                    if previous is None or not isinstance(raw['repair'],dict):
                        raise PhaseContractError('repair','Repair requires a saved previous object and field patch')
                    raw=repair_fields(previous,raw['repair'])
                    if len(json.dumps(raw,ensure_ascii=False).encode())>self.policy['max_response_bytes']:
                        raise PhaseOutputLimit('Merged response exceeds bounded phase size')
                # Empty initial observations can be honest. Emptying a rejected
                # proposal is not a correction of its source binding.
                if (previous and previous.get('observations') and raw.get('observations')==[]):
                    raise PhaseContractError('observations','Correct the proposed source references; do not discard observations to bypass validation')
                previous=raw
                if (target_only or explicit_target) and raw.get('action') in ('replace','patch'):
                    raise ValueError('Live Host-target protocol requires replace_target or edit_target; legacy output rejected')
                if target_only and raw.get('action')=='replace_target' and raw.get('target_id') not in self.live_target_scope:
                    raise ValueError('Target body not visible in this dispatch; read source and select a smaller explicit edit_target before editing')
                if raw.get('action')=='context_material' and payload.get('material_protocol_version') in (1,2,3):
                    if set(raw)!={'action','context_id','page'}:raise ValueError('Exact context material request keys required')
                    self.working.material(raw['context_id'],raw['page']);result=raw
                else:result=check(raw)
                self.save(name,result)
                dump(call_path,{'status':'COMPLETE','at':now(),'evidence':self.reference(name),
                                'correction_audit':correction_audit})
                return result
            except (ValueError,ModelFailure) as exc:
                from .mission_profiles import output_rejection
                kind=getattr(exc,'failure_class','PHASE_OUTPUT_INVALID')
                allowed={'contract':'unchanged input.rules and Host phase contract'}
                if name.startswith('edit_'):
                    allowed.update(paths=self.contract['allowed_paths'],
                        new_text_utf8_bytes=self.policy['max_replacement_bytes'])
                    schema=options.get('output_schema') or {}
                    if schema.get('oneOf'):
                        allowed['actions']=[s['properties']['action']['enum'][0] for s in schema['oneOf']]
                    if target_only:allowed['visible_target_ids']=getattr(self,'live_target_scope',[])
                feedback={**output_rejection(exc,allowed),'failure_class':kind,'required':str(exc)[:500],
                          **getattr(exc,'detail',{}),
                          'retry_rule':'Same frozen input and evidence. When violations are declared, return corrections only, not the original phase schema. Host alone merges and revalidates the full object.'}
                if kind=='CORRECTION_NO_PROGRESS' and violations:
                    feedback['violations']=json.loads(json.dumps(violations))
                for violation in feedback.get('violations',[]):
                    violation['previous_value']=field_value(previous,violation['field']) if previous is not None else {'exists':False}
                    if type(violation.get('limit')) is int and violation['limit']>0 and violation.get('unit')=='utf8_bytes':
                        has_target='soft_target' in violation
                        violation['soft_target']=max(0,violation['limit']*4//5)
                        if not has_target:
                            violation['required_action']+='; preserve meaning while simplifying. Aim for <='+str(violation['soft_target'])+' UTF-8 bytes; hard acceptance remains '+str(violation['limit'])+' bytes. Return only this declared field.'
                dump(call_path,{'status':'FAILED','at':now(),'failure_class':kind,'error':str(exc)[:500],
                                'feedback':feedback,'previous_output':previous,'correction_audit':correction_audit})
                if isinstance(exc,ModelFailure) and kind not in {'AGENT_OUTPUT_INCOMPLETE','AGENT_OUTPUT_INVALID_JSON','AGENT_OUTPUT_INVALID_SHAPE'}:raise
                if live_target_protocol:feedback=atomic_feedback(feedback)
            except HostDefect as exc:
                if model_dispatched:raise
                dump(call_path,{'status':'PRE_DISPATCH_FAILED','at':now(),'input_sha256':digest(payload),
                    'error':str(exc),'feedback':feedback,'previous_output':previous,
                    'model_dispatched':False})
                raise
        terminal=('CORRECTION_NO_PROGRESS' if kind=='CORRECTION_NO_PROGRESS' else
                  'PHASE_OUTPUT_LIMIT' if kind in {'PHASE_OUTPUT_LIMIT','AGENT_OUTPUT_INCOMPLETE'} else 'PHASE_CONTRACT_UNSATISFIED')
        if self.edit_takeover is not None and self.edit_protocol and name.startswith('edit_'):
            result=self.edit_takeover.propose(self,name,payload,check)
            self.save(name,result)
            return result
        self.run.event('host','output_retry_exhausted',phase=name,limit=self.policy['call_attempts'])
        error=ModelFailure(terminal,'Bounded phase failed: '+name+'; '+str(feedback)+'; checkpoint preserved')
        error.output_retry_exhausted=True
        raise error

    def handoff_packet(self, name):
        wrapper=self.cached(name)
        if wrapper is None or wrapper.get('research_handoff_version')!=2:
            raise HostDefect('PHASE_CHECKPOINT_DEFECT: missing reference handoff')
        packet=self.handoff.resume(wrapper['checkpoint'])
        if packet['evidence_refs']!=wrapper['evidence_ids']:
            raise HostDefect('PHASE_CHECKPOINT_DEFECT: handoff identity changed')
        return packet

    def handoff_detail(self, names, evidence_id, page, *, page_bytes=None):
        from .research_handoff import HandoffContractError
        if not isinstance(evidence_id,str) or type(page) is not int:
            raise HandoffContractError('Existing evidence_id and integer page required')
        for name in names:
            packet=self.handoff_packet(name)
            if evidence_id in packet['evidence_refs']:
                wrapper=self.cached(name)
                return {'source':wrapper['source'],**self.handoff.context(packet,evidence_id,page,
                    context_tokens=self.policy['context_tokens'],output_tokens=self.policy['max_output_tokens'],page_bytes=page_bytes)}
        raise HandoffContractError('Select an evidence_id from the Host catalog')

    def research_reference(self):
        from .research_handoff import CATEGORIES, ResearchReferenceScope
        summaries=[];refs=[];names=[]
        for i,page in enumerate(self.source_pages):
            name='research_'+str(i)
            source={k:v for k,v in page.items() if k!='text'}
            ref=self.handoff.register('source-'+digest(source)[:24],page['text'],
                hashlib.sha256(page['text'].encode()).hexdigest())
            empty={field:[] for field in CATEGORIES}
            empty['evidence_refs']=[ref['evidence_id']]
            initial=self.handoff.compile(empty,[ref])
            current=self.handoff.context(initial,ref['evidence_id'],
                context_tokens=self.policy['context_tokens'],output_tokens=self.policy['max_output_tokens'])
            # Prior packet has only category IDs and references, never prior bodies.
            prior=self.handoff.context(self.handoff_packet(names[-1])) if names else None
            scope=(ResearchReferenceScope(self.handoff,[ref],
                [r for previous_name in names for r in self.handoff_packet(previous_name)['catalog']])
                if self.working is not None and self.reference_protocol else None)
            authorization_calls=[]
            view=None;completed=None
            catalog_page=max(0,(len(summaries)-1)//4)
            observation_view=None
            def catalog(number):
                from .research_handoff import HandoffContractError
                count=max(1,(len(summaries)+3)//4)
                if type(number) is not int or not 0<=number<count:
                    raise HandoffContractError('Use an existing integer catalog page')
                return {'page':number,'page_count':count,'total':len(summaries),
                        'items':summaries[number*4:number*4+4]}
            def detail(evidence_id, page_number):
                if evidence_id==ref['evidence_id']:
                    return {'source':source,**self.handoff.context(initial,evidence_id,page_number,
                        context_tokens=self.policy['context_tokens'],output_tokens=self.policy['max_output_tokens'])}
                return self.handoff_detail(names,evidence_id,page_number)
            def check(value):
                if self.working is not None and set(value)=={'action','page'}:
                    if value['action']=='catalog':catalog(value['page']);return value
                    if value['action']=='observations':
                        self.working.detail(self.working.persist(self.feedback),value['page']);return value
                if set(value)=={'action','evidence_id','page'} and value['action']=='evidence':
                    detail(value['evidence_id'],value['page'])
                    return value
                if scope is not None:scope.validate(value)
                packet=self.handoff.save(name,value,list(scope.allowed.values()) if scope else [ref])
                p=self.handoff.path('checkpoints/'+name+'.json')
                return {'research_handoff_version':2,'checkpoint':name,'source':source,
                        'evidence_ids':packet['evidence_refs'],
                        'handoff':{'reference':str(p.relative_to(self.run.path)),'sha256':sha(p)},
                        **({'primary_evidence_ids':[ref['evidence_id']],
                            'authorization_calls':list(authorization_calls)} if scope else {})}
            for step in range(self.policy['max_research_handoff_calls']):
                payload={'task_goal':self.contract['goal'],
                    'source':source,'current_evidence':current,'prior_handoff':prior,
                    'prior_evidence_catalog':catalog(catalog_page)['items'] if self.working else [s for s in summaries], 'requested_detail':view,
                    'host_observations':self.feedback,
                    'rules':'Read-only research. Return exactly {finding:[IDs],conditions:[IDs],exceptions:[IDs],prohibitions:[IDs],uncertainty:[IDs],evidence_refs:[all current IDs]}. Category selections are ASSISTED; empty categories do not discard source evidence. At most 8 IDs/category, no descriptions/quotes. To inspect current or prior evidence return {action:evidence,evidence_id:existing ID,page:integer}. Respect page_count; one fragment is not the whole source. Host alone verifies/saves exact evidence and hashes.'}
                if self.working:
                    payload.update(catalog_view=catalog(catalog_page),requested_observations=observation_view)
                    payload['rules']+=' Browse all prior IDs with {action:catalog,page:integer}; catalog views are partial, not missing evidence.'
                if scope is not None:
                    payload['reference_protocol']=scope.protocol()
                    payload['rules']+=' reference_protocol defines the current final-output scope; read it before using any prior ID.'
                result=self.call(name+'_view_'+str(step),payload,check)
                if result.get('action')=='catalog':catalog_page=result['page'];continue
                if result.get('action')=='observations':
                    observation_view=self.working.detail(self.working.persist(self.feedback),result['page']);continue
                if result.get('action')=='evidence':
                    view=detail(result['evidence_id'],result['page'])
                    if scope is not None:
                        scope.authorize(result['evidence_id'],view)
                        authorization_calls.append(self.reference(name+'_view_'+str(step)))
                    if result['evidence_id']==ref['evidence_id']:
                        current=self.handoff.context(initial)  # Do not duplicate current body.
                    continue
                completed=result;break
            if completed is None:
                raise ModelFailure('PHASE_HANDOFF_LIMIT','Bounded research evidence requests exhausted; checkpoints preserved')
            if self.cached(name) is None:self.save(name,completed)
            elif self.cached(name)!=completed:raise HostDefect('PHASE_CHECKPOINT_DEFECT: research wrapper changed')
            names.append(name);refs.append(self.reference(name))
            summaries.append({'index':i,'evidence_ids':completed['evidence_ids']})
        return summaries,refs

    def hypothesis_reference(self, summaries, refs, check_final):
        existing=self.cached('hypothesis')
        if existing is not None:return existing
        names=['research_'+str(i) for i in range(len(refs))]
        from .research_handoff import CATEGORIES, HandoffContractError
        view=None;calls=[];fetched=set();requested=None;page_bytes=None
        observation_view=None
        def catalog(page):
            if type(page) is not int or not 0<=page<max(1,(len(names)+3)//4):
                raise HandoffContractError('Use an existing integer catalog page')
            items=[]
            for index in range(page*4,min(len(names),(page+1)*4)):
                packet=self.handoff_packet(names[index])
                items.append({**summaries[index],'source':self.cached(names[index])['source'],
                    **{f:packet[f] for f in (*CATEGORIES,'unclassified')}})
            return {'page':page,'page_count':max(1,(len(names)+3)//4),'items':items}
        catalog_view=catalog(0)
        def check(value):
            if self.working is not None and set(value)=={'action','page'} and value['action']=='observations':
                self.working.detail(self.working.persist(self.feedback),value['page']);return value
            if set(value)=={'action','page'} and value['action']=='catalog':
                catalog(value['page']);return value
            if set(value)=={'action','evidence_id','page'} and value['action']=='evidence':
                self.handoff_detail(names,value['evidence_id'],value['page'],page_bytes=page_bytes)
                return value
            final=check_final(value)
            if not set(value['evidence_indices']).issubset(fetched):
                if self.hypothesis_grounding:
                    # A schema-valid selection is not a grounded final proposal.
                    # Discard its hypothesis; Host performs one existing primary
                    # retrieval and requires a new model proposal after exposure.
                    index=next(i for i in value['evidence_indices'] if i not in fetched)
                    primary=self.cached(names[index]).get('primary_evidence_ids',summaries[index]['evidence_ids'])
                    detail=self.handoff_detail(names,primary[0],0,page_bytes=page_bytes)
                    return {'action':'evidence','evidence_id':primary[0],'page':0,
                            'host_grounding_request':True,'selected_index':index,
                            'source_sha256':detail['detail']['source_sha256']}
                raise HandoffContractError('Fetch source evidence for every selected research index before proposing a hypothesis')
            return final
        for step in range(self.policy['max_hypothesis_handoff_calls']):
            name='hypothesis_view_'+str(step)
            pinned=self.base/'packets'/(name+'.json')
            page_bytes=(load(pinned)['payload'].get('evidence_page_bytes') if pinned.exists() else
                (self.working.PAGE_BYTES if self.working else None))
            # Re-open the exact source at the next call's pinned page geometry;
            # old call inputs keep their historical geometry and hashes.
            if requested:
                view=self.handoff_detail(names,requested['evidence_id'],requested['page'],page_bytes=page_bytes)
            payload={'task_goal':self.contract['goal'],'allowed_paths':self.contract['allowed_paths'],
                'evidence_count':len(summaries),'catalog_view':catalog_view,'evidence_manifest':self.reference('research_manifest'),
                'requested_detail':view,'host_observations':self.feedback,
                'rules':'Evidence catalog IDs refer to Host snapshots, including unclassified evidence. Browse metadata with {action:catalog,page:integer}. Fetch necessary source detail with {action:evidence,evidence_id:existing ID,page:integer}; only the latest requested detail is shown. Fetch every selected research index before proposing {hypothesis:string,evidence_indices:[existing research index],paths:[allowed path]}; hypothesis <=900 UTF-8 bytes. Proposal only, not PASS; never change acceptance/validators.'}
            if page_bytes is not None:payload['evidence_page_bytes']=page_bytes
            if self.working:payload['requested_observations']=observation_view
            if self.hypothesis_protocol:
                from .hypothesis_protocol import response_protocol
                payload['response_protocol']=response_protocol(catalog_view,self.contract['allowed_paths'])
            result=self.call(name,payload,check)
            calls.append(self.reference(name))
            if result.get('action')=='context_material':
                self.material_view=self.working.material(result['context_id'],result['page']);continue
            if result.get('action')=='catalog':
                catalog_view=catalog(result['page']);continue
            if result.get('action')=='observations':
                observation_view=self.working.detail(self.working.persist(self.feedback),result['page']);continue
            if result.get('action')=='evidence':
                requested=result
                view=self.handoff_detail(names,result['evidence_id'],result['page'],page_bytes=page_bytes)
                # Supporting imports must not satisfy the existing obligation
                # to fetch a selected research index's primary source.
                fetched.update(i for i,s in enumerate(summaries) if result['evidence_id'] in
                    self.cached(names[i]).get('primary_evidence_ids',s['evidence_ids']))
                continue
            plan={**result,'handoff_calls':calls}
            self.save('hypothesis',plan)
            return plan
        raise ModelFailure('PHASE_HANDOFF_LIMIT','Bounded hypothesis evidence requests exhausted; checkpoints preserved')

    def research(self):
        if self.handoff is not None:return self.research_reference()
        findings=[];refs=[]
        for i,page in enumerate(self.source_pages):
            records={}
            for offset,text in enumerate(page['text'].splitlines()):
                line=page['start_line']+offset
                identifier='L'+str(line)+':C'+str(page.get('column',0))
                records[identifier]={'line':line,'quote':text}
            source={k:v for k,v in page.items() if k!='text'}
            source['lines']=records
            def check(value,page=page,records=records):
                if set(value)!={'observations'} or not isinstance(value['observations'],list) or len(value['observations'])>2:
                    raise PhaseContractError('observations','Return an observations array with at most two objects',
                        violation='type_or_cardinality',limit=2,
                        actual=len(value['observations']) if isinstance(value.get('observations'),list) else None,unit='items')
                violations=[]
                for index,item in enumerate(value['observations']):
                    if not isinstance(item,dict):continue
                    expected={'source_id','explanation'} if 'line' not in item and 'quote' not in item else {'line','quote','explanation'}
                    for missing in sorted(expected-set(item)):
                        violations.append({'field':'observations['+str(index)+'].'+missing,
                            'violation':'missing_required_field','limit':None,'actual':None,'unit':None,
                            'required_action':'Add the required '+missing+' field; preserve all other fields'})
                    explanation=item.get('explanation')
                    if isinstance(explanation,str) and len(explanation.encode())>400:
                        violations.append({'field':'observations['+str(index)+'].explanation',
                            'violation':'max_utf8_bytes','limit':400,'actual':len(explanation.encode()),'unit':'utf8_bytes',
                            'required_action':'Shorten explanation to <=400 UTF-8 bytes without adding facts; keep it well below the byte limit, not 400 characters'})
                if violations:
                    first=violations[0]
                    raise PhaseContractError(first['field'],first['required_action'],violations=violations)
                out=[];lines=self.files[page['path']].splitlines()
                for index,item in enumerate(value['observations']):
                    field='observations['+str(index)+']'
                    if isinstance(item,dict) and set(item)=={'source_id','explanation'}:
                        identifier=item['source_id']
                        if not isinstance(identifier,str) or identifier not in records or not records[identifier]['quote']:
                            raise PhaseContractError(field+'.source_id','Select a nonempty source_id from input.source.lines')
                        item={**records[identifier],'explanation':item['explanation']}
                    elif not isinstance(item,dict) or set(item)!={'line','quote','explanation'} or type(item['line']) is not int or not isinstance(item['quote'],str) or not item['quote']:
                        raise PhaseContractError(field,'Return {source_id:existing ID,explanation:string}; Host supplies line and exact quote')
                    line=item['line'];explanation=item['explanation']
                    if not page['start_line']<=line<page['start_line']+len(page['text'].splitlines()) or item['quote'] not in lines[line-1] or item['quote'] not in page['text']:
                        raise PhaseContractError(field,'Quote must occur on the declared single line in this page. Select a source_id instead of regenerating line/quote')
                    if not isinstance(explanation,str) or len(explanation.encode())>400:
                        raise PhaseContractError(field+'.explanation','Return an explanation of at most 400 UTF-8 bytes')
                    out.append({'path':page['path'],**item,'source_sha256':page['sha256'],'interpretation':'ASSISTED'})
                return {'observations':out,'source_sha256':page['sha256']}
            name='research_'+str(i)
            result=self.call(name,{'task_goal':self.contract['goal'],'source':source,
                'host_observations':self.feedback,
                'rules':'Read-only source observation. Return {observations:[{source_id:existing ID from source.lines,explanation:string}]}, at most two observations, explanation <=400 UTF-8 bytes. Host attaches exact source line, quote and hash. [] only if nothing relevant, not to bypass a rejected reference on retry. No code changes or commands.'},check)
            findings.extend(result['observations']);refs.append(self.reference(name))
        return findings,refs

    def execute(self):
        existing=self.cached('output')
        if existing is not None:return existing
        if self.contract.get('guided_edits'):return self.execute_guided()
        findings,refs=self.research()
        if not self.contract['allowed_paths']:
            output={'files':{},'summary':'Host source observations saved','findings':[{k:v for k,v in f.items() if k in {'path','line','quote','explanation'}} for f in findings]}
            self.save('output',output);return output
        def hypothesis(value):
            if set(value)!={'hypothesis','evidence_indices','paths'} or not isinstance(value['hypothesis'],str) or len(value['hypothesis'].encode())>900:
                raise ValueError('Brief hypothesis, evidence_indices, paths required')
            if not isinstance(value['evidence_indices'],list) or not value['evidence_indices'] or len(value['evidence_indices'])>4 or any(type(i) is not int or not 0<=i<len(refs) for i in value['evidence_indices']):
                raise ValueError('Use existing research evidence indices only')
            if not isinstance(value['paths'],list) or not value['paths'] or any(p not in self.contract['allowed_paths'] for p in value['paths']):
                raise ValueError('Choose only Host allowed paths')
            return {**value,'host_references':[refs[i] for i in value['evidence_indices']]}
        if self.cached('research_manifest') is None:self.save('research_manifest',{'references':refs,
            **({'evidence_catalog':findings,'handoff_schema_version':2} if self.handoff is not None else {'observations':findings})})
        if self.handoff is not None:plan=self.hypothesis_reference(findings,refs,hypothesis)
        else:plan=self.call('hypothesis',{'task_goal':self.contract['goal'],'allowed_paths':self.contract['allowed_paths'],
             'evidence':[{'index':i} for i in range(len(refs))],'evidence_manifest':self.reference('research_manifest'),
             'observations':findings[:8], 'observation_count':len(findings),'view_is_partial':len(findings)>8,
             'host_observations':self.feedback,'rules':'Return {hypothesis:string,evidence_indices:[existing index],paths:[allowed path]}. Brief proposal only; never change acceptance or validators. Detailed evidence remains saved.'},hypothesis)
        staged=dict(self.files);view=None
        revised_plan=None;candidate_history=[];stagnant_trials=0;best_passed=0;trial_progress=None
        observation_view=None
        source_catalog_page=0
        last_action=None
        staged_checks=None
        reads=[]
        read_page_bytes=min(2000,self.policy['source_page_bytes']) if self.edit_protocol else self.policy['source_page_bytes']
        from .source_navigation import serialized_pages
        page_source=lambda text:serialized_pages(text,read_page_bytes) if self.serialized_source_pages else pages(text,read_page_bytes)
        def test_view(receipt,folder):
            ref={'reference':(folder/'receipt.json').relative_to(self.run.path).as_posix(),'sha256':sha(folder/'receipt.json')}
            checks={'reference':ref,'completion':False,'passed':all(r['passed'] for r in receipt['results']),
                'tests':[{'test':r['test'],'passed':r['passed']} for r in receipt['results']],
                'failures':[{'test':r['test'],'stderr':r['execution']['stderr'][-400:]}for r in receipt['results']if not r['passed']][:2]}
            if self.retry_context:
                from .staged_validation import diagnostics
                checks['failures']=diagnostics(receipt)
            return checks
        if self.execution_navigation:
            from .staged_validation import observe_staged
            from .diagnostic_navigation import failure_source
            folder=self.base/'staged_tests/baseline'
            receipt=load(folder/'receipt.json') if (folder/'receipt.json').exists() else observe_staged(self.run,self.contract,staged,folder)
            if receipt['source_hashes']!={n:hashlib.sha256(t.encode()).hexdigest()for n,t in staged.items()}:
                raise HostDefect('PHASE_CHECKPOINT_DEFECT: baseline diagnostic snapshot differs')
            staged_checks=test_view(receipt,folder)
            best_passed=sum(r['passed'] for r in receipt['results'])
            if self.trial_review_version==2:
                from .trial_progress import TrialProgress
                trial_progress=TrialProgress(receipt)
            view=failure_source(receipt,staged,page_source)
        from .resolution import ResolutionCoordinator
        resolution=ResolutionCoordinator(self.run,self.contract,self.base)
        self.material_view=None
        for step in range(self.policy['max_edit_calls']):
            if step in self.repair_skipped_steps:continue
            resolution.step=step
            review_required=trial_progress.packet()['required'] if trial_progress else self.trial_review_protocol and stagnant_trials>=2
            pinned=self.base/'packets'/('edit_'+str(step)+'.json')
            diagnostic_call=self.retry_context and (not pinned.exists() or 'diagnostic_access' in load(pinned)['payload'])
            def check(value):
                action=value.get('action')
                if self.edit_protocol_version==2 and getattr(self,'live_target_scope',None) is not None:
                    if action in ('replace','patch'):
                        raise ValueError('Current v2 edits require Host target IDs; legacy operations are replay-only')
                    if action=='replace_target' and value.get('target_id') not in self.live_target_scope:
                        raise ValueError('Target body not visible; retrieve source and select a smaller explicit target')
                if resolution.advice_view() and self.working and action=='advice' and set(value)=={'action','page'}:
                    self.working.detail(self.working.persist(resolution.advice_view()),value['page']);return value
                if review_required and action in ('replace','patch','replace_target'):
                    from .research_handoff import HandoffContractError
                    error=HandoffContractError('Review threshold reached without non-regressing test-ID improvement. Compare receipts and maintain, revise or reject the hypothesis before further editing.')
                    error.failure_class='TRIAL_REVIEW_REQUIRED'
                    error.detail={'allowed_operations':['read','source_search','evidence','trial_result','test_result','reconsider','done']}
                    raise error
                if self.reconsideration and action=='reconsider':
                    from .candidate_reasoning import check_reconsideration
                    return check_reconsideration(value,self.contract['allowed_paths'],staged_checks,revised_plan or plan,review_decision=self.trial_review_version==2)
                if self.reconsideration and action=='trial_result' and set(value)=={'action','step','page'}:
                    trial=next((t for t in candidate_history if type(value['step']) is int and t['step']==value['step']),None)
                    if trial is None:raise ValueError('Select an existing Host trial step from this task attempt')
                    target=self.run.path/trial['reference']['reference']
                    if sha(target)!=trial['reference']['sha256']:raise HostDefect('PHASE_CHECKPOINT_DEFECT: trial receipt hash mismatch')
                    self.working.detail(self.working.persist(load(target)),value['page']);return value
                if self.retry_context and action in ('previous_output','test_result') and set(value)=={'action','page'}:
                    ref=self.retry_previous_ref if action=='previous_output' else staged_checks.get('reference')
                    if not ref:raise ValueError('No Host-declared '+action+' detail is available')
                    if action=='test_result':
                        target=self.run.path/ref['reference']
                        if sha(target)!=ref['sha256']:raise HostDefect('PHASE_CHECKPOINT_DEFECT: test receipt hash mismatch')
                        ref=self.working.persist(load(target))
                    self.working.detail(ref,value['page']);return value
                if self.source_navigation and action=='source_search' and set(value)=={'action','term'}:
                    from .source_navigation import search
                    search(staged,value['term'],page_source);return value
                if self.source_navigation and action=='navigation' and set(value)=={'action','page'}:
                    self.working.detail(self.working.persist(extra['source_navigation']),value['page']);return value
                if self.edit_context and action=='source_catalog' and set(value)=={'action','page'}:
                    readonly_count=sum(p not in self.contract['allowed_paths'] for p in staged)
                    if type(value['page']) is not int or not 0<=value['page']<max(1,(readonly_count+3)//4):
                        raise ValueError('Select an existing source catalog page')
                    return value
                if self.working and action=='observations' and set(value)=={'action','page'}:
                    self.working.detail(self.working.persist(self.feedback),value['page']);return value
                if action=='done' and set(value)=={'action','summary'} and isinstance(value['summary'],str):return value
                if action=='evidence' and set(value)=={'action','index'} and type(value['index']) is int and 0<=value['index']<len(refs):
                    return value
                if action=='read' and set(value)=={'action','path','page'}:
                    name=value['path'];page=value['page']
                    if not isinstance(name,str) or name not in staged or type(page) is not int or not 0<=page<len(page_source(staged[name])):
                        raise ValueError('Read requires existing path and page index')
                    if self.edit_protocol and view and view.get('path')==name and view.get('page')==page and view.get('source_sha256')==hashlib.sha256(staged[name].encode()).hexdigest():
                        from .research_handoff import HandoffContractError
                        error=HandoffContractError('This exact source page is already in source_view. Reuse it; request another page/source, patch within Host scope, or finish staging. Repeating an unchanged read is not progress.')
                        error.failure_class='TOOL_REQUEST_NO_PROGRESS'
                        error.detail={'already_visible':{'path':name,'page':page},
                                      'required_action':'Use the existing source_view; do not repeat this read',
                                      'allowed_operations':['read another page/source','patch','evidence','observations','source_catalog','done']}
                        raise error
                    return value
                if self.edit_targets is not None and action=='edit_target':
                    if set(value)!={'action','path','source_sha256','start_line','end_line'}:
                        raise ValueError('Explicit target request fields required')
                    if value['source_sha256']!=hashlib.sha256(staged.get(value['path'],'').encode()).hexdigest():
                        raise ValueError('Stale target request source hash')
                    self.edit_targets.issue(staged,value['path'],value['start_line'],value['end_line'])
                    return value
                if self.edit_targets is not None and action=='replace_target':
                    self.edit_targets.resolve(staged,value,self.policy['max_replacement_bytes'])
                    return value
                if self.edit_protocol and action=='replace':
                    if set(value)!={'action','path','source_sha256','old_text','new_text','summary'}:
                        raise ValueError('Return exactly the Host-declared exact replacement fields')
                    name=value['path']
                    if name not in self.contract['allowed_paths'] or value['source_sha256']!=hashlib.sha256(staged.get(name,'').encode()).hexdigest():
                        raise ValueError('Scope/stale source hash')
                    if not isinstance(value['summary'],str):raise ValueError('String summary required')
                    if isinstance(value['old_text'],str) and value['new_text']==value['old_text']:
                        raise PhaseContractError('new_text','Unchanged replacement. Correct new_text only to your intended actual source change; preserve path/hash/old_text. Host independently checks scope, syntax and tests.')
                    from .edit_protocol import replace_exact
                    try:replace_exact(staged.get(name,''),value['old_text'],value['new_text'],self.policy['max_replacement_bytes'])
                    except ValueError as exc:
                        if self.source_navigation and isinstance(value['old_text'],str) and staged.get(name,'').count(value['old_text'])!=1:
                            from .research_handoff import HandoffContractError
                            from .source_navigation import outline
                            chunks=page_source(staged.get(name,''))
                            symbols=outline(staged.get(name,''),chunks,4)
                            error=HandoffContractError(str(exc))
                            error.detail={'source_resolution':{'path':name,'page_count':len(chunks),'symbols':symbols},
                                'required_action':'Read actual source detail before proposing a new replacement. Do not guess absent source or resend the rejected old_text.',
                                'response_examples':[{'action':'read','path':name,'page':s['page']} for s in symbols[:2]]}
                            raise error from exc
                        raise
                    return value
                if self.edit_protocol and action=='patch' and 'replacement_lines' in value:
                    if set(value)!={'action','path','source_sha256','start_line','end_line','replacement_lines','summary'}:
                        raise ValueError('Return exactly the Host-declared patch operation fields')
                    name=value['path'];end=value['end_line']
                    if not isinstance(name,str) or type(end) is not int:raise ValueError('Explicit path and integer end_line required')
                    from .edit_protocol import render_lines
                    replacement=render_lines(value['replacement_lines'],end<len(staged.get(name,'').splitlines(True)),self.policy['max_replacement_bytes'])
                    value={k:v for k,v in value.items() if k!='replacement_lines'};value['replacement']=replacement
                if action!='patch' or set(value)!={'action','path','source_sha256','start_line','end_line','replacement','summary'}:
                    raise ValueError('Return one read/evidence, one bounded patch, or done; no complete file bundles')
                name=value['path']
                if not isinstance(name,str):raise ValueError('Explicit string source path required')
                text=staged.get(name,'');a=value['start_line'];b=value['end_line'];replacement=value['replacement']
                if name not in self.contract['allowed_paths'] or value['source_sha256']!=hashlib.sha256(text.encode()).hexdigest():raise ValueError('Scope/stale source hash')
                if type(a) is not int or type(b) is not int or not 1<=a<=len(text.splitlines(True))+1 or not a-1<=b<=len(text.splitlines(True)) or b-a>80:
                    raise ValueError('Explicit bounded line range required')
                if not isinstance(replacement,str) or len(replacement.encode())>self.policy['max_replacement_bytes'] or not isinstance(value['summary'],str):
                    raise ValueError('Atomic replacement exceeds byte bound')
                if self.edit_protocol:
                    lines=text.splitlines(True)
                    if replacement and b<len(lines) and not replacement.endswith('\n'):
                        # Explicit LINE replacement owns line serialization on
                        # Host, not the model. Preserve a following line boundary
                        # without changing any replacement line's contents.
                        replacement+='\n'
                        if len(replacement.encode())>self.policy['max_replacement_bytes']:
                            raise ValueError('Serialized line replacement exceeds unchanged byte bound')
                        value={**value,'replacement':replacement}
                    candidate=list(lines);candidate[a-1:b]=[replacement]
                    if ''.join(candidate)==text:
                        from .research_handoff import HandoffContractError
                        error=HandoffContractError('Proposed patch is byte-identical to staged source. No change occurred. Return done if staging is complete, or a different required change; do not repeat this patch.')
                        error.failure_class='TOOL_REQUEST_NO_PROGRESS'
                        error.detail={'already_visible':{'path':name,'source_sha256':value['source_sha256']},
                                      'required_action':'Return done or a different required patch; identical patches are not progress'}
                        raise error
                return value
            base_view=[({'path':p,'source_sha256':hashlib.sha256(staged.get(p,'').encode()).hexdigest(),
                        'pages':len(page_source(staged.get(p,''))),
                        'write_allowed':True} if p in self.contract['allowed_paths'] else
                        {'path':p,'write_allowed':False}) for p in sorted(set(staged)|set(self.contract['allowed_paths']))]
            extra={}
            working_plan=revised_plan or plan
            if self.edit_context:
                writable=[v for v in base_view if v['write_allowed']]
                readonly=[v for v in base_view if not v['write_allowed']]
                catalog_pages=max(1,(len(readonly)+3)//4)
                if source_catalog_page>=catalog_pages:raise HostDefect('PHASE_CONTEXT_DEFECT: invalid source catalog cursor')
                base_view=writable+readonly[source_catalog_page*4:source_catalog_page*4+4]
                working_plan={k:working_plan[k] for k in ('hypothesis','evidence_indices','paths')}
                extra={'hypothesis_reference':self.reference('hypothesis'),
                       'source_catalog':{'page':source_catalog_page,'page_count':catalog_pages,
                                         'total_readonly':len(readonly),'paged':True},
                       'catalog_access':'All writable paths remain visible. Request {"action":"source_catalog","page":0} to browse readonly source metadata; every allowed Host source remains readable by exact path.'}
            if self.edit_protocol:
                from .edit_protocol import operation_schema
                self.edit_schema=operation_schema(staged,self.contract['allowed_paths'],len(refs),navigation=self.source_navigation,diagnostics=diagnostic_call,reconsider=self.reconsideration,review_decision=self.trial_review_version==2,resolution=bool(resolution.advice_view() and self.working),
                    current_test_reference=staged_checks['reference']['reference'] if self.trial_review_version==2 and staged_checks and not staged_checks['passed'] else None,
                    targets=self.edit_targets is not None,target_only=self.edit_protocol_version==2)
                if review_required:
                    self.edit_schema={'oneOf':[s for s in self.edit_schema['oneOf'] if s['properties']['action']['enum'][0] not in ('patch','replace','replace_target')]}
                extra.update(task_goal=self.contract['goal'],
                    host_contract={k:self.contract[k] for k in ('task_id','requirement_ids','expected_outputs','validators','required_evidence','completion_conditions')},
                    tool_state={'last_action':last_action,'changed_paths':[p for p in self.contract['allowed_paths'] if staged.get(p)!=self.files.get(p)],
                                'read_rule':'source_view is the result of the last read; reuse visible code instead of requesting the same unchanged page. Staging done is not Host validation PASS.'},
                    operation_protocol={'patch_keys':['action','path','source_sha256','start_line','end_line','replacement_lines','summary'],
                        'line_rule':'replacement_lines is an array of literal source lines, NOT a string containing escaped newline sequences. Host joins lines. Hard replacement byte limit remains 900. Empty array deletes a line range. Preserve all other lines.',
                        'finish_example':{'action':'done','summary':'Staging complete; Host must independently validate'}})
            if self.source_navigation:
                from .source_navigation import outline
                extra['source_navigation']={'read_history':reads[-12:],
                    'remaining_operations':self.policy['max_edit_calls']-step,
                    'symbols':{p:outline(staged.get(p,''),page_source(staged.get(p,'')),4) for p in self.contract['allowed_paths']},
                    'literal_search':{'action':'source_search','term':'literal function or symbol from observed source'},
                    'rules':'Read page indices from symbols rather than repeatedly reading headers. History identifies prior reads; no source/Evidence was deleted. source_search searches only Host-bound code, not external files.'}
            edit_rules=('One operation only. Read existing code before editing. Prefer an exact replacement: {"action":"replace","path":"existing path","source_sha256":"current hash","old_text":"exact unique source fragment","new_text":"replacement fragment","summary":"reason"}. Host requires old_text to match exactly once and new_text <=900 UTF-8 bytes. Alternatively patch uses inclusive start_line/end_line and replacement_lines (one literal line per array item); Host owns line separators. Read {"action":"read","path":"existing path","page":0}; evidence {"action":"evidence","index":0}. Return {"action":"done","summary":"staged"} when needed edits are finished; done means run Host validation, NOT PASS. Do not repeat visible reads or identical patches.' if self.edit_protocol else
                'One operation only. {action:read,path,page} reads a page; {action:evidence,index} reads one Host-verified research checkpoint. {action:patch,path,source_sha256,start_line,end_line,replacement,summary} replaces explicit lines (end=start-1 inserts). Replacement <=900 UTF-8 bytes. New empty files start=1,end=0. Request code before editing existing files. {action:done,summary} finishes staging, NOT PASS. Host alone validates.')
            if self.edit_targets is not None:
                edit_rules='Prefer {action:replace_target,target_id:Host-issued ID,new_text:string}; do NOT copy original code. Request {action:edit_target,path,source_sha256,start_line,end_line} for an explicit bounded block. ID binds exact full-source hash and range; stale IDs stop. new_text <=900 UTF-8 bytes, retain a line separator before following lines. '+edit_rules
                if self.edit_protocol_version==2:
                    from .edit_protocol import target_rules
                    edit_rules=target_rules()
                    extra['operation_protocol']={'editing':'replace_target','authority':'Host scope/hash/range validation; unchanged acceptance'}
                if self.edit_protocol_version==1 and view and view.get('path') in self.contract['allowed_paths'] and isinstance(view.get('text'),str) and view['text']:
                    try:view['edit_target']=self.edit_targets.issue_unique(staged,view['path'],view['text'])
                    except ValueError:pass  # A partial/ambiguous page never silently becomes an edit target.
            if self.edit_protocol:extra['protected_staged_tests']=staged_checks
            if self.execution_navigation:extra['current_objective']={
                'phase':'stabilize_tested_candidate' if staged_checks and not staged_checks['passed'] else 'implement_task',
                'rules':'The original task and acceptance are unchanged. A failed tested candidate is not progress toward completion. Inspect observed error/source first; use exact current declarations, not guessed variables/types. Diagnostic location is a clue, not proof of causality. Never modify protected tests.'}
            if diagnostic_call:extra['diagnostic_access']={'test_result':{'action':'test_result','page':0},
                'previous_output':{'action':'previous_output','page':0},
                'rules':'Request exact Host test receipt or rejected output pages when present. These are observations, never formal PASS.'}
            if self.reconsideration:
                extra['candidate_reasoning']={'recent_trials':[{k:v for k,v in t.items() if k!='reference'} for t in candidate_history[-2:]],
                    'trial_access':{'action':'trial_result','step':candidate_history[-1]['step'],'page':0} if candidate_history else None,
                    'rules':'A hypothesis is revisable, never a contract. If tested edits leave the same failures, investigate contrary evidence or return {action:reconsider,hypothesis:new explanation,paths:[allowed paths],test_reference:exact current protected_staged_tests reference}. Host retains original hypothesis and all trials. No test/acceptance changes; reconsider consumes one existing operation.'}
            if self.trial_review_protocol:
                extra['trial_review']={'required':review_required,'non_improving_trials':stagnant_trials,'best_passed':best_passed}
                if trial_progress:
                    from .trial_progress import review_instructions
                    extra['trial_review']=trial_progress.packet()
                    extra['trial_review']['rules']=review_instructions()
                    extra['candidate_reasoning']['rules']='Review policy is defined once in trial_review; exact receipts via trial_result.'
            if resolution.advice_view() and self.working:
                detail=self.working.detail(self.working.persist(resolution.advice_view()),0)
                extra['resolution_advice_access']={'reference':detail['reference'],'page_count':detail['page_count'],
                    'request':{'action':'advice','page':0},'authority':'suggestion_only'}
            if self.edit_takeover is not None:self.takeover_sources=dict(staged)
            self.current_staged_files=staged
            result=self.call('edit_'+str(step),{'hypothesis':working_plan,'sources':base_view,'source_view':view,**extra,
               'evidence_count':len(refs),
               **({'host_observations':self.feedback,'requested_observations':observation_view} if self.working else {}),
               'rules':edit_rules},check)
            last_action={k:result[k] for k in ('action','path','page') if k in result}
            if result['action']=='context_material':
                self.material_view=self.working.material(result['context_id'],result['page']);continue
            if result['action']=='edit_target':
                target=self.edit_targets.issue(staged,result['path'],result['start_line'],result['end_line'])
                lines=staged.get(result['path'],'').splitlines(True)
                view={'kind':'edit_target','path':result['path'],'edit_target':target,
                      'text':''.join(lines[result['start_line']-1:result['end_line']])}
                continue
            if result['action']=='replace_target':
                result=self.edit_targets.resolve(staged,result,self.policy['max_replacement_bytes'])
            if result['action']=='advice':
                view={'kind':'resolution_advice',**self.working.detail(self.working.persist(resolution.advice_view()),result['page'])};continue
            if result['action']=='trial_result':
                trial=next(t for t in candidate_history if t['step']==result['step'])
                target=self.run.path/trial['reference']['reference']
                if sha(target)!=trial['reference']['sha256']:raise HostDefect('PHASE_CHECKPOINT_DEFECT: trial receipt hash mismatch')
                view={'kind':'trial_result','step':result['step'],
                      **self.working.detail(self.working.persist(load(target)),result['page'])}
                continue
            if result['action']=='reconsider':
                stagnant_trials=0
                if trial_progress:trial_progress.review()
                resolution.reviewed(step,result.get('decision','revise'))
                revised_plan={'hypothesis':result['hypothesis'],'paths':result['paths'],
                    'evidence_indices':(revised_plan or plan)['evidence_indices']}
                view={'kind':'reconsideration_receipt','reference':self.reference('edit_'+str(step)),
                      'test_reference':result['test_reference'],'authority':'Worker proposal only; original contract unchanged'}
                if self.trial_review_version==2:view['decision']=result.get('decision','revise')
                continue
            if self.retry_context and result['action'] in ('previous_output','test_result'):
                ref=self.retry_previous_ref
                if result['action']=='previous_output' and ref is None:
                    rejected=[load(p).get('previous_output') for p in sorted((self.base/'calls'/('edit_'+str(step))).glob('*.json'))
                        if load(p).get('previous_output') is not None]
                    if not rejected:raise HostDefect('PHASE_CHECKPOINT_DEFECT: rejected output reference absent')
                    ref=self.working.persist(rejected[-1])
                if result['action']=='test_result':
                    source=staged_checks['reference'];target=self.run.path/source['reference']
                    if sha(target)!=source['sha256']:raise HostDefect('PHASE_CHECKPOINT_DEFECT: test receipt hash mismatch')
                    ref=self.working.persist(load(target))
                view={'kind':result['action'],**self.working.detail(ref,result['page'])};continue
            if result['action']=='navigation':
                view={'kind':'navigation_detail',**self.working.detail(self.working.persist(extra['source_navigation']),result['page'])};continue
            if result['action']=='source_search':
                from .source_navigation import search
                view={'kind':'literal_source_search',**search(staged,result['term'],page_source)};continue
            if result['action']=='observations':
                observation_view=self.working.detail(self.working.persist(self.feedback),result['page']);continue
            if result['action']=='source_catalog':
                source_catalog_page=result['page'];continue
            if result['action']=='done':
                output={'files':{p:staged[p] for p in self.contract['allowed_paths'] if p in staged and staged[p]!=self.files.get(p)},'summary':result['summary'],'findings':[],
                        'phase_evidence':[self.reference('hypothesis')]+[self.reference('edit_'+str(i)) for i in range(step+1) if i not in self.repair_skipped_steps]}
                if self.handoff is not None:output['phase_evidence'].insert(0,self.reference('research_manifest'))
                self.save('output',output);return output
            if result['action']=='evidence':
                if self.handoff is not None:
                    packet=self.handoff_packet('research_'+str(result['index']))
                    view=self.handoff_detail(['research_'+str(result['index'])],packet['evidence_refs'][0],0)
                else:view={'host_evidence':self.cached('research_'+str(result['index'])),'provenance':refs[result['index']]}
                continue
            name=result['path']
            if result['action']=='read':
                view={'path':name,**page_source(staged[name])[result['page']]}
                if self.edit_protocol:view.update(page=result['page'],source_sha256=hashlib.sha256(staged[name].encode()).hexdigest())
                reads.append({'path':name,'page':result['page']})
            else:
                if result['action']=='replace':
                    from .edit_protocol import replace_exact
                    staged[name]=replace_exact(staged[name],result['old_text'],result['new_text'],self.policy['max_replacement_bytes'])
                else:
                    lines=staged.get(name,'').splitlines(True);lines[result['start_line']-1:result['end_line']]=[result['replacement']]
                    staged[name]=''.join(lines)
                if sum(len(t.encode()) for p,t in staged.items() if p in self.contract['allowed_paths'])>100000:
                    raise HostDefect('PHASE_ARTIFACT_DEFECT: staged source exceeds limit')
                view={'path':name,'source_sha256':hashlib.sha256(staged[name].encode()).hexdigest(),
                      'start_line':result.get('start_line'),'text':result.get('replacement',result.get('new_text'))}
                if self.edit_protocol:view.update(kind='patch_receipt',full_source=False)
                if self.edit_protocol:
                    from .staged_validation import observe_staged
                    folder=self.base/'staged_tests'/str(step)
                    receipt=load(folder/'receipt.json') if (folder/'receipt.json').exists() else observe_staged(self.run,self.contract,staged,folder)
                    if receipt['source_hashes']!={n:hashlib.sha256(t.encode()).hexdigest() for n,t in staged.items()}:
                        raise HostDefect('PHASE_CHECKPOINT_DEFECT: staged test source hash mismatch')
                    staged_checks=test_view(receipt,folder);ref=staged_checks['reference']
                    if self.edit_takeover is not None:
                        self.edit_takeover.validated(self,'edit_'+str(step),receipt,ref)
                    if self.reconsideration:
                        from .candidate_reasoning import trial_summary
                        candidate_history.append(trial_summary(step,ref,receipt))
                    if self.trial_review_protocol:
                        passed=sum(r['passed'] for r in receipt['results'])
                        stagnant_trials=0 if passed>best_passed else stagnant_trials+1
                        best_passed=max(best_passed,passed)
                        if trial_progress:
                            trial_progress.observe(receipt,result)
                            comparison=self.base/'staged_tests'/str(step)/'comparison.json'
                            dump(comparison,{'authority':'host','test_reference':ref,
                                 'comparison':trial_progress.comparison,'review':trial_progress.packet(),
                                 'classification':'execution change only, not semantic cause diagnosis'})
                    if self.execution_navigation:
                        from .diagnostic_navigation import failure_source
                        pointed=failure_source(receipt,staged,page_source)
                        if pointed is not None:view=pointed
                    advice=resolution.observe(step,receipt,ref,result,trial_progress,view)
                    if advice:
                        # Advice replaces a requested view, not the accumulated
                        # context. Full text stays in the hashed Host store.
                        if self.working:
                            view={'kind':'resolution_advice',**self.working.detail(self.working.persist(advice),0)}
                        else:view=advice
                    if self.staged_candidate_gate and receipt['results'] and staged_checks['passed'] and all(staged.get(p) for p in self.contract['expected_outputs']):
                        output={'files':{p:staged[p] for p in self.contract['allowed_paths'] if p in staged and staged[p]!=self.files.get(p)},
                            'summary':'Host protected staged tests passed; candidate requires independent formal validation and request review',
                            'findings':[],'phase_evidence':[self.reference('hypothesis')]+[self.reference('edit_'+str(i)) for i in range(step+1) if i not in self.repair_skipped_steps]+[ref]}
                        if self.handoff is not None:output['phase_evidence'].insert(0,self.reference('research_manifest'))
                        self.save('output',output);return output
        raise ModelFailure('PHASE_EDIT_LIMIT','Bounded edit steps exhausted; saved phases remain available')

    def execute_guided(self):
        """Same phase calls, targets, finite correction and Host test authority."""
        from .staged_validation import observe_staged,StagedRun
        from .edit_targets import target_schema
        from .search_quality import line_edits
        plan=self.contract['guided_edits'];staged=dict(self.files);refs=[]
        if not self.edit_targets or self.policy['version']!=2:raise HostDefect('Guided edits require current v2 targets')
        for path,h in plan['source_hashes'].items():
            if hashlib.sha256(staged[path].encode()).hexdigest()!=h:raise HostDefect('Guided initial source changed')
        baseline_folder=self.base/'guided_tests/baseline'
        baseline=load(baseline_folder/'receipt.json') if (baseline_folder/'receipt.json').exists() else observe_staged(self.run,self.contract,staged,baseline_folder)
        passing={r['test'] for r in baseline['results'] if r['passed']}
        for i,step in enumerate(plan['steps']):
            slot=i
            for skipped in self.repair_skipped_steps:
                if skipped<=slot:slot+=1
            operation='edit_'+str(slot)
            path=step['path'];target=self.edit_targets.issue_unique(staged,path,step['original_text'])
            target_name='guided_target_'+str(i)
            saved_target=self.cached(target_name)
            if saved_target is None:self.save(target_name,target)
            else:
                # Multiple archived generations can bind identical bytes with
                # different namespace IDs. Replay the exact pinned ID, verifying
                # both its archived record and the current staged source range.
                verified=self.edit_targets.target(saved_target['target_id'])
                if verified!=saved_target or any(saved_target[k]!=v for k,v in target.items() if k!='target_id'):
                    raise HostDefect('Guided target changed during replay')
                target=verified
            view={'kind':'edit_target','path':path,'edit_target':target,'text':step['original_text']}
            self.current_staged_files=staged;self.edit_schema={'oneOf':[target_schema()]}
            def check(value):
                actual=self.edit_targets.target(value.get('target_id',''))
                if any(actual[k]!=target[k] for k in target if k!='target_id'):
                    raise ValueError('Use current Host target for this exact source range')
                patch=self.edit_targets.resolve(staged,value,self.policy['max_replacement_bytes'])
                candidate={**staged,**line_edits([{k:patch[k] for k in ('path','source_sha256','start_line','end_line','replacement')}],staged,self.contract['allowed_paths'])}
                folder=self.base/'guided_tests'/step['id']/digest(value)
                receipt=load(folder/'receipt.json') if (folder/'receipt.json').exists() else observe_staged(self.run,self.contract,candidate,folder)
                if receipt['source_hashes']!={n:hashlib.sha256(t.encode()).hexdigest() for n,t in candidate.items()}:
                    raise HostDefect('Guided validation source hash mismatch')
                for name,expected in receipt['workspace_hashes'].items():
                    source=folder/'workspace'/name
                    if source.is_symlink() or not source.resolve().is_relative_to((folder/'workspace').resolve()) or sha(source)!=expected:
                        raise HostDefect('Guided validation workspace hash mismatch')
                if self.contract['profile']=='php_search_quality':
                    from .search_quality import execute
                    syntax=execute(StagedRun(self.run,folder/'workspace'),['-l',str(folder/'workspace'/path)])
                else:
                    try:compile(candidate[path],path,'exec');syntax={'exit_code':0,'stderr':''}
                    except SyntaxError as exc:syntax={'exit_code':1,'stderr':str(exc),'line':exc.lineno}
                if not (folder/'syntax.json').exists():dump(folder/'syntax.json',syntax)
                failed=[r for r in receipt['results'] if r['test'] in passing and not r['passed']]
                if syntax['exit_code'] or failed:
                    raise ValueError('Local validation failed. Syntax: '+syntax.get('stderr','')+'; protected regressions: '+
                        json.dumps([{'test':r['test'],'error':r['execution']['stderr']} for r in failed],ensure_ascii=False)+
                        '; correct this step only using exact source_view and instruction. Scope and limits unchanged.')
                return value
            rules='Execute this ONE instructed edit. Return ONLY {action:replace_target,target_id:current Host ID,new_text:complete replacement}. No old_text. Preserve newline boundaries. Use the supplied solution as teaching material, no unrelated changes. UTF-8 <=900 bytes. Host tests each step; do not claim completion.'
            if self.repair_skipped_steps and not (self.base/'packets'/(operation+'.json')).exists():
                rules+=' The supplied new_text is the complete replacement, including any boundary declaration. Copy it exactly. Do not invent a function body for a declaration whose existing body follows the target. Failed calls remain consumed.'
            result=self.call(operation,{'source_view':view,'sources':[{'path':path,'source_sha256':target['source_sha256'],'write_allowed':True}],
                'guided_step':{k:step[k] for k in ('id','instruction','new_text','local_check')},
                'attribution':'Codex-designed instruction, not independent problem solving',
                'host_contract':{'allowed_paths':self.contract['allowed_paths'],'new_text_max_utf8_bytes':900,'acceptance':'unchanged; final Host validation after all steps'},
                'rules':rules},check)
            check(result)
            patch=self.edit_targets.resolve(staged,result,900)
            staged.update(line_edits([{k:patch[k] for k in ('path','source_sha256','start_line','end_line','replacement')}],staged,self.contract['allowed_paths']))
            ref=self.reference(operation);refs.append(ref);name='guided_'+step['id']
            validation_folder=self.base/'guided_tests'/step['id']/digest(result)
            if self.cached(name) is None:self.save(name,{'accepted':True,'step':step['id'],'operation':ref,
                'matches_taught_text':result['new_text']==step['new_text'],'source_sha256':hashlib.sha256(staged[path].encode()).hexdigest(),
                'test_receipt':{'reference':str((validation_folder/'receipt.json').relative_to(self.run.path)), 'sha256':sha(validation_folder/'receipt.json')},
                'syntax_receipt':{'reference':str((validation_folder/'syntax.json').relative_to(self.run.path)), 'sha256':sha(validation_folder/'syntax.json')},
                'local_validation':'syntax and baseline PASS non-regression; not final acceptance',
                'calls':len(list((self.base/'calls'/operation).glob('*.json')))})
        output={'files':{p:staged[p] for p in self.contract['allowed_paths'] if p in staged},
            'summary':'Codex-designed steps executed by Worker and locally verified by Host; final acceptance still required',
            'findings':[],'phase_evidence':refs,'attribution':'CODEX_DESIGNED_STEPS'}
        self.save('output',output);return output
