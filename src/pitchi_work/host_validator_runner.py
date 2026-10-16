"""One process/argument/environment boundary for all seven Host validators."""
import argparse
import importlib
import json
import os
import runpy
from pathlib import Path
import shlex
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from . import host_validator_checks as checks
from .host_contracts import TEMPLATES

SOURCE = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 360
REGISTRY = {
    'V1': 'protected workspace scope', 'V2': 'required files / Python syntax',
    'V3': 'HTTP QA result / local model receipt', 'V4': 'retrieval / citation / DB provenance',
    'V5': 'insufficient evidence / no invented citation', 'V6': 'real HTTP search preservation / URL encoding',
    'V7': 'persisted artifact equals actual QA return',
}


def producer(path):
    """No runner arguments leak into a producer's argparse/unittest main."""
    original_args = sys.argv[:]
    sys.argv = [str(path)]
    try:
        try:
            return runpy.run_path(str(path), run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise RuntimeError('Artifact producer exited ' + str(exc.code)) from exc
    finally:
        sys.argv = original_args


def command(workspace, run_path, template, validator=None, input_path=None):
    # Explicit package root works even when inherited PYTHONPATH is absent or bad.
    parts = ['env', 'PYTHONPATH=' + str(SOURCE), 'PYTHONDONTWRITEBYTECODE=1',
             sys.executable, '-B', '-m', 'pitchi_work.host_validator_runner',
             '--workspace', str(Path(workspace).resolve()), '--run', str(Path(run_path).resolve()),
             '--template', template]
    if validator:
        parts += ['--validator', validator]
    if input_path:
        parts += ['--input', str(Path(input_path).resolve())]
    return shlex.join(parts)


def preflight(workspace, run_path):
    workspace = Path(workspace).resolve(strict=True)
    run_path = Path(run_path).resolve(strict=True)
    if Path.cwd().resolve() != workspace:
        raise RuntimeError('Host active workspace / cwd mismatch')
    if not workspace.is_dir() or not run_path.is_dir():
        raise RuntimeError('Host workspace/run must be directories')
    if not os.access(run_path, os.W_OK):
        raise PermissionError('Host evidence output directory is not writable')
    return workspace, run_path


def app_module(workspace):
    path = workspace / 'search/app.py'
    if not path.is_file():
        raise RuntimeError('Host retrieval input missing: search/app.py')
    sys.path.insert(0, str(workspace / 'search'))
    app = importlib.import_module('app')
    if Path(app.__file__).resolve() != path.resolve():
        raise RuntimeError('Host imported an app outside the active workspace')
    return app


def single(validator, workspace, run_path, template, payload):
    """LLM-free checks of persisted observations; never formal live receipts."""
    from .grounding import query_stems
    from .host_validators import POSITIVE, NEGATIVE
    if validator == 'V1':
        baseline = json.loads((run_path / 'artifacts/host_baseline.json').read_text())
        allowed = {p for t in TEMPLATES.values() for p in t['outputs']}
        return checks.scope(workspace, baseline, allowed)
    if validator == 'V2':
        return checks.syntax(workspace, TEMPLATES[template]['outputs'])
    if validator == 'V3':
        return checks.live_response(payload['result'], payload['calls'])
    app = app_module(workspace)
    if validator == 'V5':
        counts = [app.search_pages(s, None, 0, 50)[0] for s in query_stems(NEGATIVE)]
        return checks.insufficient(payload['result'], counts)
    if validator == 'V6':
        if 'search_observation' in payload:
            observed=payload['search_observation']
            return checks.search(workspace,observed['count'],observed['results'],observed['http_response'])
        service = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            stem = query_stems(POSITIVE)[-1]
            count, rows = app.search_pages(stem, None, 0, 50)
            url = 'http://127.0.0.1:' + str(service.server_port) + '/search?'
            with urllib.request.urlopen(url + urllib.parse.urlencode({'q': stem}), timeout=20) as response:
                body = json.loads(response.read())
            return checks.search(workspace, count, rows, body)
        finally:
            service.shutdown(); service.server_close(); thread.join(5)
    retrieval = []
    for stem in query_stems(POSITIVE):
        retrieval += app.search_pages(stem, None, 0, 50)[1]
    if validator == 'V4':
        return checks.citations(workspace, payload['result'], POSITIVE, payload['calls'], retrieval)
    if validator == 'V7':
        return checks.record(workspace, payload['saved'], payload['observed'], payload['calls'], POSITIVE, retrieval)
    raise ValueError('Unknown registered validator')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--template', choices=TEMPLATES, required=True)
    parser.add_argument('--validator', choices=REGISTRY)
    parser.add_argument('--input')
    args = parser.parse_args()
    from .host_validators import PREFIX
    try:
        workspace, run_path = preflight(args.workspace, args.run)
        if args.validator:
            payload = json.loads(Path(args.input).read_text()) if args.input else {}
            details = single(args.validator, workspace, run_path, args.template, payload)
            result = {'passed': True, 'details': details}
        else:
            from .host_validators import validate
            result = validate(workspace, run_path, args.template)
    except Exception as exc:
        result = checks.failure(exc)
    if args.validator:
        result.update(mode='offline_validator_check', validator=args.validator, proof_kinds=[],
                      cwd=str(Path.cwd()), pythonpath=str(SOURCE), acceptance_evidence=False)
    elif result.get('failure_owner') == 'host':
        # Preflight/import/runtime errors must also produce durable Host evidence.
        folder = Path(args.run) / 'artifacts'
        folder.mkdir(exist_ok=True)
        result.update(mode='live', checks=[], live_calls=[], proof_kinds=[])
        path = folder / ('host_evidence_' + args.template + '_' + str(__import__('time').time_ns()) + '.json')
        path.write_text(json.dumps(result, ensure_ascii=False))
    print(PREFIX + json.dumps(result, ensure_ascii=False) if args.validator else
          PREFIX + json.dumps({k: result.get(k) for k in ('passed', 'error', 'failure_class', 'failure_owner')}))
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
