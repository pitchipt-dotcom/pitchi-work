"""Host preview lifecycle, using saved real app files; no LLM or mission run."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pitchi_work.artifacts import register,operation,owned,PreviewError
from pitchi_work.store import load
from pitchi_work.tools import catalog,resolve_tool_request


class ArtifactPreview(unittest.TestCase):
    def test_finite_registry_and_request_route(self):
        self.assertIn('artifact.preview',catalog())
        self.assertEqual(resolve_tool_request('Screenshot Searchを開く')['input']['artifact_id'],'screenshot-search')
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(PreviewError):register(home,{'kind':'static_html','scope':'public'})
            with self.assertRaises(PreviewError):register(home,{'kind':'static_html','scope':'localhost'})
        self.assertFalse(owned({'pid':os.getpid(),'start_time':'fake','instance':'fake'},'backend'))

    @unittest.skipUnless(os.getenv('PITCHI_TEST_SANDBOX')=='1','Explicit local HTTP lifecycle test')
    def test_real_app_identity_lifecycle_and_gateway_port_collision(self):
        source=Path(__file__).resolve().parents[1]/'workspaces/20261008_165626_0b42d5'
        if not source.is_dir():self.skipTest('Saved real app unavailable')
        with tempfile.TemporaryDirectory(prefix='pitchi-preview-test-') as home:
            identifier=register(home,{'kind':'static_html','root':str(source),'scope':'localhost'})
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                first=operation(home,'preview',identifier,'localhost')
                time.sleep(.3)  # A completed HTTP handler must not kill its backend.
                again=operation(home,'preview',identifier,'localhost')
                self.assertEqual(first['pid'],again['pid']);self.assertEqual(first['url'],again['url'])
                with opener.open(first['url']) as response:
                    self.assertEqual(response.headers['X-Pitchi-Artifact'],identifier)
                    self.assertEqual(response.read(),(source/'index.html').read_bytes())
                with opener.open(first['url']+'app.js') as response:
                    self.assertEqual(response.read(),(source/'app.js').read_bytes())
                with self.assertRaises(urllib.error.HTTPError) as error:opener.open(first['url']+'../../state.json')
                self.assertEqual(error.exception.code,404)
                error.exception.close()
                child=load(Path(home)/'artifact_data/previews.json')[identifier]
                operation(home,'stop',identifier,'localhost');self.assertFalse(owned(child,'backend'))
                with self.assertRaises(urllib.error.HTTPError) as error:opener.open(first['url'])
                self.assertEqual(error.exception.code,503)
                error.exception.close()
                restart=operation(home,'preview',identifier,'localhost')
                self.assertEqual(first['url'],restart['url']);self.assertNotEqual(first['pid'],restart['pid'])
                record=load(Path(home)/'artifact_data/gateway.json')
                operation(home,'shutdown',scope='localhost')
                self.assertFalse(owned(record,'gateway'))
                with socket.socket() as competitor:
                    competitor.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    competitor.bind(('127.0.0.1',record['port']));competitor.listen()
                    alternative=operation(home,'preview',identifier,'localhost')
                    new=load(Path(home)/'artifact_data/gateway.json')
                    self.assertNotEqual(record['port'],new['port'])
                    self.assertEqual(competitor.getsockname()[1],record['port'])
                    with opener.open(alternative['url']) as response:self.assertEqual(response.status,200)
            finally:operation(home,'shutdown',scope='localhost')
