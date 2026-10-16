"""Existing artifacts are immutable source inputs for new Host-owned missions."""
from pathlib import Path
import fcntl
from .store import load,dump,now
from .artifacts import data_dir,digest,PreviewError,operation
from .mission_profiles import sha


def source_context(home,identifier,operation_name):
    if operation_name not in ('repair','modify','clone'):raise PreviewError('Select repair, modify or clone')
    registry=load(data_dir(home)/'registry.json',{})
    if identifier=='latest-web-app':
        ids=sorted((i for i,r in registry.items() if r['kind']=='static_web_run' and not r.get('test_fixture')),
                   key=lambda i:registry[i].get('current_run_id',registry[i].get('run_id','')))
        if not ids:raise PreviewError('No Web artifact registered')
        identifier=ids[-1]
    row=registry.get(identifier)
    if not row or row['kind'] not in ('static_web_run','static_html'):raise PreviewError('Only registered static artifacts are editable')
    root=Path(row['root']).resolve();manifest=row['manifest']
    if set(manifest)!={'index.html','app.js','style.css'}:raise PreviewError('Current finite Web profile requires index.html/app.js/style.css')
    if any(not (root/n).is_file() or (root/n).is_symlink() or sha(root/n)!=h for n,h in manifest.items()):raise PreviewError('Artifact source hash changed; refusing stale input')
    current_run_id=row.get('current_run_id',row.get('run_id'))
    previous_requirements=load(Path(home)/'runs'/str(current_run_id)/'requirements.json',{})
    return {'artifact_id':identifier,'operation':operation_name,'root':str(root),'source_run_id':row.get('source_run_id',row.get('run_id')),
            'current_run_id':row.get('current_run_id',row.get('run_id')),'revision':row.get('revision',1),
            'required_regression_behavior':previous_requirements.get('behavior'),
            'source_hashes':manifest,'registry_sha256':digest(row),'parent_artifact_id':row.get('parent_artifact_id')}


def create_artifact_mission(home,identifier,operation_name,request,endpoint,model):
    from .mission_controller import create_mission
    if not isinstance(request,str) or not request.strip():raise PreviewError('Nonempty new request required')
    context=source_context(home,identifier,operation_name)
    preview=operation(home,'preview',context['artifact_id'])
    run=create_mission(home,context['root'],request,endpoint,model)
    context['source_preview']=preview
    dump(run.path/'artifact_input.json',context)
    run.save_state(input_artifact_id=context['artifact_id'],artifact_operation=operation_name)
    run.trace('host','ARTIFACT_INPUT','Resolved immutable artifact source',artifact_id=context['artifact_id'],revision=context['revision'])
    return run


def diagnose(run,contract):
    """Save original failure conditions before any Worker writes."""
    from .web_tools import verify_web
    context=load(run.path/'artifact_input.json',{})
    if not context:return None
    path=run.path/'artifacts/artifact_diagnosis.json'
    if path.exists():return load(path)
    result=verify_web({'root':run.state()['workspace'],'behavior':contract['behavior']},
                      home=run.path.parent.parent,cancelled=lambda:(run.path/'cancel_request.json').exists())
    run.raise_if_cancelled()
    if not result['ok']:raise PreviewError('Host diagnosis failed: '+str(result.get('error')))
    from .code_validation import snapshot
    evidence={'artifact_id':context['artifact_id'],'revision':context['revision'],'source_hashes':snapshot(run.state()['workspace']),
              'original_artifact_hashes':context['source_hashes'],
              'operation':context['operation'],'passed':result['passed'],'checks':result['checks'],
              'evidence_reference':result['evidence_reference'],'evidence_sha256':result['evidence_sha256'],
              'host_receipt':load(Path(result['evidence_reference'])),'preview':context['source_preview']}
    dump(path,evidence)
    return evidence


def summary(run):
    evidence=load(run.path/'artifacts/artifact_diagnosis.json',{})
    if not evidence:return None
    return {k:evidence[k] for k in ('artifact_id','revision','source_hashes','passed','checks','evidence_reference','evidence_sha256')}|{
        'observations':[{k:v for k,v in e.get('web_observation',{}).items() if k in {'errors','observation','review_observation'}} for e in evidence['host_receipt']['executions']],
        'errors':[e.get('stderr','')[-1000:] for e in evidence['host_receipt']['executions'] if e.get('exit_code')]}


def publish_revision(home,run,identifier):
    """Move a logical artifact pointer only after Host acceptance; keep prior files."""
    context=load(run.path/'artifact_input.json',{})
    if not context:return identifier
    folder=data_dir(home)
    with (folder/'registry.lock').open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        registry=load(folder/'registry.json',{});parent=registry[context['artifact_id']]
        if digest(parent)!=context['registry_sha256']:raise PreviewError('Concurrent artifact revision changed; no overwrite')
        if any(sha(Path(context['root'])/n)!=h for n,h in context['source_hashes'].items()):raise PreviewError('Original artifact changed; no update')
        row=registry[identifier]
        target=identifier if context['operation']=='clone' else context['artifact_id']
        history=parent.get('revisions',[])+[{k:v for k,v in parent.items() if k!='revisions'}]
        row.update(artifact_id=target,source_run_id=context['source_run_id'],current_run_id=run.run_id,
                   test_fixture=parent.get('test_fixture',False),
                   parent_artifact_id=context['artifact_id'] if context['operation']=='clone' else context['parent_artifact_id'],
                   revision=1 if context['operation']=='clone' else context['revision']+1,
                   operation=context['operation'],created_at=now() if context['operation']=='clone' else parent.get('created_at',now()),
                   updated_at=now(),revisions=[] if context['operation']=='clone' else history,
                   lineage={'parent_artifact_id':context['artifact_id'],'parent_revision':context['revision']},
                   title=parent['title']+' (clone)' if context['operation']=='clone' else parent['title'])
        registry[target]=row
        if identifier!=target:del registry[identifier]
        dump(folder/'registry.json',registry)
    if target==context['artifact_id']:operation(home,'stop',target)
    dump(run.path/'artifacts/artifact_revision.json',{k:row[k] for k in ('artifact_id','source_run_id','current_run_id','parent_artifact_id','revision','operation','lineage')})
    return target


def rollback_publication(home,run,identifier):
    """Restore the pointer on infrastructure failure; never edit source content."""
    folder=data_dir(home)
    try:operation(home,'stop',identifier)
    except PreviewError:pass
    with (folder/'registry.lock').open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        registry=load(folder/'registry.json',{});row=registry.get(identifier,{})
        if row.get('current_run_id')!=run.run_id:raise PreviewError('Concurrent update prevents pointer rollback')
        if row['operation']=='clone':del registry[identifier]
        else:registry[identifier]=row['revisions'][-1]
        dump(folder/'registry.json',registry)
    if row['operation']!='clone':operation(home,'preview',identifier)
