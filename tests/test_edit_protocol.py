"""Host line serialization and bounded schema without any training material."""
import hashlib,json,re,unittest
from pitchi_work.edit_protocol import operation_schema,render_lines,replace_exact


class EditProtocolTests(unittest.TestCase):
    def test_exact_replacement_preserves_surrounding_bytes(self):
        self.assertEqual(replace_exact('prefix\na-b\nsuffix\n','a-b','a+b',900),'prefix\na+b\nsuffix\n')
        for old,new in [('missing','x'),('a','x'*901),('a','a')]:
            with self.assertRaises(ValueError):replace_exact('a',old,new,900)
        with self.assertRaises(ValueError):replace_exact('a a','a','b',900)
    def test_lines_and_literal_backslash_are_preserved(self):
        lines=['def label():',r'    return "a\nb"']
        self.assertEqual(render_lines(lines,True,900),'\n'.join(lines)+'\n')
        self.assertEqual(render_lines([],True,900),'')
        self.assertEqual(render_lines(['last'],False,900),'last')

    def test_hard_byte_limit_includes_host_line_separator(self):
        self.assertEqual(len(render_lines(['x'*899],True,900).encode()),900)
        with self.assertRaises(ValueError):render_lines(['x'*900],True,900)
        with self.assertRaises(ValueError):render_lines(['字'*301],False,900)
        with self.assertRaises(ValueError):render_lines(['first\nsecond'],True,900)
        with self.assertRaises(ValueError):render_lines(['x']*82,True,900)

    def test_operation_schema_is_scoped_and_no_new_acceptance(self):
        schema=operation_schema({'module.py':'# original\n','protected.py':'# protected\n'},['module.py'],2)
        self.assertEqual(json.loads(json.dumps(schema)),schema)
        patches=[s for s in schema['oneOf'] if s['properties']['action']['enum']==['patch']]
        self.assertEqual(len(patches),1)
        p=patches[0]['properties'];self.assertEqual(p['path']['enum'],['module.py'])
        self.assertEqual(p['source_sha256']['enum'],[hashlib.sha256(b'# original\n').hexdigest()])
        self.assertNotIn('replacement',p)
        pattern=p['replacement_lines']['items']['pattern']
        self.assertIsNone(re.fullmatch(pattern,'first\nsecond'))
        self.assertIsNotNone(re.fullmatch(pattern,r'return "a\nb"'))


if __name__=='__main__':unittest.main()
