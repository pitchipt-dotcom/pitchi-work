#!/usr/bin/env python3
"""Reuse of existing independent PTOTsearch checks as a normal CLI module."""
import hashlib, json, subprocess
from pathlib import Path

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def executor(spec,w,out,diagnostics):
    serial=0
    runtime=Path(spec['runtime']);abi=next(p.name for p in (runtime/'usr/lib/php').iterdir() if p.name.isdigit())
    prefix='/tmp/pitchi-php/usr/lib/php/'+abi
    php=['/usr/bin/env','LD_LIBRARY_PATH=/tmp/pitchi-php/usr/lib/x86_64-linux-gnu','/tmp/pitchi-php/usr/bin/php8.5','-n']
    for mod in ['pdo','pdo_sqlite','mbstring']:php+=['-d','extension='+prefix+'/'+mod+'.so']
    php+=['-d','zend.assertions=1','-d','assert.exception=1','-d','display_errors=stderr','-d','log_errors=0']
    def execute(args,extra=(),label=''):
        nonlocal serial
        serial+=1
        cmd=['bwrap','--ro-bind','/','/','--unshare-net','--unshare-pid','--die-with-parent','--tmpfs','/home','--tmpfs','/root',
             '--tmpfs','/tmp','--tmpfs','/run','--tmpfs','/mnt','--tmpfs','/media','--proc','/proc','--dev','/dev',
             '--ro-bind',str(w),'/tmp/project','--ro-bind',str(runtime),'/tmp/pitchi-php',
             '--ro-bind',spec['database'],'/tmp/evidence.sqlite','--chdir','/tmp/project']
        for src,dst in extra:cmd+=['--ro-bind',str(src),dst]
        cmd+=['--',*args]
        try:
            p=subprocess.run(cmd,capture_output=True,text=True,timeout=90,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
            receipt={'label':label,'argv':args,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr,'network':'disabled','workspace':'read-only','database':'read-only; API gets private tmpfs copy'}
        except subprocess.TimeoutExpired:receipt={'label':label,'exit_code':-1,'stdout':'','stderr':'Sandbox validation timeout'}
        save(out/f'execution-{serial:03d}.json',receipt)
        if receipt['exit_code']!=0:diagnostics.append({'label':label,'exit_code':receipt['exit_code'],'stderr':receipt['stderr'][-1600:],'stdout':receipt['stdout'][-400:]})
        return receipt
    return execute,php

def collect(spec,w,out):
    execute,php=executor(spec,w,out,[])
    gold=json.loads(Path(spec["gold"]).read_text())
    queries=list(dict.fromkeys([q["query"] for q in gold["queries"] if q["acceptance"]]+gold["holdout"]+spec.get("negative_queries",[])))
    qp=out/"queries.json";save(qp,queries)
    r=execute(php+["/tmp/collector.php"],[(spec["collector"],"/tmp/collector.php"),(qp,"/tmp/queries.json")],label="baseline_real_search")
    if r["exit_code"]!=0:raise ValueError("Baseline search failed: "+r["stderr"][-1600:])
    observation=json.loads(r["stdout"]);save(out/"observations.json",observation)
    return observation

def evaluate(spec,w,out,baseline):
    checks=[];details={};diagnostics=[]
    def check(name,value):checks.append({'name':name,'pass':bool(value)})
    execute,php=executor(spec,w,out,diagnostics)
    current={p.relative_to(w).as_posix():sha(p) for p in w.rglob('*') if p.is_file()}
    allowed={'src/GuidelineSectionSearch.php','public/assets/app.js'}|{'config/guideline_metadata/'+p.name for p in (w/'config/guideline_metadata').glob('*.json')}
    check('protected_scope',all(current.get(n)==d for n,d in baseline.items() if n not in allowed) and not(set(current)-set(baseline)-allowed))
    for n in spec['protected_tests']:
        r=execute(php+['/tmp/project/'+n],label=n);check('protected_test:'+n,r['exit_code']==0)
    r=execute(php+['-l','/tmp/project/src/GuidelineSectionSearch.php'],label='syntax');check('php_syntax',r['exit_code']==0)
    r=execute(['/usr/bin/node','--check','/tmp/project/public/assets/app.js'],label='js_syntax');check('js_syntax',r['exit_code']==0)
    gold=json.loads(Path(spec['gold']).read_text());accepted=[q for q in gold['queries'] if q['acceptance']]
    queries=list(dict.fromkeys([q['query'] for q in accepted]+gold['holdout']+spec.get('negative_queries',[])))
    qp=out/'queries.json';save(qp,queries)
    r=execute(php+['/tmp/collector.php'],[(spec['collector'],'/tmp/collector.php'),(qp,'/tmp/queries.json')],label='real_search_paths')
    check('real_search_paths',r['exit_code']==0)
    rows={};
    if r['exit_code']==0:
        observation=json.loads(r['stdout']);save(out/'observations.json',observation);rows={x['query']:x for x in observation['queries']}
    held=json.loads(Path(spec['oracle']).read_text());held={x['query']:x for x in held['queries']}
    for q in gold['holdout']:
        signature=lambda row:{k:[x.get('id') for x in v] for k,v in row['provider_top'].items()}
        check('fixed_holdout:'+q,q in rows and signature(rows[q])==signature(held[q]) and [(x.get('category'),x.get('id')) for x in rows[q]['final_ranking']]==[(x.get('category'),x.get('id')) for x in held[q]['final_ranking']])
    expected_metadata={m['id']:m for m in spec['expected_metadata']}
    catalog={}
    for path in (w/'config/guideline_metadata').glob('*.json'):
        try:m=json.loads(path.read_text());catalog[m['id']]=m
        except (ValueError,KeyError):check('catalog_parse:'+path.name,False)
    for identifier,m in expected_metadata.items():check('catalog_exact:'+identifier,catalog.get(identifier)==m)
    for definition in accepted:
        q=definition['query'];e=definition['expected'];identifier='metadata:'+e['id'];row=rows.get(q,{})
        candidates=row.get('provider_top',{}).get('guidelines',[])
        hit=next((x for x in candidates if x.get('id')==identifier),{})
        rank=next((i+1 for i,x in enumerate(candidates) if x.get('id')==identifier),999)
        fields={'guideline_title':e['title'],'issuing_body':e['issuer'],'edition':e['edition'],'publication_year':e['revision_year'],
                'source_url':e['official_url'],'collection_mode':'metadata_only'}
        matched=bool(hit) and rank<=min(1,definition['max_rank']) and all(hit.get(k)==v for k,v in fields.items())
        check('gold:'+definition['id'],matched and any(x.get('id')==identifier and x.get('category')=='guidelines' for x in row.get('final_ranking',[])))
        check('no_section_claim:'+definition['id'],bool(hit) and all(hit.get(k) is None for k in ['chapter','page_start','page_end','section_url','clinical_question','population','intervention','outcome']))
        check('old_noise_absent:'+definition['id'],not set(spec['forbidden_ids']).intersection(x['id'] for x in candidates))
        api=execute(php+['/tmp/api.php',q],[(spec['api_probe'],'/tmp/api.php')],label='actual_api:'+q)
        check('api_exec:'+definition['id'],api['exit_code']==0)
        data=json.loads(api['stdout']) if api['exit_code']==0 else {}
        api_hit=next((x for x in data.get('guidelines',[]) if x.get('id')==identifier),{})
        check('api_gold:'+definition['id'],bool(api_hit) and data.get('guidelines',[{}])[0].get('id')==identifier and all(api_hit.get(k)==v for k,v in fields.items()) and not api_hit.get('related_recommendations'))
        check('api_old_noise_absent:'+definition['id'],not set(spec['forbidden_ids']).intersection(x.get('id') for x in data.get('guidelines',[])))
        ap=out/('api-'+definition['id']+'.json');save(ap,data)
        js="""const fs=require('fs'),vm=require('vm');const src=fs.readFileSync('/tmp/project/public/assets/app.js','utf8');const fn=src.slice(src.indexOf('function guideline(g)'),src.indexOf('async function search('));const data=JSON.parse(fs.readFileSync('/tmp/api-result.json'));const row=data.guidelines.find(r=>r.id===process.argv[1]);if(!row)throw Error('no API row');const ctx={row,esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')};const rendered=vm.runInNewContext(fn+';guideline(row)',ctx,{timeout:2000});if(![row.source_url,row.guideline_title,row.issuing_body,row.edition,String(row.publication_year)].every(s=>rendered.includes(ctx.esc(s)))||!rendered.includes('メタデータのみ')||rendered.includes('原典の該当箇所を見る')||rendered.includes('該当箇所：'))throw Error('metadata card must label bibliography and link to official source without section claims');process.stdout.write(rendered);"""
        card=execute(['/usr/bin/node','-e',js,identifier],[(ap,'/tmp/api-result.json')],label='actual_card:'+q)
        check('card:'+definition['id'],card['exit_code']==0)
        details[q]={'rank':rank if hit else None,'api_rank':next((i+1 for i,x in enumerate(data.get('guidelines',[])) if x.get('id')==identifier),None),'found':bool(hit),'bibliography_matches':matched,'api_found':bool(api_hit)}
    for q in spec.get('negative_queries',[]):check('negative:'+q,not rows.get(q,{}).get('provider_top',{}).get('guidelines',[]))
    result={'passed':bool(checks) and all(c['pass'] for c in checks),'checks':checks,'diagnostics':diagnostics[:4],'gold':details,'authority':'actual PHP providers/API, protected tests and rendered JS; no model or Host','scope':'isolated metadata links only'}
    save(out/'result.json',result);return result
