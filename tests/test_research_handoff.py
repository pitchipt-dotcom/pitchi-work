"""Generic reference handoff only. No live calls, subject solutions or missions."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pitchi_work.research_handoff import (
    CATEGORIES, POLICY, HandoffStore, HandoffContractError, HandoffOutputLimit, encoded, hash_bytes,
)
from pitchi_work.runner import HostDefect
from pitchi_work.store import dump


class ResearchHandoff(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = HandoffStore(self.root/'host-checkpoint')
        # Whole original clauses. Host keeps them, Worker selects IDs, not quotes.
        self.originals = [
            '貸出期間は三時間。延長は期限前に申請し、次の予約がない場合だけ一回可能。',
            '例外として保守日は利用不可。承認なしの建物外持出しは禁止。',
            '窓口の休日対応は未確認。確認前に利用可能と断定してはいけない。',
        ]
        self.refs = [self.store.register('source-'+str(i), text) for i, text in enumerate(self.originals)]
        ids = [r['evidence_id'] for r in self.refs]
        self.proposal = {'finding': [ids[0]], 'conditions': [ids[0]], 'exceptions': [ids[1]],
                         'prohibitions': [ids[1]], 'uncertainty': [ids[2]], 'evidence_refs': ids}

    def packet(self):
        return self.store.save('research_0', self.proposal, self.refs)

    def test_next_phase_preserves_numbers_conditions_exceptions_prohibitions(self):
        packet = self.packet()
        for ref, original in zip(self.refs, self.originals):
            view = self.store.context(packet, ref['evidence_id'])
            self.assertEqual(view['detail']['text'], original)
            self.assertEqual(view['detail']['detail_sha256'], hash_bytes(original.encode()))
        self.assertEqual(packet['conditions'], packet['finding'])
        self.assertEqual(packet['interpretation'], 'ASSISTED')

    def test_no_evidence_body_or_free_description_in_worker_or_packet(self):
        for text in self.originals:
            self.assertNotIn(text, encoded(self.proposal).decode())
            self.assertNotIn(text, encoded(self.packet()).decode())
        bad = copy.deepcopy(self.proposal)
        bad['description'] = 'Invented prose'
        with self.assertRaises(HandoffContractError):
            self.store.compile(bad, self.refs)

    def test_unclassified_evidence_is_not_silently_dropped(self):
        proposal = {key: [] for key in CATEGORIES}
        proposal['evidence_refs'] = self.proposal['evidence_refs']
        packet = self.store.compile(proposal, self.refs)
        self.assertEqual(set(packet['unclassified']), set(proposal['evidence_refs']))
        for ref, original in zip(self.refs, self.originals):
            self.assertEqual(self.store.detail(packet, ref['evidence_id'])['text'], original)

    def test_missing_evidence_coverage_is_rejected(self):
        bad = copy.deepcopy(self.proposal)
        bad['evidence_refs'].pop()
        with self.assertRaises(HandoffContractError):
            self.store.compile(bad, self.refs)

    def test_unknown_reference_is_rejected(self):
        bad = copy.deepcopy(self.proposal)
        bad['finding'] = ['E'+'0'*24]
        with self.assertRaises(HandoffContractError):
            self.store.compile(bad, self.refs)

    def test_category_count_duplicate_and_type_are_host_checked(self):
        for key, bad_value in [('finding', 'not-a-list'), ('finding', [True]),
                               ('conditions', [self.refs[0]['evidence_id']]*2)]:
            bad = copy.deepcopy(self.proposal)
            bad[key] = bad_value
            with self.assertRaises(HandoffContractError):
                self.store.compile(bad, self.refs)
        refs = [self.store.register('count-'+str(i), 'Distinct record '+str(i)) for i in range(9)]
        bad = {key: [] for key in CATEGORIES}
        bad['finding'] = [r['evidence_id'] for r in refs]
        bad['evidence_refs'] = list(bad['finding'])
        with self.assertRaises(HandoffContractError):
            self.store.compile(bad, refs)

    def test_response_size_has_real_output_limit_failure(self):
        bad = copy.deepcopy(self.proposal)
        bad['finding'] = ['x'*2000]
        with self.assertRaises(HandoffOutputLimit):
            self.store.compile(bad, self.refs)

    def test_bounded_catalog_requires_split_instead_of_dropping_records(self):
        refs = [self.store.register('entry-'+str(i), 'Record '+str(i)) for i in range(17)]
        with self.assertRaises(HostDefect):
            self.store.catalog(refs)

    def test_missing_and_mutated_evidence_are_host_defects(self):
        packet = self.packet()
        target = self.store.path(self.refs[0]['reference'])
        original = target.read_bytes()
        dump(target, {'text': 'tampered'})
        with self.assertRaises(HostDefect):
            self.store.detail(packet, self.refs[0]['evidence_id'])
        target.write_bytes(original)
        target.unlink()
        with self.assertRaises(HostDefect):
            self.store.resume('research_0')

    def test_reference_identity_hash_and_traversal_are_rejected(self):
        for key, value in [('reference', '../private.json'), ('reference', '/private.json'),
                           ('evidence_id', self.refs[1]['evidence_id']), ('sha256', '0'*64),
                           ('source_sha256', '0'*64)]:
            bad = dict(self.refs[0], **{key: value})
            with self.assertRaises(HostDefect):
                self.store.verify(bad)

    def test_symlink_cannot_resolve_outside_host_namespace(self):
        victim = self.store.path(self.refs[0]['reference'])
        external = self.root/'outside.json'
        external.write_bytes(victim.read_bytes())
        victim.unlink()
        victim.symlink_to(external)
        with self.assertRaises(HostDefect):
            self.store.verify(self.refs[0])

    def test_resume_pins_same_exact_evidence_and_packet(self):
        packet = self.packet()
        before = {p: p.read_bytes() for p in self.store.root.rglob('*.json')}
        resumed = HandoffStore(self.store.root).resume('research_0')
        self.assertEqual(packet, resumed)
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.root.rglob('*.json')})
        self.assertEqual(self.store.detail(resumed, self.refs[0]['evidence_id'])['text'], self.originals[0])

    def test_completed_checkpoint_cannot_be_replaced(self):
        self.packet()
        changed = copy.deepcopy(self.proposal)
        changed['uncertainty'] = []
        with self.assertRaises(HostDefect):
            self.store.save('research_0', changed, self.refs)

    def test_interrupted_publication_can_only_finish_with_identical_verified_request(self):
        real_dump = dump
        def interrupted(path, value):
            if path.name.endswith('.receipt.json'):
                raise OSError('synthetic interruption')
            return real_dump(path, value)
        with patch('pitchi_work.research_handoff.dump', interrupted):
            with self.assertRaises(OSError):
                self.packet()
        with self.assertRaises(HostDefect):
            self.store.resume('research_0')
        changed = copy.deepcopy(self.proposal)
        changed['finding'] = []
        with self.assertRaises(HostDefect):
            self.store.save('research_0', changed, self.refs)
        packet = HandoffStore(self.store.root).save('research_0', self.proposal, self.refs)
        self.assertEqual(packet, self.store.resume('research_0'))

    def test_preserved_control_characters_and_exact_source_hash(self):
        text = 'Original\x00\tvalue\n'*600
        ref = self.store.register('escaped-source', text, hash_bytes(text.encode()))
        self.assertEqual(self.store.verify(ref)['text'], text)
        with self.assertRaises(HostDefect):
            self.store.register('wrong-source', text, '0'*64)

    def test_tampered_checkpoint_and_policy_are_rejected(self):
        self.packet()
        path = self.store.path('checkpoints/research_0.json')
        packet = self.store.resume('research_0')
        packet['policy_sha256'] = '0'*64
        dump(path, packet)
        with self.assertRaises(HostDefect):
            self.store.resume('research_0')
        with self.assertRaises(HostDefect):
            self.store.validate(packet)

    def test_long_original_is_retained_with_bounded_exact_pages(self):
        original = '保存対象の原文。条件と例外は参照先に残す。'*700
        ref = self.store.register('long-source', original)
        proposal = {f: [] for f in CATEGORIES}
        proposal['evidence_refs'] = [ref['evidence_id']]
        packet = self.store.save('long', proposal, [ref])
        first = self.store.detail(packet, ref['evidence_id'])
        details = [self.store.detail(packet, ref['evidence_id'], n) for n in range(first['page_count'])]
        self.assertEqual(''.join(d['text'] for d in details), original)
        self.assertTrue(all(len(encoded(d)) <= POLICY['max_detail_bytes'] for d in details))
        self.assertLess(len(encoded(packet)), 8192)
        for n in range(first['page_count']):
            self.assertEqual(set(self.store.context(packet, ref['evidence_id'], n)), {'handoff', 'detail'})

    def test_invalid_pages_and_small_context_are_rejected(self):
        packet = self.packet()
        for page in [-1, True, '0', 9999]:
            with self.assertRaises(HandoffContractError):
                self.store.detail(packet, self.refs[0]['evidence_id'], page)
        with self.assertRaises(HostDefect):
            self.store.context(packet, context_tokens=1024)

    def test_maximum_catalog_and_categories_still_fit_dispatch(self):
        refs = [self.store.register('source-'+str(i), 'Observation '+str(i)) for i in range(16)]
        ids = [r['evidence_id'] for r in refs]
        proposal = {f: ids[:8] for f in CATEGORIES}
        proposal['evidence_refs'] = ids
        self.assertLessEqual(len(encoded(proposal)), POLICY['max_response_bytes'])
        packet = self.store.compile(proposal, refs)
        self.assertLessEqual(len(encoded(packet)), POLICY['max_packet_bytes'])
        self.assertEqual(self.store.context(packet)['handoff'], packet)

    def test_v1_import_is_explicit_read_only_and_does_not_infer_categories(self):
        old = self.root/'legacy.json'
        dump(old, {'observations': [{'line': 1, 'quote': self.originals[0],
                                   'explanation': 'Long legacy commentary. '*30}]})
        before = old.read_bytes()
        packet = self.store.migrate_v1('imported', old, hash_bytes(before))
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(packet['unclassified'], packet['evidence_refs'])
        self.assertTrue(all(packet[f] == [] for f in CATEGORIES))
        self.assertEqual(self.store.detail(packet, packet['evidence_refs'][0])['text'], before.decode())
        self.assertFalse(json.loads(self.store.path('checkpoints/imported.receipt.json').read_text())['is_task_pass'])

    def test_migration_wrong_hash_or_unknown_schema_stops(self):
        old = self.root/'legacy.json'
        dump(old, {'observations': []})
        with self.assertRaises(HostDefect):
            self.store.migrate_v1('bad', old, '0'*64)
        dump(old, {'unexpected': 'shape'})
        with self.assertRaises(HostDefect):
            self.store.migrate_v1('bad', old, hash_bytes(old.read_bytes()))

    def test_same_snapshot_deduplicates_and_conflicting_snapshot_stays_separate(self):
        same = self.store.register('source-0', self.originals[0])
        self.assertEqual(same, self.refs[0])
        other = self.store.register('source-0', 'A different source revision')
        self.assertNotEqual(other['evidence_id'], same['evidence_id'])
        self.assertEqual(self.store.verify(same)['text'], self.originals[0])


if __name__ == '__main__':
    unittest.main()
