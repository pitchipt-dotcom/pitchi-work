"""Fresh copy and immutable provenance; never deployment or old-run mutation."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from datetime import datetime
from .store import RunStore,load,dump,now
from .host_contracts import TEMPLATES
from .host_validators import snapshot,save_bytes

SKIP={'.git','.venv','__pycache__','.aider.tags.cache.v4'}


def create_run(home,source,mission_run,endpoint,model,instruction='',parent=None):
    home=Path(home).resolve();source=Path(source).resolve();mission_run=Path(mission_run).resolve()
    if not source.is_dir():raise ValueError('Workspace does not exist')
    root=load(mission_run/'root_goal.json')
    if not root or root['requirements']!=load(mission_run/'requirements.json'):
        raise ValueError('A fixed, approved mission source is required')
    identifier=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    path=home/'runs'/identifier
    workspace=home/'workspaces'/identifier
    if workspace.is_relative_to(source):raise ValueError('Working copy cannot be nested inside source')
    path.mkdir(parents=True);workspace.mkdir(parents=True)
    source_hashes={}
    for file in sorted(source.rglob('*')):
        relative=file.relative_to(source)
        if any(p in SKIP for p in relative.parts):continue
        if file.is_symlink():raise ValueError('Explicit source copy required; symlinks are not followed: '+str(relative))
        if not file.is_file():continue
        target=workspace/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(file,target);source_hashes[relative.as_posix()]=hashlib.sha256(file.read_bytes()).hexdigest()
    # Namespace-only port in the NEW copy; preserve the frozen transport body.
    rebinding=[]
    for file in workspace.rglob('*.py'):
        if '__pycache__' in file.parts:continue
        text=file.read_text();tree=ast.parse(text);lines=text.splitlines(keepends=True)
        nodes=[n for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module=='bonsai_work_orchestrator.grounding']
        for node in reversed(sorted(nodes,key=lambda n:n.lineno)):
            old=''.join(lines[node.lineno-1:node.end_lineno])
            new=old.replace('bonsai_work_orchestrator.grounding','pitchi_work.grounding')
            lines[node.lineno-1:node.end_lineno]=[new]
        if nodes:
            before=hashlib.sha256(file.read_bytes()).hexdigest();save_bytes(file,''.join(lines).encode())
            rebinding.append({'artifact':file.relative_to(workspace).as_posix(),'before':before,'after':hashlib.sha256(file.read_bytes()).hexdigest(),'kind':'import_namespace_only'})
    (path/'artifacts').mkdir();(path/'tasks').mkdir();(path/'reviews').mkdir()
    save_bytes(path/'request.md',root['request'].encode());dump(path/'root_goal.json',root)
    dump(path/'requirements.json',root['requirements']);save_bytes(path/'instruction.md',instruction.encode())
    dump(path/'execution_context.json',{'version':1,'run_id':identifier,'active_workspace':str(workspace),
         'cwd':str(workspace),'artifact_base':str(workspace),'import_base':str(workspace),
         'source_project_bindings':[{'source':str(source),'access':'read_only_provenance'}]})
    dump(path/'source_provenance.json',{'source_workspace':str(source),'mission_source':str(mission_run),
         'parent_run':str(Path(parent).resolve()) if parent else None,'source_hashes':source_hashes,'adapter_rebinding':rebinding})
    dump(path/'artifacts/host_baseline.json',snapshot(workspace))
    dump(path/'state.json',{'run_id':identifier,'status':'READY','stage':'requirement','workspace':str(workspace),
         'created_at':now(),'parent_run':str(Path(parent).resolve()) if parent else None,
         'root_goal_sha256':hashlib.sha256((path/'root_goal.json').read_bytes()).hexdigest(),
         'local_model_endpoint':endpoint,'local_model':model,'orchestration_version':'pitchi-phase2',
         'active_processes':{},'root_replan_count':0})
    return RunStore(path)


def assert_source_unchanged(run):
    provenance=load(run.path/'source_provenance.json');source=Path(provenance['source_workspace'])
    for name,sha in provenance['source_hashes'].items():
        file=source/name
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=sha:
            raise RuntimeError('Source workspace changed: '+name)


def artifact_inputs(workspace,task):
    """Complete output files; exact AST retrieval function rather than truncation."""
    workspace=Path(workspace);files={}
    for name in task['expected_outputs']:
        file=workspace/name
        if file.is_file():files[name]=file.read_text()
    app=workspace/'search/app.py';text=app.read_text();tree=ast.parse(text)
    functions={n.name:ast.get_source_segment(text,n) for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='search_pages'}
    return {'output_files':files,'existing_retrieval_functions':functions,
            'references':{name:{'exists':(workspace/name).is_file(),'sha256':hashlib.sha256((workspace/name).read_bytes()).hexdigest() if (workspace/name).is_file() else None}
                          for name in task['input_files']}}
