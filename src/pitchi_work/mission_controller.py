"""New-mission Host controller; finite profiles, bounded attempts, no V1 logic."""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from datetime import datetime
from .store import RunStore,load,dump,now,Cancelled,terminate_run_processes
from .model import LocalRoleAdapter,ModelFailure
from .shared_model import retained_mission
from .cloud import adapter_for,CloudControlStop
from .driver import BoundedFailure
from .runner import HostDefect
from .mission_profiles import PROFILES,UnsupportedMission,HumanApprovalRequired,visible,inventory,bind_requirements,compile_contracts,sha
from .code_validation import snapshot,validate

STABLE_WORKFLOW_VERSION = 2


def check_integration_assessment(value,ids,refs):
    """Validate exact Host identifiers/references; never invent judgments."""
    rows=value.get('criteria')
    got=[r.get('id') for r in rows if isinstance(r,dict)] if isinstance(rows,list) else []
    if not isinstance(rows,list) or len(rows)!=len(ids) or set(got)!=ids:
        raise ValueError('Missing criterion IDs: '+str(sorted(ids-set(got)))+'; expected exact IDs '+str(sorted(ids)))
    for row in rows:
        if type(row.get('met')) is not bool or not isinstance(row.get('reason'),str) or not isinstance(row.get('evidence'),list):
            raise ValueError('Required criterion fields: id, met, reason, evidence')
        if any(r not in refs for r in row['evidence']) or row['met'] and not row['evidence']:
            raise ValueError('Evidence must use exact reference strings, no descriptions. Allowed: '+str(sorted(refs)))
    return value


def validation_feedback(evidence):
    return {'checks':[{'validator':e['validator'],'pass':e['pass']} for e in evidence['checks'] if not e['pass']],
            'search_comparison':evidence.get('search_comparison'),
            'execution_failures':[{'argv':x['argv'][-1:],'exit_code':x['exit_code'],'error':x['stderr'][-700:]}
                                  for x in evidence['executions'] if x['exit_code']] if evidence.get('search_comparison') else [],
            'observations':[{k:v for k,v in x.get('web_observation',{}).items()
                             if k.endswith('observation') or k in {'errors','console_errors','network_failures'}}
                            for x in evidence['executions'] if x.get('web_observation')]}


def validation_feedback_from_run(run,last):
    """Bounded Host facts/links, never full previous Worker output or log history."""
    references=[]
    for name in ('artifacts/search_before.json','artifacts/search_pipeline_before.json'):
        path=run.path/name
        if path.is_file():references.append({'reference':name,'sha256':sha(path)})
    result={'previous_failure':[str(v)[:600] for v in last[-1:]],'host_evidence':references}
    pipeline=load(run.path/'artifacts/search_pipeline_before.json',{})
    result['pipeline_view']=[{'query':q['query'],'normalized_query':q['normalized_query'],
        'concepts':q['concepts'],'metadata_expansion':q['metadata_expansion'],'stages':q['stages'],
        'sources':{name:{'count':s['observed_provider_count'],'display_ids':s['display_ids'][:3]}
                   for name,s in q['sources'].items()},'final_ranking':q['final_ranking'][:3]}
        for q in pipeline.get('queries',[])[:1]]
    task_id=run.state().get('current_task')
    task=load(run.path/'tasks'/f'{task_id}.json',{}) if task_id else {}
    for attempt in reversed(task.get('attempts',[])):
        ref=attempt.get('host_evidence')
        if ref:
            path=(run.path/ref['reference']).resolve()
            if not path.is_relative_to(run.path.resolve()) or not path.is_file() or sha(path)!=ref['sha256']:
                raise HostDefect('Phase feedback evidence hash mismatch')
            value=load(path);result['previous_validation']={'evidence':ref,
                'checks':[c for c in value['checks'] if not c['pass']]}
            break
    return result


def create_mission(home,source,request,endpoint,model,parent=None,instruction=''):
    """Fresh immutable mission/copy. No deployment and no previous evidence edits."""
    home=Path(home).resolve();source=Path(source).resolve()
    if not source.is_dir() or not request.strip():raise ValueError('Workspace and nonempty request required')
    if len(request.encode())>8000:raise UnsupportedMission('Request exceeds the finite 8KB input budget; it will not be truncated')
    if source==Path('/') or home.is_relative_to(source):raise UnsupportedMission('Select a project directory, not a workspace containing Pitchi runtime')
    files=[];total=0
    for p in sorted(source.rglob('*')):
        name=p.relative_to(source).as_posix()
        if not visible(name):continue
        if p.is_symlink():raise UnsupportedMission('Symlinks require an explicit safe project copy: '+name)
        if not p.is_file():continue
        total+=p.stat().st_size
        if p.stat().st_size>50_000_000 or total>200_000_000:raise UnsupportedMission('Project copy exceeds 50MB/file or 200MB total')
        files.append((p,name))
    identifier=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    path=home/'runs'/identifier;workspace=home/'workspaces'/identifier
    if workspace.is_relative_to(source):raise UnsupportedMission('Working copy cannot be inside source')
    workspace.mkdir(parents=True);path.mkdir(parents=True)
    hashes={}
    for p,name in files:
        dest=workspace/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest);hashes[name]=sha(p)
    root=load(Path(parent)/'root_goal.json') if parent else {'request':request,'authority':'host-profile-registry-v1'}
    dump(path/'root_goal.json',root);(path/'request.md').write_text(root['request']);(path/'instruction.md').write_text(instruction)
    dump(path/'source_provenance.json',{'source_workspace':str(source),'source_hashes':hashes,'parent_run':str(parent) if parent else None})
    dump(path/'artifacts/host_baseline.json',snapshot(workspace))
    for name in hashes:
        dest=path/'baseline_files'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(workspace/name,dest)
    for name in ('tasks','reviews'): (path/name).mkdir()
    dump(path/'execution_context.json',{'active_workspace':str(workspace),'cwd':str(workspace),'artifact_base':str(workspace),
                                     'source_project_bindings':[{'source':str(source),'access':'read_only_provenance'}]})
    dump(path/'state.json',{'run_id':identifier,'status':'READY','stage':'requirement','workspace':str(workspace),
        'created_at':now(),'orchestration_version':'pitchi-profiles-v1','parent_run':str(parent) if parent else None,
        'root_goal_sha256':sha(path/'root_goal.json'),'local_model_endpoint':endpoint,'local_model':model,'active_processes':{}})
    previous_state=load(Path(parent)/'state.json',{}) if parent else {}
    # New missions can search private memory. External consultation never
    # propagates silently across child runs; approval is explicitly renewed.
    RunStore(path).save_state(resolution_policy={'memory': previous_state.get('resolution_policy',{}).get('memory',True)},
                             advisor_budget_id=previous_state.get('advisor_budget_id',identifier))
    if parent:
        previous=(Path(parent)/'instruction.md').read_text()
        if previous:
            (path/'instruction.md').write_text(previous+'\n'+instruction)
        dump(path/'parent_evidence.json',{'parent_run':str(parent),'terminal_state':load(Path(parent)/'state.json'),
             'task_records':[load(p) for p in sorted((Path(parent)/'tasks').glob('*.json'))],
             'completion':load(Path(parent)/'artifacts/request_completion_gate.json',{})})
    run=RunStore(path)
    configure_workflow(run)
    return run


def configure_workflow(run, research_handoff_version=STABLE_WORKFLOW_VERSION, procedure_references=None):
    """Stable Host generation for new runs; explicit old-generation replay only."""
    from .resolution import write_verified, read_verified
    from .procedural_memory import ProcedureStore
    if type(research_handoff_version) is not int or research_handoff_version not in (1,2):
        raise ValueError('Research handoff version must be 1 or 2')
    refs=list(procedure_references or [])
    if len(refs)>2 or len(set(refs))!=len(refs):raise ValueError('Select at most two unique reviewed procedures')
    if run.state()['status']!='READY' or any((run.path/p).exists() for p in ('requirements.json','plan.json','checkpoints')) or list((run.path/'artifacts').glob('role_call_*')):
        raise ValueError('Workflow selection requires a fresh unplanned run')
    store=ProcedureStore(run.path.parent.parent)
    for ref in refs:
        if not isinstance(ref,str) or len(ref)!=64 or any(c not in '0123456789abcdef' for c in ref):
            raise ValueError('Invalid reviewed procedure reference')
        path=store.path/(ref+'.json')
        if store.path.is_symlink() or path.is_symlink() or not path.is_file():raise ValueError('Missing or unsafe reviewed procedure')
        value=read_verified(path)
        store.select(value['profile'],[ref])
    write_verified(run.path/'workflow_selection.json',{'research_handoff_version':research_handoff_version,
        'requirement_protocol':2 if research_handoff_version==2 else 1,
        'procedure_mode':'auto' if procedure_references is None else 'explicit','procedure_references':refs})
    run.save_state(procedure_policy={'references':refs})
    print('Host workflow v'+str(research_handoff_version)+'; reviewed procedures '+('auto (max 2)' if procedure_references is None else str(len(refs))),flush=True)


class MissionController:
    def __init__(self,run):
        self.run=run;state=run.state();self.workspace=Path(state['workspace'])
        self.adapter=adapter_for(run,LocalRoleAdapter)

    def stage(self,name,**values):
        self.run.raise_if_cancelled();self.run.save_state(status='RUNNING',stage=name,**values)
        print('['+name+'] '+self.run.run_id,flush=True);self.run.trace(name,'RUN',name)

    def ask(self,role,payload,check,max_tokens=1800,output_schema=None):
        from .mission_profiles import output_rejection
        allowed={'response_schema':payload.get('response_schema',{}),
                 'paths':payload.get('inventory',{}).get('candidate_paths',
                    payload.get('requirements',{}).get('targets',[]))}
        if 'profiles' in payload:allowed['profiles']=list(payload['profiles'])
        feedback=[]
        for attempt in range(2):
            self.run.raise_if_cancelled()
            response=None
            try:
                response=self.adapter.ask(role,json.dumps({'input':payload,'host_feedback':feedback[-1:]},ensure_ascii=False),max_output_tokens=max_tokens,
                    **({'output_schema':output_schema} if output_schema is not None else {}))
                return response,check(response)
            except HumanApprovalRequired:raise  # Permission decisions are not output repair.
            except (ValueError,ModelFailure) as exc:
                # Includes UnsupportedMission raised while validating a model
                # proposal. Genuine Host preparation failures remain outside ask.
                if isinstance(exc,ModelFailure) and exc.failure_class not in {'AGENT_OUTPUT_INCOMPLETE','AGENT_OUTPUT_INVALID_JSON','AGENT_OUTPUT_INVALID_SHAPE'}:raise
                rejection=output_rejection(exc,allowed)
                if isinstance(response,dict):
                    rejection['previous_output']=response
                    rejection['retry_rule']+=' Preserve fields that already satisfy the unchanged contract in previous_output; correct the rejected fields and return the complete response, not a new interpretation of the mission.'
                feedback.append(rejection);self.run.event(role,'regenerate',attempt=attempt+1,error=str(exc),feedback=feedback[-1])
        self.run.event('host','output_retry_exhausted',target_role=role,limit=2)
        error=BoundedFailure(role.upper()+'_OUTPUT_INVALID','; '.join(f['required'] for f in feedback))
        error.output_retry_exhausted=True
        raise error

    def assert_immutable(self):
        if sha(self.run.path/'root_goal.json')!=self.run.state()['root_goal_sha256']:
            raise HostDefect('Immutable mission changed')
        provenance=load(self.run.path/'source_provenance.json');source=Path(provenance['source_workspace'])
        if any(not (source/n).is_file() or sha(source/n)!=h for n,h in provenance['source_hashes'].items()):
            raise HostDefect('Origin files changed during isolated execution')
        if self.run.state().get('recovery_parent_run_id'):
            receipt_path=self.run.path/'artifacts/recovery_receipt.json'
            claim=load(self.run.path.parent.parent/'recoveries'/
                       (self.run.state()['recovery_parent_run_id']+'.json'))
            if claim.get('child_run_id')!=self.run.run_id or sha(receipt_path)!=claim.get('receipt_sha256'):
                raise HostDefect('RECOVERY_RECEIPT_MISMATCH: recovery lineage changed')
            receipt=load(receipt_path)
            for transfer in receipt.get('target_transfers',[]):
                path=self.run.path/transfer['transfer']
                if not path.resolve().is_relative_to(self.run.path.resolve()) or sha(path)!=transfer['transfer_sha256']:
                    raise HostDefect('RECOVERY_TARGET_TRANSFER_MISMATCH')
            if {t['task_id']:t['sha256'] for t in load(self.run.path/'plan.json')['tasks']}!=receipt['contracts']:
                raise HostDefect('RECOVERY_CONTRACT_MISMATCH: saved contracts changed')
            from .resolution import read_verified
            if read_verified(self.run.path/'workflow_selection.json')['research_handoff_version']!=receipt['workflow_version']:
                raise HostDefect('RECOVERY_VERSION_MISMATCH: saved workflow changed')
            binding=load(self.run.path/'search_binding.json',None)
            if binding and sha(Path(binding['database']))!=binding['database_sha256']:
                raise HostDefect('RECOVERY_DATABASE_MISMATCH: snapshot changed')

    def setup(self):
        self.assert_immutable();request=load(self.run.path/'root_goal.json')['request']
        if (self.run.path/'search_binding.json').exists():
            from .search_quality import search_inventory,observe,preflight
            catalog=search_inventory(self.run)
            if not (self.run.path/'artifacts/search_before.json').exists():
                self.stage('host_before_search');observe(self.run,'before')
            if not (self.run.path/'artifacts/search_baseline_tests.json').exists():
                self.stage('host_baseline_tests');preflight(self.run)
        else:catalog=inventory(self.workspace)
        if (self.run.path/'guided_edit_plan.json').exists():
            from .resolution import read_verified
            catalog['guided_edits']=read_verified(self.run.path/'guided_edit_plan.json')
        from .resolution import read_verified
        selection=read_verified(self.run.path/'workflow_selection.json') if (self.run.path/'workflow_selection.json').exists() else {'research_handoff_version':1}
        intent_protocol=selection.get('requirement_protocol',1)
        if intent_protocol not in (1,2):raise HostDefect('Unsupported saved Requirement protocol; no silent fallback')
        if not (self.run.path/'requirements.json').exists():
            self.stage('requirement')
            def bind(value):
                from .mission_profiles import normalize_request_spans,bind_requirement_intent
                if intent_protocol==2:result=bind_requirement_intent(value,request,catalog)
                else:
                    value=normalize_request_spans(value,request,(self.run.path/'instruction.md').read_text())
                    result=bind_requirements(value,request,catalog)
                context=load(self.run.path/'artifact_input.json',{})
                if context:
                    if result['profile']!='static_web_app':raise ValueError('Registered Web artifact requires its existing static_web_app profile')
                    prior=context.get('required_regression_behavior')
                    allowed={'arithmetic_practice':{'arithmetic_practice','arithmetic_practice_review'},
                             'arithmetic_practice_review':{'arithmetic_practice_review'}}
                    if prior in allowed and result.get('behavior') not in allowed[prior]:raise ValueError('Retain existing STRICT regression behavior: '+prior)
                return result
            if intent_protocol==2:
                from .mission_profiles import requirement_intent_schema,requirement_candidates
                schema=requirement_intent_schema(catalog)
                profiles=[s['properties']['profile']['enum'][0] for s in schema['oneOf']]
                candidates={}
                for name in profiles:
                    try:candidates[name]=requirement_candidates(name,catalog)
                    except UnsupportedMission:candidates[name]=[]
                proposal,requirements=self.ask('requirement',{'original_request':request,
                    'additional_instruction':(self.run.path/'instruction.md').read_text(),
                    'artifact_input':load(self.run.path/'artifact_input.json',None),
                    'profiles':{p:PROFILES[p] for p in profiles},
                    'inventory':{'candidate_paths':list(dict.fromkeys(p for rows in candidates.values() for p in rows)),
                                 'candidates_by_profile':candidates,'protected_tests':catalog['tests']},
                    'response_schema':schema,
                    'rules':'Requirement understands intent only. Return profile and needs_human; also registered behavior for static_web_app. Do not choose edit files or copy/rewrite requirements. Host keeps the ENTIRE immutable original request as M1; Planner selects bounded ownership from Host candidates, Worker investigates exact edits later. Protected tests may be read, never edited. External/publishing/destructive/credential/paid/outside-workspace work requires human approval. No new commands, validators or acceptance.'},
                    bind,max_tokens=500,output_schema=schema)
                dump(self.run.path/'artifacts/requirement_receipt.json',{'protocol_version':2,
                    'request_sha256':sha(self.run.path/'root_goal.json'),
                    'authority':'Host immutable original request and candidate scope',
                    'model_proposal':proposal,'ignored_advisory_fields':[k for k in ('targets','requirements') if k in proposal],
                    'target_candidates':requirements['targets'],'write_authority':'Planner contract, not Requirement suggestions'})
            else:
                _,requirements=self.ask('requirement',{'original_request':request,'additional_instruction':(self.run.path/'instruction.md').read_text(),
                'artifact_input':load(self.run.path/'artifact_input.json',None),
                'profiles':({'php_search_quality':PROFILES['php_search_quality']} if catalog.get('search_binding') else {k:v for k,v in PROFILES.items() if k!='php_search_quality'}),'inventory':{'candidate_paths':[r['path'] for r in catalog['files']],
                    'existing_tests':catalog['tests']},'response_schema':{'profile':'registered name or unsupported',
                'targets':['exact inventory paths OR registered static_web_app output_candidates, maximum 3'],
                **({} if catalog.get('search_binding') else {'behavior':'REQUIRED for static_web_app: registered behavior; arithmetic_practice_review for later review of incorrect answers; OMIT otherwise'}),
                'requirements':['exact spans of original_request, maximum 4'],
                'needs_human':False},'rules':'Select only a supported profile. External publishing, credentials, payments, deletion, dependency installation and outside-workspace changes need human approval. Mark needs_human=true; never execute them. Python editing uses existing implementation files and protected tests. Browser apps use static_web_app, targets exactly index.html/app.js/style.css. Select arithmetic_practice_review for later practice of incorrect answers, arithmetic_practice for other multiplication exercises, otherwise interactive_smoke. No dependencies or external services.'},
                    bind)
            instruction=(self.run.path/'instruction.md').read_text()
            if instruction:requirements['requirements'].append({'id':'I0','text':instruction})
            dump(self.run.path/'requirements.json',requirements);self.run.save_state(profile=requirements['profile'])
            self.run.handoff('requirement','planner',['root_goal.json','requirements.json'])
        requirements=load(self.run.path/'requirements.json')
        if selection.get('procedure_mode')=='auto':
            from .procedural_memory import ProcedureStore
            pin=self.run.path/'procedure_selection.json'
            if pin.exists():refs=read_verified(pin)['references']
            else:
                refs=ProcedureStore(self.run.path.parent.parent).automatic(requirements['profile'])
                from .resolution import write_verified
                write_verified(pin,{'references':refs,'profile':requirements['profile'],'authority':'host-reviewed-profile-match'})
            self.run.save_state(procedure_policy={'references':refs})
        elif 'procedure_references' in selection and selection['procedure_references']!=self.run.state().get('procedure_policy',{}).get('references',[]):
            raise HostDefect('Reviewed procedure selection changed')
        if self.run.state().get('procedure_policy'):
            from .procedural_memory import ProcedureStore
            ProcedureStore(self.run.path.parent.parent).select(requirements['profile'],self.run.state()['procedure_policy']['references'])
        if not (self.run.path/'plan.json').exists():
            self.stage('planner')
            coarse,plan=self.ask('planner',{'requirements':requirements,
                **({'guided_edit_paths':list(catalog['guided_edits']['source_hashes']),
                    'guided_edit_rules':'Explicit user-approved steps: one task must own all guided_edit_paths. Host keeps scope and acceptance unchanged.'} if catalog.get('guided_edits') else {}),
                **({'original_request_reference':{'reference':'root_goal.json','sha256':sha(self.run.path/'root_goal.json')},
                    'request_authority':'M1 contains the entire original request verbatim; no duplicate body is needed'}
                   if requirements.get('binding_version')==2 else {'original_request':request}),
                'response_schema':{'tasks':[{'requirement_ids':['selected M IDs'],'targets':['selected paths'],'depends_on':['prior zero-based indices']}]},
                'rules':'Select only from the supplied candidates, cover all IDs, at most four tasks. No formal contracts, tests or completion conditions. Prefer one task for related edits; write ownership cannot overlap.'},
                lambda r:compile_contracts(r,requirements,catalog,research_handoff_version=selection['research_handoff_version']))
            dump(self.run.path/'coarse_plan.json',coarse);dump(self.run.path/'plan.json',plan)
            for task in plan['tasks']:dump(self.run.path/'tasks'/f"{task['task_id']}.json",{'task':task,'status':'PENDING','attempts':[]})
            self.run.handoff('planner','worker',['plan.json'],authority='host-profile-registry-v1')
        plan=load(self.run.path/'plan.json')
        capabilities=['reference_handoff','working_context','evidence_retrieval','hypothesis_grounding','edit_protocol','source_navigation','receipt_review','host_validation']
        active=capabilities if any(t.get('phase_policy',{}).get('version')==2 for t in plan['tasks']) else ['host_validation']
        receipt={'workflow_version':selection['research_handoff_version'],'requirement_protocol':intent_protocol,'effective_task_versions':{t['task_id']:t.get('phase_policy',{}).get('version') for t in plan['tasks']},
            'capabilities':active,'knowledge_references':self.run.state().get('procedure_policy',{}).get('references',[]),'knowledge_delivery':'hypothesis/edit only','authority':'host'}
        dump(self.run.path/'artifacts/workflow_receipt.json',receipt)
        self.run.event('host','workflow_active',**receipt)
        print('Host capabilities: '+', '.join(active)+'; knowledge: '+', '.join(receipt['knowledge_references']),flush=True)

    def task(self,contract):
        path=self.run.path/'tasks'/f"{contract['task_id']}.json";record=load(path)
        if record['status']=='PASS':return
        from .artifact_jobs import diagnose,summary
        if load(self.run.path/'artifact_input.json',{}):
            self.stage('artifact_diagnosis')
            diagnose(self.run,contract)
        last=[record['attempts'][-1].get('error','')] if record['attempts'] and record['attempts'][-1].get('state')=='FAILED' else []
        pending=record['attempts'][-1] if record['attempts'] and record['attempts'][-1].get('state') in {'STARTED','OUTPUT_SAVED'} else None
        first=pending['attempt'] if pending else len(record['attempts'])+1
        for attempt in range(first,contract['max_attempts']+1):
            self.stage('worker',current_task=contract['task_id'])
            files={n:(self.workspace/n).read_text() for n in contract['allowed_paths'] if (self.workspace/n).is_file()}
            if contract['profile']=='code_investigation':files={n:(self.workspace/n).read_text() for n in contract['input_files']}
            prompt_files=files
            if contract['profile']=='php_search_quality' and not contract.get('phase_policy'):
                from .search_quality import source_fragments
                for name in contract['allowed_paths']:files.setdefault(name,'')
                prompt_files=source_fragments(files)
            worker_payload={'original_request':load(self.run.path/'root_goal.json')['request'],
                'additional_instruction':(self.run.path/'instruction.md').read_text(),'contract':contract,'source_files':prompt_files,
                'artifact_input':load(self.run.path/'artifact_input.json',None),'prior_diagnosis':summary(self.run),
                'artifact_rules':'Preserve working source and original regression behavior. Repair only evidenced defects; when no defect exists return files:{} and report the verified scope, do not invent bugs. Clone must visibly differ: change document title, retaining behavior. arithmetic_practice_review adds #review button and #review-count with data-count: wrong answers enter a deduplicated queue, survive next, review returns queued operands later, correct reviewed answers leave queue and next resets. Ordinary practice remains available.',
                'failure':last,'web_runtime':{'rules':'Static web: index.html loads app.js and style.css, no external assets or inline scripts. Implement the chosen behavior using Host ui_contract element IDs. arithmetic_practice: show current numeric operands, input numeric answer, submit grades correct/incorrect and feedback has data-result=correct/incorrect. next advances to another problem; repeated attempts work; empty/invalid input never correct. Include friendly Japanese labels, keyboard access and mobile layout. interactive_smoke: answer input, submit, nonempty feedback and next reset. No generated tests. Enjoyment is for human review, not a proven fact.'} if contract['profile']=='static_web_app' else None,
                'search_context':__import__('pitchi_work.search_quality',fromlist=['worker_context']).worker_context(self.run) if contract['profile']=='php_search_quality' else None,
                'rules':('Return {edits:[{path,source_sha256,span_id,before,after}],summary:string,findings:[]}. Select exact supplied paths/hashes/spans. Before must be unique WITHIN the span, at most 350 characters. After at most 700 characters; at most 4 edits. Use small changes, not full methods. The mandatory NEW regression file uses span_id=output,before="",after=COMPLETE PHP below 1000 characters, at most 3 behavior assertions, bootstrap before classes. No boilerplate or source-string assertions. Read omitted code first: {read_spans:[{path,span_id}]} selecting ONE available span below 8000 bytes; at most two reads. Preserve protected test behavior, including tested relaxation of phase/setting modifiers. Independent MAIN concepts are joint requirements; aliases of one concept are alternatives. Use observed concept roles, not unconditional AND across aliases or all modifiers. Do not ignore unrecognized subject terms. No hard-coded query/result, guessed code, commands, deployment, contract changes or self-PASS. Supplementary tests are not formal acceptance.' if contract['profile']=='php_search_quality' else
                    'Return {files:{allowed exact path:COMPLETE content}, summary:string, findings:[{path,line,quote,explanation}]}. For investigation files must be {} and findings must cite exact existing source lines. No commands, contract edits, dependencies, secrets, deployment or tests. Do not claim PASS.')} if not contract.get('phase_policy') else None
            if contract['profile']=='php_search_quality' and not contract.get('phase_policy'):
                from .search_quality import compact_worker_payload, worker_schema
                worker_payload=compact_worker_payload(worker_payload)
            worker_options={'output_schema':worker_schema(files,contract['allowed_paths']),
                            'enable_thinking':True} if contract['profile']=='php_search_quality' and not contract.get('phase_policy') else {}
            worker_tokens=3200 if contract['profile']=='php_search_quality' else 5000
            if not pending:
                record['attempts'].append({'attempt':attempt,'state':'STARTED','worker_output_ref':f'artifacts/{contract["task_id"]}_worker_{attempt}.json'})
                dump(path,record)  # Journal BEFORE dispatch, including incomplete outputs.
            journal=None
            try:
                if contract.get('phase_policy'):
                    from .phase_workflow import PhaseWorker
                    saved_feedback=(self.run.state().get('local_repair_budget') or self.run.state().get('recovery_parent_run_id') and
                        (self.run.path/'checkpoints'/contract['task_id']/str(attempt)/'working_observations.json').exists())
                    journal=PhaseWorker(self.run,contract,self.adapter,attempt,
                        None if saved_feedback else validation_feedback_from_run(self.run,last))
                    output=journal.execute()
                    record['attempts'][-1]['phase_checkpoint']=str(journal.base.relative_to(self.run.path))
                elif pending and pending['state']=='OUTPUT_SAVED':
                    output=load(self.run.path/pending['worker_output_ref'])
                else:
                    output=self.adapter.ask('worker',json.dumps(worker_payload,ensure_ascii=False,separators=(',',':')),max_output_tokens=worker_tokens,**worker_options)
            except ModelFailure as exc:
                record['attempts'][-1].update(state='FAILED',failure_class=exc.failure_class,error=str(exc))
                dump(path,record)
                raise  # Bounded phase repair is already accounted; do not silently loop.
            dump(self.run.path/record['attempts'][-1]['worker_output_ref'],output)
            record['attempts'][-1]['state']='OUTPUT_SAVED';dump(path,record)
            pending=None
            try:
                if contract['profile']=='php_search_quality' and journal is None:
                    from .search_quality import read_spans
                    for read_turn in range(2):
                        if 'read_spans' not in output:break
                        dump(self.run.path/'artifacts'/f'{contract["task_id"]}_read_{attempt}_{read_turn}.json',output)
                        worker_payload['source_files']=read_spans(output['read_spans'],files,contract['allowed_paths'])
                        worker_payload=compact_worker_payload(worker_payload)
                        output=self.adapter.ask('worker',json.dumps(worker_payload,ensure_ascii=False,separators=(',',':')),max_output_tokens=worker_tokens,**worker_options)
                    if 'read_spans' in output:raise ValueError('Bounded source read limit reached')
                    dump(self.run.path/record['attempts'][-1]['worker_output_ref'],output)
                selected=output.get('files')
                if contract['profile']=='php_search_quality' and journal is None:
                    from .search_quality import patched_files
                    selected=patched_files(output,files,contract['allowed_paths'],
                        [n for n in contract['allowed_paths'] if not (self.run.path/'baseline_files'/n).exists()])
                if not isinstance(selected,dict) or any(n not in contract['allowed_paths'] or not isinstance(text,str) for n,text in selected.items()):
                    raise ValueError('Worker may return only complete allowed files')
                for name,text in selected.items():
                    dest=self.workspace/name
                    if contract['profile']=='php_search_quality' and (dest.read_text() if dest.exists() else '')!=files.get(name,''):
                        raise ValueError('Workspace changed after source read; refusing overwrite: '+name)
                    if dest.is_symlink() or any(p.is_symlink() for p in dest.parents if p.is_relative_to(self.workspace)) or not dest.resolve().is_relative_to(self.workspace):raise ValueError('Unsafe Worker path')
                    dump(self.run.path/'artifacts'/f'{contract["task_id"]}_before_{attempt}.json',files)
                    from .host_validators import save_bytes
                    self.run.raise_if_cancelled();save_bytes(dest,text.encode())
                self.stage('host_validation')
                validation_attempt=attempt if journal is None else str(attempt)+'_phase_'+str(len(list(journal.base.glob('host_validation_*.receipt.json'))))
                evidence=validate(self.run,contract,validation_attempt,output)
                if journal:
                    journal.save('host_validation_'+str(len(list(journal.base.glob('host_validation_*.receipt.json')))),
                                 {'reference':evidence['reference'],'sha256':evidence['sha256'],
                                  'passed':evidence['passed'],'checks':evidence['checks']})
                record['attempts'][-1]['host_evidence']={'reference':evidence['reference'],'sha256':evidence['sha256']}
                if not evidence['passed']:
                    failed=next(c['validator'] for c in evidence['checks'] if not c['pass'])
                    raise BoundedFailure('HOST_PROFILE_'+failed.upper()+'_FAIL',json.dumps(validation_feedback(evidence),ensure_ascii=False))
                self.stage('reviewer')
                def review(value):
                    if set(value)!={'verdict','reason'} or value['verdict'] not in ('PASS','REWORK') or not isinstance(value['reason'],str):
                        raise ValueError('Reviewer must return verdict PASS/REWORK and reason')
                    return value
                review_contract=contract
                changed_sources=__import__('pitchi_work.search_quality',fromlist=['source_fragments']).source_fragments({n:(self.workspace/n).read_text() for n in contract['expected_outputs']}) if contract['profile']=='php_search_quality' else {n:(self.workspace/n).read_text() for n in (contract['expected_outputs'] or contract['input_files'])}
                if contract.get('guided_edits'):
                    # Host keeps full sources/teaching packet. Reviewer compares
                    # measured diff, exact hashes and per-step requirements,
                    # not duplicate before/after code embedded in the contract.
                    review_contract={**contract,'guided_edits':{'reference':'guided_edit_plan.json',
                        'sha256':sha(self.run.path/'guided_edit_plan.json'),
                        'steps':[{k:s[k] for k in ('id','instruction','local_check')} for s in contract['guided_edits']['steps']]}}
                    changed_sources={'actual_host_diff':evidence['diff'],'artifact_hashes':evidence['artifact_hashes'],
                        'full_sources_retained':'Host workspace and hash-verified phase output; this diff is not a full-source view'}
                assessment,_=self.ask('reviewer',{'original_request':load(self.run.path/'root_goal.json')['request'],
                    'requirements':load(self.run.path/'requirements.json'),'contract':review_contract,'host_evidence':{k:v for k,v in evidence.items() if k in {'reference','sha256','checks','passed','artifact_hashes','search_comparison'}} if contract['profile']=='php_search_quality' else evidence,
                    'changed_sources':changed_sources,
                    'rules':'Judge whether machine responsibilities in the original request and assigned spans are met using Host observations and actual sources. Worker claims/tests are not acceptance. Subjective enjoyment/design preferences are not provable: defer to human use, do not require their automatic certification. Return verdict PASS or REWORK, reason.'},review)
                dump(self.run.path/'reviews'/f'{contract["task_id"]}_{attempt}.json',assessment)
                if assessment['verdict']!='PASS':raise BoundedFailure('REVIEWER_REWORK',assessment['reason'])
                record['attempts'][-1]['state']='PASS';record['status']='PASS';dump(path,record);return
            except (ValueError,BoundedFailure) as exc:
                kind=getattr(exc,'failure_class','WORKER_OUTPUT_INVALID')
                last=[str(exc)];record['attempts'][-1]['state']='FAILED';record['attempts'][-1]['failure_class']=kind;record['attempts'][-1]['error']=str(exc)
                dump(path,record)
                # Same failure class twice is a safety stop, not an infinite retry.
                if len(record['attempts'])>=2 and all(a.get('failure_class')==kind for a in record['attempts'][-2:]):
                    raise BoundedFailure(kind,str(exc))
        raise BoundedFailure('TASK_ATTEMPT_LIMIT','Task attempt limit reached')

    def finish(self):
        self.stage('host_root_acceptance');records=[load(p) for p in sorted((self.run.path/'tasks').glob('*.json'))]
        if any(r['status']!='PASS' for r in records):raise HostDefect('Host root acceptance requires all tasks PASS')
        checks=[validate(self.run,r['task'],'root',load(self.run.path/r['attempts'][-1]['worker_output_ref'])) for r in records]
        dump(self.run.path/'artifacts/root_evidence.json',checks)
        if any(not r['passed'] for r in checks):raise BoundedFailure('ROOT_ACCEPTANCE_FAILURE','Final artifacts fail the Host registry')
        self.stage('integrator')
        requirements=load(self.run.path/'requirements.json');ids={r['id'] for r in requirements['requirements']}|{'R0'}
        # Integrator sees measured facts and hashed references, not full test
        # logs/diffs or Worker outputs. The unabridged Host receipts stay saved.
        view=[]
        for record,receipt in zip(records,checks):
            review=sorted((self.run.path/'reviews').glob(record['task']['task_id']+'_*.json'))[-1]
            if load(review).get('verdict')!='PASS':raise HostDefect('Reviewer PASS receipt missing')
            view.append({'task_id':record['task']['task_id'],'requirement_ids':record['task']['requirement_ids'],
                'reference':receipt['reference'],'sha256':receipt['sha256'],'passed':receipt['passed'],
                'checks':receipt['checks'],'artifact_hashes':receipt['artifact_hashes'],
                'search_comparison':receipt.get('search_comparison'),
                'changed_paths':[n for n,d in receipt['diff'].items() if d],
                'existing_test_results':[{'exit_code':t['exit_code'],'tests_run':t['tests_run']} for t in receipt['executions']],
                'reviewer':{'verdict':'PASS','reason':load(review)['reason'],'reference':str(review.relative_to(self.run.path)),'sha256':sha(review)}})
        packet={'original_request':load(self.run.path/'root_goal.json')['request'],'requirements':requirements,
            'required_criterion_ids':sorted(ids),'allowed_evidence_references':[r['reference'] for r in checks],
            'response_schema':{'criteria':[{'id':'each required_criterion_id EXACTLY once, INCLUDING R0',
                                           'met':'boolean','reason':'brief judgment','evidence':['exact member of allowed_evidence_references, not an explanation']}]},
            'root_obligation':'R0 means the original request, not merely task completion','host_evidence':view,
            'evidence_authority':'Host-owned registry; full details remain in hash-verified referenced receipts',
            'rules':'Return {criteria:[{id,met:boolean,reason:string,evidence:[Host reference]}]}. Compare original request and Host measurements/Reviewer evidence. met means machine responsibilities completed, not subjective enjoyment guaranteed. Usability is assisted, enjoyment human-subjective. Existing tests/syntax alone do not prove behavior. Negative decisions remain negative; do not invent success.'}
        dump(self.run.path/'artifacts/integration_packet.json',packet)
        self.run.handoff('reviewer','integrator',['artifacts/integration_packet.json','artifacts/root_evidence.json'])
        def assessment(value):
            return check_integration_assessment(value,ids,{r['reference'] for r in checks})
        result,_=self.ask('integrator',packet,assessment,max_tokens=2200)
        dump(self.run.path/'artifacts/integrator_assessment.json',result)
        self.assert_immutable()
        evidence_hashes=all(sha(self.run.path/r['reference'])==r['sha256'] and
            all(sha(self.workspace/n)==h for n,h in r['artifact_hashes'].items()) for r in checks)
        complete=evidence_hashes and all(r['met'] is True for r in result['criteria']) and all(r['passed'] for r in checks)
        decision={'status':'COMPLETED' if complete else 'PARTIAL','root_goal_sha256':self.run.state()['root_goal_sha256'],
                  'criteria':result['criteria'],'host_evidence_hashes_verified':evidence_hashes,
                  'requirement_task_bindings':{q['id']:[r['task']['task_id'] for r in records if q['id'] in r['task']['requirement_ids']] for q in requirements['requirements']}}
        preview=None
        if complete and self.run.state().get('profile')=='static_web_app':
            from .artifacts import prepare_completed_run
            self.stage('artifact_preview')
            preview=prepare_completed_run(self.run,checks)
            dump(self.run.path/'artifacts/human_preview.json',preview)
            if (self.run.path/'cancel_request.json').exists():
                from .artifacts import operation
                if (self.run.path/'artifact_input.json').exists():
                    from .artifact_jobs import rollback_publication
                    rollback_publication(self.run.path.parent.parent,self.run,preview['artifact_id'])
                else:operation(self.run.path.parent.parent,'stop',preview['artifact_id'])
                self.run.raise_if_cancelled()
        dump(self.run.path/'artifacts/request_completion_gate.json',decision)
        self.run.save_state(status=decision['status'],stage='idle',completion=decision,human_preview=preview)

    @retained_mission
    def execute(self):
        if self.run.state().get('recovery_pending'):
            raise HostDefect('RECOVERY_NOT_VERIFIED: unfinished recovery cannot execute')
        if self.run.state()['status'] in self.run.TERMINAL:raise ValueError('Terminal run is immutable; use continue')
        lock=self.run.path/'controller.lock';lock.touch(exist_ok=True)
        with lock.open() as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('Controller is already running')
            try:
                self.setup()
                for contract in load(self.run.path/'plan.json')['tasks']:
                    if any(load(self.run.path/'tasks'/f'{d}.json')['status']!='PASS' for d in contract['prerequisite_tasks']):raise HostDefect('Unsatisfied dependency')
                    self.task(contract)
                self.finish()
            except Cancelled:
                terminate_run_processes(self.run)
            except CloudControlStop as exc:
                self.run.save_state(status='NEEDS_HUMAN',stage='idle',failure_class=exc.failure_class,
                                    failure_owner='policy',stop_reason=str(exc))
            except (UnsupportedMission,HostDefect,BoundedFailure,ModelFailure) as exc:
                status='NEEDS_HUMAN' if isinstance(exc,HumanApprovalRequired) else 'UNSUPPORTED_MISSION' if isinstance(exc,UnsupportedMission) else 'STALLED'
                from .capability_advisor import shortage, advise, approval_pending
                human_stop=approval_pending(self.run,exc)
                if human_stop:status='NEEDS_HUMAN'
                blocker=shortage(self.run,exc)
                if blocker:
                    stopped_stage=self.run.state().get('stage')
                    blocker['stopped_stage']=stopped_stage
                    self.run.save_state(stage='capability_advisor')
                    try:
                        advice=advise(self.run,blocker,self.adapter)
                    except Cancelled:
                        terminate_run_processes(self.run)
                        return self.run.state()
                    except Exception as advisor_error:
                        # Analysis failure must not erase the observed shortage.
                        advice={'analysis_status':'UNAVAILABLE','error_type':type(advisor_error).__name__}
                    self.run.save_state(capability_advice=advice)
                    status='NEEDS_CAPABILITY'
                self.run.save_state(status=status,stage='idle',failure_class='CAPABILITY_GAP' if blocker else 'HUMAN_APPROVAL_REQUIRED' if human_stop else exc.failure_class,
                    failure_owner='capability' if blocker else 'policy' if human_stop else 'host' if isinstance(exc,HostDefect) else 'policy' if isinstance(exc,UnsupportedMission) else 'agent',
                    stop_reason=blocker['reason'] if blocker else 'Explicit human intervention required; no operation executed. Original symptom: '+str(exc) if human_stop else str(exc))
            except Exception as exc:
                self.run.save_state(status='STALLED',stage='idle',failure_class='HOST_CONTROLLER_DEFECT',stop_reason=type(exc).__name__+': '+str(exc))
            from .resolution import finalize_resolution
            finalize_resolution(self.run)
        return self.run.state()
