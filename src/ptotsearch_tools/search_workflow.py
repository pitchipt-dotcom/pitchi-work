"""Reviewed search-quality capability: dispatch existing v2, never implement edits."""
import argparse,base64,difflib,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
from .cli import load,save,sha
from . import remote
PROJECT='ptotsearch-quality'

def host_digest():
    h=hashlib.sha256()
    for f in sorted((Path(__file__).parents[1]/'pitchi_work').glob('*.py')):h.update(f.name.encode());h.update(f.read_bytes())
    return h.hexdigest()

def submit(cfg,request,config_path):
    from .workflow import start,view,asset_digest,event
    if set(request)-{'action','project','request','operation'} or request.get('operation','create')!='create':raise ValueError('Search capability allows bounded create only')
    text=request.get('request');c=cfg['search_quality']
    if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError('Bounded request required')
    if host_digest()!=c['reviewed_host_sha256']:raise ValueError('Existing v2 source review required')
    known=c['query'] in text
    if known and c.get('successful_run_id'):
        from .workflow import folder
        previous=folder(cfg,c['successful_run_id']);state=load(previous/'state.json');plan=load(previous/'plan.json')
        if state.get('kind')!='search_quality' or sha(previous/'plan.json')!=state['plan_sha256'] or not plan['isolated_verification']['passed']:
            raise ValueError('Saved search result is not verified')
        if sha(previous/'verification/result.json')!=plan['isolated_verification']['sha256'] or plan['search_summary']['before_after']['query']!=c['query']:
            raise ValueError('Saved search evidence/query changed')
        host=previous/'host-runtime/runs'/plan['host_run_id']
        if load(host/'state.json')['status']!='COMPLETED':raise ValueError('Saved Host did not complete')
        return {**view(cfg,previous),'reused_result':True,'new_llm_calls':0}
    p=Path(cfg['home'])/'tool_jobs'/('gl-'+uuid.uuid4().hex);p.mkdir(parents=True,mode=0o700);(p/'inputs').mkdir()
    save(p/'state.json',{'run_id':p.name,'kind':'search_quality','status':'RUNNING' if known else 'NEEDS_HUMAN','stage':'HOST_ADMISSION','original_request':text,
        'selected_tool':'pitchi-work-v2:edit-steps','message':'既存v2で既知解を隔離再現します。' if known else 'この検索問題の期待結果とHostが扱える手順は未確定です。調査・設計の確認が必要です。',
        'question':None if known else '対象検索語と期待する結果を提示してください。未知問題の自律改善は未実証です。','llm_calls':0,'classification':'CODEX_DESIGNED_STEPS / KNOWN_SOLUTION','asset_sha256':asset_digest()})
    save(p/'inputs/config.json',cfg);save(p/'inputs/documents.json',[])
    if known:
        from pitchi_work.recovery import manifest
        for key in ('steps','mission'):
            src=Path(c[key]);dest=p/'inputs'/src.name;dest.write_bytes(src.read_bytes())
        save(p/'inputs/frozen.json',{'source_manifest':manifest(Path(c['source'])),'database_sha256':sha(c['database']),'steps_sha256':sha(c['steps']),'mission_sha256':sha(c['mission']),'holdout_sha256':sha(c['holdout']),'regressions_sha256':sha(cfg['regressions'])})
        event(p,'search_request',request=text,classification='KNOWN_SOLUTION',local_calls_max=24,operations_max=16)
        try:start(cfg,config_path,p,worker='search')
        except Exception as e:
            from .workflow import update
            update(p,status='FAILED',message=str(e),failure_class='HOST_DISPATCH')
    return view(cfg,p)

def check_inputs(cfg,p):
    from pitchi_work.recovery import manifest
    c=cfg['search_quality'];f=load(p/'inputs/frozen.json')
    if manifest(Path(c['source']))!=f['source_manifest']:raise ValueError('Fixed source changed')
    for key in ('database','steps','mission','holdout'):
        if sha(c[key])!=f[key+'_sha256']:raise ValueError('Fixed input changed: '+key)
    for key in ('steps','mission'):
        if sha(p/'inputs'/Path(c[key]).name)!=f[key+'_sha256']:raise ValueError('Copied input changed')
    if sha(cfg['regressions'])!=f['regressions_sha256']:raise ValueError('Regression gold changed')
    steps=load(c['steps'])
    if len(steps['steps'])!=6 or steps['max_local_calls']!=24 or steps['classification']!='CODEX_DESIGNED_STEPS':raise ValueError('Unsupported known-solution contract')

def command(c,p,endpoint,model,parent=None):
    home=p/'host-runtime'
    base=[sys.executable,'-B','-m','pitchi_work.cli','--home',str(home)]
    if parent:return base+['recover',parent]
    return base+['start','--workspace',c['source'],'--request-file',str(p/'inputs'/Path(c['mission']).name),'--edit-steps',str(p/'inputs'/Path(c['steps']).name),
        '--endpoint',endpoint,'--model',model,'--search-database',c['database'],'--php-runtime',c['php_runtime'],'--no-resolution-memory']

def drive(c,p,endpoint,model):
    from .workflow import update
    from pitchi_work.store import RunStore
    home=p/'host-runtime';latest=None
    for attempt in range(2): # one formal recover, same ledger; no counter edits
        args=command(c,p,endpoint,model,latest if attempt else None)
        before=set((home/'runs').glob('*/state.json')) if (home/'runs').exists() else set()
        with (p/('host-%d.log'%attempt)).open('xb') as log:
            proc=subprocess.Popen(args,stdout=log,stderr=log,env={**{k:os.environ[k] for k in ('PATH','HOME','LANG') if k in os.environ},'PYTHONPATH':str(Path(__file__).parents[1]),'PYTHONDONTWRITEBYTECODE':'1'},start_new_session=True)
            started=time.monotonic()
            while proc.poll() is None:
                paths=[x for x in (home/'runs').glob('*/state.json') if x not in before]
                if paths:
                    latest=paths[-1].parent.name;s=load(paths[-1]);calls=sum(len(load(x)['data']['reservations']) for x in (home/'local_repair_budgets').glob('*.json'));update(p,host_run_id=latest,llm_calls=calls,stage=s.get('stage','HOST_RUNNING'),model=model,message='既存v2で編集・検証を進めています。')
                    if (p/'cancel.json').exists() or time.monotonic()-started>1200:RunStore(paths[-1].parent).request_cancel()
                if time.monotonic()-started>1320:
                    proc.terminate();proc.wait(timeout=10);raise ValueError('Host timeout; preserved for formal recovery')
                time.sleep(1)
        paths=[x for x in (home/'runs').glob('*/state.json') if x not in before]
        if len(paths)!=1:raise ValueError('Expected one formal v2 run')
        run=RunStore(paths[0].parent);latest=run.run_id
        save(p/('host-dispatch-%d.json'%attempt),{'argv':args,'exit_code':proc.returncode,'host_run_id':latest,'status':run.state()['status']})
        calls=sum(len(load(x)['data']['reservations']) for x in (home/'local_repair_budgets').glob('*.json'));update(p,host_run_id=latest,llm_calls=calls,model=model)
        if calls>24:raise ValueError('Local call budget exceeded')
        if proc.returncode==0 and run.state()['status']=='COMPLETED':return run
        if attempt or run.state()['status']!='STALLED' or (p/'cancel.json').exists():break
    raise ValueError('Existing Host did not complete; preserved run '+str(latest))

def verify_and_plan(cfg,p,run):
    from .workflow import update,asset_digest
    from pitchi_work.search_quality import prepare,observe
    c=cfg['search_quality'];fixed=load(c['holdout']);queries=[x['query'] for x in fixed['queries']];observations={}
    for label,source in [('before',c['source']),('after',run.state()['workspace'])]:
        audit=prepare(p/'audit-runtime',source,c['database'],c['php_runtime'],c['query'],'http://127.0.0.1:1/v1','no-model-called',query=c['query'])
        binding=load(audit.path/'search_binding.json');binding['queries']=[c['query'],*queries];save(audit.path/'search_binding.json',binding)
        observations[label]=observe(audit,label)
    signature=lambda q:({k:[x['id'] for x in v] for k,v in q['provider_top'].items()},[(x.get('category'),x['id']) for x in q['final_ranking']])
    checks=[{'name':'fixed_holdout:'+b['query'],'pass':signature(b)==signature(a)} for b,a in zip(observations['before']['queries'][1:],observations['after']['queries'][1:])]
    before=observations['before']['queries'][0];after=observations['after']['queries'][0];forbidden=set(load(cfg['regressions'])['forbidden_ids'])
    checks.append({'name':'old_noise_absent','pass':not forbidden.intersection(x['id'] for x in after['provider_top']['guidelines'])})
    root=load(run.path/'artifacts/P1_host_root.json');checks.extend({'name':x['validator'],'pass':x['pass']} for x in root['checks']);checks.append({'name':'existing_host_root_acceptance','pass':root.get('passed',root.get('pass',False))})
    checks.append({'name':'formal_completion','pass':run.state()['status']=='COMPLETED'})
    results={'passed':all(x['pass'] for x in checks),'checks':checks,'before_after':{'query':c['query'],'before_ids':[x['id'] for x in before['provider_top']['guidelines']],'after_ids':[x['id'] for x in after['provider_top']['guidelines']]},'host_root':root,'classification':'CODEX_DESIGNED_STEPS / KNOWN_SOLUTION'}
    (p/'verification').mkdir();save(p/'verification/result.json',results);save(p/'observations.json',observations)
    if not results['passed']:raise ValueError('Independent search acceptance failed')
    target='src/GuidelineSectionSearch.php';src=Path(c['source'])/target;dest=Path(run.state()['workspace'])/target
    old=sha(src);new=sha(dest);probe=remote.call(cfg['production'],'inspect',{'app':cfg['production']['app'],'files':[target]})
    bundle={target:{'before_sha256':old,'after_sha256':new,'content_b64':base64.b64encode(dest.read_bytes()).decode()}}
    save(p/'bundle.json',bundle);save(p/'production-before.json',probe)
    from .publication import public_observe
    public=public_observe(cfg['production']['public_origin'],[{'query':c['query']}],queries);save(p/'public-before.json',public)
    diff=''.join(difflib.unified_diff(src.read_text().splitlines(True),dest.read_text().splitlines(True),fromfile='before/'+target,tofile='after/'+target));(p/'changes.diff').write_text(diff)
    save(p/'evidence.json',{'frozen_sha256':sha(p/'inputs/frozen.json'),'observations_sha256':sha(p/'observations.json'),'diff_sha256':sha(p/'changes.diff'),'host_run_id':run.run_id})
    eligible=probe['files'][target]['sha256']==old
    plan={'kind':'search_quality','documents':[],'files':{target:{'before_sha256':old,'after_sha256':new}},'change_count':1,'database_changes':0,
        'duplicate_check':'isolated known-solution replay; live source is already updated' if not eligible else 'live before hash matches',
        'publication_eligible':eligible,'publication_block_reason':None if eligible else '修正前fixtureと現在の本番が異なります。同じ既知解の二重適用は禁止。現行ソースから別案件として再設計・検証・再承認が必要です。',
        'search_summary':results,'diff':diff,'host_run_id':run.run_id,'model':run.state().get('local_model'),
        'isolated_verification':{'passed':True,'checks':len(checks),'sha256':sha(p/'verification/result.json')},'evidence_sha256':sha(p/'evidence.json'),
        'backup':{'status':'current production files saved read-only; remote private backup required before any approved write','sha256':sha(p/'production-before.json'),'remote_reference':cfg['production']['backup_root']+'/'+p.name+'/before.json'},
        'rollback':'Restore only changed files from verified private backup; never restore live DB',
        'bundle_sha256':sha(p/'bundle.json'),'config_sha256':sha(p/'inputs/config.json'),'public_before_sha256':sha(p/'public-before.json'),'asset_sha256':asset_digest(),'expectations_sha256':{}}
    save(p/'plan.json',plan);update(p,status='NEEDS_HUMAN',stage='PRODUCTION_APPROVAL',plan_sha256=sha(p/'plan.json'),challenge=uuid.uuid4().hex,verification=plan['isolated_verification'],message='既知解の新規隔離再現と独立検証に合格しました。本番は未変更です。'+(plan['publication_block_reason'] or '反映には明示承認が必要です。'))

def prepare_task(cfg,p):
    from .workflow import update
    from pitchi_work.recovery import manifest
    import os
    if os.environ.get('PITCHI_ENABLE_LEGACY_GPU_WORKFLOW') != '1':
        raise ValueError('Legacy GPU workflow is optional and disabled; configure/review it explicitly before enabling')
    c=cfg['search_quality'];frozen=load(p/'inputs/frozen.json')
    try:
        if host_digest()!=c['reviewed_host_sha256']:raise ValueError('Existing Host source changed')
        check_inputs(cfg,p)
        if sha(Path(c['gpu_manager_root'])/'bonsai_work_v2/gpu_manager.py')!=c['gpu_manager_sha256']:raise ValueError('GPU manager source changed')
        sys.path.insert(0,c['gpu_manager_root'])
        from bonsai_work_v2.gpu_manager import GPUManager,GPUError,fetch
        manager=GPUManager();run=None
        with manager.lock():
            before=manager.snapshot();save(p/'gpu-before.json',before)
            if any(x['profile'] is None and not x['allowed_baseline'] for x in before['processes']):raise GPUError('Unknown GPU workload')
            protected={x['pid'] for x in before['processes'] if x['profile'] not in ('bonsai','qwen')};restore=manager.unit(manager.profiles['bonsai'])['active'];switched=False
            q=manager.profiles['qwen'];args=list(q['start_command']);args[args.index('-c')+1]='16384';args[args.index('--reasoning-budget')+1]='800';q['start_command']=args+['--cache-type-k','q8_0','--cache-type-v','q8_0']
            try:
                if not any(x['profile']=='qwen' for x in before['processes']):manager.stop('bonsai',force=False);switched=True;manager.start('qwen')
                model=fetch(q['endpoint']+'/models');assert model['data'][0]['id']==q['model'] and model['data'][0]['meta']['n_ctx']==16384
                save(p/'model.json',model);run=drive(c,p,q['endpoint'],q['model'])
            finally:
                if switched:manager.stop('qwen',force=False)
                if restore:manager.start('bonsai')
                after=manager.snapshot();save(p/'gpu-after.json',after);save(p/'safety.json',{'bonsai_restored':manager.unit(manager.profiles['bonsai'])['active'] if restore else None,'other_gpu_pids_preserved':protected.issubset({x['pid'] for x in after['processes']})})
            if restore and not load(p/'safety.json')['bonsai_restored']:raise ValueError('BONSAI restoration failed')
            if not load(p/'safety.json')['other_gpu_pids_preserved']:raise ValueError('GPU safety failed')
        if manifest(Path(c['source']))!=frozen['source_manifest'] or sha(c['database'])!=frozen['database_sha256']:raise ValueError('Fixed inputs changed')
        verify_and_plan(cfg,p,run)
    except Exception as e:update(p,status='FAILED',failure_class='SEARCH_HOST_TASK',message=str(e))

def public_verify(cfg,p):
    from .publication import public_observe,fetch
    c=cfg['search_quality'];hold=[x['query'] for x in load(c['holdout'])['queries']];rows=public_observe(cfg['production']['public_origin'],[{'query':c['query']}],hold);before=load(p/'public-before.json');forbidden=set(load(cfg['regressions'])['forbidden_ids'])
    checks=[{'name':'public_noise','pass':not forbidden.intersection(x['id'] for x in rows[c['query']]['guidelines'])}]
    for q in hold:checks.append({'name':'public_holdout:'+q,'pass':rows[q]['guidelines']==before[q]['guidelines'] and rows[q]['ranked_results']==before[q]['ranked_results']})
    plan=load(p/'plan.json');deployed=remote.call(cfg['production'],'inspect',{'app':cfg['production']['app'],'files':list(plan['files'])})
    checks.append({'name':'production_file_hashes','pass':all(deployed['files'][n]['sha256']==r['after_sha256'] for n,r in plan['files'].items())})
    definitions=[]
    for d in cfg['documents']:definitions.extend(q for q in load(d['expectations'])['queries'] if q.get('acceptance'))
    save(p/'public-definitions.json',definitions)
    r=subprocess.run(['/usr/bin/node',str(Path(__file__).with_name('public_probe.mjs')),cfg['production']['public_origin'],str(p/'public-definitions.json'),str(p/'public-browser.json')],capture_output=True,text=True,timeout=120,env={'PATH':'/usr/bin:/bin','HOME':os.environ.get('HOME','/tmp')})
    save(p/'public-browser-execution.json',{'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
    checks.append({'name':'public_browser_display_and_links','pass':bool(definitions) and r.returncode==0})
    result={'passed':all(x['pass'] for x in checks),'checks':checks,'queries':rows};save(p/'public-verification.json',result)
    if not result['passed']:raise ValueError('Public search acceptance failed')
    return result

def main():
    from .workflow import config_at,folder
    ap=argparse.ArgumentParser();ap.add_argument('--config',required=True);ap.add_argument('--worker',action='store_true');ap.add_argument('--run-id',required=True);args=ap.parse_args()
    prepare_task(config_at(args.config),folder(config_at(args.config),args.run_id))
if __name__=='__main__':main()
