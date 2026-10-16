"""Host-owned predicates shared by live collection and offline receipt checks.

Offline checks never create live evidence and cannot satisfy mission acceptance.
"""
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.parse


class CriterionFailure(Exception):
    """A measured artifact/behavior violates an unchanged criterion."""


def require(condition, message):
    if not condition:
        raise CriterionFailure(message)


def snapshot(workspace):
    return {p.relative_to(workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def scope(workspace, baseline, allowed):
    current = snapshot(workspace)
    require(all(current.get(p) == h for p, h in baseline.items() if p not in allowed),
            'Protected artifact changed')
    require(all(p in baseline or p in allowed for p in current), 'Undeclared new artifact')
    return {'protected_files': len(set(baseline) - set(allowed)), 'active_workspace': str(workspace)}


def syntax(workspace, outputs):
    for name in outputs:
        path = workspace / name
        require(path.is_file(), 'Missing ' + name)
        if name.endswith('.py'):
            try:
                ast.parse(path.read_text())
            except SyntaxError as exc:
                raise CriterionFailure('SyntaxError: ' + str(exc)) from exc
    return {'files': outputs}


def live_response(value, calls):
    require(isinstance(value, dict), 'HTTP QA response is not an object')
    require(bool(calls), 'No actual model invocation')
    for call in calls:
        require('qwen' in call.get('model', '').lower(), 'Response was not the configured comparison model')
        require(bool(call.get('evidence')), 'No real evidence at adapter input')
        for key in ('request_sha256', 'response_sha256'):
            digest = call.get(key, '')
            require(len(digest) == 64 and all(c in '0123456789abcdef' for c in digest),
                    'Invalid live receipt ' + key)
    return value


def citations(workspace, result, question, calls, retrieval):
    require(result.get('insufficient') is False and bool(result.get('answer', '').strip()),
            'No grounded positive answer')
    require(bool(result.get('citations')), 'Missing citations')
    call = next((c for c in reversed(calls) if c.get('question') == question), None)
    require(call is not None, 'No model receipt for the question')
    require(call['result'].get('insufficient') is False, 'Model abstained')
    text = call['result'].get('answer', '').strip()
    require(bool(text) and text in result['answer'], 'Generated answer was replaced')
    expected = {(r['book_title'], int(r['pdf_page'])) for r in call['evidence']}
    actual = {(r['book_title'], int(r['pdf_page'])) for r in result['citations']}
    require(actual == expected, 'Not all retrieved citations were preserved')
    real = {(r['book_title'], int(r['pdf_page']), r['ocr_text']) for r in retrieval}
    uri = 'file:' + urllib.parse.quote(str(workspace / 'search/library.sqlite')) + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for row in call['evidence']:
            require((row['book_title'], int(row['pdf_page']), row['ocr_text']) in real,
                    'Model context is not actual retrieval')
            require(db.execute('SELECT 1 FROM pages p JOIN books b ON b.book_id=p.book_id '
                               'WHERE b.title=? AND p.pdf_page=? AND p.text=?',
                               (row['book_title'], int(row['pdf_page']), row['ocr_text'])).fetchone(),
                    'Model context is not actual OCR DB text')
        for row in result['citations']:
            require(db.execute('SELECT 1 FROM pages p JOIN books b ON b.book_id=p.book_id '
                               'WHERE b.title=? AND p.pdf_page=?',
                               (row['book_title'], int(row['pdf_page']))).fetchone(),
                    'Citation not in real DB')
            require(row['book_title'] in result['answer'] and str(row['pdf_page']) in result['answer'],
                    'Citation missing from answer')
    return {'question': question, 'answer': result['answer'], 'citations': result['citations'],
            'grounding_verified': 'retrieval/context/response provenance; not semantic entailment'}


def insufficient(value, counts):
    require(bool(counts) and all(count == 0 for count in counts), 'Negative fixture has actual retrieval')
    require(value.get('insufficient') is True and not value.get('citations'),
            'Insufficient evidence not respected')
    require(bool(value.get('answer', '').strip()), 'Empty insufficient answer')
    return value


def search(workspace, count, rows, body):
    require(bool(count and rows), 'Actual OCR search has no results')
    require(body.get('count', body.get('total', 0)) == count,
            'HTTP search disagrees with real DB search')
    path = workspace / 'search/search_preserve_test.py'
    require(path.is_file(), 'Missing search/search_preserve_test.py')
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as exc:
        raise CriterionFailure('SyntaxError: ' + str(exc)) from exc
    require(any(isinstance(n, ast.Call) and (
        isinstance(n.func, ast.Attribute) and n.func.attr == 'urlencode' or
        isinstance(n.func, ast.Name) and n.func.id == 'urlencode') for n in ast.walk(tree)),
        'Required producer URL encoding not implemented')
    # Worker assertions are not formal evidence; HTTP/DB observations above are.
    return {'count': count, 'results': rows, 'worker_test_formal_acceptance': False}


def record(workspace, saved, observed, calls, question, retrieval):
    require(bool(observed and calls), 'Producer did not execute actual QA/model')
    value = next((v for v in observed if v.get('question') == saved.get('question')), None)
    require(value is not None and value['question'] == question, 'Changed fixed existing question')
    for key in ('answer', 'insufficient', 'citations'):
        require(saved.get(key) == value['result'].get(key), 'Saved record differs from actual return: ' + key)
    live_response(value['result'], calls)
    citations(workspace, value['result'], question, calls, retrieval)
    return saved


def failure(exc):
    """Only explicit predicate failures are application failures."""
    owner = 'worker' if isinstance(exc, CriterionFailure) else 'host'
    return {'passed': False, 'failure_owner': owner,
            'failure_class': 'ACCEPTANCE_CRITERION_FAILURE' if owner == 'worker' else 'HOST_VALIDATOR_DEFECT',
            'error': type(exc).__name__ + ': ' + str(exc)}
