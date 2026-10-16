"""Read-only loopback preview of a completed static-web mission."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .store import load


def server(run,port=0):
    state=run.state()
    if state.get('profile')!='static_web_app' or state.get('status')!='COMPLETED':
        raise ValueError('web-preview requires a Host-completed static_web_app run')
    workspace=Path(state['workspace']);receipts=load(run.path/'artifacts/root_evidence.json')
    hashes={name:hash for receipt in receipts for name,hash in receipt['artifact_hashes'].items()}
    return manifest_server(workspace,hashes,port=port)


def manifest_server(workspace,hashes,entry='index.html',port=0):
    """Shared, fixed-manifest static serving for apps and HTML reports."""
    workspace=Path(workspace).resolve()
    types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8',
           '.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.svg':'image/svg+xml','.txt':'text/plain; charset=utf-8'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in hosts or self.headers.get('Origin') and self.headers['Origin'] not in {'http://'+h for h in hosts}:
                self.send_error(403);return
            from urllib.parse import unquote
            route=unquote(urlsplit(self.path).path)
            name=entry if route=='/' else route.lstrip('/')
            if name not in hashes:self.send_error(404);return
            path=workspace/name
            if path.is_symlink() or not path.resolve().is_relative_to(workspace):self.send_error(403);return
            try:data=path.read_bytes()
            except OSError:self.send_error(404);return
            if hashlib.sha256(data).hexdigest()!=hashes[name]:self.send_error(409,'Artifact changed since Host validation');return
            self.send_response(200);self.send_header('Content-Type',types[path.suffix.lower()]);self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'none'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(data)
        do_HEAD=do_GET
    http=ThreadingHTTPServer(('127.0.0.1',port),Handler);http.daemon_threads=True
    return http


def serve(run,port=18793):
    http=server(run,port)
    print(f'Pitchi-work app preview: http://127.0.0.1:{http.server_port}/',flush=True)
    print('ASSISTED: try the app. SUBJECTIVE: enjoyment/design are yours to judge. Ctrl+C stops preview.',flush=True)
    try:http.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:http.server_close()
    return 0
