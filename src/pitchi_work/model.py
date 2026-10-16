"""Model-neutral role boundary. No model response can mutate Host state."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Protocol
from .store import dump,load,now
from .integration_packet import message_budget
from .shared_model import headers, managed_endpoint, RejectRedirect


class ModelAdapter(Protocol):
    def ask(self, role: str, prompt: str, *, timeout: int = 360, max_output_tokens: int = 2600,
            messages=None, output_schema=None, enable_thinking=False) -> dict:
        """Return parsed model output; raw request/response receipt is adapter-owned.

        Formal contract/state authority is never delegated to this interface.
        """
        ...


class ModelFailure(RuntimeError):
    def __init__(self,failure_class,message):self.failure_class=failure_class;super().__init__(message)


def local_url(url):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'} or parsed.username or parsed.password or parsed.query:
        raise ValueError('Only unauthenticated loopback HTTP endpoints are allowed')
    return url.rstrip('/')


def health(endpoint,model):
    endpoint=local_url(endpoint)
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect()).open(urllib.request.Request(endpoint+'/models',headers=headers(endpoint)),timeout=5) as response:
        data=json.loads(response.read())
    available={v for row in data['data'] for v in [row['id'],*row.get('aliases',[])]}
    if model not in available:raise ModelFailure('MODEL_UNAVAILABLE','Configured model is not actually loaded: '+model)
    return {'available':True,'model':model}


def discover_local_model(endpoint):
    """Select only the single actually loaded model; never start/switch services."""
    endpoint=local_url(endpoint)
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect()).open(urllib.request.Request(endpoint+'/models',headers=headers(endpoint)),timeout=5) as response:
        models={row['id'] for row in json.load(response).get('data',[])}
    if len(models)!=1:raise ModelFailure('MODEL_SELECTION_REQUIRED','Specify --model when zero/multiple local models are available')
    return next(iter(models))


class LocalRoleAdapter:
    """Cancelable local role calls with raw outputs/receipts, no shell tools."""
    def __init__(self,run,endpoint,model):self.run=run;self.endpoint=local_url(endpoint);self.model=model

    def ask(self,role,prompt,*,timeout=360,max_output_tokens=2600,messages=None,output_schema=None,enable_thinking=False):
        self.run.raise_if_cancelled()
        check = getattr(self, '_shared_use_check', None)
        if check: check()
        messages=messages or [{'role':'system','content':f'You are the {role} role. Return only the requested JSON. No markdown fences. No hidden state authority.'},
                              {'role':'user','content':prompt}]
        budget=message_budget(messages,max_output_tokens)
        if not budget['within_budget']:raise ModelFailure('ROLE_CONTEXT_DEFECT',role+' exceeds frozen 80% context budget')
        from .recovery import reserve_local_repair_call
        reserve_local_repair_call(self.run,role)
        folder=self.run.path/'artifacts'/f'role_call_{len(list((self.run.path/"artifacts").glob("role_call_*")))+1:04d}_{role}'
        body={'model':self.model,'messages':messages,'temperature':0,'max_tokens':max_output_tokens,
              'stream':True,'stream_options':{'include_usage':True},'reasoning_budget':0,
              'chat_template_kwargs':{'enable_thinking':False},'response_format':{'type':'json_object'}}
        if output_schema is not None:
            body['response_format']={'type':'json_schema','json_schema':{
                'name':'host_worker_output','strict':True,'schema':output_schema}}
        if enable_thinking:
            body['chat_template_kwargs']['enable_thinking']=True
            body['reasoning_budget']=800
        if managed_endpoint(self.endpoint):
            body.pop('stream_options',None)
            body.pop('chat_template_kwargs',None)
            body.pop('reasoning_budget',None)
            body['reasoning_effort']='low' if enable_thinking else 'none'
            body['reasoning_budget_tokens']=800 if enable_thinking else 0
        dump(folder/'request.json',body)
        dump(folder/'invocation.json',{'url':self.endpoint+'/chat/completions','body':body,'timeout':timeout,
                                     'response':str(folder/'response.json'),'stream':str(folder/'stream.json')})
        env={k:v for k,v in os.environ.items() if k not in {'OPENAI_API_KEY','ANTHROPIC_API_KEY','PYTHONPATH'}}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),PYTHONDONTWRITEBYTECODE='1')
        process=subprocess.Popen([sys.executable,'-B','-m','pitchi_work.transport',str(folder/'invocation.json')],env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        started=time.monotonic();receipt={'role':role,'model':self.model,'started_at':now(),'budget':budget,
                                         'request_sha256':hashlib.sha256((folder/'request.json').read_bytes()).hexdigest()}
        try:
            self.run.register_process(process.pid,'llm:'+role)
            while True:
                self.run.raise_if_cancelled()
                if time.monotonic()-started>timeout:raise ModelFailure('MODEL_TIMEOUT',role+' timeout')
                try:_,stderr=process.communicate(timeout=.2);break
                except subprocess.TimeoutExpired:continue
            if process.returncode:raise ModelFailure('MODEL_TRANSPORT_FAILURE',stderr.strip())
            response=load(folder/'response.json');receipt.update(actual_model=response.get('model'),usage=response.get('usage',{}),
                    response_sha256=hashlib.sha256((folder/'response.json').read_bytes()).hexdigest())
            if response.get('model')!=self.model:raise ModelFailure('MODEL_IDENTITY_MISMATCH','Response model differs from configuration')
            choice=response['choices'][0];receipt['finish_reason']=choice['finish_reason']
            if choice['finish_reason']!='stop':raise ModelFailure('AGENT_OUTPUT_INCOMPLETE',role+' output did not finish')
            receipt.update(reasoning_requested=enable_thinking,
                reasoning_bytes=len((choice['message'].get('reasoning_content') or '').encode()))
            raw=choice['message']['content'];(folder/'output.txt').write_text(raw,encoding='utf-8')
            try:result=json.loads(raw)
            except (ValueError,TypeError) as exc:raise ModelFailure('AGENT_OUTPUT_INVALID_JSON',str(exc)) from exc
            if not isinstance(result,dict):raise ModelFailure('AGENT_OUTPUT_INVALID_SHAPE','Expected JSON object')
            return result
        except BaseException as exc:
            receipt.update(failure_class=getattr(exc,'failure_class',type(exc).__name__),error=str(exc))
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.communicate(timeout=1.5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.communicate()
            raise
        finally:
            receipt['elapsed_seconds']=round(time.monotonic()-started,3);dump(folder/'receipt.json',receipt)
            self.run.unregister_process(process.pid)
