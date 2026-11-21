"""Per-run file snapshots (R2-8): which files did one run change?

take() records HEAD plus a content digest of every dirty file in a work tree; changed() compares two
snapshots. Nothing in the repo is touched (no stash, no branch, no index writes), so a snapshot is safe next to
other agents working in the same folder. Files another agent edits while the run is going are attributed to the
run: best effort, but far narrower than `git add -A`."""
import hashlib
import subprocess
from pathlib import Path

MAX_FILES = 2000
SKIP_PREFIXES = (".brain/.locks",)


def _git(workdir, *args, timeout=10):
    r = subprocess.run(["git", *args], cwd=workdir, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout


def _digest(path):
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "missing"   # deleted (or unreadable): still a distinct state


def take(workdir):
    """{'head': sha or '', 'files': {path: digest}} for a git work tree, or None when it isn't one."""
    if not workdir or not Path(workdir).is_dir():
        return None
    try:
        code, out = _git(workdir, "rev-parse", "--show-toplevel")
        if code != 0:
            return None
        top = out.decode("utf-8", "replace").strip()
        code, head = _git(workdir, "rev-parse", "--verify", "-q", "HEAD")
        code, st = _git(workdir, "status", "--porcelain", "-z", "-uall", "--no-renames")
        if code != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    files = {}
    for entry in st.decode("utf-8", "replace").split("\0"):
        if len(entry) < 4:
            continue
        rel = entry[3:]
        if rel.startswith(SKIP_PREFIXES):
            continue
        files[rel] = _digest(Path(top) / rel)
        if len(files) >= MAX_FILES:
            break
    return {"head": head.decode().strip() if head else "", "files": files, "top": top}


def changed(before, after, workdir=None):
    """Sorted repo-relative paths whose state differs between two snapshots (including files committed
    in between, when HEAD moved). [] when either snapshot is missing."""
    if not before or not after:
        return []
    out = {p for p in set(before["files"]) | set(after["files"])
           if before["files"].get(p) != after["files"].get(p)}
    if before["head"] and after["head"] and before["head"] != after["head"]:
        try:
            code, names = _git(workdir or after["top"], "diff", "--name-only", "-z", before["head"], after["head"])
            if code == 0:
                out |= {n for n in names.decode("utf-8", "replace").split("\0")
                        if n and not n.startswith(SKIP_PREFIXES)}
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(out)[:MAX_FILES]
