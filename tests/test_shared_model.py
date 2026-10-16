import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pitchi_work.shared_model import configuration, headers, retained_mission, job_use, RejectRedirect

class SharedModel(unittest.TestCase):
    def config(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=Path(temp.name)/'shared.env';p.write_text('STRATA_BASE_URL=http://127.0.0.1:8000/api/models/strata/v1\nSTRATA_MODEL=strata-test\nSTRATA_SHARED_TOKEN=fixture-key\n')
        return patch.dict('os.environ',{'STRATA_ENV_FILE':str(p)},clear=True)

    def test_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            RejectRedirect().redirect_request(None,None,307,'redirect',{},'https://example.invalid')

    def test_scoped_headers_and_external_rejection(self):
        with self.config():
            self.assertEqual(headers('http://127.0.0.1:8092/v1'),{})
            self.assertEqual(headers('http://127.0.0.1:8000/api/models/strata/v1'),{'Authorization':'Bearer fixture-key'})
            with self.assertRaises(ValueError):headers('http://example.invalid/api/models/strata/v1')

    def test_release_on_failure(self):
        with patch('pitchi_work.shared_model.request',return_value={'use_id':'job'}) as request:
            with self.assertRaisesRegex(RuntimeError,'worker failed'):
                with job_use():raise RuntimeError('worker failed')
            self.assertEqual(request.call_args.args,('/uses/job',))
            self.assertEqual(request.call_args.kwargs['method'],'DELETE')

    def test_whole_mission_use_and_no_legacy_use(self):
        @retained_mission
        def run(self):
            self.adapter._shared_use_check()
            return 'done'
        obj=SimpleNamespace(adapter=SimpleNamespace(endpoint='http://127.0.0.1:8000/api/models/strata/v1'))
        with patch('pitchi_work.shared_model.request',return_value={'use_id':'mission'}) as request:
            self.assertEqual(run(obj),'done')
            self.assertIsNone(obj.adapter._shared_use_check)
            self.assertEqual(request.call_count,2)
        @retained_mission
        def legacy(self):return 'legacy'
        with patch('pitchi_work.shared_model.request') as request:
            self.assertEqual(legacy(SimpleNamespace(adapter=SimpleNamespace(endpoint='http://127.0.0.1:8092/v1'))),'legacy')
            request.assert_not_called()
