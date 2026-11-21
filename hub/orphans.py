"""Run processes after a restart: is this still the same process, and what a vanished run becomes.

Two places notice a launched run whose process is gone (the scheduler for queue tasks, `progress.sync_run_progress`
for every launched row). They share the one rule here: a run whose process vanished without the worker recording
an exit did not verifiably finish, so it is `failed` (never `done`), with an honest activity line. A queue task in
that state then goes through the scheduler's normal failure path (escalate, fall back or fail).

"Same process" is the pid plus its start time (`ps -o lstart=`), recorded at launch in `agents.pid_start`. A pid
alone is not enough: after a restart the number can belong to an unrelated process. Rows written before the start
time was recorded have none and are judged by the pid alone, as before.
"""
import calendar
import os
import subprocess
import threading
import time

LOST_NOTE = "lost in a restart: its process ended without reporting an exit"


START_TTL_S = 30.0
_starts = {}   # pid -> (asked at, lstart): a process's start time never changes, so the 2 s poll need not ask again
_starts_lock = threading.Lock()
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def start_epoch(text):
    """`ps -o lstart=` text ("Mon Nov 12 10:00:00 2026", C locale) as seconds, or None. The zone is not in the text,
    so the value is only good for differences between two such strings."""
    try:
        _, mon, day, clock, year = str(text).split()
        h, m, sec = (int(x) for x in clock.split(":"))
        return calendar.timegm((int(year), _MONTHS[mon], int(day), h, m, sec))
    except (ValueError, KeyError):
        return None


def same_start(a, b):
    """The same moment, or a whole 1 or 2 hours apart (the zone or daylight saving moved between the two reads)."""
    a, b = " ".join(str(a).split()), " ".join(str(b).split())
    if a == b:
        return True
    ea, eb = start_epoch(a), start_epoch(b)
    if ea is None or eb is None:
        return False
    d = abs(ea - eb)
    return d % 3600 == 0 and d <= 7200


def proc_start(pid, fresh=False):
    """The start time of a live process as `ps` prints it (1 s resolution), or '' when unknown. Cached per pid for 30 s."""
    now = time.time()
    with _starts_lock:
        hit = _starts.get(pid)
    if hit and not fresh and now - hit[0] < START_TTL_S:
        return hit[1]
    val = _ps_start(pid)
    if val:
        with _starts_lock:
            if len(_starts) > 500:
                _starts.clear()
            _starts[pid] = (now, val)
    return val


def _ps_start(pid):
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=2, env=dict(os.environ, LC_ALL="C")).stdout
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return ""
    return " ".join(out.split())


def alive(pid, started=None):
    """True while `pid` is a live process and, when its recorded start time is known, still the same one."""
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        pass   # exists, owned by someone else
    except (TypeError, ValueError, ProcessLookupError, OSError):
        return False
    if started:
        now = proc_start(pid)
        if now and not same_start(now, started):
            return False   # the pid was recycled by a different process
    return True


def run_alive(run, now=None, nopid_grace=0.0):
    """Is the process of a launched run row still running? A row without a pid is judged by its age:
    younger than `nopid_grace` seconds counts as alive (still starting), otherwise gone."""
    pid = run.get("pid")
    if pid:
        return alive(pid, run.get("pid_start"))
    now = time.time() if now is None else now
    age = now - float(run.get("updated") or run.get("started") or now)
    return age < nopid_grace


def settle_lost(store, run, now=None):
    """Record the one outcome of a vanished run: failed, ended at its last sign of life. Releases its claims.
    Updates `run` in place and returns it."""
    now = time.time() if now is None else now
    ended = float(run.get("updated") or now)
    store.upsert(run["id"], status="failed", activity=LOST_NOTE, ended=ended)
    run["status"], run["activity"], run["ended"] = "failed", LOST_NOTE, ended
    try:
        from . import coord
        coord.release(store, run["id"], "failed", summary="lost in a restart")
    except Exception as e:   # never let a claim cleanup stop the run from being settled
        print(f"[orphans] {e!r}")
    return run
