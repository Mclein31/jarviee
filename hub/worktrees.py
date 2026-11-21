"""Opt-in git worktrees for Claude runs (R2-12): a run works on its own branch in its own folder, so two agents can
edit the same project without touching each other's files, and its result reaches the owner as one clean commit.

Why `git worktree add` here and not `claude --worktree`: Claude's flag names the folder (`.claude/worktrees/<name>`
inside the project) and the branch (`worktree-<name>`) itself, branches from the repository's default branch on the
remote (not from the project's HEAD) unless a user setting says otherwise, and in `-p` mode leaves the worktree and
its lock behind. Creating the worktree ourselves under a Jarviee-owned folder gives Jarviee a known path and branch
(`jarviee/<run-id>`), the exact start commit, and no lock, so cleanup is deterministic. Claude is simply launched with
that folder as its working directory; hooks report it as `cwd`, but Jarviee keys runs on JARVIEE_RUN_ID and keeps the
row's `workspace` on the project.

Lifecycle: `create` (before the run) -> the run -> `finish` (commit the run's files on its branch, bring them back to
the project's checked-out branch only when that is safe, remove the folder). `sweep` finishes leftovers at startup.

Safety: git is driven with argument lists; nothing is forced, reset, rebased or pushed, and a remote is never
touched. Without a safe bring-back the branch stays and the project's checkout is not touched. Never used for agy.
"""
import os
import subprocess
from pathlib import Path

from . import brand, gitops, snapshot

DEFAULTS = {"enabled": False, "levels": ["complex", "heavy"], "when": "overlap", "dir": "~/.jarviee/worktrees"}
BRANCH_PREFIX = "jarviee/"
BROUGHT = ("fast-forward", "merged", "no_changes")   # the work is in the project's branch (or there was none)
GIT_TIMEOUT = 60
MARKERS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")


def settings(cfg):
    """The `worktree` block with code defaults; an invalid `when` falls back to "overlap"."""
    w = (cfg or {}).get("worktree")
    out = dict(DEFAULTS, levels=list(DEFAULTS["levels"]))
    if isinstance(w, dict):
        out.update({k: v for k, v in w.items() if k in DEFAULTS})
    if out["when"] not in ("overlap", "always"):
        out["when"] = "overlap"
    if not isinstance(out["levels"], (list, tuple)):
        out["levels"] = list(DEFAULTS["levels"])
    return out


def root(cfg):
    return brand.data_path(settings(cfg)["dir"])


def eligible(cfg, p, decision, read_only, live, argv=()):
    """Should this run get a worktree? Only Claude, headless (queue tasks and non-live runs), writable, at an enabled
    level, and under `when: overlap` only when the coordinator would make it wait."""
    s = settings(cfg)
    if not s["enabled"] or (p or {}).get("tool") != "claude" or read_only or live:
        return False
    if (p.get("level") or p.get("tier")) not in s["levels"]:
        return False
    if "--resume" in argv:   # a reply continues a session stored under the folder it started in
        return False
    return s["when"] == "always" or decision == "wait"


def _git(cwd, *args, env=None, timeout=GIT_TIMEOUT):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, errors="replace",
                           env=env or gitops.git_env(), timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def _top(path):
    if not path or not Path(path).is_dir():
        return None
    code, out, _ = _git(path, "rev-parse", "--show-toplevel")
    return out.strip() if code == 0 and out.strip() else None


def create(cfg, run_id, project_dir):
    """Make `<root>/<run-id>` on a new branch `jarviee/<run-id>` from the project's current HEAD.
    {path, workdir, branch, base, top} or None (not a git repo, no commit yet, git refused): the run then goes on
    the normal path."""
    top = _top(project_dir)
    if not top:
        return None
    code, head, _ = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    if code != 0:
        return None
    path = root(cfg) / run_id
    branch = BRANCH_PREFIX + run_id
    try:
        if path.exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    code, _, err = _git(top, "worktree", "add", "-b", branch, str(path), "HEAD")
    if code != 0:
        print(f"[worktree] {run_id}: could not create ({' '.join(err.split())[:200]})")
        return None
    rel = os.path.relpath(os.path.realpath(str(project_dir)), os.path.realpath(top))
    workdir = path if rel == "." else path / rel
    return {"path": str(path), "workdir": str(workdir if workdir.is_dir() else path), "branch": branch,
            "base": head.strip(), "top": top}


# ---------------------------------------------------------------- finishing

def _in_progress(top):
    for marker in MARKERS + ("rebase-merge", "rebase-apply"):
        code, p, _ = _git(top, "rev-parse", "--git-path", marker)
        if code == 0:
            q = Path(p.strip())
            if not q.is_absolute():
                q = Path(top) / q
            if q.exists():
                return True
    return False


def _names(paths, n=3):
    shown = ", ".join(paths[:n])
    return shown + (f" and {len(paths) - n} more" if len(paths) > n else "")


def bring_back(top, branch, base, task):
    """(result, note, sha). Brings `branch` into the project's checked-out branch when that cannot hurt anything:
    a fast-forward, or a clean `merge --no-ff` (checked first with `merge-tree`, aborted if git still finds a
    conflict), with no staged changes and no uncommitted changes to the run's files in the checkout. Otherwise
    nothing in the checkout is touched and the result says why."""
    code, tip, _ = _git(top, "rev-parse", "--verify", "-q", branch + "^{commit}")
    if code != 0:
        return "error", "the run's branch is gone", ""
    tip = tip.strip()
    if tip == base:
        return "no_changes", "", ""
    code, names, _ = _git(top, "diff", "--name-only", "-z", "--no-renames", base, tip)
    files = [n for n in names.split("\0") if n] if code == 0 else []
    if not files:
        return "no_changes", "", ""
    code, cur, _ = _git(top, "symbolic-ref", "-q", "--short", "HEAD")
    if code != 0:
        return "detached", "the project has no branch checked out (detached HEAD)", ""
    if _in_progress(top):
        return "busy", "a merge, rebase, cherry-pick or revert is in progress in the project", ""
    code, st, _ = _git(top, "status", "--porcelain", "-z", "-uall", "--no-renames")
    if code != 0:
        return "error", "git could not read the project's status", ""
    dirty = {e[3:] for e in st.split("\0") if len(e) > 3}
    hit = sorted(set(files) & dirty)
    if hit:
        return "dirty", "the project has uncommitted changes in " + _names(hit), ""
    if _git(top, "merge-base", "--is-ancestor", tip, "HEAD")[0] == 0:
        return "no_changes", "", ""   # already in the branch
    if _git(top, "merge-base", "--is-ancestor", "HEAD", tip)[0] == 0:
        how = "fast-forward"
        code, out, err = _git(top, "merge", "--ff-only", tip)
    else:
        how = "merged"
        if _git(top, "diff", "--cached", "--quiet")[0] != 0:
            return "dirty", "the project has staged changes, which a merge commit would pick up", ""
        code, out, err = _git(top, "merge-tree", "--write-tree", "--name-only", "HEAD", tip)
        if code == 1:
            first = [ln for ln in out.splitlines()[1:] if ln.strip()][:3]
            return "conflict", "it conflicts with the project's branch" + (f" ({_names(first)})" if first else ""), ""
        env = gitops.git_env()
        env["GIT_EDITOR"] = "true"
        code, out, err = _git(top, "merge", "--no-ff", "-m", gitops.clean_commit_message(task), tip, env=env)
        if code != 0:
            _git(top, "merge", "--abort", env=env)   # puts the checkout back exactly as it was
            return "conflict", "it conflicts with the project's branch", ""
    if code != 0:
        return "error", "git could not merge it: " + " ".join((err or out).split())[-150:], ""
    _, sha, _ = _git(top, "rev-parse", "HEAD")
    return how, f"brought into {cur.strip()}", sha.strip()


def _remove(top, path):
    """Remove the worktree folder without force. (ok, why)."""
    if not Path(path).exists():
        _git(top, "worktree", "prune")
        return True, ""
    code, out, err = _git(top, "worktree", "remove", str(path))
    if code != 0 and "locked" in err.lower():
        _git(top, "worktree", "unlock", str(path))
        code, out, err = _git(top, "worktree", "remove", str(path))
    _git(top, "worktree", "prune")
    if code != 0:
        return False, " ".join(err.split())[:200]
    return True, ""


def finish(cfg, wt, task, status, log_path=None, bring=True):
    """Close a run's worktree. `wt` = {path, branch, base, top}. Commits the files the run left uncommitted on its
    branch (every status, so no work is lost with the folder), brings the branch back when the run is done and it is
    safe, then removes the folder. {result, note, sha}: result is one of fast-forward, merged, no_changes (all fine),
    conflict, dirty, detached, busy, not_done, error, commit_failed (work stays on the branch), folder_left."""
    path, branch, base, top = wt["path"], wt["branch"], wt["base"], wt["top"]
    res = {"result": "", "note": "", "sha": ""}
    if Path(path).is_dir():
        snap = snapshot.take(path)
        files = sorted((snap or {}).get("files") or {})
        if files and not gitops.auto_commit(path, task, log_path=log_path, files=files, verified=True):
            return {"result": "commit_failed", "sha": "",
                    "note": f"its files could not be committed on {branch}; the folder was left at {path}"}
    if status == "done" and bring:
        result, note, sha = bring_back(top, branch, base, task)
        res = {"result": result, "note": note, "sha": sha}
    elif status != "done":
        res = {"result": "not_done", "note": f"the run did not finish cleanly ({status})", "sha": ""}
    if res["result"] in BROUGHT:
        ok, why = _remove(top, path)
        if ok:
            _git(top, "branch", "-d", branch)   # safe delete: only when fully merged
        else:
            res["note"] = (res["note"] + "; " if res["note"] else "") + f"the folder could not be removed ({why})"
        return res
    ok, why = _remove(top, path)
    code, tip, _ = _git(top, "rev-parse", "--verify", "-q", branch + "^{commit}")
    if code == 0 and tip.strip() == base and ok:
        _git(top, "branch", "-d", branch)   # nothing on it: no reason to keep it
    if not ok:
        res["note"] += f"; the folder could not be removed ({why})"
    return res


def describe(res, branch):
    """The sentence appended to a run's note when its work did not reach the project's branch."""
    if res["result"] in BROUGHT or res["result"] == "not_done":
        return ""
    return f" (changes left on branch {branch}: {res['note']})"


# ---------------------------------------------------------------- startup sweep

def _common_top(path):
    code, out, _ = _git(path, "rev-parse", "--git-common-dir")
    if code != 0 or not out.strip():
        return None
    p = Path(out.strip())
    if not p.is_absolute():
        p = Path(path) / p
    return str(p.resolve().parent)


def sweep(cfg, store, live=lambda run_id: False):
    """Finish worktrees under the Jarviee folder whose run is over (done, failed or stopped, or the row or process
    is gone); a run that is still live is left alone. Returns the run ids cleaned."""
    d = root(cfg)
    cleaned = []
    try:
        entries = sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
    except OSError:
        return cleaned
    from . import orphans
    for p in entries:
        rid = p.name
        row = store.get(rid)
        if live(rid):
            continue
        if row and row.get("status") in ("running", "waiting", "queued") and orphans.run_alive(row, nopid_grace=60):
            continue
        top = _common_top(p)
        if not top or not (p / ".git").exists():
            continue
        status = (row or {}).get("status") if (row or {}).get("status") in ("done", "failed", "stopped") else "failed"
        done_before = (row or {}).get("worktree_result") in BROUGHT
        base = (row or {}).get("base_head") or ""
        if not base:
            _, mb, _ = _git(top, "merge-base", BRANCH_PREFIX + rid, "HEAD")
            base = mb.strip()
        wt = {"path": str(p), "branch": BRANCH_PREFIX + rid, "base": base, "top": top}
        res = finish(cfg, wt, (row or {}).get("task") or "", status, log_path=(row or {}).get("log_path"),
                     bring=not done_before)
        if row:
            store.upsert(rid, worktree_result=res["result"], worktree_note=res["note"])
        cleaned.append(rid)
    return cleaned
