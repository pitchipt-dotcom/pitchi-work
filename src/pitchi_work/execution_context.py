import hashlib
from pathlib import Path


class WorkspaceMismatch(ValueError):
    failure_class='VERIFIER_DEFECT'

def context(run):
    workspace=Path(run.state()['workspace']).resolve()
    saved=run.path/'execution_context.json'
    if saved.exists():
        result=__import__('json').loads(saved.read_text())
        if result['active_workspace']!=str(workspace):raise WorkspaceMismatch('Active workspace mismatch')
        return result
    return {'version':1,'run_id':run.run_id,'active_workspace':str(workspace),'cwd':str(workspace),'artifact_base':str(workspace),'import_base':str(workspace),'source_project_bindings':[]}

def assert_workspace(run,workspace):
    if str(Path(workspace).resolve())!=context(run)['active_workspace']:raise WorkspaceMismatch('Stage cwd mismatch')

def artifact_snapshot(workspace,task):
    result={}
    for name in dict.fromkeys(task.get('input_files',[])+task.get('expected_outputs',[])):
        path=(workspace/name).resolve()
        if not path.is_relative_to(workspace):raise WorkspaceMismatch('Contract artifact outside active workspace')
        result[name]={'exists':path.is_file(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
                     'ownership':'output' if name in task.get('expected_outputs',[]) else 'input'}
    return result
