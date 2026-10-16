"""Local streaming transport subprocess: closing it also closes the LLM socket."""
import json
import sys
import urllib.request
from pathlib import Path
from .host_validators import save_bytes
from .shared_model import headers, RejectRedirect


def main():
    invocation=json.loads(Path(sys.argv[1]).read_text())
    body=invocation['body']
    request=urllib.request.Request(invocation['url'],data=json.dumps(body,ensure_ascii=False).encode(),
                                   headers={'Content-Type':'application/json', **headers(invocation['url'].removesuffix('/chat/completions'))})
    content=[];reasoning=[];usage={};model=None;finish=None;trace=[]
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect())
    try:
        with opener.open(request,timeout=invocation['timeout']) as response:
            for raw in response:
                if not raw.startswith(b'data: '):continue
                value=raw[6:].strip()
                if value==b'[DONE]':break
                event=json.loads(value);trace.append(event);model=event.get('model',model)
                usage=event.get('usage') or usage
                for choice in event.get('choices',[]):
                    delta=choice.get('delta',{})
                    content.append(delta.get('content') or '')
                    reasoning.append(delta.get('reasoning_content') or '')
                    finish=choice.get('finish_reason') or finish
        result={'model':model,'choices':[{'message':{'content':''.join(content),'reasoning_content':''.join(reasoning)},'finish_reason':finish}], 'usage':usage}
        save_bytes(Path(invocation['response']),json.dumps(result,ensure_ascii=False).encode())
        save_bytes(Path(invocation['stream']),json.dumps(trace,ensure_ascii=False).encode())
        return 0
    except Exception as exc:
        # No request text, secrets, external API keys or Authorization headers.
        print(type(exc).__name__+': '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
