"""Deterministic validation/transaction tests; end-to-end evidence uses real PHP."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ptotsearch_tools import cli

ROOT = Path(__file__).resolve().parents[1]

class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.m = cli.metadata(ROOT/'tests/fixtures/metadata/kdigo.json')
        self.project = self.root/'project'
        self.project.mkdir()
        (self.project/'unchanged.txt').write_text('protected')
        self.state = {'current_manifest':cli.manifest(self.project),'registered':{}}
        cli.save(self.root/'session.json',self.state)
        (self.root/'history').mkdir()
    def tearDown(self):
        self.tmp.cleanup()
    def write(self,m):
        p=self.root/'input.json';cli.save(p,m);return p
    def test_reject_invalid_fields(self):
        for fields in [{'id':'../escape'},{'official_url':'http://example.org/'},
                       {'official_url':'https://user:pass@example.org/'},
                       {'revision_year':True},{'allowed_scope':'full_text'},
                       {'summary':'generated'}, {'diseases':['CKD','CKD']},
                       {'search_terms':[]}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                cli.metadata(self.write({**self.m,**fields}))
    def test_duplicate_json_keys(self):
        p=self.root/'input.json';p.write_text('{"id":"a","id":"b"}')
        with self.assertRaisesRegex(cli.Invalid,'Duplicate JSON key'):cli.load(p)
    def test_preview_register_idempotence_and_conflict(self):
        with patch.object(cli,'support_plan',return_value=None):
            _,preview=cli.plan(self.project,self.m)
            self.assertFalse(preview['noop'])
            self.assertEqual(cli.manifest(self.project),self.state['current_manifest'])
            cli.register(self.root,self.project,self.state,self.m)
            current=cli.load(self.root/'session.json')
            snapshot=cli.manifest(self.project)
            self.assertTrue(cli.register(self.root,self.project,current,self.m)['noop'])
            self.assertEqual(snapshot,cli.manifest(self.project))
            with self.assertRaisesRegex(cli.Invalid,'ID conflict'):
                cli.plan(self.project,{**self.m,'title':'different'})
            with self.assertRaisesRegex(cli.Invalid,'URL already registered'):
                cli.plan(self.project,{**self.m,'id':'different-id'})
            self.assertEqual(snapshot,cli.manifest(self.project))
    def test_failed_swap_rolls_back(self):
        original=os.replace
        def fail_stage(src,dst):
            if Path(src).name.startswith('stage-') and Path(dst)==self.project:
                raise OSError('injected swap failure')
            return original(src,dst)
        with patch.object(cli,'support_plan',return_value=None),patch.object(cli.os,'replace',side_effect=fail_stage):
            with self.assertRaisesRegex(OSError,'injected'):cli.register(self.root,self.project,self.state,self.m)
        self.assertEqual(cli.manifest(self.project),self.state['current_manifest'])
        self.assertEqual(cli.load(self.root/'session.json'),self.state)
        self.assertFalse((self.root/'pending.json').exists())
    def test_recovery_after_interruption(self):
        source=self.root/'source';source.mkdir();(source/'unchanged.txt').write_text('protected')
        (self.root/'search.sqlite').write_bytes(b'fixed-test-database')
        state={**self.state,'kind':'ptotsearch-bibliography-isolated','workspace':str(self.root),
               'source':str(source),'origin_manifest':cli.manifest(source),'inputs':{},
               'database_sha256':cli.sha(self.root/'search.sqlite')}
        cli.save(self.root/'session.json',state)
        os.replace(self.project,self.root/'backup-test');self.project.mkdir();(self.project/'partial.txt').write_text('partial')
        cli.save(self.root/'pending.json',{'status':'PREPARED','backup':'backup-test','stage':'stage-test','prior_state':state})
        with cli.session(self.root) as (_,project,recovered):
            self.assertEqual(recovered,state)
            self.assertEqual(cli.manifest(project),state['current_manifest'])
        self.assertEqual(len(list((self.root/'history').glob('recovery-*.json'))),1)
        self.assertFalse((self.root/'pending.json').exists())
    def test_foreign_workspace_rejected(self):
        with self.assertRaises(cli.Invalid):
            with cli.session(self.root):pass
    def test_gold_frozen_inputs(self):
        for p in ['tests/fixtures/metadata/aortic-gold.json','tests/fixtures/metadata/ckd-gold.json']:
            self.assertTrue(cli.gold(ROOT/p)['frozen_before_implementation'])

if __name__=='__main__':unittest.main()
