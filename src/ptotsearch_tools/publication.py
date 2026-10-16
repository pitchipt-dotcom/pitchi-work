"""Separate human-authorized bounded publication; never entered by natural-language submit."""
import argparse
import base64
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request
from .cli import load,save,sha,json_text
from . import remote

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):raise ValueError('Public verification redirected')
HTTP=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

def fetch(url):
    with HTTP.open(urllib.request.Request(url,headers={'DNT':'1','Cache-Control':'no-cache','User-Agent':'PTOTsearch-guideline-verifier/1'}),timeout=30) as r:
        body=r.read(2*1024*1024+1)
        if r.status!=200 or len(body)>2*1024*1024:raise ValueError('Public response status/size failed')
        return body

def public_observe(origin,definitions,holdout):
    u=urllib.parse.urlsplit(origin)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ('','/'):raise ValueError('Reviewed HTTPS site origin required')
    queries=list(dict.fromkeys([d['query'] for d in definitions]+holdout));rows={}
    for q in queries:
        data=json.loads(fetch(origin.rstrip('/')+'/api/search?'+urllib.parse.urlencode({'q':q,'sort':'relevance','limit':20})))
        if not isinstance(data.get('query'),str) or data['query'].strip().casefold()!=q.strip().casefold() or not isinstance(data.get('guidelines'),list) or not isinstance(data.get('ranked_results'),list):raise ValueError('Unexpected public API contract')
        rows[q]={'guidelines':data['guidelines'],'ranked_results':data['ranked_results']}
    return rows

def public_verify(cfg,p):
    if load(p/'plan.json').get('kind')=='search_quality':
        from .search_workflow import public_verify as verify_search
        return verify_search(cfg,p)
    from .workflow import event
    docs=load(p/'inputs/documents.json');definitions=[]
    for d in docs:definitions.extend(q for q in load(d['expectations'])['queries'] if q.get('acceptance'))
    regression=load(cfg['regressions']);rows=public_observe(cfg['production']['public_origin'],definitions,regression['holdout'])
    before=load(p/'public-before.json');checks=[]
    for d in definitions:
        e=d['expected'];candidates=rows[d['query']]['guidelines'];hit=candidates[0] if candidates else {}
        fields={'id':'metadata:'+e['id'],'guideline_title':e['title'],'issuing_body':e['issuer'],'edition':e['edition'],
                'publication_year':e['revision_year'],'source_url':e['official_url'],'collection_mode':'metadata_only'}
        checks.append({'name':'public_gold:'+d['id'],'pass':all(hit.get(k)==v for k,v in fields.items())})
        checks.append({'name':'public_noise:'+d['id'],'pass':not set(regression['forbidden_ids']).intersection(x['id'] for x in candidates)})
    for q in regression['holdout']:
        signature=lambda row:([x['id'] for x in row['guidelines']],[(x.get('category'),x['id']) for x in row['ranked_results']])
        checks.append({'name':'public_holdout:'+q,'pass':signature(rows[q])==signature(before[q])})
    plan=load(p/'plan.json')
    deployed=remote.call(cfg['production'],'inspect',{'app':cfg['production']['app'],'files':list(plan['files'])})
    checks.append({'name':'production_file_hashes','pass':all(deployed['files'][n]['sha256']==r['after_sha256'] for n,r in plan['files'].items())})
    script=fetch(cfg['production']['public_origin'].rstrip('/')+'/assets/app.js')
    expected=plan['files'].get('public/assets/app.js',{}).get('after_sha256')
    if expected:checks.append({'name':'public_display_asset','pass':hashlib.sha256(script).hexdigest()==expected})
    # Verify the actual public page via the same approved finite DOM probe used by Work.
    import subprocess,os
    definition_path=p/'public-definitions.json';save(definition_path,definitions)
    r=subprocess.run(['/usr/bin/node',str(Path(__file__).with_name('public_probe.mjs')),cfg['production']['public_origin'],str(definition_path),str(p/'public-browser.json')],capture_output=True,text=True,timeout=120,env={'PATH':'/usr/bin:/bin','HOME':os.environ.get('HOME','/tmp')})
    save(p/'public-browser-execution.json',{'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
    checks.append({'name':'public_browser_display_and_links','pass':r.returncode==0})
    result={'passed':all(x['pass'] for x in checks),'checks':checks,'queries':rows};save(p/'public-verification.json',result)
    event(p,'public_verification',passed=result['passed'])
    if not result['passed']:raise ValueError('Public acceptance failed; see preserved verification')
    return result

def assert_frozen(cfg,p,approved_sha):
    from .workflow import asset_digest
    s=load(p/'state.json');plan=load(p/'plan.json')
    if sha(p/'plan.json')!=approved_sha or s['plan_sha256']!=approved_sha:raise ValueError('Approval hash mismatch; reapproval required')
    for name,key in [('bundle.json','bundle_sha256'),('production-before.json','backup'),('public-before.json','public_before_sha256')]:
        expected=plan[key]['sha256'] if key=='backup' else plan[key]
        if sha(p/name)!=expected:raise ValueError('Approved evidence changed: '+name)
    if asset_digest()!=plan['asset_sha256'] or sha(p/'inputs/config.json')!=plan['config_sha256']:raise ValueError('Tool/config changed; reapproval required')
    if cfg!=load(p/'inputs/config.json'):raise ValueError('Bound execution config changed; reapproval required')
    if sha(p/'verification/result.json')!=plan['isolated_verification']['sha256'] or not load(p/'verification/result.json')['passed']:raise ValueError('Isolation evidence changed')
    for d in load(p/'inputs/documents.json'):
        if sha(d['expectations'])!=plan['expectations_sha256'][d['id']]:raise ValueError('Gold changed')
        frozen=next(x for x in plan['documents'] if x['id']==d['id'])
        if load(d['metadata'])!=frozen:raise ValueError('Metadata changed')
    if plan.get('kind')=='search_quality':
        from .search_workflow import host_digest,check_inputs
        check_inputs(cfg,p)
        if host_digest()!=cfg['search_quality']['reviewed_host_sha256'] or sha(p/'evidence.json')!=plan['evidence_sha256']:raise ValueError('Search Host/evidence changed')
        evidence=load(p/'evidence.json')
        for name,key in [('inputs/frozen.json','frozen_sha256'),('observations.json','observations_sha256'),('changes.diff','diff_sha256')]:
            if sha(p/name)!=evidence[key]:raise ValueError('Search evidence changed; reapproval required')
    return plan

def execute(cfg,p,approval):
    from .workflow import update,event
    with (p/'task.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        s=load(p/'state.json')
        if s['status'] not in ('NEEDS_HUMAN','PUBLICATION_QUEUED'):raise ValueError('Task is not awaiting approval; no replay')
        plan=assert_frozen(cfg,p,approval['sha256'])
        if not plan.get('publication_eligible',True):raise ValueError('Production replay forbidden')
        if approval.get('challenge')!=s['challenge'] or approval.get('confirmation')!='本番反映を承認 '+s['plan_sha256']:raise ValueError('Explicit human confirmation and exact challenge required')
        policy=load(cfg['publication_policy'])
        if Path(cfg['publication_policy']).stat().st_mode & 0o077 or s['plan_sha256'] not in policy.get('authorized_plan_sha256',[]):raise ValueError('Production executor permission is not granted for this exact plan')
        save(p/'approval.json',approval);event(p,'human_approval',sha256=s['plan_sha256'],actor=approval.get('actor','human'))
        update(p,status='RUNNING',stage='PRODUCTION_APPLY',message='承認された差分だけを本番へ反映します。')
        payload={'app':cfg['production']['app'],'backup':cfg['production']['backup_root']+'/'+p.name,'files':load(p/'bundle.json')}
        applied=False
        try:
            receipt=remote.call(cfg['production'],'release',payload);save(p/'production-apply.json',receipt);applied=True
            result=public_verify(cfg,p)
            update(p,status='COMPLETED',stage='PUBLIC_ACCEPTANCE',message='本番反映と公開検索・表示・公式リンクの確認に合格しました。',public_verification={'passed':True,'checks':len(result['checks'])})
        except Exception as e:
            # Never replay an uncertain write. Preserve evidence for reconciliation.
            if applied:
                try:
                    rollback=remote.call(cfg['production'],'rollback',payload);save(p/'rollback.json',rollback)
                    update(p,status='FAILED',stage='ROLLED_BACK',failure_class='PUBLIC_ACCEPTANCE_FAILED',message=str(e)+'; changed files rolled back')
                except Exception as restore:
                    update(p,status='RECOVERY_REQUIRED',stage='ROLLBACK',message=str(restore),failure_class='PUBLICATION_RECOVERY')
            else:update(p,status='RECOVERY_REQUIRED',stage='RECONCILE',message=str(e),failure_class='UNCERTAIN_OR_REJECTED_APPLY')


def admit(cfg,config_path,p,approval):
    from .workflow import update,start
    with (p/'task.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        s=load(p/'state.json')
        if s['status']!='NEEDS_HUMAN':raise ValueError('Task not awaiting approval; no replay')
        plan=assert_frozen(cfg,p,approval['sha256'])
        if not plan.get('publication_eligible',True):raise ValueError('Production replay forbidden: live source differs from frozen fixture')
        if approval['challenge']!=s['challenge'] or approval['confirmation']!='本番反映を承認 '+s['plan_sha256']:raise ValueError('Exact human confirmation required')
        policy_path=Path(cfg['publication_policy'])
        if policy_path.stat().st_mode & 0o077 or s['plan_sha256'] not in load(policy_path).get('authorized_plan_sha256',[]):raise ValueError('Production permission is not granted for this plan')
        save(p/'approval-request.json',approval)
        update(p,status='PUBLICATION_QUEUED',stage='HUMAN_APPROVED',message='明示承認を受け付けました。公開検証まで実行します。')
        try:start(cfg,config_path,p,worker='publish')
        except Exception as e:update(p,status='RECOVERY_REQUIRED',stage='DISPATCH',message=str(e))


def main():
    from .workflow import config_at,folder,view,update
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--worker',action='store_true');parser.add_argument('--run-id');args=parser.parse_args()
    try:
        cfg=config_at(args.config)
        if args.worker:
            p=folder(cfg,args.run_id)
            try:execute(cfg,p,load(p/'approval-request.json'))
            except Exception as e:update(p,status='RECOVERY_REQUIRED',stage='PRE_WRITE_RECHECK',failure_class='PUBLICATION_REVIEW',message=str(e))
            return 0
        raw=sys.stdin.buffer.read(32769)
        if len(raw)>32768:raise ValueError('Approval request too large')
        incoming=json.loads(raw);p=folder(cfg,incoming['run_id'])
        if set(incoming)!={'run_id','sha256','challenge','confirmation'}:raise ValueError('Unknown approval fields')
        admit(cfg,args.config,p,{**incoming,'actor':'explicit_chat_confirmation'})
        print(json_text({'ok':True,'result':view(cfg,p)}),end='');return 0
    except Exception as e:print(json_text({'ok':False,'error':str(e)}),end='');return 2
if __name__=='__main__':sys.exit(main())
