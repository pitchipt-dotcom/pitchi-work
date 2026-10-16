"""Explicit cloud diagnostics: offline preview by default, never a full mission."""
import json
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4
from .cloud import (configuration, fingerprint, CloudRoleAdapter, sdk_python,
                    CloudControlStop, key_path)
from .store import RunStore, dump, load


def execute(args):
    config = configuration(args.provider, args.model, args.config, allowed=True)
    # Diagnostics are synthetic and fixed. No workspace/history/evidence attached.
    messages = [{'role': 'system', 'content': 'Return only JSON.'},
                {'role': 'user', 'content': 'Return {"ok":true} for this connection test.'}]
    payload = {'provider': args.provider, 'model': config['model'], 'messages': messages,
               'max_output_tokens': min(128, config['max_output_tokens'])}
    if args.action == 'test' and not args.allow_external:
        return {'sent': False, 'payload': payload, 'approval_sha256': fingerprint(payload),
                'key_file_exists': key_path(args.provider).is_file(),
                'next': 'Review payload, then repeat with --allow-external --approve-sha <exact SHA256>'}
    if not args.allow_external:
        raise CloudControlStop('EXTERNAL_API_PERMISSION_REQUIRED', 'Model listing is network communication; requires --allow-external')
    if args.action == 'models':
        # No model inference or workspace payload. Official authenticated Models API.
        import os
        with tempfile.TemporaryDirectory(prefix='pitchi-cloud-models-') as tmp:
            root = Path(tmp)
            dump(root/'input.json', {'payload': payload, 'timeout': config['timeout_seconds'], 'action': 'models'})
            env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL')}
            env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE='1')
            try:
                subprocess.run([sdk_python(args.provider), '-B', '-m', 'pitchi_work.cloud_transport', str(root)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=config['timeout_seconds'], check=True)
            except (subprocess.SubprocessError, OSError):
                raise CloudControlStop('CLOUD_TRANSPORT_FAILURE', 'Model listing failed; provider details suppressed') from None
            result = load(root/'result.json')
            if not result['ok']:
                raise CloudControlStop(result['failure_class'], 'Model listing denied or unavailable')
            return {'provider': args.provider, 'model_ids': result['result']['model_ids'],
                    'note': 'Listed IDs are account-visible; actual inference may require additional access.'}
    if args.approve_sha != fingerprint(payload):
        raise CloudControlStop('EXTERNAL_CONTENT_APPROVAL_REQUIRED', 'Preview and approve the exact test payload hash first')
    folder = args.home/'artifact_data/cloud_diagnostics'/uuid4().hex
    folder.mkdir(parents=True, mode=0o700)
    run = RunStore(folder)
    dump(folder/'state.json', {'status': 'READY', 'llm_config': config})
    adapter = CloudRoleAdapter(run, config, approval=lambda p,h: h == args.approve_sha)
    result = adapter.ask('connection_test', '', messages=messages, max_output_tokens=128)
    # Body omitted from diagnostic stdout; private adapter artifacts remain available.
    return {'provider': args.provider, 'model': config['model'], 'response_ok': result.get('ok') is True,
            'receipt_directory': str(folder/'artifacts'), 'mission_started': False}
