EVIDENCE_TYPES={"behavior","artifact","live_local_llm","live_http","live_db","live_service"}


def validate_requirements(requirements):
    rows=requirements.get('request_completion_requirements')
    if not isinstance(rows,list) or not rows:raise ValueError('request_completion_requirements required; no task-only completion')
    expected={prefix+str(i+1) for prefix,key in [('M','mandatory_requirements'),('C','completion_conditions')]
              for i,_ in enumerate(requirements.get(key,[]))}
    ids=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'id','evidence_types'}:raise ValueError('Completion requirement needs id/evidence_types only')
        ids.append(row['id']);kinds=row['evidence_types']
        if not isinstance(kinds,list) or not kinds or any(k not in EVIDENCE_TYPES for k in kinds):raise ValueError('Invalid evidence_types')
    if set(ids)!=expected or len(set(ids))!=len(ids):raise ValueError('Completion requirements must cover every mandatory/completion item exactly once using M1../C1.. IDs')
