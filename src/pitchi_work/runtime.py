"""Independent Host authority boundaries, not a normal-use launcher.

The book-QA profile and original requirements must be explicitly supplied.
No unregistered task kinds, contracts, validators or shell commands are accepted.
"""
import hashlib
import json
from pathlib import Path
from .store import load,dump
from .host_contracts import authority,STRATEGIES
from .compiler import compile_retaining
from .request_requirements import validate_requirements
from .completion import validate_completion
from .integration_packet import build_packet,fit_messages,IntegratorContextDefect
from .assessment_prompt import request_assessment_prompt
from .runner import execute_registered,HostDefect
from .execution_context import context,assert_workspace
from .policy import WORKER_MAX_RETRIES,TASK_REPAIR_LIMIT,ROOT_REPAIR_LIMIT,SAME_FAILURE_LIMIT


class HostCore:
    def __init__(self,run):self.run=run

    def requirement(self,acknowledgement):
        root=load(self.run.path/'root_goal.json');requirements=load(self.run.path/'requirements.json')
        if root['requirements']!=requirements or root['request']!=(self.run.path/'request.md').read_text():
            raise HostDefect('Immutable original request/requirements changed')
        if hashlib.sha256((self.run.path/'root_goal.json').read_bytes()).hexdigest()!=self.run.state()['root_goal_sha256']:
            raise HostDefect('Root goal hash changed')
        validate_requirements(requirements)
        expected={r['id'] for r in requirements['request_completion_requirements']}
        if acknowledgement.get('decision')!='proceed' or set(acknowledgement.get('acknowledged_ids',[]))!=expected:
            raise ValueError('Requirement acknowledgement omitted frozen obligations')
        return root

    def planner(self,coarse,candidates=()):
        root=load(self.run.path/'root_goal.json')
        return compile_retaining(coarse,root['requirements'],context(self.run)['active_workspace'],candidates)

    def worker(self,task,output):
        """Apply files only, never Worker commands/tests/verdict/state/contract."""
        self.run.raise_if_cancelled()
        if task['host_contract_sha256']!=authority(task):raise HostDefect('Formal contract changed')
        workspace=Path(context(self.run)['active_workspace']);assert_workspace(self.run,workspace)
        if any(k in output for k in ('contract','test_contract','validators','acceptance_criteria','status')):
            raise ValueError('Worker cannot change Host authority')
        files=output.get('files')
        if not isinstance(files,dict):raise ValueError('Worker files must be an object')
        destinations=[]
        for name,content in files.items():
            original=workspace/name
            if original.is_symlink() or any(p.is_symlink() for p in original.parents if p.is_relative_to(workspace)):
                raise ValueError('Worker symlink write forbidden')
            path=original.resolve()
            if name not in task['allowed_paths'] or not path.is_relative_to(workspace) or not isinstance(content,str):
                raise ValueError('Worker write outside formal outputs')
            destinations.append((path,content))
        # Immutable attempt snapshot precedes any write; no prior PASS is rewritten.
        record=load(self.run.path/'tasks'/f"{task['task_id']}.json",{})
        if record.get('status')=='PASS':raise ValueError('PASS task requires an explicit new repair revision')
        revision=record.get('plan_revision',0)
        revision_attempts=sum(a.get('plan_revision',0)==revision for a in record.get('attempts',[]))
        if revision_attempts>=WORKER_MAX_RETRIES+1:raise ValueError('Worker attempt bound reached')
        number=len(record.get('attempts',[]))+1
        folder=self.run.path/'artifacts'/f"{task['task_id']}_attempt_{number}"
        before={p.relative_to(workspace).as_posix():p.read_text() if p.is_file() else None for p,_ in destinations}
        dump(folder/'before.json',before);dump(folder/'worker_output.json',output)
        for path,content in destinations:
            self.run.raise_if_cancelled();path.parent.mkdir(parents=True,exist_ok=True)
            from .host_validators import save_bytes
            save_bytes(path,content.encode())
        record.setdefault('attempts',[]).append({'attempt':number,'plan_revision':revision,'changed_files':list(files),'output_ref':str((folder/'worker_output.json').relative_to(self.run.path))})
        dump(self.run.path/'tasks'/f"{task['task_id']}.json",record)
        return list(files)

    def validation(self,task):
        try:return execute_registered(self.run,task)
        except HostDefect as exc:
            self.run.save_state(status='STALLED',failure_class=exc.failure_class,failure_owner='host',stop_reason=str(exc))
            raise  # NEVER return a Worker retry decision for a Host failure.

    @staticmethod
    def reviewer(task,tests,assessment):
        from .host_evidence import observed_kinds
        if task['host_contract_sha256']!=authority(task):raise HostDefect('Formal contract changed')
        needed=set(task['test_contract']['evidence_types'])-{'behavior','artifact'}
        observed=set().union(*(observed_kinds(task,t) for t in tests)) if tests else set()
        host_pass=bool(tests) and all(t.get('exit_code')==0 for t in tests) and needed<=observed
        if any(t.get('registry_result',{}).get('failure_owner')=='host' for t in tests):
            raise HostDefect('Host failure cannot enter Worker rework')
        return 'PASS' if host_pass and assessment.get('verdict')=='PASS' else 'REWORK'

    def repair_strategy(self,task,record,choice):
        """A finite strategy can change implementation, never contract/criteria."""
        if record.get('plan_revision',0)>=TASK_REPAIR_LIMIT:raise ValueError('Host task repair bound reached')
        if set(choice)!={'strategy'} or choice['strategy'] not in STRATEGIES:raise ValueError('Invalid finite recovery choice')
        return {**record,'plan_revision':record.get('plan_revision',0)+1,'repair_strategy':choice['strategy']}

    def integrator_inputs(self,evidence):
        packet=build_packet(self.run,evidence)
        batches=[request_assessment_prompt(evidence,start,packet) for start in range(0,len(evidence['obligations']),8)]
        for _,prompt,_ in batches:self.integrator_messages(evidence,prompt)
        return packet,batches

    @staticmethod
    def integrator_messages(evidence,prompt):
        suffix='\nHOST EXECUTION CONTEXT (authoritative for all roles; project origin is read-only provenance, NEVER test cwd/import root): '+json.dumps(evidence['execution_context'],ensure_ascii=False)
        messages=[{'role':'system','content':'You are the integrator role. Think briefly. Return only the requested JSON; do not use markdown fences.'},
                  {'role':'user','content':prompt+suffix}]
        stats=fit_messages(messages,2600)
        if not stats['within_budget']:raise IntegratorContextDefect('Integrator envelope exceeds frozen 80% budget')
        return messages,stats

    def root_acceptance(self,records):
        # No model-generated root code; rerun only registered Host contracts.
        if not records or any(r['status']!='PASS' for r in records):raise ValueError('All tasks must PASS before root acceptance')
        return [self.validation(r['task']) for r in records]

    @staticmethod
    def completion(assessment,evidence):
        errors=validate_completion(assessment,evidence)
        if not evidence.get('tasks') or any(r.get('status')!='PASS' for r in evidence['tasks']):
            errors.append('Every task must have a Host-backed Reviewer PASS')
        if not evidence.get('final_tests') or any(t.get('exit_code')!=0 for t in evidence['final_tests']):
            errors.append('Host final/root acceptance failed')
        completed=not errors and assessment.get('verdict')=='COMPLETED'
        return {'root_goal_sha256':evidence.get('root_goal_sha256'),'errors':errors,'request_completed':completed,
                'status':'COMPLETED' if completed else 'PARTIAL'}
