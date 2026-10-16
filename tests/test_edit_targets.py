"""Synthetic Host block references; no subject answers or network requests."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from pitchi_work.edit_targets import EditTargets, text_hash
from pitchi_work.cloud_takeover_live import apply_edit
from pitchi_work.phase_workflow import digest
from pitchi_work.resolution import read_verified, write_verified


class EditTargetTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'host/targets.json'
        self.files={'calc.py':'def add(a,b):\r\n    return a - b\r\n# unchanged\r\n','other.py':'protected\n'}
        self.contract={'allowed_paths':['calc.py'],'phase_policy':{'max_response_bytes':1800,'max_replacement_bytes':900}}
        self.contract['sha256']=digest(self.contract)
        self.store=EditTargets(self.path,self.contract['allowed_paths'],self.contract['sha256'])
        self.target=self.store.issue(self.files,'calc.py',2,2)
        self.response={'action':'replace_target','target_id':self.target['target_id'],'new_text':'    return a + b\r\n'}

    def test_valid_without_copying_original_and_unaffected_bytes(self):
        candidate=apply_edit(self.response,self.files,self.contract,targets=self.store)
        self.assertNotIn('old_text',self.response)
        self.assertEqual(candidate['calc.py'],'def add(a,b):\r\n    return a + b\r\n# unchanged\r\n')
        self.assertEqual(candidate['other.py'],self.files['other.py'])

    def test_unknown_and_stale_targets(self):
        with self.assertRaises(ValueError):self.store.resolve(self.files,{**self.response,'target_id':'invented'},900)
        changed={**self.files,'calc.py':self.files['calc.py']+'# concurrent edit\n'}
        with self.assertRaisesRegex(ValueError,'Stale'):self.store.resolve(changed,self.response,900)

    def test_scope_range_and_ambiguity_rejected(self):
        for path,a,b in [('other.py',1,1),('calc.py',0,1),('calc.py',1,200),('calc.py',True,1)]:
            with self.subTest(path=path,a=a),self.assertRaises(ValueError):self.store.issue(self.files,path,a,b)
        repeated={'calc.py':'same\nsame\n'}
        with self.assertRaises(ValueError):self.store.issue_unique(repeated,'calc.py','same\n')
        with self.assertRaises(ValueError):self.store.issue_unique(self.files,'calc.py','return a - b')

    def test_no_response_path_or_range_override(self):
        for field,value in [('path','other.py'),('start_line',1),('source_sha256','0'*64)]:
            with self.subTest(field=field),self.assertRaises(ValueError):self.store.resolve(self.files,{**self.response,field:value},900)

    def test_invalid_new_text_byte_limit_and_no_progress(self):
        for value in (None,[], '\x00', '\ud800', 'x'*901, '    return a - b\r\n','joined without newline'):
            with self.subTest(value=repr(value)),self.assertRaises(ValueError):self.store.resolve(self.files,{**self.response,'new_text':value},900)

    def test_resume_id_original_hash_and_content(self):
        reopened=EditTargets(self.path,self.contract['allowed_paths'],self.contract['sha256'])
        self.assertEqual(reopened.issue(self.files,'calc.py',2,2),self.target)
        record=read_verified(self.path)['targets'][self.target['target_id']]
        self.assertEqual(record['original_text'],'    return a - b\r\n')
        self.assertEqual(record['original_sha256'],text_hash(record['original_text']))
        self.assertEqual(reopened.resolve(self.files,self.response,900)['start_line'],2)

    def test_registry_tamper_wrong_binding_and_foreign_namespace(self):
        with self.assertRaises(ValueError):EditTargets(self.path,['other.py'],self.contract['sha256'])
        with self.assertRaises(ValueError):EditTargets(self.path,['calc.py'],'wrong')
        other=EditTargets(self.path.parent/'foreign.json',['calc.py'],self.contract['sha256'])
        with self.assertRaises(ValueError):other.resolve(self.files,self.response,900)
        data=read_verified(self.path);data['targets'][self.target['target_id']]['start_line']=1;write_verified(self.path,data)
        with self.assertRaises(ValueError):self.store.resolve(self.files,self.response,900)

    def test_deletion_and_empty_file_insertion(self):
        self.assertEqual(apply_edit({**self.response,'new_text':''},self.files,self.contract,targets=self.store)['calc.py'],'def add(a,b):\r\n# unchanged\r\n')
        target=self.store.issue({'calc.py':''},'calc.py',1,0)
        patch=self.store.resolve({'calc.py':''},{'action':'replace_target','target_id':target['target_id'],'new_text':'# new\n'},900)
        self.assertEqual(patch['replacement'],'# new\n')

    def test_metadata_does_not_duplicate_original_and_schema_common(self):
        self.assertNotIn('original_text',self.target)
        self.assertLess(len(json.dumps(self.target).encode()),450)
        from pitchi_work.edit_protocol import operation_schema
        schema=operation_schema(self.files,['calc.py'],0,targets=True)
        actions=[v['properties']['action']['enum'][0] for v in schema['oneOf']]
        self.assertIn('replace_target',actions);self.assertIn('replace',actions)


if __name__=='__main__':unittest.main()
