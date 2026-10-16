"""Host-owned exact source blocks. Worker supplies an ID, never original code.

Registry lives outside the editable workspace. IDs bind registry, scope, full
source, exact inclusive line range and original bytes. No fuzzy matching.
"""
import hashlib
from pathlib import Path, PurePosixPath
from .resolution import read_verified, write_verified


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class EditTargets:
    VERSION=1

    @classmethod
    def replay(cls,path,allowed,binding,namespace):
        """Verified historical reader; cannot issue an ID or mutate its archive."""
        result=cls.__new__(cls)
        result.path=Path(path);result.allowed=sorted(allowed)
        result.identity={'version':1,'namespace':namespace,'allowed_paths':result.allowed,'binding':binding}
        result.readonly=True;result._read()
        return result

    def __init__(self,path,allowed,binding):
        self.path=Path(path)
        self.allowed=sorted(allowed)
        for name in self.allowed:
            if not isinstance(name,str) or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                raise ValueError('Unsafe edit scope')
        self.identity={'version':1,'namespace':str(self.path.resolve()),'allowed_paths':self.allowed,'binding':binding}
        if self.path.exists():self._read()

    def _id(self,record):
        import json
        return 'T'+hashlib.sha256(json.dumps([self.identity,record],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def _read(self):
        data=read_verified(self.path) if self.path.exists() else {'identity':self.identity,'targets':{}}
        if data['identity']!=self.identity:raise ValueError('Target registry scope/binding mismatch')
        for identifier,record in data['targets'].items():
            if identifier!=self._id(record) or text_hash(record['original_text'])!=record['original_sha256']:
                raise ValueError('Target record hash mismatch')
        return data

    def issue(self,files,path,start_line,end_line):
        if path not in self.allowed:raise ValueError('Target path outside edit scope')
        text=files.get(path,'');lines=text.splitlines(True)
        if type(start_line) is not int or type(end_line) is not int or not 1<=start_line<=len(lines)+1 or not start_line-1<=end_line<=len(lines) or end_line-start_line>80:
            raise ValueError('Target requires one explicit bounded inclusive range')
        original=''.join(lines[start_line-1:end_line])
        record={'path':path,'source_sha256':text_hash(text),'start_line':start_line,
                'end_line':end_line,'original_sha256':text_hash(original),'original_text':original}
        identifier=self._id(record);data=self._read();data['targets'][identifier]=record
        if getattr(self,'readonly',False):
            if identifier not in self._read()['targets']:
                raise ValueError('Historical replay cannot issue a new target')
        else:write_verified(self.path,data)
        return {'target_id':identifier,**{k:v for k,v in record.items() if k!='original_text'}}

    def issue_unique(self,files,path,exact_text):
        """Migration only when literal original text uniquely covers whole lines."""
        if not isinstance(exact_text,str) or not exact_text or files.get(path,'').count(exact_text)!=1:
            raise ValueError('Ambiguous or absent exact target; no fuzzy migration')
        text=files[path];a=text.index(exact_text);b=a+len(exact_text)
        offsets=[0]
        for line in text.splitlines(True):offsets.append(offsets[-1]+len(line))
        if a not in offsets or b not in offsets:raise ValueError('Explicit whole-line target required')
        return self.issue(files,path,offsets.index(a)+1,offsets.index(b))

    def target(self,identifier):
        record=self._read()['targets'].get(identifier)
        if record is None:raise ValueError('Unknown target_id')
        return {'target_id':identifier,**{k:v for k,v in record.items() if k!='original_text'}}

    def resolve(self,files,response,limit):
        if not isinstance(response,dict) or set(response)!={'action','target_id','new_text'} or response['action']!='replace_target':
            raise ValueError('Return only action=replace_target, target_id, new_text')
        identifier=response['target_id'];data=self._read()
        if not isinstance(identifier,str) or identifier not in data['targets']:raise ValueError('Unknown target_id')
        record=data['targets'][identifier];path=record['path']
        if path not in self.allowed:raise ValueError('Target outside current edit scope')
        text=files.get(path,'')
        if text_hash(text)!=record['source_sha256']:raise ValueError('Stale target source hash; stop and obtain a new target')
        lines=text.splitlines(True);original=''.join(lines[record['start_line']-1:record['end_line']])
        if original!=record['original_text'] or text_hash(original)!=record['original_sha256']:
            raise ValueError('Target range/original hash mismatch')
        replacement=response['new_text']
        if not isinstance(replacement,str) or '\x00' in replacement:raise ValueError('new_text must be source text without NUL')
        try:encoded=replacement.encode('utf-8')
        except UnicodeError as exc:raise ValueError('new_text must be valid UTF-8') from exc
        if len(encoded)>limit:raise ValueError('Unchanged replacement byte limit exceeded')
        if replacement==original:raise ValueError('Target edit has no progress')
        if replacement and record['end_line']<len(lines) and not replacement.endswith(('\n','\r')):
            raise ValueError('Preserve line boundary before following source: nonempty new_text must end '
                'with a newline (JSON escape \\n); target '+identifier+' replaces lines '
                +str(record['start_line'])+'-'+str(record['end_line'])+' and line '
                +str(record['end_line']+1)+' remains. Correct new_text only; keep target_id and the '
                +str(limit)+'-byte contract limit.')
        return {'action':'patch','path':path,'source_sha256':record['source_sha256'],
                'start_line':record['start_line'],'end_line':record['end_line'],
                'replacement':replacement,'summary':'Host target '+identifier}


def target_schema():
    return {'type':'object','properties':{'action':{'type':'string','enum':['replace_target']},
        'target_id':{'type':'string'},'new_text':{'type':'string'}},
        'required':['action','target_id','new_text'],'additionalProperties':False}


class HistoricalTargets:
    """Read-only replay over verified generations; never accepts a stale edit."""
    def __init__(self,readers):self.readers=readers

    def target(self,identifier):
        for reader in self.readers:
            record=reader._read()['targets'].get(identifier)
            if record is not None:return {'target_id':identifier,**{k:v for k,v in record.items() if k!='original_text'}}
        raise ValueError('Unknown historical target')

    def issue(self,files,path,start_line,end_line):
        for reader in self.readers:
            try:return reader.issue(files,path,start_line,end_line)
            except ValueError:pass
        raise ValueError('Historical replay cannot issue a new target')

    def issue_unique(self,files,path,text):
        for reader in self.readers:
            try:return reader.issue_unique(files,path,text)
            except ValueError:pass
        raise ValueError('No verified historical whole-line target')

    def resolve(self,files,response,limit):
        for reader in self.readers:
            if response.get('target_id') in reader._read()['targets']:
                return reader.resolve(files,response,limit)
        raise ValueError('Unknown historical target')
