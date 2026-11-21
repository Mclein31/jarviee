"""Run again and reply (R2-9): start a new run from a finished launched run.

* Run again: the same brief, optionally on another tool or level.
* Reply: a Claude run continues its own conversation (`--resume <session id>`, recorded at launch, same
  folder and model); any other tool is re-run with the original brief, the tail of its log and the reply."""
from . import launcher

LOG_TAIL = 3000
DONE = ("done", "failed", "stopped")


class FollowupError(ValueError):
    pass


def _check(row, tool, tier, cfg=None):
    if not row:
        raise FollowupError("no such run")
    if row.get("source") != "launcher" or not row.get("task"):
        raise FollowupError("only runs launched from Jarviee can be run again")
    if row.get("status") not in DONE:
        raise FollowupError("it is still running; wait for it to finish (or reply in its window)")
    known = (cfg or {}).get("tools") or ("agy", "claude", "omniroute")
    if tool and tool not in known:
        raise FollowupError(f"unknown tool '{tool}'")
    if tier and tier not in ("basic", "light", "complex", "heavy"):
        raise FollowupError(f"unknown level '{tier}'")


def _log_tail(row):
    try:
        with open(row["log_path"], encoding="utf-8", errors="replace") as f:
            return launcher.strip_tty(f.read())[-LOG_TAIL:].strip()
    except (OSError, KeyError, TypeError):
        return ""


def start(row, cfg, store, reply="", tool=None, tier=None):
    """Launch the follow-up run. Returns (run_id, plan, mode) with mode 'resume', 'rerun' or 'reply'."""
    reply = (reply or "").strip()
    _check(row, tool, tier, cfg)
    project, cwd = row.get("project") or None, row.get("workspace") or None
    prev_tool = row.get("tool") or ""
    if reply and not tool and not tier and row.get("session_id") \
            and ((cfg.get("tools") or {}).get(prev_tool) or {}).get("resume_args"):
        # same conversation: same model and effort too, the session's context was built with them
        p = launcher.plan(row["task"], cfg, prev_tool, row.get("tier") or None, project, model=row.get("model"))
        if p["tool"] == prev_tool:
            rid, p = launcher.launch(f"Reply: {reply}", cfg, store, project=project, cwd=cwd, p=p, prompt=reply,
                                     parent_run_id=row["id"], resume=row["session_id"])
            return rid, p, "resume"
    task, prompt = row["task"], None
    if reply:
        tail = _log_tail(row)
        context = f"\n\nWhat the previous attempt printed (tail):\n<<<\n{tail}\n>>>" if tail else ""
        task = f"{row['task']}\n\nFollow-up from the owner: {reply}"
        built, _ = launcher.build_prompt(row["task"], cfg, project)
        prompt = f"{built}{context}\n\nFollow-up from the owner: {reply}"
    p = launcher.plan(task, cfg, tool or row.get("tool"), tier or row.get("tier") or None, project)
    rid, p = launcher.launch(task, cfg, store, project=project, cwd=cwd, p=p, prompt=prompt, parent_run_id=row["id"])
    return rid, p, "reply" if reply else "rerun"
