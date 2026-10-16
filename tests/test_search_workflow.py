import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from ptotsearch_tools import workflow as w,search_workflow as sq,publication as pub
from ptotsearch_tools.cli import save,load,sha
from test_guideline_workflow import WorkflowTests
ROOT=Path(__file__).resolve().parents[1]

class SearchConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)
        source=self.p/'source';source.mkdir();(source/'fixture').write_text('original')
        for name in ('mission.txt','db','holdout','regressions'): (self.p/name).write_text('{}')
        save(self.p/'steps.json',{'steps':[{}]*6,'classification':'CODEX_DESIGNED_STEPS','max_local_calls':24})
        save(self.p/'policy',{'authorized_plan_sha256':[]})
        self.cfg={'home':str(self.p),'documents':[],'publication_policy':str(self.p/'policy'),'regressions':str(self.p/'regressions'),'search_quality':{'query':'大動脈解離 リハビリテーション','reviewed_host_sha256':sq.host_digest(),'source':str(source),'database':str(self.p/'db'),'steps':str(self.p/'steps.json'),'mission':str(self.p/'mission.txt'),'holdout':str(self.p/'holdout'),'php_runtime':'/reviewed/php'}}
    def tearDown(self):self.tmp.cleanup()
    def test_formal_dispatch_and_status(self):
        with patch.object(w,'start') as start:
            r=w.dispatch(self.cfg,{'action':'submit','project':sq.PROJECT,'request':'大動脈解離 リハビリテーションの検索結果を改善して'},'/config')
        p=w.folder(self.cfg,r['run_id']);sq.check_inputs(self.cfg,p)
        start.assert_called_once_with(self.cfg,'/config',p,worker='search')
        argv=sq.command(self.cfg['search_quality'],p,'http://127.0.0.1:1/v1','qwen')
        self.assertIn('pitchi_work.cli',argv);self.assertIn('--edit-steps',argv);self.assertIn('--search-database',argv)
        self.assertEqual(w.dispatch(self.cfg,{'action':'status','run_id':r['run_id']},'/config')['run_id'],r['run_id'])
        self.assertEqual(sq.command(self.cfg['search_quality'],p,'x','y','parent')[-2:],['recover','parent'])
    def test_unknown_problem_never_starts_model(self):
        with patch.object(w,'start') as start:
            r=w.dispatch(self.cfg,{'action':'submit','project':sq.PROJECT,'request':'不明な検索問題を直して'},'/config')
        self.assertEqual(r['status'],'NEEDS_HUMAN');self.assertEqual(r['llm_calls'],0);start.assert_not_called()
    def test_pin_and_frozen_input_denial(self):
        c=self.cfg['search_quality'];c['reviewed_host_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'review'):sq.submit(self.cfg,{'request':c['query']},'/config')
        c['reviewed_host_sha256']=sq.host_digest()
        with patch.object(w,'start'):r=sq.submit(self.cfg,{'request':c['query']},'/config')
        p=w.folder(self.cfg,r['run_id']);(p/'inputs/steps.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'Copied'):sq.check_inputs(self.cfg,p)
    def test_current_production_replay_denied_even_with_policy(self):
        # Reuse the already-tested approval envelope and authority machinery.
        helper=WorkflowTests();helper.setUp()
        try:
            p,a=helper.frozen();plan=load(p/'plan.json');plan['publication_eligible']=False;save(p/'plan.json',plan)
            s=load(p/'state.json');s['plan_sha256']=sha(p/'plan.json');save(p/'state.json',s)
            a.update(sha256=s['plan_sha256'],confirmation='本番反映を承認 '+s['plan_sha256']);save(helper.policy,{'authorized_plan_sha256':[s['plan_sha256']]})
            with patch.object(w,'start') as start,self.assertRaisesRegex(ValueError,'replay'):pub.admit(helper.cfg,'/config',p,a)
            start.assert_not_called();self.assertFalse(w.view(helper.cfg,p)['approval']['execution_enabled'])
        finally:helper.tearDown()
    def test_public_failure_rolls_back_using_existing_executor(self):
        helper=WorkflowTests();helper.setUp()
        try:
            p,a=helper.frozen();save(helper.policy,{'authorized_plan_sha256':[a['sha256']]})
            with patch.object(pub.remote,'call',return_value={}) as call,patch.object(pub,'public_verify',side_effect=ValueError('fixed acceptance failure')):
                pub.execute(helper.cfg,p,a)
            self.assertEqual([c.args[1] for c in call.call_args_list],['release','rollback'])
            self.assertEqual(load(p/'state.json')['stage'],'ROLLED_BACK')
        finally:helper.tearDown()
    def test_search_public_verification_measures_hash_api_and_browser(self):
        cfg={**self.cfg,'documents':[{'expectations':str(ROOT/'tests/fixtures/metadata/ckd-gold.json')}],'production':{'app':'/fixture','public_origin':'https://example.org'}}
        save(self.p/'holdout',{'queries':[{'query':'fixed holdout'}]})
        save(self.p/'regressions',{'forbidden_ids':['noise']})
        rows={cfg['search_quality']['query']:{'guidelines':[],'ranked_results':[]},'fixed holdout':{'guidelines':[],'ranked_results':[]}}
        save(self.p/'public-before.json',rows);save(self.p/'plan.json',{'files':{'src/GuidelineSectionSearch.php':{'after_sha256':'expected'}}})
        from types import SimpleNamespace
        with patch.object(pub,'public_observe',return_value=rows),patch.object(sq.remote,'call',return_value={'files':{'src/GuidelineSectionSearch.php':{'sha256':'expected'}}}),patch.object(sq.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as browser:
            result=sq.public_verify(cfg,self.p)
            self.assertTrue(result['passed']);self.assertIn('public_probe.mjs',browser.call_args.args[0][1])
        with patch.object(pub,'public_observe',return_value=rows),patch.object(sq.remote,'call',return_value={'files':{'src/GuidelineSectionSearch.php':{'sha256':'drift'}}}),patch.object(sq.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')):
            with self.assertRaisesRegex(ValueError,'acceptance'):sq.public_verify(cfg,self.p)

    def test_successful_run_reference_never_starts_model(self):
        # Independent verified fixture, not replayed model output.
        with patch.object(w,'start'):r=sq.submit(self.cfg,{'request':self.cfg['search_quality']['query']},'/config')
        p=w.folder(self.cfg,r['run_id']);(p/'verification').mkdir();save(p/'verification/result.json',{'passed':True})
        host=p/'host-runtime/runs/fixture';host.mkdir(parents=True);save(host/'state.json',{'status':'COMPLETED'})
        save(p/'plan.json',{'host_run_id':'fixture','isolated_verification':{'passed':True,'sha256':sha(p/'verification/result.json')},'search_summary':{'before_after':{'query':self.cfg['search_quality']['query']}}})
        state=load(p/'state.json');state.update(status='NEEDS_HUMAN',plan_sha256=sha(p/'plan.json'),challenge='x');save(p/'state.json',state)
        self.cfg['search_quality']['successful_run_id']=r['run_id']
        with patch.object(w,'start') as start:result=sq.submit(self.cfg,{'request':self.cfg['search_quality']['query']+'の検索結果を改善して'},'/config')
        self.assertTrue(result['reused_result']);self.assertEqual(result['run_id'],r['run_id']);start.assert_not_called();self.assertEqual(result['new_llm_calls'],0)
if __name__=='__main__':unittest.main()


class LegacyGpuOptInTests(unittest.TestCase):
    def test_legacy_gpu_workflow_is_disabled_before_any_manager_use(self):
        import os
        from ptotsearch_tools import search_workflow
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'optional and disabled'):
                search_workflow.prepare_task({},Path('/tmp/synthetic-disabled-workflow'))
