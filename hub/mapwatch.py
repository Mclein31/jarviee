"""Keep project maps current: when a project's code moves on, an agent updates its .brain/FLOW.json.

Every `check_s` the watcher looks at each project whose map is committed in git. Source files changed in commits
after the map (or after the last update check) mean the map is behind. When the project has been quiet for
`settle_s` (no new commit, no agent running there), it launches one headless update run (`projmap.update_brief`).
When that run ends, Jarviee validates the file: a broken map is put back from the backup taken at launch, a good one
is committed (only that file). `cooldown_s` per project and `max_per_day` overall keep it from burning quota.
State lives in ~/.jarviee/map-watch.json. Never raises into the server.
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

from . import brand, obsidian, projmap

DEFAULTS = {"auto_update": True, "check_s": 300, "settle_s": 600, "cooldown_s": 10800, "max_per_day": 8}
SOURCE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".html", ".py",
              ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift", ".dart", ".sql", ".prisma", ".graphql", ".toml", ".yaml", ".yml"}
SKIP_DIRS = {".brain", "node_modules", "dist", "build", ".next", ".nuxt", "out", "coverage", "vendor", "Pods", ".venv", "venv",
             "__pycache__", ".turbo", ".vercel", ".wrangler", "legacy"}
SKIP_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "poetry.lock", "Cargo.lock"}
LIVE = ("running", "waiting", "queued")
_lock = threading.Lock()
_status = {}          # project name -> status dict for the dashboard


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("maps") or {})}


def _state_file(cfg):
    if (cfg or {}).get("map_watch_path"):
        return Path(cfg["map_watch_path"])
    return brand.home() / "map-watch.json"


def _load(cfg):
    try:
        data = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cfg, data):
    f = _state_file(cfg)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


def is_source(path):
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]) or parts[-1] in SKIP_FILES:
        return False
    return os.path.splitext(parts[-1])[1].lower() in SOURCE_EXT


def behind(project_dir, since_head=""):
    """(base commit, changed source files, last commit time) for a project's map; base '' when it can't tell."""
    rel = f".brain/{projmap.FLOW_FILE}"
    code, flow_commit, _ = projmap._git(project_dir, "log", "-1", "--format=%H", "--", rel)
    if code or not flow_commit:
        return "", [], 0
    base = flow_commit
    if since_head and since_head != flow_commit and projmap._git(project_dir, "merge-base", "--is-ancestor", flow_commit, since_head)[0] == 0:
        base = since_head   # an update ran after the map's commit and found nothing to change: start from there
    code, out, _ = projmap._git(project_dir, "diff", "--name-only", base, "HEAD")
    changed = [f for f in out.splitlines() if f and is_source(f)] if not code else []
    last = projmap._git(project_dir, "log", "-1", "--format=%ct")[1]
    return base, changed, int(last) if last.isdigit() else 0


def _busy(store, project_dir):
    root = project_dir.resolve()
    for a in store.all():
        ws = a.get("workspace")
        if a.get("status") in LIVE and ws:
            try:
                w = Path(ws).resolve()
            except (OSError, ValueError):
                continue
            if w == root or root in w.parents:
                return a
    return None


def status(name):
    return dict(_status.get(name) or {})


def _set(name, **kw):
    _status[name] = {**_status.get(name, {}), **kw, "checked": time.time()}


def launch(cfg, store, name, force=False, now=None):
    """(ok, message, run_id) after starting an update run for one project's map."""
    now = now or time.time()
    p = obsidian.find_project(cfg, name)
    if not p:
        return False, f"no project '{name}'", None
    flow = p / obsidian._b(cfg)["project_brain_dir"] / projmap.FLOW_FILE
    if not flow.is_file():
        return False, "this project has no map yet; use Map this project", None
    with _lock:
        state = _load(cfg)
        ps = state.setdefault(p.name, {})
        run = ps.get("run")
        if run and (store.get(run) or {}).get("status") in LIVE:
            return False, "an update is already running", run
        base, changed, _ = behind(p, ps.get("head", ""))
        if not changed and not force:
            return False, "the map is up to date", None
        head = projmap._git(p, "rev-parse", "HEAD")[1]
        backup = _state_file(cfg).parent / "map-backups" / f"{p.name}.json"
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(flow, backup)
        except OSError as e:
            return False, f"couldn't back up the map: {e}", None
        task = projmap.update_brief(p.name, base or head, changed or ["(no source changes detected; check the whole map against the code)"])
        ps.update(run=None, started=now, head_at_launch=head, result="starting")
        state.setdefault("_runs", []).append(now)
        _save(cfg, state)
    try:
        from . import launcher   # late: launcher imports a lot
        rid, _p = launcher.launch(task, cfg, store, None, "complex", p.name, None,
                                  on_finish=lambda r, st, code: finish(cfg, p, r, st, backup, head, "update"), log_runs=False)
    except Exception as e:   # noqa: BLE001 - never take the watcher down
        _set(p.name, state="error", note=f"couldn't start the update: {e}"[:200])
        return False, f"couldn't start the update: {e}", None
    with _lock:
        state = _load(cfg)
        state.setdefault(p.name, {}).update(run=rid, result="running")
        _save(cfg, state)
    _set(p.name, state="updating", run=rid, changed=len(changed))
    return True, f"Updating the {p.name} map: an agent is reading {len(changed)} changed file{'s' if len(changed) != 1 else ''}.", rid


def finish(cfg, project_dir, run_id, run_status, backup, head, mode):
    """Run callback: validate the map, put the backup back if it's broken, commit it if it changed."""
    flow = project_dir / obsidian._b(cfg)["project_brain_dir"] / projmap.FLOW_FILE
    name, result, note = project_dir.name, "", ""
    try:
        projmap.validate(json.loads(flow.read_text(encoding="utf-8")))
        ok = True
    except (OSError, ValueError) as e:
        ok, note = False, str(e)[:160]
    if not ok:
        if backup and Path(backup).is_file():
            shutil.copyfile(backup, flow)
            result, note = "invalid", f"the agent wrote a broken map ({note}); the previous one was put back"
        else:
            result = "invalid"
    else:
        same = backup and Path(backup).is_file() and Path(backup).read_bytes() == flow.read_bytes()
        if same:
            result, note = "unchanged", "nothing in the map needed to change"
        else:
            msg = "docs: update project map (.brain/FLOW.json)" if mode == "update" else "docs: project map (.brain/FLOW.json) for Jarviee Project Maps"
            committed, info = projmap.commit_flow(project_dir, msg)
            result, note = ("updated", f"committed {info}") if committed else ("updated", f"not committed: {info}")
    if run_status != "done" and result != "updated":
        result = result or "failed"
        note = note or f"the update run ended {run_status}"
    with _lock:
        state = _load(cfg)
        ps = state.setdefault(name, {})
        ps.update(run=None, finished=time.time(), result=result, note=note, run_last=run_id)
        if result in ("updated", "unchanged") and head:
            ps["head"] = head        # checked up to here: only newer commits count as behind
        _save(cfg, state)
    _set(name, state="current" if result in ("updated", "unchanged") else "error", run=None, result=result, note=note)


def check(cfg, store, now=None):
    """One pass over the projects: refresh the dashboard status, launch updates where due. Returns launched names."""
    now = now or time.time()
    st = settings(cfg)
    state = _load(cfg)
    runs_today = [t for t in state.get("_runs", []) if now - t < 86400]
    launched = []
    bdir = obsidian._b(cfg)["project_brain_dir"]
    for p in obsidian._project_dirs(cfg):
        if not (p / bdir / projmap.FLOW_FILE).is_file():
            continue
        if not (p / ".git").exists():
            _set(p.name, state="untracked", note="not a git repo, so changes can't be tracked")
            continue
        ps = state.get(p.name, {})
        run = ps.get("run")
        if run and (store.get(run) or {}).get("status") in LIVE:
            _set(p.name, state="updating", run=run)
            continue
        base, changed, last_commit = behind(p, ps.get("head", ""))
        if not base:
            _set(p.name, state="uncommitted", note="the map isn't committed yet", changed=0)
            continue
        if not changed:
            _set(p.name, state="current", changed=0, result=ps.get("result"), note=ps.get("note", ""), finished=ps.get("finished"))
            continue
        _set(p.name, state="behind", changed=len(changed), files=changed[:8], result=ps.get("result"), note=ps.get("note", ""))
        if not st["auto_update"]:
            continue
        why = ("settling" if now - last_commit < st["settle_s"] else
               "busy" if _busy(store, p) else
               "cooldown" if now - (ps.get("started") or 0) < st["cooldown_s"] else
               "daily cap" if len(runs_today) >= st["max_per_day"] else "")
        if why:
            _set(p.name, waiting=why)
            continue
        ok, _msg, _rid = launch(cfg, store, p.name, now=now)
        if ok:
            launched.append(p.name)
            runs_today.append(now)
            _set(p.name, waiting="")
    return launched


class MapWatcher(threading.Thread):
    def __init__(self, cfg, store):
        super().__init__(daemon=True, name="jarviee-mapwatch")
        self.cfg, self.store, self._stop = cfg, store, threading.Event()

    def run(self):
        if self._stop.wait(45):          # let the server settle first
            return
        while not self._stop.is_set():
            try:
                check(self.cfg, self.store)
            except Exception as e:   # noqa: BLE001
                print(f"[mapwatch] {e}")
            self._stop.wait(max(60, int(settings(self.cfg)["check_s"])))

    def stop(self):
        self._stop.set()
