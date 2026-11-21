"""Running an agent inside an iTerm2 pane (hooks/pane_runner.py does the work in the pane)."""
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import guards, panes, progress, runstate, terminal
from .runplan import interactive_argv

TURN_DONE_RE = re.compile(r"^(turn finished|claude is waiting for your input)", re.I)   # alerts.REPLIED_RE


def pane_title(p, title, workdir):
    place = p.get("project") or (Path(workdir).name if workdir else "") or "—"
    short = title if len(title) <= 40 else title[:39].rstrip() + "…"
    if p.get("fallback_from"):
        return f"fallback from {p['fallback_from']} · {p['tool']} · {short}"
    return f"{p['tool']} · {p['tier']} · {place} · {short}"


def log_note(log_path, text):
    try:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n[pane] {text}\n")
    except OSError:
        pass


def run_in_pane(run_id, term, cfg, store, tracker, p, title, argv, workdir, env, log_path, interactive=False,
                unattended=False):
    """Run inside an iTerm2 pane (hooks/pane_runner.py). Returns (exit code, start error, True),
    or (None, "", False) when the pane can't open or never claims the run: then it runs in the background.
    `interactive`: the real claude / agy TUI (logo, colours, input) instead of a one-shot print run.
    `unattended` (queue / pipeline runs): every finished turn is the end of the run, nobody is asked to reply."""
    L = runstate.launcher()
    live = interactive_argv(p["tool"], argv, unattended=unattended) if interactive else None
    if live:
        argv = live
        L._log_note(log_path, runstate.LIVE_NOTE + ": reply, approve commands or /exit there")
    exe = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    rule = panes.close_rule(cfg)
    spec = {"run_id": run_id, "argv": [exe, *argv[1:]], "cwd": workdir or os.getcwd(), "env": env,
            "log": str(log_path), "title": L._pane_title(p, title, workdir),
            "close": rule, "close_delay": panes.CLOSE_RULES[rule], "interactive": bool(live),
            "unattended": bool(live and unattended)}
    if live and unattended:   # how long the pane keeps a run whose turn ended waiting on its own background task
        spec["background_wait"] = guards.background_wait_seconds(cfg)
    run = panes.register(run_id, spec)
    if live:
        runstate.LIVE_RUNS.add(run_id)
        store.upsert(run_id, interactive=runstate.LIVE_UNATTENDED if unattended else 1)

    def background(why):
        panes.drop(run_id)
        runstate.LIVE_RUNS.discard(run_id)   # the background fallback runs the original one-shot command
        store.upsert(run_id, interactive=0)
        L._log_note(log_path, f"{why}; running in the background instead")
        store.upsert(run_id, activity="starting")
        return None, "", False

    try:
        sid = term.open_pane(run_id, terminal.pane_command(cfg, run_id),
                             **({"orchestrate": p["orchestrate"]} if p.get("orchestrate") else {}))   # /orchestrate: lead or worker window
    except Exception as e:   # iterm.TerminalError, PermissionDenied, ValueError: never lose the run
        return background(f"iTerm2 pane unavailable ({str(e)[:160]})")
    store.upsert(run_id, pane_id=sid, activity="opening iTerm2 pane")
    if not run.claimed_evt.wait(panes.START_TIMEOUT) and panes.abandon(run_id):
        threading.Thread(target=term.close_pane, args=(run_id,), daemon=True).start()
        return background("the iTerm2 pane didn't start in time")
    if not run.started.wait(panes.CLAIMED_TIMEOUT) and not run.exited.is_set():
        panes.drop(run_id)
        runstate.LIVE_RUNS.discard(run_id)
        return None, "the pane runner didn't start the agent", True
    proc = runstate.PROCS[run_id] = panes.PaneProc(run.pid)
    store.upsert(run_id, pid=run.pid, activity="working")
    row = store.get(run_id)
    if row and row.get("status") == "stopped":   # Stop was pressed while the pane was opening
        proc.terminate()
    while not run.exited.wait(0.3):
        L._observe_log_steps(run_id, log_path, store, tracker, running=True)
        row = store.get(run_id)
        if row:
            L.sync_run_progress(row, store)
        if proc.poll() is not None:   # runner gone without an exit event: pane closed by hand
            run.exited.wait(1.0)
            break
    runstate.PROCS.pop(run_id, None)
    runstate.LIVE_RUNS.discard(run_id)
    L._observe_log_steps(run_id, log_path, store, tracker, running=False)
    row = store.get(run_id)
    if row:
        L.sync_run_progress(row, store)
        if int(row.get("steps") or 0) > 0:
            tracker["has_output"] = True
    panes.drop(run_id)
    if not run.exited.is_set():
        store.upsert(run_id, status="stopped", activity="stopped (pane closed)", closed_by_user=1, ended=time.time())
        return None, "", True
    if run.stopped and run.code != 124:   # stopped in or by its pane (Ctrl-C, pane closed, Stop): the owner's call
        cur = store.get(run_id) or {}
        act = str(cur.get("activity") or "")
        if act.startswith(("stopped", "dismissed")):
            pass
        elif unattended and cur.get("status") == "failed":
            pass   # a queue run's turn failed (rate limit, auth...): the pane closed itself, the run stays failed
        elif unattended and cur.get("status") == "running" and act.startswith(runstate.BG_WAIT):
            # its turn ended waiting on a background task and the pane ended it at the guards.background_wait cap:
            # finished, as it would have been without the wait
            L._log_note(log_path, "a background task was still running at the background-wait cap; run finished")
            store.upsert(run_id, status="done", closed_by_user=1, activity=runstate.FINISHED_CLOSED, ended=time.time())
        elif cur.get("status") == "done" or (cur.get("status") == "waiting" and TURN_DONE_RE.match(act)):
            # a live session closed after its turn had finished: that's how a live session ends, not a stop
            store.upsert(run_id, closed_by_user=1, activity=runstate.FINISHED_CLOSED)
        else:
            store.upsert(run_id, status="stopped", closed_by_user=1, activity="stopped (closed in its pane)", ended=time.time())
    return run.code, "", True
