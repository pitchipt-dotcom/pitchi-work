"""Tool boundaries only; real private NAS results are never test fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pitchi_work import cli
from pitchi_work.image_ocr import command,ocr_image,parse_tsv,OcrFailure
from pitchi_work.tools import catalog,execute_tool,resolve_ocr_request,save_tool_result


class ImageOcrTests(unittest.TestCase):
    def test_registry_and_natural_request_are_finite(self):
        self.assertFalse(catalog()['image.ocr']['network'])
        self.assertEqual(resolve_ocr_request('このスクショの文字を抜き出して: "a b.png"','/project'),
                         {'name':'image.ocr','input':{'path':'/project/a b.png'}})
        for request in ('画像を検索できるようにして','OCR a.png; rm x','OCR a.png b.png',
                        'OCR a.png そして公開して'):
            self.assertIsNone(resolve_ocr_request(request))
        self.assertFalse(execute_tool('unknown',{})['ok'])

    def test_missing_invalid_input_and_engine_errors(self):
        self.assertEqual(ocr_image({'path':'/nonexistent/image.png'})['error']['code'],'FILE_NOT_FOUND')
        for request in ({},{'path':'x','shell':'x'},{'path':'x','psm':True},{'path':'x','timeout_seconds':float('nan')}):
            self.assertEqual(ocr_image(request)['error']['code'],'INVALID_INPUT')
        with TemporaryDirectory() as directory:
            file=Path(directory)/'broken.png';file.write_bytes(b'not an image')
            self.assertEqual(ocr_image({'path':str(file)})['error']['code'],'INVALID_IMAGE')
            with patch('pitchi_work.image_ocr.shutil.which',return_value=None):
                self.assertEqual(ocr_image({'path':str(file)})['error']['code'],'ENGINE_UNAVAILABLE')

    def test_tsv_has_layout_confidence_and_no_rewriting(self):
        header='level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        parsed=parse_tsv(header+'5\t1\t1\t1\t1\t0\t1\t2\t3\t90\t日本語：\n'+
                         '5\t1\t1\t1\t1\t2\t1\t2\t3\t80\tABC:123\n',11)
        self.assertEqual(parsed['text'],'日本語： ABC:123')
        self.assertAlmostEqual(parsed['confidence'],.85)
        with self.assertRaises(OcrFailure):parse_tsv('invalid',11)

    def test_private_artifact_contains_hash_without_mission_verdict(self):
        import hashlib
        with TemporaryDirectory() as directory:
            artifact=save_tool_result(directory,'image.ocr',{'path':'example.png'},{'ok':False})
            path=Path(artifact['path'])
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777,0o700)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),artifact['sha256'])
            self.assertFalse(json.loads(path.read_text())['mission_completion'])

    def test_cli_request_route_does_not_call_llm_or_create_mission(self):
        with TemporaryDirectory() as directory,patch.object(cli,'health') as health,\
             patch.object(cli,'create_mission') as mission,contextlib.redirect_stdout(io.StringIO()) as output:
            code=cli.main(['--home',directory,'--workspace',directory,'--request','この画像を読んで missing.png'])
            self.assertEqual(code,2)
            self.assertEqual(json.loads(output.getvalue())['result']['error']['code'],'FILE_NOT_FOUND')
            health.assert_not_called();mission.assert_not_called()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--home',directory,'--workspace',directory,
                    '--request','OCR image.png','--instruction','publish elsewhere']),2)
            health.assert_not_called();mission.assert_not_called()

    def test_real_cpu_png_and_jpeg_keep_originals(self):
        from PIL import Image,ImageDraw,ImageFont
        from pitchi_work.image_ocr import digest
        font=ImageFont.truetype('DejaVuSans.ttf',40)
        with TemporaryDirectory() as directory:
            for extension in ('png','jpg'):
                path=Path(directory)/('example.'+extension)
                image=Image.new('RGB',(450,100),'white')
                ImageDraw.Draw(image).text((15,15),'HELLO 123',font=font,fill='black');image.save(path)
                before=digest(path)
                result=execute_tool('image.ocr',{'path':str(path),'languages':'eng','psm':6})
                self.assertTrue(result['ok'],result['error'])
                self.assertIn('HELLO',result['text'])
                self.assertIn('123',result['text'])
                self.assertEqual(digest(path),before)
                self.assertTrue(result['source_unchanged'])
                with patch('pitchi_work.image_ocr.digest',return_value='different'):
                    invalid=ocr_image({'path':str(path),'languages':'eng','psm':6})
                self.assertEqual(invalid['error']['code'],'SOURCE_CHANGED')
                self.assertFalse(invalid['ok']);self.assertEqual(invalid['text'],'')

    def test_timeout_and_cancel_reap_process(self):
        import sys
        real_popen=subprocess.Popen
        processes=[]
        def spawn(*args,**kwargs):
            process=real_popen(*args,**kwargs);processes.append(process);return process
        with TemporaryDirectory() as directory,patch('pitchi_work.image_ocr.subprocess.Popen',side_effect=spawn):
            with self.assertRaises(OcrFailure) as failure:
                command([sys.executable,'-c','import time;time.sleep(60)'],.2,lambda:False,directory,os.environ.copy())
            self.assertEqual(failure.exception.code,'TIMEOUT')
            count=[0]
            def cancelled():count[0]+=1;return count[0]>3
            with self.assertRaises(OcrFailure) as failure:
                command([sys.executable,'-c','import time;time.sleep(60)'],5,cancelled,directory,os.environ.copy())
            self.assertEqual(failure.exception.code,'CANCELLED')
        self.assertTrue(all(p.poll() is not None for p in processes))


if __name__=='__main__':unittest.main()
