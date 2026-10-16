"""Host artifact registry and owned preview gateway. No LLM or public binding."""
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import html
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .store import dump,load,_process_start_time
from .runner import HostDefect

_launched_gateways={}


class PreviewError(HostDefect):
    failure_class='ARTIFACT_PREVIEW_DEFECT'


def data_dir(home):
    folder=Path(home).resolve()/'artifact_data'
    if folder.is_symlink():raise PreviewError('Unsafe manager directory')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    return folder


def tailscale_ip():
    response=subprocess.run(['ip','-j','-4','addr','show','dev','tailscale0'],capture_output=True,text=True,timeout=5)
    for interface in json.loads(response.stdout or '[]'):
        if 'UP' not in interface.get('flags',[]):continue
        for address in interface.get('addr_info',[]):
            value=address.get('local','')
            if ipaddress.ip_address(value) in ipaddress.ip_network('100.64.0.0/10'):return value
    return None


def scope_choice(scope):
    if scope not in {'auto','localhost','tailscale'}:raise PreviewError('Only localhost or existing Tailscale scope is supported')
    ip=tailscale_ip() if scope!='localhost' else None
    if scope=='tailscale' and not ip:raise PreviewError('Existing Tailscale IPv4 interface is unavailable')
    return ('tailscale' if ip else 'localhost'),ip


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _register(home,spec):
    folder=data_dir(home);identifier=digest({k:v for k,v in spec.items() if k!='title'})[:24]
    with (folder/'registry.lock').open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        registry=load(folder/'registry.json',{})
        if identifier in registry and registry[identifier].get('revision',1)>1:
            if registry[identifier].get('root')==spec.get('root'):return identifier
            identifier=digest({'historical_snapshot':spec})[:24]
        registry[identifier]={'artifact_id':identifier,**spec};dump(folder/'registry.json',registry)
    return identifier


def approved_run(home,run,checks=None,scope='auto'):
    """Called by Host after root judgment, or for an already completed run."""
    state=run.state()
    if state.get('profile')!='static_web_app':raise PreviewError('Not a static web mission')
    if checks is None:
        if state['status']!='COMPLETED':raise PreviewError('Run has not completed Host acceptance')
        gate=load(run.path/'artifacts/request_completion_gate.json',{})
        if gate.get('status')!='COMPLETED' or not gate.get('host_evidence_hashes_verified'):raise PreviewError('Host completion receipt missing')
        checks=load(run.path/'artifacts/root_evidence.json',[])
    hashes={}
    for receipt in checks:
        if not receipt.get('passed') or digest_bytes(run.path/receipt['reference'])!=receipt['sha256']:
            raise PreviewError('Host evidence hash/PASS mismatch')
        hashes.update(receipt['artifact_hashes'])
    workspace=Path(state['workspace']).resolve()
    if not hashes or any(digest_bytes(workspace/name)!=h for name,h in hashes.items()):raise PreviewError('Completed artifact changed')
    selected,_=scope_choice(scope)
    return _register(home,{'kind':'static_web_run','root':str(workspace),'manifest':hashes,'entry':'index.html',
        'scope':selected,'title':'Web app '+run.run_id,'run_id':run.run_id,'acceptance':'host_root_pass'})


def digest_bytes(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def register(home,request):
    if not isinstance(request,dict) or set(request)-{'kind','root','run','entry','database','scope','title'}:raise PreviewError('Unknown registration fields')
    kind=request.get('kind');selected,_=scope_choice(request.get('scope','auto'))
    title=request.get('title',kind)
    if not isinstance(title,str) or not title.strip() or len(title)>120:raise PreviewError('Invalid artifact title')
    if kind=='static_web_run':
        from .store import ReadOnlyRun
        path=(Path(home)/'runs'/str(request.get('run',''))).resolve()
        if path.parent!=(Path(home)/'runs').resolve():raise PreviewError('Use an existing direct Pitchi run ID')
        return approved_run(home,ReadOnlyRun(path),scope=selected)
    if not isinstance(request.get('root'),str) or not request['root'].strip():raise PreviewError('Explicit root is required')
    root=Path(request['root']).expanduser().resolve()
    if not root.is_dir():raise PreviewError('Explicit existing artifact directory required')
    if kind=='screenshot_search':
        from .screenshots import database_path,connect,root_row
        database=database_path(request,home)
        with connect(database) as connection:root_row(connection,root)
        return _register(home,{'kind':kind,'root':str(root),'database':str(database),'scope':selected,'title':title,
                               'acceptance':'indexed_source_read_only'})
    if kind!='static_html':raise PreviewError('Registered kinds: static_web_run, static_html, screenshot_search')
    from .mission_profiles import visible
    suffixes={'.html','.css','.js','.png','.jpg','.jpeg','.svg','.txt'}
    manifest={}
    for path in root.rglob('*'):
        name=path.relative_to(root).as_posix()
        if not visible(name) or path.suffix.lower() not in suffixes:continue
        if path.is_symlink():raise PreviewError('Artifact symlinks are forbidden')
        if not path.is_file():continue
        if path.stat().st_size>20_000_000 or len(manifest)>=200:raise PreviewError('Static artifact exceeds serving limits')
        manifest[name]=digest_bytes(path)
    entry=request.get('entry','index.html')
    if entry not in manifest or not str(entry).endswith('.html'):raise PreviewError('Registered HTML entry required')
    return _register(home,{'kind':kind,'root':str(root),'manifest':manifest,'entry':entry,'scope':selected,'title':title,
                           'acceptance':'user_registered_files_not_mission_certification'})


def owned(record,role):
    pid=record.get('pid');start=record.get('start_time')
    if type(pid) is not int or not start or _process_start_time(pid)!=start:return False
    try:
        if Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]=='Z':return False
        args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return b'pitchi_work.artifacts' in args and role.encode() in args and record['instance'].encode() in args
    except (OSError,KeyError):return False


def stop_owned(record,role):
    if not owned(record,role):return
    os.kill(record['pid'],signal.SIGTERM)
    deadline=time.monotonic()+3
    while owned(record,role) and time.monotonic()<deadline:time.sleep(.05)
    if owned(record,role):os.kill(record['pid'],signal.SIGKILL)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*_):raise PreviewError('Preview redirect blocked')


def http_json(url,body=None):
    request=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,
                                   headers={'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=20) as response:
        return json.load(response)


def spawn(home,role,instance,extra=()):
    folder=data_dir(home)
    env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1','LANG':'C.UTF-8'}
    fd=os.open(folder/(role+'.log'),os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a') as log:
        process=subprocess.Popen([sys.executable,'-B','-m','pitchi_work.artifacts',role,str(Path(home).resolve()),instance,*extra],
            cwd=folder,env=env,stdin=subprocess.PIPE,stdout=log,stderr=log,start_new_session=True)
        process.stdin.close()
    return process


def gateway(home,scope='auto'):
    selected,ip=scope_choice(scope);folder=data_dir(home)
    with (folder/'gateway.lock').open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        record=load(folder/'gateway.json',{})
        if owned(record,'gateway'):
            health=http_json(f"http://127.0.0.1:{record['port']}/_health")
            if health.get('instance')!=record['instance']:raise PreviewError('Gateway identity mismatch')
            if selected=='tailscale' and ip not in record['addresses']:raise PreviewError('Restart the managed gateway to enable the current Tailscale address')
            return record
        instance=uuid.uuid4().hex;ready=folder/(instance+'.ready.json')
        process=spawn(home,'gateway',instance,(selected,ip or '',str(record.get('port',0))))
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if ready.exists():
                record=load(ready);dump(folder/'gateway.json',record)
                _launched_gateways[record['pid']]=process
                return record
            if process.poll() is not None:break
            time.sleep(.05)
        if process.poll() is None:process.terminate();process.wait(timeout=5)
        raise PreviewError('Gateway startup failed; inspect private artifact_data/gateway.log')


def operation(home,action,identifier=None,scope='auto'):
    if action=='list':return list(load(data_dir(home)/'registry.json',{}).values())
    registry=load(data_dir(home)/'registry.json',{})
    if identifier=='screenshot-search':
        candidates=[i for i,r in registry.items() if r['kind']=='screenshot_search']
        if len(candidates)!=1:raise PreviewError('Select an explicit screenshot artifact ID when zero/multiple sources exist')
        identifier=candidates[0]
    if identifier=='latest-web-app':
        candidates=sorted((i for i,r in registry.items() if r['kind']=='static_web_run' and not r.get('test_fixture')),
                          key=lambda i:registry[i].get('current_run_id',registry[i].get('run_id','')))
        if not candidates:raise PreviewError('No completed Web artifact registered')
        identifier=candidates[-1]
    record=gateway(home,scope)
    result=http_json(f"http://127.0.0.1:{record['port']}/_manage",{'action':action,'artifact_id':identifier})
    if not result.get('ok'):raise PreviewError(result.get('error','Preview operation failed'))
    if action=='shutdown':
        deadline=time.monotonic()+10
        while owned(record,'gateway') and time.monotonic()<deadline:time.sleep(.05)
        if owned(record,'gateway'):raise PreviewError('Managed gateway did not stop')
        process=_launched_gateways.pop(record['pid'],None)
        if process:process.wait(timeout=5)
        else:
            try:os.waitpid(record['pid'],os.WNOHANG)
            except ChildProcessError:pass
    return result


def prepare_completed_run(run,checks,scope='auto'):
    """Host-only handoff after all completion judgments, before state commit."""
    home=run.path.parent.parent
    identifier=approved_run(home,run,checks,scope)
    result=operation(home,'preview',identifier,scope)
    # The emitted external URL must be reachable on the Host at the chosen IP.
    for url in (result['localhost_url'],result['url']):
        request=urllib.request.Request(url,method='HEAD')
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=10) as response:
            if response.status!=200 or response.headers.get('X-Pitchi-Artifact')!=identifier:raise PreviewError('Issued URL identity/HTTP verification failed')
    from .artifact_jobs import publish_revision
    context=load(run.path/'artifact_input.json',{})
    if context:
        if context['operation']!='clone':operation(home,'stop',identifier,scope)
        identifier=publish_revision(home,run,identifier)
        try:
            result=operation(home,'preview',identifier,scope)
            for url in (result['localhost_url'],result['url']):
                with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(urllib.request.Request(url,method='HEAD'),timeout=10) as response:
                    if response.status!=200 or response.headers.get('X-Pitchi-Artifact')!=identifier:raise PreviewError('Revision URL verification failed')
        except Exception:
            from .artifact_jobs import rollback_publication
            rollback_publication(home,run,identifier)
            raise
    return result


def artifact_tool(name,request,*,home=None,cancelled=lambda:False):
    from .screenshots import DEFAULT_HOME
    home=Path(home or DEFAULT_HOME)
    try:
        if cancelled():raise PreviewError('Cancelled before preview operation')
        if name=='artifact.register':result={'artifact_id':register(home,request)}
        elif name=='artifact.inspect':
            from .artifact_jobs import source_context
            result=source_context(home,request.get('artifact_id'),request.get('operation','repair'))
        elif name=='artifact.list':
            result={'artifacts':[{k:v for k,v in r.items() if k in {'artifact_id','kind','title','scope','run_id','acceptance','revision','parent_artifact_id','source_run_id','current_run_id','operation','lineage'}} for r in operation(home,'list')]}
        else:
            if set(request)-{'artifact_id','scope'}:raise PreviewError('Use artifact_id and scope only')
            result=operation(home,name.split('.')[1],request.get('artifact_id'),request.get('scope','auto'))
        return {'capability':name,'ok':True,**result}
    except Exception as exc:return {'capability':name,'ok':False,'error':{'code':'ARTIFACT_PREVIEW_DEFECT','owner':'host','message':str(exc)}}


def backend(home,instance,identifier,parent_pid):
    # Only manager-owned children: die with the manager, never touch old services.
    if os.getppid()!=int(parent_pid):return
    parent_start=_process_start_time(int(parent_pid))
    record=load(data_dir(home)/'registry.json',{})[identifier]
    if record['kind']=='screenshot_search':
        from .screenshot_ui import server
        http=server(home,record['root'],record['database'],base_path='/artifacts/'+identifier)
    else:
        from .web_preview import manifest_server
        http=manifest_server(record['root'],record['manifest'],record['entry'])
    original=http.RequestHandlerClass
    class Identity(original):
        def end_headers(self):self.send_header('X-Pitchi-Artifact',identifier);super().end_headers()
    http.RequestHandlerClass=Identity
    ready={'pid':os.getpid(),'start_time':_process_start_time(os.getpid()),'instance':instance,'port':http.server_port,'artifact_id':identifier}
    dump(data_dir(home)/(instance+'.ready.json'),ready)
    def stop(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    finished=threading.Event()
    def watch_parent():
        while not finished.wait(.2):
            if os.getppid()!=int(parent_pid) or _process_start_time(int(parent_pid))!=parent_start:
                os.kill(os.getpid(),signal.SIGTERM);return
    threading.Thread(target=watch_parent,daemon=True).start()
    try:http.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:pass
    finally:finished.set();http.server_close()


def serve_gateway(home,instance,scope,ip,preferred):
    folder=data_dir(home);children={};mutex=threading.RLock();servers=[]
    addresses=['127.0.0.1']+([ip] if scope=='tailscale' else [])
    def begin(identifier):
        registry=load(folder/'registry.json',{})
        if identifier not in registry:raise PreviewError('Unknown artifact ID')
        if identifier in children and owned(children[identifier],'backend'):return children[identifier]
        previous=children.pop(identifier,None)
        if previous:previous['_process'].wait(timeout=5)
        nonce=uuid.uuid4().hex;process=spawn(home,'backend',nonce,(identifier,str(os.getpid())))
        ready=folder/(nonce+'.ready.json');deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if ready.exists():
                child=load(ready);child['_process']=process;children[identifier]=child
                dump(folder/'previews.json',{k:{n:v for n,v in c.items() if n!='_process'} for k,c in children.items()})
                return child
            if process.poll() is not None:break
            time.sleep(.05)
        if process.poll() is None:process.terminate()
        process.wait(timeout=5);raise PreviewError('Backend failed; inspect private artifact_data/backend.log')
    def halt(identifier):
        child=children.pop(identifier,None)
        if child:
            stop_owned(child,'backend');child['_process'].wait(timeout=5)
        dump(folder/'previews.json',{k:{n:v for n,v in c.items() if n!='_process'} for k,c in children.items()})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def reply(self,status,body,kind='application/json; charset=utf-8',headers=None):
            if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            for k,v in (headers or {}).items():self.send_header(k,v)
            self.end_headers()
            if self.command!='HEAD':
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
        def permitted(self):
            hosts={f'{a}:{self.server.server_port}' for a in addresses}|{f'localhost:{self.server.server_port}'}
            host=self.headers.get('Host')
            return host in hosts and (not self.headers.get('Origin') or self.headers['Origin']=='http://'+host)
        def do_POST(self):
            if not self.permitted() or self.client_address[0]!='127.0.0.1' or self.path!='/_manage':self.reply(403,{'error':'Local management only'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<4096:raise PreviewError('Invalid management size')
                request=json.loads(self.rfile.read(length));identifier=request.get('artifact_id');action=request['action']
                with mutex:
                    if action=='shutdown':
                        self.reply(200,{'ok':True});os.kill(os.getpid(),signal.SIGTERM);return
                    row=load(folder/'registry.json',{}).get(identifier)
                    if not row:raise PreviewError('Unknown artifact ID')
                    if action=='stop':halt(identifier);self.reply(200,{'ok':True,'artifact_id':identifier,'status':'stopped'});return
                    if action=='preview':child=begin(identifier)
                    elif action=='status':child=children.get(identifier,{})
                    else:raise PreviewError('Unknown management operation')
                    live=owned(child,'backend');url_ip=ip if row['scope']=='tailscale' else '127.0.0.1'
                    if row['scope']=='tailscale' and scope!='tailscale':raise PreviewError('Tailscale gateway unavailable')
                    if live:
                        probe=urllib.request.Request(f"http://127.0.0.1:{child['port']}/",method='HEAD')
                        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(probe,timeout=5) as response:
                            if response.status!=200 or response.headers.get('X-Pitchi-Artifact')!=identifier:raise PreviewError('Backend identity/HTTP check failed')
                    self.reply(200,{'ok':True,'artifact_id':identifier,'kind':row['kind'],'status':'running' if live else 'stopped',
                        'pid':child.get('pid') if live else None,'backend_port':child.get('port') if live else None,'scope':row['scope'],
                        'url':f'http://{url_ip}:{self.server.server_port}/artifacts/{identifier}/',
                        'localhost_url':f'http://127.0.0.1:{self.server.server_port}/artifacts/{identifier}/'})
            except Exception as exc:self.reply(400,{'ok':False,'error':str(exc)})
        def do_GET(self):
            if not self.permitted():self.reply(403,{'error':'Invalid Host/Origin'});return
            route=urllib.parse.urlsplit(self.path)
            if route.path=='/_health':self.reply(200,{'instance':instance});return
            if route.path=='/':
                rows=load(folder/'registry.json',{})
                links=''.join('<li><a href="/artifacts/'+i+'/">'+html.escape(r['title'])+'</a></li>' for i,r in rows.items()
                    if self.client_address[0]=='127.0.0.1' or r['scope']=='tailscale')
                self.reply(200,('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><h1>Pitchi-work Artifacts</h1><p>確認用の成果物。デザインと使いやすさは人間が判断してください。</p><ul>'+links+'</ul>').encode(),
                    'text/html; charset=utf-8',{'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"});return
            parts=route.path.split('/')
            if len(parts)<4 or parts[1]!='artifacts':self.reply(404,{'error':'Unknown route'});return
            identifier=parts[2];row=load(folder/'registry.json',{}).get(identifier)
            if not row:self.reply(404,{'error':'Unknown artifact'});return
            if row['scope']!='tailscale' and self.client_address[0]!='127.0.0.1':self.reply(403,{'error':'Local artifact only'});return
            try:
                with mutex:
                    child=children.get(identifier)
                    if not child or not owned(child,'backend'):self.reply(503,{'error':'Preview stopped; open from Pitchi-work'});return
                    port=child['port']
                path='/'+'/'.join(parts[3:])+('?' +route.query if route.query else '')
                request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,method=self.command)
                try:response=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=20)
                except urllib.error.HTTPError as exc:response=exc
                with response:
                    if response.headers.get('X-Pitchi-Artifact')!=identifier:raise PreviewError('Wrong backend identity; refusing content')
                    data=response.read(50_000_001)
                    if len(data)>50_000_000:raise PreviewError('Preview payload limit')
                    headers={k:response.headers[k] for k in ('Content-Security-Policy','X-Frame-Options','Referrer-Policy') if response.headers.get(k)}
                    headers['X-Pitchi-Artifact']=identifier
                    self.reply(response.status,data,response.headers.get('Content-Type','application/octet-stream'),headers)
            except Exception as exc:self.reply(502,{'error':str(exc)})
        do_HEAD=do_GET
    # Atomic socket reservation; if a remembered port is occupied, never kill its owner.
    for wanted in (int(preferred),0):
        try:
            for address in addresses:
                port=servers[0].server_port if servers else wanted
                server=ThreadingHTTPServer((address,port),Handler);server.daemon_threads=True;servers.append(server)
            break
        except OSError:
            for server in servers:server.server_close()
            servers=[]
    if not servers:raise PreviewError('Unable to bind only approved preview addresses')
    ready={'pid':os.getpid(),'start_time':_process_start_time(os.getpid()),'instance':instance,
           'port':servers[0].server_port,'addresses':addresses,'scope':scope}
    dump(folder/(instance+'.ready.json'),ready)
    def stop(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.1},daemon=True) for s in servers]
    for thread in threads:thread.start()
    try:
        while True:
            for child in list(children.values()):child['_process'].poll()
            time.sleep(.2)
    except KeyboardInterrupt:pass
    finally:
        for identifier in list(children):halt(identifier)
        for server in servers:server.shutdown();server.server_close()
        for thread in threads:thread.join(timeout=2)


if __name__=='__main__':
    role,home,instance=sys.argv[1:4]
    if role=='gateway':serve_gateway(home,instance,*sys.argv[4:7])
    elif role=='backend':backend(home,instance,*sys.argv[4:6])
