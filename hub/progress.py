"""Progress of a launched run: step counts from its log and (agy) transcript, and orphan reconciliation.

`sync_run_progress` runs for every launcher row on every `/api/state` poll, so it is incremental: it remembers
how far it has read each run's log and transcript (byte offsets), looks for an agy transcript at a throttled
rate, and does nothing at all for a finished run whose files have not changed since it last looked.
"""
import json
import os
import re
import time
from pathlib import Path

from . import brand, jobstatus, orphans, runstate

TOOL_LINE_RE = re.compile(
    r"^\s*(?:Running|Ran|Calling|Called|Executing|Executed|Tool:|Using(?:\s+tool)?|"
    r"\[agy\]|\[claude\]|\[tool\]|●|▸|▶|"
    r"Read(?:ing)?|Wrote|Writing|Edit(?:ed|ing)?|Creat(?:ed|ing)|Updat(?:ed|ing)|"
    r"Search(?:ed|ing)|List(?:ed|ing)|Check(?:ed|ing)|Test(?:ed|ing)|"
    r"run_command|read_file|edit_file|write_file|Bash|Read|Edit|Write|Grep|Glob)\b[:\s]*(.*)",
    re.I,
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

READ_CAP = 8 * 1024 * 1024        # bytes of one log/transcript read per call; a bigger backlog takes a few polls
SEARCH_EVERY = (5.0, 30.0)        # seconds between looks for an agy transcript while running (first, longest)
STATE_LIMIT = 1000                # remembered runs; the oldest are forgotten beyond it

_transcript_cache = {}   # run_id -> {"path": Path, "lines": int, "steps": int, "act": str} (kept for old callers)
_state = {}              # run_id -> _Cursor


def strip_tty(text):
    """Terminal output (runs in an iTerm2 pane) → plain lines: no ANSI/OSC escapes, last state of \\r lines."""
    text = ANSI_RE.sub("", text).replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def is_tool_line(s):
    return bool(TOOL_LINE_RE.match(s) or '"tool_use"' in s or '"toolCall"' in s or '"tool_name"' in s)


def observe_log_steps(run_id, log_path, store, tracker, running=True):
    """Scan new log lines for tool invocations or output and bump steps when hooks haven't."""
    row = store.get(run_id) or {}
    if row.get("job_id") and tracker.get("job_id") != row["job_id"]:
        if "job_id" in tracker:   # a new job in the same live pane: count only its own steps
            tracker["log_steps"] = 0
            tracker["has_output"] = False
            tracker["offset"] = max(tracker["offset"], int(row.get("job_log_offset") or 0))
        tracker["job_id"] = row["job_id"]
    chunk = _read_from(log_path, tracker["offset"])
    if not chunk:
        return
    if running and not chunk.endswith(b"\n"):
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return
        chunk = chunk[:last_nl + 1]
    tracker["offset"] += len(chunk)
    text = strip_tty(chunk.decode("utf-8", errors="replace"))
    latest_act = ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("$ ") or s.startswith("[git]"):
            continue
        tracker["has_output"] = True
        if is_tool_line(s):
            tracker["log_steps"] += 1
            latest_act = s[:80]
    target = tracker["log_steps"] or (1 if tracker["has_output"] else 0)
    if target <= 0:
        return
    cur = store.get(run_id)
    if not cur:
        return
    _raise_steps(store, run_id, int(cur.get("steps") or 0), target, cur.get("updated"))
    if running and latest_act and jobstatus.job_status(cur) == "running" and cur.get("activity") in ("starting", "working"):
        store.upsert(run_id, activity=latest_act)


def _read_from(path, offset, cap=None):
    """Bytes of `path` from `offset` (at most `cap`); b'' when missing or unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(cap) if cap else f.read()
    except OSError:
        return b""


def _raise_steps(store, run_id, cur, target, updated=None):
    """Bring the stored step count up to `target` (never down)."""
    if target <= cur:
        return
    if target - cur <= 3:
        for _ in range(target - cur):
            store.bump_steps(run_id)
    else:   # a backlog: one write, keeping the row's `updated` (progress is not a state change)
        fields = {"steps": target}
        if updated:
            fields["updated"] = updated
        store.upsert(run_id, **fields)


def find_agy_transcript(task, started):
    """Locate the Antigravity transcript.jsonl corresponding to a launched task."""
    root_str = brand.env("AGY_BRAIN_ROOT")
    brain_root = Path(root_str) if root_str else (Path.home() / ".gemini" / "antigravity-cli" / "brain")
    try:
        if not brain_root.is_dir():
            return None
        needle = (task or "").strip()[:40].lower()
        if not needle:
            return None
        words = [w.lower() for w in re.findall(r"\b[a-zA-Z0-9_-]{4,}\b", task or "")][:4]
        candidates = []
        for d in brain_root.iterdir():
            tp = d / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = tp.stat()
            except OSError:
                continue
            if started and st.st_mtime < (started - 30):
                continue
            candidates.append((st.st_mtime, tp))
        candidates.sort(key=lambda x: -x[0])
        for _, tp in candidates[:25]:
            try:
                first = ""
                with open(tp, encoding="utf-8", errors="replace") as f:
                    first = f.readline()
                low_first = first.lower()
                if needle in low_first or (words and all(w in low_first for w in words)):
                    return tp
                full_tp = tp.parent / "transcript_full.jsonl"
                if full_tp.exists():
                    with open(full_tp, encoding="utf-8", errors="replace") as f:
                        first_full = f.readline()
                    low_full = first_full.lower()
                    if needle in low_full or (words and all(w in low_full for w in words)):
                        return tp
            except OSError:
                continue
    except OSError:
        return None
    return None


class _Cursor:
    """What `sync_run_progress` has already read of one run."""
    __slots__ = ("log_off", "hits", "body", "t_off", "t_steps", "t_act", "t_path", "searches", "next_search",
                 "settled")

    def __init__(self):
        self.log_off = 0          # bytes of the log counted so far
        self.hits = 0             # tool lines in them
        self.body = False         # any output line in them
        self.t_off = 0            # bytes of the transcript consumed
        self.t_steps = 0
        self.t_act = ""
        self.t_path = None
        self.searches = 0         # looks for the transcript so far
        self.next_search = 0.0
        self.settled = None       # file signature of a finished run that is fully read


def _cursor(run_id):
    cur = _state.get(run_id)
    if cur is None:
        if len(_state) >= STATE_LIMIT:
            for k in list(_state)[:STATE_LIMIT // 4]:
                _state.pop(k, None)
        cur = _state[run_id] = _Cursor()
    return cur


def _sig(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p) if p else None
            out.append((st.st_size, st.st_mtime_ns) if st else None)
        except OSError:
            out.append(None)
    return tuple(out)


def _transcript(a, run_id, cur, finished, store, log_path):
    """Count agy tool calls in the run's transcript, from the byte offset reached last time."""
    L = runstate.launcher()
    if cur.t_path is None:
        now = time.time()
        if now < cur.next_search or (finished and cur.searches):
            return   # looked recently (or, once finished, looked already): never rescan the brain directory per poll
        found = L._find_agy_transcript(a.get("task", ""), a.get("started") or 0)
        cur.searches += 1
        cur.next_search = now + min(SEARCH_EVERY[1], SEARCH_EVERY[0] * cur.searches)
        if not found:
            return
        cur.t_path = found
        _transcript_cache[run_id] = {"path": found, "lines": 0, "steps": 0, "act": ""}
    chunk = _read_from(cur.t_path, cur.t_off, READ_CAP)
    if not chunk:
        return
    if not finished and not chunk.endswith(b"\n"):   # a half-written last line waits for the next call
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return
        chunk = chunk[:nl + 1]
    cur.t_off += len(chunk)
    appended = []
    for ln in chunk.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for tc in obj.get("tool_calls") or []:
            tname = tc.get("name") or tc.get("tool_name") or "tool"
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            summary = args.get("toolSummary") or args.get("Path") or args.get("CommandLine") or ""
            if isinstance(summary, str):
                summary = summary.strip('"')[:60]
            cur.t_steps += 1
            cur.t_act = f"ran {tname}"
            appended.append(f"Ran {tname}" + (f": {summary}" if summary else ""))
    entry = _transcript_cache.get(run_id)
    if entry:
        entry.update(steps=cur.t_steps, act=cur.t_act)
    if appended and log_path and log_path.exists():
        try:
            existing_log = log_path.read_text(encoding="utf-8", errors="replace")
            to_write = [x for x in appended if x not in existing_log]
            if to_write:
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write("\n".join(to_write) + "\n")
        except OSError:
            pass
    cur_steps = int(a.get("steps") or 0)
    if cur.t_steps > cur_steps:
        _raise_steps(store, run_id, cur_steps, cur.t_steps, a.get("updated"))
        a["steps"] = cur.t_steps
    if jobstatus.job_status(a) == "running" and cur.t_act and a.get("activity") in ("starting", "working"):
        store.upsert(run_id, activity=cur.t_act)
        a["activity"] = cur.t_act


def _log(a, run_id, cur, log_path, finished, store):
    """Count tool lines in the part of the log not read yet."""
    chunk = _read_from(log_path, cur.log_off, READ_CAP)
    if chunk and not finished and not chunk.endswith(b"\n"):
        nl = chunk.rfind(b"\n")
        chunk = chunk[:nl + 1] if nl != -1 else b""
    if chunk:
        cur.log_off += len(chunk)
        for ln in strip_tty(chunk.decode("utf-8", errors="replace")).splitlines():
            s = ln.strip()
            if not s or s.startswith("$ ") or s.startswith("[git]"):
                continue
            cur.body = True
            if is_tool_line(s):
                cur.hits += 1
    target = cur.hits or (1 if cur.body else 0)
    cur_steps = int(a.get("steps") or 0)
    if target > cur_steps:
        _raise_steps(store, run_id, cur_steps, target, a.get("updated"))
        a["steps"] = target


def sync_run_progress(a, store):
    """Reconcile orphan PIDs and sync live step counts from transcript.jsonl / log files."""
    run_id = a.get("id") or ""
    if not run_id:
        return
    status = jobstatus.job_status(a)
    finished = status not in runstate.LIVE
    log_str = a.get("log_path") or ""
    log_path = Path(log_str) if log_str else None
    cur = _state.get(run_id)
    if finished:
        if cur is None and int(a.get("steps") or 0) > 0:
            # a finished run the worker already counted (it syncs once more as it ends): nothing to read
            cur = _cursor(run_id)
            cur.settled = _sig(log_path, None)
            return
        if cur is not None and cur.settled is not None:
            if _sig(log_path, cur.t_path) == cur.settled:
                return   # same files as when it settled: nothing new to count
    cur = cur or _cursor(run_id)

    # 1. agy: the transcript counts tool calls even when non-TTY stdout is buffered
    if a.get("tool") == "agy":
        _transcript(a, run_id, cur, finished, store, log_path)
    # 2. the log: tool lines (the fallback when no transcript was found, and for claude / echo)
    if log_path:
        _log(a, run_id, cur, log_path, finished, store)
    if finished:
        sig = _sig(log_path, cur.t_path)
        log_sig, tr_sig = sig
        drained = (log_sig is None or cur.log_off >= log_sig[0]) and (tr_sig is None or cur.t_off >= tr_sig[0])
        cur.settled = sig if drained else None   # a backlog bigger than one read: finish it on the next poll

    # 3. Reconcile orphaned launcher runs from a previous server process (no active worker thread)
    thread = runstate.THREADS.get(run_id)
    is_managed = (run_id in runstate.PROCS) or bool(thread and thread.is_alive())
    if status in runstate.LIVE and a.get("source") == "launcher" and not is_managed:
        if not orphans.run_alive(a, nopid_grace=60):
            # no worker recorded an exit, so it did not verifiably finish: the one shared rule (hub/orphans.py)
            orphans.settle_lost(store, a)
