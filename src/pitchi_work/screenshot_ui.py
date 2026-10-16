"""Loopback-only read-only browser entry. Search authority is screenshot.search."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import signal
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .screenshots import connect,database_path,root_row,SearchFailure
from .tools import execute_tool

PAGE = '''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pitchi-work Screenshot Search</title>
<style>body{font:16px system-ui;background:#111827;color:#e5e7eb;margin:24px}h1{font-size:24px}
input,select,button{font:inherit;padding:9px;border:1px solid #64748b;border-radius:5px;background:#1f2937;color:inherit}
form{display:flex;gap:10px;flex-wrap:wrap}#cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}
article{background:#1f2937;padding:14px;border-radius:10px;overflow-wrap:anywhere}article img{width:100%;height:260px;object-fit:contain;background:#0f172a}
pre{white-space:pre-wrap;font-size:14px;max-height:280px;overflow:auto}a{color:#93c5fd}small{display:block;margin:8px 0}
.warning{color:#fbbf24}#status{margin:20px 0}label{display:flex;align-items:center;gap:5px}</style>
<h1>Pitchi-work · Screenshot Search</h1>
<p>文字列とOCR上の手がかりで候補を絞り、人間が画像から最終選択します。自動で意味的な正解を認定しません。LLM/APIは呼びません。</p>
<form id="form"><input id="q" placeholder="ChatGPT / n8n / 検索したい文字" aria-label="検索語">
<input id="intent" placeholder="任意: ワークフローを編集 / 文章の修正指示" aria-label="探す作業内容">
<label>開始日<input id="from" type="date"></label><label>終了日<input id="to" type="date"></label>
<select id="app" aria-label="アプリ"><option value="">すべてのアプリ</option></select><button>検索</button></form>
<p class="warning">個人画像・OCRを表示するローカル専用画面です。外部共有しないでください。アプリ絞り込みはファイル名由来です。</p>
<div id="status" role="status"></div><p><span id="selected">選択 0枚</span> <button id="export" type="button">選択参照をJSON保存</button></p><section id="cards"></section>
<script>
const el=id=>document.getElementById(id);
el('q').value=new URLSearchParams(location.search).get('q')||'';
el('intent').value=new URLSearchParams(location.search).get('intent')||'';
const selected=new Map();
function selectionStatus(){el('selected').textContent='選択 '+selected.size+'枚（人間による選択）'}
el('export').onclick=()=>{
 const blob=new Blob([JSON.stringify({verification_mode:'assisted',selection_owner:'human',images:[...selected.values()]},null,2)],{type:'application/json'});
 const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='private-screenshot-selection.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
let searchGeneration=0;
async function search(){
 const generation=++searchGeneration;
 el('status').textContent='検索中…';el('cards').replaceChildren();selected.clear();selectionStatus();
 const p=new URLSearchParams({query:el('q').value,limit:el('intent').value?'5':'60'});
 if(el('intent').value)p.set('intent',el('intent').value);
 for(const [key,id] of [['date_from','from'],['date_to','to'],['app','app']])if(el(id).value)p.set(key,el(id).value);
 try{
 const r=await fetch('/api/search?'+p);const data=await r.json();if(!data.ok)throw Error(data.error.message);
 if(generation!==searchGeneration)return;
 el('status').textContent='検索語「'+data.query+'」 · '+data.total+'件（表示 '+data.matches.length+'件） · 索引更新 '+(data.index_updated_at||'完了した更新記録なし')+
 ' · OCR 成功 '+(data.ocr_coverage.ok||0)+' / 失敗 '+(data.ocr_coverage.failed||0)+' / 未処理 '+(data.ocr_coverage.pending||0);
 if(data.requires_human_selection)el('status').textContent+=' · assisted: 最終選択は人間 / LLM呼出し 0';
 for(const row of data.matches){
 const card=document.createElement('article');const img=document.createElement('img');img.loading='lazy';img.src='/thumb?id='+row.id;img.alt=row.filename;
 const title=document.createElement('strong');title.textContent=row.filename;
 const details=document.createElement('small');details.textContent=(row.app||'アプリ不明')+' · '+(row.datetime||'日時不明')+' · OCR '+row.ocr_status;
 const why=document.createElement('small');why.textContent='一致: '+row.match_reasons.join(', ')+' · 文字列score '+row.score+'（確率ではありません）'+
 (row.activity_hints?' · 作業手がかり: '+(row.activity_hints.join(', ')||'なし'):'');
 const snippet=document.createElement('pre');snippet.textContent=row.ocr_match||'OCR一致箇所なし（メタデータ候補）';
 const link=document.createElement('a');link.href='/image?id='+row.id;link.target='_blank';link.rel='noopener';link.textContent='原画像を確認';
 const choose=document.createElement('label');const checkbox=document.createElement('input');checkbox.type='checkbox';
 checkbox.onchange=()=>{if(checkbox.checked)selected.set(row.id,{id:row.id,file:row.file,source_sha256:row.source_sha256});else selected.delete(row.id);selectionStatus()};
 choose.append(checkbox,document.createTextNode('候補を確認して選択'));
 const button=document.createElement('button');button.textContent='OCR全文';button.type='button';button.onclick=async()=>{
  try{const r=await fetch('/api/ocr?id='+row.id);const d=await r.json();snippet.textContent=d.text||JSON.stringify(d.error||{},null,2);}
  catch(_){snippet.textContent='OCR読取に失敗しました';}
 };
 card.append(img,title,details,why,snippet,link,document.createTextNode(' '),button,choose);el('cards').append(card);
 }
 }catch(e){if(generation===searchGeneration)el('status').textContent='エラー: '+e.message;}
}
el('form').onsubmit=e=>{e.preventDefault();search()};
fetch('/api/options').then(r=>r.json()).then(data=>{for(const app of data.apps||[]){const o=document.createElement('option');o.value=app;o.textContent=app;el('app').append(o)}search()}).catch(()=>el('status').textContent='索引へ接続できません');
</script></html>'''


def server(home,root,database=None,port=0,base_path=''):
    if type(port) is not int or not 0<=port<=65535:raise ValueError('port must be 0..65535')
    root=Path(root).expanduser().resolve();request={'root':str(root)}
    if database:request['database']=str(database)
    db=database_path(request,home)
    with connect(db) as conn:root_row(conn,root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass  # no private filenames, queries or client details in logs

        def reply(self,status,body,kind='application/json; charset=utf-8'):
            if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'; connect-src 'self'; base-uri 'none'")
            self.end_headers()
            try:
                if self.command!='HEAD':self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

        def do_GET(self):
            expected={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in expected:
                self.reply(403,{'error':'Non-loopback Host refused'});return
            if self.headers.get('Origin') and self.headers['Origin'] not in {'http://'+h for h in expected}:
                self.reply(403,{'error':'Cross-origin request refused'});return
            url=urlsplit(self.path);params=parse_qs(url.query)
            value=lambda key,default='':params.get(key,[default])[0]
            try:
                if url.path=='/':
                    page=PAGE
                    for prefix in ('/api/','/thumb?','/image?'):
                        page=page.replace("'"+prefix,"'"+base_path+prefix)
                    self.reply(200,page.encode(),'text/html; charset=utf-8');return
                if url.path=='/api/search':
                    args={**request,'query':value('query'),'limit':int(value('limit','60'))}
                    for key in ('app','date_from','date_to'):
                        if value(key):args[key]=value(key)
                    name='screenshot.search'
                    if value('intent'):
                        name='screenshot.assisted_search';args.update(intent=value('intent'),candidate_limit=30)
                    result=execute_tool(name,args,home=home)
                    self.reply(200 if result['ok'] else 400,result);return
                with connect(db) as conn:
                    rid=root_row(conn,root)['id']
                    if url.path=='/api/options':
                        apps=[r[0] for r in conn.execute('SELECT DISTINCT app FROM images WHERE root_id=? AND active=1 AND app IS NOT NULL ORDER BY app',(rid,))]
                        self.reply(200,{'apps':apps});return
                    if url.path not in {'/image','/thumb','/api/ocr'}:self.reply(404,{'error':'Not found'});return
                    row=conn.execute('SELECT * FROM images WHERE id=? AND root_id=? AND active=1',(int(value('id')),rid)).fetchone()
                    if row is None:self.reply(404,{'error':'Image is not in this indexed root'});return
                    if url.path=='/api/ocr':
                        self.reply(200,{'text':row['text'],'status':row['ocr_status'],'source_sha256':row['sha256'],
                                        'confidence':row['confidence'],'error':json.loads(row['error']) if row['error'] else None});return
                    path=root/row['relative_path']
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        self.reply(403,{'error':'Image outside approved root'});return
                    if path.stat().st_size>50_000_000:self.reply(413,{'error':'Image too large'});return
                    data=path.read_bytes()
                    if hashlib.sha256(data).hexdigest()!=row['sha256']:
                        self.reply(409,{'error':'Source changed; refresh index'});return
                    from PIL import Image
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width*image.height>25_000_000:raise ValueError('Image exceeds pixel limit')
                        if url.path=='/thumb':
                            image.thumbnail((300,400));output=io.BytesIO();image.convert('RGB').save(output,format='JPEG',quality=80)
                            self.reply(200,output.getvalue(),'image/jpeg');return
                        kind=Image.MIME.get(image.format)
                        if not kind or image.format not in {'PNG','JPEG','BMP','TIFF','WEBP'}:raise ValueError('Unsupported image format')
                        self.reply(200,data,kind);return
            except (ValueError,OSError,sqlite3.Error,SearchFailure):self.reply(400,{'error':'Invalid request or unavailable indexed source'})
        do_HEAD=do_GET

    http=ThreadingHTTPServer(('127.0.0.1',port),Handler);http.daemon_threads=True
    return http


def serve(home,root,database=None,port=0):
    http=server(home,root,database,port)
    print(f'Pitchi-work Screenshot Search: http://127.0.0.1:{http.server_port}/',flush=True)
    print('Local/private only. Ctrl+C stops this UI; NAS and index remain unchanged.',flush=True)
    previous=signal.getsignal(signal.SIGINT)
    def stop(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGINT,stop)
    try:http.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:http.server_close();signal.signal(signal.SIGINT,previous)
    return 0
