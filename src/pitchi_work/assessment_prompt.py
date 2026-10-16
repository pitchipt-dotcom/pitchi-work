import json


def assessment_errors(result, packet, assigned_ids):
    """Validate the response envelope/references, not whether a goal is met.

    Negative judgments may have empty reference lists. Never turn a negative
    judgment into PASS or fill absent references on the model's behalf.
    """
    if not isinstance(result, dict):return ['assessment must be an object']
    errors=[]
    if result.get('verdict') not in ('COMPLETED','PARTIAL','NEEDS_HUMAN'):
        errors.append('verdict must be COMPLETED/PARTIAL/NEEDS_HUMAN')
    if not isinstance(result.get('summary'),str):errors.append('summary must be a string')
    blockers=result.get('blocking_evidence')
    if not isinstance(blockers,list) or any(not isinstance(b,str) for b in blockers):
        errors.append('blocking_evidence must be a list of strings')
    rows=result.get('criteria')
    if not isinstance(rows,list):return errors+['criteria must be a list']
    ids=[r.get('id') for r in rows if isinstance(r,dict)]
    if len(ids)!=len(rows) or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids) or set(ids)!=set(assigned_ids):
        errors.append('Every assigned obligation must occur exactly once')
    artifacts=set(packet['artifacts'])|set(packet.get('input_artifacts',{}))
    commands={ref for t in packet['final_tests'] if t.get('exit_code')==0
              for ref in (t['test_id'],t['command'])}
    for row in rows:
        if not isinstance(row,dict):continue
        identifier=str(row.get('id','unknown'))
        if type(row.get('met')) is not bool:errors.append(identifier+': met must be boolean')
        if not isinstance(row.get('reason'),str):errors.append(identifier+': reason must be a string')
        for field,known in (('artifacts',artifacts),('commands',commands)):
            refs=row.get(field)
            if not isinstance(refs,list) or any(not isinstance(r,str) for r in refs):
                errors.append(identifier+': '+field+' must be an explicit list of known references')
            elif any(r not in known for r in refs):
                errors.append(identifier+': '+field+' contains unknown references')
            elif row.get('met') is True and not refs:
                errors.append(identifier+': met=true requires nonempty '+field+' references')
    return errors


def obligations(root):
    rows=[{'id':prefix+str(i+1),'text':text} for prefix,key in
            [('M','mandatory_requirements'),('C','completion_conditions')]
            for i,text in enumerate(root['requirements'].get(key,[]))]
    if root.get('request'):
        rows.append({'id':'R0','text':'Achieve the ORIGINAL request exactly, including requirements omitted or weakened by interpretation: '+root['request']})
    return rows

def request_assessment_prompt(evidence,start,integration_packet):
    """Same deterministic prompt builder for dispatch and saved-run replay."""
    if integration_packet is None:raise ValueError('Host integration packet is mandatory')
    source=integration_packet
    payload={k:v for k,v in source.items() if k!='obligations'}
    # Varying batch IDs last keeps the long common prefix cacheable.
    payload['obligations']=evidence['obligations'][start:start+8]
    prefix=('Verify REQUEST COMPLETION, not task completion. Assess ONLY the listed batch obligations against the full immutable root request. '
            'Do not mark unassigned obligations as blockers: other batches assess them. Return JSON verdict COMPLETED/PARTIAL/NEEDS_HUMAN, summary string, '
            'criteria:[{id: exact assigned ID, met:boolean, artifacts:[exact paths from artifacts OR protected input_artifacts], commands:[successful test IDs such as F1], reason:short}], blocking_evidence:[short actual blockers]. '
            'All fields are mandatory. Every met=true assessment needs nonempty artifact and successful test references, including preservation obligations. For met=false include explicit lists, which may be empty. Never invent references or change a judgment to satisfy the schema. '
            'One assessment per assigned ID. Prefer test IDs, not repeated long commands. Read actual artifacts/Reviewer evidence. Task PASS or exit 0 alone is not request completion. '
            'Missing inputs, implementation or real-data verification block completion. A stub/injected provider is not proof of a required live local LLM. '
            'Compare original request too; never accept a weakened interpretation or documentation of failure as success. For absent evidence mark met=false. Compact JSON. ')
    from .integration_packet import fit_payload
    prefix+='The integration_packet contains Host-owned check results and hash-verified saved evidence references, not source bodies. Unresolved issues remain blockers. Do not require omitted duplicate bodies when the corresponding Host validator explicitly verified them. Evidence: '
    payload,prompt,stats=fit_payload(prefix,payload)
    return payload,prompt,stats
