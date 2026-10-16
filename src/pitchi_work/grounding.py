"""Reusable local-first evidence pipeline; no project/query-specific fixtures."""
import json,os,re,unicodedata,urllib.request,urllib.parse

INSUFFICIENT='十分な根拠がない'

def query_stems(question):
    text=unicodedata.normalize('NFKC',question).strip().rstrip('。?!！？ ')
    candidates=[text]
    suffix=re.compile(r'(?:について(?:教えて(?:ください)?|説明して(?:ください)?|知りたい)?|とは(?:何(?:ですか)?|どういう意味(?:ですか)?)?|(?:を)?(?:教えて|説明して)(?:ください)?|は何ですか|ですか|でしょうか|ください)$')
    core=text
    for _ in range(5):
        shorter=suffix.sub('',core).rstrip(' 。?!')
        if shorter==core:break
        core=shorter
    core=re.sub(r'[をがはの]$','',core).strip()
    candidates.append(core)
    # Only fallback to meaningful topic phrases, never interrogative particles.
    parts=re.split(r'[、,\s]+|について|とは|[をがはの]',core)
    interrogative={'説明','解説','内容','特徴','意味','方法','理由','仕組み','何ですか','教えて'}
    candidates.extend(sorted((p for p in parts if len(p)>=2 and p not in interrogative),key=len,reverse=True))
    return list(dict.fromkeys(c for c in candidates if len(c)>=2))

def normalize_evidence(evidence):
    rows=[]
    for original in evidence:
        row=dict(original);text=row.get('ocr_text',row.get('text'));title=row.get('book_title')
        page=row.get('pdf_page')
        if not isinstance(text,str) or not text.strip():raise ValueError('Evidence row has no OCR text')
        if not isinstance(title,str) or not title:raise ValueError('Evidence row has no exact book_title')
        if isinstance(page,bool) or not str(page).isdigit():raise ValueError('Evidence row has invalid pdf_page')
        row.update(ocr_text=text,book_title=title,pdf_page=int(page));rows.append(row)
    return rows

class LocalLLMAdapter:
    def generate(self,question,evidence):
        url=os.getenv('BOOK_AI_QA_LLM_URL') or f"http://{os.getenv('LLM_HOST','127.0.0.1')}:{os.getenv('LLM_PORT','8092')}/v1/chat/completions"
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('Private evidence may only be sent to the permitted local LLM')
        body={'model':os.getenv('BOOK_AI_QA_LLM_MODEL') or os.getenv('LLM_MODEL',''),
              'messages':[{'role':'system','content':'提示された根拠だけを使って質問に短く回答する。根拠が不足する場合だけinsufficient=true。事実を追加しない。JSONだけを返す。'},
                          {'role':'user','content':json.dumps({'question':question,'evidence':evidence},ensure_ascii=False)}],
              'temperature':0,'max_tokens':1200,'reasoning_budget':0,'chat_template_kwargs':{'enable_thinking':False},
              'response_format':{'type':'json_schema','json_schema':{'name':'grounded_answer','strict':True,'schema':{'type':'object','properties':{'answer':{'type':'string'},'insufficient':{'type':'boolean'}},'required':['answer','insufficient'],'additionalProperties':False}}}}
        request=urllib.request.Request(url,data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=90) as response:data=json.loads(response.read())
        choice=data['choices'][0]
        if choice.get('finish_reason')!='stop':raise RuntimeError('Local LLM response truncated or incomplete: '+str(choice.get('finish_reason')))
        result=json.loads(choice['message']['content'])
        if not isinstance(result.get('answer'),str) or not result['answer'].strip() or not isinstance(result.get('insufficient'),bool):
            raise ValueError('Invalid grounded answer schema')
        return result

class DeterministicAdapter:
    """Explicit test adapter, never the production/default model replacement."""
    def generate(self,question,evidence):
        return {'answer':'\n'.join(row['ocr_text'][:180] for row in evidence) if evidence else INSUFFICIENT,'insufficient':not bool(evidence)}

def answer_with_evidence(question,evidence,adapter=None):
    rows=normalize_evidence(evidence)
    if not rows:return {'answer':INSUFFICIENT,'insufficient':True,'citations':[]}
    result=(adapter or LocalLLMAdapter()).generate(question,rows)
    if result['insufficient']:return {'answer':INSUFFICIENT,'insufficient':True,'citations':[], 'evidence_count':len(rows)}
    citations=[{k:r[k] for k in ('book_title','pdf_page','source_file','book_pdf_page','citation_id') if k in r} for r in rows]
    text=result['answer']+'\n'+'\n'.join(f"出典: {r['book_title']} / PDF p.{r['pdf_page']}" for r in rows)
    return {'answer':text,'citations':citations,'insufficient':False}

def answer_question(question,retrieve,adapter=None,trace=None):
    if not isinstance(question,str) or not question.strip():raise ValueError('Question is required')
    if not callable(retrieve):
        raise TypeError('grounding.answer_question requires retrieve(stem)->list[rows], not evidence rows. For already retrieved rows use answer_with_evidence(question,rows,adapter).')
    rows=[];selected=None
    for stem in query_stems(question):
        rows=retrieve(stem)
        if trace:trace({'stage':'retrieval','stem':stem,'count':len(rows)})
        if rows:selected=stem;break
    normalized=normalize_evidence(rows)
    if trace:trace({'stage':'normalized_evidence','count':len(normalized),'citations':[{'book_title':r['book_title'],'pdf_page':r['pdf_page']} for r in normalized]})
    result=answer_with_evidence(question,normalized,adapter)
    if trace:trace({'stage':'answer','adapter':type(adapter or LocalLLMAdapter()).__name__,'evidence_count':len(normalized),'insufficient':result['insufficient'],'citations':result['citations']})
    return {**result,'query_stem':selected}
