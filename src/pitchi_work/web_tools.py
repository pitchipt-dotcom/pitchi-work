"""Reusable Host browser verification; no mission state or generated oracle."""
import hashlib
import json
from pathlib import Path
import uuid
from .store import RunStore,dump
from .mission_profiles import PROFILES,sha
from .runner import HostDefect
from .web_validation import observe


def verify_web(request,*,home=None,cancelled=lambda:False):
    from .screenshots import DEFAULT_HOME
    result={'capability':'web.verify','ok':False,'passed':False,'error':None,'verification_mode':'strict',
            'human_review':['usability','enjoyment'],'llm_calls':0}
    try:
        if not isinstance(request,dict) or set(request)-{'root','behavior'}:raise ValueError('Use root and registered behavior only')
        root=Path(request['root']).expanduser().resolve()
        behavior=request.get('behavior','interactive_smoke')
        if behavior not in PROFILES['static_web_app']['behavior_candidates']:raise ValueError('Unknown Host browser behavior')
        if not root.is_dir():raise ValueError('Missing Web workspace')
        for name in PROFILES['static_web_app']['output_candidates']:
            p=root/name
            if p.is_symlink() or not p.is_file() or p.stat().st_size>200000:raise ValueError('Missing/unsafe/oversized static app file')
        before={n:sha(root/n) for n in PROFILES['static_web_app']['output_candidates']}
        folder=Path(home or DEFAULT_HOME)/'tool_results'/uuid.uuid4().hex
        folder.mkdir(mode=0o700,parents=True)
        dump(folder/'state.json',{'workspace':str(root),'active_processes':{}})
        job=RunStore(folder)
        def check_cancel():
            if cancelled():
                from .store import Cancelled
                raise Cancelled()
        job.raise_if_cancelled=check_cancel
        contract={'task_id':'web','expected_outputs':PROFILES['static_web_app']['output_candidates'],
                  'behavior':behavior,'timeout':120}
        checks,executions,diffs=observe(job,contract,'tool')
        if any(sha(root/n)!=h for n,h in before.items()):raise HostDefect('Read-only source changed during verification')
        receipt={'checks':checks,'executions':executions,'artifact_hashes':before,'source_unchanged':True}
        dump(folder/'evidence.json',receipt)
        result.update(ok=True,passed=all(c['pass'] for c in checks),checks=checks,
                      evidence_reference=str(folder/'evidence.json'),evidence_sha256=sha(folder/'evidence.json'))
    except Exception as exc:
        from .store import Cancelled
        code='CANCELLED' if isinstance(exc,Cancelled) else getattr(exc,'failure_class','WEB_INPUT_ERROR')
        result['error']={'code':code,'owner':'host' if isinstance(exc,HostDefect) else 'tool','message':str(exc)}
    return result
