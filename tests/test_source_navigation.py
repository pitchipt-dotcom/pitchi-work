import unittest
import tempfile,json
from pitchi_work.source_navigation import outline,search,serialized_pages
from pitchi_work.phase_workflow import pages
from pitchi_work.working_context import WorkingContext,encoded


class NavigationTests(unittest.TestCase):
    def test_escaped_source_budget_preserves_every_original_byte(self):
        text=('quoted = "'+'\\"'*90+'"\n')*30+'# 字\n'
        chunks=serialized_pages(text,1000)
        self.assertEqual(''.join(p['text'] for p in chunks),text)
        self.assertTrue(all(len(json.dumps(p,ensure_ascii=False,separators=(',',':')).encode())<=1000 for p in chunks))
    def test_optional_navigation_history_is_referenced_not_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            context=WorkingContext(root+'/history',root)
            nav={'symbols':{'file'+str(i):[{'symbol':'function_'+str(i),'page':i}] for i in range(70)},
                'read_history':[{'path':'source_'+str(i),'page':i} for i in range(12)]}
            payload={'objective':'x'*4000,'source_view':{'text':'y'*2000},'rules':'fixed rules',
                'host_contract':{'mandatory':True},'source_navigation':nav}
            result=context.select(payload,16384,2000)
            self.assertLessEqual(len(encoded(result)),16384*80//100-2000-512-context.ENVELOPE_RESERVE)
            self.assertEqual(result['objective'],payload['objective']);self.assertEqual(result['source_view'],payload['source_view'])
            self.assertEqual(result['host_contract'],payload['host_contract'])
            self.assertEqual(context.read(result['source_navigation']['reference']),nav)
    def test_symbols_and_literal_results_identify_non_header_pages(self):
        source='# header\n'*80+'def preserve_records():\n    return 17\n'
        chunks=pages(source,200)
        view=outline(source,chunks)
        self.assertEqual(view[0]['symbol'],'preserve_records');self.assertGreater(view[0]['page'],0)
        result=search({'module.py':source},'preserve_records',lambda t:pages(t,200))
        self.assertEqual(result['hits'][0]['page'],view[0]['page'])
        self.assertEqual(result['hits'][0]['line'],81)

    def test_bounded_literal_search_not_regex_or_external_files(self):
        result=search({'x.py':'literal .*\n'*20},'.*',lambda t:pages(t,100),3)
        self.assertEqual(result['total'],20);self.assertEqual(len(result['hits']),3);self.assertTrue(result['partial'])
        self.assertEqual(search({'x.py':'literal\n'},'.*',lambda t:pages(t,100))['total'],0)
        with self.assertRaises(ValueError):search({},'x'*121,lambda t:pages(t,100))
