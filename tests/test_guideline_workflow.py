import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from ptotsearch_tools import workflow as w,publication as pub,remote
from ptotsearch_tools.cli import load,save,sha,metadata,gold
ROOT=Path(__file__).resolve().parents[1]

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.policy=self.root/'policy.json';save(self.policy,{'authorized_plan_sha256':[]});self.policy.chmod(0o600)
        self.cfg={'home':str(self.root),'publication_policy':str(self.policy),'documents':[{'aliases':['KDIGO','CKD'],'metadata':str(ROOT/'tests/fixtures/metadata/kdigo.json'),'expectations':str(ROOT/'tests/fixtures/metadata/ckd-gold.json')}], 'production':{'app':'/fixture','backup_root':'/backups','public_origin':'https://example.org'},'regressions':str(ROOT/'tests/fixtures/metadata/regressions.json')}
    def tearDown(self):self.tmp.cleanup()
    def frozen(self):
        p=self.root/'tool_jobs'/('gl-'+'a'*32);(p/'inputs').mkdir(parents=True);(p/'verification').mkdir()
        m=metadata(self.cfg['documents'][0]['metadata']);mp=p/'inputs/meta.json';gp=p/'inputs/gold.json';save(mp,m);save(gp,gold(self.cfg['documents'][0]['expectations']))
        save(p/'inputs/documents.json',[{'id':m['id'],'metadata':str(mp),'expectations':str(gp)}]);save(p/'inputs/config.json',self.cfg)
        save(p/'bundle.json',{});save(p/'production-before.json',{});save(p/'public-before.json',{});save(p/'verification/result.json',{'passed':True})
        plan={'documents':[m],'asset_sha256':w.asset_digest(),'bundle_sha256':sha(p/'bundle.json'),'backup':{'sha256':sha(p/'production-before.json')},'public_before_sha256':sha(p/'public-before.json'),'config_sha256':sha(p/'inputs/config.json'),'isolated_verification':{'sha256':sha(p/'verification/result.json')},'expectations_sha256':{m['id']:sha(gp)}}
        save(p/'plan.json',plan);save(p/'state.json',{'run_id':p.name,'status':'NEEDS_HUMAN','plan_sha256':sha(p/'plan.json'),'challenge':'b'*32})
        approval={'sha256':sha(p/'plan.json'),'challenge':'b'*32,'confirmation':'本番反映を承認 '+sha(p/'plan.json'),'run_id':p.name}
        return p,approval
    def test_natural_language_does_not_approve(self):
        with patch.object(w,'start') as launch:
            result=w.dispatch(self.cfg,{'action':'submit','project':w.PROJECT,'request':'KDIGO CKD 2024を本番まで反映して'},'/private/config')
        self.assertEqual(result['status'],'RUNNING');self.assertIsNone(result['approval']);self.assertEqual(result['llm_calls'],0)
        self.assertFalse((w.folder(self.cfg,result['run_id'])/'approval.json').exists());launch.assert_called_once()
    def test_unknown_url_is_not_invented(self):
        with patch.object(w,'start') as launch:
            result=w.dispatch(self.cfg,{'action':'submit','project':w.PROJECT,'request':'この公式URL https://example.org/unknown を追加して'},'/private/config')
        self.assertEqual(result['status'],'NEEDS_HUMAN');launch.assert_not_called()
    def test_arbitrary_fields_and_invalid_run(self):
        with self.assertRaises(ValueError):w.dispatch(self.cfg,{'action':'submit','shell':'anything'},'/private/config')
        with self.assertRaises(ValueError):w.folder(self.cfg,'../escape')
    def test_permission_denial_never_calls_remote(self):
        p,a=self.frozen()
        with patch.object(remote,'call') as call, self.assertRaisesRegex(ValueError,'permission'):
            pub.admit(self.cfg,'/private/config',p,a)
        call.assert_not_called();self.assertEqual(load(p/'state.json')['status'],'NEEDS_HUMAN')
    def test_hash_tamper_requires_new_approval(self):
        p,a=self.frozen();save(p/'bundle.json',{'tampered':True})
        with self.assertRaisesRegex(ValueError,'changed'):pub.assert_frozen(self.cfg,p,a['sha256'])
    def test_gold_and_metadata_pinned(self):
        p,a=self.frozen();d=load(p/'inputs/documents.json')[0];save(Path(d['metadata']),{'id':'different'})
        with self.assertRaisesRegex(ValueError,'Metadata changed'):pub.assert_frozen(self.cfg,p,a['sha256'])
    def test_explicit_reservation_no_replay(self):
        p,a=self.frozen();save(self.policy,{'authorized_plan_sha256':[a['sha256']]})
        with patch.object(w,'start') as launch:
            pub.admit(self.cfg,'/private/config',p,a)
            with self.assertRaisesRegex(ValueError,'no replay'):pub.admit(self.cfg,'/private/config',p,a)
        launch.assert_called_once();self.assertEqual(load(p/'state.json')['status'],'PUBLICATION_QUEUED')
    def test_public_api_normalizes_case_but_rejects_wrong_query(self):
        data={'query':'ckd','guidelines':[],'ranked_results':[]}
        with patch.object(pub,'fetch',return_value=json.dumps(data).encode()):
            self.assertIn('CKD',pub.public_observe('https://example.org',[{'query':'CKD'}],[]))
        data['query']='different'
        with patch.object(pub,'fetch',return_value=json.dumps(data).encode()),self.assertRaisesRegex(ValueError,'contract'):
            pub.public_observe('https://example.org',[{'query':'CKD'}],[])
    def test_terminal_failure_immutable(self):
        p,a=self.frozen();w.update(p,status='FAILED',message='preserved failure')
        with self.assertRaisesRegex(ValueError,'immutable'):w.update(p,status='RUNNING')
        self.assertEqual(load(p/'state.json')['message'],'preserved failure')
    def test_remote_atomic_release_and_rollback(self):
        app=self.root/'app';app.mkdir();old=app/'public/assets/app.js';old.parent.mkdir(parents=True);old.write_text('old')
        db=self.root/'live.sqlite';db.write_bytes(b'unchanged-original-database');db_hash=sha(db)
        content=b'new';files={'public/assets/app.js':{'before_sha256':sha(old),'after_sha256':__import__('hashlib').sha256(content).hexdigest(),'content_b64':base64.b64encode(content).decode()},'config/guideline_metadata/fixture.json':{'before_sha256':None,'after_sha256':__import__('hashlib').sha256(b'{}').hexdigest(),'content_b64':base64.b64encode(b'{}').decode()}}
        payload={'app':str(app),'backup':str(self.root/'backup'),'files':files}
        def execute(program):
            result=subprocess.run(['python3','-c',program],input=json.dumps(payload),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
        self.assertEqual(len(execute(remote.RELEASE)['applied']),2);self.assertEqual(old.read_text(),'new')
        self.assertEqual(len(execute(remote.ROLLBACK)['restored']),2);self.assertEqual(old.read_text(),'old')
        self.assertFalse((app/'config/guideline_metadata/fixture.json').exists());self.assertEqual(sha(db),db_hash)
    def test_remote_drift_prevents_write(self):
        app=self.root/'app';app.mkdir();target=app/'public/assets/app.js';target.parent.mkdir(parents=True);target.write_text('current')
        p={'app':str(app),'backup':str(self.root/'backup'),'files':{'public/assets/app.js':{'before_sha256':'0'*64,'after_sha256':'0'*64,'content_b64':''}}}
        r=subprocess.run(['python3','-c',remote.RELEASE],input=json.dumps(p),capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0);self.assertEqual(target.read_text(),'current')

    def test_registered_metadata_checks_public_search_without_register(self):
        with patch.object(w,'start'):r=w.dispatch(self.cfg,{'action':'submit','project':w.PROJECT,'request':'KDIGO CKD 2024の登録状態を確認して'},'/config')
        p=w.folder(self.cfg,r['run_id']);m=metadata(self.cfg['documents'][0]['metadata']);name='config/guideline_metadata/'+m['id']+'.json'
        live={'files':{name:{'content_b64':base64.b64encode(json.dumps(m).encode()).decode(),'sha256':'fixture'}},'catalog':[{'id':m['id'],'official_url':m['official_url']}]}
        queries=gold(self.cfg['documents'][0]['expectations'])['queries'];rows={}
        for q in queries:
            e=q['expected'];rows[q['query']]={'guidelines':[{'id':'metadata:'+e['id'],'guideline_title':e['title'],'issuing_body':e['issuer'],'edition':e['edition'],'publication_year':e['revision_year'],'source_url':e['official_url'],'collection_mode':'metadata_only'}],'ranked_results':[]}
        with patch.object(remote,'call',return_value=live) as call,patch.object(pub,'public_observe',return_value=rows),patch.object(w,'cli_call') as cli:
            w.prepare(self.cfg,p)
        cli.assert_not_called();self.assertEqual([x.args[1] for x in call.call_args_list],['inspect'])
        result=w.view(self.cfg,p);self.assertEqual(result['status'],'COMPLETED');self.assertIsNone(result['approval']);self.assertEqual(result['result_details']['change_count'],0);self.assertEqual(result['llm_calls'],0)
    def test_catalog_conflict_stops_before_cli(self):
        with patch.object(w,'start'):r=w.dispatch(self.cfg,{'action':'submit','project':w.PROJECT,'request':'KDIGO CKDを登録して'},'/config')
        p=w.folder(self.cfg,r['run_id']);m=metadata(self.cfg['documents'][0]['metadata']);n='config/guideline_metadata/'+m['id']+'.json'
        live={'files':{n:{'content_b64':base64.b64encode(b'{}').decode()}},'catalog':[{'id':m['id'],'official_url':m['official_url']}]}
        with patch.object(remote,'call',return_value=live),patch.object(w,'cli_call') as cli:w.prepare(self.cfg,p)
        cli.assert_not_called();self.assertEqual(w.view(self.cfg,p)['status'],'NEEDS_HUMAN')
    def test_wrong_capability_stops_before_dispatch(self):
        with patch.object(w,'start') as start:
            for project,text in [('ptotsearch-quality','KDIGO CKDを登録して'),(w.PROJECT,'CKDの検索結果を改善して')]:
                with self.assertRaisesRegex(ValueError,'一致'):w.dispatch(self.cfg,{'action':'submit','project':project,'request':text},'/config')
        start.assert_not_called()

if __name__=='__main__':unittest.main()
