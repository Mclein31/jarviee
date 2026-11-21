"""Start an agent on the model the router picked, and track it until it ends.

This module is the entry point (`launch`, `plan`, `stop`, `sync_run_progress`, ...). The work lives in focused modules
and every old name is re-exported here, because other code and the tests call and patch `launcher.<name>`:

  runplan   routing, prompt and command line      (plan, build_argv, build_prompt, readonly_args, interactive_argv)
  runjob    the worker of one run                 (RunJob: wait for files, run, bridge, fallback, retry, finish)
  panerun   running inside an iTerm2 pane         (run_in_pane)
  progress  steps from the log / agy transcript   (sync_run_progress, strip_tty)
  gitops    commit and diff                       (auto_commit, run_diff, clean_commit_message)
  stopper   stopping processes                    (stop_plan, stop_status, stop, stop_with_reason, closed_by_user)
  runstate  the shared dicts (_procs, _threads, LIVE_RUNS)
"""
import json
import os
import shutil   # noqa: F401  (kept: tests and callers patch launcher.shutil)
import subprocess   # noqa: F401  (kept: tests and callers patch launcher.subprocess)
import threading
import time
import uuid
from pathlib import Path

from . import board_api, brand, coord, gitops, obsidian, outcome, panerun, progress, runplan, runstate, snapshot, stopper, terminal
from .runjob import RunJob
from .titles import make_summary, make_title

# ---- shared state (the same objects the other modules use) ----
_procs = runstate.PROCS
_threads = runstate.THREADS
LIVE_RUNS = runstate.LIVE_RUNS
LIVE = runstate.LIVE
FINISHED_CLOSED = runstate.FINISHED_CLOSED
LIVE_NOTE = runstate.LIVE_NOTE
TURN_DONE_RE = panerun.TURN_DONE_RE

# ---- planning and command lines ----
PLACEHOLDER = runplan.PLACEHOLDER
pick_effort = runplan.pick_effort
plan = runplan.plan
readonly_args = runplan.readonly_args
build_argv = runplan.build_argv
build_bridge_argv = runplan.build_bridge_argv
build_prompt = runplan.build_prompt
interactive_argv = runplan.interactive_argv
env_for = runplan.env_for

# ---- progress ----
ANSI_RE = progress.ANSI_RE
TOOL_LINE_RE = progress.TOOL_LINE_RE
_transcript_cache = progress._transcript_cache
strip_tty = progress.strip_tty
_observe_log_steps = progress.observe_log_steps
_find_agy_transcript = progress.find_agy_transcript
sync_run_progress = progress.sync_run_progress

# ---- git ----
AI_TERMS_RE = gitops.AI_TERMS_RE
CONVENTIONAL_RE = gitops.CONVENTIONAL_RE
clean_commit_message = gitops.clean_commit_message
_git_env = gitops.git_env
_git_status = gitops.git_status
auto_commit = gitops.auto_commit
run_diff = gitops.run_diff

# ---- stopping ----
AGENT_NAMES = stopper.AGENT_NAMES
STALE_AFTER = stopper.STALE_AFTER
APP_HOSTS = stopper.APP_HOSTS
proc_name = stopper.proc_name
_is_agent = stopper.is_agent
stop_plan = stopper.stop_plan
stop_status = stopper.stop_status
stop = stopper.stop
stop_with_reason = stopper.stop_with_reason
closed_by_user = stopper.closed_by_user

# ---- panes ----
_pane_title = panerun.pane_title
_log_note = panerun.log_note
_run_in_pane = panerun.run_in_pane


def is_live(run_id, store=None):
    """True for a launched run that is a live CLI session in a pane (its Stop hook means "your turn").
    Kept on the row too (agents.interactive), so a hub restart doesn't forget it: in memory only, every live
    session started before a restart looked like a print run that never finished, and held up the reports."""
    if run_id in LIVE_RUNS:
        return True
    row = store.get(run_id) if store is not None and run_id else None
    if not row or row.get("source") != "launcher" or row.get("status") not in ("running", "waiting"):
        return False
    if row.get("interactive"):
        return True
    try:   # rows from before the column existed: the launcher noted it at the top of the run's log
        with open(row.get("log_path") or "", "rb") as f:
            return LIVE_NOTE.encode() in f.read(4096)
    except (OSError, TypeError):
        return False


def prune_logs(cfg, days=30, now=None):
    """Delete run logs older than `days` days (never one a live run is writing). Returns how many were removed."""
    d, n, cut = Path(cfg["logs_dir"]).expanduser(), 0, (now or time.time()) - days * 86400
    for f in (d.glob("run-*.log") if d.is_dir() else []):
        try:
            if f.stat().st_mtime < cut and f.stem not in _procs:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def agy_log_dir():
    return Path(brand.env("AGY_LOG_DIR") or "~/.gemini/antigravity-cli/log").expanduser()


def agy_soft_denied(since, log_dir=None):
    """True when an agy CLI log written since `since` shows print mode soft-denying a tool confirmation:
    agy then exits 0 although the shell command it needed never ran."""
    try:
        for f in Path(log_dir or agy_log_dir()).glob("cli-*.log"):
            if f.stat().st_mtime >= since - 1 and "soft-denying tool confirmation" in f.read_text(
                    encoding="utf-8", errors="replace"):
                return True
    except OSError:
        pass
    return False


def wait(run_id, timeout=None):
    t = _threads.get(run_id)
    if t:
        t.join(timeout)
        return not t.is_alive()
    return True


# ---- starting a run ----
def _resolve(task, cfg, tool, tier, project, cwd, p, prompt, fallback_from):
    """Validate the request and settle what will run: (route plan, prompt, working folder)."""
    if project and not obsidian.find_project(cfg, project):
        raise ValueError(f"unknown project '{project}' (no {project}/.brain/ in the workspace)")
    p = p or plan(task, cfg, tool, tier, project)
    if p.get("read_only") and not readonly_args(p["tool"]):
        raise ValueError(f"{p['tool']} has no read-only mode; a read-only task is never run writable")
    if fallback_from:
        p["fallback_from"] = fallback_from
    built, meta = build_prompt(task, cfg, p["project"])
    if prompt is None:
        prompt = built
    if not meta and p["project"]:
        meta = {"path": str(obsidian.find_project(cfg, p["project"]) or "")}
    ws_dir = obsidian.workspace(cfg).expanduser()
    workdir = cwd or meta.get("path") or (str(ws_dir) if ws_dir.is_dir() else None)
    if workdir and not Path(workdir).is_dir():
        raise ValueError(f"working directory not found: {workdir}")
    return p, prompt, workdir


def _claim_files(store, run_id, task, p, workdir, ro):
    """Coordination (hub/coord.py): claim the files this run will touch. Overlap with an active agent means it
    waits for that agent (sequential + hand-off) or is told which files to keep off (split)."""
    co = {"decision": "go", "with": [], "note": ""}
    if not ro:
        try:
            co = coord.register(store, run_id, workdir or os.getcwd(), task,
                                list(p.get("files") or []) or coord.files_in(task, workdir or os.getcwd()),
                                title=make_title(task))
        except Exception as e:   # coordination must never stop a run
            print(f"[coord] {e!r}")
    return co


def _open_run(store, run_id, task, title, p, workdir, log_path, session, parent_run_id, fallback_from, hops):
    """Write the log header and the `running` row; returns the header's byte length (where the log body starts)."""
    eff = f", effort {p['effort']}" if p["effort"] else ""
    header = f"$ {p['tool']} --model {p['model']}{eff}  (tier: {p['tier']})\n\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(header, encoding="utf-8")
    except OSError:
        pass
    store.upsert(run_id, source="launcher", tool=p["tool"], task=task, project=p["project"],
                 title=title, summary=make_summary(task, title),
                 tier=p["tier"], suggested_tier=p["suggested_tier"], model=p["model"],
                 effort=p["effort"], reasons=json.dumps(p["reasons"]), status="running",
                 activity="starting", workspace=workdir or os.getcwd(), log_path=str(log_path),
                 parent_run_id=parent_run_id, fallback_from=fallback_from, hops=hops, session_id=session,
                 job_id="job-" + uuid.uuid4().hex[:8], job_no=0, job_log_offset=0)
    return len(header.encode("utf-8"))


def launch(task, cfg, store, tool=None, tier=None, project=None, cwd=None,
           p=None, prompt=None, on_finish=None, log_runs=True,
           parent_run_id=None, hops=0, fallback_from=None, resume=None, retries=0):
    """Start one agent run. `resume` is a session id from an earlier run of the same tool: `prompt` is then
    only the reply, sent into that conversation. The scheduler passes a pre-routed `p`, a full `prompt`,
    an `on_finish(run_id, status, exit_code)` callback and log_runs=False.

    Validates and records the run here, then hands it to a `RunJob` worker thread (hub/runjob.py)."""
    p, prompt, workdir = _resolve(task, cfg, tool, tier, project, cwd, p, prompt, fallback_from)
    run_id = "run-" + uuid.uuid4().hex[:8]
    log_path = Path(cfg["logs_dir"]) / f"{run_id}.log"
    ro = bool(p.get("read_only"))   # research / review runs: plan mode, nothing is edited
    tcfg = cfg["tools"][p["tool"]]
    base_env = dict(os.environ, JARVIEE_RUN_ID=run_id, JARVIEE_PORT=str(cfg["port"]))
    base_env.pop("AGENT_HUB_RUN_ID", None)
    base_env.pop("AGENT_HUB_PORT", None)
    base_env.pop("JARVIEE_TOOL", None)
    env = runplan.env_for(tcfg, cfg, base_env)
    session = resume or (str(uuid.uuid4()) if tcfg.get("session_args") else None)
    co = _claim_files(store, run_id, task, p, workdir, ro)
    if co["decision"] == "split":
        prompt = prompt + "\n\n" + co["note"]

    def argv_for(pr):
        a = build_argv(tcfg, pr, p["model"], p["effort"], read_only=ro, session=session, resume=bool(resume))
        return runplan.append_flags(a, readonly_args(p["tool"])) if ro else a

    title = make_title(task)
    header_len = _open_run(store, run_id, task, title, p, workdir, log_path, session, parent_run_id,
                           fallback_from, hops)
    job = RunJob(run_id=run_id, task=task, cfg=cfg, store=store, p=p, prompt=prompt, argv_for=argv_for,
                 workdir=workdir, log_path=log_path, env=env,
                 tracker={"offset": header_len, "log_steps": 0, "has_output": False},
                 co=co, title=title, term=terminal.active(cfg), ro=ro, on_finish=on_finish, log_runs=log_runs,
                 hops=hops, fallback_from=fallback_from, retries=retries)
    t = threading.Thread(target=job.run_guarded, daemon=True)
    _threads[run_id] = t
    t.start()
    return run_id, p


def _finish(run_id, status, note, p, task, cfg, store, log_runs=True,
            workdir=None, log_path=None, auto_git=True, files=None):
    cur = store.get(run_id) or {}
    if cur.get("closed_by_user") and cur.get("activity") != FINISHED_CLOSED:   # closed while working: stays closed
        status = "stopped"
        note = str(cur.get("activity") or "") or "stopped by you"
    if log_runs:
        try:  # log first, so "done" on the dashboard means the brain line is written
            obsidian.log_run(cfg, {**p, "task": task, "status": status})
        except OSError as e:
            note += f" (could not write AGENT_RUNS.md: {e})"
    # verified = finished cleanly and we know exactly which files it changed; only those are committed
    verified = status == "done" and bool(files)
    if verified and auto_git and workdir:
        auto_commit(workdir, task, log_path=log_path, files=files, verified=True)
    store.upsert(run_id, status=status, activity=note, ended=time.time(), verified=int(verified))
    board_api.notify_run(cfg, run_id, status, files)   # the linked Project Board card advances (never raises)
    try:   # release this run's claims and hand its result to anyone waiting on it
        said = ""
        if log_path:
            try:
                said = outcome.closing_message(strip_tty(Path(log_path).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                said = ""
        coord.release(store, run_id, status, summary=" ".join(str(said or note).split())[:300])
    except Exception as e:
        print(f"[coord] {e!r}")
