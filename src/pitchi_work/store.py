from __future__ import annotations
import json,os,tempfile,signal,time
from datetime import datetime,timezone
from pathlib import Path
from typing import Any

def now():return datetime.now(timezone.utc).isoformat()

class ReadOnlyRun:
    """Saved-run source, deliberately unable to update legacy state/evidence."""
    def __init__(self,path):
        self.path=Path(path).resolve();self.run_id=self.path.name
    def state(self):return load(self.path/'state.json',{})

class Cancelled(RuntimeError):pass

class RunStore(ReadOnlyRun):
    TERMINAL={'COMPLETED','PARTIAL','STALLED','FAILED','STOPPED_BY_USER','CANCELLED','UNSUPPORTED_MISSION','NEEDS_HUMAN','NEEDS_CAPABILITY'}
    def save_state(self,**changes):
        state=self.state();previous=state.get('status')
        if previous in self.TERMINAL:
            if changes.get('status',previous)!=previous:raise ValueError('Terminal run is immutable; use a child run')
            if any(k in changes for k in ('stop_reason','failure_class')):raise ValueError('Terminal reason is immutable')
        elif (self.path/'cancel_request.json').exists() and changes.get('status') not in {'STOPPED_BY_USER','CANCELLED'}:
            changes['status']='CANCELLING'
        if 'workspace' in changes and state.get('workspace') and changes['workspace']!=state['workspace']:
            raise ValueError('Active workspace is immutable')
        state.update(changes);state['updated_at']=now()
        if state.get('status') in self.TERMINAL:
            state.setdefault('terminal_record',{'status':state['status'],'failure_class':state.get('failure_class'),'reason':state.get('stop_reason',''),'at':state['updated_at']})
        dump(self.path/'state.json',state)
    def event(self,role,kind,**data):
        path=self.path/'logs/events.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps({'at':now(),'role':role,'kind':kind,**data},ensure_ascii=False)+'\n')
    def trace(self,role,status,message,**data):self.event(role,'activity',status=status,message=message,**data)
    def request_cancel(self):
        if self.state().get('status') in self.TERMINAL:return
        dump(self.path/'cancel_request.json',{'at':now()});self.save_state(status='CANCELLING')
        # READY/orphaned runs have no controller to acknowledge the token.
        # Use the same Host lock, never infer idleness merely from a UI flag.
        import fcntl
        with (self.path/'controller.lock').open('a+') as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            terminate_run_processes(self)
    def raise_if_cancelled(self):
        if (self.path/'cancel_request.json').exists():
            # Terminal cancellation is recorded only AFTER registered children stop.
            raise Cancelled('Host cancellation requested')
    def register_process(self,pid,kind):
        self.raise_if_cancelled();active=self.state().get('active_processes',{})
        active[str(pid)]={'pid':pid,'pgid':os.getpgid(pid),'kind':kind,'start_time':_process_start_time(pid)}
        self.save_state(active_processes=active)
    def unregister_process(self,pid):
        active=self.state().get('active_processes',{});active.pop(str(pid),None);self.save_state(active_processes=active)
    def handoff(self,from_role,to_role,source_artifacts,**data):
        path=self.path/'handoffs';path.mkdir(parents=True,exist_ok=True)
        dump(path/f'{len(list(path.glob("*.json")))+1:03d}_{from_role}_to_{to_role}.json',{'from_role':from_role,'to_role':to_role,'source_artifacts':source_artifacts,'timestamp':now(),**data})


def _process_start_time(pid: int) -> str | None:
    """Linux process start tick, used to guard against PID reuse."""
    try:
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1]
        return tail.split()[19]  # starttime is field 22; tail begins at field 3
    except (OSError, IndexError):
        return None

def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def load(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def terminate_run_processes(run: RunStore, grace_seconds: float = 1.5) -> list[str]:
    """Terminate only identity-checked process groups registered to this run."""
    if run.state().get('status') in RunStore.TERMINAL:return []
    stopped: list[str] = []; groups: set[int] = set()
    for entry in run.state().get("active_processes", {}).values():
        pid, pgid = entry.get("pid"), entry.get("pgid")
        if not isinstance(pid, int) or not isinstance(pgid, int): continue
        # Never signal the caller's own group (important for in-process tests
        # and defensive if a controller was accidentally registered).
        if pid == os.getpid(): continue
        expected = entry.get("start_time")
        if expected is not None and _process_start_time(pid) != expected: continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            groups.add(pgid); stopped.append(f"{entry.get('kind', 'process')} (pid {pid})")
        except ProcessLookupError:
            continue
        except PermissionError:
            run.event("controller", "cancel_process_denied", pid=pid, pgid=pgid)
    deadline = time.monotonic() + grace_seconds
    while groups and time.monotonic() < deadline:
        alive: set[int] = set()
        for pgid in groups:
            try: os.killpg(pgid, 0); alive.add(pgid)
            except ProcessLookupError: pass
            except PermissionError: alive.add(pgid)
        groups = alive
        if groups: time.sleep(0.05)
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
            run.event("controller", "cancel_process_killed", pgid=pgid)
        except (ProcessLookupError, PermissionError): pass
    run.save_state(status="STOPPED_BY_USER", stopped_at=now(), active_processes={})
    run.event("controller", "cancel_completed", stopped=stopped)
    run.trace("controller", "OK", "Project cancelled safely", detail="、".join(stopped) or "待機・再試行を停止")
    return stopped
