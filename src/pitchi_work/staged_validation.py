"""Protected test observations on staged code, never a completion decision."""
from pathlib import Path
import hashlib
from .store import dump,load
from .code_validation import invocation
from .runner import HostDefect


def ensure_query_input(run):
    """Materialize an execution INPUT from trusted binding, not search evidence."""
    binding=load(run.path/'search_binding.json',{})
    queries=binding.get('queries')
    if not isinstance(queries,list) or not 1<=len(queries)<=12 or any(not isinstance(q,str) or not q.strip() for q in queries):
        raise HostDefect('Staged collector requires valid Host-bound query inputs')
    path=run.path/'artifacts/search_queries.json'
    if path.exists():
        if path.is_symlink() or load(path)!=queries:raise HostDefect('Host query input differs from fixed binding')
    else:dump(path,queries)
    return {'reference':str(path.relative_to(run.path)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'binding_sha256':hashlib.sha256((run.path/'search_binding.json').read_bytes()).hexdigest(),'authority':'host','kind':'execution_input_not_evidence'}


class StagedRun:
    def __init__(self,run,workspace):self.run=run;self.path=run.path;self.workspace=workspace
    def state(self):return {**self.run.state(),'workspace':str(self.workspace)}
    def __getattr__(self,name):return getattr(self.run,name)


def diagnostics(receipt,limit=120):
    """Bounded exact first/last views, never an LLM diagnosis or verdict."""
    from .phase_workflow import pages
    result=[]
    for item in receipt['results']:
        if item['passed']:continue
        execution=item['execution'];streams={}
        for key in ('stderr','stdout'):
            text=execution.get(key,'');chunks=pages(text,limit)
            streams[key]={'first':chunks[0]['text'],'last':chunks[-1]['text'],
                          'partial':len(chunks)>1,'utf8_bytes':len(text.encode())}
        result.append({'test':item['test'],'exit_code':execution['exit_code'],**streams})
        if len(result)==2:break
    return result


def observe_staged(run,contract,files,folder):
    query_input=ensure_query_input(run) if contract['profile']=='php_search_quality' else None
    folder=Path(folder).resolve();workspace=folder/'workspace';workspace.mkdir(parents=True,exist_ok=True)
    if workspace.is_symlink():raise HostDefect('Unsafe staged workspace')
    baseline=load(run.path/'artifacts/host_baseline.json',None)
    snapshot={n:t.encode() for n,t in files.items()}
    if baseline is not None:
        if not isinstance(baseline,dict):raise HostDefect('Invalid Host source snapshot manifest')
        origin=Path(run.state()['workspace']).resolve();snapshot={}
        plan=load(run.path/'plan.json',{})
        writable={n for task in plan.get('tasks',[contract]) for n in task.get('allowed_paths',[])}
        for name in baseline:
            p=(origin/name).resolve()
            if origin not in p.parents or (origin/name).is_symlink() or not p.is_file():
                raise HostDefect('Missing/unsafe baseline input for staged test: '+name)
            snapshot[name]=p.read_bytes()
            if name not in writable and hashlib.sha256(snapshot[name]).hexdigest()!=baseline[name]:
                raise HostDefect('Protected baseline input changed before staged test: '+name)
        for name in contract['allowed_paths']:
            if name in files:snapshot[name]=files[name].encode()
    # The research/read catalog is not the execution dependency closure.
    # Copy full Host-bound baseline assets and overlay only task-owned code.
    if any(name not in snapshot for name in contract['existing_tests']):
        raise HostDefect('Protected test is absent from Host staging inputs')
    present={p.relative_to(workspace).as_posix() for p in workspace.rglob('*') if p.is_file()}
    if not present.issubset(snapshot):raise HostDefect('Unexpected file in incomplete Host staging workspace')
    for name,content in snapshot.items():
        p=(workspace/name).resolve()
        if workspace.resolve() not in p.parents:raise ValueError('Unsafe staged path')
        if p.is_symlink() or (p.exists() and p.read_bytes()!=content):raise HostDefect('Incomplete Host staging workspace source mismatch')
        p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
    staged=StagedRun(run,workspace);results=[]
    for name in contract['existing_tests']:
        if contract['profile']=='php_search_quality':
            from .search_quality import execute
            result=execute(staged,[str(workspace/name)])
            passed=result['exit_code']==0
        else:
            bootstrap=('import sys,unittest,json;sys.path.insert(0,sys.argv[1]);'
                'suite=unittest.defaultTestLoader.discover(sys.argv[2],pattern=sys.argv[3]);'
                'r=unittest.TextTestRunner(verbosity=2).run(suite);'
                'print("PITCHI_HOST_TEST_RESULT="+json.dumps({"completed":True,"tests_run":r.testsRun,"skipped":len(r.skipped)}));'
                'sys.exit(0 if r.wasSuccessful() else 1)')
            test=Path(name)
            result=invocation(staged,['/usr/bin/python3','-I','-B','-c',bootstrap,str(workspace),str(workspace/test.parent),test.name],contract['timeout'])
            passed=result['exit_code']==0 and result['tests_run']>0
        results.append({'test':name,'passed':passed,'execution':result})
    receipt={'authority':'host','kind':'protected_staged_tests','completion':False,'results':results,'query_input':query_input,
             'source_hashes':{n:hashlib.sha256(t.encode()).hexdigest() for n,t in files.items()},
             'workspace_hashes':{n:hashlib.sha256(b).hexdigest() for n,b in snapshot.items()}}
    dump(folder/'receipt.json',receipt)
    return receipt
