"""Generic diagnostic navigation and tested provenance, no training case."""
import hashlib,unittest
from pitchi_work.diagnostic_navigation import failure_source
from pitchi_work.source_navigation import serialized_pages
from pitchi_work.runner import HostDefect


class ExecutionContextTests(unittest.TestCase):
    def test_automatic_window_is_bounded_without_cutting_a_source_line(self):
        source='def combine():\n'+''.join('    # note '+str(i)+' '+('x'*180)+'\n'for i in range(20))+'    return missing\n'
        receipt=self.receipt('module.py',source,'File "/workspace/module.py", line 22, in combine')
        result=failure_source(receipt,{'module.py':source},lambda t:serialized_pages(t,2000))
        self.assertLessEqual(len(result['text'].encode()),900);self.assertIn('return missing',result['text'])
        lines=source.splitlines(True)
        self.assertEqual(result['text'],''.join(lines[result['start_line']-1:result['end_line']]))
        self.assertEqual(''.join(p['text']for p in serialized_pages(source,2000)),source)
        source='return '+('x'*1400)+'\n';receipt=self.receipt('module.py',source,'File "/workspace/module.py", line 1, in combine')
        result=failure_source(receipt,{'module.py':source},lambda t:serialized_pages(t,2000))
        self.assertEqual(result['text'],'');self.assertTrue(result['oversized_line_requires_page_request'])
        self.assertEqual(''.join(p['text']for p in serialized_pages(source,2000)),source)

    def receipt(self,name,text,error):
        return {'results':[{'test':'tests/protected.py','passed':False,
            'execution':{'exit_code':1,'stderr':error,'stdout':''}}],
            'source_hashes':{name:hashlib.sha256(text.encode()).hexdigest()}}

    def test_python_non_header_error_page_is_exact_and_scoped(self):
        source='# Generic header\n'*120+'def combine(a,b):\n    return unknown_value\n'
        error='Traceback:\n  File "/isolated/workspace/tests/protected.py", line 1, in test\n  File "/isolated/workspace/module.py", line 122, in combine\nNameError: unknown_value\n'
        receipt=self.receipt('module.py',source,error);protected='combine(2,3)\n'
        receipt['source_hashes']['tests/protected.py']=hashlib.sha256(protected.encode()).hexdigest()
        result=failure_source(receipt,{'module.py':source,'tests/protected.py':protected},lambda t:serialized_pages(t,500))
        self.assertEqual(result['diagnostic_line'],122);self.assertGreater(result['source_page'],0)
        self.assertIn('unknown_value',result['text']);self.assertEqual(result['enclosing_declaration']['declaration'],'def combine(a,b):')
        self.assertEqual(result['source_sha256'],hashlib.sha256(source.encode()).hexdigest())
        self.assertIn('not a proven root cause',result['interpretation'])

    def test_php_full_location_survives_long_runtime_prefix(self):
        source='<?php\nfunction combine(int $a,int $b):int {\n return $missing;\n}\n'
        error='Warning: Undefined variable $missing in /'+('very_long_prefix/'*30)+'workspace/module.php on line 3\n'
        result=failure_source(self.receipt('module.php',source,error),{'module.php':source},lambda t:serialized_pages(t,500))
        self.assertEqual(result['path'],'module.php');self.assertEqual(result['diagnostic_line'],3)
        self.assertEqual(result['enclosing_declaration']['declaration'],'function combine(int $a,int $b):int {')

    def test_foreign_path_or_impossible_line_cannot_become_source(self):
        source='def combine(): return 7\n'
        for error in ('File "/outside/secrets.py", line 1, in module','Fatal at /workspace/module.py:99'):
            self.assertIsNone(failure_source(self.receipt('module.py',source,error),{'module.py':source},lambda t:serialized_pages(t,500)))
        receipt=self.receipt('module.py',source,'File "/workspace/module.py", line 1, in module')
        receipt['source_hashes']['module.py']='0'*64
        with self.assertRaises(HostDefect):failure_source(receipt,{'module.py':source},lambda t:serialized_pages(t,500))

    def test_pass_has_no_invented_diagnosis(self):
        source='return 7\n';receipt=self.receipt('module.py',source,'/workspace/module.py:1')
        receipt['results'][0]['passed']=True
        self.assertIsNone(failure_source(receipt,{'module.py':source},lambda t:serialized_pages(t,500)))
