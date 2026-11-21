"""Git around a run: the commit a verified run gets, and the diff of what one run changed."""
import os
import re
import subprocess
from pathlib import Path

from . import obsidian, snapshot, worktrees

AI_TERMS_RE = re.compile(
    r"\b(?:claude(?:\s+code)?|antigravity|gemini|agy|ai\s+assistant|ai\s+agent|anthropic|openai|copilot)\b",
    re.I,
)
CONVENTIONAL_RE = re.compile(r"^(feat|fix|chore|docs|refactor|test|style|perf|ci|build)(\([^)]+\))?:\s*", re.I)


def clean_commit_message(task):
    """Build a conventional commit subject with zero AI co-author or tool references."""
    lines = [
        ln.strip() for ln in str(task or "").splitlines()
        if ln.strip() and not re.match(r"^(co-authored-by|generated with|signed-off-by)\b", ln.strip(), re.I)
    ]
    subject = lines[0] if lines else ""
    subject = re.sub(r"(?i)\bco-authored-by:.*$", "", subject).strip()
    subject = AI_TERMS_RE.sub("", subject)
    subject = re.sub(r"\s{2,}", " ", subject).strip(" -–—,:;.")
    if not subject:
        return "chore: update project files"
    m = CONVENTIONAL_RE.match(subject)
    if m:
        prefix = m.group(0).lower()
        rest = subject[m.end():].strip()
        return f"{prefix}{rest[:72].rstrip()}" if rest else "chore: update project files"
    low = subject.lower()
    if re.search(r"\b(fix|bug|typo|broken|error|crash|issue)\b", low):
        kind = "fix"
    elif re.search(r"\b(doc|docs|readme|comment|changelog)\b", low):
        kind = "docs"
    elif re.search(r"\b(refactor|simplify|restructure)\b", low):
        kind = "refactor"
    elif re.search(r"\b(test|tests|unittest)\b", low):
        kind = "test"
    elif re.search(r"\b(rename|sort|format|lint|bump|clean|remove|chore|config)\b", low):
        kind = "chore"
    else:
        kind = "feat"
    if len(subject) > 1 and subject[0].isupper() and not subject[1].isupper():
        subject = subject[0].lower() + subject[1:]
    return f"{kind}: {subject[:72].rstrip()}"


def git_env():
    """Preserve user git configuration while removing any AI/bot author environment overrides."""
    env = dict(os.environ)
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        if k in env and AI_TERMS_RE.search(env[k]):
            env.pop(k, None)
    return env


def auto_commit(workdir, task, log_path=None, files=None, verified=True):
    """Stage and commit only `files` (the run's own, from hub/snapshot.py) for verified runs, with zero AI traces."""
    if not verified or not workdir or not Path(workdir).is_dir():
        return False
    env = git_env()
    try:
        chk = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workdir, capture_output=True, text=True, env=env, timeout=10,
        )
        if chk.returncode != 0 or chk.stdout.strip() != "true":
            return False
        if not files:   # never `git add -A`: without a list of the run's own files there is nothing safe to commit
            return False
        specs = [f":(top,literal){f}" for f in files]
        subprocess.run(["git", "add", "-A", "--", *specs], cwd=workdir, capture_output=True, env=env, timeout=15)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *specs], cwd=workdir,
                              capture_output=True, env=env, timeout=10)
        if diff.returncode == 0:
            return False
        msg = clean_commit_message(task)
        # `-- specs` commits only those paths, even when other files are staged (another agent's work)
        res = subprocess.run(
            ["git", "commit", "-m", msg, "--only", "--", *specs],
            cwd=workdir, capture_output=True, text=True, env=env, timeout=20,
        )
        if res.returncode == 0 and log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as log:
                    log.write(f"\n[git] committed: {msg}\n")
            except OSError:
                pass
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def git_status(workdir):
    """`git status --porcelain` of the folder, or '' when it is not a repository (used to see if a run touched files)."""
    if not workdir or not Path(workdir).is_dir():
        return ""
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=workdir,
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def run_diff(agent, cfg):
    """Unified diff of what one run changed: its own files (hub/snapshot.py) against the HEAD it started from,
    so it still shows after the run auto-committed. Sessions without a file list get the whole tree's diff."""
    workdir = agent.get("workspace") or agent.get("cwd")
    if not workdir and agent.get("project"):
        proj_path = obsidian.find_project(cfg, agent["project"])
        if proj_path:
            workdir = str(proj_path)
    if not workdir or not Path(workdir).is_dir():
        return "No directory found for this run."
    p = Path(workdir)
    snap = snapshot.take(p)
    if not snap:
        return f"Directory {p} is not a git repository."
    files = agent.get("files") or []
    base = agent.get("base_head") or snap["head"] or "HEAD"
    specs = [f":(top,literal){f}" for f in files]

    def git(*args):
        return subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, timeout=15)
    try:
        branch = ""   # a worktree run whose work was left on its branch: show that branch's diff
        if agent.get("worktree_branch") and agent.get("worktree_result") not in worktrees.BROUGHT + ("",) \
                and git("rev-parse", "--verify", "-q", agent["worktree_branch"] + "^{commit}").returncode == 0:
            branch = agent["worktree_branch"]
        r = git("diff", "--stat", "--patch", base, *([branch] if branch else []), *(["--", *specs] if specs else []))
        if r.returncode != 0:
            return f"git diff failed: {r.stderr}"
        out = r.stdout
        for f in ([] if branch else files):   # new files git doesn't track yet have no entry in `git diff <commit>`
            if git("ls-files", "--error-unmatch", "--", f":(top,literal){f}").returncode != 0 \
                    and (Path(snap["top"]) / f).is_file():
                out += git("diff", "--no-index", "--", "/dev/null", str(Path(snap["top"]) / f)).stdout
        return out or "(no changes recorded for this run)"
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error running git diff: {e}"
