"""Formal metadata CLI; all mutations restricted to an owned isolated workspace."""
import argparse, contextlib, datetime, difflib, fcntl, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, uuid
from pathlib import Path
from urllib.parse import urlsplit
from .verification import evaluate

TESTS=['tests/search_quality_regression.php','tests/guideline_section_search.php','tests/search_result_ranking.php','tests/evidence_search.php','tests/official_document_search.php']
class Invalid(ValueError):pass
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def json_text(v):return json.dumps(v,ensure_ascii=False,indent=2)+'\n'
def atomic(p,data):
    p.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=p.parent,prefix='.ptot-')
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,p)
    finally:
        if os.path.exists(temp):os.unlink(temp)
def save(p,v):atomic(p,json_text(v).encode())
def load(p):
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out:raise Invalid('Duplicate JSON key: '+k)
            out[k]=v
        return out
    return json.loads(Path(p).read_text(),object_pairs_hook=unique)
def manifest(root):
    result={}
    for p in root.rglob('*'):
        if p.is_symlink():raise Invalid('Symlink in project: '+str(p))
        if p.is_file():result[p.relative_to(root).as_posix()]=sha(p)
    return result

def metadata(p):
    m=load(p)
    if not isinstance(m,dict):raise Invalid('Metadata must be an object')
    for k in ['id','title','issuer','edition','official_url','language','terms_status','allowed_scope','ingestion_status','checked_at']:
        if not isinstance(m.get(k),str) or not m[k].strip():raise Invalid('Missing/non-string: '+k)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}',m['id']):raise Invalid('Unsafe document ID')
    if type(m.get('revision_year'))!=int or not 1900<=m['revision_year']<=2100:raise Invalid('Invalid revision_year')
    for k in ['diseases','synonyms','search_terms','gold_ids']:
        if not isinstance(m.get(k),list) or any(not isinstance(v,str) or not v.strip() for v in m[k]):raise Invalid('Invalid list: '+k)
        if len(m[k])!=len(set(m[k])):raise Invalid('Duplicate values: '+k)
    if not m['diseases'] or not m['search_terms'] or not m['gold_ids']:raise Invalid('Disease/search/gold lists must not be empty')
    u=urlsplit(m['official_url'])
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443) or re.search(r'\s',m['official_url']):raise Invalid('Official URL must be credential-free HTTPS')
    if m['allowed_scope']!='metadata_only' or m['ingestion_status']!='metadata_only':raise Invalid('Only metadata_only registration is supported')
    if m['terms_status'] not in ['link_metadata_only','review_pending','permission_confirmed']:raise Invalid('Unknown terms_status')
    datetime.date.fromisoformat(m['checked_at'])
    if any(k in m for k in ['body','full_text','pdf_bytes','ai_summary','summary','introduction_ja']):raise Invalid('This CLI registers bibliography only; no body or summaries')
    return m

def catalog(project):
    result={};urls={}
    for p in (project/'config/guideline_metadata').glob('*.json'):
        m=metadata(p)
        if m['id'] in result or m['official_url'] in urls:raise Invalid('Duplicate catalog ID or official URL')
        if p.name!=m['id']+'.json':raise Invalid('Catalog filename must match ID')
        result[m['id']]=m;urls[m['official_url']]=m['id']
    return result

def gold(p):
    g=load(p)
    if not isinstance(g,dict) or not isinstance(g.get('queries'),list) or any(not isinstance(q,dict) for q in g['queries']):raise Invalid('Gold must contain a query object list')
    accepted=[q for q in g['queries'] if q.get('acceptance') is True]
    if g.get('frozen_before_implementation') is not True or g.get('required_mode')!='metadata_only' or not accepted:raise Invalid('Frozen metadata-only expectations required')
    if len({q['query'] for q in g['queries']})!=len(g['queries']):raise Invalid('Duplicate gold query')
    for q in accepted:
        if not q.get('query') or type(q.get('max_rank'))!=int or q['max_rank']<1:raise Invalid('Invalid gold query/rank')
        if not isinstance(q.get('expected'),dict):raise Invalid('Gold expectation must be an object')
        if any(not q['expected'].get(k) for k in ['id','title','issuer','edition','revision_year','official_url']):raise Invalid('Exact bibliographic gold required')
    return g

def support_plan(project):
    patch=Path(__file__).with_name('search-support.patch')
    p=subprocess.run(['git','apply','--check',str(patch)],cwd=project,capture_output=True,text=True)
    if p.returncode==0:return patch
    # Exact reverse applicability identifies the already installed stable implementation.
    p=subprocess.run(['git','apply','--reverse','--check',str(patch)],cwd=project,capture_output=True,text=True)
    if p.returncode!=0:raise Invalid('Search implementation differs from supported baseline; review user changes before applying')
    return None

@contextlib.contextmanager
def session(path):
    root=Path(path).absolute()
    if root.is_symlink() or not (root/'session.json').is_file():raise Invalid('Only CLI-created isolated workspaces are writable')
    if any(p.is_symlink() for p in root.parents):raise Invalid('Symlink workspace parent')
    with (root/'lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=load(root/'session.json')
        if state.get('kind')!='ptotsearch-bibliography-isolated' or state['workspace']!=str(root):raise Invalid('Workspace identity mismatch')
        pending=root/'pending.json'
        if pending.exists():
            tx=load(pending)
            if tx.get('status') not in ('PREPARED','COMMITTED') or not re.fullmatch(r'backup-[a-zA-Z0-9_-]+',tx.get('backup','')) or not re.fullmatch(r'stage-[a-zA-Z0-9_-]+',tx.get('stage','')):raise Invalid('Invalid pending transaction; preserve evidence and stop')
            backup=root/tx['backup'];stage=root/tx['stage']
            if tx['status']=='PREPARED':
                if backup.exists():
                    if (root/'project').exists():shutil.move(str(root/'project'),str(root/('failed-'+uuid.uuid4().hex)))
                    os.replace(backup,root/'project')
                state=tx['prior_state'];save(root/'session.json',state)
                tx['status']='ROLLED_BACK';save(root/'history'/('recovery-'+uuid.uuid4().hex+'.json'),tx)
            pending.unlink()
            if stage.exists():shutil.rmtree(stage)
        project=root/'project'
        if manifest(project)!=state['current_manifest']:raise Invalid('Workspace changed outside registration CLI')
        for p,h in state['inputs'].items():
            if sha(p)!=h:raise Invalid('Pinned input changed: '+p)
        if manifest(Path(state['source']))!=state['origin_manifest']:raise Invalid('Original source changed')
        if sha(root/'search.sqlite')!=state['database_sha256']:raise Invalid('Fixed DB copy changed')
        yield root,project,state

def initialize(args):
    root=Path(args.workspace).absolute();source=Path(args.source).resolve()
    if root.exists() or source==root or source in root.parents:raise Invalid('Workspace must be new and separate from source')
    if any(p.is_symlink() for p in root.parents):raise Invalid('Symlink workspace parent')
    source_manifest=manifest(source)
    for n in TESTS+['src/GuidelineSectionSearch.php','public/assets/app.js','schema.sql','config.example.php']:
        if n not in source_manifest:raise Invalid('Required existing application input absent: '+n)
    regression=load(args.regressions)
    if len(regression['holdout'])!=3 or len(regression['forbidden_ids'])!=6:raise Invalid('Three fixed holdouts and six forbidden IDs required')
    paths=[Path(args.database).resolve(),Path(args.regressions).resolve(),*[Path(p).resolve() for p in args.expectations]]
    for p in args.expectations:
        g=gold(p)
        if g['holdout']!=regression['holdout']:raise Invalid('Gold and regression holdouts differ')
    root.mkdir(parents=True,exist_ok=False)
    try:
        shutil.copytree(source,root/'project')
        initial_entries=catalog(root/'project')
        support_plan(root/'project')
        with sqlite3.connect('file:'+str(Path(args.database).resolve())+'?mode=ro',uri=True) as src,sqlite3.connect(root/'search.sqlite') as dest:src.backup(dest)
        (root/'history').mkdir()
        state={'kind':'ptotsearch-bibliography-isolated','workspace':str(root),'source':str(source),'origin_manifest':source_manifest,
               'baseline_manifest':manifest(root/'project'),'current_manifest':manifest(root/'project'),
               'database_sha256':sha(root/'search.sqlite'),'inputs':{str(p):sha(p) for p in paths},
               'runtime':str(Path(args.php_runtime).resolve()),'regressions':str(Path(args.regressions).resolve()),
               'expectations':[str(Path(p).resolve()) for p in args.expectations],'registered':initial_entries,'version':1}
        # Existing collector provides before/holdout evidence, even when raw baseline fails tests.
        out=root/'history/baseline';out.mkdir()
        spec=verification_spec(root,state,state['expectations'],[])
        spec['oracle']=str(root/'before.json')
        # Collector is run once independently to establish baseline before checking gold.
        from .verification import collect
        collect(spec,root/'project',out)
        shutil.copy2(out/'observations.json',root/'before.json')
        state['before_sha256']=sha(root/'before.json');save(root/'session.json',state)
    except BaseException:
        shutil.rmtree(root);raise
    return {'initialized':True,'workspace':str(root),'baseline_reference':'history/baseline/observations.json','production_changed':False}

def verification_spec(root,state,expectations,entries):
    regression=load(state['regressions']);queries=[]
    for p in expectations:queries.extend(gold(p)['queries'])
    merged={'queries':[q for q in queries if q.get('acceptance') is True],'holdout':regression['holdout']}
    # Same query in different files is invalid; do not silently override frozen expectations.
    if len({q['query'] for q in merged['queries']})!=len(merged['queries']):raise Invalid('Overlapping selected expectations')
    gp=root/'selected-gold.json';save(gp,merged)
    collector=Path(__file__).parents[1]/'pitchi_work/search_probe.php'
    return {'runtime':state['runtime'],'database':str(root/'search.sqlite'),'gold':str(gp),'oracle':str(root/'before.json'),
            'collector':str(collector),'api_probe':str(Path(__file__).with_name('api_probe.php')),
            'protected_tests':TESTS,'expected_metadata':entries,**regression}

def plan(project,m):
    entries=catalog(project)
    if m['id'] in entries:
        if entries[m['id']]!=m:raise Invalid('ID conflict: existing metadata differs; no overwrite')
        return None,{'noop':True,'id':m['id'],'changes':[]}
    if any(x['official_url']==m['official_url'] for x in entries.values()):raise Invalid('URL already registered under another ID')
    patch=support_plan(project);name='config/guideline_metadata/'+m['id']+'.json'
    diff=''.join(difflib.unified_diff([],json_text(m).splitlines(True),fromfile='/dev/null',tofile=name))
    return patch,{'noop':False,'id':m['id'],'changes':[name]+(['src/GuidelineSectionSearch.php','public/assets/app.js'] if patch else []),
                 'metadata_diff':diff,'search_support_diff':patch.read_text() if patch else ''}

def register(root,project,state,m):
    patch,preview=plan(project,m)
    if preview['noop']:return {'registered':True,**preview}
    token=uuid.uuid4().hex;stage=root/('stage-'+token);backup=root/('backup-'+token)
    shutil.copytree(project,stage)
    try:
        if patch:
            p=subprocess.run(['git','apply',str(patch)],cwd=stage,capture_output=True,text=True)
            if p.returncode:raise Invalid('Support patch failed: '+p.stderr)
        save(stage/'config/guideline_metadata'/(m['id']+'.json'),m)
        catalog(stage);new_manifest=manifest(stage)
        allowed=set(preview['changes'])
        if any(new_manifest.get(k)!=v for k,v in state['current_manifest'].items() if k not in allowed) or set(new_manifest)-set(state['current_manifest'])-allowed:
            raise Invalid('Unexpected registration change')
        tx={'status':'PREPARED','backup':backup.name,'stage':stage.name,'prior_state':state,'preview':preview}
        save(root/'pending.json',tx);os.replace(project,backup);os.replace(stage,project)
        next_state={**state,'current_manifest':new_manifest,'registered':{**state['registered'],m['id']:m}}
        save(root/'session.json',next_state);tx['status']='COMMITTED';save(root/'pending.json',tx)
        receipt={'registered':True,'noop':False,'id':m['id'],'changes':preview['changes'],'metadata_sha256':sha(project/'config/guideline_metadata'/(m['id']+'.json')),'backup':backup.name}
        save(root/'history'/('register-'+token+'.json'),receipt);(root/'pending.json').unlink()
        return receipt
    except BaseException:
        if backup.exists():
            if project.exists():shutil.rmtree(project)
            os.replace(backup,project);save(root/'session.json',state)
        (root/'pending.json').unlink(missing_ok=True)
        raise
    finally:
        if stage.exists():shutil.rmtree(stage)

def main():
    parser=argparse.ArgumentParser(prog='ptot-guidelines',description='Metadata-only guideline registration in owned isolated workspaces; no production operation')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('init')
    for n in ['source','workspace','database','php-runtime','regressions']:p.add_argument('--'+n,required=True)
    p.add_argument('--expectations',action='append',required=True)
    for c in ['validate','preview','register','verify']:
        p=sub.add_parser(c);p.add_argument('--workspace',required=c!='validate')
        if c!='verify':p.add_argument('--metadata',required=True)
        else:p.add_argument('--expectations',action='append');p.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.command=='init':result=initialize(args)
        elif args.command=='validate' and not args.workspace:
            m=metadata(args.metadata);result={'valid':True,'id':m['id'],'url_format_checked':True,'official_status_not_inferred':True}
        else:
            with session(args.workspace) as (root,project,state):
                if args.command in ['validate','preview','register']:
                    m=metadata(args.metadata);patch,preview=plan(project,m)
                    result={'valid':True,'id':m['id'],'noop':preview['noop']} if args.command=='validate' else preview
                    if args.command=='register':result=register(root,project,state,m)
                else:
                    expectations=[str(Path(p).resolve()) for p in (args.expectations or state['expectations'])]
                    if not set(expectations)<=set(state['expectations']):raise Invalid('Expectations were not pinned at initialization')
                    if sha(root/'before.json')!=state['before_sha256']:raise Invalid('Baseline evidence changed')
                    out=Path(args.output).absolute()
                    source=Path(state['source'])
                    if out.exists() or out==root or root in out.parents or out==source or source in out.parents or any(p.is_symlink() for p in out.parents):raise Invalid('Output must be new and outside workspace/source; no symlink parents')
                    out.mkdir(parents=True,exist_ok=False)
                    spec=verification_spec(root,state,expectations,list(catalog(project).values()))
                    result=evaluate(spec,project,out,state['baseline_manifest'])
                    result['input_hashes']=state['inputs'];result['database_sha256']=state['database_sha256']
                    save(out/'result.json',result)
        print(json_text(result),end='')
        return 0 if result.get('passed',True) else 1
    except (Invalid,OSError,ValueError,KeyError,subprocess.SubprocessError) as e:
        print(json_text({'error':str(e),'changed_production':False}),file=sys.stderr,end='');return 2
if __name__=='__main__':sys.exit(main())
