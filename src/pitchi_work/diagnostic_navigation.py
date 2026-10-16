"""Exact, scoped error-to-source navigation; never a diagnosis or solution."""
import hashlib,re
from .runner import HostDefect


def failure_source(receipt,files,page_source):
    """Locate an observed PHP/Python source position within the tested snapshot.

    A location in stderr is an ASSISTED navigation clue, not proven causality.
    Test verdicts and all original output remain in the Host receipt.
    """
    for item in receipt['results']:
        if item['passed']:continue
        for stream in ('stderr','stdout'):
            text=item['execution'].get(stream,'')
            matches=[]
            for name,source in files.items():
                patterns=[r'(?:^|/)'+re.escape(name)+r' on line (\d+)',
                    r'(?:^|/)'+re.escape(name)+r'["\x27], line (\d+)',
                    r'(?:^|/)'+re.escape(name)+r':(\d+)(?=[:\s]|$)']
                for index,pattern in enumerate(patterns):
                    for found in re.finditer(pattern,text,re.M):
                        matches.append((found.start(),name,int(found.group(1)),source,index))
            if not matches:continue
            # Native Python traceback order is caller -> failing frame. PHP's
            # primary "on line" report precedes its stack. This is format
            # navigation only, not a semantic claim that this caused failure.
            python=[m for m in matches if m[4]==1]
            _,name,line,source,_=max(python,key=lambda x:x[0]) if python else min(matches,key=lambda x:x[0])
            expected=hashlib.sha256(source.encode()).hexdigest()
            if receipt['source_hashes'].get(name)!=expected:
                raise HostDefect('PHASE_CHECKPOINT_DEFECT: diagnostic source differs from tested snapshot')
            if not 1<=line<=len(source.splitlines()):continue
            chunks=page_source(source)
            page=next((i for i,c in enumerate(chunks) if c['start_line']<=line<c['start_line']+max(1,len(c['text'].splitlines()))),None)
            if page is None:continue
            declarations=[(i,s) for i,s in enumerate(source.splitlines(),1)
                if i<=line and re.search(r'\b(?:function|def)\s+[A-Za-z_]\w*',s)]
            symbol=None
            if declarations:
                number,declaration=declarations[-1]
                # Only expose a complete bounded literal declaration. No
                # inferred type, intended implementation or proposed patch.
                symbol={'line':number,'declaration':declaration if len(declaration.encode())<=200 else None}
            # This is an automatic location cue, not an explicit whole-page
            # request. Preserve whole lines around the observed position under
            # a bounded view; original source/pages remain retrievable by hash.
            lines=source.splitlines(True);start=max(1,line-3);end=min(len(lines),line+3)
            while len(''.join(lines[start-1:end]).encode())>900 and (start<line or end>line):
                if end>line:end-=1
                elif start<line:start+=1
            text=''.join(lines[start-1:end]);oversized=len(text.encode())>900
            return {'kind':'diagnostic_source','path':name,'source_sha256':expected,
                'diagnostic_line':line,'source_page':page,'page_count':len(chunks),
                'start_line':start,'end_line':end,'text':'' if oversized else text,
                'oversized_line_requires_page_request':oversized,'view_is_partial':True,
                'full_source':False,'enclosing_declaration':symbol,
                'test':item['test'],'stream':stream,'interpretation':'ASSISTED navigation; not a proven root cause',
                'request_example':{'action':'read','path':name,'page':page}}
    return None
