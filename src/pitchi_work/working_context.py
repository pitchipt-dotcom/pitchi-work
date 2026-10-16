"""Deterministic working views; immutable Host history is not the model context."""
import json
import hashlib
import re
from pathlib import Path

from .runner import HostDefect
from .store import dump, load


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


class WorkingContext:
    VERSION = 1
    PAGE_BYTES = 1200
    CATALOG_ITEMS = 4
    # Space for the operation envelope, feedback and bounded correction output.
    ENVELOPE_RESERVE = 1800

    def __init__(self, root, run_root):
        self.root = Path(root)
        self.run_root = Path(run_root).resolve()
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(self.run_root):
            raise HostDefect('PHASE_CONTEXT_DEFECT: unsafe history store')
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root.parent/'material_scope.json').exists():self.material_ids()

    def persist(self, value):
        raw = encoded(value)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / (digest+'.json')
        if path.exists():
            if path.is_symlink() or encoded(load(path)) != raw:
                raise HostDefect('PHASE_CONTEXT_DEFECT: history hash mismatch')
        else:
            # store.dump formatting is not the canonical content identity.
            dump(path, value)
        return {'reference': str(path.relative_to(self.run_root)),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'content_sha256': digest}

    def read(self, ref):
        if (not isinstance(ref, dict) or set(ref) != {'reference','sha256','content_sha256'} or
                not isinstance(ref['reference'],str) or any(not isinstance(ref[key],str) or
                    not re.fullmatch('[0-9a-f]{64}',ref[key]) for key in ('sha256','content_sha256'))):
            raise HostDefect('PHASE_CONTEXT_DEFECT: history reference schema')
        path = self.run_root / ref['reference']
        if (path.parent != self.root or path.is_symlink() or
                path.name != ref['content_sha256']+'.json' or not path.is_file() or
                hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']):
            raise HostDefect('PHASE_CONTEXT_DEFECT: history reference/hash mismatch')
        value = load(path)
        if hashlib.sha256(encoded(value)).hexdigest() != ref['content_sha256']:
            raise HostDefect('PHASE_CONTEXT_DEFECT: history content mismatch')
        return value

    def detail(self, ref, page):
        from .phase_workflow import pages
        from .research_handoff import HandoffContractError
        chunks = pages(encoded(self.read(ref)).decode(), self.PAGE_BYTES)
        if type(page) is not int or not 0 <= page < len(chunks):
            raise HandoffContractError('Use an existing observation page')
        text = chunks[page]['text']
        return {'reference': ref, 'page': page, 'page_count': len(chunks),
                'text': text, 'detail_sha256': hashlib.sha256(text.encode()).hexdigest()}

    def observations(self, value):
        ref = self.persist(value)
        first = self.detail(ref, 0)
        # Deterministic latest page, exact source, explicitly a partial view.
        recent = self.detail(ref, first['page_count']-1)
        return {'reference': ref, 'page_count': first['page_count'],
                'recent': recent, 'view_is_partial': first['page_count'] > 1}

    def authorize_material(self, value):
        from .resolution import read_verified,write_verified
        path=self.root.parent/'material_scope.json'
        if path.is_symlink():raise HostDefect('PHASE_CONTEXT_DEFECT: unsafe material registry')
        known=read_verified(path) if path.exists() else {}
        ref=self.persist(value);identity=ref['content_sha256']
        if identity not in known and len(known)>=64:
            raise HostDefect('PHASE_CONTEXT_DEFECT: finite material scope exhausted')
        if identity in known and known[identity]!=ref:
            raise HostDefect('PHASE_CONTEXT_DEFECT: material binding changed')
        known[identity]=ref;write_verified(path,known)
        return identity

    def material(self, identity, page):
        from .resolution import read_verified
        from .research_handoff import HandoffContractError
        path=self.root.parent/'material_scope.json'
        if path.is_symlink():raise HostDefect('PHASE_CONTEXT_DEFECT: unsafe material registry')
        known=read_verified(path) if path.exists() else {}
        if not isinstance(identity,str) or identity not in known:
            raise HandoffContractError('Use a Host-issued context_id from an input material card')
        if known[identity].get('content_sha256')!=identity:
            raise HostDefect('PHASE_CONTEXT_DEFECT: material identity mismatch')
        return self.detail(known[identity],page)

    def material_ids(self):
        from .resolution import read_verified
        path=self.root.parent/'material_scope.json'
        if path.is_symlink():raise HostDefect('PHASE_CONTEXT_DEFECT: unsafe material registry')
        known=read_verified(path) if path.exists() else {}
        if not isinstance(known,dict) or len(known)>64:
            raise HostDefect('PHASE_CONTEXT_DEFECT: material scope schema')
        for identity,ref in known.items():
            if not isinstance(ref,dict) or ref.get('content_sha256')!=identity:
                raise HostDefect('PHASE_CONTEXT_DEFECT: material scope binding')
            self.read(ref)
        return list(known)

    def compose(self, payload, context_tokens, output_tokens, *, envelope_reserve=None,
                materials=None):
        """Reserve fixed input first, then admit whole material views by priority.

        Material declarations are Host-owned. Unknown fields remain mandatory;
        adding a new kind only requires a declaration, not another overflow rule.
        A fallback must retain a usable, existing retrieval operation. No strings
        are shortened and no permanent evidence is removed.
        """
        reserve = self.ENVELOPE_RESERVE if envelope_reserve is None else envelope_reserve
        if type(reserve) is not int or reserve < 0:
            raise HostDefect('PHASE_CONTEXT_DEFECT: invalid envelope reserve')
        limit = context_tokens*80//100-output_tokens-512-reserve
        declarations = dict(materials or {})
        if payload.get('material_protocol_version') in (1,2,3):
            # Current objective precedes automatic source previews. Historical
            # version-1 packets retain their pinned admission ordering.
            goal_priority=200 if payload['material_protocol_version'] in (2,3) else 50
            for field,priority in (('task_goal',goal_priority),('hypothesis',40),('abstract_procedures',20)):
                if field not in payload:continue
                value=payload[field]
                if isinstance(value,dict) and value.get('body_deferred'):
                    identity=value['context_id'];self.material(identity,0)
                else:identity=self.authorize_material(value)
                request={'action':'context_material','context_id':identity,'page':0}
                declarations.setdefault(field,{'priority':priority,'card':False,
                    'fallback':{'kind':field,'context_id':identity,'body_deferred':True,'request':request},
                    'request':request})
                if payload['material_protocol_version']==3:
                    declarations[field]['fallback']={'context_id':identity,'body_deferred':True}
        if payload.get('requested_detail') is not None and isinstance(payload.get('current_evidence'),dict):
            # The requested page supersedes the automatic body, not its scope.
            self.persist(payload['current_evidence'])
            payload={**payload,'current_evidence':{**payload['current_evidence'],'detail':None}}
        if payload.get('prior_handoff') is not None and 'catalog_view' in payload:
            declarations.setdefault('prior_handoff',{'priority':5,'fallback':{},
                'request':{'action':'catalog','page':0}})
        catalog=payload.get('catalog_view')
        if payload.get('context_catalog_version')==1 and isinstance(catalog,dict) and isinstance(catalog.get('items'),list) and all(
                isinstance(v,dict) and 'index' in v and 'evidence_ids' in v for v in catalog['items']):
            # Same lossless navigation projection as select(), admitted through
            # the shared budget composer rather than a separate overflow path.
            compact={**catalog,'items':[{k:v for k,v in item.items() if
                k in {'index','evidence_ids'} or (k in {'finding','conditions','exceptions',
                'prohibitions','uncertainty','unclassified'} and v)} for item in catalog['items']],
                'source_metadata_deferred':True}
            declarations.setdefault('catalog_view',{'priority':10,'fallback':compact,
                'request':{'action':'catalog','page':catalog.get('page',0)}})
        detail=payload.get('requested_detail')
        if payload.get('context_catalog_version')==1 and isinstance(detail,dict) and isinstance(detail.get('handoff'),dict) and isinstance(detail.get('detail'),dict):
            body=detail['detail'];handoff=detail['handoff']
            # The exact requested page is mandatory. Its repeated catalog is
            # Host provenance, not another source body: retain IDs/interpretation
            # here, and keep the full metadata/hash in the immutable history.
            compact={**detail,'handoff':{k:handoff[k] for k in ('evidence_refs','interpretation')},
                'metadata_deferred':True}
            route={'action':'evidence','evidence_id':body['evidence_id'],'page':body['page']}
            compact['metadata_request']=route
            declarations.setdefault('requested_detail',{'priority':100,'fallback':compact,
                'request':route,'card':False})
        # Existing navigation operations authorize these views independently of
        # context size. Keep mandatory test identity/results and all scope IDs.
        if 'host_observations' in payload:
            value = payload['host_observations']
            view = value if isinstance(value, dict) and 'reference' in value else self.observations(value)
            self.read(view['reference'])
            declarations.setdefault('host_observations', {'priority':20,
                'fallback':{**view, 'recent':None, 'view_is_partial':True},
                'request':{'action':'observations','page':0}})
            payload = {**payload, 'host_observations':view}
        if isinstance(payload.get('source_navigation'), dict):
            value=payload['source_navigation']
            declarations.setdefault('source_navigation', {'priority':10,
                'fallback':{'remaining_operations':value.get('remaining_operations'),
                            'request':{'action':'navigation','page':0}},
                'request':{'action':'navigation','page':0}})
        if 'diagnostic_access' in payload:
            if payload.get('context_navigation_version')==1:
                # Exact state and retrieval operations remain; their repeated
                # narration is already supplied by rules/current_objective.
                for field,route in (('diagnostic_access',{'action':'test_result','page':0}),
                                    ('tool_state',{'action':'observations','page':0})):
                    if isinstance(payload.get(field),dict):
                        declarations.setdefault(field,{'priority':0,'card':False,
                            'fallback':{k:v for k,v in payload[field].items() if k not in ('rules','read_rule')},
                            'request':route})
            # Same lossless navigation projections as the legacy selector, but
            # declare them before reservation rather than after overflow.
            if isinstance(payload.get('sources'),list):
                declarations.setdefault('sources',{'priority':15,'card':False,
                    'fallback':[v for v in payload['sources'] if v.get('write_allowed')],
                    'request':{'action':'source_catalog','page':0}})
            for field,route,action in (
                    ('history_access','observations/page','observations'),
                    ('catalog_access','source_catalog/page; read/path/page','source_catalog')):
                if field in payload:
                    declarations.setdefault(field,{'priority':0,'card':False,
                        'fallback':route,'request':{'action':action,'page':0}})
            history=payload.get('candidate_reasoning')
            if isinstance(history,dict) and not history.get('recent_trials') and history.get('trial_access') is None:
                declarations.setdefault('candidate_reasoning',{'priority':0,'card':False,
                    'fallback':None,'request':{'action':'observations','page':0}})
            if isinstance(payload.get('current_objective'),dict):
                declarations.setdefault('current_objective',{'priority':5,'card':False,
                    'fallback':{**payload['current_objective'],'rules':
                        'Inspect failed receipt/source. Errors are clues, not proof. Protected tests and host_contract are unchanged.'},
                    'request':{'action':'test_result','page':0}})
        tests=payload.get('protected_staged_tests')
        if isinstance(tests,dict) and tests.get('reference'):
            declarations.setdefault('protected_staged_tests', {'priority':50,
                'fallback':{**tests,'failures':[], 'view_is_partial':True},
                'request':{'action':'test_result','page':0}})
        view=payload.get('source_view')
        if isinstance(view,dict) and isinstance(view.get('text'),str) and view.get('path'):
            # No partial replacement code is fabricated. Host retains the exact
            # range; Worker must navigate pages and select a bounded target.
            declarations.setdefault('source_view', {'priority':100,
                'fallback':{k:v for k,v in view.items() if k!='text'},
                'request':{'action':'read','path':view['path'],'page':0}})
        result={k:v for k,v in payload.items() if k not in declarations}
        refs={}
        for field,decl in declarations.items():
            if field not in payload:continue
            if not isinstance(decl.get('request'),dict) or not decl['request'].get('action'):
                raise HostDefect('PHASE_CONTEXT_DEFECT: material has no authorized retrieval route')
            value=payload[field]
            prior=None
            if isinstance(value,dict) and value.get('body_deferred') and value.get('context_id'):
                content_id=value['context_id']
                if not isinstance(content_id,str) or not re.fullmatch('[0-9a-f]{64}',content_id):
                    raise HostDefect('PHASE_CONTEXT_DEFECT: invalid material identity')
                path=self.root/(content_id+'.json')
                if path.is_symlink() or not path.is_file():
                    raise HostDefect('PHASE_CONTEXT_DEFECT: missing material identity')
                prior={'reference':str(path.relative_to(self.run_root)),
                       'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'content_sha256':content_id}
            refs[field]=prior or self.persist(value)
            if prior:self.read(prior)
            fallback=decl['fallback']
            if decl.get('card',True):
                fallback={**fallback,'body_deferred':True,'request':decl['request']}
            # Worker needs an opaque content identity and its authorized route,
            # not repeated filesystem/hash metadata; full references stay in
            # the Host receipt and are validated when rebuilding the card.
            if decl.get('card',True) and 'reference' not in fallback:fallback['context_id']=refs[field]['content_sha256']
            if field=='source_view':fallback['requires_source_read_and_target_selection']=True
            # A reference card must never make a small complete view larger.
            if len(encoded(fallback))>=len(encoded(value)):fallback=value
            result[field]=fallback
        fixed_bytes=len(encoded({k:v for k,v in payload.items() if k not in declarations}))
        minimum_bytes=len(encoded(result))
        audit={'authority':'host','operation':'working_context_composition','version':1,
            'original':self.persist(payload),
            'limit_bytes':limit,'output_reserved_bytes':output_tokens,
            'envelope_reserved_bytes':reserve,'fixed_bytes':fixed_bytes,
            'minimum_bytes':minimum_bytes,'original_bytes':len(encoded(payload)),
            'field_bytes':{k:len(encoded(v)) for k,v in payload.items()},
            'materials':{},'is_task_pass':False}
        if minimum_bytes>limit:
            audit.update(status='INFEASIBLE',selected_bytes=minimum_bytes)
            self.persist(audit)
            raise HostDefect('PHASE_CONTEXT_DEFECT: reserved working set cannot fit; '
                f'bytes={minimum_bytes}, limit={limit}, field_bytes={audit["field_bytes"]}')
        for field,decl in sorted(declarations.items(), key=lambda item:(-item[1]['priority'],item[0])):
            if field not in payload:continue
            candidate={**result,field:payload[field]}
            admitted=len(encoded(candidate))<=limit
            if admitted:result=candidate
            audit['materials'][field]={'reference':refs[field], 'request':decl['request'],
                'original_bytes':len(encoded(payload[field])), 'selected_bytes':len(encoded(result[field])),
                'deferred':not admitted,'priority':decl['priority']}
        audit.update(status='READY',selected_bytes=len(encoded(result)),
                     removed_bytes=len(encoded(payload))-len(encoded(result)))
        self.persist(audit)
        return result

    def select(self, payload, context_tokens, output_tokens, *, envelope_reserve=None):
        """Never omit objective/rules/current material/explicitly requested detail.

        Only optional recent history views can be evicted. Their references remain.
        Dispatch still enforces the original 80% guard on the actual full prompt.
        """
        result = dict(payload)
        if result.get('requested_detail') is not None and isinstance(result.get('current_evidence'), dict):
            # A requested exact page takes priority over the initial auto-view.
            # Keep its IDs/hash and retrieval path; do not send two source bodies.
            result['current_evidence'] = {**result['current_evidence'], 'detail': None}
        observations=payload.get('host_observations',[])
        if isinstance(observations,dict) and set(observations)=={'reference','page_count','recent','view_is_partial'}:
            # Re-projecting a working view must not turn its reference metadata
            # into the observations themselves, nor add another wrapper/body.
            self.read(observations['reference'])
            count=self.detail(observations['reference'],0)['page_count']
            recent=observations['recent']
            if observations['page_count']!=count or (recent is not None and
                    recent!=self.detail(observations['reference'],recent.get('page'))):
                raise HostDefect('PHASE_CONTEXT_DEFECT: projected observation view mismatch')
            result['host_observations']=dict(observations)
        else:result['host_observations'] = self.observations(observations)
        reserve=self.ENVELOPE_RESERVE if envelope_reserve is None else envelope_reserve
        if type(reserve) is not int or reserve<0:raise HostDefect('PHASE_CONTEXT_DEFECT: invalid envelope reserve')
        limit = context_tokens*80//100-output_tokens-512-reserve
        if len(encoded(result)) > limit:
            result['host_observations'] = {**result['host_observations'], 'recent': None,
                                           'view_is_partial': True}
        if len(encoded(result)) > limit and 'prior_handoff' in result:
            # All prior checkpoints remain accessible through the paged catalog.
            result['prior_handoff'] = None
        if len(encoded(result)) > limit and isinstance(result.get('candidate_reasoning'),dict) and 'trial_review' in result:
            # Derived counts duplicate trial_review/current tests. Keep all
            # visible step IDs and the scoped receipt route; full receipts are
            # immutable and fetched with trial_result. No test or rule changes.
            history=result['candidate_reasoning']
            result['candidate_reasoning']={
                'recent_trials':[{'step':v['step']} for v in history.get('recent_trials',[])],
                'trial_access':history.get('trial_access'), 'view_is_partial':True}
        if len(encoded(result)) > limit and isinstance(result.get('source_navigation'),dict):
            navigation=result['source_navigation']
            # Derived symbols/read indexes are optional navigation, NOT the
            # current source, goal or contract. Retain exact Host history/hash.
            reference=navigation.get('reference')
            if reference is not None:self.read(reference)
            result['source_navigation']={**navigation,'symbols':{},
                'read_history':navigation.get('read_history',[])[-4:],
                'reference':reference or self.persist(navigation),'view_is_partial':True,
                'request_example':{'action':'navigation','page':0}}
        if len(encoded(result)) > limit and isinstance(result.get('source_navigation'),dict):
            result['source_navigation']={k:v for k,v in result['source_navigation'].items()
                if k in {'reference','view_is_partial','request_example','remaining_operations'}}
        if len(encoded(result)) > limit and 'diagnostic_access' in result:
            tests=result.get('protected_staged_tests',{})
            if tests.get('reference'):
                result['protected_staged_tests']={**tests,'failures':[], 'view_is_partial':True,
                    'request_example':{'action':'test_result','page':0}}
        if len(encoded(result)) > limit and 'diagnostic_access' in result and isinstance(result.get('sources'),list):
            # Paged readonly catalog remains fetchable; retain every writable
            # path/current hash. Explicitly requested source_view is untouched.
            result['sources']=[v for v in result['sources'] if v.get('write_allowed')]
        if len(encoded(result)) > limit and isinstance(result.get('source_view'),dict):
            view=result['source_view']
            # An automatic location cue is an optional navigation view, unlike
            # a source page expressly requested by Worker. Retain its exact
            # snapshot/path/line/hash and read route; never cut source text.
            if view.get('kind')=='diagnostic_source' and view.get('text'):
                reference=view.get('reference')
                if reference is not None:self.read(reference)
                result['source_view']={**view,'text':'','reference':reference or self.persist(view),
                    'automatic_body_deferred':True,'view_is_partial':True}
        if len(encoded(result)) > limit and 'diagnostic_access' in result:
            # Host-derived navigation narration is not the formal contract.
            # Retain phase, literal retrieval routes, all acceptance/scope and
            # expressly requested source; do not compress Worker findings.
            if isinstance(result.get('current_objective'),dict):
                objective=result['current_objective']
                result['current_objective']={**objective,'rules':
                    'Inspect failed receipt/source. Errors are clues, not proof. Protected tests and host_contract are unchanged.'}
            if 'catalog_access' in result:
                result['catalog_access']='Read exact path/page; source_catalog pages readonly metadata. Write scope stays in host_contract.'
        if len(encoded(result)) > limit and isinstance(result.get('source_view'),dict):
            view=result['source_view']
            if view.get('kind')=='diagnostic_source' and view.get('automatic_body_deferred') and view.get('reference'):
                # Only the automatically chosen diagnostic cue: explicit read
                # pages are never touched. Full cue and provenance are hashed
                # in Host history; source navigation is already in the contract.
                self.read(view['reference'])
                compact={k:v for k,v in view.items() if k in
                    {'kind','path','source_sha256','start_line','end_line','reference','view_is_partial'}}
                before=len(encoded(result));after=len(encoded({**result,'source_view':compact}))
                if after<before:
                    original=self.persist(view)
                    result['source_view']=compact
                    self.persist({'authority':'host','operation':'working_context_reduction',
                        'field':'source_view','original':original,'before_bytes':before,
                        'after_bytes':after,'removed_bytes':before-after,'limit_bytes':limit,
                        'retained':['path','source hash','line range','full cue reference'],
                        'deferred':['automatic diagnostic metadata'],
                        'retrieval':{'action':'read','path':view['path'],'page':0},'is_task_pass':False})
        if len(encoded(result)) > limit and 'diagnostic_access' in result:
            # These Host navigation descriptions duplicate the exact operation
            # schema/rules. Keep literal routes, not repeated explanatory prose.
            # In particular, the second dispatch guard includes its envelope;
            # satisfying the first selection alone is not sufficient.
            for field, route in (('history_access','observations/page'),
                                 ('catalog_access','source_catalog/page; read/path/page')):
                if len(encoded(result))<=limit:break
                value=result.get(field)
                if isinstance(value,str) and len(encoded(value))>len(encoded(route)):
                    before=len(encoded(result));original=self.persist({field:value})
                    result[field]=route;after=len(encoded(result))
                    self.persist({'authority':'host','operation':'working_context_reduction',
                        'field':field,'original':original,'before_bytes':before,'after_bytes':after,
                        'removed_bytes':before-after,'limit_bytes':limit,
                        'retained':['literal retrieval routes','unchanged operation schema'],
                        'deferred':['redundant Host navigation prose'],'is_task_pass':False})
            history=result.get('candidate_reasoning')
            if (len(encoded(result))>limit and isinstance(history,dict) and
                    set(history)<= {'recent_trials','trial_access','view_is_partial'} and
                    history.get('recent_trials')==[] and history.get('trial_access') is None):
                before=len(encoded(result));original=self.persist(history)
                result['candidate_reasoning']=None
                after=len(encoded(result))
                self.persist({'authority':'host','operation':'working_context_reduction',
                    'field':'candidate_reasoning','original':original,'before_bytes':before,
                    'after_bytes':after,'removed_bytes':before-after,'limit_bytes':limit,
                    'deferred':['empty automatic trial history'],
                    'retained':['all trial_review conditions'],'is_task_pass':False})
        # Catalog metadata is an automatic navigation aid, not requested
        # evidence or source. Retain all IDs/indexes and nonempty semantic
        # classifications; exact source metadata stays in Host history and is
        # also returned by the existing evidence retrieval operation.
        if len(encoded(result)) > limit and isinstance(result.get('catalog_view'), dict):
            catalog = result['catalog_view']
            if isinstance(catalog.get('items'), list) and all(
                    isinstance(item, dict) and 'index' in item and 'evidence_ids' in item
                    for item in catalog['items']):
                before = len(encoded(result))
                compact = {**catalog, 'items': [
                    {key: value for key, value in item.items()
                     if key in {'index', 'evidence_ids'} or
                     (key in {'finding', 'conditions', 'exceptions', 'prohibitions',
                              'uncertainty', 'unclassified'} and value)}
                    for item in catalog['items']], 'source_metadata_deferred': True}
                # Redundant unclassified IDs need not be copied twice. This is
                # explicit, not a claim of successful semantic classification.
                for item in compact['items']:
                    if item.get('unclassified') == item['evidence_ids']:
                        item.pop('unclassified')
                        item['all_unclassified'] = True
                after = len(encoded({**result, 'catalog_view': compact}))
                if after < before:
                    original = self.persist(catalog)
                    result['catalog_view'] = compact
                    receipt = {'authority': 'host', 'operation': 'working_context_reduction',
                        'field': 'catalog_view', 'original': original,
                        'before_bytes': before, 'after_bytes': after,
                        'removed_bytes': before-after, 'limit_bytes': limit,
                        'retained': ['indexes', 'evidence_ids', 'nonempty classifications'],
                        'deferred': ['source metadata', 'empty classification arrays'],
                        'retrieval': {'action': 'evidence', 'evidence_id': 'catalog ID', 'page': 0},
                        'is_task_pass': False}
                    # Receipt is Host-only; adding audit prose to the Worker
                    # packet would consume the bytes this operation saves.
                    self.persist(receipt)
        if len(encoded(result)) > limit:
            sizes={key:len(encoded(value)) for key,value in result.items()}
            raise HostDefect('PHASE_CONTEXT_DEFECT: mandatory working set exceeds budget; '
                f'bytes={len(encoded(result))}, limit={limit}, field_bytes={sizes}')
        return result

    def dispatch(self, payload, operation, feedback, previous, context_tokens, output_tokens, *, phase_name=None):
        """Budget the actual retry envelope, not merely the initial input.

        Exact rejected output stays in the immutable store. Semantic field
        corrections keep their declared previous values in feedback; an atomic
        tool retry can request the original object by its explicit page action.
        """
        ref=self.persist(previous) if previous is not None else None
        shown=previous
        # Atomic edit corrections already carry exact declared previous values
        # in Host feedback. Do not duplicate the whole rejected edit (including
        # unchanged source) in the retry envelope. Host retains it for merge and
        # the existing previous_output page route; never defer missing values.
        violations=(feedback or {}).get('violations',[])
        scoped_edit_correction=(previous is not None and
            operation['kind']=='contract_correction' and
            previous.get('action') in ('replace','patch','replace_target') and bool(violations) and
            all(isinstance(v,dict) and 'previous_value' in v for v in violations))
        if previous is not None and (operation['kind']!='contract_correction' or scoped_edit_correction):
            shown={'reference':ref,'view_is_partial':True,
                   'identity':{k:previous[k] for k in ('action',) if k in previous},
                   'request_example':{'action':'previous_output','page':0}}
            if scoped_edit_correction:
                # Correction scope/values come from feedback, and Host merges
                # against the retained object. Its path/hash identity is not a
                # Worker-selected input; the verified reference identifies it.
                shown.pop('identity')
        envelope={'operation':operation,'feedback':feedback,'previous_output':shown}
        # The actual envelope already includes operation/feedback/previous.
        # Do not ALSO reserve the legacy 1800 estimate for those same bytes.
        # 256 covers only outer keys and the bounded phase name. The final
        # dispatch still measures every byte against the unchanged 80% guard.
        composer=self.compose if payload.get('working_context_composition')==1 else self.select
        if payload.get('context_envelope_version')==1 and phase_name is not None:
            full={'phase':phase_name,'operation':operation,'input':{},'feedback':feedback,'previous_output':shown}
            selected=composer(payload,context_tokens,output_tokens,envelope_reserve=len(encoded(full))-2)
        else:
            selected=composer(payload,context_tokens,output_tokens+len(encoded(envelope)),envelope_reserve=256)
        return selected,shown,ref
