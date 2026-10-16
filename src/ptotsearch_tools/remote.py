"""Fixed SSH file inspection/publication, reusing PTOTsearch's bounded file release procedure."""
import base64
import json
import shlex
import subprocess

# No incoming request can supply commands. Paths and SSH identity are operator bindings.
INSPECT = r'''
import base64,hashlib,json,pathlib,sys,re
p=json.load(sys.stdin);app=pathlib.Path(p['app']).resolve();result={}
for name in p['files']:
 path=app/name
 if name not in ('src/GuidelineSectionSearch.php','public/assets/app.js') and not re.fullmatch(r'config/guideline_metadata/[a-z0-9][a-z0-9_-]{0,99}\.json',name):raise ValueError('File outside publication scope')
 if not path.resolve().is_relative_to(app) or path.is_symlink():raise ValueError('Unsafe file')
 result[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'content_b64':base64.b64encode(path.read_bytes()).decode()} if path.exists() else {'sha256':None,'content_b64':None}
catalog=[]
for path in (app/'config/guideline_metadata').glob('*.json'):
 m=json.loads(path.read_text());catalog.append({'id':m['id'],'official_url':m['official_url'],'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
print(json.dumps({'files':result,'catalog':catalog,'database_write':False}))
'''
RELEASE = r'''
import base64,fcntl,hashlib,json,os,pathlib,sys,re
p=json.load(sys.stdin);app=pathlib.Path(p['app']).resolve();backup=pathlib.Path(p['backup']);backup.mkdir(parents=True,exist_ok=True,mode=0o700)
with (backup.parent/'guideline-release.lock').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 digest=lambda path:hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
 # Validate every input/drift before any application file is written.
 for name,row in p['files'].items():
  path=app/name
  if name not in ('src/GuidelineSectionSearch.php','public/assets/app.js') and not re.fullmatch(r'config/guideline_metadata/[a-z0-9][a-z0-9_-]{0,99}\.json',name):raise ValueError('File outside publication scope')
  if not path.resolve().is_relative_to(app) or path.is_symlink():raise ValueError('Unsafe file')
  if digest(path)!=row['before_sha256']:raise ValueError('Production drift: '+name)
  if hashlib.sha256(base64.b64decode(row['content_b64'])).hexdigest()!=row['after_sha256']:raise ValueError('Bundle mismatch')
 if (backup/'before.json').exists():raise ValueError('Release already reserved; reconcile before retry')
 original={};applied=[]
 for name,row in p['files'].items():
  path=app/name;original[name]=base64.b64encode(path.read_bytes()).decode() if path.exists() else None
 (backup/'before.json').write_text(json.dumps({'files':original,'hashes':{n:r['before_sha256'] for n,r in p['files'].items()}}));os.chmod(backup/'before.json',0o600)
 try:
  for name,row in p['files'].items():
   path=app/name;path.parent.mkdir(parents=True,exist_ok=True)
   staged=path.with_name(path.name+'.guideline-candidate');staged.write_bytes(base64.b64decode(row['content_b64']))
   if name.endswith('.php'):
    import subprocess
    subprocess.run(['php','-l',str(staged)],check=True,stdout=subprocess.DEVNULL)
   os.replace(staged,path);applied.append(name)
  print(json.dumps({'applied':applied,'backup_verified':True,'database_write':False}))
 except BaseException:
  for name in reversed(applied):
   path=app/name;data=original[name]
   if data is None:path.unlink()
   else:path.write_bytes(base64.b64decode(data))
  raise
'''
ROLLBACK = r'''
import base64,fcntl,hashlib,json,pathlib,sys,os,re
p=json.load(sys.stdin);app=pathlib.Path(p['app']).resolve();backup=pathlib.Path(p['backup'])
with (backup.parent/'guideline-release.lock').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 prior=json.loads((backup/'before.json').read_text());restored=[]
 for name,row in p['files'].items():
  path=app/name
  if name not in ('src/GuidelineSectionSearch.php','public/assets/app.js') and not re.fullmatch(r'config/guideline_metadata/[a-z0-9][a-z0-9_-]{0,99}\.json',name):raise ValueError('File outside rollback scope')
  if not path.resolve().is_relative_to(app) or path.is_symlink():raise ValueError('Unsafe rollback file')
  data=prior['files'][name]
  if (hashlib.sha256(base64.b64decode(data)).hexdigest() if data is not None else None)!=row['before_sha256']:raise ValueError('Backup hash mismatch')
  actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
  if actual!=row['after_sha256']:raise ValueError('Rollback drift: '+name)
 for name,row in p['files'].items():
  path=app/name;data=prior['files'][name]
  if data is None:path.unlink()
  else:
   temp=path.with_name(path.name+'.guideline-restore');temp.write_bytes(base64.b64decode(data));os.replace(temp,path)
  restored.append(name)
 print(json.dumps({'restored':restored,'database_restored':False}))
'''

def call(config, operation, payload):
    script={'inspect':INSPECT,'release':RELEASE,'rollback':ROLLBACK}[operation]
    p=subprocess.run([*config['ssh_argv'],'python3','-c',shlex.quote(script)],
                     input=json.dumps(payload),capture_output=True,text=True,timeout=90)
    if p.returncode:raise ValueError('Remote '+operation+' failed: '+p.stderr[-600:])
    return json.loads(p.stdout)
