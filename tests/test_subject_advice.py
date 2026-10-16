"""Original failure remains immutable; Advisor lifecycle is independent."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from pitchi_work.headless import run_at, view
from pitchi_work.subject_advice import request_advice, result_advice


class SubjectAdviceTests(unittest.TestCase):
    def test_saved_subject_remains_unchanged(self):
        home = os.environ.get('PITCHI_FOLLOWUP_SAVED_HOME')
        if not home: self.skipTest('Saved Work home required')
        subject = run_at(Path(home), '20261008_203253_71b180')
        files = [p for p in subject.path.rglob('*') if p.is_file()]
        before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        with TemporaryDirectory() as temporary:
            config = {'endpoint':'http://127.0.0.1:8092/v1','model':'test','process_supervisor':'systemd'}
            with patch('pitchi_work.subject_advice.subprocess.run') as launch:
                launch.return_value.returncode = 0
                response = request_advice(Path(temporary),subject,config)
            self.assertEqual(response['subject_run_id'],subject.run_id)
            self.assertEqual(response['failure_class'],'HOST_PROFILE_BROWSER_INTERACTION_FAIL')
            self.assertIn('Visible app and registered interaction elements required',response['reason'])
            self.assertEqual(response['status'],'RUNNING')
            self.assertEqual(result_advice(Path(temporary),subject.run_id,response['advisor_run_id']),response)
            with self.assertRaises(ValueError):
                result_advice(Path(temporary),'other',response['advisor_run_id'])
            with patch('pitchi_work.subject_advice.subprocess.run') as launch:
                launch.return_value.returncode = 1
                failed = request_advice(Path(temporary),subject,config)
            self.assertEqual(failed['status'],'FAILED')
            self.assertEqual(failed['advisor_failure_class'],'ADVISOR_DISPATCH_FAILURE')
            self.assertEqual(failed['subject_failure_class'],'HOST_PROFILE_BROWSER_INTERACTION_FAIL')
            self.assertEqual(view(subject)['original_request'],'足し算アプリ作って')
        self.assertEqual(before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


if __name__ == '__main__': unittest.main()
