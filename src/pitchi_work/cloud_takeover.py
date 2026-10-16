"""Mock-only atomic edit substitution; existing PhaseWorker is the authority.

No provider SDK, external sending, generated commands or root decision here.
Private records retain raw output. Test PASS is a stage receipt, never completion.
"""
import copy
import hashlib
import json
import time
from datetime import datetime

from .model import ModelFailure
from .runner import HostDefect
from .store import Cancelled, dump, load, now
from .mission_profiles import sha


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class MockCloudWorker:
    """Synthetic response factory, not a real provider or SDK adapter."""
    provider='mock'
    model='mock-cloud-edit-v1'

    def __init__(self, respond):
        self.respond=respond
        self.calls=0

    def generate(self, packet):
        self.calls+=1
        return copy.deepcopy(self.respond(copy.deepcopy(packet)))


class MockEditTakeover:
    """One reserved mock operation per run; no automatic real-cloud fallback."""
    def __init__(self, worker):
        if type(worker) is not MockCloudWorker:
            raise ValueError('Only MockCloudWorker is supported; live takeover is not enabled')
        self.worker=worker

    def _folder(self, run):
        folder=run.path/'private_takeovers'
        if folder.is_symlink():raise HostDefect('TAKEOVER_STORE_DEFECT: unsafe store')
        folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        if folder.stat().st_mode & 0o077:
            raise HostDefect('TAKEOVER_STORE_DEFECT: private store permissions required')
        return folder

    def propose(self, phase, name, payload, check):
        run=phase.run;run.raise_if_cancelled()
        folder=self._folder(run)
        # Exclusive reservation survives interruption/resume, even before output.
        try:
            with (folder/'reservation.json').open('x') as handle:
                json.dump({'schema_version':1,'phase':name,'at':now(),'mock':True},handle)
        except FileExistsError:
            raise ModelFailure('TAKEOVER_LIMIT','One mock takeover already reserved; no retry or reset')
        started=time.monotonic()
        identifier=phase.contract['task_id']+'_'+name
        record_path=folder/(identifier+'.json')
        calls=phase.base/'calls'/name
        failures=[{'reference':str(p.relative_to(run.path)),'sha256':sha(p),
                   'failure_class':load(p).get('failure_class')} for p in sorted(calls.glob('*.json'))]
        if len(failures)!=phase.policy['call_attempts'] or any(load(run.path/f['reference']).get('status')!='FAILED' for f in failures):
            raise HostDefect('TAKEOVER_CONDITION_DEFECT: finite local failure evidence required')
        contract_path=folder/(identifier+'.contract.json');dump(contract_path,phase.contract)
        # Do not duplicate the full navigation/history envelope or schemas for
        # operations the takeover is not permitted to perform. Formal conditions
        # stay present, and full provenance stays in the private Host store.
        scoped_input={k:payload[k] for k in ('task_goal','hypothesis','host_contract','source_view') if k in payload}
        scoped_input['sources']=[s for s in payload['sources'] if s['write_allowed']]
        packet={'schema_version':1,'operation':'atomic_edit_takeover','phase':name,
                'contract_reference':{'reference':str(contract_path.relative_to(run.path)),'sha256':sha(contract_path)},
                'input':scoped_input,'edit_schema':{'oneOf':[s for s in phase.edit_schema['oneOf']
                    if s['properties']['action']['enum'][0] in {'patch','replace','replace_target'}]},
                'failures':failures,'last_failure':load(run.path/failures[-1]['reference']).get('error'),
                'authority':'Host validates; return one patch or replace only'}
        if len(json.dumps(packet,ensure_ascii=False,separators=(',',':')).encode())+512+phase.policy['max_output_tokens']>phase.policy['context_tokens']*80//100:
            raise HostDefect('PHASE_CONTEXT_DEFECT: takeover packet exceeds unchanged 80% guard')
        packet_path=folder/(identifier+'.input.json');dump(packet_path,packet)
        record={'schema_version':1,'mission_id':run.state().get('mission_id',run.run_id),
                'run_id':run.run_id,'takeover_id':identifier,'target_phase':name,
                'task_id':phase.contract['task_id'],'original_worker':run.state().get('model','local'),
                'mock':True,'provider':self.worker.provider,'model':self.worker.model,
                'host_condition':'finite_local_edit_failure','failure_evidence':failures,
                'input':{'reference':str(packet_path.relative_to(run.path)),'sha256':sha(packet_path)},
                'contract_sha256':phase.contract['sha256'],'status':'RESERVED',
                'usage':{'input_tokens':None,'output_tokens':None,'mock_calls':1,'api_calls':0,
                         'actual_cost_usd':0,'estimated_cost_usd':None},'at':now()}
        dump(record_path,record)
        try:
            from .staged_validation import observe_staged
            before=observe_staged(run,phase.contract,phase.takeover_sources,folder/(identifier+'.before'))
            record['before']={'reference':str((folder/(identifier+'.before/receipt.json')).relative_to(run.path)),
                              'sha256':sha(folder/(identifier+'.before/receipt.json'))}
            raw=self.worker.generate(packet)
            run.raise_if_cancelled()
            output_path=folder/(identifier+'.output.json');dump(output_path,raw)
            record['output']={'reference':str(output_path.relative_to(run.path)),'sha256':sha(output_path)}
            if not isinstance(raw,dict) or raw.get('action') not in {'patch','replace','replace_target'}:
                raise ValueError('Takeover must return one existing edit operation, not commands/read/done')
            if len(json.dumps(raw,ensure_ascii=False).encode())>phase.policy['max_response_bytes']:
                raise ValueError('Takeover response exceeds unchanged edit response limit')
            result=check(raw)  # SAME closure as local Worker, not a second validator.
            path=phase.edit_targets.resolve(phase.takeover_sources,result,phase.policy['max_replacement_bytes'])['path'] if result['action']=='replace_target' else result['path']
            record.update(status='EDIT_ACCEPTED',edited_paths=[path])
            return result
        except (ValueError,ModelFailure) as exc:
            record.update(status='REJECTED',stop_reason=str(exc),failure_class='TAKEOVER_EDIT_REJECTED')
            raise ModelFailure('TAKEOVER_EDIT_REJECTED',str(exc)) from exc
        except HostDefect as exc:
            record.update(status='HOST_DEFECT',stop_reason=str(exc),failure_class='HOST_VALIDATOR_DEFECT')
            raise
        except Cancelled:
            record.update(status='CANCELLED',stop_reason='Host cancellation requested')
            raise
        finally:
            record['elapsed_seconds']=time.monotonic()-started
            dump(record_path,record)
            run.event('host','mock_edit_takeover',takeover_id=identifier,status=record['status'])

    def validated(self, phase, name, receipt, reference):
        path=self._folder(phase.run)/(phase.contract['task_id']+'_'+name+'.json')
        if not path.exists():return  # A normal local edit is not a takeover.
        record=load(path)
        for field in ('input','output'):
            ref=record[field];target=(phase.run.path/ref['reference']).resolve()
            if not target.is_relative_to(phase.run.path.resolve()) or sha(target)!=ref['sha256']:
                raise HostDefect('TAKEOVER_STORE_DEFECT: raw provenance changed')
        before_ref=record['before'];before_path=phase.run.path/before_ref['reference']
        if sha(before_path)!=before_ref['sha256']:
            raise HostDefect('TAKEOVER_STORE_DEFECT: baseline receipt changed')
        prior={r['test']:r['passed'] for r in load(before_path)['results']}
        current={r['test']:r['passed'] for r in receipt['results']}
        record.update(host_validation=reference,
                      improved_test_ids=[k for k,v in current.items() if v and prior.get(k) is False],
                      regressed_test_ids=[k for k,v in current.items() if not v and prior.get(k) is True],
                      passed_test_ids=[k for k,v in current.items() if v],
                      failed_test_ids=[k for k,v in current.items() if not v])
        passed=bool(current) and all(current.values())
        record['status']='HOST_STAGE_PASS' if passed else 'HOST_TEST_REJECTED'
        record['stop_reason']=None if passed else 'Independent protected tests did not all pass'
        record['elapsed_seconds']=(datetime.fromisoformat(now())-datetime.fromisoformat(record['at'])).total_seconds()
        record['completion']=False
        dump(path,record)
        if not passed:
            raise ModelFailure('TAKEOVER_HOST_TEST_FAILED','Independent protected tests rejected takeover; no task/root PASS')


def run_record(run):
    """JSONL-ready export; reporting only, never writes/promotes run state.

    Stage = unique (task_id, phase dispatch name) across attempt checkpoints.
    Local retries and a cloud replacement share that stage, not extra stages.
    Counts refer to bounded PhaseWorker stages, not arbitrary LLM messages.
    """
    records=[load(p) for p in (run.path/'private_takeovers').glob('*.json')
             if not p.name.endswith(('.input.json','.output.json','.contract.json')) and p.name!='reservation.json']
    records += [load(p) for p in (run.path/'private_takeovers').glob('*/record.json')]
    stages={(p.parents[2].name,p.stem) for p in (run.path/'checkpoints').glob('*/*/packets/*.json')}
    external={(r['task_id'],r['target_phase']) for r in records if 'output' in r}
    stages |= external  # A manually substituted edit has the same logical identity.
    stage_pass=set()
    for r in records:
        if r['status']=='HOST_STAGE_PASS' and r.get('host_validation'):
            ref=r['host_validation'];path=(run.path/ref['reference']).resolve()
            if not path.is_relative_to(run.path.resolve()) or sha(path)!=ref['sha256']:
                raise HostDefect('TAKEOVER_REPORT_DEFECT: stage receipt changed')
            stage_pass.add((r['task_id'],r['target_phase']))
    for path in (run.path/'checkpoints').glob('*/*/staged_tests/*/receipt.json'):
        receipt=load(path)
        if path.parent.name.isdigit() and receipt.get('authority')=='host' and receipt.get('results') and all(r['passed'] for r in receipt['results']):
            stage_pass.add((path.parents[3].name,'edit_'+path.parent.name))
    completed=run.state().get('status')=='COMPLETED'
    gate=load(run.path/'artifacts/request_completion_gate.json',{})
    roots=load(run.path/'artifacts/root_evidence.json',[])
    if completed:
        if gate.get('status')!='COMPLETED' or not gate.get('host_evidence_hashes_verified') or not roots:
            raise HostDefect('TAKEOVER_REPORT_DEFECT: existing root acceptance missing')
        for ref in roots:
            path=(run.path/ref['reference']).resolve()
            if not path.is_relative_to(run.path.resolve()) or not ref['passed'] or sha(path)!=ref['sha256']:
                raise HostDefect('TAKEOVER_REPORT_DEFECT: root evidence invalid')
    return {'schema_version':1,'run_id':run.run_id,'timestamp':now(),
            'status':run.state().get('status'),'mock':any(r['mock'] for r in records),
            'completion_classification':('CLOUD_ASSISTED_COMPLETED' if external else 'LOCAL_COMPLETED') if completed else 'NOT_COMPLETED',
            'stage_definition':'unique task_id + phase dispatch name; retries/replacement do not add stages',
            'total_stages':len(stages),'external_stages':len(external),
            'external_stage_ratio':len(external)/len(stages) if stages else 0,
            'host_verified_takeover_stages':len(stage_pass & external),
            'host_verified_stages':len(stage_pass),
            'edit_trial_attempts':sum(1 for p in (run.path/'checkpoints').glob('*/*/staged_tests/*/receipt.json') if p.parent.name.isdigit()),
            'local_call_attempts':sum(1 for p in (run.path/'checkpoints').glob('*/*/calls/*/*.json')),
            'takeover_attempts':len(records),'api_calls':sum(r['usage'].get('api_calls',r.get('api_calls',0)) for r in records),
            'usage_scope':'takeover-only; existing provider and shared mission ledgers unchanged',
            'existing_run_cloud_call_count':run.state().get('cloud_call_count',0),
            'actual_api_cost_usd':None if any(not r['mock'] for r in records) else sum(r['usage']['actual_cost_usd'] for r in records),
            'estimated_api_cost_usd':sum(r.get('estimated_cost_usd') or 0 for r in records) if records and all(r['mock'] or r.get('estimated_cost_usd') is not None for r in records) else None,
            'provider_api_calls':{p:sum(r.get('api_calls',0) for r in records if r['provider']==p) for p in ('openai','anthropic')},
            'takeovers':records}
