"""Index boundaries, with actual image.ocr reuse. Private NAS is not a fixture."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import urllib.request

from PIL import Image,ImageDraw,ImageFont
from pitchi_work import cli
from pitchi_work.screenshots import normalized
from pitchi_work.tools import REGISTRY,execute_tool,resolve_tool_request


def picture(path,text):
    image=Image.new('RGB',(700,100),'white')
    ImageDraw.Draw(image).text((10,10),text,fill='black',font=ImageFont.truetype('DejaVuSans.ttf',36))
    image.save(path)


class ScreenshotTests(unittest.TestCase):
    def test_actual_ocr_incremental_change_move_delete_and_failure(self):
        with TemporaryDirectory() as temporary:
            home=Path(temporary);root=home/'images';root.mkdir()
            one=root/'Screenshot_20261001_120000_ChatGPT.png';picture(one,'n8n WordPress Codex')
            two=root/'Screenshot_20261002_120000_Termius.jpg';picture(two,'YouTube LINE 123')
            broken=root/'broken.png';broken.write_bytes(b'invalid image')
            before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
            args={'root':str(root),'languages':'eng','psm':6,'workers':2}
            original=REGISTRY['image.ocr']['handler']
            with patch.dict(REGISTRY['image.ocr'],{'handler':unittest.mock.Mock(wraps=original)}) as _:
                first=execute_tool('screenshot.index',args,home=home)
                self.assertTrue(first['ok'],first);self.assertEqual(first['ocr_success'],2);self.assertEqual(first['ocr_failed'],1)
                self.assertEqual(REGISTRY['image.ocr']['handler'].call_count,3)
                self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()})
                found=execute_tool('screenshot.search',{'root':str(root),'query':'WordPress'},home=home)
                self.assertTrue(found['ok']);self.assertEqual(found['total'],1)
                self.assertIn('ocr_text',found['matches'][0]['match_reasons'])
                again=execute_tool('screenshot.index',args,home=home)
                self.assertEqual(again['ocr_calls'],0);self.assertEqual(again['unchanged'],3)
                duplicate=root/'duplicate.png';duplicate.write_bytes(one.read_bytes())
                duplicate_result=execute_tool('screenshot.index',args,home=home)
                self.assertEqual(duplicate_result['ocr_calls'],0);self.assertEqual(duplicate_result['cached_reused'],1)
                duplicate.unlink()
                removed_duplicate=execute_tool('screenshot.index',args,home=home)
                self.assertEqual(removed_duplicate['deleted'],1);self.assertEqual(removed_duplicate['ocr_calls'],0)
                moved=root/'moved.png';one.rename(moved)
                repeat=execute_tool('screenshot.index',args,home=home)
                self.assertEqual(repeat['ocr_calls'],0);self.assertEqual(repeat['cached_reused'],1);self.assertEqual(repeat['deleted'],1)
                picture(moved,'ComfyUI 456')
                updated=execute_tool('screenshot.index',args,home=home)
                self.assertEqual(updated['ocr_calls'],1)
                self.assertEqual(execute_tool('screenshot.search',{'root':str(root),'query':'WordPress'},home=home)['total'],0)
                self.assertEqual(execute_tool('screenshot.search',{'root':str(root),'query':'ComfyUI'},home=home)['total'],1)
                moved.unlink()
                deleted=execute_tool('screenshot.index',args,home=home)
                self.assertEqual(deleted['deleted'],1);self.assertEqual(deleted['ocr_calls'],0)
                self.assertEqual(execute_tool('screenshot.search',{'root':str(root),'query':'ComfyUI'},home=home)['total'],0)
                retry=execute_tool('screenshot.index',{**args,'retry_errors':True},home=home)
                self.assertEqual(retry['ocr_calls'],1);self.assertEqual(retry['ocr_failed'],1)

    def test_filters_private_db_unrelated_db_and_finite_router(self):
        import sqlite3
        with TemporaryDirectory() as temporary:
            home=Path(temporary);root=home/'images';root.mkdir()
            picture(root/'Screenshot_20261001_120000_LINE.png','HELLO 123')
            out=execute_tool('screenshot.index',{'root':str(root),'languages':'eng','psm':6},home=home)
            self.assertTrue(out['ok'],out)
            self.assertEqual(os.stat(out['database']).st_mode&0o777,0o600)
            for query in ('" OR *','日本語','; DROP TABLE images;'):
                self.assertTrue(execute_tool('screenshot.search',{'root':str(root),'query':query},home=home)['ok'])
            found=execute_tool('screenshot.search',{'root':str(root),'query':'','app':'LINE','date_from':'2026-10-01','date_to':'2026-10-01'},home=home)
            self.assertEqual(found['total'],1)
            self.assertFalse(execute_tool('screenshot.search',{'root':str(root),'date_from':'bad'},home=home)['ok'])
            self.assertFalse(execute_tool('screenshot.index',{'root':str(root),'database':str(root/'db.sqlite3')},home=home)['ok'])
            unknown=home/'unknown.sqlite3'
            with sqlite3.connect(unknown) as conn:conn.execute('CREATE TABLE protected(x)')
            before=unknown.read_bytes()
            result=execute_tool('screenshot.index',{'root':str(root),'database':str(unknown)},home=home)
            self.assertEqual(result['error']['code'],'UNSUPPORTED_DATABASE');self.assertEqual(before,unknown.read_bytes())
            self.assertEqual(normalized('日本 語　ＡＢＣ'),'日本語abc')
            routed=resolve_tool_request(str(root)+' からChatGPT関連のスクリーンショットを探す')
            self.assertEqual(routed['input']['query'],'ChatGPT')
            self.assertEqual(resolve_tool_request('このフォルダを検索可能にして',root)['input']['root'],str(root))
            with patch.object(cli,'health') as health,patch.object(cli,'create_mission') as create,contextlib.redirect_stdout(io.StringIO()) as stdout:
                code=cli.main(['--home',str(home),'--workspace',str(root),'--request','LINEのスクリーンショットを探して'])
                self.assertEqual(code,0);self.assertEqual(json.loads(stdout.getvalue())['result']['total'],1)
                health.assert_not_called();create.assert_not_called()

    def test_cancel_and_unindexed_root_are_structured(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary)/'images';root.mkdir()
            self.assertEqual(execute_tool('screenshot.search',{'root':str(root),'query':'x'},home=temporary)['error']['code'],'INDEX_NOT_FOUND')
            result=execute_tool('screenshot.index',{'root':str(root)},home=temporary,cancelled=lambda:True)
            self.assertEqual(result['error']['code'],'CANCELLED')

    @unittest.skipUnless(os.getenv('PITCHI_TEST_SANDBOX')=='1','Explicit loopback HTTP test')
    def test_ui_uses_search_api_and_protects_origin_and_image_scope(self):
        from pitchi_work.screenshot_ui import server
        with TemporaryDirectory() as temporary:
            root=Path(temporary)/'images';root.mkdir()
            picture(root/'Screenshot_20261001_120000_ChatGPT.png','HELLO 123')
            execute_tool('screenshot.index',{'root':str(root),'languages':'eng','psm':6},home=temporary)
            http=server(temporary,root);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{http.server_port}'
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                data=json.loads(opener.open(base+'/api/search?query=ChatGPT').read())
                self.assertEqual(data['total'],1);identifier=data['matches'][0]['id']
                for route in ('/','/api/options','/thumb?id='+str(identifier),'/image?id='+str(identifier),'/api/ocr?id='+str(identifier)):
                    self.assertEqual(opener.open(base+route).status,200)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(urllib.request.Request(base+'/',headers={'Host':'evil.invalid'}))
                self.assertEqual(error.exception.code,403)
                error.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as error:opener.open(base+'/image?id=99999')
                error.exception.close()
            finally:http.shutdown();http.server_close();thread.join()


if __name__=='__main__':unittest.main()
