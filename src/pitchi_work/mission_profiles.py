"""Finite Host registry for new missions; no model-authored acceptance rules."""
import ast
import hashlib
from pathlib import Path

PROFILES={
    'php_search_quality':{'template':'bounded_php_search_edit',
        'validators':['scope','php_syntax','protected_php_tests','real_before_after','control_routes','auxiliary_regression'],
        'completion':['host_checks_pass','reviewer_request_match','integrator_root_match'],
        'verification':'STRICT execution and measured concept coverage; ASSISTED semantic relevance',
        'required_output_paths':['tests/pitchi_search_concepts_regression.php']},
    'static_web_app':{'template':'static_web','validators':['scope','required_files','web_syntax','local_http','browser_interaction'],
                      'completion':['host_checks_pass','reviewer_request_match','integrator_root_match'],
                      'output_candidates':['index.html','app.js','style.css'],
                      'behavior_candidates':['interactive_smoke','arithmetic_practice','arithmetic_practice_review'],
                      'verification':'strict execution; assisted usability; subjective enjoyment'},
    'code_change':{'template':'python_edit','validators':['scope','python_syntax','existing_unittest','change_diff'],
                   'completion':['host_checks_pass','reviewer_request_match','integrator_root_match']},
    'bug_fix':{'template':'python_edit','validators':['scope','python_syntax','existing_unittest','change_diff'],
               'completion':['host_checks_pass','reviewer_request_match','integrator_root_match']},
    'code_investigation':{'template':'read_only_inspection','validators':['scope','source_references'],
                          'completion':['host_checks_pass','reviewer_request_match','integrator_root_match']},
    'book_qa':{'template':'approved_book_qa','validators':['V1','V2','V3','V4','V5','V6','V7'],
               'completion':['frozen_book_root_acceptance']},
}
FORBIDDEN_PARTS={'.git','.venv','venv','node_modules','__pycache__','.aws','.ssh','.config','.codex'}
FORBIDDEN_NAMES={'.env','credentials','secrets','token','password','id_rsa','id_ed25519'}
MAX_CONTENT_BYTES=12000


class UnsupportedMission(ValueError):
    failure_class='UNSUPPORTED_MISSION'


class HumanApprovalRequired(UnsupportedMission):
    failure_class='HUMAN_APPROVAL_REQUIRED'


def output_rejection(exc, allowed):
    """Common Host feedback for untrusted proposals, not a permission grant."""
    return {'authority':'host','required':str(exc),
            'allowed_response':allowed,
            'retry_rule':'Correct the rejected response using the unchanged Host contract and allowed scope. Do not change acceptance or request additional permissions.'}


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def visible(name):
    path=Path(name)
    return not any(p in FORBIDDEN_PARTS or p.startswith('.env') for p in path.parts) and not any(
        word in path.name.lower() for word in FORBIDDEN_NAMES)


def inventory(workspace):
    """Observed finite candidates, not inferred paths or generated test commands."""
    workspace=Path(workspace);files=[];tests=[]
    for path in sorted(workspace.rglob('*')):
        name=path.relative_to(workspace).as_posix()
        if not visible(name) or path.is_symlink() or not path.is_file():continue
        if path.suffix not in {'.py','.md','.txt','.log','.html','.js','.css'}:continue
        if path.stat().st_size>MAX_CONTENT_BYTES:continue
        try:text=path.read_text(encoding='utf-8',errors='strict')
        except UnicodeError:raise UnsupportedMission('Readable profile inputs must be UTF-8: '+name)
        if path.suffix=='.py':
            try:tree=ast.parse(text)
            except SyntaxError:tree=None
            if tree and any(isinstance(n,ast.ClassDef) and any(
                isinstance(b,ast.Name) and b.id=='TestCase' or
                isinstance(b,ast.Attribute) and b.attr=='TestCase' for b in n.bases) for n in ast.walk(tree)):
                tests.append(name)
        files.append({'path':name,'sha256':sha(path),'bytes':len(text.encode())})
    if len(files)>300 or len(tests)>20:raise UnsupportedMission('Workspace exceeds the finite profile inventory limit')
    return {'files':files,'tests':tests}


def bind_requirements(proposal,request,catalog):
    """Only select profiles, exact request spans and observed file candidates."""
    fields={'profile','targets','requirements','needs_human'}
    if proposal.get('profile')=='static_web_app':fields.add('behavior')
    if set(proposal)!=fields:
        raise ValueError('Required fields for selected profile: '+', '.join(sorted(fields))+'; missing: '+', '.join(sorted(fields-set(proposal))))
    profile=proposal['profile']
    if not isinstance(profile,str) or profile not in PROFILES:raise UnsupportedMission('No registered Host profile fits the request')
    if proposal['needs_human'] is not False:
        raise HumanApprovalRequired('Destructive, credential, paid/external service or outside-workspace operation requires human approval; not executed')
    if profile=='book_qa':
        raise UnsupportedMission('book_qa requires --mission-from with its approved immutable mission; arbitrary book criteria are not inferred')
    rows=proposal['requirements'];targets=proposal['targets']
    if not isinstance(rows,list) or not 1<=len(rows)<=4 or any(not isinstance(r,str) or not r.strip() or r not in request for r in rows):
        raise ValueError('requirements must be 1..4 exact nonempty spans from original_request')
    known={r['path'] for r in catalog['files']}
    if profile=='static_web_app':known.update(PROFILES[profile]['output_candidates'])
    if not isinstance(targets,list) or not 1<=len(targets)<=(4 if profile=='php_search_quality' else 3) or any(not isinstance(p,str) or p not in known for p in targets):
        raise UnsupportedMission('Select at most three existing readable workspace files')
    if len(set(targets))!=len(targets):raise ValueError('Duplicate targets')
    if sum(r['bytes'] for r in catalog['files'] if r['path'] in targets)>(64000 if profile=='php_search_quality' else 14000):
        raise UnsupportedMission('Selected files exceed the complete-source prompt budget')
    if profile=='static_web_app':
        if set(targets)!=set(PROFILES[profile]['output_candidates']):raise ValueError('Static Web app uses all three Host output paths')
        if proposal['behavior'] not in PROFILES[profile]['behavior_candidates']:raise ValueError('Select registered browser behavior only')
        return {'profile':profile,'targets':targets,'behavior':proposal['behavior'],
                'requirements':[{'id':'M'+str(i+1),'text':r} for i,r in enumerate(rows)]}
    if profile=='php_search_quality':
        from .search_quality import TARGETS, REGRESSION
        if not catalog.get('search_binding') or any(p not in TARGETS+[REGRESSION] for p in targets):
            raise UnsupportedMission('Search profile requires an explicit read-only database/runtime binding')
        targets=list(targets)
        if REGRESSION not in targets:targets.append(REGRESSION)
    elif profile!='code_investigation':
        if not catalog['tests']:raise UnsupportedMission('Code editing requires existing unittest.TestCase tests; no test generation fallback')
        if any(not p.endswith('.py') or p in catalog['tests'] or Path(p).name.startswith('test') for p in targets):
            raise UnsupportedMission('Only existing Python implementation files may be edited; tests remain protected')
    return {'profile':profile,'targets':targets,'requirements':[{'id':'M'+str(i+1),'text':r} for i,r in enumerate(rows)]}


def normalize_request_spans(proposal,request,instruction):
    """Additional instructions remain Host-owned I0, not invented M spans."""
    rows=proposal.get('requirements')
    if instruction and isinstance(rows,list) and rows and all(isinstance(r,str) and r and (r in request or r in instruction) for r in rows):
        original=[r for r in rows if r in request]
        if original:return {**proposal,'requirements':original}
    return proposal


def requirement_candidates(profile,catalog):
    """Host candidates, not write permission. Planner selects bounded ownership."""
    known=[r['path'] for r in catalog['files']]
    if profile=='php_search_quality':
        from .search_quality import TARGETS,REGRESSION
        if not catalog.get('search_binding'):
            raise UnsupportedMission('Search profile requires an explicit read-only database/runtime binding')
        return [p for p in TARGETS+[REGRESSION] if p in known]
    if profile=='static_web_app':return list(PROFILES[profile]['output_candidates'])
    if profile=='code_investigation':return known
    if not catalog['tests']:
        raise UnsupportedMission('Code editing requires existing unittest.TestCase tests; no test generation fallback')
    return [p for p in known if p.endswith('.py') and p not in catalog['tests']
            and not Path(p).name.startswith('test') and 'tests' not in Path(p).parts]


def requirement_intent_schema(catalog):
    """Same finite intent contract for prompt, model grammar and Host binding."""
    profiles=['php_search_quality'] if catalog.get('search_binding') else [p for p in PROFILES if p not in {'php_search_quality','book_qa'}]
    variants=[]
    for name in profiles:
        props={'profile':{'type':'string','enum':[name]},'needs_human':{'type':'boolean'}}
        if name=='static_web_app':props['behavior']={'type':'string','enum':PROFILES[name]['behavior_candidates']}
        variants.append({'type':'object','properties':props,'required':list(props),'additionalProperties':False})
    return {'oneOf':variants}


def bind_requirement_intent(proposal,request,catalog):
    """V2 intent only. Original request/acceptance never come from an LLM copy.

    Older-shaped responses may carry suggestions, but cannot grant write scope
    or replace the request. Old saved contracts still use bind_requirements.
    """
    if not isinstance(proposal,dict) or not {'profile','needs_human'}<=set(proposal) or set(proposal)-{'profile','needs_human','behavior','targets','requirements'}:
        raise ValueError('Return profile and needs_human; behavior only for static_web_app. No model-authored contracts.')
    if type(proposal['needs_human']) is not bool:raise ValueError('needs_human must be a boolean')
    if proposal['needs_human']:
        raise HumanApprovalRequired('Human authorization required; no operation executed')
    profile=proposal['profile']
    available=['php_search_quality'] if catalog.get('search_binding') else [p for p in PROFILES if p not in {'php_search_quality','book_qa'}]
    if not isinstance(profile,str) or profile not in available:raise UnsupportedMission('Select an available Host profile: '+', '.join(available))
    targets=requirement_candidates(profile,catalog)
    if not targets:raise UnsupportedMission('No Host-observed eligible implementation candidates for this profile')
    result={'profile':profile,'targets':targets,'requirements':[{'id':'M1','text':request}],
            'binding_version':2,'target_selection':'Host candidates only; Planner grants bounded ownership'}
    if profile=='static_web_app':
        if proposal.get('behavior') not in PROFILES[profile]['behavior_candidates']:
            raise ValueError('Select a registered static_web_app behavior: '+', '.join(PROFILES[profile]['behavior_candidates']))
        result['behavior']=proposal['behavior']
    elif proposal.get('behavior') is not None:raise ValueError('behavior applies only to static_web_app')
    return result


def compile_contracts(coarse,requirements,catalog,*,research_handoff_version=1):
    profile=requirements['profile'];policy=PROFILES[profile];rows=coarse.get('tasks')
    if set(coarse)!={'tasks'} or not isinstance(rows,list) or not 1<=len(rows)<=4:
        raise ValueError('Planner must return 1..4 coarse tasks only')
    required={r['id'] for r in requirements['requirements']};covered=set();contracts=[]
    for i,row in enumerate(rows):
        if not isinstance(row,dict) or set(row)!={'requirement_ids','targets','depends_on'}:
            raise ValueError('Choose only requirement_ids, targets, depends_on from Host candidates')
        ids=row['requirement_ids'];targets=row['targets'];deps=row['depends_on']
        if not isinstance(ids,list) or not ids or any(not isinstance(r,str) or r not in required for r in ids):raise ValueError('Unknown requirement ID')
        if not isinstance(targets,list) or not targets or any(not isinstance(p,str) or p not in requirements['targets'] for p in targets):raise ValueError('Unknown target')
        if requirements.get('binding_version')==2:
            limit=4 if profile=='php_search_quality' else 3
            selected=set(targets)|{p for t in contracts for p in t['input_files']}
            budget=64000 if profile=='php_search_quality' else 14000
            if len(targets)!=len(set(targets)) or len(selected)>limit:
                raise ValueError('Planner must select unique Host candidates within the existing '+str(limit)+'-file mission limit')
            if sum(f['bytes'] for f in catalog['files'] if f['path'] in selected)>budget:
                raise ValueError('Planner selection exceeds the unchanged complete-source budget')
        if not isinstance(deps,list) or any(type(d)!=int or d<0 or d>=i for d in deps):raise ValueError('Prerequisites must refer to prior coarse tasks')
        # Keep edit ownership unique; later tasks can read prior outputs.
        if profile!='code_investigation' and any(set(targets)&set(t['allowed_paths']) for t in contracts):
            raise ValueError('Overlapping write ownership; select one task for those files')
        covered.update(ids)
        contract={'task_id':'P'+str(i+1),'profile':profile,'template':policy['template'],
                  'requirement_ids':ids,'goal':[r['text'] for r in requirements['requirements'] if r['id'] in ids],
                  'input_files':targets,'allowed_paths':targets if profile!='code_investigation' else [],
                  'expected_outputs':targets if profile!='code_investigation' else [],
                  'prerequisite_tasks':['P'+str(d+1) for d in deps],
                  'validators':policy['validators'],'existing_tests':catalog['tests'] if profile!='code_investigation' else [],
                  'required_evidence':['source_hashes','host_checks','source_references'] if profile=='code_investigation'
                      else ['source_hashes','syntax','existing_test_execution','diff'],
                  'completion_conditions':policy['completion'],'max_attempts':3,'timeout':120}
        if profile=='static_web_app':
            contract.update(input_files=[p for p in targets if p in {f['path'] for f in catalog['files']}],
                            behavior=requirements['behavior'],existing_tests=[],
                            required_evidence=['source_hashes','syntax','local_http','browser_interaction','diff'],
                            ui_contract={'answer':'#answer','submit':'#submit','feedback':'#feedback','next':'#next',
                                         'operand_a':'#operand-a','operand_b':'#operand-b'},
                            verification_modes={'execution':'STRICT','usability':'ASSISTED','enjoyment':'SUBJECTIVE'})
            if requirements['behavior'].startswith('arithmetic_practice'):contract['validators']=[*policy['validators'],'arithmetic_practice']
            if requirements['behavior']=='arithmetic_practice_review':
                contract['validators'].append('incorrect_answer_review')
                contract['ui_contract'].update(review='#review',review_count='#review-count[data-count]')
            if len(rows)!=1 or set(targets)!=set(PROFILES[profile]['output_candidates']):raise ValueError('One coherent static Web app task must own all three outputs')
        if profile=='php_search_quality':
            if len(rows)!=1:raise ValueError('One search-quality task owns the bounded edit')
            # Fixed template output, not an inferred Planner artifact.
            for name in policy['required_output_paths']:
                if name not in contract['allowed_paths']:
                    for key in ('input_files','allowed_paths','expected_outputs'):
                        contract[key]=list(contract[key])+[name]
            known_inputs={f['path'] for f in catalog['files'] if f.get('exists',True)}
            contract['input_files']=[p for p in contract['input_files'] if p in known_inputs]
            contract.update(required_evidence=['source_hashes','syntax','protected_test_execution','diff',
                'before_after_real_search','retrieval_stages','control_queries','supplementary_regression'],
                verification_modes={'execution':'STRICT','semantic_relevance':'ASSISTED'})
        from .phase_workflow import policy_for
        phase_policy=policy_for(profile,sum(f['bytes'] for f in catalog['files'] if f['path'] in targets),len(ids),
                                research_handoff_version=research_handoff_version)
        if phase_policy:
            contract['phase_policy']=phase_policy
            contract['research_inputs']=list(dict.fromkeys(contract['input_files']+contract['existing_tests']+
                                                         catalog.get('research_inputs',[])))
        contract['sha256']=hashlib.sha256(__import__('json').dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if catalog.get('guided_edits'):
            guided=catalog['guided_edits']
            if research_handoff_version!=2 or not phase_policy or len(rows)!=1 or not set(guided['source_hashes']).issubset(contract['allowed_paths']):
                raise ValueError('Guided steps require one v2 task owning all explicit source paths')
            contract['guided_edits']=guided
            contract['sha256']=hashlib.sha256(__import__('json').dumps({k:v for k,v in contract.items() if k!='sha256'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        contracts.append(contract)
    if covered!=required:raise ValueError('Every fixed requirement must be mapped to a task')
    return {'profile':profile,'tasks':contracts,'authority':'host-profile-registry-v1'}
