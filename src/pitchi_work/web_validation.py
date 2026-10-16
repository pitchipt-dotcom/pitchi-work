"""Finite static-web Host validation. Worker code is never the test oracle."""
import base64
import difflib
from html.parser import HTMLParser
import json
from pathlib import Path
from .runner import HostDefect


class Assets(HTMLParser):
    def __init__(self):super().__init__();self.scripts=[];self.styles=[];self.inline=False
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='script':
            if not attrs.get('src'):self.inline=True
            else:self.scripts.append(attrs['src'])
        if tag=='link' and attrs.get('rel')=='stylesheet':self.styles.append(attrs.get('href'))


def observe(run,contract,attempt):
    from .code_validation import invocation
    workspace=Path(run.state()['workspace']);checks=[];executions=[];diffs={}
    exists=all((workspace/n).is_file() for n in contract['expected_outputs'])
    checks.append({'validator':'required_files','pass':exists})
    if not exists:return checks,executions,diffs
    assets=Assets();assets.feed((workspace/'index.html').read_text())
    syntax=invocation(run,['/usr/bin/node','--check',str(workspace/'app.js')],30)
    executions.append(syntax)
    checks.append({'validator':'web_syntax','pass':syntax['exit_code']==0 and not assets.inline and
                   assets.scripts==['app.js'] and assets.styles==['style.css']})
    for name in contract['expected_outputs']:
        original=run.path/'baseline_files'/name
        before=original.read_text() if original.is_file() else ''
        diffs[name]=''.join(difflib.unified_diff(before.splitlines(True),(workspace/name).read_text().splitlines(True),fromfile=name,tofile=name))
    if not checks[-1]['pass']:return checks,executions,diffs
    probe=Path(__file__).with_name('web_probe.mjs')
    execution=invocation(run,['/usr/bin/node','/tmp/pitchi-web-probe.mjs',str(workspace),contract['behavior']],
                         contract['timeout'],host_files=[(probe,'/tmp/pitchi-web-probe.mjs')])
    lines=execution['stdout'].splitlines()
    reports=[line.partition('=')[2] for line in lines if line.startswith('PITCHI_HOST_WEB_RESULT=')]
    if len(reports)!=1:raise HostDefect('Host browser receipt missing: '+execution['stderr'][-1500:])
    observation=json.loads(reports[0])
    if not observation.get('completed') and not observation.get('presentation'):
        raise HostDefect('Host browser startup failure: '+str(observation.get('errors')))
    images=[line.partition('=')[2] for line in lines if line.startswith('PITCHI_HOST_SCREENSHOT=')]
    if images:
        destination=run.path/'artifacts'/f"{contract['task_id']}_browser_{attempt}.png"
        from .host_validators import save_bytes
        save_bytes(destination,base64.b64decode(images[0],validate=True))
        observation['screenshot_reference']=str(destination.relative_to(run.path))
    execution.update(stdout='Host browser details saved in observation',web_observation=observation,
                     tests_run=observation.get('observation',{}).get('tests_run',0))
    executions.append(execution)
    checks.extend(observation.get('checks',[]))
    if not observation.get('completed'):checks.append({'validator':'browser_interaction','pass':False})
    return checks,executions,diffs
