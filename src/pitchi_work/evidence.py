"""Hash-verified saved Host evidence. Reading is not live collection."""
import hashlib
import json
from pathlib import Path
from .store import load
from .host_contracts import TEMPLATES
from .host_validator_checks import require


def saved_ref(run_path, name, expected=None):
    base=Path(run_path).resolve();path=(base/name).resolve()
    require(path.is_relative_to(base) and path.is_file(), 'Missing/outside saved evidence reference')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if expected is not None:require(digest==expected,'Saved evidence hash mismatch')
    return {'artifact':name,'sha256':digest}


def verify_receipt(run_path, task, test):
    require(test['command']=='host-validator:'+task['template_id'],'Unregistered validator command')
    refs={p['evidence_artifact'] for p in test.get('host_execution_evidence',[])}
    require(bool(refs),'No saved Host evidence')
    registry=test['registry_result']
    require(registry.get('mode')=='live','Offline checks are not acceptance evidence')
    hashes=[]
    for ref in sorted(refs):
        hashes.append(saved_ref(run_path,ref))
        require(load(Path(run_path)/ref)==registry,'Embedded result differs from saved Host receipt')
    expected=set(TEMPLATES[task['template_id']]['validators'])
    measured={c['validator'] for c in registry['checks'] if c.get('pass') is True}
    require(expected<=measured and registry.get('passed') is True and test.get('exit_code')==0,'Host check did not PASS')
    for call in registry.get('live_calls',[]):
        for name in ('request','context','response'):
            hashes.append(saved_ref(run_path,call[name+'_artifact'],call[name+'_sha256']))
        request=load(Path(run_path)/call['request_artifact'])
        response=load(Path(run_path)/call['response_artifact'])
        context=load(Path(run_path)/call['context_artifact'])
        require(json.loads(request['messages'][-1]['content'])==context,'Saved model context mismatch')
        require(call.get('http_status')==200 and response.get('model')==call.get('model'),'Invalid actual local model receipt')
        require(response['choices'][0]['finish_reason']=='stop','Incomplete live response')
    return hashes
