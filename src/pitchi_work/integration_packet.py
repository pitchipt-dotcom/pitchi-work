"""V1.5 Integrator view: immutable Host evidence by reference, never by body.

This module does not run validators or decide completion. The original evidence
continues to be the input to the Host completion gate.
"""
import copy
import hashlib
import json
from pathlib import Path

CONTEXT_TOKENS = 32768
BUDGET_TOKENS = CONTEXT_TOKENS * 80 // 100
CHAT_OVERHEAD_RESERVE = 512


class IntegratorContextDefect(ValueError):
    failure_class = 'INTEGRATOR_CONTEXT_DEFECT'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_packet(run, evidence):
    """Project only current facts; retain hashed links to unabridged sources."""
    from .store import load
    from .host_contracts import TEMPLATES
    workspace = Path(evidence['execution_context']['active_workspace']).resolve()
    references = {}
    reference_ids = {}
    issues = []

    def reference(name):
        path = (run.path / name).resolve()
        if not path.is_relative_to(run.path.resolve()) or not path.is_file():
            raise IntegratorContextDefect('Missing saved integration evidence: ' + name)
        if name not in reference_ids:
            identifier = 'E' + str(len(references) + 1)
            reference_ids[name] = identifier
            references[identifier] = {'artifact': name, 'sha256': digest(path)}
        return reference_ids[name]

    root_ref = reference('root_goal.json')
    artifacts = {}
    for name in evidence['artifacts']:
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            raise IntegratorContextDefect('Missing integration artifact: ' + name)
        artifacts[name] = {'sha256': digest(path)}
    tasks = []
    for item in evidence['tasks']:
        task = item['task']; tid = task['task_id']
        record_path = 'tasks/' + tid + '.json'
        record_ref = reference(record_path)
        record = load(run.path / record_path)
        review_candidates = []
        for path in sorted((run.path / 'reviews').glob(tid + '_*.json')):
            review = load(path)
            assessment = review.get('assessment', review)
            review_candidates.append((path, assessment))
        # Only the current Reviewer verdict is inline; all history remains saved.
        if review_candidates:
            path, review = review_candidates[-1]
            reviewer = {'verdict': review.get('verdict'),
                        'evidence_ref': reference(path.relative_to(run.path).as_posix())}
        else:
            reviewer = {'verdict': None, 'evidence_ref': None}
        if reviewer['verdict'] != 'PASS':
            issues.append({'task_id': tid, 'kind': 'reviewer_not_pass', **reviewer})
        if record.get('status') != 'PASS':
            issues.append({'task_id': tid, 'kind': 'task_not_pass', 'status': record.get('status')})
        for issue in record.get('attempts', [{}])[-1:] or [{}]:
            if issue.get('unresolved_issues'):
                issues.append({'task_id': tid, 'kind': 'unresolved_worker_issues',
                               'count': len(issue['unresolved_issues']), 'evidence_ref': record_ref})
        tasks.append({'task_id': tid, 'task_final_state': record.get('status'),
                      'requirement_ids': task['requirement_ids'],
                      'validator_ids': TEMPLATES[task['template_id']]['validators'],
                      'contract_sha256': task['host_contract_sha256'],
                      'record_ref': record_ref, 'reviewer': reviewer,
                      'outputs': task['expected_outputs']})
    tests = []
    for index, test in enumerate(evidence['final_tests'], 1):
        registry = test.get('registry_result', {})
        refs = sorted({p['evidence_artifact'] for p in test.get('host_execution_evidence', [])})
        # Negative receipts also remain visible even when typed success evidence
        # is absent. Resolve collection_id from the saved Host receipt, not prose.
        if not refs and registry:
            refs = [p.relative_to(run.path).as_posix() for p in
                    (run.path / 'artifacts').glob('host_evidence_*.json')
                    if load(p, {}).get('collection_id') == registry.get('collection_id')]
        if not refs:
            raise IntegratorContextDefect('Final Host test has no saved receipt: F' + str(index))
        refs = [reference(ref) for ref in refs]
        checks = []
        for check in registry.get('checks', []):
            row = {'validator_id': check['validator'], 'pass': check.get('pass') is True,
                   'evidence_ref_ids': refs}
            # Counts/flags describe the measured result, never expose OCR bodies,
            # generated answers, commands, or Worker-authored test contents.
            details = check.get('details', {})
            for key in ('insufficient', 'grounding_verified', 'worker_test_formal_acceptance'):
                if key in details: row[key] = details[key]
            if 'citations' in details: row['citation_count'] = len(details['citations'])
            if 'answer' in details: row['answer_nonempty'] = bool(details['answer'])
            if row not in checks: checks.append(row)
            if not row['pass']:
                issues.append({'kind': 'validator_failed', 'task_id': test.get('task_id'),
                               'test_id': 'F' + str(index), **row})
        calls = []
        for call in registry.get('live_calls', []):
            saved = {k: call[k] for k in ('model', 'http_status', 'finish_reason') if k in call}
            for key in ('request_artifact', 'context_artifact', 'response_artifact'):
                if key in call: saved[key] = reference(call[key])
            calls.append(saved)
        tests.append({'test_id': 'F' + str(index), 'command': test['command'],
                      'task_id': test.get('task_id'), 'exit_code': test.get('exit_code'),
                      'passed': registry.get('passed') is True, 'mode': registry.get('mode'),
                      'proof_kinds': registry.get('proof_kinds', []),
                      'checks': checks, 'evidence_ref_ids': refs, 'live_calls': calls})
        if test.get('exit_code') != 0:
            issues.append({'kind': 'final_test_failed', 'test_id': 'F' + str(index),
                           'evidence_ref_ids': refs})
    for name, item in evidence.get('input_artifacts', {}).items():
        if item.get('unchanged_from_parent') is False:
            issues.append({'kind': 'protected_input_changed', 'artifact': name})
    bindings = [{'requirement_id': o['id'], 'root_obligation_id': o['id'],
                 'task_ids': [t['task_id'] for t in tasks
                              if o['id'] == 'R0' or o['id'] in t['requirement_ids']]}
                for o in evidence['obligations']]
    for binding in bindings:
        if not binding['task_ids']:
            issues.append({'kind': 'requirement_unmapped', 'requirement_id': binding['requirement_id']})
    return {'packet_version': 1, 'root_goal_ref': root_ref,
            'original_request': evidence['root_goal']['request'],
            'obligations': evidence['obligations'], 'requirement_bindings': bindings,
            'tasks': tasks, 'final_tests': tests, 'artifacts': artifacts,
            'input_artifacts': evidence.get('input_artifacts', {}),
            'unresolved_issues': issues, 'evidence_references': references,
            'evidence_authority': 'Host-owned validators; Worker tests are NOT formal acceptance',
            'completion_authority': 'Original immutable root obligations and Host completion gate; task PASS alone is insufficient'}


def compact(packet):
    """Deduplicate repeated test/receipt metadata without deleting any bindings."""
    result = copy.deepcopy(packet)
    if any(o['id'] == 'R0' for o in result['obligations']):
        # R0 already contains the verbatim original request, not a summary.
        result.pop('original_request', None)
        result['original_request_obligation_id'] = 'R0'
    if 'host_receipts' in result:
        return result
    receipts = {}
    nested_call_refs = set()
    for test in result['final_tests']:
        key = tuple(test['evidence_ref_ids'])
        identifier = '|'.join(key)
        data = {k: test.pop(k) for k in ('checks', 'live_calls', 'proof_kinds', 'mode', 'passed')}
        for check in data['checks']:
            # The parent receipt already binds every check to these exact IDs.
            if check['evidence_ref_ids'] == list(key):check.pop('evidence_ref_ids')
        for call in data['live_calls']:
            for field in ('request_artifact', 'context_artifact', 'response_artifact'):
                if field in call:nested_call_refs.add(call.pop(field))
            # The hash-verified Host receipt contains all nested paths/hashes.
            call['details_ref_ids'] = list(key)
        if identifier in receipts and receipts[identifier] != data:
            raise IntegratorContextDefect('Inconsistent duplicate Host evidence')
        receipts[identifier] = data
        test['receipt_ref'] = identifier
    result['host_receipts'] = receipts
    for identifier in nested_call_refs:
        result['evidence_references'].pop(identifier, None)
    return result


def message_budget(messages, max_output_tokens):
    # UTF-8 bytes conservatively upper-bound byte-BPE tokens used by Qwen.
    # Include a separate chat-template reserve and the full output allocation.
    size = sum(len(m['content'].encode('utf-8')) for m in messages)
    upper = size + CHAT_OVERHEAD_RESERVE + max_output_tokens
    return {'prompt_bytes': size, 'input_token_upper_bound': size + CHAT_OVERHEAD_RESERVE,
            'reserved_output_tokens': max_output_tokens, 'context_token_upper_bound': upper,
            'context_limit_tokens': CONTEXT_TOKENS, 'budget_tokens': BUDGET_TOKENS,
            'within_budget': upper <= BUDGET_TOKENS, 'method': 'conservative UTF-8 byte-BPE upper bound'}


def fit_messages(messages, max_output_tokens):
    """Guard the actual envelope, including transport/re-generation suffixes."""
    stats = message_budget(messages, max_output_tokens)
    stats['dispatch_compacted'] = False
    if not stats['within_budget']:
        for message in messages:
            if message.get('role') != 'user' or 'Evidence: ' not in message['content']:continue
            prefix, content = message['content'].split('Evidence: ', 1)
            packet, end = json.JSONDecoder().raw_decode(content)
            if packet.get('packet_version') != 1:continue
            message['content'] = prefix + 'Evidence: ' + json.dumps(compact(packet), ensure_ascii=False, separators=(',', ':')) + content[end:]
        stats = {**message_budget(messages, max_output_tokens), 'dispatch_compacted': True}
    return stats


def fit_payload(prefix, packet):
    # Reserve the existing 2,600 output cap and execution-context suffix. The
    # actual API messages are checked again after all suffixes/retry feedback.
    for level, candidate in enumerate((packet, compact(packet))):
        prompt = prefix + json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))
        stats = message_budget([{'content': prompt}], 2600 + 1536)
        stats['compaction_level'] = level
        if stats['within_budget']: return candidate, prompt, stats
    raise IntegratorContextDefect('Integration packet exceeds 80% context budget after deterministic compaction')
