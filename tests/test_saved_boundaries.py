"""Actual saved-run inputs only. These checks are not live Acceptance evidence."""
import json
import os
import unittest
from pathlib import Path
from pitchi_work.store import ReadOnlyRun,load
from pitchi_work.runtime import HostCore
from pitchi_work.model import local_url
from pitchi_work.workspace import artifact_inputs
from pitchi_work.integration_packet import message_budget

SOURCE=Path(os.environ.get('PITCHI_SAVED_RUN','/path/to/saved-run'))


@unittest.skipUnless(os.environ.get('PITCHI_SAVED_RUN'), 'Private saved-run fixture must be supplied explicitly; not distributed')
class SavedBoundaries(unittest.TestCase):
    def test_frozen_contract_and_acknowledgement(self):
        core=HostCore(ReadOnlyRun(SOURCE))
        core.requirement(json.loads((SOURCE/'artifacts/host_requirement_read_raw.txt').read_text()))
        actual=core.planner(json.loads((SOURCE/'artifacts/host_planner_raw_1.txt').read_text()),load(SOURCE/'continuation.json')['candidates'])
        self.assertEqual(actual,load(SOURCE/'plan.json'))

    def test_worker_inputs_are_complete_outputs_and_exact_retrieval_ast(self):
        workspace=Path(load(SOURCE/'state.json')['workspace'])
        for row in load(SOURCE/'plan.json')['tasks']:
            inputs=artifact_inputs(workspace,row)
            for name,text in inputs['output_files'].items():self.assertEqual(text,(workspace/name).read_text())
            self.assertIn('search_pages',inputs['existing_retrieval_functions'])
            self.assertNotIn('truncated',inputs['existing_retrieval_functions']['search_pages'])

    def test_saved_reviewer_judgments(self):
        core=HostCore(ReadOnlyRun(SOURCE))
        paths=sorted((SOURCE/'reviews').glob('H*.json'))
        self.assertTrue(paths, 'Saved Reviewer evidence must exist')
        for path in paths:
            review=load(path);tid=path.name.split('_')[0];record=load(SOURCE/'tasks'/f'{tid}.json')
            assessment=review.get('assessment',review.get('model_assessment'))
            tests=review.get('tests',review.get('evidence',{}).get('tests',[]))
            self.assertEqual(core.reviewer(record['task'],tests,assessment),'PASS')

    def test_existing_model_endpoint_is_local(self):
        self.assertEqual(local_url(load(SOURCE/'state.json')['local_model_endpoint']),'http://127.0.0.1:8092/v1')


if __name__=='__main__':unittest.main()
