"""Opt-in, bounded Host-reviewed abstract procedures; never answers/acceptance.

Raw cases remain in resolution.CaseStore. Publishing abstraction requires a
human review; this module cannot promote a raw case or execute its suggestions.
"""
from pathlib import Path
import json
from .resolution import read_verified, write_verified, digest
from .runner import HostDefect

FIELDS={'id','profile','applicability','checks','steps','success','avoid','reviewed'}

def validate(value):
    if not isinstance(value,dict) or set(value)!=FIELDS or value['reviewed'] is not True:
        raise HostDefect('PROCEDURE_INVALID: reviewed abstract schema required')
    if any(not isinstance(value[k],str) or not value[k] or len(value[k])>80 for k in ('id','profile')):
        raise HostDefect('PROCEDURE_INVALID: identity')
    for key in ('applicability','checks','steps','success','avoid'):
        if not isinstance(value[key],list) or not 1<=len(value[key])<=6 or any(not isinstance(x,str) for x in value[key]):
            raise HostDefect('PROCEDURE_INVALID: bounded procedure fields')
    if len(json.dumps(value,ensure_ascii=False).encode())>1800:
        raise HostDefect('PROCEDURE_INVALID: procedure budget')
    return value

class ProcedureStore:
    def __init__(self,home):self.path=Path(home)/'recovery_memory'/'abstract_procedures'
    def publish(self,value):
        value=validate(value);identifier=digest(value)
        write_verified(self.path/(identifier+'.json'),value)
        return identifier
    def automatic(self,profile):
        """Deterministic, reviewed abstract-only profile matching, never raw cases."""
        candidates={}
        for path in sorted(Path(__file__).with_name('stable_procedures').glob('*.json')):
            value=validate(json.loads(path.read_text()))
            if value['profile']==profile:candidates[digest(value)]=value
        if self.path.is_symlink():raise HostDefect('PROCEDURE_INVALID: unsafe memory store')
        for path in sorted(self.path.glob('*.json')):
            if path.is_symlink():raise HostDefect('PROCEDURE_INVALID: unsafe memory reference')
            value=validate(read_verified(path))
            if digest(value)!=path.stem:raise HostDefect('PROCEDURE_INVALID: hash mismatch')
            if value['profile']==profile:candidates[path.stem]=value
        # Stable, bounded ordering; unrelated profiles are never selected.
        chosen=sorted(candidates,key=lambda ref:(candidates[ref]['id'],ref))[:2]
        for ref in chosen:self.publish(candidates[ref])
        self.select(profile,chosen)
        return chosen
    def select(self,profile,identifiers):
        if not isinstance(identifiers,list) or len(identifiers)>2 or len(set(identifiers))!=len(identifiers):
            raise HostDefect('PROCEDURE_INVALID: at most two explicit reviewed procedures')
        result=[]
        for identifier in identifiers:
            if not isinstance(identifier,str) or len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier):
                raise HostDefect('PROCEDURE_INVALID: reference')
            path=self.path/(identifier+'.json')
            if self.path.is_symlink() or path.is_symlink() or not path.is_file():
                raise HostDefect('PROCEDURE_INVALID: missing or unsafe memory reference')
            value=validate(read_verified(path))
            if digest(value)!=identifier or value['profile']!=profile:
                raise HostDefect('PROCEDURE_INVALID: hash/profile mismatch')
            result.append({'reference':identifier,'procedure':value,'authority':'assisted_procedure_only'})
        return result

def pinned_procedures(run,contract,base):
    policy=run.state().get('procedure_policy')
    if not policy:return []
    rows=ProcedureStore(run.path.parent.parent).select(contract['profile'],policy.get('references'))
    pin=Path(base)/'procedures.json'
    if pin.exists():
        if read_verified(pin)!=rows:raise HostDefect('PROCEDURE_INVALID: checkpoint knowledge changed')
    else:write_verified(pin,rows)
    run.event('host','procedure_retrieval',references=[r['reference'] for r in rows],
              count=len(rows),authority='assisted_procedure_only',body_logged=False)
    return rows
