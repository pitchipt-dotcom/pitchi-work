"""Thin official Work task entry around the verified registration CLI; no model/editor Host."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from .cli import load,save,sha,metadata,gold,json_text
from . import remote

PROJECT='ptotsearch-guidelines'
ID=re.compile(r'gl-[a-f0-9]{32}')

def asset_digest():
    root=Path(__file__).parent
    paths=sorted([p for p in root.iterdir() if p.is_file() and p.suffix in ('.py','.php','.patch','.mjs')])
    collector=root.parent/'pitchi_work/search_probe.php'
    h=hashlib.sha256()
    for p in [*paths,collector]:h.update(p.name.encode());h.update(p.read_bytes())
    return h.hexdigest()

def config_at(path):
    path=Path(path)
    if path.stat().st_mode & 0o077:raise ValueError('Private tool config requires owner-only permissions')
    cfg=load(path)
    if not cfg.get('enabled') or cfg['reviewed_asset_sha256']!=asset_digest():raise ValueError('Guideline tool disabled or source review required')
    return cfg

def folder(cfg,identifier):
    if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise ValueError('Invalid guideline task ID')
    p=Path(cfg['home'])/'tool_jobs'/identifier
    if p.is_symlink() or not (p/'state.json').exists():raise ValueError('Unknown guideline task')
    return p

def event(p,kind,**fields):
    with (p/'events.jsonl').open('a') as f:f.write(json.dumps({'kind':kind,**fields},ensure_ascii=False)+'\n')

def update(p,**changes):
    state=load(p/'state.json')
    if state['status'] in ('FAILED','CANCELLED','COMPLETED') and any(state.get(k)!=v for k,v in changes.items()):raise ValueError('Terminal tool result is immutable')
    state.update(changes);save(p/'state.json',state);event(p,'state',status=state['status'],stage=state.get('stage'))
    return state

def view(cfg,p):
    s=load(p/'state.json');plan=load(p/'plan.json') if (p/'plan.json').exists() else None
    if plan and sha(p/'plan.json')!=s['plan_sha256']:raise ValueError('Approval plan changed; new review required')
    return {**{k:s.get(k) for k in ['run_id','status','stage','message','original_request','failure_class','verification','llm_calls','selected_tool','host_run_id','model','classification','result_details','public_verification']},
            'saved_plan':plan if s['status']=='COMPLETED' else None,
            'native_status':s['status'],'status':'RUNNING' if s['status']=='PUBLICATION_QUEUED' else ('NEEDS_HUMAN' if s['status']=='RECOVERY_REQUIRED' else s['status']),'artifact':None,'tool_task':True,
            'approval':{'plan':plan,'sha256':s['plan_sha256'],'challenge':s['challenge'],
                        'execution_enabled':plan.get('publication_eligible',True) and s['plan_sha256'] in load(cfg['publication_policy']).get('authorized_plan_sha256',[])} if s['status']=='NEEDS_HUMAN' and plan else None,
            'human_confirmation':{'approval_required':True,'question':s.get('question')} if s['status']=='NEEDS_HUMAN' else None}

def select(cfg,text):
    # Finite matching of operator-reviewed metadata, never invented bibliography or shell.
    return [d for d in cfg['documents'] if any(alias.casefold() in text.casefold() for alias in d['aliases'])]

def start(cfg,config_path,p,worker='prepare'):
    log=p/'worker.log'
    env={k:os.environ[k] for k in ('PATH','HOME','LANG','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    env.update(PYTHONPATH=str(Path(__file__).parents[1]),PYTHONDONTWRITEBYTECODE='1')
    module={'publish':'ptotsearch_tools.publication','search':'ptotsearch_tools.search_workflow','prepare':'ptotsearch_tools.workflow'}[worker]
    cmd=[sys.executable,'-B','-m',module,'--config',str(config_path),'--worker',*(['prepare'] if worker=='prepare' else []),'--run-id',p.name]
    if cfg.get('supervisor')=='systemd':
        result=subprocess.run(['systemd-run','--user','--quiet','--collect','--unit=pitchi-guidelines-'+p.name,
            '--property=Type=exec','--property=StandardOutput=append:'+str(log),'--property=StandardError=append:'+str(log),
            '/usr/bin/env','-i',*[k+'='+v for k,v in env.items()],*cmd],capture_output=True,timeout=10)
        if result.returncode:raise ValueError('Independent tool worker could not start')
    else:
        if '.service' in Path('/proc/self/cgroup').read_text():raise ValueError('Service requires independent systemd supervisor')
        with log.open('ab') as f:subprocess.Popen(cmd,env=env,stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True)

def dispatch(cfg,request,config_path):
    allowed={'catalog':{'action'},'submit':{'action','project','request','operation'},'result':{'action','run_id'},
             'status':{'action','run_id'},'cancel':{'action','run_id'}}
    action=request.get('action')
    if action not in allowed or set(request)-allowed[action]:raise ValueError('Unsupported tool operation/fields')
    if action=='catalog':return {'projects':[PROJECT]+(['ptotsearch-quality'] if cfg.get('search_quality') else []),'operations':['create'],'tool':'ptot-guidelines','llm_required':False}
    if action!='submit':
        p=folder(cfg,request.get('run_id'))
        if action=='cancel' and load(p/'state.json')['status']=='RUNNING':
            if load(p/'state.json').get('stage')=='PRODUCTION_APPLY':raise ValueError('Publication is running; do not terminate a write')
            save(p/'cancel.json',{'requested':True})
            return view(cfg,p)
        if action=='cancel':
            with (p/'task.lock').open('a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                if load(p/'state.json')['status']=='RUNNING':save(p/'cancel.json',{'requested':True})
                elif load(p/'state.json')['status']=='NEEDS_HUMAN':update(p,status='CANCELLED',message='本番反映せず中止しました。')
        return view(cfg,p)
    text=request.get('request')
    if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError('Bounded nonempty request required')
    registration=any(x in text for x in ('登録','追加','収録'))
    improvement='検索' in text and any(x in text for x in ('改善','無関係','調べて','アルゴリズム'))
    if registration and improvement:raise ValueError('登録と検索アルゴリズム改善は別の仕事です。対象能力を確認してください。')
    if (request.get('project')=='ptotsearch-quality' and registration) or (request.get('project')==PROJECT and improvement):
        raise ValueError('依頼と選択能力が一致しません。実行前に停止しました。')
    if request.get('project')=='ptotsearch-quality':
        from .search_workflow import submit
        return submit(cfg,request,config_path)
    text=request.get('request')
    if request.get('project')!=PROJECT or request.get('operation','create')!='create':raise ValueError('Guideline tasks allow create only; approval is a separate endpoint')
    if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError('Bounded nonempty request required')
    p=Path(cfg['home'])/'tool_jobs'/('gl-'+uuid.uuid4().hex);p.mkdir(parents=True,mode=0o700)
    chosen=select(cfg,text)
    save(p/'state.json',{'run_id':p.name,'status':'RUNNING' if chosen else 'NEEDS_HUMAN','stage':'CLI_PREPARATION',
        'original_request':text,'message':'公式書誌とCLIで隔離検証します。' if chosen else '確認済み書誌がありません。公式資料の名称・版・発行元・検索語の確認が必要です。',
        'question':None if chosen else '資料の正式名称と版、発行元を確認してください。',
        'selected_tool':'ptot-guidelines','llm_calls':0,'operations':0,'budget':12,'asset_sha256':asset_digest()})
    event(p,'request_received',request=text,selected_tool='ptot-guidelines',llm_calls=0)
    if chosen:
        (p/'inputs').mkdir()
        docs=[]
        for d in chosen:
            m=metadata(d['metadata']);g=gold(d['expectations'])
            mp=p/'inputs'/(m['id']+'.json');gp=p/'inputs'/(m['id']+'-gold.json');save(mp,m);save(gp,g)
            docs.append({'id':m['id'],'metadata':str(mp),'expectations':str(gp)})
        save(p/'inputs/documents.json',docs)
        save(p/'inputs/config.json',cfg)
        try:start(cfg,config_path,p)
        except Exception as e:update(p,status='FAILED',failure_class='TOOL_DISPATCH',message=str(e))
    return view(cfg,p)

def cli_call(p,*arguments):
    if (p/'cancel.json').exists():raise ValueError('Cancellation requested')
    s=load(p/'state.json')
    if s['operations']>=s['budget']:raise ValueError('CLI operation budget exhausted')
    number=s['operations']+1;update(p,operations=number,stage='CLI:'+arguments[0])
    cmd=[sys.executable,'-B','-m','ptotsearch_tools.cli',*map(str,arguments)]
    env={'PATH':'/usr/bin:/bin','HOME':os.environ.get('HOME','/tmp'),'LANG':'C.UTF-8','PYTHONPATH':str(Path(__file__).parents[1]),'PYTHONDONTWRITEBYTECODE':'1'}
    r=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=180)
    save(p/('cli-%02d.json'%number),{'argv':list(map(str,arguments)),'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
    if r.returncode:raise ValueError('CLI '+arguments[0]+' failed; see preserved receipt '+str(number))
    return json.loads(r.stdout)

def prepare(cfg,p):
    with (p/'task.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if load(p/'state.json')['status']!='RUNNING':return
        try:
            docs=load(p/'inputs/documents.json');workspace=p/'workspace'
            # Read-only admission first: do not repeat registration for live ID/URL.
            names=['config/guideline_metadata/'+d['id']+'.json' for d in docs]
            live=remote.call(cfg['production'],'inspect',{'app':cfg['production']['app'],'files':names})
            save(p/'catalog-before.json',live)
            duplicates=[d for d in docs if any(x['id']==d['id'] or x['official_url']==metadata(d['metadata'])['official_url'] for x in live['catalog'])]
            if duplicates:
                exact=len(duplicates)==len(docs) and all(live['files'][n]['content_b64'] and json.loads(base64.b64decode(live['files'][n]['content_b64']))==metadata(d['metadata']) for d,n in zip(docs,names))
                if not exact:
                    update(p,status='NEEDS_HUMAN',stage='CATALOG_CONFLICT',question='登録済み資料との差異を確認してください。',message='IDまたは公式URLが登録済みです。混在・内容相違を検出したため、登録前に停止しました。',result_details={'documents':[metadata(d['metadata']) for d in docs],'change_count':0,'duplicate_check':'ID/URL conflict; no register','verification':None})
                    return
                from .publication import public_observe
                definitions=[q for d in docs for q in gold(d['expectations'])['queries'] if q.get('acceptance')]
                observed=public_observe(cfg['production']['public_origin'],definitions,[]);checks=[]
                for q in definitions:
                    e=q['expected'];hits=observed[q['query']]['guidelines'];hit=hits[0] if hits else {}
                    fields={'id':'metadata:'+e['id'],'guideline_title':e['title'],'issuing_body':e['issuer'],'edition':e['edition'],'publication_year':e['revision_year'],'source_url':e['official_url'],'collection_mode':'metadata_only'}
                    checks.append({'name':'registered_public_gold:'+q['id'],'pass':all(hit.get(k)==v for k,v in fields.items())})
                    checks.append({'name':'registered_noise:'+q['id'],'pass':not set(load(cfg['regressions'])['forbidden_ids']).intersection(x['id'] for x in hits)})
                result={'passed':all(x['pass'] for x in checks),'checks':checks,'queries':observed,'live_metadata_exact':True,'production_writes':0}
                (p/'verification').mkdir();save(p/'verification/result.json',result)
                if not result['passed']:raise ValueError('Registered document public search verification failed')
                update(p,status='COMPLETED',stage='ALREADY_REGISTERED',message='公式資料は本番に登録済みです。書誌情報・公式URL・公開検索を確認しました。差分0件、再登録・本番書込みは行っていません。',verification={'passed':True,'checks':len(checks),'sha256':sha(p/'verification/result.json')},result_details={'documents':[metadata(d['metadata']) for d in docs],'change_count':0,'diff':'変更なし（登録済み書誌と一致）','duplicate_check':'same ID/URL/metadata; register not executed','verification':result})
                return
            argv=['init','--source',cfg['source'],'--workspace',workspace,'--database',cfg['database'],'--php-runtime',cfg['php_runtime'],'--regressions',cfg['regressions']]
            for d in docs:argv+=['--expectations',d['expectations']]
            cli_call(p,*argv)
            for d in docs:
                for operation in ('validate','preview','register'):cli_call(p,operation,'--workspace',workspace,'--metadata',d['metadata'])
            verified=cli_call(p,'verify','--workspace',workspace,'--output',p/'verification')
            if not verified.get('passed'):raise ValueError('Independent CLI verification failed')
            project=workspace/'project';session=load(workspace/'session.json')
            changed=[n for n,h in session['current_manifest'].items() if session['baseline_manifest'].get(n)!=h]
            allowed={'src/GuidelineSectionSearch.php','public/assets/app.js',*['config/guideline_metadata/'+d['id']+'.json' for d in docs]}
            if set(changed)-allowed:raise ValueError('Publication scope mismatch')
            probe=remote.call(cfg['production'],'inspect',{'app':cfg['production']['app'],'files':changed})
            for d in docs:
                m=metadata(d['metadata'])
                if any(x['id']==m['id'] or x['official_url']==m['official_url'] for x in probe['catalog']):raise ValueError('Production catalog duplicate; review instead of overwrite')
            files={}
            for name in changed:
                before=probe['files'][name]
                if before['sha256']!=session['baseline_manifest'].get(name):raise ValueError('Production drift from isolated source: '+name)
                files[name]={'before_sha256':before['sha256'],'after_sha256':sha(project/name),'content_b64':base64.b64encode((project/name).read_bytes()).decode()}
            save(p/'production-before.json',probe);save(p/'bundle.json',files)
            from .publication import public_observe
            definitions=[q for d in docs for q in gold(d['expectations'])['queries'] if q.get('acceptance')]
            observed=public_observe(cfg['production']['public_origin'],definitions,load(cfg['regressions'])['holdout'])
            save(p/'public-before.json',observed)
            plan={'documents':[metadata(d['metadata']) for d in docs],
                'files':{n:{'before_sha256':r['before_sha256'],'after_sha256':r['after_sha256']} for n,r in files.items()},
                'change_count':len(files),'duplicate_check':'no catalog duplicate ID/URL',
                'database_changes':0,'index_changes':'existing JSON catalog retrieval; no DB/FTS migration',
                'isolated_verification':{'passed':True,'checks':len(verified['checks']),'sha256':sha(p/'verification/result.json')},
                'backup':{'status':'exact pre-change files saved locally and hash checked; remote private backup before first publish write','sha256':sha(p/'production-before.json'),'local_reference':'production-before.json','remote_reference':cfg['production']['backup_root']+'/'+p.name+'/before.json'},
                'rollback':'Restore only changed files from verified private backup; remove newly created JSON; never restore whole live DB',
                'bundle_sha256':sha(p/'bundle.json'),'config_sha256':sha(p/'inputs/config.json'),
                'public_before_sha256':sha(p/'public-before.json'),'asset_sha256':asset_digest(),
                'expectations_sha256':{d['id']:sha(d['expectations']) for d in docs},'source_role':'Codex-created deterministic CLI; no Qwen edits'}
            if (p/'cancel.json').exists():raise ValueError('Cancellation requested')
            save(p/'plan.json',plan)
            update(p,status='NEEDS_HUMAN',stage='PRODUCTION_APPROVAL',message='隔離登録・検索検証に合格しました。本番変更内容を確認し、明示承認してください。本番は未変更です。',
                   plan_sha256=sha(p/'plan.json'),challenge=uuid.uuid4().hex,verification=plan['isolated_verification'])
        except Exception as e:update(p,status='CANCELLED' if (p/'cancel.json').exists() else 'FAILED',failure_class='CLI_TOOL_TASK',message=str(e))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--worker',choices=['prepare']);parser.add_argument('--run-id')
    args=parser.parse_args()
    try:
        cfg=config_at(args.config)
        if args.worker:prepare(cfg,folder(cfg,args.run_id));return 0
        data=sys.stdin.buffer.read(32769)
        if len(data)>32768:raise ValueError('Input budget exceeded')
        result=dispatch(cfg,json.loads(data),args.config);print(json_text({'ok':True,'result':result}),end='');return 0
    except Exception as e:print(json_text({'ok':False,'error':str(e)}),end='');return 2
if __name__=='__main__':sys.exit(main())
