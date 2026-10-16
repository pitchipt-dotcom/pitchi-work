"""Host stagnation + private, revalidated memory. Advice is never acceptance.

No commands are executed here. Cloud consent remains per exact SDK payload.
Old runs without resolution_policy do not opt into this workflow.
"""
import fcntl
import hashlib
import json
import platform
from pathlib import Path
from .store import dump, load, now
from .runner import HostDefect
from .model import ModelFailure
from .cloud import CloudControlStop, cloud_advice, terminal_approval, fingerprint


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def private(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def read_verified(path):
    try: envelope = load(path)
    except (OSError, ValueError) as exc:
        raise HostDefect('RESOLUTION_MEMORY_DEFECT: unreadable envelope') from exc
    if not isinstance(envelope, dict) or digest(envelope.get('data')) != envelope.get('sha256'):
        raise HostDefect('RESOLUTION_MEMORY_DEFECT: hash mismatch')
    return envelope['data']


def write_verified(path, data):
    private(path.parent)
    dump(path, {'data': data, 'sha256': digest(data)})


def environment(contract):
    return {'profile': contract['profile'], 'system': platform.system(),
            'python': platform.python_version(), 'validators': contract.get('validators', []),
            'allowed_suffixes': sorted({Path(p).suffix for p in contract['allowed_paths']})}


def observed_problem(receipt):
    if receipt.get('authority') != 'host' or receipt.get('completion') is not False:
        raise HostDefect('RESOLUTION_RECEIPT_DEFECT: protected Host receipt required')
    rows = receipt['results']
    if len({r['test'] for r in rows}) != len(rows) or any(type(r['passed']) is not bool for r in rows):
        raise HostDefect('RESOLUTION_RECEIPT_DEFECT: invalid test identities')
    # Full diagnostics stay in receipts. Fingerprints include full diagnostics,
    # not just PASS counts or an LLM's declaration that nothing improved.
    failed = [{ 'test': r['test'], 'execution': r.get('execution', {})}
              for r in rows if not r['passed']]
    return {'failed_ids': sorted(r['test'] for r in failed), 'fingerprint': digest(failed)}


class CaseStore:
    def __init__(self, home):
        self.path = Path(home) / 'recovery_memory' / 'resolution_cases'

    def find(self, problem, env):
        if not self.path.exists(): return []
        matches = []
        for path in sorted(self.path.glob('*.json')):
            case = read_verified(path)
            if case['problem'] == problem and case['environment'] == env:
                matches.append(case)
        return sorted(matches, key=lambda c: (c['outcome'] == 'success', c['created_at']), reverse=True)

    def save(self, case):
        case = {**case, 'created_at': now(), 'authority': 'historical_observation_only',
                'reuse_conditions': 'Exact diagnostic/environment match; current Host validation and root acceptance remain mandatory.'}
        identifier = digest(case)
        write_verified(self.path / (identifier + '.json'), case)
        return identifier


def check_advice(value):
    # Finite suggestions; not formal contracts, executable commands or tests.
    if not isinstance(value, dict) or set(value) != {'can_help', 'suggestions', 'reason'}:
        raise CloudControlStop('CLOUD_ADVICE_INVALID', 'Advisor must return can_help, suggestions, reason')
    if type(value['can_help']) is not bool or not isinstance(value['reason'], str):
        raise CloudControlStop('CLOUD_ADVICE_INVALID', 'Invalid advisor types')
    suggestions = value['suggestions']
    if not isinstance(suggestions, list) or len(suggestions) > 3 or any(not isinstance(s, str) or len(s.encode()) > 1200 for s in suggestions) or len(value['reason'].encode()) > 600:
        raise CloudControlStop('CLOUD_ADVICE_INVALID', 'Advisor suggestion budget exceeded')
    if value['can_help'] and not suggestions:
        raise CloudControlStop('CLOUD_ADVICE_INVALID', 'No actionable suggestions returned')
    return value


class ResolutionCoordinator:
    """Replay-safe observer attached to existing finite edit/review operations."""
    def __init__(self, run, contract, base, *, approval=None, consult=None):
        self.run, self.contract = run, contract
        self.policy = run.state().get('resolution_policy', {})
        self.path = Path(base) / 'resolution.json'
        self.home = run.path.parent.parent
        self.cases = CaseStore(self.home)
        self.env = environment(contract)
        self.approval = approval or terminal_approval
        self.consult = consult or cloud_advice
        self.step = None
        self.data = read_verified(self.path) if self.path.exists() else {
            'steps': {}, 'reviews': [], 'same_error_count': 0, 'last_problem': None,
            'advice': None, 'events': [], 'edits': [], 'environment': self.env}

    def save(self):
        write_verified(self.path, self.data)
        self.run.save_state(resolution_summary={'reference':str(self.path.relative_to(self.run.path)),
            'sha256':digest(self.data), 'observed_trials':len(self.data['edits']),
            'review_count':len(self.data['reviews']), 'same_error_count':self.data['same_error_count'],
            'advice_origin':(self.data.get('advice') or {}).get('origin'),
            'completion_authority':'existing_host_root_gate'})

    def reviewed(self, step, decision):
        if not self.policy: return
        if step not in self.data['reviews']:
            self.data['reviews'].append(step)
            self.data['events'].append({'kind': 'review', 'step': step, 'decision': decision})
            self.save()

    def budget_path(self):
        identifier = self.run.state().get('advisor_budget_id', self.run.run_id)
        if not isinstance(identifier, str) or not identifier.replace('_', '').isalnum():
            raise HostDefect('RESOLUTION_BUDGET_DEFECT: invalid mission ledger')
        return self.home / 'recovery_memory' / 'advisor_budgets' / (identifier + '.json')

    def reserve(self, payload, sha256):
        if not self.approval(payload, sha256): return False
        if fingerprint(payload) != sha256:
            raise CloudControlStop('CLOUD_APPROVAL_MISMATCH', 'Payload changed during approval')
        path = self.budget_path(); private(path.parent)
        with path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = read_verified(path) if path.exists() else {'calls': []}
            # An extra call is an explicit new grant AND still requires SEND.
            limit = 1 + int(bool(self.policy.get('extra_call')))
            if len(ledger['calls']) >= limit:
                raise CloudControlStop('CLOUD_ADVISOR_CALL_LIMIT', 'Mission advisor budget exhausted; reapproval required, no provider fallback')
            ledger['calls'].append({'run_id': self.run.run_id, 'sha256': sha256,
                                    'provider': self.policy['cloud']['provider'], 'at': now()})
            write_verified(path, ledger)
        return True

    def observe(self, step, receipt, reference, edit, progress, source_view=None):
        if not self.policy or not progress: return None
        self.step = step
        key = str(step)
        if key in self.data['steps']: return self.data['steps'][key]
        target = (self.run.path / reference['reference']).resolve()
        if not target.is_relative_to(self.run.path.resolve()) or hashlib.sha256(target.read_bytes()).hexdigest() != reference['sha256'] or load(target) != receipt:
            raise HostDefect('RESOLUTION_RECEIPT_DEFECT: invalid receipt reference/hash')
        problem = observed_problem(receipt)
        same = self.data['last_problem'] == problem
        self.data['same_error_count'] = self.data['same_error_count'] + 1 if same else 1
        self.data['last_problem'] = problem
        self.data['edits'].append({'step': step, 'signature': digest(edit), 'reference': reference,
                                  'change':{k:v for k,v in edit.items() if k in ('action','path','old_text','new_text','start_line','end_line','replacement_lines','replacement','summary')},
                                  'operation_reference': str((self.path.parent/('edit_'+key+'.json')).relative_to(self.run.path))})
        self.data['events'].append({'kind': 'trial', 'step': step, 'problem': problem,
                                   'comparison': progress.comparison, 'review': progress.packet()})
        advice = self.data['advice']
        if advice and (not progress.comparison.get('improved_ids') or progress.comparison.get('regressed_ids')):
            advice['non_improving_after'] += 1
            if advice['non_improving_after'] >= 4:
                if advice['origin']=='memory' and self.policy.get('cloud'):
                    # An old success is not truth in today's environment.
                    # Keep its failed trial before considering approved cloud advice.
                    self.data.setdefault('prior_advice',[]).append(advice)
                    self.data['memory_failed']=True
                    self.data['advice']=None;advice=None
                    self.save()
                else:
                    self.save()
                    raise CloudControlStop('RESOLUTION_NO_PROGRESS', 'Revalidated advice did not improve four Host trials; stop wasted retries')
        elif advice and not progress.comparison.get('regressed_ids'):
            advice['non_improving_after'] = 0
        stalled = bool(problem['failed_ids'] and (self.data['same_error_count'] >= 3 or
                       progress.streak >= 3 or progress.comparison.get('repeated_edit')))
        view = None
        if stalled and not advice:
            found = self.cases.find(problem, self.env) if self.policy.get('memory', True) and not self.data.get('memory_failed') else []
            failed_advice={digest(c['advice']) for c in found if c['outcome']=='failure'}
            successful = next((c for c in found if c['outcome'] == 'success' and digest(c['advice']) not in failed_advice), None)
            if successful:
                view = self.adopt(successful['advice'], 'memory', problem)
            elif found:
                self.save()
                raise CloudControlStop('RESOLUTION_KNOWN_FAILURE', 'Matching failed attempt exists; human review required before repeating it')
            elif self.policy.get('cloud') and self.data['reviews']:
                packet = {'goal': load(self.run.path/'root_goal.json')['request'],
                    'failure_class': 'HOST_OBSERVED_STAGNATION', 'stopped_phase': 'edit',
                    'evidence_refs': [reference], 'environment': self.env,
                    'errors': [{'test': r['test'], 'execution': r.get('execution', {})}
                               for r in receipt['results'] if not r['passed']],
                    'attempted_fixes': self.data['edits'][-2:],
                    'validation_conditions': {'contract_reference': self.contract['task_id'],
                                             'contract_sha256':self.contract.get('sha256'),
                                             'validators': self.contract.get('validators',[]),
                                             'required_evidence':self.contract.get('required_evidence',[]),
                                             'failed_test_ids': problem['failed_ids']},
                    'related_code': source_view,
                    'response_contract': {'can_help': 'boolean', 'suggestions': '0..3 short strings, each <=1200 UTF-8 bytes', 'reason': '<=600 bytes'}}
                # No truncation to sneak past budgets: overlarge input stops for
                # human selection. Approval shows the EXACT outgoing content.
                self.save()
                try:
                    response = self.consult(self.run, self.policy['cloud'], packet,
                                            enabled=True, approval=self.reserve)
                except ModelFailure as exc:
                    # An API outage is not a request to repair Worker artifacts.
                    raise CloudControlStop(exc.failure_class, 'Cloud advice unavailable; no provider fallback or automatic retry') from exc
                value = check_advice(response['advice'])
                if not value['can_help']:
                    self.data['advice'] = {'value': value, 'origin': 'cloud', 'problem': problem, 'non_improving_after': 0,'issued_after_step':step}
                    self.save()
                    raise CloudControlStop('CLOUD_ADVISOR_NO_SOLUTION', 'Advisor reports no useful next step; stop consultations')
                view = self.adopt(value, 'cloud', problem)
        self.data['steps'][key] = view
        self.save()
        return view

    def adopt(self, value, origin, problem):
        value = check_advice(value)
        self.data['advice'] = {'value': value, 'origin': origin, 'problem': problem, 'non_improving_after': 0,'issued_after_step':self.step}
        return {'kind': 'resolution_advice', 'authority': 'suggestion_only',
                'origin': origin, 'advice': value,
                'rules': 'Propose edits using current sources. Historical/cloud advice is NOT PASS. Original contract, tests and finite operations are unchanged.'}

    def advice_view(self):
        history=[*self.data.get('prior_advice',[]), self.data.get('advice')]
        # Resume must not inject later advice into an earlier pinned call.
        available=[a for a in history if a and (self.step is None or a['issued_after_step'] < self.step)]
        advice=available[-1] if available else None
        if not advice: return None
        return {'kind':'resolution_advice','authority':'suggestion_only',
                'origin':advice['origin'],'advice':advice['value'],
                'rules':'Use current sources and original acceptance; advice is not PASS.'}


def finalize_resolution(run):
    """Only the existing root gate can promote a provisional case to success."""
    state = run.state()
    if state.get('status') not in run.TERMINAL: return
    completed = state.get('status') == 'COMPLETED'
    gate = load(run.path/'artifacts/request_completion_gate.json', {})
    if completed and (gate.get('status') != 'COMPLETED' or not gate.get('host_evidence_hashes_verified')):
        raise HostDefect('RESOLUTION_ACCEPTANCE_DEFECT: root receipt missing')
    roots=load(run.path/'artifacts/root_evidence.json',[])
    for receipt in roots if completed else []:
        path=(run.path/receipt['reference']).resolve()
        if not path.is_relative_to(run.path.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=receipt['sha256'] or not receipt['passed']:
            raise HostDefect('RESOLUTION_ACCEPTANCE_DEFECT: root evidence changed')
    for path in (run.path/'checkpoints').glob('*/*/resolution.json'):
        data = read_verified(path)
        advice = data.get('advice')
        if not advice or data.get('saved_case'): continue
        receipts = [load(p) for p in (run.path/'artifacts').glob('role_call_*_cloud_advisor/receipt.json')]
        case = {'problem': advice['problem'], 'environment': data['environment'],
                'outcome': 'success' if completed else 'failure', 'advice': advice['value'],
                'source_run_id': run.run_id, 'used_ai':{'worker':state.get('local_model'), 'advisor':advice['origin']},
                'failed_and_effective_trials': data['events'], 'worker_edits': data['edits'],
                'failed_historical_advice':data.get('prior_advice',[]),
                'validation': {'root_gate_sha256': digest(gate), 'status': state['status'],
                    'host_receipts':[{k:r[k] for k in ('reference','sha256','passed','artifact_hashes') if k in r} for r in roots]},
                'api_usage': [{k: r.get(k) for k in ('provider', 'model', 'usage', 'estimated_cost_usd', 'success')} for r in receipts]}
        data['saved_case'] = CaseStore(run.path.parent.parent).save(case)
        write_verified(path, data)
