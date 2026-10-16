"""Thin foreground CLI. HostCore/Driver, not the UI, own all decisions."""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import sys
import termios
import threading
import tty
from .store import RunStore,load,Cancelled
from .model import health
from .workspace import create_run
from .driver import Driver
from .mission_controller import MissionController,create_mission,STABLE_WORKFLOW_VERSION
from .tools import catalog,execute_tool,resolve_tool_request,save_tool_result

DEFAULT_HOME=Path(__file__).resolve().parents[2]


def foreground_tool(home,name,request):
    """Tool success is an observation, never mission completion."""
    cancelled=threading.Event();previous={}
    for sig in (signal.SIGINT,signal.SIGTERM):
        previous[sig]=signal.signal(sig,lambda *_:cancelled.set())
    try:
        def progress(record):print(json.dumps({'tool':name,'progress':record},ensure_ascii=False),file=sys.stderr,flush=True)
        result=execute_tool(name,request,cancelled=cancelled.is_set,home=home,progress=progress)
        artifact=save_tool_result(home,name,request,result)
        print(json.dumps({'result':result,'artifact':artifact},ensure_ascii=False,indent=2))
        return 0 if result['ok'] else 2
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


def foreground(run):
    done=threading.Event();original=None;old_signals={}
    def cancel(*_):run.request_cancel()
    for sig in (signal.SIGINT,signal.SIGTERM):
        old_signals[sig]=signal.signal(sig,cancel)
    if sys.stdin.isatty() and run.state().get('llm_config',{}).get('provider','local')=='local' and not run.state().get('resolution_policy',{}).get('cloud'):
        original=termios.tcgetattr(sys.stdin);tty.setcbreak(sys.stdin)
        def input_loop():
            while not done.wait(.1):
                if select.select([sys.stdin],[],[],.1)[0]:
                    if os.read(sys.stdin.fileno(),1) in (b'q',b'\x1b'):run.request_cancel()
        thread=threading.Thread(target=input_loop,daemon=True);thread.start()
    try:
        controls='q / Esc / Ctrl+C' if original is not None else 'Ctrl+C'
        print('Run: '+run.run_id+' | '+controls+': cancel | no automatic deployment',flush=True)
        controller=MissionController if run.state().get('orchestration_version')=='pitchi-profiles-v1' else Driver
        result=controller(run).execute();print(json.dumps({'run_id':run.run_id,'status':result['status'],
              'workspace':result['workspace'],'reason':result.get('stop_reason'),'completion':result.get('completion'),
              'human_preview':result.get('human_preview'),'capability_advice':result.get('capability_advice')},ensure_ascii=False),flush=True)
        return 0 if result['status']=='COMPLETED' else 2
    finally:
        done.set()
        if original is not None:thread.join(timeout=.5)
        if original is not None:termios.tcsetattr(sys.stdin,termios.TCSADRAIN,original)
        for sig,previous in old_signals.items():signal.signal(sig,previous)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    offset=2 if len(argv)>1 and argv[0]=='--home' else 0
    if len(argv)>offset and argv[offset] not in ('start','status','cancel','resume','recover','continue','result','advice','tools','tool','artifacts','search-ui','web-preview','cloud','-h','--help'):
        argv.insert(offset,'start')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,default=DEFAULT_HOME)
    commands=parser.add_subparsers(dest='command',required=True)
    start=commands.add_parser('start',help='new natural-language mission in an isolated working copy')
    sources=start.add_mutually_exclusive_group(required=True)
    sources.add_argument('--workspace',type=Path)
    sources.add_argument('--artifact',help='registered static Artifact ID as an immutable mission input')
    start.add_argument('--operation',choices=('repair','modify','clone'),help='required with --artifact')
    start.add_argument('--mission-from',type=Path,help='optional: explicit approved book_qa mission only')
    requests=start.add_mutually_exclusive_group()
    requests.add_argument('--request',help='natural-language request (quote it in the shell)')
    requests.add_argument('--request-file',type=Path,help='UTF-8 request, preserved without rewriting')
    start.add_argument('--instruction',default='')
    start.add_argument('--edit-steps',type=Path,help='explicit Host-authored guided edit plan; local v2 only')
    start.add_argument('--endpoint',default=os.getenv('PITCHI_ENDPOINT','http://127.0.0.1:8000/api/models/strata/v1'))
    start.add_argument('--model',default=os.getenv('PITCHI_MODEL','qwen3.8-flash-next-iq2_xs'))
    start.add_argument('--provider',choices=('local','openai','anthropic'),default='local')
    start.add_argument('--allow-external',action='store_true',help='opt in; EACH exact cloud payload still requires terminal review and SEND')
    start.add_argument('--cloud-config',type=Path,help='nonsecret JSON: model, finite limits and optional USD/Mtoken rates')
    start.add_argument('--cloud-advisor',choices=('openai','anthropic'),help='local Worker only; opt in to stagnation advice with exact payload approval')
    start.add_argument('--advisor-model');start.add_argument('--advisor-config',type=Path)
    start.add_argument('--no-resolution-memory',action='store_true',help='disable private solution-case lookup (clean evaluation)')
    start.add_argument('--worker-thinking',action='store_true',help='opt in to bounded hypothesis/edit reasoning; backend must enable a finite reasoning budget')
    start.add_argument('--research-handoff-version',type=int,choices=(1,2),default=STABLE_WORKFLOW_VERSION,help='stable v2 for new missions; 1 is old-generation replay compatibility only')
    start.add_argument('--procedure-ref',action='append',default=None,help='override automatic reviewed knowledge selection; repeat at most twice')
    start.add_argument('--search-database',type=Path,help='approved real SQLite snapshot for bounded PHP search-quality profile')
    start.add_argument('--php-runtime',type=Path,help='private extracted PHP CLI/PDO/mbstring runtime; no system installation')
    start.add_argument('--search-query',help='explicit exact request query; default only for the documented initial search-quality case')
    start.add_argument('--search-gold',type=Path,help='frozen bibliography/link gold for coverage acceptance; protected tests remain mandatory')
    commands.add_parser('tools',help='list registered Host tools (no LLM needed)')
    cloud=commands.add_parser('cloud',help='official SDK diagnostics; test previews offline unless explicitly approved')
    cloud.add_argument('action',choices=('models','test'))
    cloud.add_argument('--provider',required=True,choices=('openai','anthropic'))
    cloud.add_argument('--model');cloud.add_argument('--config',type=Path)
    cloud.add_argument('--allow-external',action='store_true')
    cloud.add_argument('--approve-sha',help='exact offline test preview SHA256; required for live test')
    advisory=commands.add_parser('advice',help='read saved Capability Advisor result; no new LLM call or execution')
    advisory.add_argument('run',help='run ID with saved capability advice')
    tool=commands.add_parser('tool',help='execute a read-only Host tool; JSON output and private artifact')
    tool.add_argument('name',nargs='?',choices=tuple(catalog()))
    tool.add_argument('--path',type=Path)
    tool.add_argument('--artifact-id')
    tool.add_argument('--kind',choices=('static_web_run','static_html','screenshot_search'))
    tool.add_argument('--run')
    tool.add_argument('--scope',choices=('auto','localhost','tailscale'),default='auto')
    tool.add_argument('--request',help='explicit OCR, index, lexical search or visible-activity search request')
    tool.add_argument('--root',type=Path,help='source folder for screenshot tools')
    tool.add_argument('--database',type=Path,help='private local index outside source folder')
    tool.add_argument('--before',type=Path,help='saved collector JSON before a change (search.pipeline_compare)')
    tool.add_argument('--after',type=Path,help='saved collector JSON after a change (search.pipeline_compare)')
    tool.add_argument('--control',action='append',default=[],help='observed control query, repeatable')
    tool.add_argument('--query',default='')
    tool.add_argument('--app')
    tool.add_argument('--date-from')
    tool.add_argument('--date-to')
    tool.add_argument('--limit',type=int,help='lexical default 30; semantic default 5')
    tool.add_argument('--workers',type=int,default=4)
    tool.add_argument('--retry-errors',action='store_true')
    tool.add_argument('--intent',help='requested activity for assisted search (default route) or explicit experimental Vision')
    tool.add_argument('--candidate-limit',type=int,default=30,help='semantic shortlist cap, at most 30 images')
    tool.add_argument('--vision-endpoint',help='local loopback /v1 endpoint only')
    tool.add_argument('--vision-model',help='already loaded multimodal model; no automatic service changes')
    tool.add_argument('--behavior',choices=('interactive_smoke','arithmetic_practice','arithmetic_practice_review'),default='interactive_smoke',help='registered Host web.verify behavior')
    tool.add_argument('--languages',choices=('jpn+eng','jpn','eng'),default='jpn+eng')
    tool.add_argument('--psm',type=int,choices=(3,6,11,12),default=11)
    tool.add_argument('--timeout',type=float,default=60)
    artifacts=commands.add_parser('artifacts',help='Host-managed previews: stable artifact URLs, automatic internal ports')
    artifacts.add_argument('action',choices=('register','list','inspect','open','status','stop','shutdown'))
    artifacts.add_argument('artifact_id',nargs='?')
    artifacts.add_argument('--kind',choices=('static_web_run','static_html','screenshot_search'))
    artifacts.add_argument('--root',type=Path);artifacts.add_argument('--database',type=Path)
    artifacts.add_argument('--run');artifacts.add_argument('--entry',default='index.html');artifacts.add_argument('--title')
    artifacts.add_argument('--scope',choices=('auto','localhost','tailscale'),default='auto')
    ui=commands.add_parser('search-ui',help='open registered Screenshot Search through managed private preview gateway')
    ui.add_argument('--root',type=Path,required=True)
    ui.add_argument('--database',type=Path)
    ui.add_argument('--port',type=int,default=0,help='deprecated, ignored: Host manages internal ports safely')
    ui.add_argument('--scope',choices=('auto','localhost','tailscale'),default='auto')
    preview=commands.add_parser('web-preview',help='open a Host-completed app through the managed private gateway')
    preview.add_argument('run');preview.add_argument('--port',type=int,default=18793)
    preview.add_argument('--scope',choices=('auto','localhost','tailscale'),default='auto')
    for name in ('status','cancel','resume','recover','continue','result'):
        p=commands.add_parser(name);p.add_argument('run',nargs='?' if name=='status' else None,help='Pitchi run ID; status without ID lists runs')
        if name in ('resume','continue'):p.add_argument('--allow-external',action='store_true',help='explicit opt-in again for a cloud run; per-payload approval still required')
        if name in ('resume','continue'):
            p.add_argument('--cloud-advisor',choices=('openai','anthropic'))
            p.add_argument('--advisor-model');p.add_argument('--advisor-config',type=Path)
            p.add_argument('--advisor-extra-call',action='store_true',help='explicit second mission consultation grant; still requires payload SHA/SEND')
        if name=='continue':p.add_argument('--instruction',help='additional instructions; omit for stdin input')
        if name=='recover':p.add_argument('--repair-calls',type=int,help='explicit new local call grant (1..24), reuse confirmed phases in an unused task attempt; never reset failed retry')
    args=parser.parse_args(argv)
    try:
        if args.command=='cloud':
            from .cloud_cli import execute
            print(json.dumps(execute(args),ensure_ascii=False,indent=2));return 0
        if args.command=='tools':print(json.dumps(catalog(),ensure_ascii=False,indent=2));return 0
        if args.command=='artifacts':
            request={'scope':args.scope}
            if args.action=='register':
                if not args.kind:raise ValueError('--kind is required')
                request.update(kind=args.kind,entry=args.entry)
                for key in ('root','database','run','title'):
                    value=getattr(args,key)
                    if value is not None:request[key]=str(value)
            elif args.artifact_id:request['artifact_id']=args.artifact_id
            return foreground_tool(args.home,'artifact.'+('preview' if args.action=='open' else args.action),request)
        if args.command=='search-ui':
            from .artifacts import register
            request={'kind':'screenshot_search','root':str(args.root),'scope':args.scope,'title':'Screenshot Search'}
            if args.database:request['database']=str(args.database)
            identifier=register(args.home,request)
            return foreground_tool(args.home,'artifact.preview',{'artifact_id':identifier,'scope':args.scope})
        if args.command=='tool':
            if args.request is not None:
                if args.name or args.path:raise ValueError('Use --request alone, or tool name with --path')
                invocation=resolve_tool_request(args.request,args.root)
                if invocation is None:raise ValueError('Unsupported/ambiguous tool request; use a registered tool with explicit path/root')
                name=invocation['name'];request=invocation['input']
            else:
                name=args.name
                if name and name.startswith('artifact.'):
                    request={'scope':args.scope}
                    if name=='artifact.register':
                        request['kind']=args.kind
                        for key in ('root','database','run'):
                            if getattr(args,key) is not None:request[key]=str(getattr(args,key))
                    elif args.artifact_id:request['artifact_id']=args.artifact_id
                elif name=='image.ocr':
                    if args.path is None:raise ValueError('tool image.ocr requires --path')
                    request={'path':str(args.path)}
                elif name=='search.pipeline_compare':
                    if args.before is None or args.after is None:raise ValueError('search.pipeline_compare requires --before and --after')
                    request={'before':str(args.before),'after':str(args.after),'controls':args.control}
                elif name in ('screenshot.index','screenshot.search','screenshot.semantic_search','screenshot.assisted_search','web.verify'):
                    if args.root is None:raise ValueError('Screenshot tools require --root')
                    if args.path is not None:raise ValueError('Screenshot tools use --root, not --path')
                    request={'root':str(args.root)}
                else:raise ValueError('Tool name is required')
            if name=='image.ocr':request.update(languages=args.languages,psm=args.psm,timeout_seconds=args.timeout)
            if name=='web.verify':request.update(behavior=args.behavior)
            if name=='screenshot.index':request.update(languages=args.languages,psm=args.psm,workers=args.workers,retry_errors=args.retry_errors)
            if name=='screenshot.search':
                if args.request is None:request['query']=args.query
                request.update(limit=30 if args.limit is None else args.limit)
                for key,value in (('app',args.app),('date_from',args.date_from),('date_to',args.date_to)):
                    if value is not None:request[key]=value
            if name in ('screenshot.semantic_search','screenshot.assisted_search'):
                if args.request is None:request.update(query=args.query,intent=args.intent,limit=5 if args.limit is None else args.limit)
                elif args.limit is not None:request['limit']=args.limit
                request.update(candidate_limit=args.candidate_limit)
                if name=='screenshot.semantic_search':request['timeout_seconds']=args.timeout
                for key,value in (('app',args.app),('date_from',args.date_from),('date_to',args.date_to),
                                  ('endpoint',args.vision_endpoint),('model',args.vision_model)):
                    if value is not None and (name=='screenshot.semantic_search' or key not in ('endpoint','model')):request[key]=value
            if name.startswith('screenshot.') and args.database is not None:request['database']=str(args.database)
            return foreground_tool(args.home,name,request)
        if args.command=='start':
            if len(args.procedure_ref or [])>2 or len(set(args.procedure_ref or []))!=len(args.procedure_ref or []):raise ValueError('At most two unique --procedure-ref values')
            if args.mission_from and (args.procedure_ref or '--research-handoff-version' in argv):raise ValueError('Workflow options require a fresh profile mission, not legacy replay')
            from .cloud import configuration
            explicit_model=args.model if any(a=='--model' or a.startswith('--model=') for a in argv) else None
            cloud_config=configuration(args.provider,explicit_model,args.cloud_config,args.allow_external)
            if args.cloud_advisor and args.provider!='local':raise ValueError('Cloud Advisor requires a local Worker')
            if args.cloud_advisor and args.mission_from:raise ValueError('Cloud Advisor is supported on new profile phase workflows, not legacy replay missions')
            if not args.cloud_advisor and (args.advisor_model or args.advisor_config):raise ValueError('Advisor settings require --cloud-advisor')
            advisor_config=configuration(args.cloud_advisor,args.advisor_model,args.advisor_config,args.allow_external) if args.cloud_advisor else None
            if args.provider!='local':
                # --model names the cloud model; do not silently reconfigure
                # existing local-only validators/tools or their runtime endpoint.
                args.model=os.getenv('PITCHI_MODEL','qwen3.8-flash-next-iq2_xs')
            def bind_model(run):
                if args.edit_steps:
                    if args.provider!='local' or args.research_handoff_version!=2:raise ValueError('Guided edits require local stable v2')
                    from .phase_workflow import install_edit_steps
                    install_edit_steps(run,args.edit_steps)
                if args.provider!='local':run.save_state(llm_config=cloud_config)
                if run.state().get('orchestration_version')=='pitchi-profiles-v1':
                    from .mission_controller import configure_workflow
                    configure_workflow(run,args.research_handoff_version,args.procedure_ref)
                    run.save_state(resolution_policy={'memory':not args.no_resolution_memory,**({'cloud':advisor_config} if advisor_config else {})})
                if args.worker_thinking:run.save_state(worker_reasoning=True)
                return run
            if args.artifact:
                if not args.operation or args.mission_from or args.instruction:raise ValueError('--artifact requires --operation and a new request only')
                request=args.request if args.request is not None else args.request_file.read_text(encoding='utf-8') if args.request_file else read_request()
                if args.provider=='local' and not os.getenv('PITCHI_MODEL') and not any(a=='--model' or a.startswith('--model=') for a in argv):
                    from .model import discover_local_model
                    args.model=discover_local_model(args.endpoint)
                if args.provider=='local':health(args.endpoint,args.model)
                from .artifact_jobs import create_artifact_mission
                run=create_artifact_mission(args.home,args.artifact,args.operation,request,args.endpoint,args.model)
                return foreground(bind_model(run))
            if args.operation:raise ValueError('--operation requires --artifact')
            if args.mission_from and (args.request is not None or args.request_file):
                raise ValueError('--mission-from preserves the fixed book mission; use --instruction, not a new --request')
            request=None if args.mission_from else (args.request if args.request is not None else
                args.request_file.read_text(encoding='utf-8') if args.request_file else read_request())
            if request is not None and not request.strip():raise ValueError('Empty request; no run started')
            invocation=resolve_tool_request(request,args.workspace)
            if invocation is not None:
                if args.procedure_ref or '--research-handoff-version' in argv:raise ValueError('Workflow options require a mission, not a single tool call')
                if args.instruction:raise ValueError('Single tool request does not accept extra --instruction')
                return foreground_tool(args.home,invocation['name'],invocation['input'])
            if args.provider=='local' and args.search_database and not os.getenv('PITCHI_MODEL') and not any(a=='--model' or a.startswith('--model=') for a in argv):
                from .model import discover_local_model
                args.model=discover_local_model(args.endpoint)
            if args.provider=='local':health(args.endpoint,args.model)  # No cloud health request without content approval.
            if args.search_database or args.php_runtime:
                if not args.search_database or not args.php_runtime or args.mission_from or args.instruction:
                    raise ValueError('Search profile requires both --search-database and --php-runtime and a fresh request')
                from .search_quality import prepare
                run=prepare(args.home,args.workspace,args.search_database,args.php_runtime,request,args.endpoint,args.model,query=args.search_query,gold_path=args.search_gold)
                return foreground(bind_model(run))
            run=create_run(args.home,args.workspace,args.mission_from,args.endpoint,args.model,args.instruction) if args.mission_from else \
                create_mission(args.home,args.workspace,request,args.endpoint,args.model,instruction=args.instruction)
            return foreground(bind_model(run))
        if args.command=='status' and args.run is None:
            print(json.dumps([{'run_id':p.parent.name,**{k:load(p).get(k) for k in ('status','stage','profile','updated_at')}}
                              for p in sorted((args.home/'runs').glob('*/state.json'))],ensure_ascii=False,indent=2));return 0
        path=(Path(args.run) if Path(args.run).is_absolute() else args.home/'runs'/args.run).resolve()
        if path.parent!=(args.home/'runs').resolve():raise ValueError('Only direct Pitchi-work run directories can be operated')
        run=RunStore(path)
        if not (path/'state.json').is_file():raise ValueError('Run not found')
        if args.command=='web-preview':
            from .artifacts import register
            identifier=register(args.home,{'kind':'static_web_run','run':run.run_id,'scope':args.scope})
            return foreground_tool(args.home,'artifact.preview',{'artifact_id':identifier,'scope':args.scope})
        if args.command=='status':print(json.dumps(run.state(),ensure_ascii=False,indent=2));return 0
        if args.command=='advice':
            report=load(path/'artifacts/capability_advice.json',None)
            if report is None:raise ValueError('No saved capability advice for this run; existing terminal runs are not rewritten')
            print(json.dumps(report,ensure_ascii=False,indent=2));return 0
        if args.command=='result':
            state=run.state()
            print(json.dumps({'run_id':run.run_id,'status':state['status'],'workspace':state['workspace'],
                'original_request':load(path/'root_goal.json',{}).get('request'),'completion':load(path/'artifacts/request_completion_gate.json',None),
                'assessment':load(path/'artifacts/integrator_assessment.json',None),'reason':state.get('stop_reason'),
                'human_preview':state.get('human_preview'),
                'resolution_summary':state.get('resolution_summary'),
                'capability_advice':load(path/'artifacts/capability_advice.json',state.get('capability_advice')),
                'task_reports':[{ 'task_id':r['task']['task_id'],'status':r['status'],'attempts':r['attempts']}
                    for p in sorted((path/'tasks').glob('*.json')) for r in [load(p)]],
                'evidence_directory':str(path/'artifacts'),'deployed':False},ensure_ascii=False,indent=2));return 0
        if args.command=='cancel':
            if run.state()['status'] in run.TERMINAL:print('Already terminal; no processing to cancel');return 0
            run.request_cancel();print('Cancel requested; controller stops only registered children');return 0
        if args.command=='recover':
            from .recovery import recover_mission, assert_recoverable_journal
            state=run.state()
            if state.get('llm_config',{}).get('provider','local')!='local':
                raise ValueError('Recovery currently requires a local Worker; no cloud permission inherited')
            if args.repair_calls is None:assert_recoverable_journal(run)
            health(state['local_model_endpoint'],state['local_model'])
            child=recover_mission(run,repair_calls=args.repair_calls)
            print('Recovery child: '+child.run_id+' | parent: '+run.run_id,flush=True)
            return foreground(child)
        state=run.state()
        cloud_config=state.get('llm_config',{'provider':'local'})
        if cloud_config['provider']=='local':health(state['local_model_endpoint'],state['local_model'])
        elif not args.allow_external:
            from .cloud import CloudControlStop
            raise CloudControlStop('EXTERNAL_API_PERMISSION_REQUIRED','Cloud resume/continue requires --allow-external again')
        if not args.cloud_advisor and (args.advisor_extra_call or args.advisor_model or args.advisor_config):
            raise ValueError('Advisor grant/settings require --cloud-advisor')
        if args.command=='continue':
            if state['status'] not in run.TERMINAL:raise ValueError('Cancel/finish the parent before creating a child')
            instruction=args.instruction if args.instruction is not None else read_request()
            if not instruction.strip():raise ValueError('Additional instruction is empty')
            if state.get('orchestration_version')=='pitchi-profiles-v1':
                child=create_mission(args.home,state['workspace'],load(path/'root_goal.json')['request'],
                    state['local_model_endpoint'],state['local_model'],parent=path,instruction=instruction)
                if cloud_config['provider']!='local':child.save_state(llm_config=cloud_config)
                if args.cloud_advisor:
                    if cloud_config['provider']!='local':raise ValueError('Cloud Advisor requires a local Worker')
                    child.save_state(resolution_policy={**child.state()['resolution_policy'],
                        'cloud':configuration(args.cloud_advisor,args.advisor_model,args.advisor_config,args.allow_external),
                        'extra_call':args.advisor_extra_call})
                if (path/'artifact_input.json').exists():
                    from .store import dump
                    context=load(path/'artifact_input.json');context['resume_from_run']=run.run_id
                    dump(child.path/'artifact_input.json',context)
                    child.save_state(input_artifact_id=context['artifact_id'],artifact_operation=context['operation'])
                return foreground(child)
            child=create_run(args.home,state['workspace'],path,state['local_model_endpoint'],state['local_model'],instruction,parent=path)
            if cloud_config['provider']!='local':child.save_state(llm_config=cloud_config)
            dump_parent=load(path/'artifacts/integrator_assessment.json',{})
            from .store import dump
            dump(child.path/'parent_evidence.json',{'parent_run':str(path),'terminal_state':state['status'],
                 'terminal_reason':state.get('stop_reason'),'task_records':[(str(p.relative_to(path)),load(p)) for p in sorted((path/'tasks').glob('*.json'))],
                 'reviewer_refs':[str(p.relative_to(path)) for p in sorted((path/'reviews').glob('*.json'))],
                 'integrator_assessment':dump_parent})
            return foreground(child)
        if (path/'cancel_request.json').exists():raise ValueError('Cancelled run remains immutable; use continue for a child')
        if state['status'] in run.TERMINAL:raise ValueError('Terminal run remains immutable; use continue for a child')
        if args.cloud_advisor:
            if cloud_config['provider']!='local':raise ValueError('Cloud Advisor requires a local Worker')
            run.save_state(resolution_policy={**state.get('resolution_policy',{'memory':True}),
                'cloud':configuration(args.cloud_advisor,args.advisor_model,args.advisor_config,args.allow_external),
                'extra_call':args.advisor_extra_call})
        elif state.get('resolution_policy',{}).get('cloud') and not args.allow_external:
            from .cloud import CloudControlStop
            raise CloudControlStop('EXTERNAL_API_PERMISSION_REQUIRED','Cloud Advisor resume requires --allow-external again')
        return foreground(run)
    except Exception as exc:
        print(getattr(exc,'failure_class',type(exc).__name__)+': '+str(exc),file=sys.stderr);return 2
    except KeyboardInterrupt:
        print('Input cancelled; no new run started',file=sys.stderr);return 130


def read_request():
    """Readline editing for TTY; piped/file input is preserved byte-for-byte."""
    if not sys.stdin.isatty():return sys.stdin.read()
    # GNU readline avoids the kernel canonical-mode per-line paste limit and
    # supplies ordinary terminal editing/IME handling without a form widget.
    try:import readline
    except ImportError:raise ValueError('TTY editing unavailable; use --request-file or piped UTF-8 input')
    # Switch before advertising readiness: an immediate long paste must not
    # hit the kernel's canonical 4096-byte line buffer before readline starts.
    terminal = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin, termios.TCSANOW)
    try:
        print('PITCHI WORK\n依頼を入力してください。複数行可。単独の /send 行、または Ctrl+D で送信。',flush=True)
        lines=[]
        while True:
            try:line=input('> ')
            except EOFError:break
            if line=='/send':break
            lines.append(line+'\n')
        return ''.join(lines)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, terminal)


if __name__=='__main__':raise SystemExit(main())
