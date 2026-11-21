"""/orchestrate <task>: Opus leads a team of agents in one 4×2 tab of the Jarviee iTerm2 window.

The lead is a live Claude Code session (always Opus) in window 1 of a NEW grid tab; the other seven windows are free.
It runs `hub.py orch assign / wait / result / review / status` (hub/orch_cli.py -> /api/orch/*, hub/orch_api.py) to
split the task into jobs, give each worker a load (basic / light / complex / heavy, which the dispatch table turns into
a tool and model; the lead may name a tool or model itself), check every result when a worker finishes, and hand the
freed window the next job. Workers are ordinary launcher runs, so the existing machinery keeps working for them:
quota-aware routing at launch, and a fallback agy <-> claude -> OmniRoute when a tool runs out mid-run
(runjob: `p["orchestrate"]` keeps the fallback on for these runs, which otherwise only non-queue runs get).

State is persisted under `brand.home() / "orch" / "<lead_id>.json"` and lazily restored on demand (the runs
themselves also stay in `hub.db` on the dashboard).
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import brand, finaltext, launcher, outcome, terminal

HUB = Path(__file__).resolve().parent.parent / "hub.py"
LOADS = ("basic", "light", "complex", "heavy")
VERDICTS = ("accept", "rework")
FINISHED_STATUSES = ("done", "failed", "stopped")
WAIT_MAX = 540.0   # a lead's shell tool times out at 10 minutes: `orch wait` answers before that, the lead calls it again
REPORT_CHARS = 1800
PRUNE_DAYS = 7
POLL_INTERVAL = 0.5
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

_orchs = {}
_lock = threading.Lock()


class Orch:
    def __init__(self, lead_id, task, project, cwd):
        self.id, self.task, self.project = lead_id, task, project or ""
        self.cwd = str(cwd) if cwd else None
        self.workers = {}
        self.next_n = 1
        self.created = time.time()
        self.cond = threading.Condition()
        self._orphaned = set()
        self._needs_prune = False


def capacity(cfg):
    """Workers that can run at once: every window of the grid but the lead's."""
    cols, rows = terminal.grid_dims(terminal.settings(cfg))
    return cols * rows - 1


def _orch_dir():
    return brand.home() / "orch"


def _orch_path(lead_id):
    if not isinstance(lead_id, str) or not _ID_RE.match(lead_id):
        return None
    return _orch_dir() / f"{lead_id}.json"


def _serialize(o):
    return {
        "version": 1,
        "id": o.id,
        "task": o.task,
        "project": o.project,
        "cwd": o.cwd,
        "next_n": o.next_n,
        "created": o.created,
        "workers": {str(n): dict(w) for n, w in sorted(o.workers.items())},
    }


def _save_locked(o):
    """Atomically write orchestration state to `<home>/orch/<lead_id>.json` (0600). Caller holds `o.cond`."""
    path = _orch_path(o.id)
    if path is None:
        return
    raw = json.dumps(_serialize(o), ensure_ascii=False, indent=1)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(prefix=f".{o.id}.", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        f = None
        try:
            os.fchmod(fd, 0o600)
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(raw)
            f.flush()
        os.replace(tmp, path)
    except OSError as e:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"[orch] could not save {path}: {e}", file=sys.stderr)


def _int_val(val, field):
    if isinstance(val, bool):
        raise ValueError(f"invalid {field}")
    return int(val)


def _str_val(val, default=""):
    return default if val is None else str(val)


def _num_or_none(val):
    return float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else None


def _deserialize(data, expected_id):
    if not isinstance(data, dict):
        raise ValueError("orchestration state must be a JSON object")
    if not isinstance(expected_id, str) or not expected_id or data.get("id") != expected_id:
        raise ValueError("mismatched orchestration id")
    task = data.get("task")
    if not isinstance(task, str):
        raise ValueError("invalid task")
    project = data.get("project", "")
    if project is not None and not isinstance(project, str):
        raise ValueError("invalid project")
    cwd = data.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("invalid cwd")
    raw_workers = data.get("workers")
    if not isinstance(raw_workers, dict):
        raise ValueError("invalid workers")

    workers = {}
    for k, raw in raw_workers.items():
        if not isinstance(raw, dict):
            raise ValueError("invalid worker entry")
        n = _int_val(raw.get("n", k), "worker number")
        if n < 1:
            raise ValueError("invalid worker number")
        w_task = raw.get("task", "")
        if not isinstance(w_task, str):
            raise ValueError("invalid worker task")
        status = raw.get("status", "failed")
        if not isinstance(status, str) or not status:
            raise ValueError("invalid worker status")
        raw_files = raw.get("files")
        if raw_files is not None and not isinstance(raw_files, list):
            raise ValueError("invalid worker files")
        redo_raw = raw.get("redo_of")
        redo_of = _int_val(0 if redo_raw is None else redo_raw, "redo_of")
        w = {
            "n": n,
            "task": w_task,
            "load": _str_val(raw.get("load")),
            "tool": _str_val(raw.get("tool")),
            "model": _str_val(raw.get("model")),
            "effort": _str_val(raw.get("effort")),
            "run_id": _str_val(raw.get("run_id")),
            "status": status,
            "note": _str_val(raw.get("note")),
            "files": [str(x) for x in (raw_files or [])],
            "summary": _str_val(raw.get("summary")),
            "delivered": bool(raw.get("delivered", False)),
            "review": _str_val(raw.get("review")),
            "review_note": _str_val(raw.get("review_note")),
            "started": _num_or_none(raw.get("started")),
            "ended": _num_or_none(raw.get("ended")),
            "redo_of": redo_of,
        }
        if "exit_code" in raw:
            w["exit_code"] = raw["exit_code"]
        workers[n] = w

    next_n = _int_val(data.get("next_n", 1), "next_n")
    o = Orch(expected_id, task, project, cwd)
    o.workers = workers
    o.next_n = max(next_n, max(workers, default=0) + 1)
    created = _num_or_none(data.get("created"))
    if created is not None:
        o.created = created
    o._orphaned = {n for n, w in workers.items() if w["status"] in ("starting", "running")}
    return o


def _load(lead_id):
    path = _orch_path(lead_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _deserialize(data, lead_id)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as e:
        print(f"[orch] ignoring corrupt state {path}: {e}", file=sys.stderr)
        return None


def _prune(store):
    """Delete persisted orchestration files whose lead run finished more than PRUNE_DAYS days ago."""
    if store is None:
        return
    d = _orch_dir()
    if not d.is_dir():
        return
    cutoff = time.time() - PRUNE_DAYS * 86400
    with _lock:
        live_ids = set(_orchs)
    for path in d.glob("*.json"):
        if path.name.startswith("."):
            continue
        lid = path.stem
        if lid in live_ids:
            continue
        try:
            row = store.get(lid)
        except Exception:
            continue
        stale = False
        if row is not None:
            if row.get("status") in FINISHED_STATUSES:
                finished_at = row.get("ended") if row.get("ended") is not None else row.get("updated")
                if finished_at is not None and float(finished_at) < cutoff:
                    stale = True
        else:
            try:
                if path.stat().st_mtime < cutoff:
                    stale = True
            except OSError:
                continue
        if stale:
            try:
                path.unlink()
            except OSError as e:
                print(f"[orch] could not prune {path}: {e}", file=sys.stderr)


def _child_run_id(store, parent_id):
    db = getattr(store, "db", None)
    if db is None:
        return None
    from . import store as store_mod
    try:
        with store_mod._lock:
            r = db.execute("SELECT id FROM agents WHERE parent_run_id=? ORDER BY started DESC LIMIT 1",
                           (parent_id,)).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _latest_run(store, run_id):
    """Follow fallback / retry child runs started by runjob when `run_id` failed to launch a handoff."""
    seen = set()
    cur_id = run_id
    row = store.get(cur_id)
    while row and cur_id not in seen:
        seen.add(cur_id)
        act = str(row.get("activity") or "")
        if row.get("status") == "failed" and ("fallback to " in act or act.startswith("retrying (")):
            nxt = _child_run_id(store, cur_id)
            if not nxt or nxt in seen:
                break
            nxt_row = store.get(nxt)
            if not nxt_row:
                break
            cur_id, row = nxt, nxt_row
        else:
            break
    return row


def _reconcile_locked(o, store):
    """Update orphaned workers (restored without an in-memory on_finish callback) from `store`. Caller holds `o.cond`."""
    if store is None or not o._orphaned:
        return False
    changed = False
    for n in list(o._orphaned):
        w = o.workers.get(n)
        if w is None or w["status"] not in ("starting", "running"):
            o._orphaned.discard(n)
            continue
        rid = w.get("run_id") or ""
        if not rid:
            w.update(status="failed", ended=time.time(),
                     note=w.get("note") or "interrupted while starting (server restarted)",
                     delivered=bool(w.get("review")))
            o._orphaned.discard(n)
            changed = True
            continue
        row = _latest_run(store, rid)
        if not row:
            continue
        if row["id"] != rid:
            w["run_id"] = row["id"]
            if row.get("tool"):
                w["tool"] = row["tool"]
            if row.get("model"):
                w["model"] = row["model"]
            changed = True
        st = row.get("status")
        if st in FINISHED_STATUSES:
            w.update(status=st, ended=row.get("ended") or row.get("updated") or time.time(),
                     delivered=bool(w.get("review")))
            fill_report(store, w)
            o._orphaned.discard(n)
            changed = True
        elif w["status"] == "starting" and st in ("running", "waiting", "queued"):
            w["status"] = "running"
            changed = True
    if changed:
        _save_locked(o)
        o.cond.notify_all()
    return changed


def get(lead_id, store=None):
    with _lock:
        o = _orchs.get(lead_id)
    if o is None:
        path = _orch_path(lead_id)
        if path is not None and path.is_file():
            if store is not None:
                _prune(store)
            loaded = _load(lead_id)
            if loaded is not None:
                loaded._needs_prune = store is None
                with _lock:
                    o = _orchs.setdefault(lead_id, loaded)
    if o is None:
        raise ValueError("no orchestration with that id (it starts with /orchestrate <task>)")
    if store is not None:
        if getattr(o, "_needs_prune", False):
            o._needs_prune = False
            _prune(store)
        if o._orphaned:
            with o.cond:
                _reconcile_locked(o, store)
    return o


def lead_brief(cfg):
    py, hub, cap = sys.executable, HUB, capacity(cfg)
    cmd = f"{py} {hub} orch"
    return f"""You are the ORCHESTRATOR of a team of AI coding agents (Jarviee /orchestrate). You sit in window 1 of an \
iTerm2 panel; {cap} worker windows are free. Your job is to split the task below into jobs, hand them out, check every \
result, and keep every window busy. Do not do the jobs yourself, except a trivial fix while reviewing.

Control the team only with these shell commands (use your Bash tool):
  {cmd} assign "<self-contained job>" --load basic|light|complex|heavy [--tool agy|claude] [--model <id>] [--files a.py,b.py] [--redo N --note "<what was wrong>"]
  {cmd} wait                    blocks until a worker finishes and prints its report; run it with a Bash timeout of 600000 ms, and call it again if it says nothing finished yet
  {cmd} result N                the full report of worker N
  {cmd} review N accept|rework "<note>"
  {cmd} status                  every worker, its tool, model, state and review

How to work:
1. Plan first: split the task into independent jobs, on different files where you can. A worker sees only its job, so \
write the goal, the files, the constraints and how to verify it.
2. Give each job a load that fits it: basic (trivial edit, rename, format), light (small contained change), complex \
(multi-file feature or non-trivial bug), heavy (architecture, hard debugging, security). Jarviee turns the load into a \
tool (agy or claude) and a model; name --tool / --model only when you have a reason. When a tool is out of quota \
Jarviee falls back agy <-> claude -> OmniRoute by itself.
3. Start up to {cap} jobs at once. `assign` says when every window is busy: run `wait`.
4. Loop: `wait` -> for each finished worker read the report, look at the real changes (git diff, run the tests) -> \
`review N accept`, or `review N rework "..."` and re-assign it with `assign --redo N`. Then give the freed window the \
next job right away so it is never idle.
5. When no jobs are left, assign nothing more: keep calling `wait` until every worker has finished, and review each one.
6. You are done when every worker is reviewed and accepted. Then commit the combined changes with a conventional commit \
message (no AI co-author trailer, do not push) and end with a short report: what each worker did, how it was verified, \
anything left open.
7. Run long commands (test suites, builds, `orch wait`) in the foreground and do not end your turn while a background \
task or monitor is still running: your turn ending is the end of your job. Finish with the written report.

THE TASK:"""


def worker_brief(n, lead_id):
    return (f"You are worker {n} of a team led by an orchestrator. Do only the job below. Other workers edit this "
            "repository at the same time: stay on your own files and do not revert or reformat anything else. Do not "
            "commit. Run long commands (test suites, builds) in the foreground and do not end your turn while a "
            "background task or monitor is still running: your turn ending is the end of your job. Finish with the "
            "written report (in a few lines: what you changed and which files, how you verified it, and anything "
            "left undone — the orchestrator reviews it).\n\nYOUR JOB:\n")


# ---- starting the lead ----
def start(cfg, store, task, project=None, cwd=None):
    """Launch the orchestrator (Claude Opus, heavy) into window 1 of a new panel. Returns (run id, plan)."""
    p = launcher.plan(task, cfg, "claude", "heavy", project or None)   # no quota rerouting: Opus leads (runtime fallback still applies)
    p["orchestrate"] = {"role": "lead"}
    built, _ = launcher.build_prompt(task, cfg, p["project"])
    rid, p = launcher.launch(task, cfg, store, "claude", "heavy", project or None, cwd or None, p=p,
                             prompt=lead_brief(cfg) + "\n" + built)
    o = Orch(rid, task, p["project"], cwd)
    with _lock:
        _orchs[rid] = o
    with o.cond:
        _save_locked(o)
    return rid, p


# ---- the lead's commands ----
def _running(o):
    return [w for w in o.workers.values() if w["status"] in ("starting", "running")]


def assign(cfg, store, lead_id, task, load="", tool="", model="", project="", files=None, redo=0, note=""):
    """Start a worker in a free window. ValueError (a 400) when nothing can start: every window busy, the lead gone,
    an unknown load, tool or model."""
    o = get(lead_id, store)
    lead = store.get(lead_id) or {}
    if lead.get("status") not in ("running", "waiting"):
        raise ValueError("the orchestrator session has ended, so nothing more can be assigned")
    task = (task or "").strip()
    if load and load not in LOADS:
        raise ValueError(f"load must be one of {', '.join(LOADS)}")
    with o.cond:
        prev = o.workers.get(int(redo)) if redo else None
        if redo and prev is None:
            raise ValueError(f"no worker {redo} to redo")
        if prev:
            task = task or prev["task"]
            task += ("\n\nA reviewer rejected an earlier attempt at this job"
                     + (f": {note.strip()}" if note and note.strip() else ".")
                     + "\nCheck git status and the diff first, then fix it from the existing changes.")
        if not task:
            raise ValueError("the job is empty")
        if len(_running(o)) >= capacity(cfg):
            raise ValueError(f"all {capacity(cfg)} worker windows are busy: run `orch wait`, then assign again")
        n = o.next_n
        o.next_n += 1
        w = o.workers[n] = {"n": n, "task": task, "load": load, "tool": "", "model": "", "effort": "", "run_id": "",
                            "status": "starting", "note": "", "files": [], "summary": "", "delivered": False,
                            "review": "", "review_note": "", "started": time.time(), "ended": None,
                            "redo_of": int(redo) if prev else 0}
        if prev:
            prev["review"], prev["review_note"], prev["delivered"] = "rework", (note or "").strip(), True
        _save_locked(o)
    try:
        proj = project or o.project or None
        p = launcher.plan(task, cfg, tool or None, load or None, proj, model=model or None, store=store)
        p["orchestrate"] = {"role": "worker", "lead": lead_id}
        if files:
            p["files"] = list(files)
        built, _ = launcher.build_prompt(task, cfg, p["project"])
        rid, p = launcher.launch(task, cfg, store, None, None, proj, o.cwd or lead.get("workspace") or None, p=p,
                                 prompt=worker_brief(n, lead_id) + built, parent_run_id=lead_id,
                                 on_finish=lambda r, s, c, n=n: _finished(o, n, r, s, c))
    except Exception:
        with o.cond:
            o.workers.pop(n, None)
            _save_locked(o)
            o.cond.notify_all()
        raise
    with o.cond:
        if w["status"] == "starting":
            w["status"] = "running"
        w.update(run_id=rid, tool=p["tool"], model=p["model"], effort=p["effort"], load=p["tier"],
                 note="; ".join(str(x) for x in (p.get("reasons") or [])[-2:]))
        _save_locked(o)
    return {"worker": n, "run_id": rid, "tool": p["tool"], "model": p["model"], "effort": p["effort"],
            "load": p["tier"], "fallback": p.get("fallback") or "", "free_windows": capacity(cfg) - len(_running(o))}


def _report_text(store, run_id):
    row = store.get(run_id) or {}
    path = row.get("log_path")
    text = ""
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    return outcome.closing_message(launcher.strip_tty(finaltext.after_mark(text)))[-REPORT_CHARS:], row


def _finished(o, n, run_id, status, code):
    """on_finish of a worker's last run in its fallback chain: the run id changes when it fell back to another tool."""
    with o.cond:
        w = o.workers.get(n)
        if w is None:
            return
        was_running = w["status"] in ("starting", "running")
        w.update(run_id=run_id, status=status if status in ("done", "failed", "stopped", "waiting") else "failed",
                 ended=time.time(), exit_code=code)
        if was_running and not w.get("review"):
            w["delivered"] = False
        o._orphaned.discard(n)
        _save_locked(o)
        o.cond.notify_all()


def fill_report(store, w):
    """Refresh a finished worker's summary, files and the tool / model that really ran it (a fallback may have changed them)."""
    text, row = _report_text(store, w["run_id"])
    files = row.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except ValueError:
            files = []
    w.update(summary=text, files=list(files or []), tool=row.get("tool") or w["tool"],
             model=row.get("model") or w["model"], note=str(row.get("activity") or w["note"])[:200])


def _fill_report_changed(store, w):
    before = (w.get("summary"), list(w.get("files") or []), w.get("tool"), w.get("model"), w.get("note"))
    fill_report(store, w)
    after = (w.get("summary"), list(w.get("files") or []), w.get("tool"), w.get("model"), w.get("note"))
    return after != before


def _public(w):
    keys = ("n", "task", "load", "tool", "model", "effort", "run_id", "status", "note", "files", "summary", "review",
            "review_note", "redo_of")
    out = {k: w.get(k) for k in keys}
    out["task"] = out["task"][:400]
    return out


def wait(cfg, store, lead_id, timeout=WAIT_MAX):
    """Block until a worker has finished that the lead has not been told about, then report it. Also returns when
    nothing is running any more, or after `timeout` seconds with nothing new."""
    o = get(lead_id, store)
    end = time.time() + max(1.0, min(float(timeout), WAIT_MAX))
    with o.cond:
        while True:
            if o._orphaned:
                _reconcile_locked(o, store)
            fresh = sorted(
                (w for w in o.workers.values()
                 if w["status"] not in ("starting", "running") and not w.get("delivered") and not w.get("review")),
                key=lambda w: w["n"],
            )
            if fresh:
                for w in fresh:
                    fill_report(store, w)
                    w["delivered"] = True
                _save_locked(o)
                return _state(cfg, o, finished=[_public(w) for w in fresh])
            if not _running(o):
                return _state(cfg, o, finished=[], idle=True)
            left = end - time.time()
            if left <= 0:
                return _state(cfg, o, finished=[], timeout=True)
            o.cond.wait(min(left, POLL_INTERVAL if o._orphaned else 5.0))


def _state(cfg, o, **extra):
    ws = sorted(o.workers.values(), key=lambda w: w["n"])
    running = _running(o)
    return {**extra, "running": [w["n"] for w in running], "free_windows": capacity(cfg) - len(running),
            "to_review": [w["n"] for w in ws if w["status"] not in ("starting", "running") and w["review"] != "accept"
                          and not (w["review"] == "rework" and any(x["redo_of"] == w["n"] for x in ws))],
            "all_accepted": bool(ws) and not running and all(
                w["review"] == "accept" or (w["review"] == "rework" and any(x["redo_of"] == w["n"] for x in ws))
                for w in ws)}


def status(cfg, store, lead_id):
    o = get(lead_id, store)
    with o.cond:
        changed = False
        for w in o.workers.values():
            if w["status"] not in ("starting", "running") and not w["summary"]:
                if _fill_report_changed(store, w):
                    changed = True
        if changed:
            _save_locked(o)
        return {"task": o.task[:300], "capacity": capacity(cfg),
                "workers": [dict(_public(w), summary=w["summary"][-300:]) for w in sorted(o.workers.values(), key=lambda w: w["n"])],
                **_state(cfg, o)}


def result(cfg, store, lead_id, n):
    o = get(lead_id, store)
    with o.cond:
        w = o.workers.get(int(n))
        if w is None:
            raise ValueError(f"no worker {n}")
        if w["status"] not in ("starting", "running"):
            if _fill_report_changed(store, w):
                _save_locked(o)
        return _public(w)


def review(cfg, lead_id, n, verdict, note="", store=None):
    if verdict not in VERDICTS:
        raise ValueError("verdict must be accept or rework")
    o = get(lead_id, store)
    with o.cond:
        w = o.workers.get(int(n))
        if w is None:
            raise ValueError(f"no worker {n}")
        if w["status"] in ("starting", "running"):
            raise ValueError(f"worker {n} is still working")
        w["review"], w["review_note"], w["delivered"] = verdict, (note or "").strip(), True
        _save_locked(o)
        out = _state(cfg, o)
    out["hint"] = (f"re-assign it: orch assign \"<job>\" --redo {n} --note \"...\"" if verdict == "rework"
                   else ("every worker is accepted: commit and report" if out["all_accepted"] else ""))
    return out
