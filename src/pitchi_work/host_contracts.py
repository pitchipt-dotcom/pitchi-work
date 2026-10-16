import hashlib,json
from pathlib import Path


TEMPLATES = {
    'T-QA': {'outputs': ['search/qa.py', 'search/qa_api.py'],
             'requirements': ['M1','M2','M3','M4','M5','M7','M8','M9','M10','M12','C1','C2','C3','C4'],
             'validators': ['V1','V2','V3','V4','V5'], 'evidence': ['behavior','artifact','live_local_llm','live_http','live_db']},
    'T-SEARCH': {'outputs': ['search/search_preserve_test.py'],
                 'requirements': ['M2','M6','M7','M11','M12','C5','C7'],
                 'validators': ['V1','V2','V6'], 'evidence': ['behavior','artifact','live_http','live_db']},
    'T-RECORD': {'outputs': ['search/qa_artifact_generator.py','search/qa_artifacts.json'],
                 'requirements': ['M10','M11','M12','C6','C8'],
                 'validators': ['V1','V2','V7'], 'evidence': ['behavior','artifact','live_local_llm','live_db']},
}

INPUTS = ['search/app.py','search/library.sqlite','search/qa.py','search/qa_api.py',
          'search/qa_artifact_generator.py','search/qa_artifacts.json',
          'search/search_preserve_test.py','search/qa_insufficient_test.py']

STRATEGIES = ['repair_existing_api_binding','repair_existing_artifact_producer','preserve_and_revalidate']

class ExperimentStopped(BaseException):
    """Separate experimental bound; never a model verdict or user cancellation."""

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def authority(task):
    return digest({k:v for k,v in task.items() if k != 'host_contract_sha256'})

def compile_plan(coarse, requirements, workspace):
    from .request_requirements import validate_requirements
    validate_requirements(requirements)
    tasks=coarse.get('tasks')
    if not isinstance(tasks,list) or len(tasks)!=3:
        raise ValueError('Choose each of the three approved templates exactly once; no Host-added tasks')
    if {t.get('template') for t in tasks} != set(TEMPLATES):
        raise ValueError('Invalid finite template selection')
    for index,t in enumerate(tasks):
        if set(t) != {'template','goal','strategy','depends_on'} or not isinstance(t['goal'],str) or not t['goal'].strip():
            raise ValueError('Coarse task requires template, nonempty goal, strategy, depends_on only')
        if t['strategy'] not in STRATEGIES or not isinstance(t['depends_on'],list):
            raise ValueError('Invalid finite strategy or dependency candidates')
        if any(type(d)!=int or d<0 or d>=len(tasks) or d==index for d in t['depends_on']):
            raise ValueError('Invalid dependency index')
    record=next(i for i,t in enumerate(tasks) if t['template']=='T-RECORD')
    edges={i:set(t['depends_on']) for i,t in enumerate(tasks)}
    # Approved producer bindings, not a guess about a model-written goal.
    edges[record].update(i for i in range(3) if i!=record)
    order=[]
    while len(order)<3:
        ready=[i for i in range(3) if i not in order and edges[i]<=set(order)]
        if not ready:raise ValueError('Dependency cycle conflicts with approved artifact binding')
        order.append(ready[0])
    ids={i:'H'+str(n+1) for n,i in enumerate(order)}
    rows={r['id']:r for r in requirements['request_completion_requirements']}
    if set(rows)!={f'M{i}' for i in range(1,13)}|{f'C{i}' for i in range(1,9)}:
        raise ValueError('This finite experiment only supports the approved immutable 20 obligations')
    obligations={**{f'M{i+1}':v for i,v in enumerate(requirements['mandatory_requirements'])},
                 **{f'C{i+1}':v for i,v in enumerate(requirements['completion_conditions'])}}
    plan=[]
    for i in order:
        choice=tasks[i];template=TEMPLATES[choice['template']]
        inputs=[p for p in INPUTS if (Path(workspace)/p).is_file()]
        for dep in edges[i]:inputs+=TEMPLATES[tasks[dep]['template']]['outputs']
        task={'task_id':ids[i], 'goal':choice['goal'], 'input_files':list(dict.fromkeys(inputs)),
              'expected_outputs':template['outputs'], 'allowed_paths':template['outputs'],
              'forbidden_paths':[p for p in requirements['immutable_scope'] if not Path(p).is_absolute() and p!='all paths outside workspace'],
              'prerequisite_tasks':[ids[d] for d in order if d in edges[i]],
              'expected_behavior':[obligations[r] for r in template['requirements']],
              'acceptance_criteria':[obligations[r] for r in template['requirements']],
              'requirement_ids':template['requirements'], 'max_retries':2,
              'test_contract':{'owner':'host','immutable':True,'commands':['host-validator:'+choice['template']],
                               'evidence_types':template['evidence'],'validator_ids':template['validators']},
              'host_contract_version':1,'template_id':choice['template'],'implementation_strategy':choice['strategy']}
        task['host_contract_sha256']=authority(task);plan.append(task)
    return {'tasks':plan,'host_contract_version':1}

def errors(plan,root):
    rows=(root or {}).get('requirements',{}).get('request_completion_requirements',[])
    coverage={r['id']:set() for r in rows};problems=[]
    seen=set()
    for task in plan.get('tasks',[]):
        name=task.get('template_id');catalog=TEMPLATES.get(name)
        if catalog is None:problems.append('Unknown Host template');continue
        if task.get('host_contract_sha256')!=authority(task):problems.append('Host contract hash mismatch')
        if task.get('expected_outputs')!=catalog['outputs'] or task.get('allowed_paths')!=catalog['outputs']:
            problems.append('Host write/output registry mismatch')
        if task.get('requirement_ids')!=catalog['requirements']:problems.append('Host requirement mapping mismatch')
        contract=task.get('test_contract',{})
        if contract != {'owner':'host','immutable':True,'commands':['host-validator:'+name],
                        'evidence_types':catalog['evidence'],'validator_ids':catalog['validators']}:
            problems.append('Host validator binding mismatch')
        if any(d not in seen for d in task.get('prerequisite_tasks',[])):problems.append('Unsatisfied dependency ordering')
        seen.add(task.get('task_id'))
        for r in catalog['requirements']:
            if r in coverage:coverage[r].update(catalog['evidence'])
    for row in rows:
        if set(row['evidence_types'])-coverage[row['id']]:problems.append('Uncovered immutable requirement '+row['id'])
    return problems
