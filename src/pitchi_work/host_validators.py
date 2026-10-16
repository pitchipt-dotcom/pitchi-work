"""Host-owned book-QA registry. No Worker-authored assertion defines a verdict."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import tempfile
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from . import host_validator_checks as predicates

POSITIVE='リハビリテーションの実際について'
NEGATIVE='宇宙飛行士の月面着陸記録について'
PREFIX='HOST_REGISTRY_RESULT='


class HostCollectorDefect(RuntimeError):
    """Persistence/collection failure owned by the Host, not a Worker verdict."""


def save_bytes(path, value):
    temporary=None
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
        with os.fdopen(fd,'wb') as handle:
            handle.write(value);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path);temporary=None
    except OSError as exc:
        raise HostCollectorDefect('Host evidence persistence failed: '+str(exc)) from exc
    finally:
        if temporary is not None:Path(temporary).unlink(missing_ok=True)


def hashfile(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(workspace):
    return {p.relative_to(workspace).as_posix():hashfile(p) for p in workspace.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts}


def validate(workspace,run_path,template):
    from pitchi_work.host_contracts import TEMPLATES
    from pitchi_work.grounding import query_stems
    from .store import load,dump
    workspace=Path(workspace).resolve();run_path=Path(run_path).resolve()
    assert Path.cwd().resolve()==workspace,'Active workspace mismatch'
    catalog=TEMPLATES[template];checks=[];calls=[];proofs=[]
    collection_id=template+'_'+str(time.time_ns())
    collection_folder=run_path/'artifacts'/('collection_'+collection_id)
    def save(path,value):
        save_bytes(path,json.dumps(value,ensure_ascii=False,indent=2).encode())
    def reference(path):return str(path.relative_to(run_path))
    def check(name,fn):
        try:
            value=fn();checks.append({'validator':name,'pass':True,'details':value});return value
        except Exception as exc:
            checks.append({'validator':name,'pass':False,'error':type(exc).__name__+': '+str(exc)});raise
    baseline=load(run_path/'artifacts/host_baseline.json')
    allowed={p for t in TEMPLATES.values() for p in t['outputs']}
    def scope():
        return predicates.scope(workspace,baseline,allowed)
    def syntax():
        return predicates.syntax(workspace,catalog['outputs'])
    originals=urllib.request.urlopen
    class Response:
        def __init__(self,response,request,entry,folder):
            self.response=response;self.request=request;self.body=b'';self.entry=entry;self.folder=folder
        def __getattr__(self,name):return getattr(self.response,name)
        def __enter__(self):self.response.__enter__();return self
        def read(self,*args):
            data=self.response.read(*args);self.body+=data;return data
        def __exit__(self,*args):
            try:
                path=self.folder/'response.json';save_bytes(path,self.body)
                self.entry.update(response_artifact=reference(path),response_sha256=hashlib.sha256(self.body).hexdigest(),
                                  http_status=self.response.status,finished_at=time.time())
                data=json.loads(self.body);choice=data['choices'][0]
                if choice.get('finish_reason')!='stop':raise RuntimeError('Incomplete live response')
                if 'qwen' not in data.get('model','').lower():raise RuntimeError('Response was not the configured comparison model')
                request=json.loads(self.request.data)
                context=json.loads(request['messages'][-1]['content'])
                predicates.require(isinstance(context.get('evidence'),list) and context['evidence'],'No real evidence at adapter input')
                result=json.loads(choice['message']['content'])
                self.entry.update({'model':data['model'],'usage':data.get('usage',{}),
                              'request_sha256':hashlib.sha256(self.request.data).hexdigest(),
                              'response_sha256':hashlib.sha256(self.body).hexdigest(),
                              'question':context['question'],'evidence':context['evidence'],'result':result,
                              'finish_reason':choice.get('finish_reason')})
                save(self.folder/'receipt.json',self.entry)
            finally:self.response.__exit__(*args)
    def observe(request,*args,**kwargs):
        url=request.full_url if isinstance(request,urllib.request.Request) else str(request)
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'}:
            raise RuntimeError('Host transport policy: external transport forbidden')
        if parsed.port==8721:
            raise AssertionError('Original prohibition: production search port 8721 must not be used for tests')
        if parsed.path.endswith('/chat/completions'):
            if url!=os.environ['BOOK_AI_QA_LLM_URL']:raise RuntimeError('Host model endpoint configuration mismatch')
            folder=collection_folder/('call_'+str(len(calls)+1))
            request_path=folder/'request.json';save_bytes(request_path,request.data)
            context=json.loads(json.loads(request.data)['messages'][-1]['content'])
            context_path=folder/'context.json';save(context_path,context)
            entry={'request_artifact':reference(request_path),'context_artifact':reference(context_path),
                   'request_sha256':hashlib.sha256(request.data).hexdigest(),
                   'context_sha256':hashfile(context_path),'started_at':time.time()}
            calls.append(entry)
            try:response=originals(request,*args,**kwargs)
            except Exception as exc:
                entry.update(error=type(exc).__name__+': '+str(exc));save(folder/'receipt.json',entry);raise
            return Response(response,request,entry,folder)
        return originals(request,*args,**kwargs)
    def citations(result,question):
        retrieval=[]
        for stem in query_stems(question):retrieval+=app.search_pages(stem,None,0,50)[1]
        return predicates.citations(workspace,result,question,calls,retrieval)
    servers=[]
    def server(handler):
        service=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start();servers.append((service,thread))
        return 'http://127.0.0.1:'+str(service.server_port)
    def post(url,question):
        req=urllib.request.Request(url+'/qa',data=json.dumps({'question':question}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=150) as r:
            assert r.status==200;return json.loads(r.read())
    outcome={}
    try:
        check('V1',scope);check('V2',syntax)
        sys.path.insert(0,str(workspace/'search'))
        app=importlib.import_module('app')
        urllib.request.urlopen=observe
        if template=='T-QA':
            qa_api=importlib.import_module('qa_api');url=server(qa_api.QAHandler)
            before=len(calls)
            positive=check('V3',lambda:post(url,POSITIVE))
            predicates.live_response(positive,calls[before:])
            check('V4',lambda:citations(positive,POSITIVE))
            def insufficient():
                counts=[app.search_pages(s,None,0,50)[0] for s in query_stems(NEGATIVE)]
                answer=post(url,NEGATIVE)
                return predicates.insufficient(answer,counts)
            check('V5',insufficient)
            proofs=['live_local_llm','live_http','live_db']
        elif template=='T-SEARCH':
            url=server(app.Handler)
            def search():
                stem=query_stems(POSITIVE)[-1];count,rows=app.search_pages(stem,None,0,50)
                with urllib.request.urlopen(url+'/search?'+urllib.parse.urlencode({'q':stem}),timeout=20) as response:
                    body=json.loads(response.read())
                return {'query':stem,'http_response':body,**predicates.search(workspace,count,rows,body)}
            check('V6',search);proofs=['live_http','live_db']
        else:
            from pitchi_work.host_artifact_producer import produce
            qa=importlib.import_module('qa');original=qa.answer_question;observed=[]
            def capture(*args,**kwargs):
                assert kwargs.get('adapter') is None and (len(args)<3 or args[2] is None),'Injected adapter forbidden'
                result=original(*args,**kwargs);observed.append({'question':args[0],'result':result});return result
            qa.answer_question=capture
            def record():
                produce(workspace,POSITIVE,qa,app.search_pages,save)
                outcome['observed_returns']=observed
                save(collection_folder/'observed_returns.json',observed)
                saved=json.loads((workspace/'search/qa_artifacts.json').read_text())
                retrieval=[]
                for stem in query_stems(POSITIVE):retrieval+=app.search_pages(stem,None,0,50)[1]
                value=predicates.record(workspace,saved,observed,calls,POSITIVE,retrieval)
                outcome['artifact_provenance']={'artifact':'search/qa_artifacts.json',
                    'artifact_sha256':hashfile(workspace/'search/qa_artifacts.json'),
                    'producer_owner':'host','producer_sha256':hashfile(Path(__file__).with_name('host_artifact_producer.py')),
                    'qa_sha256':hashfile(workspace/'search/qa.py'),'app_sha256':hashfile(workspace/'search/app.py'),
                    'database_sha256':hashfile(workspace/'search/library.sqlite'),
                    'source_receipts':[c['response_artifact'] for c in calls],
                    'actual_return_sha256':hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
                save(collection_folder/'artifact_provenance.json',outcome['artifact_provenance'])
                return value
            check('V7',record);proofs=['live_local_llm','live_db']
        check('V1',scope)
    except Exception as exc:
        outcome['error']=type(exc).__name__+': '+str(exc)
        # Assertions denote explicit behavioral criteria. Failures in Host
        # execution/import/transport plumbing are not Worker REWORK evidence.
        frames=traceback.extract_tb(exc.__traceback__)
        origin=Path(frames[-1].filename).resolve() if frames else None
        worker_origin=origin is not None and origin.is_relative_to(workspace)
        missing_existing=False
        if isinstance(exc,ModuleNotFoundError):
            module=(exc.name or '').split('.')[0]
            missing_existing=any((workspace/base/(module+'.py')).is_file() for base in ('','search'))
        # An undeclared missing dependency imported by a Worker artifact is a
        # Worker defect. An existing module invisible to the runner is Host.
        worker_error=not isinstance(exc,HostCollectorDefect) and (isinstance(exc,(predicates.CriterionFailure,AssertionError,SyntaxError)) or (
            worker_origin and not missing_existing) or isinstance(exc.__cause__,SystemExit))
        outcome['failure_owner']='worker' if worker_error else 'host'
        outcome['failure_class']='ACCEPTANCE_CRITERION_FAILURE' if worker_error else 'HOST_VALIDATOR_DEFECT'
        outcome['failure_origin']={'file':str(origin) if origin else None,'existing_workspace_module':missing_existing}
    finally:
        urllib.request.urlopen=originals
        for service,thread in servers:service.shutdown();service.server_close();thread.join(5)
    outcome.update(passed=not outcome.get('error'),mode='live',checks=checks,live_calls=calls,proof_kinds=proofs if not outcome.get('error') else [])
    outcome['collection_id']=collection_id
    save(run_path/'artifacts'/('host_evidence_'+collection_id+'.json'),outcome)
    return outcome


if __name__=='__main__':
    from pitchi_work.host_validator_runner import main
    raise SystemExit(main())
