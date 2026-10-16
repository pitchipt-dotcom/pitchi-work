"""Read-only, CPU image OCR capability. No model, network or shell invocation."""
import csv
import hashlib
import io
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

SCHEMA_VERSION = 1
MAX_BYTES = 50_000_000
MAX_PIXELS = 25_000_000
FORMATS = {'PNG', 'JPEG', 'BMP', 'TIFF', 'WEBP'}


class OcrFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def stop(process):
    """Stop and reap only our own newly-created process group."""
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.communicate()


def command(argv, timeout, cancelled, cwd, env):
    if cancelled(): raise OcrFailure('CANCELLED', 'OCR cancelled')
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled(): raise OcrFailure('CANCELLED', 'OCR cancelled')
            if time.monotonic() >= deadline:
                raise OcrFailure('TIMEOUT', 'OCR processing deadline exceeded')
            try:
                output, _ = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired: pass
        if cancelled(): raise OcrFailure('CANCELLED', 'OCR cancelled')
        if process.returncode:
            # Engine stderr can contain source paths/text: never propagate it.
            raise OcrFailure('ENGINE_ERROR', 'OCR engine exited with code '+str(process.returncode))
        if len(output) > 16_000_000:
            raise OcrFailure('OUTPUT_LIMIT', 'OCR engine output exceeds limit')
        return output.decode('utf-8', errors='strict')
    except BaseException:
        stop(process)
        raise


def parse_tsv(output, psm):
    rows = csv.DictReader(io.StringIO(output), delimiter='\t', quoting=csv.QUOTE_NONE)
    required = {'level', 'page_num', 'block_num', 'par_num', 'line_num', 'conf',
                'left', 'top', 'width', 'height', 'text'}
    if not required.issubset(rows.fieldnames or []):
        raise OcrFailure('INVALID_ENGINE_OUTPUT', 'Missing TSV fields')
    groups = {}
    for row in rows:
        if row['level'] != '5' or not row['text'].strip(): continue
        score = float(row['conf'])
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise OcrFailure('INVALID_ENGINE_OUTPUT', 'Invalid confidence')
        box = [int(row[k]) for k in ('left', 'top', 'width', 'height')]
        if any(n < 0 for n in box):
            raise OcrFailure('INVALID_ENGINE_OUTPUT', 'Invalid bounding box')
        key = tuple(int(row[k]) for k in ('page_num', 'block_num', 'par_num', 'line_num'))
        groups.setdefault(key, []).append({'text': row['text'], 'confidence': score / 100,
                                           'bbox': box})
    lines = []
    for key, words in groups.items():
        # Preserve engine token boundaries; no Japanese rewriting/cleanup.
        lines.append({'id': ':'.join(map(str, key)), 'text': ' '.join(w['text'] for w in words),
                      'words': words})
    words = [w for line in lines for w in line['words']]
    return {'text': '\n'.join(line['text'] for line in lines), 'lines': lines,
            'confidence': sum(w['confidence'] for w in words) / len(words) if words else None,
            'confidence_method': 'mean_tesseract_word_confidence_not_accuracy', 'psm': psm}


def ocr_image(request, *, cancelled=lambda: False):
    """Reusable JSON-compatible result, including structured errors.

    Caller must authorize the supplied path. Files are copied to private temp
    storage and processed there; the original is never passed to an engine.
    """
    started = time.monotonic()
    result = {'schema_version': SCHEMA_VERSION, 'capability': 'image.ocr', 'ok': False,
              'file': None, 'source_sha256': None, 'text': '', 'lines': [],
              'confidence': None, 'engine': {'name': 'tesseract', 'version': None},
              'error': None, 'warnings': [], 'source_unchanged': False}
    try:
        if not isinstance(request, dict) or set(request) - {'path', 'languages', 'psm', 'timeout_seconds'}:
            raise OcrFailure('INVALID_INPUT', 'Expected path and registered OCR options only')
        value = request.get('path')
        if not isinstance(value, str) or not value or '\x00' in value:
            raise OcrFailure('INVALID_INPUT', 'path must be a nonempty string')
        languages = request.get('languages', 'jpn+eng')
        if languages not in {'jpn+eng', 'jpn', 'eng'}:
            raise OcrFailure('INVALID_INPUT', 'Supported languages: jpn+eng, jpn, eng')
        psm = request.get('psm', 11)
        if type(psm) is not int or psm not in {3, 6, 11, 12}:
            raise OcrFailure('INVALID_INPUT', 'Supported page segmentation modes: 3, 6, 11, 12')
        timeout = request.get('timeout_seconds', 60)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise OcrFailure('INVALID_INPUT', 'timeout_seconds must be within (0, 300]')
        result['languages'] = languages
        path = Path(value).expanduser().resolve()
        result['file'] = str(path)
        if not path.is_file(): raise OcrFailure('FILE_NOT_FOUND', 'Image is not a regular file')
        if path.stat().st_size > MAX_BYTES:
            raise OcrFailure('INPUT_LIMIT', 'Image exceeds 50MB limit')
        executable = shutil.which('tesseract')
        if not executable: raise OcrFailure('ENGINE_UNAVAILABLE', 'Tesseract is not installed')
        try: from PIL import Image
        except ImportError:
            raise OcrFailure('DEPENDENCY_UNAVAILABLE', 'Pillow is required for safe image validation')
        # Credentials and provider configuration are not inherited by OCR.
        env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
               'OMP_THREAD_LIMIT': '2'}
        with tempfile.TemporaryDirectory(prefix='pitchi-ocr-') as temporary:
            copied = Path(temporary) / 'input.image'
            with path.open('rb') as source, copied.open('xb') as target:
                h = hashlib.sha256(); count = 0
                while chunk := source.read(1_048_576):
                    if cancelled(): raise OcrFailure('CANCELLED', 'OCR cancelled')
                    count += len(chunk)
                    if count > MAX_BYTES: raise OcrFailure('INPUT_LIMIT', 'Image exceeds 50MB limit')
                    h.update(chunk); target.write(chunk)
            result['source_sha256'] = h.hexdigest()
            try:
                with Image.open(copied) as image:
                    if image.format not in FORMATS:
                        raise OcrFailure('UNSUPPORTED_IMAGE', 'Supported: PNG/JPEG/BMP/TIFF/WebP')
                    if image.width * image.height > MAX_PIXELS:
                        raise OcrFailure('INPUT_LIMIT', 'Image exceeds 25 million pixel limit')
                    if getattr(image, 'n_frames', 1) != 1:
                        raise OcrFailure('UNSUPPORTED_IMAGE', 'Multi-frame images require explicit frame selection')
                    result['image'] = {'width': image.width, 'height': image.height, 'format': image.format}
                    image.verify()
            except OcrFailure: raise
            except Exception:
                raise OcrFailure('INVALID_IMAGE', 'Image decoding/validation failed')
            version = command([executable, '--version'], 5, cancelled, temporary, env)
            result['engine']['version'] = version.splitlines()[0]
            installed = command([executable, '--list-langs'], 5, cancelled, temporary, env).splitlines()[1:]
            if not set(languages.split('+')).issubset(installed):
                raise OcrFailure('LANGUAGE_UNAVAILABLE', 'Required Tesseract language data is missing')
            output = command([executable, str(copied), 'stdout', '-l', languages,
                              '--psm', str(psm), 'tsv'], timeout, cancelled, temporary, env)
            result.update(parse_tsv(output, psm))
            if digest(path) != result['source_sha256']:
                raise OcrFailure('SOURCE_CHANGED', 'Original changed during OCR; discard this result')
            result['source_unchanged'] = True
            if not result['text']: result['warnings'].append('NO_TEXT_DETECTED')
            result['ok'] = True
    except OcrFailure as exc:
        if exc.code == 'SOURCE_CHANGED':
            result.update(text='', lines=[], confidence=None)
        result['error'] = {'code': exc.code, 'message': str(exc), 'owner': 'tool'}
    except (ValueError, KeyError, TypeError, UnicodeError):
        result['error'] = {'code': 'INVALID_ENGINE_OUTPUT', 'message': 'OCR output could not be parsed', 'owner': 'tool'}
    except OSError:
        result['error'] = {'code': 'IO_ERROR', 'message': 'Image or engine could not be accessed', 'owner': 'tool'}
    result['elapsed_seconds'] = round(time.monotonic() - started, 6)
    return result
