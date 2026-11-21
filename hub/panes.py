"""Runs shown in an iTerm2 pane: the run spec the pane fetches once, and the events it reports.

Flow: launcher registers a spec → terminal opens a pane running hooks/pane_runner.py <run_id> →
the runner GETs /api/run-spec (claim, once) → POSTs start / exit / close to /api/run-event.
The prompt and environment travel over this local HTTP call, never through AppleScript.
"""
import os
import re
import signal
import threading
from pathlib import Path

RUN_RE = re.compile(r"^[a-z0-9-]{1,64}$")
RUNNER = Path(__file__).resolve().parent.parent / "hooks" / "pane_runner.py"
CLOSE_RULES = {"never": None, "after_30s": 30, "immediately": 0}
START_TIMEOUT = 20.0    # seconds for the pane to claim its spec before the run falls back
CLAIMED_TIMEOUT = 60.0  # seconds from claim to "start" before the run counts as failed

_runs = {}
_lock = threading.Lock()


class PaneRun:
    def __init__(self, spec):
        self.spec = spec
        self.claimed = self.abandoned = False
        self.pid = None
        self.code = None
        self.stopped = False
        self.claimed_evt = threading.Event()
        self.started = threading.Event()
        self.exited = threading.Event()


def valid_pid(pid):
    """A pid that names one other process: os.kill(0 / -1 / -n) signals a whole group or every process we own."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 1


class PaneProc:
    """Stands in for a Popen in launcher._procs, so Stop signals the pane runner (which stops the agent)."""

    def __init__(self, pid):
        self.pid = int(pid)

    def terminate(self):
        if not valid_pid(self.pid):
            return
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def poll(self):
        if not valid_pid(self.pid):
            return 0   # nothing we may signal: count it as gone
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            return 0
        except PermissionError:
            return None


def close_rule(cfg):
    rule = str((cfg.get("terminal") or {}).get("close_on_finish", "never"))   # owner: close finished panes yourself
    return rule if rule in CLOSE_RULES else "never"


def register(run_id, spec):
    with _lock:
        r = _runs[run_id] = PaneRun(spec)
    return r


def get(run_id):
    with _lock:
        return _runs.get(run_id)


def claim(run_id):
    """The spec for the pane runner, once. None if unknown, already claimed, or moved to the background."""
    with _lock:
        r = _runs.get(run_id)
        if not r or r.claimed or r.abandoned:
            return None
        r.claimed = True
        spec, r.spec = r.spec, None
    r.claimed_evt.set()
    return spec


def abandon(run_id):
    """Give up on the pane (it never claimed): True if the run may now start in the background."""
    with _lock:
        r = _runs.get(run_id)
        if not r or r.claimed:
            return False
        r.abandoned = True
        r.spec = None
        return True


def event(run_id, body):
    """Apply a runner event. Returns (http_code, payload)."""
    if not RUN_RE.match(str(run_id or "")):
        return 400, {"error": "bad run id"}
    r = get(run_id)
    if not r:
        return 404, {"error": "no pane run with that id"}
    kind = body.get("event")
    if kind == "start":
        try:
            pid = int(body.get("pid"))
        except (TypeError, ValueError):
            return 400, {"error": "start needs a pid"}
        if not valid_pid(pid):   # 0 / -1 would make Stop signal our own group or every process of the user
            return 400, {"error": "start needs the runner's own pid"}
        r.pid = pid
        r.started.set()
    elif kind == "exit":
        try:
            r.code = int(body.get("code"))
        except (TypeError, ValueError):
            r.code = None
        r.stopped = bool(body.get("stopped"))
        r.exited.set()
    elif kind != "close":
        return 400, {"error": f"unknown event {kind!r}"}
    return 200, {"ok": True}


def drop(run_id):
    with _lock:
        _runs.pop(run_id, None)
