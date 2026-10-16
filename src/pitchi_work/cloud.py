"""Opt-in official SDK adapters. Host authority and local defaults are unchanged.

Each exact outgoing payload needs human approval. Telemetry contains metadata,
never request/response text, credential values, or provider exception messages.
"""
import hashlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from .model import ModelFailure
from .store import dump, load, now
from .integration_packet import message_budget

PROVIDERS = ('local', 'openai', 'anthropic')
DEFAULTS = {'openai': 'gpt-6-luna', 'anthropic': 'claude-haiku-5-5'}
LIMITS = {'timeout_seconds': 60, 'max_output_tokens': 1800,
          'max_calls_per_run': 10, 'max_attempts': 1, 'max_input_bytes': 24000}
# Optional user-maintained USD / million tokens. Unknown price is never zero.
PRICES = {'openai': {'gpt-6-luna': [0.1, 0.5]}, 'anthropic': {}}


class CloudControlStop(RuntimeError):
    """Non-retryable Host policy boundary, not an agent implementation failure."""
    def __init__(self, kind, message):
        self.failure_class = kind
        super().__init__(message)


def key_path(provider):
    return Path.home() / '.config/bonsai' / (provider + '.env')


def read_key(provider):
    """Read only a literal assignment; never source a shell script."""
    name = provider.upper() + '_API_KEY'
    value = os.environ.get(name)
    if not value:
        try:
            for line in key_path(provider).read_text().splitlines():
                match = re.fullmatch(r'\s*(?:export\s+)?' + name + r'\s*=\s*(.*?)\s*', line)
                if match:
                    value = match[1].strip().strip('\"\'')
                    break
        except OSError:
            pass
    if not value or any(c in value for c in '\n\r'):
        raise CloudControlStop('CLOUD_KEY_MISSING', 'Configured API key is absent; value is never displayed')
    return value


def configuration(provider, model=None, path=None, allowed=False):
    if provider not in PROVIDERS:
        raise ValueError('Unknown model provider')
    if provider == 'local':
        return {'provider': 'local'}
    if not allowed:
        raise CloudControlStop('EXTERNAL_API_PERMISSION_REQUIRED', 'External API requires --allow-external')
    supplied = load(Path(path), {}) if path else {}
    allowed_keys = set(LIMITS) | {'model', 'input_usd_per_million', 'output_usd_per_million'}
    if not isinstance(supplied, dict) or set(supplied) - allowed_keys:
        raise ValueError('Cloud config permits only model, finite limits and prices; no credentials/endpoints')
    result = {**LIMITS, **supplied, 'provider': provider,
              'model': model or supplied.get('model') or DEFAULTS[provider], 'external_allowed': True}
    for field, maximum in [('timeout_seconds', 300), ('max_output_tokens', 8000),
                           ('max_calls_per_run', 100), ('max_input_bytes', 64000), ('max_attempts', 1)]:
        if type(result[field]) is not int or not 1 <= result[field] <= maximum:
            raise ValueError('Invalid finite cloud limit: ' + field)
    if not isinstance(result['model'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', result['model']):
        raise ValueError('Invalid model ID')
    for field in ('input_usd_per_million', 'output_usd_per_million'):
        if field in result and (type(result[field]) not in (int, float) or not 0 <= result[field] <= 10000):
            raise ValueError('Invalid price')
    return result


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def terminal_approval(payload, digest):
    """No piped/chat input can silently grant consent; no approval persistence."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    print('\n外部API送信候補（機密・個人情報を確認してください。自動送信しません）')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print('SHA256: ' + digest)
    return input('この内容の外部送信を許可する場合だけ SEND と入力: ').strip() == 'SEND'


def sdk_python(provider):
    if importlib.util.find_spec(provider):
        return sys.executable
    candidate = Path(__file__).resolve().parents[2] / '.venv-cloud/bin/python'
    if candidate.is_file():
        return str(candidate)
    raise CloudControlStop('CLOUD_SDK_MISSING', 'Install the cloud optional dependencies; no API call made')


def sdk_client(provider, timeout):
    # Fixed official endpoints, SDK automatic retries OFF. Never trust env base URL.
    if provider == 'openai':
        from openai import OpenAI
        return OpenAI(api_key=read_key(provider), base_url='https://api.openai.com/v1',
                      timeout=timeout, max_retries=0)
    from anthropic import Anthropic
    return Anthropic(api_key=read_key(provider), base_url='https://api.anthropic.com',
                     timeout=timeout, max_retries=0)


def sdk_generate(client, payload):
    """Normalize official SDK responses, not Host checks or model assertions."""
    if payload['provider'] == 'openai':
        response = client.responses.create(model=payload['model'], input=payload['messages'],
                    max_output_tokens=payload['max_output_tokens'], store=False)
        usage = response.usage
        return {'model': response.model, 'content': response.output_text,
                'finish_reason': 'stop' if response.status == 'completed' else 'length',
                'usage': {'prompt_tokens': usage.input_tokens, 'completion_tokens': usage.output_tokens,
                          'total_tokens': usage.total_tokens}, 'provider_request_id': response.id}
    system = '\n'.join(m['content'] for m in payload['messages'] if m['role'] in ('system', 'developer'))
    response = client.messages.create(model=payload['model'], system=system,
                messages=[m for m in payload['messages'] if m['role'] in ('user', 'assistant')],
                max_tokens=payload['max_output_tokens'])
    usage = response.usage
    cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
    cache_write = getattr(usage, 'cache_creation_input_tokens', 0) or 0
    incoming = usage.input_tokens + cache_read + cache_write
    return {'model': response.model, 'content': ''.join(b.text for b in response.content if b.type == 'text'),
            'finish_reason': 'stop' if response.stop_reason == 'end_turn' else 'length',
            'usage': {'prompt_tokens': incoming, 'completion_tokens': usage.output_tokens,
                      'total_tokens': incoming + usage.output_tokens,
                      'cache_read_input_tokens': cache_read, 'cache_creation_input_tokens': cache_write},
            'provider_request_id': response.id}


def error_class(exc):
    code = getattr(exc, 'status_code', None)
    return {401: 'CLOUD_AUTHENTICATION_FAILED', 403: 'CLOUD_MODEL_ACCESS_DENIED',
            404: 'CLOUD_MODEL_UNAVAILABLE', 429: 'CLOUD_RATE_LIMITED'}.get(code, 'CLOUD_TRANSPORT_FAILURE')


def estimate(config, usage):
    rates = PRICES.get(config['provider'], {}).get(config['model'])
    if 'input_usd_per_million' in config and 'output_usd_per_million' in config:
        rates = [config['input_usd_per_million'], config['output_usd_per_million']]
    if not rates:
        return None
    return round((usage.get('prompt_tokens', 0) * rates[0] +
                  usage.get('completion_tokens', 0) * rates[1]) / 1_000_000, 8)


class CloudRoleAdapter:
    def __init__(self, run, config, approval=None, client=None):
        if config.get('provider') not in ('openai','anthropic'):
            raise ValueError('Cloud provider required')
        # Validate restored configs too; no unknown settings become authority.
        validated = configuration(config['provider'], config.get('model'), allowed=config.get('external_allowed',False))
        if set(config) - (set(validated) | {'input_usd_per_million','output_usd_per_million'}):
            raise ValueError('Unknown cloud configuration field')
        for name,maximum in [('timeout_seconds',300),('max_output_tokens',8000),('max_calls_per_run',100),('max_input_bytes',64000),('max_attempts',1)]:
            if type(config.get(name)) is not int or not 1 <= config[name] <= maximum:
                raise ValueError('Invalid restored cloud limit: '+name)
        self.run, self.config, self.approval, self.client = run, dict(config), approval or terminal_approval, client

    def ask(self, role, prompt, *, timeout=360, max_output_tokens=2600,
            messages=None, output_schema=None, enable_thinking=False):
        self.run.raise_if_cancelled()
        c = self.config
        if not re.fullmatch(r'[a-zA-Z0-9_]{1,64}',role):
            raise CloudControlStop('CLOUD_INPUT_REJECTED','Invalid role identifier')
        if not c.get('external_allowed'):
            raise CloudControlStop('EXTERNAL_API_PERMISSION_REQUIRED', 'External API is disabled')
        # No files, history or environment are automatically attached. Existing Host
        # working sets are preserved; callers may explicitly pass a smaller packet.
        messages = messages or [{'role': 'system', 'content': f'You are the {role} role. Return only requested JSON; no state authority.'},
                                {'role': 'user', 'content': prompt}]
        if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or set(m) != {'role', 'content'} or m['role'] not in ('system', 'developer', 'user', 'assistant')
               or not isinstance(m['content'], str) for m in messages):
            raise CloudControlStop('CLOUD_INPUT_REJECTED', 'Only explicit text messages are permitted')
        payload = {'provider': c['provider'], 'model': c['model'], 'messages': messages,
                   'max_output_tokens': min(max_output_tokens, c['max_output_tokens'])}
        # Schema is instruction only; Host still independently validates every field.
        if output_schema is not None:
            payload['messages'] = [*messages, {'role': 'user', 'content': 'Required JSON schema: ' + json.dumps(output_schema)}]
        # Known credential-shaped input is refused even before the review display.
        # This is not a claim that arbitrary personal/confidential data can be
        # detected automatically; human per-payload review remains mandatory.
        if any(re.search(r'(?:sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:OPENAI|ANTHROPIC)_API_KEY\s*=\s*\S+)',m['content']) for m in payload['messages']):
            raise CloudControlStop('CLOUD_SENSITIVE_INPUT','Credential-shaped content blocked; value suppressed')
        budget = message_budget(payload['messages'], payload['max_output_tokens'])
        if not budget['within_budget'] or len(json.dumps(payload, ensure_ascii=False).encode()) > c['max_input_bytes']:
            raise CloudControlStop('CLOUD_CONTEXT_LIMIT', 'Explicit cloud payload exceeds Host context/input guard')
        digest = fingerprint(payload)
        if not self.approval(payload, digest):
            raise CloudControlStop('EXTERNAL_CONTENT_APPROVAL_REQUIRED', 'Exact outgoing payload was not approved; nothing sent')
        if fingerprint(payload)!=digest:
            raise CloudControlStop('CLOUD_APPROVAL_MISMATCH','Payload changed during approval; nothing sent')
        self.run.raise_if_cancelled()
        with (self.run.path/'cloud_calls.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state = self.run.state()
            count = state.get('cloud_call_count', 0)
            if count >= c['max_calls_per_run']:
                raise CloudControlStop('CLOUD_CALL_LIMIT', 'Finite per-run API call limit reached')
            folder = self.run.path / 'artifacts' / f'role_call_{len(list((self.run.path/"artifacts").glob("role_call_*")))+1:04d}_{role}'
            folder.mkdir(parents=True, exist_ok=False, mode=0o700)
            self.run.save_state(cloud_call_count=count + 1)
        receipt = {'provider': c['provider'], 'model': c['model'], 'role': role,
                   'reason': 'explicit_role:' + role, 'started_at': now(), 'request_sha256': digest,
                   'budget': budget, 'usage': {}, 'estimated_cost_usd': None, 'success': False,
                   'attempt': 1, 'timeout_seconds': min(timeout, c['timeout_seconds'])}
        started = time.monotonic()
        try:
            result = sdk_generate(self.client, payload) if self.client is not None else self.transport(payload, receipt['timeout_seconds'])
            receipt.update(usage=result['usage'], actual_model=result['model'],
                           provider_request_id=result.get('provider_request_id'), finish_reason=result['finish_reason'],
                           estimated_cost_usd=estimate(c, result['usage']))
            # Provider aliases can resolve to dated IDs. Keep both without asserting
            # equality to the alias; official server identity is preserved.
            response = {'model': result['model'], 'choices': [{'message': {'content': result['content']},
                        'finish_reason': result['finish_reason']}], 'usage': result['usage']}
            dump(folder/'response.json', response)
            receipt['response_sha256'] = hashlib.sha256((folder/'response.json').read_bytes()).hexdigest()
            if result['finish_reason'] != 'stop':
                raise ModelFailure('AGENT_OUTPUT_INCOMPLETE', 'Cloud output did not complete')
            (folder/'output.txt').write_text(result['content'], encoding='utf-8')
            (folder/'output.txt').chmod(0o600)
            try:
                value = json.loads(result['content'])
            except (ValueError, TypeError):
                raise ModelFailure('AGENT_OUTPUT_INVALID_JSON', 'Cloud output is not valid JSON') from None
            if not isinstance(value, dict):
                raise ModelFailure('AGENT_OUTPUT_INVALID_SHAPE', 'Expected JSON object')
            receipt['success'] = True
            return value
        except (ModelFailure, CloudControlStop) as exc:
            receipt['failure_class'] = exc.failure_class
            raise
        except Exception as exc:
            receipt['failure_class'] = error_class(exc)
            stop = CloudControlStop if receipt['failure_class'] in {
                'CLOUD_AUTHENTICATION_FAILED','CLOUD_MODEL_ACCESS_DENIED','CLOUD_MODEL_UNAVAILABLE'} else ModelFailure
            raise stop(receipt['failure_class'], 'Cloud request failed; provider error body suppressed') from None
        finally:
            receipt['elapsed_seconds'] = round(time.monotonic()-started, 3)
            dump(folder/'receipt.json', receipt)

    def transport(self, payload, timeout):
        # Cancellable subprocess: SDK credential lives only in child memory.
        with tempfile.TemporaryDirectory(prefix='pitchi-cloud-') as tmp:
            root = Path(tmp)
            dump(root/'input.json', {'payload': payload, 'timeout': timeout})
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'LC_ALL')}
            env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE='1')
            process = subprocess.Popen([sdk_python(self.config['provider']), '-B', '-m',
                         'pitchi_work.cloud_transport', str(root)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            start = time.monotonic()
            try:
                self.run.register_process(process.pid, 'cloud_llm')
                while process.poll() is None:
                    self.run.raise_if_cancelled()
                    if time.monotonic()-start > timeout:
                        raise ModelFailure('MODEL_TIMEOUT', 'Cloud request timed out')
                    time.sleep(.1)
                result = load(root/'result.json', {})
                if not result.get('ok'):
                    kind = result.get('failure_class', 'CLOUD_TRANSPORT_FAILURE')
                    stop = CloudControlStop if kind in {'CLOUD_AUTHENTICATION_FAILED','CLOUD_MODEL_ACCESS_DENIED',
                        'CLOUD_MODEL_UNAVAILABLE','CLOUD_KEY_MISSING'} else ModelFailure
                    raise stop(kind, 'Cloud request rejected; verify model access/key/SDK; no provider body logged')
                return result['result']
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait()
                self.run.unregister_process(process.pid)


def advisor_trigger(state, repeated_failures=0, *, enabled=False):
    """Suggestion-only escalation hook; no API call, grant, or state mutation."""
    return {'enabled': enabled, 'eligible': bool(enabled and
            (state.get('status') == 'STALLED' or repeated_failures >= 2)),
            'subject_run_id': state.get('run_id'), 'requires_content_approval': True,
            'automatic_execution': False}


def cloud_advice(run, config, packet, *, enabled=False, approval=None):
    """Explicit suggestion-only connection point. OFF unless a caller opts in.

    Send references/observed metadata, never expand Evidence or add acceptance.
    The same per-payload consent, budget and run call cap apply.
    """
    if not enabled:
        return {'enabled': False, 'called': False}
    allowed = {'goal','failure_class','stopped_phase','capability_catalog','evidence_refs',
               'environment','errors','attempted_fixes','validation_conditions','related_code','response_contract'}
    if not isinstance(packet,dict) or set(packet)-allowed or len(json.dumps(packet).encode())>8000:
        raise CloudControlStop('CLOUD_ADVISOR_INPUT_REJECTED','Use a bounded explicit metadata packet')
    adapter = CloudRoleAdapter(run,config,approval=approval)
    answer = adapter.ask('cloud_advisor',json.dumps({'subject_run_id':run.run_id,'observed':packet,
        'instruction':'Propose options using only observed facts. Do not reassess Host PASS/FAIL or execute operations.'},ensure_ascii=False))
    return {'enabled':True,'called':True,'subject_run_id':run.run_id,'authority':'suggestion_only','advice':answer}


def adapter_for(run, local_factory):
    config = run.state().get('llm_config', {'provider': 'local'})
    if config['provider'] == 'local':
        return local_factory(run, run.state()['local_model_endpoint'], run.state()['local_model'])
    return CloudRoleAdapter(run, config)
