

def observed_kinds(task,test):
    from .host_contracts import authority,TEMPLATES
    if test.get('exit_code')!=0 or task.get('host_contract_sha256')!=authority(task):return set()
    result=test.get('registry_result',{})
    if result.get('passed') is not True or result.get('mode')!='live':return set()
    expected=set(TEMPLATES[task['template_id']]['validators'])
    passed={c['validator'] for c in result.get('checks',[]) if c.get('pass') is True}
    if not expected<=passed:return set()
    kinds=set()
    for p in test.get('host_execution_evidence',[]):
        if p.get('source')=='host_validator' and p.get('host_contract_sha256')==task['host_contract_sha256']:
            kinds.add(p['kind'])
    return kinds
