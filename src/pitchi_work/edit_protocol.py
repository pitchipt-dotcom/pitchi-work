"""Host-owned operation serialization only, never a solution or acceptance."""
import hashlib


def target_rules():
    return ('One operation. Edit only with replace_target(target_id,new_text), '
        'or request edit_target(path,source_sha256,start_line,end_line). '
        'Do not return old_text or legacy patch. Read needed source/Evidence first. '
        'Deferred target metadata is not edit permission; select a smaller explicit range until its full body is visible. '
        'new_text <=900 UTF-8 bytes. If a nonempty replacement has following source lines, '
        'end new_text with a newline (JSON escape \\n); otherwise code joins the following line. '
        'For a larger change, split it into multiple small replace_target operations. '
        'Each edit must leave syntactically complete valid source, not an unfinished fragment. '
        'After each edit, use the updated source hash and obtain a fresh target for the next block. '
        'Use existing navigation/diagnostic actions. done requests Host validation, not PASS. '
        'No repeated unchanged reads or edits.')


def target_dispatch(payload, files, targets):
    """Upgrade only a live dispatch; pinned historical packets stay unchanged."""
    value=dict(payload)
    value['rules']=target_rules()
    if value.get('guided_step'):
        value['rules']+=' This is a Codex-designed guided replacement. Return ONLY replace_target for the visible target. Copy input.guided_step.new_text exactly as the complete replacement. A trailing function declaration is a boundary line whose original body follows outside the target; do not add or complete that body. Syntax validity is checked on the entire resulting file, not on this replacement alone.'
    value['operation_protocol']={'editing':'replace_target','authority':'Host scope/hash/range validation; unchanged acceptance'}
    view=value.get('source_view')
    if isinstance(view,dict) and view.get('kind')=='edit_target' and view.get('edit_target'):
        target=view['edit_target']
        verified=targets.issue(files,target['path'],target['start_line'],target['end_line'])
        from .edit_targets import HistoricalTargets
        if isinstance(targets,HistoricalTargets):
            if targets.target(target['target_id'])!=target or any(
                    verified[k]!=target[k] for k in verified if k!='target_id'):
                raise ValueError('Historical target/source binding changed')
        elif verified!=target:raise ValueError('Live target/source binding changed')
        text=''.join(files[target['path']].splitlines(True)[target['start_line']-1:target['end_line']])
        value['source_view']={**view,'text':text}
        for key in ('body_deferred','requires_source_read_and_target_selection','context_id','request'):
            value['source_view'].pop(key,None)
    return value


def atomic_feedback(feedback):
    """Remove duplicated navigation hints, never correction values/evidence."""
    if not feedback or feedback.get('violations'):return feedback
    value=dict(feedback)
    value.pop('retry_rule',None)  # phase_operation defines execution vs correction.
    examples=value.get('response_examples')
    if isinstance(examples,list):
        value['response_examples']=list({repr(v):v for v in examples}.values())
    # Symbols remain obtainable through the existing navigation action.
    resolution=value.get('source_resolution')
    if isinstance(resolution,dict) and 'symbols' in resolution:
        value['source_resolution']={k:v for k,v in resolution.items() if k!='symbols'}
        value['source_resolution']['symbols_request']={'action':'navigation','page':0}
    return value


def visible_targets(payload,files,targets):
    """Permission comes from an exact complete view, never opaque metadata."""
    view=payload.get('source_view') or {}
    target=view.get('edit_target')
    if not target or not isinstance(view.get('text'),str) or view.get('body_deferred'):return []
    current=targets.issue(files,target['path'],target['start_line'],target['end_line'])
    original=''.join(files[target['path']].splitlines(True)[target['start_line']-1:target['end_line']])
    if current!=target or view['text']!=original:raise ValueError('Visible target does not match Host source/hash/range')
    return [target['target_id']]


def dispatch_schema(schema,identifiers):
    """Same editing permission in prompt, grammar and live Host validation."""
    import copy
    result=[]
    for spec in schema['oneOf']:
        action=spec['properties']['action']['enum'][0]
        if action in ('replace','patch'):continue
        if action=='replace_target':
            if not identifiers:continue
            spec=copy.deepcopy(spec)
            spec['properties']['target_id']['enum']=identifiers
        result.append(spec)
    return {'oneOf':result}


def operation_schema(files,allowed,evidence_count,*,navigation=False,diagnostics=False,reconsider=False,review_decision=False,current_test_reference=None,resolution=False,targets=False,target_only=False):
    if target_only and not targets:raise ValueError('Target-only protocol requires a Host registry')
    def obj(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    action=lambda name:{'type':'string','enum':[name]}
    paths=sorted(files)
    schemas=[obj({'action':action('done'),'summary':{'type':'string'}}),
             obj({'action':action('read'),'path':{'type':'string','enum':paths},'page':{'type':'integer','minimum':0}}),
             obj({'action':action('source_catalog'),'page':{'type':'integer','minimum':0}}),
             obj({'action':action('observations'),'page':{'type':'integer','minimum':0}})]
    if evidence_count:schemas.append(obj({'action':action('evidence'),'index':{'type':'integer','minimum':0,'maximum':evidence_count-1}}))
    if resolution:schemas.append(obj({'action':action('advice'),'page':{'type':'integer','minimum':0}}))
    if navigation:
        schemas.append(obj({'action':action('source_search'),'term':{'type':'string','minLength':1,'maxLength':120}}))
        schemas.append(obj({'action':action('navigation'),'page':{'type':'integer','minimum':0}}))
    if diagnostics:
        for name in ('previous_output','test_result'):
            schemas.append(obj({'action':action(name),'page':{'type':'integer','minimum':0}}))
    if reconsider:
        proposal={'action':action('reconsider'),'hypothesis':{'type':'string'},
            'paths':{'type':'array','minItems':1,'maxItems':len(allowed),'uniqueItems':True,
                     'items':{'type':'string','enum':list(allowed)}},
            'test_reference':{'type':'string',**({'enum':[current_test_reference]} if current_test_reference is not None else {})}}
        if review_decision:
            schemas.append(obj({'action':action('reconsider'),'decision':{'type':'string','enum':['maintain']},
                                'test_reference':proposal['test_reference']}))
            schemas.append(obj({'action':proposal['action'],'decision':{'type':'string','enum':['revise','reject']},
                                **{k:v for k,v in proposal.items() if k!='action'}}))
        else:schemas.append(obj(proposal))
        schemas.append(obj({'action':action('trial_result'),'step':{'type':'integer','minimum':0},
            'page':{'type':'integer','minimum':0}}))
    for name in ([] if target_only else allowed):
        schemas.append(obj({'action':action('replace'),'path':{'type':'string','enum':[name]},
            'source_sha256':{'type':'string','enum':[hashlib.sha256(files.get(name,'').encode()).hexdigest()]},
            'old_text':{'type':'string','minLength':1},'new_text':{'type':'string'},'summary':{'type':'string'}}))
        schemas.append(obj({'action':action('patch'),'path':{'type':'string','enum':[name]},
            'source_sha256':{'type':'string','enum':[hashlib.sha256(files.get(name,'').encode()).hexdigest()]},
            'start_line':{'type':'integer','minimum':1},'end_line':{'type':'integer','minimum':0},
            'replacement_lines':{'type':'array','maxItems':81,'items':{'type':'string','pattern':r'^[^\r\n]*$'}},
            'summary':{'type':'string'}}))
    if targets:
        from .edit_targets import target_schema
        schemas.append(target_schema())
        schemas.append(obj({'action':action('edit_target'),'path':{'type':'string','enum':list(allowed)},
            'source_sha256':{'type':'string'},'start_line':{'type':'integer','minimum':1},'end_line':{'type':'integer','minimum':0}}))
    return {'oneOf':schemas}


def replace_exact(text,old,new,limit):
    if not isinstance(old,str) or not old or not isinstance(new,str):
        raise ValueError('Exact replacement requires nonempty old_text and string new_text')
    if len(new.encode())>limit:raise ValueError('Exact replacement exceeds unchanged UTF-8 byte bound')
    if text.count(old)!=1:raise ValueError('old_text must match exactly once in the current source; read the exact source first')
    if old==new:raise ValueError('Exact replacement has no progress')
    return text.replace(old,new,1)


def render_lines(value,following_line,limit):
    if not isinstance(value,list) or len(value)>81 or any(not isinstance(line,str) or '\n' in line or '\r' in line for line in value):
        raise ValueError('replacement_lines must be at most 81 strings, each one literal source line')
    result='\n'.join(value)
    if value and following_line:result+='\n'
    if len(result.encode())>limit:raise ValueError('Host-rendered replacement exceeds unchanged UTF-8 byte bound')
    return result
