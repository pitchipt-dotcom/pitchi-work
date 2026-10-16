from pathlib import Path
from .host_contracts import errors as contract_errors

def validate_completion(result,evidence):
    """A verdict alone cannot certify completion; every root obligation needs evidence."""
    errors=contract_errors({'tasks':[r['task'] for r in evidence['tasks']]},evidence['root_goal'])
    from .requirements import execution_gate
    errors+=execution_gate(evidence['root_goal'],evidence['tasks'],final_tests=evidence.get('final_tests'),
                           workspace=Path(evidence['execution_context']['active_workspace']) if evidence.get('execution_context') else None)['errors']
    from .language_rules import plan_language_errors
    errors+=plan_language_errors({'tasks':[r['task'] for r in evidence['tasks']]},evidence['root_goal'])
    errors+=['Protected input changed from parent snapshot: '+name for name,item in evidence.get('input_artifacts',{}).items() if item.get('write_protected') and item.get('unchanged_from_parent') is False]
    required={x['id'] for x in evidence['obligations']}
    rows=result.get('criteria',[])
    if not required or not isinstance(rows,list):return errors+['missing root acceptance evidence']
    ids=[r.get('id') for r in rows if isinstance(r,dict)]
    if len(ids)!=len(rows) or len(set(ids))!=len(ids) or set(ids)!=required:errors.append('every immutable root obligation must be assessed exactly once')
    names=set(evidence['artifacts'])|set(evidence.get('input_artifacts',{}))
    commands={ref for i,t in enumerate(evidence['final_tests'],1) if t.get('exit_code')==0 for ref in (t['command'],f'F{i}')}
    for row in rows:
        if not isinstance(row,dict):continue
        refs=row.get('artifacts',[]);checks=row.get('commands',[])
        if row.get('met') is not True:errors.append(str(row.get('id'))+': '+str(row.get('reason','not achieved')))
        if not isinstance(refs,list) or not refs or any(n not in names for n in refs):errors.append(str(row.get('id'))+': missing/unknown artifact evidence')
        if not isinstance(checks,list) or not checks or any(c not in commands for c in checks):errors.append(str(row.get('id'))+': missing/successful command evidence')
    if result.get('blocking_evidence'):errors.append('blocking evidence: '+str(result['blocking_evidence']))
    return errors
