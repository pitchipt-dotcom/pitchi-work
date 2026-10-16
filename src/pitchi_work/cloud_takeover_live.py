"""Explicitly approved atomic edit takeover using existing SDK/Host tools.

Not an auto-rescue trigger. No shell authority, root decision or memory injection.
"""
import fcntl
import math
import json
import time
import hashlib
from pathlib import Path

from .cloud import CloudRoleAdapter, CloudControlStop, fingerprint
from .store import dump, load, now
from .resolution import read_verified, write_verified
from .runner import HostDefect
from .phase_workflow import digest
from .edit_protocol import replace_exact, render_lines
from .search_quality import line_edits
from .mission_profiles import sha


class TakeoverBudget:
    """Private grant shared explicitly by parent/child/resume; reservations persist.

    Prices are conservative USD/million bounds in the immutable grant. Unknown
    response usage retains its entire reservation; no crash silently refunds it.
    Provider invoice amounts are distinct from usage-based cost upper bounds.
    """
    def __init__(self, path, policy=None):
        self.path=Path(path)
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.path.exists():
                data=read_verified(self.path)
                if policy is not None and data['policy']!=policy:raise CloudControlStop('BUDGET_POLICY_CHANGED','Existing grant cannot be reset')
            elif policy is not None:
                if not 1<=policy['max_calls']<=16 or set(policy['limits_usd'])!={'openai','anthropic'} or any(v!=0.5 for v in policy['limits_usd'].values()):
                    raise ValueError('Explicit finite experiment grant required')
                write_verified(self.path,{'schema_version':1,'policy':policy,'calls':[]})
            else:raise CloudControlStop('BUDGET_GRANT_MISSING','No existing experiment grant')

    def preview(self,payload):
        data=read_verified(self.path);p=payload['provider'];model=payload['model']
        rates=data['policy']['rates'].get(p,{}).get(model)
        if not rates or len(rates)!=2 or any(type(x) not in (float,int) or not math.isfinite(x) or x<=0 for x in rates):
            raise CloudControlStop('PRICE_UNVERIFIED','No verified conservative model price')
        # Byte count + envelope allowance upper-bounds ordinary text tokenization;
        # reserve all max_output_tokens, including hidden reasoning tokens.
        incoming=sum(len(m['content'].encode())+64 for m in payload['messages'])+1024
        if incoming>24000:raise CloudControlStop('CLOUD_CONTEXT_LIMIT','Bounded edit input exceeded')
        cost=math.ceil((incoming*rates[0]+payload['max_output_tokens']*rates[1])*1.25)/1_000_000
        spent=sum(c['charged_bound_usd'] for c in data['calls'] if c['provider']==p)
        return {'sha256':fingerprint(payload),'provider':p,'model':model,'input_token_upper_bound':incoming,
                'max_output_tokens':payload['max_output_tokens'],'maximum_cost_usd':cost,
                'provider_charged_or_reserved_usd':spent,'provider_remaining_usd':0.5-spent,
                'rates_usd_per_million':rates,'within_budget':spent+cost<=0.5 and len(data['calls'])<data['policy']['max_calls'],
                'calls_reserved':len(data['calls']),'invoice_cost_usd':None}

    def reserve(self,payload,approved_sha,run_id):
        with self.path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            estimate=self.preview(payload);data=read_verified(self.path)
            if fingerprint(payload)!=approved_sha:raise CloudControlStop('CLOUD_APPROVAL_MISMATCH','SEND must match this exact payload')
            if any(c['sha256']==approved_sha for c in data['calls']):raise CloudControlStop('CLOUD_DUPLICATE_REQUEST','Exact request already reserved; never resend')
            if not estimate['within_budget']:raise CloudControlStop('CLOUD_BUDGET_EXHAUSTED','Provider budget or finite operation limit exhausted')
            index=len(data['calls'])
            data['calls'].append({**estimate,'run_id':run_id,'at':now(),'status':'RESERVED',
                                  'charged_bound_usd':estimate['maximum_cost_usd']})
            write_verified(self.path,data);return index

    def settle(self,index,receipt):
        with self.path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            data=read_verified(self.path);call=data['calls'][index];usage=receipt.get('usage',{})
            call.update(status='RESPONSE' if usage else 'UNCONFIRMED',usage=usage,
                        receipt_success=receipt.get('success',False),invoice_cost_usd=None)
            if usage:
                i=usage.get('prompt_tokens');o=usage.get('completion_tokens')
                if type(i) is not int or type(o) is not int or i<0 or o<0 or i>call['input_token_upper_bound'] or o>call['max_output_tokens']:
                    write_verified(self.path,data)
                    raise CloudControlStop('CLOUD_USAGE_DEFECT','Usage exceeded reserved bounds; stop, reservation retained')
                rates=call['rates_usd_per_million']
                call['charged_bound_usd']=math.ceil((i*rates[0]+o*rates[1])*1.25)/1_000_000
            write_verified(self.path,data)


def apply_edit(raw,files,contract,*,targets=None):
    """Existing exact replacement/line serialization tools, fixed edit limits."""
    if not isinstance(raw,dict) or len(json.dumps(raw,ensure_ascii=False).encode())>contract['phase_policy']['max_response_bytes']:
        raise ValueError('Existing bounded edit object required')
    if raw.get('action')=='replace_target':
        if targets is None or targets.allowed!=sorted(contract['allowed_paths']) or targets.identity['binding']!=contract['sha256']:
            raise ValueError('Host target registry with current contract scope required')
        patch=targets.resolve(files,raw,contract['phase_policy']['max_replacement_bytes'])
        return {**files,**line_edits([{k:patch[k] for k in ('path','source_sha256','start_line','end_line','replacement')}],files,contract['allowed_paths'])}
    action=raw.get('action');name=raw.get('path')
    if name not in contract['allowed_paths'] or raw.get('source_sha256')!=hashlib.sha256(files.get(name,'').encode()).hexdigest():
        raise ValueError('Scope or current source hash mismatch')
    if not isinstance(raw.get('summary'),str):raise ValueError('Edit summary required')
    result=dict(files);limit=contract['phase_policy']['max_replacement_bytes']
    if action=='replace' and set(raw)=={'action','path','source_sha256','old_text','new_text','summary'}:
        result[name]=replace_exact(files.get(name,''),raw['old_text'],raw['new_text'],limit)
    elif action=='patch' and set(raw)=={'action','path','source_sha256','start_line','end_line','replacement_lines','summary'}:
        text=files.get(name,'');lines=text.splitlines(True)
        replacement=render_lines(raw['replacement_lines'],raw['end_line']<len(lines) or text.endswith('\n'),limit)
        result.update(line_edits([{k:raw[k] for k in ('path','source_sha256','start_line','end_line')}|{'replacement':replacement},],files,contract['allowed_paths']))
    else:raise ValueError('One existing patch/replace operation only; no generated commands')
    if result==files:raise ValueError('NO_PROGRESS: unchanged candidate')
    return result


class LiveEditTakeover:
    """Manual one-stage invocation. Caller must present then approve exact payload.

    A local failure is linked by immutable refs, not fabricated automatic trigger.
    Formal task/root validation remains the existing MissionController pathway.
    """
    def __init__(self,run,contract,budget,config):
        self.run,self.contract,self.budget,self.config=run,contract,budget,config
        check=dict(contract);expected=check.pop('sha256')
        if digest(check)!=expected:raise HostDefect('Formal contract hash mismatch')
        state=run.state();bound=state.get('takeover_budget_path')
        if not bound and state.get('parent_run'):
            bound=load(Path(state['parent_run'])/'state.json',{}).get('takeover_budget_path')
        if bound and Path(bound).resolve()!=budget.path.resolve():
            raise CloudControlStop('BUDGET_POLICY_CHANGED','Child/resume must use the inherited grant')
        if not state.get('takeover_budget_path'):run.save_state(takeover_budget_path=str(budget.path.resolve()))

    def execute(self,payload,approved_sha,files,phase_id,*,client=None,targets=None,response_handler=None):
        self.run.raise_if_cancelled()
        if self.run.state()['status'] in self.run.TERMINAL:raise ValueError('Never reopen a terminal run')
        if not phase_id.startswith('edit_') or not phase_id[5:].isdigit() or int(phase_id[5:])>=self.contract['phase_policy']['max_edit_calls']:
            raise CloudControlStop('PHASE_EDIT_LIMIT','Existing finite edit operation limit')
        if payload['provider']!=self.config['provider'] or payload['model']!=self.config['model']:raise ValueError('Provider/model mismatch')
        # Exclusive shared reservation is taken by the SDK approval callback,
        # immediately before the existing adapter reserves/sends its one call.
        reservation=[]
        def approve(body,sha256):
            if body!=payload or sha256!=approved_sha:return False
            reservation.append(self.budget.reserve(body,approved_sha,self.run.run_id));return True
        folder=self.run.path/'private_takeovers'/phase_id;folder.mkdir(mode=0o700,parents=True,exist_ok=False)
        dump(folder/'input.json',payload);started=time.monotonic()
        record={'schema_version':1,'mock':False,'mission_id':self.run.state().get('takeover_grant_id',self.run.run_id),
                'run_id':self.run.run_id,'takeover_id':phase_id,'target_phase':phase_id,'task_id':self.contract['task_id'],
                'original_worker':self.run.state().get('local_model','local'),
                'host_condition':'explicit_manual_saved_local_edit_failure','provider':payload['provider'],'model':payload['model'],
                'input':{'reference':str((folder/'input.json').relative_to(self.run.path)),'sha256':sha(folder/'input.json')},
                'request_sha256':approved_sha,'status':'STARTED','at':now(),'completion':False}
        prior_receipts=set((self.run.path/'artifacts').glob('role_call_*_cloud_edit_worker/receipt.json'))
        adapter=CloudRoleAdapter(self.run,self.config,approval=approve,client=client)
        try:
            raw=adapter.ask('cloud_edit_worker','',messages=payload['messages'],max_output_tokens=payload['max_output_tokens'])
            self.run.raise_if_cancelled();dump(folder/'output.json',raw)
            record['output']={'reference':str((folder/'output.json').relative_to(self.run.path)),'sha256':sha(folder/'output.json')}
            if response_handler is not None:
                detail=response_handler(raw)
                if detail is not None:
                    record.update(status='HOST_REQUEST_RESOLVED',request_result=detail)
                    return files,record
            candidate=apply_edit(raw,files,self.contract,targets=targets)
            from .staged_validation import observe_staged
            before=observe_staged(self.run,self.contract,files,folder/'before')
            after=observe_staged(self.run,self.contract,candidate,folder/'after')
            from .trial_progress import TrialProgress
            progress=TrialProgress(before);progress.observe(after,raw)
            record.update(status='HOST_STAGE_PASS' if after['results'] and all(r['passed'] for r in after['results']) else 'HOST_TEST_REJECTED',
                comparison=progress.comparison,edited_paths=[p for p in candidate if candidate[p]!=files.get(p,'')],
                before={'reference':str((folder/'before/receipt.json').relative_to(self.run.path)),'sha256':sha(folder/'before/receipt.json')},
                host_validation={'reference':str((folder/'after/receipt.json').relative_to(self.run.path)),'sha256':sha(folder/'after/receipt.json')})
            dump(folder/'candidate.json',candidate)
            return candidate,record
        except Exception as exc:
            record.update(status='REJECTED',failure_class=getattr(exc,'failure_class','TAKEOVER_EDIT_REJECTED'),stop_reason=str(exc))
            raise
        finally:
            receipts=sorted(set((self.run.path/'artifacts').glob('role_call_*_cloud_edit_worker/receipt.json'))-prior_receipts)
            receipt=load(receipts[-1],{}) if receipts else {}
            try:
                if reservation:self.budget.settle(reservation[0],receipt)
            finally:
                record.update(elapsed_seconds=time.monotonic()-started,usage=receipt.get('usage',{}),
                    api_calls=len(reservation),actual_api_cost_usd=None,estimated_cost_usd=receipt.get('estimated_cost_usd'))
                dump(folder/'record.json',record)
                self.run.event('host','live_edit_takeover',takeover_id=phase_id,status=record['status'])
