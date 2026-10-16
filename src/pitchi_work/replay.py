"""Read-only independent replay of the completed real V1.5 run.

No old Python modules, application imports, HTTP, LLM, or producer execution.
Saved live observations are checked again by frozen Host predicates and real DB.
"""
import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from .store import ReadOnlyRun,load,dump
from .compiler import compile_retaining
from .assessment_prompt import obligations,request_assessment_prompt
from .host_validator_checks import require
from . import host_validator_checks as checks
from .host_contracts import TEMPLATES,authority
from .host_validators import POSITIVE,NEGATIVE
from .grounding import query_stems
from .evidence import verify_receipt
from .runtime import HostCore
from .integration_packet import build_packet,message_budget


def fingerprints(folder):
    return {p.relative_to(folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts}


def database_retrieval(workspace,calls):
    # Fixed schema/JOIN from the frozen Host predicate, not generated SQL.
    with sqlite3.connect((workspace/'search/library.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=ON');rows=[]
        for call in calls:
            for entry in call['evidence']:
                rows.extend({'book_title':t,'pdf_page':p,'ocr_text':text} for t,p,text in db.execute(
                    'SELECT b.title,p.pdf_page,p.text FROM pages p JOIN books b ON b.book_id=p.book_id WHERE b.title=? AND p.pdf_page=?',
                    (entry['book_title'],entry['pdf_page'])))
    return rows


def replay(source,output):
    run=ReadOnlyRun(source);workspace=Path(run.state()['workspace']).resolve()
    output=Path(output).resolve()
    if output.is_relative_to(run.path) or output.is_relative_to(workspace):raise ValueError('Replay output must be outside original evidence/workspace')
    before={'run':fingerprints(run.path),'workspace':fingerprints(workspace)}
    root=load(run.path/'root_goal.json');records=[load(p) for p in sorted((run.path/'tasks').glob('*.json'))]
    core=HostCore(run);ack=json.loads((run.path/'artifacts/host_requirement_read_raw.txt').read_text())
    require(core.requirement(ack)==root,'Frozen requirements differ')
    raw=json.loads((run.path/'artifacts/host_planner_raw_1.txt').read_text())
    compiled=core.planner(raw,load(run.path/'continuation.json')['candidates'])
    require(compiled==load(run.path/'plan.json'),'Formal compiled contract differs from saved V1.5 plan')
    require(all(authority(r['task'])==r['task']['host_contract_sha256'] for r in records),'Formal task hash mismatch')
    tests=run.state()['final_tests'];reference_hashes=[];judgments=[]
    allowed={p for t in TEMPLATES.values() for p in t['outputs']}
    with sqlite3.connect((workspace/'search/library.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        negative_counts=[db.execute('SELECT count(*) FROM pages WHERE instr(text,?)>0',(stem,)).fetchone()[0] for stem in query_stems(NEGATIVE)]
    # Recheck all saved final + root measurements, not Worker-generated tests.
    for test in tests:
        task=next(r['task'] for r in records if r['task']['task_id']==test['task_id'])
        reference_hashes+=verify_receipt(run.path,task,test)
        registry=test['registry_result'];calls=registry.get('live_calls',[])
        retrieval=database_retrieval(workspace,calls)
        for check in registry['checks']:
            validator=check['validator'];details=check.get('details',{})
            if validator=='V1':checks.scope(workspace,load(run.path/'artifacts/host_baseline.json'),allowed)
            elif validator=='V2':checks.syntax(workspace,TEMPLATES[task['template_id']]['outputs'])
            elif validator=='V3':checks.live_response(details,calls)
            elif validator=='V4':
                # V4's saved details are a projection, not the complete QA return.
                # The frozen collector validated the same V3 HTTP response.
                positive=next(c['details'] for c in registry['checks'] if c['validator']=='V3')
                checks.citations(workspace,positive,POSITIVE,calls,retrieval)
            elif validator=='V5':checks.insufficient(details,negative_counts)
            elif validator=='V6':checks.search(workspace,details['count'],details['results'],details['http_response'])
            elif validator=='V7':checks.record(workspace,details,registry['observed_returns'],calls,POSITIVE,retrieval)
            else:raise ValueError('Unregistered validator')
            require(check['pass'] is True,'Existing Host measurement disagrees with replay')
            judgments.append({'task_id':task['task_id'],'validator_id':validator,'saved_pass':check['pass'],'pitchi_pass':True})
    reviews=[]
    for record in records:
        tid=record['task']['task_id'];path=sorted((run.path/'reviews').glob(tid+'_*.json'))[-1];review=load(path)
        assessment=review.get('assessment',review.get('model_assessment'))
        task_tests=review.get('tests',review.get('evidence',{}).get('tests',[]))
        verdict=core.reviewer(record['task'],task_tests,assessment)
        require(verdict=='PASS' and record['status']=='PASS','Reviewer/Host judgment differs')
        reviews.append({'task_id':tid,'verdict':verdict,'review_ref':str(path.relative_to(run.path))})
    # Rebuild the model-facing packet from saved evidence, never full prompts.
    saved_packet=load(run.path/'artifacts/integration_packet.json')
    evidence={'root_goal':root,'root_goal_sha256':run.state()['root_goal_sha256'],
              'execution_context':load(run.path/'execution_context.json'),'obligations':obligations(root),
              'tasks':records,'final_tests':tests,'artifacts':saved_packet['artifacts'],
              'input_artifacts':saved_packet['input_artifacts']}
    packet,batches=core.integrator_inputs(evidence)
    require(packet==saved_packet,'Integration packet differs from saved V1.5 packet')
    budget=[]
    for _,prompt,_ in batches:
        _,stats=core.integrator_messages(evidence,prompt)
        require(stats['within_budget'],'Independent packet exceeds budget');budget.append(stats)
    from .requirements import execution_gate
    # The saved request gate ran before independent root tests were appended.
    task_tests=tests[:len(records)]
    execution=execution_gate(root,records,workspace,task_tests)
    def canonical_gate(gate):
        return {**gate,'requirements':[{**row,'proofs':sorted(row['proofs'],key=lambda p:json.dumps(p,sort_keys=True))} for row in gate['requirements']]}
    require(canonical_gate(execution)==canonical_gate(load(run.path/'artifacts/request_execution_gate.json')),'Execution requirement mapping differs')
    decision=core.completion(load(run.path/'artifacts/integrator_assessment.json'),evidence)
    saved=load(run.path/'artifacts/request_completion_gate.json')
    require({k:decision[k] for k in saved}==saved,'Host completion judgment differs')
    require(decision['status']==run.state()['status']=='COMPLETED','Terminal result differs')
    after={'run':fingerprints(run.path),'workspace':fingerprints(workspace)}
    require(before==after,'Replay changed existing run or workspace')
    # The core is not permitted to import the old orchestration packages.
    import sys
    require(not any(n.startswith(('bonsai_work_orchestrator','bonsai_work_v2')) for n in sys.modules),'Legacy runtime dependency loaded')
    result={'source_run':run.run_id,'replay_only':True,'new_full_runs':0,'llm_calls':0,
            'formal_contract_equal':True,'requirement_execution_gate_equal':True,
            'integration_packet_equal':True,'evidence_hashes':list({r['artifact']:r for r in reference_hashes}.values()),
            'formal_task_count':len(compiled['tasks']),
            'request_requirement_count':len(execution['requirements']),
            'root_obligation_count':len(evidence['obligations']),
            'saved_request_gate_test_count':len(task_tests),'all_final_and_root_test_count':len(tests),
            'validator_ids':sorted({j['validator_id'] for j in judgments}),
            'saved_host_check_count':len(judgments),
            'integration_reference_count':len(packet['evidence_references']),
            'request_gate_comparison':'exact fields and multiplicity; proof list order canonicalized because original iterates a set',
            'host_judgments':judgments,'reviewers':reviews,'completion':decision,
            'original_run_and_workspace_unchanged':True,'legacy_modules_loaded':False,
            'integrator_budgets':budget,'grounding_limit':'provenance/DB checks, not semantic entailment'}
    dump(output/'result.json',result)
    dump(output/'frozen_before.json',before);dump(output/'frozen_after.json',after)
    return result
