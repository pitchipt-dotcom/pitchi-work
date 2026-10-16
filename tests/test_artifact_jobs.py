"""Artifact boundaries and saved failure regression; no live LLM acceptance."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pitchi_work.artifact_jobs import source_context,publish_revision
from pitchi_work.artifacts import register,data_dir,PreviewError
from pitchi_work.store import load,dump,ReadOnlyRun
from pitchi_work.mission_profiles import normalize_request_spans,bind_requirements,inventory,sha


class ArtifactJobs(unittest.TestCase):
    def test_saved_review_failure_reaches_worker_feedback(self):
        from pitchi_work.mission_controller import validation_feedback
        path=Path(__file__).resolve().parents[1]/'runs/20261008_182154_b82652/artifacts/P1_host_1.json'
        if not path.exists():self.skipTest('Private failure unavailable')
        value=validation_feedback(load(path))
        self.assertIn('Mastered reviewed problem not removed',value['observations'][0]['review_observation']['failures'])

    def test_saved_additional_instruction_outputs_keep_original_and_i0(self):
        base=Path(__file__).resolve().parents[1]/'runs/20261008_182559_1c75dc'
        if not base.exists():self.skipTest('Private saved failure unavailable')
        request=(base/'request.md').read_text();instruction=(base/'instruction.md').read_text()
        catalog=inventory(load(base/'state.json')['workspace'])
        for file in sorted((base/'artifacts').glob('role_call_*/output.txt')):
            value=load(file);original=json.dumps(value,sort_keys=True)
            with self.assertRaises(ValueError):bind_requirements(value,request,catalog)
            result=bind_requirements(normalize_request_spans(value,request,instruction),request,catalog)
            self.assertEqual(result['requirements'][0]['text'],request)
            self.assertEqual(json.dumps(value,sort_keys=True),original)
            self.assertTrue(instruction)  # Host appends the full immutable instruction as I0.

    def test_revision_clone_lineage_and_source_protection(self):
        source=Path(__file__).resolve().parents[1]/'workspaces/20261008_165626_0b42d5'
        if not source.exists():self.skipTest('Private real app unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary);copy=home/'original';shutil.copytree(source,copy)
            parent=register(home,{'kind':'static_html','root':str(copy),'scope':'localhost'})
            hashes={n:sha(copy/n) for n in ('index.html','app.js','style.css')}
            for operation_name in ('clone','modify'):
                context=source_context(home,parent,operation_name)
                runpath=home/'runs'/operation_name;runpath.mkdir(parents=True)
                dump(runpath/'artifact_input.json',context);run=ReadOnlyRun(runpath)
                changed=home/operation_name;shutil.copytree(copy,changed)
                identifier=register(home,{'kind':'static_html','root':str(changed),'scope':'localhost'})
                with patch('pitchi_work.artifact_jobs.operation',return_value={'ok':True}):
                    result=publish_revision(home,run,identifier)
                row=load(data_dir(home)/'registry.json')[result]
                self.assertEqual(hashes,{n:sha(copy/n) for n in hashes})
                if operation_name=='clone':
                    self.assertNotEqual(result,parent);self.assertEqual(row['parent_artifact_id'],parent)
                    self.assertEqual(row['revision'],1)
                else:
                    self.assertEqual(result,parent);self.assertEqual(row['revision'],2)
                    self.assertEqual(row['revisions'][0]['root'],str(copy))
                self.assertEqual(row['lineage'],{'parent_artifact_id':parent,'parent_revision':1})
            # Registering the old source must not reset the new logical pointer.
            historical=register(home,{'kind':'static_html','root':str(copy),'scope':'localhost'})
            self.assertNotEqual(historical,parent)
            self.assertEqual(load(data_dir(home)/'registry.json')[parent]['current_run_id'],'modify')
            (changed/'app.js').write_text('changed outside Host')
            with self.assertRaises(PreviewError):source_context(home,parent,'repair')
