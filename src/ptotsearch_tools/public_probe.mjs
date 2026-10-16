import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
const [origin,definitionsPath,output]=process.argv.slice(2);
const u=new URL(origin);if(u.protocol!=='https:'||u.username||u.password)throw Error('HTTPS origin required');
const definitions=JSON.parse(fs.readFileSync(definitionsPath,'utf8'));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'ptot-public-'));
const child=spawn('/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--disable-background-networking','--disable-sync','--no-first-run','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
const pause=ms=>new Promise(r=>setTimeout(r,ms));let socket;
try {
 let port;for(let n=0;n<100;n++){try{port=Number(fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0]);break;}catch{}await pause(100);}
 const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();socket=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
 let seq=0;const pending=new Map();socket.addEventListener('message',e=>{const v=JSON.parse(e.data);if(pending.has(v.id)){const [yes,no]=pending.get(v.id);pending.delete(v.id);v.error?no(Error(v.error.message)):yes(v.result);}});
 const call=(method,params={})=>new Promise((yes,no)=>{const id=++seq;pending.set(id,[yes,no]);socket.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error('Public page evaluation failed');return r.result.value;};
 await call('Page.enable');await call('Page.addScriptToEvaluateOnNewDocument',{source:"Object.defineProperty(navigator,'doNotTrack',{get:()=> '1'})"});await call('Network.enable');await call('Network.setExtraHTTPHeaders',{headers:{DNT:'1'}});
 const observations=[];
 for(const d of definitions){
  await call('Page.navigate',{url:origin+'/?'+new URLSearchParams({q:d.query})});
  let ready=false;for(let n=0;n<100;n++){ready=await evaluate("!!document.querySelector('#search-form')");if(ready)break;await pause(100);}
  if(!ready)throw Error('Search form unavailable');
  await evaluate(`(()=>{const f=document.querySelector('#search-form');f.elements.q.value=${JSON.stringify(d.query)};f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));})()`);
  let card;
  for(let n=0;n<150;n++){
   card=await evaluate(`(()=>{const a=[...document.querySelectorAll('.guideline-card')].find(n=>n.querySelector('a')?.getAttribute('href')===${JSON.stringify(d.expected.official_url)});return a?{text:a.innerText,title:a.querySelector('h3')?.textContent,url:a.querySelector('a')?.href}:null;})()`);
   if(card)break;await pause(200);
  }
  if(!card||card.title!==d.expected.title||!card.text.includes(d.expected.issuer)||!card.text.includes(d.expected.edition)||!card.text.includes('メタデータのみ')||card.url!==d.expected.official_url||card.text.includes('該当箇所：'))throw Error('Public card acceptance failed: '+d.id);
  observations.push({query:d.query,...card});
 }
 fs.writeFileSync(output,JSON.stringify({passed:true,observations},null,2),{mode:0o600});
 console.log(JSON.stringify({passed:true,queries:observations.length}));
} finally {socket?.close();child.kill('SIGTERM');await pause(300);fs.rmSync(profile,{recursive:true,force:true});}
