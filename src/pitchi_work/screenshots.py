"""Private incremental screenshot index/search. OCR is the registered image.ocr tool."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata

APPLICATION_ID = 0x50575332
SCHEMA_VERSION = 1
EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}
DEFAULT_HOME = Path(__file__).resolve().parents[2]


class SearchFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', str(text or '')).casefold().split())


def database_path(request, home=None):
    value = request.get('database')
    if value is not None and (not isinstance(value, str) or not value):
        raise SearchFailure('INVALID_INPUT', 'database must be a local SQLite path')
    path = Path(value) if value else Path(home or DEFAULT_HOME) / 'screenshot_data/index.sqlite3'
    if path.is_symlink(): raise SearchFailure('UNSAFE_DATABASE', 'Database symlinks are not supported')
    return path.expanduser().resolve()


def connect(path, writable=False):
    """Never alter an unrelated existing database."""
    if path.is_file():
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as check:
            if check.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or \
               check.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise SearchFailure('UNSUPPORTED_DATABASE', 'Not a supported Pitchi screenshot index')
    elif not writable:
        raise SearchFailure('INDEX_NOT_FOUND', 'Build this folder with screenshot.index first')
    else:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as initial:
            initial.executescript(f'''
                PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version={SCHEMA_VERSION};
                CREATE TABLE roots(id INTEGER PRIMARY KEY,path TEXT UNIQUE NOT NULL,updated_at TEXT);
                CREATE TABLE ocr_cache(sha256 TEXT,policy TEXT,result TEXT NOT NULL,PRIMARY KEY(sha256,policy));
                CREATE TABLE images(id INTEGER PRIMARY KEY,root_id INTEGER NOT NULL,relative_path TEXT NOT NULL,
                    filename TEXT,app TEXT,captured_at TEXT,date_source TEXT,size INTEGER,mtime_ns INTEGER,
                    sha256 TEXT,policy TEXT,ocr_status TEXT,text TEXT DEFAULT '',confidence REAL,error TEXT,
                    active INTEGER NOT NULL DEFAULT 1,filename_norm TEXT,app_norm TEXT,text_norm TEXT,
                    UNIQUE(root_id,relative_path));
                CREATE VIRTUAL TABLE image_fts USING fts5(filename,app,text);
            ''')
    conn = sqlite3.connect(str(path) if writable else path.as_uri()+'?mode=ro',
                           uri=not writable, timeout=15)
    conn.row_factory = sqlite3.Row
    if writable: conn.execute('PRAGMA journal_mode=WAL')
    else: conn.execute('PRAGMA query_only=ON')
    return conn


def metadata(path, stat):
    match = re.fullmatch(r'Screenshot_(\d{8})_(\d{6})(?:_(.+))?\.[^.]+', path.name, re.I)
    captured = None
    if match:
        try: captured = datetime.strptime(match[1]+match[2], '%Y%m%d%H%M%S').isoformat()
        except ValueError: pass
    return {'filename': path.name, 'app': match[3] if match else None,
            'captured_at': captured or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            'date_source': 'filename_device_time_unspecified_zone' if captured else 'mtime_utc',
            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def file_hash(path, cancelled):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1_048_576):
            if cancelled(): raise SearchFailure('CANCELLED', 'Index cancelled; committed entries are retained')
            h.update(chunk)
    return h.hexdigest()


def scan(root, cancelled):
    paths = []
    def failed(_): raise SearchFailure('SCAN_ERROR', 'Folder enumeration failed; missing entries are not removed')
    for directory, folders, files in os.walk(root, followlinks=False, onerror=failed):
        if cancelled(): raise SearchFailure('CANCELLED', 'Index cancelled')
        folders[:] = sorted(f for f in folders if not f.startswith('.') and not (Path(directory)/f).is_symlink())
        for name in sorted(files):
            path = Path(directory)/name
            if path.suffix.lower() not in EXTENSIONS or path.is_symlink() or name.startswith('.'): continue
            paths.append(path)
            if len(paths) > 100_000: raise SearchFailure('INPUT_LIMIT', 'Folder exceeds 100000 supported images')
    return paths


def root_row(conn, root):
    row = conn.execute('SELECT * FROM roots WHERE path=?', (str(root),)).fetchone()
    if row is None: raise SearchFailure('ROOT_NOT_INDEXED', 'Requested folder is not indexed; use screenshot.index')
    return row


def update_text(conn, image_id, result):
    text = result.get('text', '') if result['ok'] else ''
    conn.execute('UPDATE images SET ocr_status=?,text=?,text_norm=?,confidence=?,error=? WHERE id=?',
                 ('ok' if result['ok'] else 'failed', text, normalized(text), result.get('confidence'),
                  json.dumps(result.get('error'), ensure_ascii=False) if result.get('error') else None, image_id))
    row = conn.execute('SELECT filename,app,text FROM images WHERE id=?', (image_id,)).fetchone()
    conn.execute('DELETE FROM image_fts WHERE rowid=?', (image_id,))
    conn.execute('INSERT INTO image_fts(rowid,filename,app,text) VALUES(?,?,?,?)',
                 (image_id, row['filename'], row['app'] or '', row['text']))


def index_screenshots(request, *, cancelled=lambda: False, home=None, progress=None):
    started = time.monotonic()
    result = {'capability': 'screenshot.index', 'ok': False, 'schema_version': SCHEMA_VERSION,
              'discovered': 0, 'unchanged': 0, 'cached_reused': 0, 'ocr_calls': 0,
              'deleted': 0, 'processed': 0, 'error': None}
    conn = None; lock = None; pool = None
    try:
        if not isinstance(request, dict) or set(request)-{'root','database','workers','languages','psm','retry_errors'}:
            raise SearchFailure('INVALID_INPUT', 'Expected root and registered index options')
        if not isinstance(request.get('root'), str) or not request['root']:
            raise SearchFailure('INVALID_INPUT', 'Explicit root folder is required')
        workers = request.get('workers', 4)
        if type(workers) is not int or not 1 <= workers <= 4:
            raise SearchFailure('INVALID_INPUT', 'workers must be 1..4')
        if type(request.get('retry_errors', False)) is not bool:
            raise SearchFailure('INVALID_INPUT', 'retry_errors must be boolean')
        languages = request.get('languages', 'jpn+eng'); psm = request.get('psm', 11)
        if languages not in {'jpn+eng','jpn','eng'} or type(psm) is not int or psm not in {3,6,11,12}:
            raise SearchFailure('INVALID_INPUT', 'Unsupported OCR language or segmentation mode')
        root = Path(request['root']).expanduser().resolve()
        if not root.is_dir(): raise SearchFailure('ROOT_NOT_FOUND', 'Image folder is not readable')
        database = database_path(request, home)
        if database.is_relative_to(root):
            raise SearchFailure('UNSAFE_DATABASE', 'Store the index outside the source folder')
        database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(str(database)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, 'a+')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise SearchFailure('INDEX_BUSY', 'Another index writer is active')
        conn = connect(database, True)
        executable = shutil.which('tesseract')
        if not executable: raise SearchFailure('ENGINE_UNAVAILABLE', 'Existing image.ocr requires Tesseract')
        version = subprocess.run([executable,'--version'], capture_output=True, text=True, timeout=5,
                                  env={'PATH':os.defpath,'LANG':'C.UTF-8'}).stdout.splitlines()[0]
        settings = {'tool':'image.ocr','schema':1,'engine':version,'languages':languages,'psm':psm}
        policy = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        paths = scan(root, cancelled)
        result.update(discovered=len(paths), database=str(database), root=str(root), ocr_policy=settings)
        conn.execute('INSERT OR IGNORE INTO roots(path) VALUES(?)', (str(root),))
        root_id = root_row(conn, root)['id']
        conn.execute('CREATE TEMP TABLE seen(relative_path TEXT PRIMARY KEY)')
        jobs = {}
        for scan_number,path in enumerate(paths,1):
            if cancelled(): raise SearchFailure('CANCELLED', 'Index cancelled; committed entries are retained')
            relative = path.relative_to(root).as_posix()
            conn.execute('INSERT INTO seen VALUES(?)', (relative,))
            previous = conn.execute('SELECT * FROM images WHERE root_id=? AND relative_path=?', (root_id,relative)).fetchone()
            try:
                before = path.stat(); info = metadata(path, before)
                if before.st_size > 50_000_000: raise SearchFailure('INPUT_LIMIT', 'Image exceeds image.ocr size limit')
                sha = file_hash(path, cancelled)
                after = path.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    raise SearchFailure('SOURCE_CHANGED', 'Source changed during hashing')
            except (OSError, SearchFailure) as exc:
                if isinstance(exc, SearchFailure) and exc.code == 'CANCELLED': raise
                conn.execute('INSERT INTO images(root_id,relative_path,filename,ocr_status,error,active) VALUES(?,?,?,?,?,1) '
                             'ON CONFLICT(root_id,relative_path) DO UPDATE SET ocr_status=excluded.ocr_status,error=excluded.error,active=1',
                             (root_id,relative,path.name,'failed',json.dumps({'code':getattr(exc,'code','IO_ERROR'),'owner':'tool'})))
                image_id=conn.execute('SELECT id FROM images WHERE root_id=? AND relative_path=?',(root_id,relative)).fetchone()[0]
                update_text(conn,image_id,{'ok':False,'error':{'code':getattr(exc,'code','IO_ERROR'),'owner':'tool'}})
                conn.commit();continue
            conn.execute('''INSERT INTO images(root_id,relative_path,filename,app,captured_at,date_source,size,mtime_ns,
                sha256,policy,ocr_status,filename_norm,app_norm) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)
                ON CONFLICT(root_id,relative_path) DO UPDATE SET filename=excluded.filename,app=excluded.app,
                captured_at=excluded.captured_at,date_source=excluded.date_source,size=excluded.size,mtime_ns=excluded.mtime_ns,
                sha256=excluded.sha256,policy=excluded.policy,active=1,filename_norm=excluded.filename_norm,app_norm=excluded.app_norm''',
                (root_id,relative,info['filename'],info['app'],info['captured_at'],info['date_source'],info['size'],info['mtime_ns'],
                 sha,policy,normalized(info['filename']),normalized(info['app'])))
            image_id = conn.execute('SELECT id FROM images WHERE root_id=? AND relative_path=?',(root_id,relative)).fetchone()[0]
            retry = request.get('retry_errors',False) and previous and previous['ocr_status']=='failed'
            if previous and previous['sha256']==sha and previous['policy']==policy and previous['ocr_status'] in {'ok','failed'} and not retry:
                result['unchanged'] += 1
            else:
                cached = conn.execute('SELECT result FROM ocr_cache WHERE sha256=? AND policy=?',(sha,policy)).fetchone()
                if cached and not retry:
                    update_text(conn,image_id,json.loads(cached[0]));result['cached_reused'] += 1
                else:
                    update_text(conn,image_id,{'ok':False,'error':{'code':'OCR_PENDING','owner':'tool'}})
                    conn.execute("UPDATE images SET ocr_status='pending' WHERE id=?",(image_id,))
                    jobs.setdefault(sha,{'path':path,'ids':[]})['ids'].append(image_id)
            conn.commit()
            if progress and scan_number % 100 == 0:
                progress({'stage':'scan','discovered':len(paths),'scheduled_ocr_hashes':len(jobs),'unchanged':result['unchanged']})
        # Only a complete enumeration/update pass may hide missing source paths.
        result['deleted'] = conn.execute('SELECT count(*) FROM images WHERE root_id=? AND active=1 AND relative_path NOT IN (SELECT relative_path FROM seen)',(root_id,)).fetchone()[0]
        conn.execute('UPDATE images SET active=0 WHERE root_id=? AND relative_path NOT IN (SELECT relative_path FROM seen)',(root_id,));conn.commit()
        from .tools import execute_tool  # single OCR capability; no second OCR implementation
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = {pool.submit(execute_tool,'image.ocr',{'path':str(job['path']),'languages':languages,'psm':psm},cancelled=cancelled):sha for sha,job in jobs.items()}
        for future in as_completed(futures):
            if cancelled(): raise SearchFailure('CANCELLED', 'Index cancelled; rerun resumes committed entries')
            sha = futures[future]
            try: output = future.result()
            except Exception: output={'ok':False,'error':{'code':'TOOL_ERROR','owner':'tool'}}
            if output.get('ok') and (output.get('source_sha256')!=sha or output.get('engine',{}).get('version')!=version):
                output={'ok':False,'error':{'code':'SOURCE_CHANGED','owner':'tool'}}
            if output.get('error',{} ) and output['error'].get('code')=='CANCELLED':
                raise SearchFailure('CANCELLED','OCR cancelled')
            # Cache observations, not source paths. A move retains hash provenance.
            cached = {k:v for k,v in output.items() if k not in {'file'}}
            conn.execute('INSERT OR REPLACE INTO ocr_cache VALUES(?,?,?)',(sha,policy,json.dumps(cached,ensure_ascii=False)))
            for image_id in jobs[sha]['ids']: update_text(conn,image_id,output)
            conn.commit();result['ocr_calls'] += 1;result['processed'] += len(jobs[sha]['ids'])
            if progress and (result['ocr_calls'] % 25 == 0 or result['ocr_calls']==len(jobs)):
                progress({'stage':'ocr','finished':result['ocr_calls'],'scheduled':len(jobs),
                          'elapsed_seconds':round(time.monotonic()-started,1)})
        counts={row[0]:row[1] for row in conn.execute('SELECT ocr_status,count(*) FROM images WHERE root_id=? AND active=1 GROUP BY ocr_status',(root_id,))}
        result.update(indexed=sum(counts.values()),ocr_success=counts.get('ok',0),ocr_failed=counts.get('failed',0),ocr_pending=counts.get('pending',0))
        conn.execute('UPDATE roots SET updated_at=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),root_id));conn.commit()
        result['ok'] = True
    except SearchFailure as exc:
        result['error'] = {'code':exc.code,'message':str(exc),'owner':'tool'}
    except (OSError, sqlite3.Error, subprocess.SubprocessError, IndexError):
        result['error'] = {'code':'INDEX_IO_ERROR','message':'Index or OCR preflight could not be accessed','owner':'tool'}
    finally:
        if pool: pool.shutdown(wait=True,cancel_futures=True)
        if conn: conn.close()
        if lock: lock.close()
    result['elapsed_seconds'] = round(time.monotonic()-started,6)
    return result


def snippet(text, query):
    folded=[];positions=[]
    for offset,char in enumerate(text):
        for item in unicodedata.normalize('NFKC',char).casefold():
            if not item.isspace(): folded.append(item);positions.append(offset)
    index=''.join(folded).find(normalized(query))
    start=positions[index] if index>=0 else 0
    return text[max(0,start-80):start+len(query)+240]


def search_screenshots(request, *, cancelled=lambda: False, home=None):
    result={'capability':'screenshot.search','ok':False,'matches':[],'error':None,
            'semantics':'lexical_candidates_not_visual_or_intent_verification'}
    conn=None
    try:
        if not isinstance(request,dict) or set(request)-{'root','database','query','app','date_from','date_to','limit'}:
            raise SearchFailure('INVALID_INPUT','Expected root, query and registered filters')
        query=request.get('query','');limit=request.get('limit',30)
        if not isinstance(query,str) or len(query)>256 or type(limit) is not int or not 1<=limit<=100:
            raise SearchFailure('INVALID_INPUT','Query max 256 characters; limit 1..100')
        if not isinstance(request.get('root'),str) or not request['root']:
            raise SearchFailure('INVALID_INPUT','Explicit indexed root is required')
        if not isinstance(request.get('app',''),str):raise SearchFailure('INVALID_INPUT','app must be a string')
        for key in ('date_from','date_to'):
            value=request.get(key)
            if value is not None:
                if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
                    raise SearchFailure('INVALID_INPUT','Dates must be YYYY-MM-DD')
                try: date.fromisoformat(value)
                except ValueError:raise SearchFailure('INVALID_INPUT','Invalid calendar date')
        if request.get('date_from') and request.get('date_to') and request['date_from']>request['date_to']:
            raise SearchFailure('INVALID_INPUT','date_from must not exceed date_to')
        if cancelled():raise SearchFailure('CANCELLED','Search cancelled')
        conn=connect(database_path(request,home))
        root=Path(request['root']).expanduser().resolve();row=root_row(conn,root)
        needle=normalized(query);params=[row['id']];where=['active=1','root_id=?']
        fts_ids=set()
        if needle:
            phrase='"'+query.replace('"','""')+'"'
            try: fts_ids={r[0] for r in conn.execute('SELECT rowid FROM image_fts WHERE image_fts MATCH ?',(phrase,))}
            except sqlite3.OperationalError: pass  # punctuation-only query still has literal matching
            where.append('(instr(filename_norm,?)>0 OR instr(app_norm,?)>0 OR instr(text_norm,?)>0'+
                         (' OR id IN (SELECT rowid FROM image_fts WHERE image_fts MATCH ?)' if fts_ids else '')+')')
            params += [needle]*3+([phrase] if fts_ids else [])
        if request.get('app'):where.append('app_norm=?');params.append(normalized(request['app']))
        if request.get('date_from'):where.append('substr(captured_at,1,10)>=?');params.append(request['date_from'])
        if request.get('date_to'):where.append('substr(captured_at,1,10)<=?');params.append(request['date_to'])
        matches=[]
        for item in conn.execute('SELECT * FROM images WHERE '+' AND '.join(where),params):
            reasons=[];score=0
            for field,label,weight in (('app_norm','app_name',40),('filename_norm','filename',20),('text_norm','ocr_text',10)):
                if needle and needle in (item[field] or ''):reasons.append(label);score+=weight
            if item['id'] in fts_ids:reasons.append('fts_phrase');score+=5
            matches.append({'id':item['id'],'file':str(root/item['relative_path']),'relative_path':item['relative_path'],
                'filename':item['filename'],'datetime':item['captured_at'],'date_source':item['date_source'],
                'app':item['app'],'source_sha256':item['sha256'],'ocr_status':item['ocr_status'],
                'ocr_match':snippet(item['text'] or '',query) if 'ocr_text' in reasons or 'fts_phrase' in reasons else '',
                'match_reasons':reasons or ['filter_only'],'score':score})
        matches.sort(key=lambda r:(r['score'],r['datetime'] or '',r['filename']),reverse=True)
        coverage={item[0]:item[1] for item in conn.execute('SELECT ocr_status,count(*) FROM images WHERE root_id=? AND active=1 GROUP BY ocr_status',(row['id'],))}
        result.update(ok=True,query=query,total=len(matches),matches=matches[:limit],root=str(root),
                      index_updated_at=row['updated_at'],limit=limit,ocr_coverage=coverage)
    except SearchFailure as exc:result['error']={'code':exc.code,'message':str(exc),'owner':'tool'}
    except (OSError,sqlite3.Error):result['error']={'code':'INDEX_IO_ERROR','message':'Index could not be read','owner':'tool'}
    finally:
        if conn:conn.close()
    return result
