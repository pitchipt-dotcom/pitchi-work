"""Opt-in v2 research handoff: Host snapshots, bounded IDs, paged detail.

Not a completion gate and not a replacement for an existing pinned v1 policy.
Worker category selections are ASSISTED interpretations, never proven facts.
"""
import hashlib
import json
import re
from pathlib import Path

from .phase_workflow import pages
from .runner import HostDefect
from .store import dump

CATEGORIES = ('finding', 'conditions', 'exceptions', 'prohibitions', 'uncertainty')
POLICY = {'schema_version': 2, 'max_evidence': 16, 'max_items_per_category': 8,
          'max_response_bytes': 1800, 'max_packet_bytes': 8192,
          'max_source_bytes': 65536, 'detail_page_bytes': 3000,
          'max_detail_bytes': 4096}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


class HandoffContractError(ValueError):
    failure_class = 'PHASE_CONTRACT_UNSATISFIED'


class HandoffOutputLimit(HandoffContractError):
    failure_class = 'PHASE_OUTPUT_LIMIT'


class ResearchReferenceScope:
    """Host-owned permissions, not model-declared imports or task acceptance.

    Discovery is not permission. Only a verified exact retrieval from this task's
    immutable catalog authorizes a supporting reference in a new research step.
    """
    def __init__(self, store, current, discoverable):
        self.store = store
        self.allowed = {r['evidence_id']: r for r in current}
        self.discoverable = {r['evidence_id']: r for r in discoverable}
        for ref in [*current, *discoverable]:
            store.verify(ref)

    def protocol(self):
        return {'version': 1, 'allowed_evidence_ids': list(self.allowed),
                'discovery_is_authorization': False,
                'retrieval_request': {'action': 'evidence', 'evidence_id': 'catalog ID', 'page': 0},
                'final_rule': 'Return exactly all allowed_evidence_ids in evidence_refs. Category IDs must be allowed. '
                              'A catalog/prior-handoff ID is not allowed until Host retrieves it. '
                              'Request a needed prior ID first; after Host retrieval the next input lists it as allowed.'}

    def authorize(self, evidence_id, view):
        ref = self.allowed.get(evidence_id) or self.discoverable.get(evidence_id)
        if ref is None:
            raise HandoffContractError('Evidence ID is not in this task catalog')
        proposal = {f: [] for f in CATEGORIES}
        proposal['evidence_refs'] = [evidence_id]
        packet = self.store.compile(proposal, [ref])
        detail = view.get('detail') if isinstance(view, dict) else None
        if not isinstance(detail, dict) or detail != self.store.detail(packet, evidence_id, detail.get('page')):
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: authorization requires exact verified retrieval')
        if evidence_id not in self.allowed and len(self.allowed) >= POLICY['max_evidence']:
            raise HandoffContractError('Authorized evidence cardinality limit; split phase')
        self.allowed[evidence_id] = ref

    def validate(self, value):
        if isinstance(value, dict):
            used = {v for f in (*CATEGORIES, 'evidence_refs')
                    for v in (value.get(f) if isinstance(value.get(f), list) else []) if isinstance(v, str)}
            rejected = sorted(used-set(self.allowed))
            if rejected:
                exc = HandoffContractError('Evidence outside current authorized scope; request retrieval before use')
                retrievable = [v for v in rejected if v in self.discoverable]
                example = {f: [] for f in CATEGORIES}
                example['evidence_refs'] = list(self.allowed)
                exc.detail = {'scope_violation': True, 'field': 'evidence_refs/category IDs',
                             'allowed_evidence_ids': list(self.allowed),
                             'rejected_evidence_ids': rejected,
                             'retrievable_rejected_ids': retrievable,
                             'required_action': 'Do not resend out-of-scope IDs. Request a known ID using retrieval, '
                                                'or return only authorized IDs; unknown IDs cannot be retrieved.',
                             'response_examples': {'final': example, 'retrieval':
                                 {'action': 'evidence', 'evidence_id': retrievable[0], 'page': 0} if retrievable else None}}
                raise exc
        # No schema, cardinality, retention, byte or classification rule relaxed.
        return self.store.compile(value, list(self.allowed.values()))


class HandoffStore:
    """One Host-only checkpoint namespace; no arbitrary filesystem resolver.

    Place root outside Worker write scope. Contents are immutable after creation.
    Hashes detect alteration relative to pinned receipts, not hostile Host writes.
    """
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: symlink root')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()

    def path(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(
                r'(?:evidence/E[0-9a-f]{24}|checkpoints/[A-Za-z0-9_-]{1,64}(?:\.receipt)?)\.json', reference):
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: unsafe reference')
        path = self.root / reference
        if any(p.is_symlink() for p in (path, path.parent)) or not path.resolve().is_relative_to(self.root):
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: reference escaped namespace')
        return path

    def immutable(self, reference, value):
        path = self.path(reference)
        if path.exists():
            if self.read(reference) != value:
                raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: immutable content changed')
        else:
            dump(path, value)
        return hash_bytes(path.read_bytes())

    def read(self, reference, maximum=100000):
        path = self.path(reference)
        try:
            if not path.is_file() or path.stat().st_size > maximum:
                raise ValueError('missing or oversized evidence')
            return json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: '+str(exc)) from exc

    def register(self, source_id, text, expected_source_sha256=None):
        """Freeze exact source once. No semantic extraction, shortening or rewriting."""
        if not isinstance(source_id, str) or not source_id or len(source_id.encode()) > 128:
            raise HostDefect('HANDOFF_INPUT_DEFECT: bounded source ID required')
        if not isinstance(text, str) or len(text.encode()) > POLICY['max_source_bytes']:
            raise HostDefect('HANDOFF_INPUT_DEFECT: source must be explicitly paged, not truncated')
        source_hash = hash_bytes(text.encode())
        if expected_source_sha256 is not None and source_hash != expected_source_sha256:
            raise HostDefect('HANDOFF_INPUT_DEFECT: source hash mismatch')
        record = {'schema_version': 2, 'source_id': source_id, 'source_sha256': source_hash, 'text': text}
        identifier = 'E'+hash_bytes(encoded(record))[:24]
        reference = 'evidence/'+identifier+'.json'
        file_hash = self.immutable(reference, record)
        return {'evidence_id': identifier, 'reference': reference, 'sha256': file_hash,
                'source_sha256': source_hash, 'source_id': source_id}

    def verify(self, ref):
        keys = {'evidence_id', 'reference', 'sha256', 'source_sha256', 'source_id'}
        if not isinstance(ref, dict) or set(ref) != keys or not isinstance(ref['evidence_id'], str):
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: reference schema')
        if ref['reference'] != 'evidence/'+ref['evidence_id']+'.json':
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: reference identity mismatch')
        record = self.read(ref['reference'], maximum=POLICY['max_source_bytes']*6+1024)
        if (hash_bytes(self.path(ref['reference']).read_bytes()) != ref['sha256'] or
                not isinstance(record, dict) or set(record) != {'schema_version', 'source_id', 'source_sha256', 'text'} or
                record['schema_version'] != 2 or not isinstance(record['text'], str) or
                not 0 <= len(record['text'].encode()) <= POLICY['max_source_bytes'] or
                hash_bytes(record['text'].encode()) != ref['source_sha256'] or
                record['source_sha256'] != ref['source_sha256'] or record['source_id'] != ref['source_id'] or
                'E'+hash_bytes(encoded(record))[:24] != ref['evidence_id']):
            raise HostDefect('HANDOFF_EVIDENCE_DEFECT: evidence/hash mismatch')
        return record

    def catalog(self, refs):
        if not isinstance(refs, list) or not 0 < len(refs) <= POLICY['max_evidence']:
            raise HostDefect('HANDOFF_INPUT_DEFECT: split catalog into bounded phases')
        result = {}
        for ref in refs:
            self.verify(ref)
            if ref['evidence_id'] in result:
                raise HostDefect('HANDOFF_INPUT_DEFECT: duplicate evidence ID')
            result[ref['evidence_id']] = ref
        return result

    def compile(self, proposal, refs):
        """Host validates finite selections; all input evidence must remain addressable."""
        catalog = self.catalog(refs)
        if not isinstance(proposal, dict) or set(proposal) != set(CATEGORIES) | {'evidence_refs'}:
            raise HandoffContractError('Exact category/evidence_refs schema required; no descriptions or quotes')
        if len(encoded(proposal)) > POLICY['max_response_bytes']:
            raise HandoffOutputLimit('Handoff response exceeds bounded size')
        for field in (*CATEGORIES, 'evidence_refs'):
            values = proposal[field]
            maximum = POLICY['max_evidence'] if field == 'evidence_refs' else POLICY['max_items_per_category']
            if (not isinstance(values, list) or len(values) > maximum or
                    any(not isinstance(v, str) or v not in catalog for v in values) or
                    len(set(values)) != len(values)):
                raise HandoffContractError('Use unique existing IDs within cardinality for '+field)
        if set(proposal['evidence_refs']) != set(catalog):
            raise HandoffContractError('Retain every input evidence ID, including unclassified evidence')
        classified = {v for field in CATEGORIES for v in proposal[field]}
        packet = {'schema_version': 2, 'policy_sha256': hash_bytes(encoded(POLICY)),
                  'interpretation': 'ASSISTED',
                  **{field: list(proposal[field]) for field in (*CATEGORIES, 'evidence_refs')},
                  'unclassified': sorted(set(catalog)-classified),
                  'catalog': [catalog[v] for v in sorted(catalog)]}
        if len(encoded(packet)) > POLICY['max_packet_bytes']:
            raise HostDefect('HANDOFF_CONTEXT_DEFECT: split phase, do not discard evidence')
        return packet

    def validate(self, packet):
        expected = {'schema_version', 'policy_sha256', 'interpretation', 'unclassified', 'catalog',
                    'evidence_refs', *CATEGORIES}
        if not isinstance(packet, dict) or set(packet) != expected:
            raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: packet schema')
        try:
            proposal = {f: packet[f] for f in (*CATEGORIES, 'evidence_refs')}
            rebuilt = self.compile(proposal, packet['catalog'])
        except HandoffContractError as exc:
            raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: '+str(exc)) from exc
        if packet != rebuilt:
            raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: policy/derived fields changed')
        return packet

    def save(self, name, proposal, refs):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: checkpoint name')
        packet = self.compile(proposal, refs)
        reference = 'checkpoints/'+name+'.json'
        receipt = 'checkpoints/'+name+'.receipt.json'
        # Reusing an existing checkpoint requires both its receipt and linked evidence.
        if self.path(reference).exists():
            if not self.path(receipt).exists():
                # A crash between packet and receipt may be completed only by an
                # explicit identical Host request, never by blindly trusting disk.
                if self.validate(self.read(reference)) != packet:
                    raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: interrupted packet changed')
                self.immutable(receipt, {'schema_version': 2,
                    'sha256': hash_bytes(self.path(reference).read_bytes()),
                    'authority': 'host', 'is_task_pass': False})
            if self.resume(name) != packet:
                raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: immutable packet changed')
            return packet
        file_hash = self.immutable(reference, packet)
        self.immutable(receipt, {'schema_version': 2, 'sha256': file_hash, 'authority': 'host', 'is_task_pass': False})
        return packet

    def resume(self, name):
        reference = 'checkpoints/'+name+'.json'
        receipt = self.read('checkpoints/'+name+'.receipt.json')
        packet = self.read(reference, maximum=POLICY['max_packet_bytes']*2)
        if (receipt != {'schema_version': 2, 'sha256': hash_bytes(self.path(reference).read_bytes()),
                       'authority': 'host', 'is_task_pass': False}):
            raise HostDefect('HANDOFF_CHECKPOINT_DEFECT: receipt mismatch')
        return self.validate(packet)

    def detail(self, packet, evidence_id, page=0, *, page_bytes=None):
        """A subsequent phase fetches one bounded exact page, not a model summary."""
        self.validate(packet)
        if type(page) is not int or page < 0 or evidence_id not in packet['evidence_refs']:
            raise HandoffContractError('Use an existing ID and nonnegative integer page')
        ref = next(r for r in packet['catalog'] if r['evidence_id'] == evidence_id)
        record = self.verify(ref)
        size=POLICY['detail_page_bytes'] if page_bytes is None else page_bytes
        if type(size) is not int or not 1<=size<=POLICY['detail_page_bytes']:
            raise HostDefect('HANDOFF_CONTEXT_DEFECT: invalid detail page reservation')
        chunks = pages(record['text'], size)
        if page >= len(chunks):
            raise HandoffContractError('Page outside evidence; no silent truncation')
        result = {'evidence_id': evidence_id, 'source_sha256': ref['source_sha256'],
                  'page': page, 'page_count': len(chunks), **chunks[page]}
        result['detail_sha256'] = hash_bytes(result['text'].encode())
        if size!=POLICY['detail_page_bytes']:result['page_bytes']=size
        if len(encoded(result)) > POLICY['max_detail_bytes']:
            raise HostDefect('HANDOFF_CONTEXT_DEFECT: detail exceeds page budget')
        return result

    def context(self, packet, evidence_id=None, page=0, *, context_tokens=16384, output_tokens=2000, page_bytes=None):
        """Bound the entire next-phase view; never accumulate previously fetched pages.

        Byte-count proxy reserves one token per UTF-8 byte plus chat overhead;
        actual adapter dispatch budgeting remains mandatory for a concrete model.
        """
        self.validate(packet)
        if type(context_tokens) is not int or type(output_tokens) is not int or min(context_tokens, output_tokens) <= 0:
            raise HostDefect('HANDOFF_CONTEXT_DEFECT: invalid model budgets')
        view = {'handoff': packet, 'detail': None}
        if evidence_id is not None:
            view['detail'] = self.detail(packet, evidence_id, page, page_bytes=page_bytes)
        estimate = len(encoded(view))+output_tokens+512
        if estimate > context_tokens*80//100:
            raise HostDefect('HANDOFF_CONTEXT_DEFECT: complete dispatch budget exceeded; split phase')
        return view

    def migrate_v1(self, name, legacy_path, expected_sha256):
        """Explicit read-only import, no category inference and no v1 acceptance override.

        The full legacy checkpoint (including long descriptions) becomes a Host
        snapshot. It is not rewritten into invented semantic findings.
        """
        legacy_path = Path(legacy_path)
        if legacy_path.is_symlink() or not legacy_path.is_file() or legacy_path.stat().st_size > POLICY['max_source_bytes']:
            raise HostDefect('HANDOFF_MIGRATION_DEFECT: unsafe/oversized legacy source')
        raw = legacy_path.read_bytes()
        if hash_bytes(raw) != expected_sha256:
            raise HostDefect('HANDOFF_MIGRATION_DEFECT: legacy hash mismatch')
        try:
            original = json.loads(raw)
            if not isinstance(original, dict) or not isinstance(original.get('observations'), list):
                raise ValueError('unknown legacy research schema')
            text = raw.decode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise HostDefect('HANDOFF_MIGRATION_DEFECT: '+str(exc)) from exc
        ref = self.register('legacy-checkpoint:'+expected_sha256, text, expected_sha256)
        proposal = {field: [] for field in CATEGORIES}
        proposal['evidence_refs'] = [ref['evidence_id']]
        return self.save(name, proposal, [ref])
