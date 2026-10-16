"""Literal, deterministic navigation over Host-bound source, not a diagnosis."""
import re
import json


def serialized_pages(text,limit):
    from .phase_workflow import pages
    raw_limit=limit
    while True:
        chunks=pages(text,raw_limit)
        if all(len(json.dumps(p,ensure_ascii=False,separators=(',',':')).encode())<=limit for p in chunks):return chunks
        if raw_limit<=16:raise ValueError('Source page metadata cannot fit the fixed serialized budget')
        raw_limit=max(16,raw_limit//2)


def outline(text,chunks,limit=12):
    result=[]
    for number,line in enumerate(text.splitlines(),1):
        match=re.search(r'\b(?:function|def|class)\s+([A-Za-z_][A-Za-z_0-9]*)',line)
        if not match:continue
        for page,chunk in enumerate(chunks):
            if chunk['start_line']<=number<=chunk['start_line']+max(0,len(chunk['text'].splitlines())-1):
                result.append({'symbol':match.group(1),'line':number,'page':page});break
        if len(result)>=limit:break
    return result


def search(files,term,page_function,limit=8):
    if not isinstance(term,str) or not 1<=len(term.encode())<=120:raise ValueError('Literal search term must be 1..120 UTF-8 bytes')
    hits=[];total=0
    for name,text in sorted(files.items()):
        for page,chunk in enumerate(page_function(text)):
            for offset,line in enumerate(chunk['text'].splitlines()):
                if term in line:
                    total+=1
                    if len(hits)<limit:hits.append({'path':name,'page':page,'line':chunk['start_line']+offset,'text':line[:160]})
    return {'term':term,'hits':hits,'total':total,'partial':total>len(hits),'authority':'host','interpretation':'ASSISTED'}
