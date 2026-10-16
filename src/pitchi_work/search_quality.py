"""Finite PHP search-quality profile: protected source, fixed data, before/after.

Semantic relevance stays ASSISTED; lexical coverage is an observation, not truth.
"""
import difflib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid
import re
from .store import dump, load
from .mission_profiles import sha, visible, UnsupportedMission
from .runner import HostDefect
from .code_validation import invocation

TARGETS = ['src/GuidelineSectionSearch.php','src/ClinicalQueryNormalizer.php','src/SearchResultRanking.php']
REGRESSION = 'tests/pitchi_search_concepts_regression.php'
TESTS = ['tests/search_quality_regression.php','tests/guideline_section_search.php',
         'tests/search_result_ranking.php','tests/evidence_search.php','tests/official_document_search.php']
CONTROLS = ['脳卒中','リハビリテーション','脳卒中 歩行']


def search_inventory(run):
    workspace=Path(run.state()['workspace'])
    # Read scope is separate from edit scope. Host-observed implementation and
    # protected tests can be inspected; this grants no additional write paths.
    from .mission_profiles import visible
    research=[p.relative_to(workspace).as_posix() for directory in ('src','config')
              for p in sorted((workspace/directory).glob('*.php'))
              if p.is_file() and not p.is_symlink() and visible(p.relative_to(workspace).as_posix())]
    return {'files':[{'path':name,'exists':(workspace/name).exists(),'bytes':(workspace/name).stat().st_size if (workspace/name).exists() else 0}
                     for name in TARGETS+[REGRESSION]],'tests':TESTS,'search_binding':True,'research_inputs':research}


def patched_files(output, files, allowed, replaceable_new_paths=()):
    """Exact replacements only; validate all patches before any write."""
    if 'edits' in output:
        if output['edits'] and isinstance(output['edits'][0],dict) and 'span_id' in output['edits'][0]:
            return span_edits(output['edits'],files,allowed,replaceable_new_paths)
        return line_edits(output['edits'],files,allowed)
    patches=output.get('patches')
    if not isinstance(patches,list) or not 1<=len(patches)<=12:
        raise ValueError('Return 1..12 exact patches')
    result={}
    for patch in patches:
        if not isinstance(patch,dict) or set(patch)!={'path','before','after'}:
            raise ValueError('Patch fields are path,before,after only')
        name,before,after=patch['path'],patch['before'],patch['after']
        if name not in allowed or not isinstance(before,str) or not isinstance(after,str):raise ValueError('Invalid patch path/text')
        text=result.get(name,files.get(name,''))
        if not before:
            if name!=REGRESSION or (text and name not in replaceable_new_paths):
                raise ValueError('Empty before allowed only for the new registered regression file')
            result[name]=after
        elif text.count(before)!=1:raise ValueError('Patch before must occur exactly once in '+name)
        else:result[name]=text.replace(before,after,1)
    if sum(len(v.encode()) for v in result.values())>100000:raise ValueError('Patched source exceeds fixed budget')
    return result


def line_edits(edits, files, allowed):
    """Hash-pinned explicit line ranges; no fuzzy matching or semantic repair."""
    import hashlib
    if not isinstance(edits,list) or not 1<=len(edits)<=12:raise ValueError('Select 1..12 explicit line edits')
    groups={}
    for edit in edits:
        if not isinstance(edit,dict) or set(edit)!={'path','source_sha256','start_line','end_line','replacement'}:
            raise ValueError('Line edit fields: path,source_sha256,start_line,end_line,replacement')
        name=edit['path'];start=edit['start_line'];end=edit['end_line'];replacement=edit['replacement']
        if name not in allowed or type(start)!=int or type(end)!=int or not isinstance(replacement,str):raise ValueError('Invalid line edit scope/type')
        text=files.get(name,'');lines=text.splitlines(True)
        if edit['source_sha256']!=hashlib.sha256(text.encode()).hexdigest():raise ValueError('Stale source hash: '+name)
        if not 1<=start<=len(lines)+1 or not start-1<=end<=len(lines):raise ValueError('Invalid line range: '+name)
        groups.setdefault(name,[]).append((start,end,replacement))
    result={}
    for name,rows in groups.items():
        rows.sort();last=0
        for start,end,_ in rows:
            if start<=last:raise ValueError('Overlapping line edits: '+name)
            last=max(start,end)
        lines=files.get(name,'').splitlines(True)
        for start,end,replacement in reversed(rows):lines[start-1:end]=[replacement]
        result[name]=''.join(lines)
    if sum(len(v.encode()) for v in result.values())>100000:raise ValueError('Edited source exceeds fixed budget')
    return result


def worker_context(run):
    observed=load(run.path/'artifacts/search_before.json')['observations']
    fields={'id','title','section_title','guideline_title','name','source_url','url',
            'matched_concepts','matched_concept_count','query_concept_count','relevance_score'}
    baseline=load(run.path/'artifacts/search_baseline_tests.json',{})
    return {'data_access':'Host fixed read-only real SQLite snapshot; no production writes or service changes',
       'baseline_test_failures':[{'test':name,'exit_code':r['exit_code'],'error':r['stderr'][-650:]} for name,r in baseline.items() if r['exit_code']],
       'before_reference':'artifacts/search_before.json',
       'queries':[{'query':r['query'],'normalization_reference':'artifacts/search_before.json',
           'concept_names':[c.get('id',c.get('name',c.get('canonical_concept'))) for c in r['concepts']],
           'concept_roles':[{'id':c.get('id'),'role':c.get('role'),'required_subject':c.get('required_subject')} for c in r['concepts']],
           'metadata_expansion':r['metadata_expansion'],'retrieval_stages':r['retrieval_stages'],
           'guideline_concept_groups':r.get('guideline_concept_groups'),
           'counts':r['retrieval_counts'],
           'top_candidates':{k:[{key:value for key,value in item.items() if key in fields} for item in rows[:3]]
                             for k,rows in r['provider_top'].items() if rows}} for r in observed['queries'][:1]],
       'regression_path':REGRESSION,'test_execution':'Protected existing tests plus supplemental regression run in network-isolated PHP sandbox',
       'quality_boundary':'Host measures coverage/noise and route preservation. Semantic relevance remains ASSISTED, not lexical truth.'}


def preflight(run):
    results={name:execute(run,[str(Path(run.state()['workspace'])/name)]) for name in TESTS}
    dump(run.path/'artifacts/search_baseline_tests.json',results)
    return results


def source_fragments(files):
    """Explicit partial source view, not a falsely complete/truncated artifact.

    Full sources and hashes remain saved. Exact patch matches are checked against
    the complete file. This finite adapter exposes search/query-concept methods.
    """
    result={};remaining=14000
    for name,text in files.items():
        spans=method_spans(text);chunks=[]
        if name==REGRESSION:spans={'output':(0,len(text))}
        for identifier,(start,end) in spans.items():
            if identifier not in {'preamble','search','queryConcepts','score','contains','evidenceConcepts','output'}:continue
            excerpt=text[start:end]
            if excerpt and len(excerpt.encode())<=remaining:
                chunks.append({'span_id':identifier,'text':excerpt,'complete_method':True,
                               'start_line':text[:start].count('\n')+1})
                remaining-=len(excerpt.encode())
        result[name]={'partial_view':True,'line_count':len(text.splitlines()),
            'source_sha256':__import__('hashlib').sha256(text.encode()).hexdigest(),
            'available_spans':{key:{'bytes':len(text[a:b].encode())} for key,(a,b) in spans.items()},'fragments':chunks}
    return result


def worker_schema(files, allowed):
    """Bound edit serialization, not the implementation or acceptance semantics."""
    choices=[]
    for name in allowed:
        text=files.get(name,'')
        spans=['output'] if name==REGRESSION else list(method_spans(text))
        choices.append({'type':'object','properties':{
            'path':{'type':'string','enum':[name]},
            'source_sha256':{'type':'string','enum':[__import__('hashlib').sha256(text.encode()).hexdigest()]},
            'span_id':{'type':'string','enum':spans},
            'before':{'type':'string','maxLength':350},
            'after':{'type':'string','maxLength':1000 if name==REGRESSION else 700}},
            'required':['path','source_sha256','span_id','before','after'],'additionalProperties':False})
    edit_schema={'type':'object','properties':{
        'edits':{'type':'array','items':{'oneOf':choices},'minItems':1,'maxItems':4},
        'summary':{'type':'string','maxLength':200},
        'findings':{'type':'array','items':{'type':'string'},'maxItems':0}},
        'required':['edits','summary','findings'],'additionalProperties':False}
    reads=[]
    for name in allowed:
        text=files.get(name,'')
        spans={'output':(0,len(text))} if name==REGRESSION else method_spans(text)
        identifiers=[key for key,(a,b) in spans.items() if len(text[a:b].encode())<=8000]
        if identifiers:
            reads.append({'type':'object','properties':{'path':{'type':'string','enum':[name]},
                'span_id':{'type':'string','enum':identifiers}},
                'required':['path','span_id'],'additionalProperties':False})
    return {'oneOf':[edit_schema,{'type':'object','properties':{
        'read_spans':{'type':'array','items':{'oneOf':reads},'minItems':1,'maxItems':1}},
        'required':['read_spans'],'additionalProperties':False}]}


def method_spans(text):
    matches=list(re.finditer(r'(?m)^[ \t]*(?:public|private|protected) static function ([A-Za-z_][A-Za-z_0-9]*)\s*\(',text))
    result={match.group(1):(match.start(),matches[i+1].start() if i+1<len(matches) else len(text))
            for i,match in enumerate(matches)}
    if matches:result['preamble']=(0,matches[0].start())
    return result


def read_spans(requests,files,allowed):
    if not isinstance(requests,list) or not 1<=len(requests)<=3:raise ValueError('Select 1..3 registered source spans')
    view=source_fragments(files);size=0
    for item in view.values():item['fragments']=[]
    for request in requests:
        if not isinstance(request,dict) or set(request)!={'path','span_id'}:raise ValueError('Read fields: path,span_id')
        name=request['path'];identifier=request['span_id']
        if name not in allowed:raise ValueError('Read outside registered source scope')
        text=files.get(name,'');spans={'output':(0,len(text))} if name==REGRESSION else method_spans(text)
        if identifier not in spans:raise ValueError('Unknown source span')
        a,b=spans[identifier];excerpt=text[a:b];size+=len(excerpt.encode())
        if size>8000:raise ValueError('Read exceeds 8000 bytes; select fewer source spans')
        item=view.setdefault(name,{})
        item.update(source_sha256=__import__('hashlib').sha256(text.encode()).hexdigest(),line_count=len(text.splitlines()),
                    available_spans={key:{'bytes':len(text[x:y].encode())} for key,(x,y) in spans.items()})
        if not item.get('_selected'):item.update(fragments=[],_selected=True)
        item['fragments'].append({'span_id':identifier,'text':excerpt,'complete_method':True})
    for name,item in view.items():
        item.pop('_selected',None)
        # Keep true full-source hashes even for omitted source bodies.
        item['source_sha256']=__import__('hashlib').sha256(files[name].encode()).hexdigest()
    return view


def span_edits(edits,files,allowed,replaceable_new_paths=()):
    """An explicit finite span disambiguates exact matches; never guess a match."""
    import hashlib
    if not isinstance(edits,list) or not 1<=len(edits)<=4:raise ValueError('Select 1..4 explicit span edits')
    positions={}
    for edit in edits:
        if not isinstance(edit,dict) or set(edit)!={'path','source_sha256','span_id','before','after'}:
            raise ValueError('Span edit fields: path,source_sha256,span_id,before,after')
        name=edit['path'];text=files.get(name,'')
        if name not in allowed or edit['source_sha256']!=hashlib.sha256(text.encode()).hexdigest():raise ValueError('Invalid scope/stale source hash')
        before,after=edit['before'],edit['after']
        if not isinstance(before,str) or not isinstance(after,str):raise ValueError('Span patch text required')
        if len(before)>350 or len(after)>(1000 if name==REGRESSION else 700):
            raise ValueError('Edit text exceeds the declared micro-patch budget')
        spans={'output':(0,len(text))} if name==REGRESSION else method_spans(text)
        if edit['span_id'] not in spans:raise ValueError('Unknown source span')
        start,end=spans[edit['span_id']];chunk=text[start:end]
        if not before:
            if name!=REGRESSION or text and name not in replaceable_new_paths:
                raise ValueError('Whole generation allowed only for new registered supplementary output')
            a,b=start,end
        else:
            if chunk.count(before)!=1:raise ValueError('Exact before must occur once in the selected span')
            a=start+chunk.index(before);b=a+len(before)
        positions.setdefault(name,[]).append((a,b,after))
    result={}
    for name,rows in positions.items():
        rows.sort();last=-1
        for a,b,_ in rows:
            if a<last:raise ValueError('Overlapping span edits')
            last=b
        text=files.get(name,'')
        for a,b,after in reversed(rows):text=text[:a]+after+text[b:]
        result[name]=text
    if sum(len(v.encode()) for v in result.values())>100000:raise ValueError('Edited source exceeds fixed budget')
    return result


def compact_worker_payload(payload):
    """No criterion changes: full contract saved, only duplicate/UI fields omitted."""
    result=dict(payload)
    contract=result['contract']
    result['contract_reference']={'task_id':contract['task_id'],'sha256':contract['sha256']}
    result['contract']={k:contract[k] for k in ('task_id','profile','allowed_paths','expected_outputs','existing_tests','validators','required_evidence',
                                              'max_attempts','timeout','verification_modes')}
    for name in ('artifact_input','prior_diagnosis','artifact_rules','web_runtime'):
        result.pop(name,None)
    context=dict(result['search_context'])
    context['queries']=[{**r,'top_candidates':{k:v for k,v in r['top_candidates'].items() if k=='guidelines'}}
                        for r in context['queries']]
    result['search_context']=context
    # Retry feedback was serialized as a JSON string and repeated every control
    # result. Keep the failed checks and actual stderr; full comparisons remain
    # in the immutable referenced Host evidence.
    if result.get('failure'):
        try:
            failure=json.loads(result['failure'][-1])
            result['failure']={k:failure[k] for k in ('checks','execution_failures') if k in failure}
            comparison=failure.get('search_comparison') or {}
            result['failure']['comparison']={k:comparison[k] for k in ('query','before_incomplete','after_incomplete',
                'guideline_before_count','guideline_after_count','before_reference','after_reference') if k in comparison}
        except (ValueError,TypeError):pass
    # Omit complete optional spans, never cut source text. Available span IDs and
    # hashes retain a bounded re-read path. Search/scoring/concept methods take
    # precedence over large constant preambles already reflected in real search.
    def size():return len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())
    if size()>21500:
        for view in result['source_files'].values():
            view['fragments']=[f for f in view['fragments'] if f['span_id']!='preamble']
    if size()>21500:
        for row in context['queries']:row['top_candidates']={}
    return result


def prepare(home, source, database, runtime, request, endpoint, model, query=None,gold_path=None):
    from .mission_controller import create_mission
    source=Path(source).resolve(); database=Path(database).resolve(); runtime=Path(runtime).resolve()
    query=query or '大動脈解離 リハビリテーション'
    if not query.strip() or query not in request:
        raise UnsupportedMission('Explicit --search-query must occur exactly in the original request')
    if not all((source/p).is_file() for p in TARGETS+TESTS):
        raise UnsupportedMission('Search-quality adapter requires registered PHP source/test paths')
    if not (runtime/'usr/bin/php8.5').is_file() or not database.is_file():
        raise UnsupportedMission('Explicit PHP runtime and real SQLite snapshot required')
    gold=None
    if gold_path:
        from .search_link_coverage import check_gold
        gold=check_gold(load(gold_path),request)
    package=Path(home)/'artifact_data'/'search_packages'/uuid.uuid4().hex
    package.mkdir(parents=True)
    hashes={}
    for directory in ('src','config','tests','public','migrations'):
        for path in (source/directory).rglob('*'):
            name=path.relative_to(source).as_posix()
            if path.is_symlink() or not path.is_file() or not visible(name):continue
            if path.suffix not in {'.php','.json','.sql','.js'} or path.stat().st_size>2_000_000:continue
            if 'production' in path.name or '.local.' in path.name:continue
            dest=package/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest);hashes[name]=sha(path)
    for name in ('schema.sql','config.example.php'):
        shutil.copy2(source/name,package/name);hashes[name]=sha(source/name)
    run=create_mission(home,package,request,endpoint,model)
    # Stable read-only SQLite input; backup API preserves consistency without writing origin.
    dbcopy=run.path/'artifacts/search_input.sqlite'
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as origin,sqlite3.connect(dbcopy) as dest:
        origin.backup(dest)
    coverage={}
    queries=[query,*CONTROLS]
    if gold:
        dump(run.path/'search_gold.json',gold)
        coverage={'gold_sha256':sha(run.path/'search_gold.json'),'gold_source_sha256':sha(gold_path)}
        queries=list(dict.fromkeys([q['query'] for q in gold['queries'] if q['acceptance']]+CONTROLS+gold['holdout']))
    dump(run.path/'search_binding.json',{'source':str(source),'source_hashes':hashes,
       'database_source':str(database),'database':str(dbcopy),'database_sha256':sha(dbcopy),
       'runtime':str(runtime),'queries':queries,**coverage,
       'snapshot_scope':'Full paper/guideline search tables; real official top-120 candidates per target/control query, not full official index'})
    return run


def php_args(run):
    binding=load(run.path/'search_binding.json')
    modules=Path(binding['runtime'])/'usr/lib/php'
    names=[p.name for p in modules.iterdir() if p.is_dir() and p.name.isdigit()]
    if len(names)!=1:raise HostDefect('Ambiguous PHP module ABI')
    prefix='/tmp/pitchi-php/usr/lib/php/'+names[0]
    return ['/usr/bin/env','LD_LIBRARY_PATH=/tmp/pitchi-php/usr/lib/x86_64-linux-gnu',
       '/tmp/pitchi-php/usr/bin/php8.5','-n',
       '-d','extension='+prefix+'/pdo.so','-d','extension='+prefix+'/pdo_sqlite.so',
       '-d','extension='+prefix+'/mbstring.so','-d','zend.assertions=1','-d','assert.exception=1',
       '-d','display_errors=stderr','-d','log_errors=0']


def execute(run, arguments):
    binding=load(run.path/'search_binding.json')
    # Same Host runner: no network, credentials, original filesystem or writable DB.
    result=invocation(run,php_args(run)+arguments,120,
       [(Path(binding['runtime']),'/tmp/pitchi-php'),
        (Path(run.state()['workspace']),'/tmp/project'),
        (Path(binding['database']),'/tmp/evidence.sqlite'),
        (run.path/'artifacts/search_queries.json','/tmp/queries.json'),
        (Path(__file__).with_name('search_probe.php'),'/tmp/host-search-probe.php')])
    return result


def runtime_failure_owner(run, receipt, name):
    origin=re.search(r' in /tmp/project/(src/[^\s:]+\.php)(?::| on line)',receipt['stderr'])
    if name!='before' and (run.path/'artifacts/search_before.json').exists() and origin:
        path=origin.group(1);baseline=run.path/'baseline_files'/path
        if path in TARGETS and baseline.exists() and sha(Path(run.state()['workspace'])/path)!=sha(baseline):
            return 'worker'
    return 'host'


def observe(run, name):
    binding=load(run.path/'search_binding.json')
    dump(run.path/'artifacts/search_queries.json',binding['queries'])
    receipt=execute(run,['/tmp/host-search-probe.php'])
    if receipt['exit_code']:
        dump(run.path/'artifacts'/('search_'+str(name)+'_failure.json'),receipt)
        if runtime_failure_owner(run,receipt,name)=='worker':
            return {'implementation_runtime_failure':True,'execution':receipt,
                    'failure_reference':'artifacts/search_'+str(name)+'_failure.json'}
        raise HostDefect('Search evidence collector failed: '+receipt['stderr'][-1800:])
    try:result=json.loads(receipt['stdout'])
    except ValueError as exc:raise HostDefect('Search collector did not return JSON') from exc
    receipt['stdout']='saved separately as structured search observations'
    dump(run.path/'artifacts'/('search_'+name+'.json'),{'observations':result,'execution':receipt,
           'database_sha256':binding['database_sha256']})
    from .search_evidence import record
    record(run,name,result,'artifacts/search_'+name+'.json',Path(__file__).with_name('search_probe.php'),Path(binding['database']))
    return result


def validate(run, contract, attempt):
    binding=load(run.path/'search_binding.json');workspace=Path(run.state()['workspace'])
    if any(sha(Path(binding['source'])/n)!=h for n,h in binding['source_hashes'].items()):
        raise HostDefect('Original project changed; do not overwrite user changes')
    if sha(binding['database'])!=binding['database_sha256']:raise HostDefect('Fixed search DB changed')
    checks=[];executions=[];diffs={}
    for name in contract['allowed_paths']:
        result=execute(run,['-l',str(workspace/name)]);executions.append(result)
        checks.append({'validator':'php_syntax:'+name,'pass':result['exit_code']==0})
        baseline=run.path/'baseline_files'/name
        before=baseline.read_text() if baseline.exists() else '';after=(workspace/name).read_text() if (workspace/name).exists() else ''
        diffs[name]=''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=name,tofile=name))
    for name in TESTS:
        result=execute(run,[str(workspace/name)]);executions.append(result)
        # Protected existing tests, never newly authored Worker tests.
        checks.append({'validator':'protected_test:'+name,'pass':result['exit_code']==0})
    if not (workspace/REGRESSION).is_file():
        checks.append({'validator':'supplementary_regression','pass':False})
    else:
        result=execute(run,[str(workspace/REGRESSION)]);executions.append(result)
        checks.append({'validator':'supplementary_regression','pass':result['exit_code']==0,
                       'authority':'supplementary only; Host real-search assertions remain mandatory'})
    after=observe(run,str(attempt));before=load(run.path/'artifacts/search_before.json')['observations']
    if after.get('implementation_runtime_failure'):
        executions.append(after['execution'])
        checks.insert(0,{'validator':'search_implementation_runtime','pass':False})
        return checks,executions,diffs,{'failure_owner':'worker','failure_reference':after['failure_reference'],
            'before_reference':'artifacts/search_before.json','verification':'No search-quality PASS: modified implementation failed at runtime'}
    def signature(row):
        return {k:len(v) for k,v in row['provider_top'].items()}
    controls=[]
    for b,a in zip(before['queries'][1:],after['queries'][1:]):
        controls.append({'query':b['query'],'before':signature(b),'after':signature(a),
                         'nonempty_routes_preserved':all(not count or signature(a).get(k,0)>0 for k,count in signature(b).items())})
    checks.append({'validator':'control_nonempty_routes','pass':all(c['nonempty_routes_preserved'] for c in controls)})
    b=before['queries'][0];a=after['queries'][0]
    with sqlite3.connect('file:'+binding['database']+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        canonical={}
        consistent=True
        metadata={}
        if binding.get('gold_sha256'):
            from .search_link_coverage import canonical_metadata
            metadata=canonical_metadata(run)
        for query_row in after['queries']:
            for row in query_row['provider_top']['guidelines']:
                source=db.execute('SELECT * FROM guideline_sections WHERE id=?',(row.get('id'),)).fetchone()
                if source is None and row.get('id') in metadata:
                    source=metadata[row['id']]
                if source is None or row.get('section_title')!=source['section_title']:
                    consistent=False;continue
                canonical[row['id']]=dict(source)
                if any(row[k]!=source[k] for k in row if k in source.keys()):consistent=False
        # Baseline rows are also resolved from the fixed DB, not editable counts.
        for row in b['provider_top']['guidelines']:
            source=db.execute('SELECT * FROM guideline_sections WHERE id=?',(row.get('id'),)).fetchone()
            if source is not None:canonical[row['id']]=dict(source)
    checks.append({'validator':'guideline_rows_match_fixed_source','pass':consistent})
    def incomplete(rows):
        # Independent Host observation; never trust editable provider hit counts.
        concepts=b['concepts']
        return sum(not all(any(alias.casefold() in json.dumps(
            {k:v for k,v in canonical.get(row['id'],{}).items() if k not in {'matched_terms','strong_matched_terms','matched_fields',
              'matched_concept_count','query_concept_count','relevance_score','search_relevance'}},
            ensure_ascii=False).casefold() for alias in concept['aliases']) for concept in concepts) for row in rows)
    improvement={'query':b['query'],'guideline_before_count':len(b['provider_top']['guidelines']),
       'guideline_after_count':len(a['provider_top']['guidelines']),
       'before_incomplete':incomplete(b['provider_top']['guidelines']),
       'after_incomplete':incomplete(a['provider_top']['guidelines']),
       'verification':'ASSISTED: concept fields and count comparisons do not prove semantic relevance',
       'scope':binding['snapshot_scope'],
       'controls':controls,'official_data_available':a['official_available'],
       'before_reference':'artifacts/search_before.json','after_reference':'artifacts/search_'+str(attempt)+'.json'}
    dump(run.path/'artifacts'/('search_comparison_'+str(attempt)+'.json'),improvement)
    from .search_evidence import compare, pipeline
    # Older active runs may have a normal search_before collector but predate
    # the pipeline projection.  Derive a bounded projection from that
    # Host-owned observation for the current run only; never mutate a terminal
    # saved run or treat this projection as a new search result.
    before_pipeline_path=run.path/'artifacts/search_pipeline_before.json'
    if before_pipeline_path.exists():
        before_pipeline=load(before_pipeline_path)
    else:
        before_pipeline=pipeline(before)
        source_path=run.path/'artifacts/search_before.json'
        before_pipeline['provenance']={'authority':'host','source_reference':'artifacts/search_before.json',
            'source_sha256':sha(source_path),'data_sha256':binding['database_sha256'],
            'producer_sha256':None,'note':'Legacy saved collector; original producer hash unavailable'}
        dump(before_pipeline_path,before_pipeline)
    pipeline_comparison=compare(before_pipeline,
        load(run.path/'artifacts'/('search_pipeline_'+str(attempt)+'.json')),binding['queries'][1:])
    dump(run.path/'artifacts'/('search_pipeline_comparison_'+str(attempt)+'.json'),pipeline_comparison)
    if binding.get('gold_sha256'):
        from .search_link_coverage import validate_links
        coverage_checks,coverage=validate_links(run,before,after)
        checks.extend(coverage_checks)
        improvement['link_coverage']=coverage
        dump(run.path/'artifacts'/('search_comparison_'+str(attempt)+'.json'),improvement)
    else:
        checks.append({'validator':'target_incomplete_reduction','pass':improvement['after_incomplete']<improvement['before_incomplete']})
    checks.append({'validator':'real_search_paths_observed','pass':all(k in a['provider_top'] for k in ('papers','guidelines','documents','official_documents'))})
    # Absence of real official data is not evidence that that production route is correct.
    checks.append({'validator':'official_real_data_available','pass':a['official_available']})
    return checks,executions,diffs,improvement
