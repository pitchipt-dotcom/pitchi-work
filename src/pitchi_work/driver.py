"""Independent foreground controller. The CLI cannot certify any result."""
import fcntl
import hashlib
import json
from pathlib import Path
from .store import load,dump,Cancelled,terminate_run_processes
from .runtime import HostCore
from .runner import HostDefect
from .model import LocalRoleAdapter,ModelFailure
from .shared_model import retained_mission
from .cloud import adapter_for,CloudControlStop
from .host_contracts import TEMPLATES,STRATEGIES
from .host_validators import hashfile
from .assessment_prompt import obligations,assessment_errors
from .integration_packet import IntegratorContextDefect
from .requirements import execution_gate
from .policy import PLANNER_ATTEMPTS,ASSESSMENT_ATTEMPTS,WORKER_MAX_RETRIES,TASK_REPAIR_LIMIT,ROOT_REPAIR_LIMIT,SAME_FAILURE_LIMIT
from .workspace import artifact_inputs,assert_source_unchanged


class BoundedFailure(RuntimeError):
    def __init__(self,kind,message):self.failure_class=kind;super().__init__(message)


def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


class Driver:
    def __init__(self,run):
        self.run=run;self.core=HostCore(run)
        self.adapter=adapter_for(run,LocalRoleAdapter)

    def stage(self,stage,**data):
        self.run.raise_if_cancelled();self.run.save_state(status='RUNNING',stage=stage,**data)
        self.run.trace(stage,'RUN',stage)
        print('['+stage+'] '+self.run.run_id,flush=True)

    def assessed(self,role,prefix,payload,validator,max_tokens=1800,attempts=ASSESSMENT_ATTEMPTS):
        feedback=[];failure=None;previous=None;streak=0
        for index in range(attempts):
            self.run.raise_if_cancelled()
            try:
                response=self.adapter.ask(role,prefix+'\n'+compact(payload)+('\nHost feedback: '+compact(feedback[-1:]) if feedback else ''),max_output_tokens=max_tokens)
                result=validator(response)
                return response,result
            except (ValueError,ModelFailure) as exc:
                failure=getattr(exc,'failure_class','AGENT_DECISION_INVALID');streak=streak+1 if previous==failure else 1;previous=failure
                feedback.append(str(exc));self.run.event(role,'regenerate',failure_class=failure,attempt=index+1,limit=attempts)
                if streak>=SAME_FAILURE_LIMIT:break
        raise BoundedFailure(failure or 'AGENT_DECISION_INVALID','; '.join(feedback[-2:]))

    def records(self):return [load(p) for p in sorted((self.run.path/'tasks').glob('*.json'))]

    def setup(self):
        root=load(self.run.path/'root_goal.json')
        if not (self.run.path/'requirement_ack.json').exists():
            self.stage('requirement')
            ack,_=self.assessed('requirement','Read the fixed mission. Do NOT edit requirements. Return {"decision":"proceed","acknowledged_ids":[all exact IDs]} or decision needs_human with reason.',
                               root,self.core.requirement,max_tokens=800)
            dump(self.run.path/'requirement_ack.json',ack)
            self.run.handoff('requirement','planner',['root_goal.json','requirement_ack.json'])
        else:self.core.requirement(load(self.run.path/'requirement_ack.json'))
        if not (self.run.path/'plan.json').exists():
            self.stage('planner')
            payload={'original_request':root['request'],'mandatory_requirements':root['requirements']['mandatory_requirements'],
                     'completion_conditions':root['requirements']['completion_conditions'],'catalog':TEMPLATES,'strategies':STRATEGIES,
                     'existing_paths':sorted(load(self.run.path/'artifacts/host_baseline.json'))}
            parent=load(self.run.path/'parent_evidence.json',{})
            payload['parent_progress']=[{'task_id':r['task']['task_id'],'template':r['task']['template_id'],'status':r['status'],'last_failure':r.get('last_failure')}
                                        for _,r in parent.get('task_records',[])]
            payload['additional_instruction']=(self.run.path/'instruction.md').read_text()
            candidates=[{'contract':r['task']} for _,r in parent.get('task_records',[]) if r['status']=='PASS']
            coarse,plan=self.assessed('planner','Choose exactly each of the three Host templates once. Return {"tasks":[{"template":catalog ID,"goal":short implementation goal,"strategy":allowed strategy,"depends_on":[zero-based task indices]}]}. No formal contracts, tests or acceptance changes. Preserve existing valid artifacts; no unnecessary rewrites.',
                                      payload,lambda c:self.core.planner(c,candidates),max_tokens=1800,attempts=PLANNER_ATTEMPTS)
            dump(self.run.path/'coarse_plan.json',coarse);dump(self.run.path/'plan.json',plan)
            for task in plan['tasks']:
                reused=any(r['status']=='PASS' and r['task']==task for _,r in parent.get('task_records',[]))
                dump(self.run.path/'tasks'/f"{task['task_id']}.json",{'task':task,'contract':task,'status':'PENDING','attempts':[],'plan_revision':0,'reuse_candidate':reused})
            self.run.handoff('planner','worker',['plan.json','coarse_plan.json'],authority='host_compiler')

    def review(self,task,test,number):
        self.stage('reviewer',current_task=task['task_id'])
        registry=test['registry_result']
        evidence={'task':task,'host_receipt_ref':next(iter({p['evidence_artifact'] for p in test.get('host_execution_evidence',[])}),None),
                  'host_pass':test['exit_code']==0,'failure_owner':registry.get('failure_owner'),
                  'failure':registry.get('error'),'proof_kinds':registry.get('proof_kinds',[]),
                  'checks':[{k:c.get(k) for k in ('validator','pass','error')} for c in registry.get('checks',[])]}
        assessment,verdict=self.assessed('reviewer','Judge ONLY the Host formal contract and Host evidence. Worker tests/self-report are not formal acceptance. Return {"verdict":"PASS" or "REWORK","reason":short}. PASS requires all Host checks passed.',
                                         evidence,lambda v:self.core.reviewer(task,[test],v),max_tokens=1000)
        dump(self.run.path/'reviews'/f"{task['task_id']}_attempt_{number:03d}.json",{'assessment':assessment,'verdict':verdict,'tests':[test]})
        return assessment,verdict

    def task(self,record):
        task=record['task'];tid=task['task_id']
        while True:
            self.run.raise_if_cancelled();record=load(self.run.path/'tasks'/f'{tid}.json')
            if record['status']=='PASS':return
            revision=record.get('plan_revision',0)
            attempts=[a for a in record.get('attempts',[]) if a.get('plan_revision',0)==revision]
            if record.get('reuse_candidate') and not record['attempts']:
                # Parent PASS is only a candidate: fresh Host + Reviewer receipt required.
                self.stage('host_validation',current_task=tid)
                test=self.core.validation(task);assessment,verdict=self.review(task,test,0)
                record['attempts'].append({'attempt':0,'plan_revision':revision,'reuse':True,'tests_run':[test],'review_verdict':verdict})
                if verdict=='PASS':
                    record['status']='PASS';dump(self.run.path/'tasks'/f'{tid}.json',record);return
                record.update(reuse_candidate=False,last_failure={'failure_class':'REUSE_REVALIDATION_FAILED','error':assessment.get('reason','')})
                dump(self.run.path/'tasks'/f'{tid}.json',record);continue
            if len(attempts)>=WORKER_MAX_RETRIES+1:
                if revision>=TASK_REPAIR_LIMIT:raise BoundedFailure('HOST_RECOVERY_LIMIT',tid+' task repair limit reached')
                self.stage('planner',current_task=tid)
                choice,updated=self.assessed('planner','Select only {"strategy":one allowed value}. No contract/goal/criteria changes.',
                       {'choices':STRATEGIES,'task_goal':task['goal'],'failure':record.get('last_failure')},
                       lambda c:self.core.repair_strategy(task,record,c),max_tokens=300)
                dump(self.run.path/'tasks'/f'{tid}.json',updated);continue
            self.stage('worker',current_task=tid)
            number=len(record['attempts'])+1
            payload={'task':task,'instruction':(self.run.path/'instruction.md').read_text(),
                     'artifacts':artifact_inputs(self.run.state()['workspace'],task),'failure':record.get('last_failure'),
                     'repair_strategy':record.get('repair_strategy',task['implementation_strategy'])}
            # Every actual attempt, even malformed output, consumes the same bound.
            try:
                output=self.adapter.ask('worker',
                    'Return {"files":{exact allowed relative path: COMPLETE text},"summary":short}. Use {} when existing files already meet the contract. Do not output commands/contract/status. No protected writes/dependencies. Use pitchi_work.grounding, not old BONSAI Work imports. No fake answers.\n'+compact(payload),max_output_tokens=5000)
                self.core.worker(task,output)
            except (ValueError,ModelFailure) as exc:
                current=load(self.run.path/'tasks'/f'{tid}.json')
                if len(current['attempts'])==len(record['attempts']):
                    current['attempts'].append({'attempt':number,'plan_revision':revision,'failure_class':getattr(exc,'failure_class','WORKER_OUTPUT_INVALID'),'error':str(exc)})
                current['last_failure']={'failure_class':getattr(exc,'failure_class','WORKER_OUTPUT_INVALID'),'error':str(exc)}
                dump(self.run.path/'tasks'/f'{tid}.json',current);self.repeat_guard(current);continue
            self.stage('host_validation',current_task=tid)
            test=self.core.validation(task)
            dump(self.run.path/'artifacts'/f'{tid}_validation_{number:03d}.json',test)
            self.run.handoff('worker','reviewer',[f'tasks/{tid}.json',f'artifacts/{tid}_validation_{number:03d}.json'],contract_sha256=task['host_contract_sha256'])
            assessment,verdict=self.review(task,test,number)
            current=load(self.run.path/'tasks'/f'{tid}.json');attempt=current['attempts'][-1]
            attempt.update(tests_run=[test],review_verdict=verdict,reviewer_reason=assessment.get('reason',''))
            current['status']=verdict
            if verdict!='PASS':
                current['last_failure']={'failure_class':test['registry_result'].get('failure_class','REVIEWER_REWORK'),
                                         'error':test['registry_result'].get('error') or assessment.get('reason','')}
                attempt.update(current['last_failure'])
            dump(self.run.path/'tasks'/f'{tid}.json',current)
            if verdict=='PASS':self.run.trace('reviewer','OK',tid+' PASS');return
            self.repeat_guard(current)

    @staticmethod
    def repeat_guard(record):
        recent=record['attempts'][-SAME_FAILURE_LIMIT:]
        classes=[a.get('failure_class') for a in recent]
        if len(classes)==SAME_FAILURE_LIMIT and classes[0] and len(set(classes))==1:
            raise BoundedFailure(classes[0],'Same failure class twice; retain evidence and stop')

    def evidence(self,records,tests):
        workspace=Path(self.run.state()['workspace']);baseline=load(self.run.path/'artifacts/host_baseline.json')
        outputs={name for r in records for name in r['task']['expected_outputs']}
        inputs={name for r in records for name in r['task']['input_files']} - outputs
        return {'root_goal':load(self.run.path/'root_goal.json'),'root_goal_sha256':self.run.state()['root_goal_sha256'],
                'execution_context':load(self.run.path/'execution_context.json'),'obligations':obligations(load(self.run.path/'root_goal.json')),
                'tasks':records,'final_tests':tests,'artifacts':{n:{'sha256':hashfile(workspace/n)} for n in sorted(outputs)},
                'input_artifacts':{n:{'sha256':hashfile(workspace/n),'write_protected':True,'unchanged_from_parent':hashfile(workspace/n)==baseline.get(n)} for n in sorted(inputs)}}

    def integrator_batch(self,evidence,payload,prompt,index):
        """Regenerate only a malformed batch; never rework Worker artifacts."""
        ids={r['id'] for r in payload['obligations']}
        limit=min(ASSESSMENT_ATTEMPTS,SAME_FAILURE_LIMIT)
        feedback='';errors=[]
        for attempt in range(limit):
            self.run.raise_if_cancelled()
            request_prompt=prompt+feedback
            messages,_=self.core.integrator_messages(evidence,request_prompt)
            result=self.adapter.ask('integrator',request_prompt,messages=messages,max_output_tokens=2600)
            errors=assessment_errors(result,payload,ids)
            if not errors:
                dump(self.run.path/'artifacts'/f'integrator_batch_{index}.json',result)
                return result
            dump(self.run.path/'artifacts'/f'integrator_batch_{index}_rejected_{attempt+1}.json',
                 {'assessment':result,'errors':errors,'attempt':attempt+1,'limit':limit})
            self.run.event('integrator','assessment_rejected',batch=index,attempt=attempt+1,
                           limit=limit,failure_class='INTEGRATOR_ASSESSMENT_INVALID',errors=errors)
            feedback='\nHost response validation feedback: '+compact({
                'errors':errors,'assigned_ids':sorted(ids),
                'allowed_artifact_paths':sorted(set(payload['artifacts'])|set(payload.get('input_artifacts',{}))),
                'successful_test_ids':[t['test_id'] for t in payload['final_tests'] if t.get('exit_code')==0]})
            feedback+='\nRegenerate only this batch with explicit valid references. Keep evidence and acceptance unchanged; do not claim met=true without support.'
        raise BoundedFailure('INTEGRATOR_ASSESSMENT_INVALID',compact(errors))

    def finish(self):
        records=self.records();self.stage('host_root_acceptance')
        tests=self.core.root_acceptance(records)
        self.run.save_state(final_tests=tests)
        evidence=self.evidence(records,tests)
        gate=execution_gate(evidence['root_goal'],records,Path(self.run.state()['workspace']),tests)
        dump(self.run.path/'artifacts/request_execution_gate.json',gate)
        if gate['errors'] or any(t['exit_code']!=0 for t in tests):
            failed={t['task_id'] for t in tests if t['exit_code']!=0}
            if not failed:raise BoundedFailure('HOST_COMPLETION_EVIDENCE_UNRESOLVED',compact(gate['errors']))
            if self.run.state()['root_replan_count']>=ROOT_REPAIR_LIMIT:raise BoundedFailure('HOST_ROOT_RECOVERY_LIMIT','Root repair limit reached')
            # Unrelated PASS records remain byte-identical; failed original stays in history.
            for record in records:
                if record['task']['task_id'] not in failed:continue
                tid=record['task']['task_id'];revision=record.get('plan_revision',0)
                dump(self.run.path/'history'/f'{tid}_root_revision_{revision}.json',record)
                if revision>=TASK_REPAIR_LIMIT:raise BoundedFailure('HOST_RECOVERY_LIMIT',tid+' repair bound reached')
                record.update(status='PENDING',plan_revision=revision+1,last_failure={'failure_class':'ROOT_ACCEPTANCE_FAILURE','error':compact([t['registry_result'].get('error') for t in tests if t['task_id']==tid])})
                dump(self.run.path/'tasks'/f'{tid}.json',record)
            self.run.save_state(root_replan_count=self.run.state()['root_replan_count']+1)
            return False
        self.stage('integrator');packet,batches=self.core.integrator_inputs(evidence)
        dump(self.run.path/'artifacts/integration_packet.json',packet)
        self.run.handoff('reviewer','integrator',['artifacts/integration_packet.json','artifacts/request_execution_gate.json'])
        rows=[];blockers=[];summaries=[]
        for index,(payload,prompt,_) in enumerate(batches):
            result=self.integrator_batch(evidence,payload,prompt,index)
            rows.extend(result['criteria']);blockers.extend(result['blocking_evidence']);summaries.append(result['summary'])
        assessment={'verdict':'COMPLETED' if all(r.get('met') is True for r in rows) and not blockers else 'PARTIAL',
                    'criteria':rows,'blocking_evidence':blockers,'summary':' '.join(summaries)}
        dump(self.run.path/'artifacts/integrator_assessment.json',assessment)
        decision=self.core.completion(assessment,evidence);assert_source_unchanged(self.run)
        dump(self.run.path/'artifacts/request_completion_gate.json',decision)
        self.run.save_state(status=decision['status'],stage='idle',completion=decision)
        return True

    @retained_mission
    def execute(self):
        if self.run.state()['status'] in self.run.TERMINAL:raise ValueError('Terminal run is immutable; use continue')
        lock=self.run.path/'controller.lock';lock.touch(exist_ok=True)
        with lock.open() as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('This run already has an active controller')
            try:
                self.setup()
                while True:
                    for record in self.records():
                        if any(r['status']!='PASS' for r in self.records() if r['task']['task_id'] in record['task']['prerequisite_tasks']):
                            raise HostDefect('Host dependency not satisfied')
                        self.task(record)
                    if self.finish():break
            except Cancelled:
                terminate_run_processes(self.run)
                self.run.save_state(status='STOPPED_BY_USER',stage='idle')
            except CloudControlStop as exc:
                self.run.save_state(status='NEEDS_HUMAN',stage='idle',failure_class=exc.failure_class,
                                    failure_owner='policy',stop_reason=str(exc))
            except (HostDefect,IntegratorContextDefect,ModelFailure,BoundedFailure) as exc:
                if self.run.state()['status'] not in self.run.TERMINAL:
                    self.run.save_state(status='STALLED',stage='idle',failure_class=exc.failure_class,
                                        failure_owner='host' if isinstance(exc,(HostDefect,IntegratorContextDefect)) else 'agent',stop_reason=str(exc))
                self.run.trace('host','ERROR',str(exc),failure_class=exc.failure_class)
            except Exception as exc:
                if self.run.state()['status'] not in self.run.TERMINAL:
                    self.run.save_state(status='STALLED',stage='idle',failure_class='HOST_CONTROLLER_DEFECT',
                                        failure_owner='host',stop_reason=type(exc).__name__+': '+str(exc))
                self.run.trace('host','ERROR',str(exc),failure_class='HOST_CONTROLLER_DEFECT')
            finally:assert_source_unchanged(self.run)
        return self.run.state()
