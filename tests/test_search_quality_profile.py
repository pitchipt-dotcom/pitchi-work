"""Host patch/scope boundaries, not evidence of search-quality success."""
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from pitchi_work.search_quality import patched_files, line_edits, span_edits, read_spans, source_fragments, runtime_failure_owner, worker_schema, REGRESSION, TARGETS, TESTS
from pitchi_work.mission_profiles import bind_requirements, compile_contracts, UnsupportedMission


class SearchQualityBoundaries(unittest.TestCase):
    def test_exact_patch_and_registered_new_test(self):
        name=TARGETS[0]
        output={'patches':[{'path':name,'before':'old','after':'new'},
                           {'path':REGRESSION,'before':'','after':'<?php assert(true);'}]}
        result=patched_files(output,{name:'old'},[name,REGRESSION])
        self.assertEqual(result[name],'new')
        self.assertTrue(result[REGRESSION].startswith('<?php'))

    def test_ambiguous_match_and_scope_are_rejected(self):
        name=TARGETS[0]
        for patches in ([{'path':name,'before':'x','after':'y'}],
                        [{'path':'../config.php','before':'x','after':'y'}],
                        [{'path':name,'before':'','after':'replacement'}]):
            with self.assertRaises(ValueError):patched_files({'patches':patches},{name:'xx'},[name])

    def test_all_patches_checked_before_output(self):
        name=TARGETS[0]
        files={name:'old'}
        with self.assertRaises(ValueError):patched_files({'patches':[
            {'path':name,'before':'old','after':'new'},
            {'path':name,'before':'missing','after':'bad'}]},files,[name])
        self.assertEqual(files[name],'old')

    def test_only_new_template_output_can_be_regenerated(self):
        output={'patches':[{'path':REGRESSION,'before':'','after':'new test'}]}
        with self.assertRaises(ValueError):patched_files(output,{REGRESSION:'user original'},[REGRESSION])
        self.assertEqual(patched_files(output,{REGRESSION:'prior Worker output'},[REGRESSION],[REGRESSION])[REGRESSION],'new test')
        with self.assertRaises(ValueError):patched_files({'patches':[{'path':TARGETS[0],'before':'','after':'bad'}]},
            {TARGETS[0]:'implementation'},[TARGETS[0]],[TARGETS[0]])

    def test_line_edits_pin_hash_range_and_scope(self):
        import hashlib
        name=TARGETS[0];text='same\nkeep\nsame\n';digest=hashlib.sha256(text.encode()).hexdigest()
        edit={'path':name,'source_sha256':digest,'start_line':3,'end_line':3,'replacement':'fixed\n'}
        self.assertEqual(line_edits([edit],{name:text},[name])[name],'same\nkeep\nfixed\n')
        for override in ({'source_sha256':'stale'},{'path':'../config.php'},{'end_line':99},{'start_line':True}):
            with self.assertRaises(ValueError):line_edits([{**edit,**override}],{name:text},[name])
        with self.assertRaises(ValueError):line_edits([edit,edit],{name:text},[name])

    def test_explicit_source_span_not_fuzzy_matching(self):
        import hashlib
        name=TARGETS[0];text='    public static function search() { return "x"; }\n    private static function score() { return "x"; }\n'
        edit={'path':name,'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
              'span_id':'search','before':'return "x";','after':'return "y";'}
        updated=span_edits([edit],{name:text},[name])[name]
        self.assertEqual(updated.count('return "y";'),1)
        self.assertEqual(updated.count('return "x";'),1)
        with self.assertRaises(ValueError):span_edits([{**edit,'span_id':'unknown'}],{name:text},[name])
        with self.assertRaises(ValueError):span_edits([{**edit,'after':'x'*701}],{name:text},[name])
        schema=worker_schema({name:text},[name])
        self.assertEqual(schema['oneOf'][0]['properties']['edits']['maxItems'],4)
        self.assertEqual(schema['oneOf'][1]['properties']['read_spans']['maxItems'],1)
        view=read_spans([{'path':name,'span_id':'score'}],{name:text},[name])
        self.assertIn('return "x";',view[name]['fragments'][0]['text'])
        self.assertTrue(view[name]['fragments'][0]['complete_method'])

    def test_profile_needs_host_binding_and_protected_tests(self):
        names=[TARGETS[0],REGRESSION]
        catalog={'files':[{'path':n,'bytes':100} for n in names],'tests':TESTS}
        proposal={'profile':'php_search_quality','targets':names,'requirements':['investigate'],'needs_human':False}
        with self.assertRaises(UnsupportedMission):bind_requirements(proposal,'investigate',catalog)
        catalog['search_binding']=True
        requirements=bind_requirements(proposal,'investigate',catalog)
        plan=compile_contracts({'tasks':[{'requirement_ids':['M1'],'targets':names,'depends_on':[]}]},requirements,catalog)
        task=plan['tasks'][0]
        self.assertEqual(task['existing_tests'],TESTS)
        self.assertNotIn(REGRESSION,task['existing_tests'])
        self.assertEqual(task['verification_modes']['semantic_relevance'],'ASSISTED')
        self.assertEqual(task['max_attempts'],3)

    def test_fragments_are_explicitly_partial_and_bounded(self):
        text='    public static function search($db) {\n'+('        return "x";\n'*1500)+'    }\n'
        view=source_fragments({TARGETS[0]:text})
        item=view[TARGETS[0]]
        self.assertTrue(item['partial_view'])
        self.assertEqual(item['fragments'],[])
        self.assertGreater(item['available_spans']['search']['bytes'],8500)

    def test_changed_implementation_runtime_is_not_host_defect(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);workspace=root/'workspace';name=TARGETS[0]
            for base in (workspace,root/'baseline_files'):
                (base/name).parent.mkdir(parents=True,exist_ok=True);(base/name).write_text('original')
            (root/'artifacts').mkdir();(root/'artifacts/search_before.json').write_text('{}')
            run=SimpleNamespace(path=root,state=lambda:{'workspace':str(workspace)})
            receipt={'stderr':'Fatal error: SQL incomplete input in /tmp/project/'+name+':90'}
            self.assertEqual(runtime_failure_owner(run,receipt,'1'),'host')
            (workspace/name).write_text('changed')
            self.assertEqual(runtime_failure_owner(run,receipt,'1'),'worker')
            self.assertEqual(runtime_failure_owner(run,receipt,'before'),'host')
            self.assertEqual(runtime_failure_owner(run,{'stderr':'Fatal error: import in /tmp/host-search-probe.php:3'},'1'),'host')


if __name__=='__main__':unittest.main()
