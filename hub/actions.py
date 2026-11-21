"""POST endpoints that are more than a store write: attachments, terminal, hub actions, quota probe, run events."""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import auth, detector, launcher, omniroute, outcome, panes, terminal

MAX_ATTACH = 15_000_000
_IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def save_attachment(cfg, handler):
    """POST /api/attach: store one pasted image under <logs_dir>/../attachments and return its path.

    Content-Length must be a positive number: -1 used to read the socket to its end, past the 15 MB cap, and held the
    thread until the client hung up. Pasted screenshots are private: the folder is 0700 and each file 0600."""
    ext = _IMG_EXT.get((handler.headers.get("Content-Type") or "").split(";")[0].strip().lower())
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return 400, {"error": "invalid Content-Length"}
    if not ext:
        return 400, {"error": "only png, jpeg, gif or webp images can be attached"}
    if n <= 0 or n > MAX_ATTACH:
        return 400, {"error": "image is empty or larger than 15 MB"}
    data = handler.rfile.read(n)
    if len(data) != n:
        return 400, {"error": "the image upload was cut short"}
    d = Path(cfg["logs_dir"]).expanduser().parent / "attachments"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth.private(d)   # a folder made by an older version was 0755
    f = d / f"img-{uuid.uuid4().hex[:16]}{ext}"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    return 200, {"path": str(f)}


def terminal_action(cfg, store, body):
    """POST /api/terminal: show | retile | close-finished | enable | disable | dismiss. Returns (code, payload)."""
    term, act = cfg.get("_terminal"), body.get("action")
    if not term:
        return 400, {"error": "iTerm2 panes aren't set up on this server"}
    if act in ("enable", "disable"):
        term.set_enabled(act == "enable")
        return 200, {"ok": True, "enabled": term.enabled()}
    if act == "dismiss":
        term.dismiss(str(body.get("key") or ""))
        return 200, {"ok": True}
    if act == "show":
        ok, why = term.select_pane(str(body.get("id") or ""))
        return 200, {"ok": ok, "reason": why}
    if act == "retile":
        return 200, {"ok": True, "message": term.retile()}
    if act == "close-finished":
        live = [a["id"] for a in store.all() if a.get("status") in launcher.LIVE]
        n = term.close_finished(live)
        return 200, {"ok": True, "message": f"Closed {n} finished pane(s)." if n else "No finished panes to close."}
    return 400, {"error": f"unknown action {act!r}"}


ACTION_RE = re.compile(r"^[a-z-]{1,32}$")


def hub_action(name, cfg, store, sched):
    """POST /api/action/<name>. Returns (code, payload)."""
    if not ACTION_RE.match(name):
        return 404, {"error": "unknown action"}
    if name in ("pause", "resume"):
        if not sched:
            return 200, {"ok": False, "id": None, "reason": "the queue isn't running on this server"}
        (sched.pause if name == "pause" else sched.resume)()
        return 200, {"ok": True, "id": None, "reason": ""}
    if name == "open-dashboard":   # `jarviee` / Jarviee.app: the grid window, then the dashboard's own Safari window
        threading.Thread(target=open_workspace, args=(cfg,), daemon=True).start()
        return 200, {"ok": True, "id": None, "reason": ""}
    if name == "show-next":
        term = cfg.get("_terminal")
        if not term:
            return 200, {"ok": False, "id": None, "reason": "iTerm2 panes aren't set up on this server"}
        rows = [a for a in store.all() if a.get("source") == "launcher" and term.has_pane(a["id"])]
        waiting = sorted((a for a in rows if a.get("status") == "waiting"), key=lambda a: a.get("updated") or 0)
        running = sorted((a for a in rows if a.get("status") == "running"), key=lambda a: -(a.get("started") or 0))
        pick = (waiting or running or [None])[0]
        if not pick:
            return 200, {"ok": False, "id": None, "reason": "no waiting or running run has a pane"}
        ok, why = term.select_pane(pick["id"])
        return 200, {"ok": ok, "id": pick["id"], "reason": why}
    return 404, {"error": f"unknown action {name!r}"}


APPROVAL_PROMPT_RE = re.compile(
    r"^\s*(?:approve\s+running|confirm\s+|restart\s+(?:hub\.py|python3)|decide\s+whether\s+to|should\s+i\s+|please\s+confirm)\b",
    re.I
)


def probe_tool_quota(tool, model, cfg, store):
    """Run a cheap probe (e.g. agy models or claude --version) to clear quota flag early if recovered."""
    if tool not in cfg.get("tools", {}):
        return False, f"Unknown tool '{tool}'"
    cmd = cfg["tools"][tool]["command"][0]
    if not shutil.which(cmd):
        return False, f"Binary '{cmd}' not found"

    if tool == "omniroute":
        omniroute.reset_cache()
        ok, reason = omniroute.health(cfg)
        if not ok:
            return False, reason or "OmniRoute gateway unavailable"
        store.clear_quota(tool, model)
        return True, "omniroute probe succeeded; quota flag cleared"

    probe_cmd = [cmd, "models"] if tool == "agy" else [cmd, "--version"]
    try:
        r = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=8)
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        det = detector.detect_failure(tool, r.returncode, combined)
        if det and det.kind == detector.QUOTA:
            return False, det.message or "Quota still exhausted"
        store.clear_quota(tool, model)
        return True, f"{tool} probe succeeded; quota flag cleared"
    except Exception as e:
        return False, f"Probe error: {e}"


def close_verdict(store, rid, wait_s=10.0):
    """(ok, why): a pane may close only when its run is really done, or failed and handed to a retry.
    Waits briefly for the hub to settle the run's final status after the runner reports its exit."""
    if store is None:
        return True, ""
    end = time.time() + wait_s
    while True:
        row = store.get(rid) or {}
        status, note = row.get("status"), row.get("activity")
        if status not in ("running", "queued") or time.time() >= end:
            break
        time.sleep(0.25)
    if outcome.may_close(status, note):
        return True, ""
    if status == "waiting":
        return False, "needs you"
    if status == "failed":
        return False, "failed; kept open for you"
    return False, f"status is {status or 'unknown'}"


def open_workspace(cfg):
    """Open Jarviee: the iTerm2 grid of free panes (grid layout), then the dashboard in its own Safari window on the
    secondary screen, last, so the dashboard ends up in front. Never raises."""
    from . import browser
    term = terminal.active(cfg)
    if term and term.grid():
        try:
            term.prefill()
        except Exception as e:   # the dashboard must open anyway
            print(f"[terminal] grid prefill failed: {e}")
    try:
        browser.open_dashboard(cfg)
    except Exception as e:
        print(f"[browser] dashboard window failed: {e}")
        browser._open_default(f"http://127.0.0.1:{cfg['port']}/")   # `open` on macOS, webbrowser elsewhere


def run_event(cfg, body, store=None):
    """POST /api/run-event from hooks/pane_runner.py. `close` closes the pane even after the run is over;
    `free` (grid layout) puts the run's pane back in the free set once its waiter listens."""
    rid = str(body.get("id") or "")
    term = cfg.get("_terminal")
    if body.get("event") == "free":
        if not panes.RUN_RE.match(rid):
            return 400, {"error": "bad run id"}
        return 200, {"ok": True, "freed": bool(term and term.free_pane(rid))}
    if body.get("event") == "close":
        if not panes.RUN_RE.match(rid):
            return 400, {"error": "bad run id"}
        ok, why = (True, "") if body.get("force") else close_verdict(store, rid)
        if not ok:   # needs you, final failure, or still running: the pane stays open
            return 200, {"ok": True, "closed": False, "reason": why}
        if term and term.has_pane(rid):
            if term.release_last(rid):   # the last full screen pane: it exits and iTerm2 closes the window
                return 200, {"ok": True, "closed": True, "exit": True}
            threading.Thread(target=term.close_pane, args=(rid,), daemon=True).start()
        return 200, {"ok": True, "closed": True}
    return panes.event(rid, body)

