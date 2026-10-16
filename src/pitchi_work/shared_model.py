"""Authenticated shared Host client. No local-model fallback or implicit retries."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import urllib.parse
import urllib.request

def configuration():
    config = {}
    path = Path(os.environ.get('STRATA_ENV_FILE', str(Path.home()/'.config/strata/shared.env')))
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                config[key] = value.strip().strip('"').strip("'")
    config.update({k:v for k,v in os.environ.items() if k.startswith('STRATA_')})
    url = urllib.parse.urlsplit(config.get('STRATA_BASE_URL', ''))
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
        or url.username or url.password or url.query or url.fragment
        or url.path.rstrip('/') != '/api/models/strata/v1'):
        raise ValueError('shared Strata requires the approved loopback Host endpoint')
    if not config.get('STRATA_SHARED_TOKEN') or not config.get('STRATA_MODEL'):
        raise ValueError('shared Strata credentials/model missing')
    return config, urllib.parse.urlunsplit((url.scheme,url.netloc,'/api/models/strata','',''))

class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('local model redirects are prohibited')


def request(route, body=None, method='POST', timeout=600):
    config, base = configuration()
    req = urllib.request.Request(base+route,
        data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),
        method=method, headers={'Content-Type':'application/json',
        'Authorization':'Bearer '+config['STRATA_SHARED_TOKEN']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect())
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)

@contextmanager
def job_use():
    lease = request('/uses', {})
    stop = threading.Event()
    errors = []
    def renew():
        while not stop.wait(30):
            try: request('/uses/'+lease['use_id']+'/renew', {}, timeout=20)
            except Exception as error:
                errors.append(error)
                return
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    def check():
        if errors: raise RuntimeError('shared Strata job lease renewal failed') from errors[0]
    try:
        yield check
        check()
    finally:
        stop.set()
        thread.join(timeout=21)
        try: request('/uses/'+lease['use_id'], method='DELETE', timeout=20)
        except Exception: pass  # finite server lease expires after disconnected clients


def managed_endpoint(endpoint):
    parsed = urllib.parse.urlsplit(endpoint)
    return parsed.path.rstrip('/') == '/api/models/strata/v1'

def headers(endpoint):
    if not managed_endpoint(endpoint): return {}
    config, base = configuration()
    if endpoint.rstrip('/') != base+'/v1':
        raise ValueError('shared model endpoint differs from approved configuration')
    return {'Authorization':'Bearer '+config['STRATA_SHARED_TOKEN']}

def retained_mission(operation):
    from functools import wraps
    @wraps(operation)
    def wrapped(self, *args, **kwargs):
        endpoint = getattr(self.adapter, 'endpoint', '')
        if not managed_endpoint(endpoint): return operation(self, *args, **kwargs)
        with job_use() as check:
            self.adapter._shared_use_check = check
            try: return operation(self, *args, **kwargs)
            finally: self.adapter._shared_use_check = None
    return wrapped
