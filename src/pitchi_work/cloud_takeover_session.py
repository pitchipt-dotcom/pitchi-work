"""Bounded navigation/reconsideration connection, not a second orchestrator.

Host supplies approved references; each model call still needs exact SEND.
No SDK calls occur in prepare(), no commands or arbitrary filesystem reads.
"""
import fcntl
import json
from .cloud import CloudControlStop
from .cloud_takeover_live import LiveEditTakeover
from .candidate_reasoning import check_reconsideration
from .edit_targets import EditTargets, text_hash
from .resolution import read_verified, write_verified
from .source_navigation import serialized_pages, outline, search
from .working_context import encoded


class CloudTakeoverSession:
    def __init__(self, phase, budget, config, references, hypothesis, checks):
        """references: Host-approved ID -> {kind, ref}, never Worker paths.

        ref is a WorkingContext immutable reference. Before results and failed
        Host receipts must be imported by the Host, not synthesized by a model.
        checks uses the existing reconsideration receipt schema.
        """
        self.phase=phase
        self.live=LiveEditTakeover(phase.run,phase.contract,budget,config)
        self.path=phase.base/'cloud_session.json'
        self.targets=EditTargets(phase.base/'cloud_session_targets.json',phase.contract['allowed_paths'],phase.contract['sha256'])
        for item in references.values():
            if set(item)!={'kind','ref'} or item['kind'] not in ('evidence','before','receipt'):
                raise ValueError('Host-approved reference catalog required')
            phase.working.read(item['ref'])
        if not any(v['kind']=='before' for v in references.values()):
            raise ValueError('Saved Before search reference required')
        if not checks or checks['passed']:
            raise ValueError('Existing failed Host receipt required')
        receipt_id=checks['reference']['reference']
        receipts=[v for v in references.values() if v['kind']=='receipt' and v['ref']['reference']==receipt_id]
        if len(receipts)!=1 or phase.working.read(receipts[0]['ref'])!=checks['receipt']:
            raise ValueError('Current failed receipt must match the approved Host store')
        receipt=checks['receipt']
        if (receipt.get('authority')!='host' or receipt.get('kind')!='protected_staged_tests'
                or not receipt.get('results') or all(r['passed'] for r in receipt['results'])
                or receipt.get('source_hashes')!={p:text_hash(t) for p,t in phase.files.items()}):
            raise ValueError('Failed Host receipt must bind the current source snapshot')
        if set(hypothesis)!={'hypothesis','paths'}:
            raise ValueError('Existing bounded hypothesis and scoped paths required')
        check_reconsideration({'action':'reconsider','decision':'maintain',**hypothesis,
            'test_reference':receipt_id},phase.contract['allowed_paths'],checks,hypothesis,True)
        self.binding={'contract':phase.contract['sha256'],'references':references,'checks':checks,
                      'initial_hypothesis':hypothesis,'budget':str(budget.path.resolve()),'config':config}
        with self._lock():
            if not self.path.exists():
                # Navigation consumes the SAME edit-operation ceiling. Starting
                # a connection cannot hide operations already consumed locally.
                consumed=[int(p.stem[5:])+1 for p in (phase.base/'packets').glob('edit_*.json')
                          if p.stem[5:].isdigit()]
                consumed += [int(p.name[5:])+1 for p in (phase.run.path/'private_takeovers').glob('edit_*')
                             if p.is_dir() and p.name[5:].isdigit()]
                write_verified(self.path,{'binding':self.binding,'files':phase.working.persist(phase.files),
                    'proposal':hypothesis,'detail':None,'seen':[], 'steps':max(consumed,default=0),'stopped':False})
            self.state()

    def _lock(self):
        class Lock:
            def __enter__(inner):
                inner.file=self.path.with_suffix('.lock').open('a+')
                fcntl.flock(inner.file,fcntl.LOCK_EX)
            def __exit__(inner,*args):inner.file.close()
        return Lock()

    def state(self):
        state=read_verified(self.path)
        if state['binding']!=self.binding:raise ValueError('Session binding changed; no reset on resume')
        return state

    def prepare(self):
        state=self.state()
        self.phase.run.raise_if_cancelled()
        if self.phase.run.state()['status'] in self.phase.run.TERMINAL:
            raise ValueError('Never reopen a terminal run')
        if state['stopped'] or state['steps']>=self.phase.policy['max_edit_calls']:
            raise CloudControlStop('TAKEOVER_SESSION_STOPPED','Finite session stopped; no reset')
        for item in self.binding['references'].values():
            self.phase.working.read(item['ref'])
        files=self.phase.working.read(state['files'])
        packet={'operation':'cloud_edit_navigation','objective':self.phase.contract.get('goal'),
            'formal_contract':self.phase.contract,'proposal':state['proposal'],
            'proposal_authority':'ASSISTED, unproven; may maintain, revise or reject using evidence',
            'failed_test_reference':self.binding['checks']['reference']['reference'],
            'catalog':self.binding['references'],
            'sources':[{'path':p,'source_sha256':text_hash(t),'write_allowed':p in self.phase.contract['allowed_paths']}
                       for p,t in sorted(files.items())],
            'requested_detail':state['detail'],
            'protocol':{
                'evidence':{'action':'evidence','id':'catalog ID','page':0},
                'read':{'action':'read','path':'source catalog path','page':0},
                'navigation':{'action':'navigation','path':'source catalog path'},
                'source_search':{'action':'source_search','term':'literal text'},
                'reconsider':{'action':'reconsider','decision':'maintain|revise|reject',
                    'hypothesis':'bounded explanation','paths':['allowed path'],
                    'test_reference':'failed_test_reference'},
                'edit_target':{'action':'edit_target','path':'allowed path','source_sha256':'current hash',
                    'start_line':1,'end_line':2},
                'replace_target':{'action':'replace_target','target_id':'Host-issued ID','new_text':'source text'}},
            'instructions':'Return one operation only. Obtain saved Before/evidence pages and source pages as needed. '
                'Do not treat the local proposal or target as correct. No shell commands. All edits require Host-issued targets.',
            'step':state['steps']}
        payload={'provider':self.live.config['provider'],'model':self.live.config['model'],
            'messages':[{'role':'user','content':encoded(packet).decode()}],
            'max_output_tokens':min(self.live.config['max_output_tokens'],self.phase.policy['max_output_tokens'])}
        # Same conservative byte/token guard as atomic takeover, no exception.
        if len(encoded(payload))+512+payload['max_output_tokens']>self.phase.policy['context_tokens']*80//100:
            raise CloudControlStop('PHASE_CONTEXT_DEFECT','Session packet exceeds unchanged 80% guard')
        return payload,self.live.budget.preview(payload)

    def execute(self,payload,approved_sha,*,client=None):
        with self._lock():
            expected,_=self.prepare()
            if payload!=expected:raise ValueError('Stale session packet; present a new SEND preview')
            state=self.state();files=self.phase.working.read(state['files'])
            step=state['steps'];state['steps']+=1
            # Crash conservatively stops; consumed operations cannot reset.
            state['stopped']=True;write_verified(self.path,state)
            def handle(raw):
                if not isinstance(raw,dict) or len(json.dumps(raw,ensure_ascii=False).encode())>self.phase.policy['max_response_bytes']:
                    raise ValueError('Existing response budget/schema required')
                if raw.get('action')=='replace_target':return None
                request_hash=text_hash(encoded(raw).decode())
                if request_hash in state['seen']:raise ValueError('NO_PROGRESS: repeated material/proposal request')
                detail=self._resolve(raw,files,state)
                state['seen'].append(request_hash)
                return detail
            candidate,record=self.live.execute(payload,approved_sha,files,'edit_'+str(step),
                client=client,targets=self.targets,response_handler=handle)
            if record['status']=='HOST_STAGE_PASS':
                state['files']=self.phase.working.persist(candidate)
                # Local edit success is NOT root acceptance. Do not re-edit automatically.
                state['detail']={'host_stage_receipt':record['host_validation'],'completion':False}
            elif record['status']=='HOST_REQUEST_RESOLVED':
                state['detail']=record['request_result'];state['stopped']=False
            write_verified(self.path,state)
            return candidate,record

    def _resolve(self,raw,files,state):
        action=raw.get('action')
        if action=='evidence' and set(raw)=={'action','id','page'}:
            item=self.binding['references'].get(raw['id'])
            if item is None:raise ValueError('Unknown/unapproved Evidence ID')
            return {'id':raw['id'],'kind':item['kind'],**self.phase.working.detail(item['ref'],raw['page'])}
        chunks=lambda t:serialized_pages(t,self.phase.policy['source_page_bytes'])
        if action=='source_search' and set(raw)=={'action','term'}:
            return search(files,raw['term'],chunks)
        if action in ('read','navigation') and set(raw)==({'action','path','page'} if action=='read' else {'action','path'}):
            path=raw['path']
            if path not in files:raise ValueError('Source outside Host catalog')
            pages=chunks(files[path])
            if action=='navigation':return {'path':path,'source_sha256':text_hash(files[path]),'outline':outline(files[path],pages),'pages':len(pages)}
            page=raw['page']
            if type(page) is not int or not 0<=page<len(pages):raise ValueError('Invalid source page')
            return {'path':path,'source_sha256':text_hash(files[path]),'page':page,'page_count':len(pages),'detail':pages[page]}
        if action=='reconsider':
            if raw.get('decision') not in ('maintain','revise','reject'):
                raise ValueError('Explicit maintain/revise/reject decision required')
            proposal=check_reconsideration(raw,self.phase.contract['allowed_paths'],self.binding['checks'],state['proposal'],True)
            state['proposal']={k:proposal[k] for k in ('hypothesis','paths')}
            return {'proposal':state['proposal'],'decision':proposal['decision'],'semantic_assessment':'ASSISTED'}
        if action=='edit_target' and set(raw)=={'action','path','source_sha256','start_line','end_line'}:
            path=raw['path']
            if path not in files or text_hash(files[path])!=raw['source_sha256']:raise ValueError('Current source hash/scope required')
            target=self.targets.issue(files,path,raw['start_line'],raw['end_line'])
            return {'target':target,'text':''.join(files[path].splitlines(True)[raw['start_line']-1:raw['end_line']])}
        raise ValueError('Only existing bounded navigation, reconsideration and target operations allowed')
