"""Stopping runs: which process may be signalled, and what a stop records on the row."""
import os
import signal
import subprocess
import time

from . import runstate

AGENT_NAMES = {"claude": ("claude",), "agy": ("agy",)}
STALE_AFTER = 600   # seconds without hook events before a dead-PID session counts as gone
APP_HOSTS = ("language_server", "antigravity")
LIVE = runstate.LIVE


def proc_name(pid):
    """Basename of a live process's command (argv[0]), or '' when it is gone or unreadable."""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except PermissionError:
        pass
    except (ProcessLookupError, ValueError, TypeError, OSError):
        return ""
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return os.path.basename(out).lower()


def is_agent(pid, tool):
    name = runstate.launcher().proc_name(pid)
    return bool(name) and any(n == name or name.startswith(n + "-") for n in AGENT_NAMES.get(tool, (tool or "",)))


def stop_plan(a):
    """('stop', pid or None, '') | ('dismiss', None, why) | (None, None, why).

    'stop' signals a verified process (pid None: nothing left to signal). A waiting agent that can't
    be signalled can still be dismissed: the row moves to Finished and returns if the session reports again.
    """
    ok, pid, why = runstate.launcher().stop_status(a)
    if ok:
        return "stop", pid, ""
    if a and a.get("status") == "waiting":
        return "dismiss", None, why
    return None, None, why


def stop_status(a):
    """(stoppable, pid_to_signal, reason). Never offers a PID it hasn't checked is this agent."""
    L = runstate.launcher()
    if not a or a.get("status") not in LIVE:
        return False, None, "Only running or waiting agents can be stopped."
    rid, tool = a.get("id") or "", a.get("tool") or ""
    if rid in runstate.PROCS:
        return True, None, ""
    if a.get("source") == "launcher":
        pid = a.get("pid")
        if pid and L.proc_name(pid):
            return True, int(pid), ""
        return False, None, "The launched process has already ended."
    if a.get("agent_host") == "antigravity-app":
        return False, None, "Runs inside the Antigravity app, which hosts every conversation in one process. Stop it in the app."
    for pid in (a.get("agent_pid"), a.get("pid")):
        if pid and L._is_agent(pid, tool):
            return True, int(pid), ""
    reported = [x for x in (a.get("agent_pid"), a.get("pid")) if x]
    if reported and not any(L.proc_name(x) for x in reported):
        quiet = time.time() - float(a.get("updated") or 0)
        if quiet > STALE_AFTER:
            return True, None, ""   # nothing left to signal; Stop just moves the stale row to Finished
        return False, None, "Its reported process has exited, but the session is still sending events. Stop it in its own app."
    if reported:
        return False, None, "The process it reported isn't this agent, so it won't be signalled."
    return False, None, "This session hasn't reported its process ID yet. It will after its next prompt or tool call."


def stop(run_id, store):
    """Stop a run. Returns True when the row is now stopped (kept for the old callers)."""
    return runstate.launcher().stop_with_reason(run_id, store)[0]


def stop_with_reason(run_id, store):
    """(ok, reason). Terminates a launched process or SIGTERMs a hook session's verified agent PID.

    Refuses, with the reason, when the agent is alive but can't be signalled on its own (inside the
    Antigravity app, or no PID reported yet), so the dashboard never claims a stop that didn't happen.
    A run whose process is already gone is simply recorded as stopped.
    """
    if not run_id:
        return False, "no agent id"
    row = store.get(run_id) if store else None
    proc = runstate.PROCS.get(run_id)

    def mark(activity="stopped by you"):
        # Written BEFORE the signal: the worker, the retry/fallback branches and the agent's own dying hooks
        # all read it, so a force-closed run is never taken for a failure and relaunched.
        if store:
            store.upsert(run_id, status="stopped", activity=activity, ended=time.time(), closed_by_user=1)

    if proc:
        mark()
        try:
            proc.terminate()
        except OSError:
            pass
        return True, ""
    if row and row.get("status") in LIVE:
        kind, pid, reason = runstate.launcher().stop_plan(row)
        if kind == "dismiss":
            mark("dismissed by you (process not signalled)")
            return True, ""
        if kind is None and row.get("source") != "launcher":
            return False, reason
        mark()
        if kind == "stop" and pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass
        return True, ""
    if row and row.get("status") in ("done", "failed", "stopped"):
        return True, "already finished"   # never relabel a finished run as stopped
    mark()
    return True, ""


def closed_by_user(store, run_id):
    """True once the owner stopped or closed this run: nothing may restart it on its own."""
    row = store.get(run_id) if store and run_id else None
    return bool(row and (row.get("closed_by_user") or row.get("status") == "stopped"))
