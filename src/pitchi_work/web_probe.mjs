// Host-owned browser observations; never loaded from the Worker workspace.
import {spawn} from 'node:child_process';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
const root=process.argv[2],behavior=process.argv[3];
const result={completed:false,checks:[],errors:[],console_errors:[],network_failures:[],browser:'isolated_chrome',behavior};
const allowed={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css'};
const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'};
const server=http.createServer(async(req,res)=>{
 try{const file=allowed[new URL(req.url,'http://localhost').pathname];if(!file){res.writeHead(404);res.end();return}
 const data=await fs.readFile(path.join(root,file));res.writeHead(200,{'Content-Type':types[path.extname(file)],
 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'none'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"});res.end(data)
 }catch(_){res.writeHead(500);res.end()}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const url='http://127.0.0.1:'+server.address().port+'/';
const profile=await fs.mkdtemp(path.join(os.tmpdir(),'pitchi-web-probe-'));
const chrome=spawn('/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
 '--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions',
 '--no-first-run','--no-default-browser-check','--disable-features=MediaRouter,OptimizationHints',
 '--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
let socket;
const delay=ms=>new Promise(r=>setTimeout(r,ms));
try{
 for(const file of Object.keys(allowed)){
  const response=await fetch(url.replace(/\/$/,'')+file);
  if(response.status!==200)throw Error('HTTP '+file+' did not return 200');
 }
 result.checks.push({validator:'local_http',pass:true});
 let port;
 for(let i=0;i<100;i++){try{port=Number((await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]);break}catch(_){}await delay(100)}
 if(!port)throw Error('Host browser did not start');
 const pages=await(await fetch('http://127.0.0.1:'+port+'/json/list')).json();
 socket=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
 await new Promise((yes,no)=>{socket.addEventListener('open',yes,{once:true});socket.addEventListener('error',no,{once:true})});
 let id=0;const pending=new Map();
 socket.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.method==='Runtime.exceptionThrown')result.errors.push({text:m.params.exceptionDetails.text,line:m.params.exceptionDetails.lineNumber,
  description:(m.params.exceptionDetails.exception?.description||'').slice(0,1000)});
  if(m.method==='Runtime.consoleAPICalled'&&m.params.type==='error')result.console_errors.push(m.params.args.map(a=>String(a.value??a.description??'').slice(0,500)));
  if(m.method==='Network.loadingFailed')result.network_failures.push({request_id:m.params.requestId,error:m.params.errorText});
  if(pending.has(m.id)){const [yes,no]=pending.get(m.id);pending.delete(m.id);m.error?no(Error(m.error.message)):yes(m.result)}});
 const call=(method,params={})=>new Promise((yes,no)=>{const n=++id;pending.set(n,[yes,no]);socket.send(JSON.stringify({id:n,method,params}))});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(r.exceptionDetails)throw Error('Page evaluation failed');return r.result.value};
 await call('Runtime.enable');await call('Page.enable');await call('Network.enable');
 // Repeatable host seed, not app-private state or an app-authored oracle.
 await call('Page.addScriptToEvaluateOnNewDocument',{source:'let pitchiSeed=928371;Math.random=()=>{pitchiSeed=(1664525*pitchiSeed+1013904223)>>>0;return pitchiSeed/4294967296}'});
 await call('Page.navigate',{url});
 for(let n=0;n<100;n++){if(await evaluate("document.readyState==='complete'"))break;await delay(50)}
 const baseline=await evaluate(`({title:document.title,body:document.body.innerText.slice(0,300),
 elements:['answer','submit','feedback','next'].every(id=>!!document.getElementById(id)),
 visible_controls:['answer','submit','next'].every(id=>{const e=document.getElementById(id);return e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0})})`);
 result.presentation=baseline;
 if(!baseline.title||!baseline.body||!baseline.elements||!baseline.visible_controls)throw Error('Visible app and registered interaction elements required');
 const observation=await evaluate(`(()=>{
 const get=id=>document.getElementById(id),seen=new Set(),failures=[];
 function submit(value){get('answer').value=value;get('answer').dispatchEvent(new Event('input',{bubbles:true}));get('submit').click();return get('feedback').dataset.result}
 if(${JSON.stringify(behavior)}.startsWith('arithmetic_practice')){
  for(let n=0;n<1000;n++){
   const a=Number(get('operand-a')?.textContent),b=Number(get('operand-b')?.textContent);
   if(!Number.isInteger(a)||!Number.isInteger(b)||a<1||a>9||b<1||b>9){failures.push('Operands must be integers 1..9');break}
   const key=a+','+b;seen.add(key);
   if(submit('')==='correct'||submit('abc')==='correct')failures.push('Invalid answer accepted');
   if(submit(String(a*b+1))!=='incorrect')failures.push('Wrong answer not rejected');
   const incorrect=get('feedback').textContent;
   if(submit(String(a*b))!=='correct')failures.push('Correct answer not accepted');
   if(!get('feedback').textContent||get('feedback').textContent===incorrect)failures.push('Feedback not differentiated');
   get('next').click();
   if(get('answer').value!==''||get('feedback').textContent.trim()!=='')failures.push('Next did not reset answer/feedback');
   let next=get('operand-a').textContent+','+get('operand-b').textContent;
   // A random repeat is not a stuck UI. Verify progression within a bounded
   // number of clicks, without inventing a no-adjacent-duplicate requirement.
   for(let retry=0;next===key&&retry<5;retry++){get('next').click();next=get('operand-a').textContent+','+get('operand-b').textContent}
   if(next===key)failures.push('Next stayed on the same operands after six clicks');
   if(failures.length||seen.size===81)break;
  }
  if(seen.size!==81)failures.push('Not all 81 operand combinations reachable under fixed seed');
 }else{
  get('answer').value='example';get('submit').click();if(!get('feedback').textContent.trim())failures.push('No response to submit');
  get('next').click();get('answer').value='second';get('submit').click();if(!get('feedback').textContent.trim())failures.push('Cannot repeat interaction');
 }
 return {failures,combinations:seen.size,tests_run:${JSON.stringify(behavior)}.startsWith('arithmetic_practice')?seen.size*5:2,
 focusable:['answer','submit','next'].every(id=>get(id).tabIndex>=0),samples:[...seen].slice(0,5)};
})()`);
 result.observation=observation;
 result.checks.push({validator:'browser_interaction',pass:observation.failures.length===0&&result.errors.length===0&&result.console_errors.length===0&&result.network_failures.length===0});
 if(behavior.startsWith('arithmetic_practice'))result.checks.push({validator:'arithmetic_practice',pass:observation.failures.length===0&&observation.combinations===81});
 if(behavior==='arithmetic_practice_review'){
  // Independent review scenario, not contamination from the 81-case regression.
  await call('Storage.clearDataForOrigin',{origin:new URL(url).origin,storageTypes:'all'});
  await call('Page.navigate',{url});
  for(let n=0;n<100;n++){if(await evaluate("document.readyState==='complete' && !!document.getElementById('operand-a')"))break;await delay(50)}
  const review=await evaluate(`(()=>{const get=id=>document.getElementById(id),failures=[];
   if(!get('review')||!get('review-count'))return {failures:['Missing review controls']};
   const count=()=>Number(get('review-count').dataset.count);
   const before=count(),a=Number(get('operand-a').textContent),b=Number(get('operand-b').textContent);
   get('answer').value=String(a*b+1);get('submit').click();const queued=count();
   if(!Number.isInteger(queued)||queued<=before)failures.push('Wrong answer not queued');
   get('next').click();if(count()!==queued)failures.push('Queue lost on next');
   get('review').click();
   if(Number(get('operand-a').textContent)!==a||Number(get('operand-b').textContent)!==b)failures.push('Later review did not retrieve the wrong problem');
   get('answer').value=String(a*b+1);get('submit').click();if(count()!==queued)failures.push('Duplicate wrong answer duplicated queue');
   get('answer').value=String(a*b);get('submit').click();if(count()!==queued-1)failures.push('Mastered reviewed problem not removed');
   get('next').click();if(get('answer').value!==''||get('feedback').textContent.trim()!=='')failures.push('Next after review did not reset');
   return {failures,before,queued,remaining:count(),reviewed_operands:[a,b]}})()`);
  result.review_observation=review;
  result.checks.push({validator:'incorrect_answer_review',pass:review.failures.length===0});
 }
 result.completed=true;
 await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 const screenshot=await call('Page.captureScreenshot',{format:'png'});
 console.log('PITCHI_HOST_SCREENSHOT='+screenshot.data);
 console.log('PITCHI_HOST_WEB_RESULT='+JSON.stringify(result));
}catch(error){result.errors.push(error.message);console.log('PITCHI_HOST_WEB_RESULT='+JSON.stringify(result));process.exitCode=2}
finally{socket?.close();chrome.kill('SIGTERM');await Promise.race([new Promise(r=>chrome.once('exit',r)),delay(1000)]);
 if(chrome.exitCode===null&&chrome.signalCode===null)chrome.kill('SIGKILL');server.close();await fs.rm(profile,{recursive:true,force:true})}
