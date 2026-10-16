"""Finite Host-owned tool registry, callable by other workers/indexers.

Tools return observations, never mission PASS/COMPLETED or LLM contracts.
OCR strings are untrusted data, not executable instructions.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .image_ocr import ocr_image
from .screenshots import index_screenshots,search_screenshots
from .screenshot_semantic import semantic_search, assisted_search
from .web_tools import verify_web
from .artifacts import artifact_tool
from .search_evidence import compare_tool

REGISTRY = {'search.pipeline_compare':{'description':'Compare observed query stages/source candidates/ranks; no LLM or automatic relevance/completion certification',
    'input':{'before':'explicit local collector JSON','after':'same-query collector JSON','controls':'optional observed control query list'},
    'access':'read_only','network':False,'handler':compare_tool},
    'web.verify':{'description':'Host-owned static app HTTP/browser checks; no LLM, no mission completion',
    'input':{'root':'read-only static app workspace','behavior':'interactive_smoke, arithmetic_practice, arithmetic_practice_review (later wrong-answer review)'},
    'access':'read_only_source_private_evidence_write','network':'isolated_loopback','handler':verify_web},
    'image.ocr': {'description': 'Read text from one local image; Japanese/English, CPU only',
    'input': {'path': 'explicit readable image path', 'languages': 'jpn+eng (default), jpn, eng',
              'psm': '11 (default), 3, 6, 12', 'timeout_seconds': '60 (default), max 300'},
    'access': 'read_only', 'network': False, 'handler': ocr_image},
    'screenshot.index': {'description':'Incrementally index image metadata and existing image.ocr observations',
        'input':{'root':'explicit image folder','database':'optional private local SQLite path',
                 'workers':'1..4, default 4','languages':'jpn+eng, jpn, eng','psm':'11 default, 3, 6, 12',
                 'retry_errors':'false default; explicitly retry failed OCR'},
        'access':'read_only_source_private_index_write','network':False,'handler':index_screenshots},
    'screenshot.search': {'description':'Find lexical screenshot candidates from metadata/OCR; not visual intent verification',
        'input':{'root':'explicit indexed folder','query':'literal words, max 256 characters',
                 'database':'optional private local SQLite path','app':'optional exact filename-derived app',
                 'date_from':'optional YYYY-MM-DD','date_to':'optional YYYY-MM-DD','limit':'1..100, default 30'},
        'access':'read_only','network':False,'handler':search_screenshots},
    'screenshot.assisted_search': {'description':'Python/OCR candidate ranking for human thumbnail selection; no LLM or network',
        'input':{'root':'indexed folder','query':'literal retrieval words','intent':'requested activity',
                 'limit':'desired human selection count, 1..30 default 5; not certified matches',
                 'candidate_limit':'limit..30 default 30','database':'optional private SQLite',
                 'app':'optional app','date_from':'optional date','date_to':'optional date'},
        'access':'read_only','network':False,'handler':assisted_search},
    'screenshot.semantic_search': {'description':'Optional experimental local Vision assessment; can misclassify, human confirmation required',
        'input':{'root':'explicit indexed folder','query':'lexical retrieval word','intent':'requested visible activity',
                 'limit':'1..20 default 5','candidate_limit':'limit..30 default 30','database':'optional private SQLite',
                 'app':'optional exact app','date_from':'optional date','date_to':'optional date',
                 'endpoint':'loopback /v1 only; PITCHI_VISION_ENDPOINT or local default',
                 'model':'loaded multimodal model; PITCHI_VISION_MODEL or model discovery',
                 'timeout_seconds':'5..300 default 120'},
        'access':'read_only_source_private_cache_write','network':'loopback_only','handler':semantic_search}}


for _action in ('register','list','inspect','preview','status','stop','shutdown'):
    REGISTRY['artifact.'+_action]={'description':'Host-owned artifact '+_action+'; no mission judgment or public binding',
        'input':({'kind':'static_web_run, static_html, screenshot_search','root':'explicit source directory','run':'completed run ID',
                  'scope':'auto (existing Tailscale else localhost), localhost, tailscale'} if _action=='register' else
                 {'artifact_id':'registered ID, screenshot-search, latest-web-app','scope':'auto, localhost, tailscale'}),
        'access':'read_only_source_private_manager_write','network':'localhost_or_existing_tailscale','handler':artifact_tool}


def catalog():
    return {name: {k: v for k, v in spec.items() if k != 'handler'} for name, spec in REGISTRY.items()}


def execute_tool(name, request, *, cancelled=lambda: False, home=None, progress=None):
    if name not in REGISTRY:
        return {'capability': name, 'ok': False, 'error': {'code': 'UNKNOWN_TOOL', 'owner': 'host',
                                                         'message': 'Tool is not registered'}}
    if name.startswith('artifact.'):return artifact_tool(name,request,cancelled=cancelled,home=home)
    if name=='screenshot.index':return index_screenshots(request,cancelled=cancelled,home=home,progress=progress)
    if name=='screenshot.search':return search_screenshots(request,cancelled=cancelled,home=home)
    if name=='screenshot.semantic_search':return semantic_search(request,cancelled=cancelled,home=home,progress=progress)
    if name=='screenshot.assisted_search':return assisted_search(request,cancelled=cancelled,home=home,progress=progress)
    if name=='web.verify':return verify_web(request,cancelled=cancelled,home=home)
    return REGISTRY[name]['handler'](request, cancelled=cancelled)


def resolve_tool_request(request,workspace=None):
    invocation=resolve_ocr_request(request,workspace)
    if invocation is not None:return invocation
    if not isinstance(request,str):return None
    text=request.strip();root=workspace
    if text in ('Screenshot Searchを開く','スクリーンショット検索を開く'):
        return {'name':'artifact.preview','input':{'artifact_id':'screenshot-search'}}
    semantic_text=text
    prefix=re.fullmatch(r'(.+?)\s*から[、,]?\s*(.+)',text)
    if prefix:root=prefix[1].strip().strip('"\x27');semantic_text=prefix[2]
    semantic=re.fullmatch(r'([^\s、]+?)(?:の|で|が)(.+?)(?:スクリーンショット|画面)を(?:(\d+)枚)?探(?:す|して)',semantic_text)
    if semantic and root is not None and any(word in semantic[2] for word in ('編集','操作','修正','変更')):
        return {'name':'screenshot.assisted_search','input':{'root':str(root),'query':semantic[1],
                'intent':semantic_text,'limit':int(semantic[3] or 5)}}
    match=re.fullmatch(r'(.+?)\s*から\s*(.+?)(?:関連)?(?:のスクリーンショット|のスクショ|を使っている画面|を編集している画面)を探(?:す|して)',text)
    if match:root=match[1].strip().strip('"\x27');query=match[2]
    else:
        match=re.fullmatch(r'(.+?)(?:関連)?(?:のスクリーンショット|のスクショ|を使っている画面|を編集している画面)を探(?:す|して)',text)
        if match:query=match[1]
        else:
            match=re.fullmatch(r'(?:このフォルダを検索可能にして|このフォルダのスクショを検索できるようにして|フォルダを索引化して)(?:\s*[:：]?\s*(.+))?',text)
            if match:
                selected=match[1].strip().strip('"\x27') if match[1] else root
                if selected is not None:return {'name':'screenshot.index','input':{'root':str(selected)}}
            return None
    if root is None or not query.strip():return None
    return {'name':'screenshot.search','input':{'root':str(root),'query':query.strip()}}


def resolve_ocr_request(request, workspace=None):
    """Narrow, deterministic request route; ambiguous/compound missions untouched.

    An exact OCR instruction + one explicit path only. Never browse a folder,
    invent a path, use OCR contents as instructions, or make an LLM choice.
    """
    if not isinstance(request, str): return None
    phrases = r'(?:この画像を読んで|このスクショの文字を抜き出して|画像の文字を抜き出して|画像をOCRして|OCR)'
    match = re.fullmatch(phrases + r'\s*[:：]?\s*(?:"([^"\n]+)"|\x27([^\x27\n]+)\x27|([^\s]+))\s*', request.strip(), re.IGNORECASE)
    if not match: return None
    path = next(p for p in match.groups() if p is not None)
    if Path(path).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}: return None
    if workspace and not Path(path).is_absolute(): path = str(Path(workspace) / path)
    return {'name': 'image.ocr', 'input': {'path': path}}


def save_tool_result(home, name, request, result):
    """Private local artifact, atomically saved with hash/provenance, not run state."""
    base = Path(home) / 'tool_results'
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    payload = {'schema_version': 1, 'tool': name, 'input': request, 'result': result,
               'authority': 'host_tool_registry', 'mission_completion': False}
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.result-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        destination = directory / 'result.json'
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {'path': str(destination), 'sha256': hashlib.sha256(data).hexdigest(),
            'private': True}
