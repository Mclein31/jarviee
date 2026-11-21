"""Review inbox: finished launched runs that changed files wait here until the owner decides.

A run is "to review" when it was launched from Jarviee (`source = launcher`), is done or failed, recorded a
changed-file list (hub/snapshot.py) and has no decision yet (`agents.reviewed = 0`). Decisions:
  accept   reviewed = 1    the owner looked at it and keeps the work
  dismiss  reviewed = 2    nothing to judge
  revert   reviewed = 3    `git revert` of the run's own commit, only when provably safe (see revert_check)
A deterministic test gate is opt-in per project (check_command): no LLM reviewer, no guessed commands.

Git is only ever read, except revert, which makes one new commit with `git revert --no-edit`: never reset,
never history rewriting, never force, never a remote. Everything shells out to `git` with an argument list."""
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import board_api, brand, gitops, obsidian, worktrees
from .titles import make_title

PENDING, ACCEPTED, DISMISSED, REVERTED = 0, 1, 2, 3
DONE = ("done", "failed")
MAX_ITEMS = 40
MAX_AGE_S = 30 * 86400
GIT_TIMEOUT = 15
CHECK_TIMEOUT_S = 300
OUTPUT_TAIL = 3000


class InboxError(ValueError):
    """A refusal with a stable `code` (the UI keys on it) and a sentence for the owner."""

    def __init__(self, message, code="refused"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- git helpers

def _git(top, *args, env=None, timeout=GIT_TIMEOUT):
    r = subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True, env=env, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _specs(files):
    return [f":(top,literal){f}" for f in files]


class Repo:
    """What one request needs to know about a work tree, read once: top, HEAD and the dirty paths."""

    def __init__(self, workdir):
        self.top = self.head = None
        self.dirty = set()
        self.error = ""
        if not workdir or not Path(workdir).is_dir():
            self.error = "the run's folder is gone"
            return
        try:
            code, out, _ = _git(workdir, "rev-parse", "--show-toplevel")
            if code != 0:
                self.error = "the folder is not a git repository"
                return
            self.top = out.strip()
            code, out, _ = _git(self.top, "rev-parse", "--verify", "-q", "HEAD")
            self.head = out.strip() if code == 0 else None
            code, out, _ = _git(self.top, "status", "--porcelain", "-z", "-uall", "--no-renames")
            if code == 0:
                self.dirty = {e[3:] for e in out.split("\0") if len(e) > 3}
        except (OSError, subprocess.SubprocessError) as e:
            self.top, self.error = None, f"git failed: {e}"


class Repos:
    def __init__(self):
        self._by = {}

    def get(self, workdir):
        key = str(workdir or "")
        if key not in self._by:
            self._by[key] = Repo(workdir)
        return self._by[key]


def _workdir(row, cfg):
    w = row.get("workspace") or row.get("cwd")
    if not w and row.get("project"):
        p = obsidian.find_project(cfg, row["project"])
        w = str(p) if p else None
    return w


# ---------------------------------------------------------------- what a run changed

def _line_count(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return 0, True
    if b"\0" in data[:8000]:
        return 0, True
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), False


def left_on_branch(row):
    """The branch a worktree run's work was left on (hub/worktrees.py), or ''. A run whose work was brought back
    (fast-forward or merge) is not special: its commit is in the checked-out branch like any other."""
    if row.get("worktree_branch") and row.get("worktree_result") not in worktrees.BROUGHT + ("",):
        return row["worktree_branch"]
    return ""


def file_stats(repo, row):
    """[{path, added, removed, binary}] for the run's recorded files against the HEAD it started from
    (against its branch, when the work was left there)."""
    files = row.get("files") or []
    out = {}
    branch = left_on_branch(row)
    if repo.top and files:
        base = row.get("base_head") or repo.head or "HEAD"
        try:
            code, text, _ = _git(repo.top, "diff", "--numstat", "-z", "--no-renames", base, *([branch] if branch else []),
                                 "--", *_specs(files))
        except (OSError, subprocess.SubprocessError):
            code, text = 1, ""
        if code == 0:
            for entry in text.split("\0"):
                parts = entry.split("\t", 2)
                if len(parts) == 3:
                    a, r, path = parts
                    out[path] = {"path": path, "added": 0 if a == "-" else int(a), "removed": 0 if r == "-" else int(r),
                                 "binary": a == "-"}
    for f in files:
        if f in out:
            continue
        added, binary = 0, False
        full = None if branch else Path(repo.top or ".") / f
        if repo.top and full and full.is_file():
            try:
                tracked = _git(repo.top, "ls-files", "--error-unmatch", "--", *_specs([f]))[0] == 0
            except (OSError, subprocess.SubprocessError):
                tracked = True
            if not tracked:
                added, binary = _line_count(full)
        out[f] = {"path": f, "added": added, "removed": 0, "binary": binary}
    return [out[f] for f in files]


def run_commits(repo, row, cfg=None, anywhere=False):
    """The commit(s) the run's auto-commit made: commits in base_head..HEAD whose subject is the run's own cleaned
    message and that touched only the run's files. [] when it did not commit (or the start point is unknown).
    `anywhere` also looks on the other local branches (to tell "left this branch" from "never committed")."""
    files, base = row.get("files") or [], row.get("base_head")
    if not (repo.top and repo.head and files and base):
        return []
    subject = gitops.clean_commit_message(row.get("task"))
    try:
        revs = ["--branches", f"^{base}"] if anywhere else [f"{base}..HEAD"]
        code, text, _ = _git(repo.top, "log", "--no-merges", "--format=%H%x1f%s", "--reverse", *revs,
                             "--", *_specs(files))
        if code != 0:
            return []
        found = []
        for line in text.splitlines():
            sha, _, subj = line.partition("\x1f")
            if not sha or subj != subject:
                continue
            code, names, _ = _git(repo.top, "show", "--no-renames", "--name-only", "--format=", "-z", sha)
            touched = [n for n in names.split("\0") if n]
            if code == 0 and touched and set(touched) <= set(files):
                found.append({"sha": sha, "short": sha[:8], "subject": subj, "files": touched})
        return found
    except (OSError, subprocess.SubprocessError):
        return []


# ---------------------------------------------------------------- revert

def _refuse(code, message):
    return {"ok": False, "code": code, "reason": message}


def revert_check(repo, row, commits, elsewhere=None):
    """{ok, code, reason, sha}. ok only when a plain `git revert` of the run's commit cannot hurt other work."""
    if row.get("source") != "launcher":
        return _refuse("not_launched", "Only runs launched from Jarviee can be reverted here.")
    if not repo.top:
        return _refuse("no_repo", f"Cannot revert: {repo.error or 'not a git repository'}.")
    if not row.get("base_head"):
        return _refuse("no_base", "Cannot revert: the commit this run started from was not recorded.")
    if not commits:
        if left_on_branch(row):
            return _refuse("on_branch", f"Nothing to revert: the run's changes were never brought into this branch "
                           f"(they are on {row['worktree_branch']}: {row.get('worktree_note') or 'see the run'}).")
        if elsewhere:
            return _refuse("unreachable", "Cannot revert: the run's commit is not on the checked-out branch "
                           "(it is on another local branch).")
        if row.get("status") == "failed":
            return _refuse("no_commit", "Nothing to revert: a failed run is never committed. Its changes are "
                           "uncommitted in the working tree.")
        return _refuse("no_commit", "Nothing to revert: this run did not auto-commit its changes.")
    if len(commits) > 1:
        return _refuse("many_commits", f"Cannot revert in one step: this run made {len(commits)} commits.")
    c = commits[0]
    try:
        if _git(repo.top, "cat-file", "-e", c["sha"] + "^{commit}")[0] != 0:
            return _refuse("unreachable", "Cannot revert: the run's commit no longer exists in this repository.")
        if _git(repo.top, "merge-base", "--is-ancestor", c["sha"], "HEAD")[0] != 0:
            return _refuse("unreachable", "Cannot revert: the run's commit is not on the checked-out branch "
                           "(another branch is checked out, or history was rewritten).")
        touched = c["files"]
        dirty = sorted(set(touched) & repo.dirty)
        if dirty:
            return _refuse("dirty", "Cannot revert: uncommitted changes in " + _names(dirty) + ".")
        code, later, _ = _git(repo.top, "log", "--format=%h %s", f"{c['sha']}..HEAD", "--", *_specs(touched))
        if code != 0:
            return _refuse("git", "Cannot revert: git could not list later commits.")
        if later.strip():
            first = later.strip().splitlines()[0]
            n = len(later.strip().splitlines())
            return _refuse("later_commit", f"Cannot revert: {n} later commit{'s' if n > 1 else ''} touched the same "
                           f"files (latest: {first}).")
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            code, path, _ = _git(repo.top, "rev-parse", "--git-path", marker)
            p = Path(path.strip())
            if not p.is_absolute():
                p = Path(repo.top) / p
            if code == 0 and p.exists():
                return _refuse("in_progress", "Cannot revert: a merge, rebase, cherry-pick or revert is in progress.")
    except (OSError, subprocess.SubprocessError) as e:
        return _refuse("git", f"Cannot revert: git failed ({e}).")
    return {"ok": True, "code": "ok", "reason": "", "sha": c["sha"]}


def _names(paths, n=3):
    shown = ", ".join(paths[:n])
    return shown + (f" and {len(paths) - n} more" if len(paths) > n else "")


_revert_lock = threading.Lock()


def revert(cfg, store, rid):
    """Revert the run's commit (a new commit) when revert_check allows it. Returns the new commit's sha.
    One at a time: a double submit waits, then finds the run already reviewed and is refused."""
    with _revert_lock:
        return _revert_locked(cfg, store, rid)


def _revert_locked(cfg, store, rid):
    row = _reviewable(store, rid)   # re-read inside the lock: `reviewed` may have changed while this one waited
    repo = Repo(_workdir(row, cfg))
    commits = run_commits(repo, row, cfg)
    chk = revert_check(repo, row, commits, None if commits else run_commits(repo, row, cfg, anywhere=True))
    if not chk["ok"]:
        raise InboxError(chk["reason"], chk["code"])
    env = gitops.git_env()
    env["GIT_EDITOR"] = "true"
    try:
        code, out, err = _git(repo.top, "revert", "--no-edit", chk["sha"], env=env, timeout=30)
        if code != 0:
            try:   # leave the tree as it was: a half-done revert would block everything after it
                _git(repo.top, "revert", "--abort", env=env)
            except (OSError, subprocess.SubprocessError):
                pass
            tail = " ".join((err or out).split())[-300:]
            raise InboxError(f"git could not revert it ({tail}). Nothing was changed.", "git")
        _, new, _ = _git(repo.top, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as e:
        raise InboxError(f"git failed: {e}", "git")
    store.upsert(rid, reviewed=REVERTED, reviewed_at=time.time(), revert_sha=new.strip())
    invalidate(rid)
    return new.strip(), chk["sha"]


# ---------------------------------------------------------------- the test gate

def check_command(cfg, project, workdir=None):
    """(argv list, display string, source) naming the project's test command, or (None, '', ''). `source` is
    "config" (the Jarviee config, `checks.<project>`, the owner's own word) or "file" (the first non-comment line of
    `<project>/.brain/CHECK_COMMAND`, which any run can write: only a suggestion until the owner confirms it).
    Nothing is guessed. The command runs as an argument list (shlex), never through a shell."""
    raw, source = "", ""
    checks = cfg.get("checks") or {}
    v = checks.get(project) if isinstance(checks, dict) and project else None
    if isinstance(v, str):
        raw = v.strip()
    elif isinstance(v, list) and all(isinstance(x, str) for x in v) and v:
        raw = " ".join(shlex.quote(x) for x in v)
    if raw:
        source = "config"
    if not raw and project:
        p = obsidian.find_project(cfg, project)
        if p:
            try:
                for line in (p / ".brain" / "CHECK_COMMAND").read_text(encoding="utf-8").splitlines():
                    if line.strip() and not line.strip().startswith("#"):
                        raw, source = line.strip(), "file"
                        break
            except OSError:
                pass
    if not raw:
        return None, "", ""
    try:
        return shlex.split(raw), raw, source
    except ValueError:
        return None, "", ""


def is_trusted(store, project, shown, source):
    """A config command is the owner's; a file command runs only after the owner confirmed exactly this text."""
    return source == "config" or (source == "file" and store.trusted_check(project) == shown)


def _check_dir(cfg, row):
    p = obsidian.find_project(cfg, row.get("project")) if row.get("project") else None
    return str(p) if p else _workdir(row, cfg)


def _clean_env():
    """The server's environment without anything that carries the hub's own names or a token."""
    drop = (brand.ENV_PREFIX, brand.LEGACY_ENV_PREFIX)
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop) and "TOKEN" not in k.upper()}
    env["CI"] = "1"
    return env


_check_lock = threading.Lock()
_checking = set()


def run_check(cfg, store, rid, trust_command=None):
    row = _reviewable(store, rid)
    project = row.get("project")
    argv, shown, source = check_command(cfg, project)
    if not argv:
        raise InboxError(f"No test command is configured for {project or 'this project'}. Add one line to "
                         f"<project>/.brain/CHECK_COMMAND or set checks.<project> in the Jarviee config.", "no_command")
    if not is_trusted(store, project, shown, source):
        if trust_command is None or str(trust_command).strip() != shown:
            raise InboxError(f"The command in .brain/CHECK_COMMAND has not been confirmed: {shown}", "untrusted")
        store.trust_check(project, shown)
    cwd = _check_dir(cfg, row)
    if not cwd or not Path(cwd).is_dir():
        raise InboxError("The project folder is gone, so there is nothing to run the checks in.", "no_dir")
    with _check_lock:
        if rid in _checking:
            raise InboxError("The checks are already running for this run.", "busy")
        _checking.add(rid)
    try:
        return _run_check_locked(cfg, store, rid, argv, shown, cwd)
    finally:
        with _check_lock:
            _checking.discard(rid)


def _run_check_locked(cfg, store, rid, argv, shown, cwd):
    timeout = float((cfg.get("checks") or {}).get("timeout_s", CHECK_TIMEOUT_S)) if isinstance(cfg.get("checks"), dict) \
        else CHECK_TIMEOUT_S
    t0 = time.time()
    store.upsert(rid, check_status="running", check_output="", check_at=t0)
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", stdin=subprocess.DEVNULL, env=_clean_env(), start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            status = "pass" if proc.returncode == 0 else "fail"
            out = (stdout or "") + (("\n" + stderr) if stderr else "")
            out = f"$ {shown}\n{out}".rstrip() + f"\n[exit {proc.returncode}]"
        except subprocess.TimeoutExpired:
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if killpg is not None and sigkill is not None:
                try:   # the whole group: the test runner's own children must not outlive the timeout
                    killpg(proc.pid, sigkill)
                except OSError:
                    proc.kill()
            else:
                proc.kill()
            try:
                stdout, _ = proc.communicate(timeout=5)
            except subprocess.SubprocessError:
                stdout = ""
            status, out = "fail", f"$ {shown}\n[timed out after {int(timeout)} s]"
            if stdout:
                out = f"$ {shown}\n{stdout}\n[timed out after {int(timeout)} s]"
    except (OSError, ValueError) as e:
        status, out = "fail", f"$ {shown}\n[could not run: {e}]"
    out = out[-OUTPUT_TAIL:]
    store.upsert(rid, check_status=status, check_output=out, check_at=time.time())
    invalidate(rid)
    return {"status": status, "output": out, "at": time.time(), "command": shown, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- listing and decisions

def _is_candidate(row, now):
    if row.get("source") != "launcher" or row.get("status") not in DONE or not row.get("files"):
        return False
    if int(row.get("reviewed") or 0) != PENDING:
        return False
    if str(row.get("activity") or "").startswith("retrying"):   # the retry is the run to judge
        return False
    return (now - float(row.get("ended") or row.get("updated") or 0)) < MAX_AGE_S


def _reviewable(store, rid):
    row = store.get(str(rid or ""))
    if not row:
        raise InboxError("no such run", "no_run")
    if not _is_candidate(row, time.time()):
        raise InboxError("this run is not waiting for review", "not_pending")
    return row


FACTS_TTL_S = 60.0
_facts = {}   # run id -> (key, computed at, {commits, stats, chk, anywhere})
_facts_lock = threading.Lock()


def invalidate(rid=None):
    """Forget the cached git facts of a run (accept / dismiss / revert / check), or of all runs."""
    with _facts_lock:
        if rid is None:
            _facts.clear()
        else:
            _facts.pop(rid, None)


def _facts_key(repo, row):
    files = tuple(row.get("files") or [])
    mt = []
    for f in files:
        try:
            mt.append(os.stat(os.path.join(repo.top or "", f)).st_mtime_ns)
        except OSError:
            mt.append(None)
    return (row["id"], repo.head, row.get("base_head"), files, tuple(mt), row.get("status"))


def _git_facts(cfg, row, repo, detail):
    """commits, per-file stats and the revert verdict of one run: about ten git commands, so cached for a minute per
    (run, HEAD, files, file mtimes). The "on another branch?" lookup runs only for the run being opened."""
    key, now = _facts_key(repo, row), time.time()
    with _facts_lock:
        hit = _facts.get(row["id"])
    if hit and hit[0] == key and now - hit[1] < FACTS_TTL_S and (hit[2]["anywhere"] or not detail):
        return hit[2]
    commits = run_commits(repo, row, cfg)
    elsewhere = run_commits(repo, row, cfg, anywhere=True) if (detail and not commits) else None
    facts = {"commits": commits, "stats": file_stats(repo, row), "anywhere": bool(detail or commits),
             "chk": revert_check(repo, row, commits, elsewhere)}
    with _facts_lock:
        _facts[row["id"]] = (key, now, facts)
        if len(_facts) > 4 * MAX_ITEMS:
            for k in sorted(_facts, key=lambda k: _facts[k][1])[:MAX_ITEMS]:
                _facts.pop(k, None)
    return facts


def item(cfg, row, repos, store, detail=False):
    repo = repos.get(_workdir(row, cfg))
    facts = _git_facts(cfg, row, repo, detail)
    commits, stats, chk = facts["commits"], facts["stats"], facts["chk"]
    argv, shown, source = check_command(cfg, row.get("project"))
    end = row.get("ended") or row.get("updated")
    return {
        "id": row["id"],
        "title": row.get("title") or make_title(row.get("task") or "") or "Untitled task",
        "project": row.get("project") or "",
        "tool": row.get("tool") or "", "model": row.get("model") or "", "level": row.get("tier") or "",
        "effort": row.get("effort") or "",
        "status": row.get("status"), "activity": row.get("activity") or "",
        "started": row.get("started"), "ended": end,
        "duration": round(end - row["started"], 1) if end and row.get("started") else None,
        "files": stats,
        "added": sum(f["added"] for f in stats), "removed": sum(f["removed"] for f in stats),
        "committed": bool(commits),
        "worktree": {"branch": row.get("worktree_branch") or "", "result": row.get("worktree_result") or "",
                     "note": row.get("worktree_note") or ""} if row.get("worktree_branch") else None,
        "commits": [{"sha": c["sha"], "short": c["short"], "subject": c["subject"]} for c in commits],
        "revert": {"ok": chk["ok"], "code": chk["code"], "reason": chk["reason"]},
        "check": {"configured": bool(argv), "command": shown, "status": row.get("check_status") or "",
                  "output": row.get("check_output") or "", "at": row.get("check_at") or None,
                  **({"source": source, "trusted": is_trusted(store, row.get("project"), shown, source)} if argv else {})},
    }


def list_items(cfg, store, open_id=None):
    """The runs awaiting review, newest first. `open_id` is the run being opened: only it pays for the full lookup."""
    now = time.time()
    rows = [r for r in store.finished_launched() if _is_candidate(r, now)]
    rows.sort(key=lambda r: r.get("ended") or r.get("updated") or 0, reverse=True)
    repos = Repos()
    return [item(cfg, r, repos, store, detail=(r["id"] == open_id)) for r in rows[:MAX_ITEMS]]


def decide(store, rid, state, cfg=None):
    """Mark a run accepted or dismissed. Returns the stored state. Passing `cfg` lets an accept finish the run's board card."""
    _reviewable(store, rid)
    store.upsert(rid, reviewed=state, reviewed_at=time.time())
    invalidate(rid)
    if cfg is not None and state == ACCEPTED:
        board_api.notify_accept(cfg, rid)   # the linked Project Board card moves to done (never raises)
    return state
