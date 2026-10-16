"""Boundary tests only; private real screenshots provide Phase 2.5 acceptance."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import os

from pitchi_work.screenshot_semantic import endpoint_url, semantic_search, validate_assessment, select_candidates, ground_assessment
from pitchi_work.screenshots import SearchFailure
from pitchi_work.tools import catalog, resolve_tool_request
from pitchi_work import cli


class SemanticBoundaries(unittest.TestCase):
    def test_generic_request_routes_and_existing_tools_remain(self):
        for request, query in [
            ('n8nのワークフローを実際に編集・操作しているスクリーンショットを5枚探して','n8n'),
            ('ChatGPTで文章について修正指示を出している画面を探して','ChatGPT'),
            ('Codexが実際にコードやプロジェクトを変更している画面を探して','Codex'),
            ('WordPressの記事を編集している画面を探して','WordPress')]:
            routed = resolve_tool_request(request, '/path/to/images')
            self.assertEqual(routed['name'], 'screenshot.assisted_search')
            self.assertEqual(routed['input']['query'], query)
            self.assertEqual(routed['input']['limit'], 5)
        self.assertEqual(resolve_tool_request('ChatGPTのスクリーンショットを探して','/x')['name'],'screenshot.search')
        self.assertEqual(resolve_tool_request('このフォルダを検索可能にして','/x')['name'],'screenshot.index')
        self.assertEqual(resolve_tool_request('この画像を読んで: /x.png')['name'],'image.ocr')
        self.assertIn('screenshot.semantic_search',catalog())

    def test_privacy_limits_cancel_and_no_model_when_no_candidates(self):
        for endpoint in ('https://example.com/v1','http://example.com/v1','http://user:secret@localhost/v1',
                         'http://127.0.0.1/v1?token=secret'):
            with self.assertRaises(SearchFailure): endpoint_url(endpoint)
        self.assertEqual(endpoint_url('http://127.0.0.1:8081/v1/'),'http://127.0.0.1:8081/v1')
        with TemporaryDirectory() as temporary:
            args={'root':temporary,'query':'app','intent':'visible activity'}
            for extras in ({'candidate_limit':31},{'limit':0},{'intent':''},{'timeout_seconds':999}):
                self.assertEqual(semantic_search({**args,**extras},home=temporary)['error']['code'],'INVALID_INPUT')
            with patch('pitchi_work.tools.execute_tool',return_value={'ok':True,'total':0,'matches':[]}):
                with patch('pitchi_work.screenshot_semantic.opener') as network:
                    result=semantic_search(args,home=temporary)
                    self.assertTrue(result['ok']);self.assertEqual(result['shortfall'],5)
                    self.assertEqual(result['vision_calls'],0);network.assert_not_called()
                    cancelled=semantic_search(args,home=temporary,cancelled=lambda:True)
                    self.assertEqual(cancelled['error']['code'],'CANCELLED')

    def test_model_cannot_invent_ids_or_self_certify(self):
        valid={'decision':'match','screen_type':'editor','observations':['canvas','controls'],'evidence_line_ids':[1,2],'reason':'visible'}
        self.assertEqual(validate_assessment(valid),valid)
        for invalid in ({**valid,'decision':'PASS'},{**valid,'id':123},{**valid,'observations':['word mention']},
                        {**valid,'decision':'unknown'},{}):
            with self.assertRaises(SearchFailure): validate_assessment(invalid)
        items=[{'id':i,'app':'Chrome','score':15,'datetime':''} for i in range(100)]
        rows={i:{'text':'an article'} for i in range(100)}
        rows[1]['text']='Editor Canvas Save'
        selected=select_candidates(items,rows,30)
        self.assertEqual(len(selected),30);self.assertEqual(selected[0]['id'],1)
        self.assertEqual(ground_assessment(valid,{1:'Editor',2:'S a v e'}),valid)
        with self.assertRaises(SearchFailure):ground_assessment({**valid,'evidence_line_ids':[1,3]},{1:'Editor',2:'Save'})
        with self.assertRaises(SearchFailure):validate_assessment({**valid,'evidence_line_ids':[]})
        rows[2]['text']='文章 文面 校正'
        self.assertEqual(select_candidates(items,rows,30,'文章について修正指示')[0]['id'],2)

    def test_cli_default_semantic_limit_and_no_mission_authority(self):
        with TemporaryDirectory() as temporary:
            with patch.object(cli,'foreground_tool',return_value=0) as tool, \
                    patch.object(cli,'health') as health, patch.object(cli,'create_mission') as mission:
                code=cli.main(['--home',temporary,'tool','screenshot.semantic_search',
                              '--root',temporary,'--query','app','--intent','visible activity'])
                self.assertEqual(code,0);self.assertEqual(tool.call_args.args[2]['limit'],5)
                self.assertEqual(tool.call_args.args[2]['candidate_limit'],30)
                code=cli.main(['--home',temporary,'--workspace',temporary,'--request',
                              'AnyAppの記事を編集している画面を探して'])
                self.assertEqual(code,0);self.assertEqual(tool.call_args.args[1],'screenshot.assisted_search')
                health.assert_not_called();mission.assert_not_called()

    def test_assisted_empty_and_cancel_never_call_model(self):
        from pitchi_work.screenshot_semantic import assisted_search
        with TemporaryDirectory() as temporary:
            args={'root':temporary,'query':'app','intent':'編集'}
            with patch('pitchi_work.tools.execute_tool',return_value={'ok':True,'total':0,'matches':[]}), \
                    patch('pitchi_work.screenshot_semantic.opener') as network:
                cancelled=assisted_search(args,home=temporary,cancelled=lambda:True)
                self.assertEqual(cancelled['error']['code'],'CANCELLED')
                self.assertEqual(cancelled['vision_calls'],0)
                result=assisted_search(args,home=temporary)
                self.assertTrue(result['ok']);self.assertTrue(result['requires_human_selection'])
                network.assert_not_called()

    @unittest.skipUnless(os.getenv('PITCHI_TEST_SANDBOX')=='1','Explicit loopback HTTP test')
    def test_real_child_cancel_and_privacy_redirect(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        import time
        from pitchi_work.screenshot_semantic import call_image,opener
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302);self.send_header('Location','https://example.com/');self.end_headers()
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));time.sleep(.4)
                self.send_response(200);self.end_headers()
            def log_message(self,*_):pass
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        endpoint='http://127.0.0.1:'+str(http.server_port)
        try:
            with self.assertRaises(SearchFailure) as error:opener().open(endpoint)
            self.assertEqual(error.exception.code,'PRIVACY_REDIRECT_BLOCKED')
            with TemporaryDirectory() as temporary:
                started=time.monotonic()
                with self.assertRaises(SearchFailure) as error:
                    call_image(endpoint,'model',{},temporary,5,lambda:time.monotonic()-started>.2)
                self.assertEqual(error.exception.code,'CANCELLED')
                self.assertLess(time.monotonic()-started,2)
                self.assertEqual(list(Path(temporary).iterdir()),[])
        finally:http.shutdown();http.server_close();thread.join(timeout=2)


if __name__ == '__main__': unittest.main()
