from .host_contracts import errors as plan_errors

def execution_gate(root,records,workspace=None,final_tests=None):
    req=root.get('requirements',{})
    if 'request_completion_requirements' not in req:raise ValueError('Structured immutable requirements are mandatory')
    errors=plan_errors({'tasks':[r['task'] for r in records]},root);results=[]
    for row in req['request_completion_requirements']:
        observed=set();owners=[];proofs=[]
        for record in records:
            task=record['task']
            if row['id'] not in task.get('requirement_ids',[]):continue
            owners.append(task['task_id'])
            if record.get('status')!='PASS':continue
            attempts=record.get('attempts',[])
            if final_tests is not None:attempts=[{'review_verdict':'PASS','tests_run':[t for t in final_tests if t.get('task_id')==task['task_id']]}]
            for attempt in attempts:
                if attempt.get('review_verdict')!='PASS':continue
                for test in attempt.get('tests_run',[]):
                    if test.get('exit_code')!=0 or not test.get('execution_context'):continue
                    snapshot=test.get('artifact_snapshot',{})
                    if not snapshot or any(not p.get('exists') for p in snapshot.values()):continue
                    if workspace is not None:
                        from pathlib import Path
                        from .execution_context import artifact_snapshot
                        if test['execution_context'].get('active_workspace')!=str(Path(workspace).resolve()):continue
                        if snapshot!=artifact_snapshot(Path(workspace).resolve(),task):continue
                    observed.update(set(task.get('test_contract',{}).get('evidence_types',[])) & {'artifact','behavior'})
                    if task.get('host_contract_version')==1:
                        from .host_evidence import observed_kinds
                        kinds=observed_kinds(task,test)
                        observed.update(kinds)
                        proofs.extend({'task_id':task['task_id'],'command':test['command'],'source':'host_validator','kind':k} for k in kinds)
                        continue
                    raise ValueError('Only Host-owned evidence is supported')
        missing=set(row['evidence_types'])-observed
        if missing:errors.append('REQUEST_EXECUTION_EVIDENCE_MISSING '+row['id']+': '+','.join(sorted(missing)))
        results.append({'id':row['id'],'required_evidence':row['evidence_types'],'observed_evidence':sorted(observed),'met':not missing,'task_ids':owners,'proofs':proofs})
    return {'legacy':False,'errors':list(dict.fromkeys(errors)),'requirements':results}
