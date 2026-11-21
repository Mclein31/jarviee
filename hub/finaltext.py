"""The clean last answer of a live session (the real claude / agy TUI in a pane).

A live session's log is the TUI's screen output (cursor moves, redraws, the logo), not text the pipeline can read.
The agent's own transcript holds the answer as plain text, so when a live run ends it is copied to the end of the
run log after MARK, and `after_mark` hands readers (scheduler, pipeline) only that part.
"""
import json
import re
from pathlib import Path

from . import progress

MARK = "[jarviee] final answer:"
CLAUDE_PROJECTS = None   # ~/.claude/projects when None (tests point it elsewhere)


def _lines_backwards(path, limit=4000):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return list(reversed(lines[-limit:]))


def claude_transcript(session, projects_dir=None):
    """The newest ~/.claude/projects/*/<session>.jsonl, or None."""
    if not session or "/" in str(session):
        return None
    root = Path(projects_dir or CLAUDE_PROJECTS or Path.home() / ".claude" / "projects")
    try:
        found = sorted(root.glob(f"*/{session}.jsonl"), key=lambda p: -p.stat().st_mtime)
    except OSError:
        return None
    return found[0] if found else None


def claude_text(session, projects_dir=None):
    """The text of the last turn's closing assistant messages in ~/.claude/projects/*/<session>.jsonl, or ''."""
    path = claude_transcript(session, projects_dir)
    if not path:
        return ""
    parts = []
    for line in _lines_backwards(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind = d.get("type")
        if kind == "user":   # a tool result or the prompt: the closing message starts after it
            if parts:
                break
            continue
        if kind != "assistant":
            continue
        content = (d.get("message") or {}).get("content")
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"] \
            if isinstance(content, list) else [content] if isinstance(content, str) else []
        text = "\n".join(t for t in texts if t and t.strip())
        if text:
            parts.append(text)
    return "\n\n".join(reversed(parts)).strip()


def agy_text(task, started):
    """The last model response in the agy transcript of this run, or ''."""
    tp = progress.find_agy_transcript(task, started)
    if not tp:
        return ""
    for line in _lines_backwards(tp):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("source") == "MODEL" and d.get("type") == "PLANNER_RESPONSE" and str(d.get("content") or "").strip():
            return str(d["content"]).strip()
    return ""


# ---- background work still pending when a turn ends -------------------------------------------------------------
# Claude Code transcript (~/.claude/projects/<dir>/<session>.jsonl, one JSON object per line), as of 2026-11:
#   started:  a "user" line carrying the tool_result of the launching call, with a `toolUseResult` object:
#               Bash run_in_background   {"backgroundTaskId": "<id>", ...}
#               Agent / Task in the background  {"isAsync": true, "status": "async_launched", "agentId": "<id>", ...}
#               Monitor                  {"taskId": "<id>", "timeoutMs": ..., ...}   (TaskUpdate also returns a
#                                        "taskId": a todo item, never background work, so a Monitor call is required)
#             The tool_use_id is in message.content[].tool_use_id of that line.
#   finished: a <task-notification> with a <status> (completed / failed / killed / stopped) naming <task-id> and
#             usually <tool-use-id>, DELIVERED to the model: a "user" line whose message.content is that text (origin
#             kind "task-notification"), or an attachment line {"attachment": {"type": "queued_command", "prompt": ...}}
#             when it was absorbed mid-turn. "queue-operation" enqueue lines are only queued (the agent is about to take
#             another turn), and Monitor *event* notifications carry <event> but no <status>: neither ends a task,
#             except a Monitor's expiry notice "<event>[Monitor expired after 5m ...", its last one.
NOTE_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTE_TASK_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
MONITOR_EXPIRED = "<event>[Monitor expired"   # a Monitor's last notice when it times out: an event, no <status>
NOTE_TOOL_RE = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")


def _delivered_notes(d):
    """The task-notification texts a transcript line delivered to the model ([] for anything else)."""
    texts = []
    att = d.get("attachment")
    if isinstance(att, dict) and att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
        texts.append(att["prompt"])
    elif d.get("type") == "user" and not d.get("isSidechain"):
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):   # text blocks only: a tool_result may quote a notification (a grep)
            texts += [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"
                      and isinstance(b.get("text"), str)]
    return [n for t in texts for n in NOTE_RE.findall(t) if "<status>" in n or MONITOR_EXPIRED in n]


def claude_pending(session, projects_dir=None):
    """Ids of the background tasks a Claude Code session started and hasn't been told the end of yet ([] when none),
    or None when its transcript can't be found or read."""
    path = claude_transcript(session, projects_dir)
    if not path:
        return None
    monitors, started, ended = set(), {}, set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "task-notification>" in line and '"queue-operation"' not in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    for note in _delivered_notes(d):
                        ended.update(NOTE_TASK_RE.findall(note) + NOTE_TOOL_RE.findall(note))
                if '"Monitor"' in line and '"tool_use"' in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    content = (d.get("message") or {}).get("content")
                    monitors.update(b.get("id") for b in content or [] if isinstance(b, dict)
                                    and b.get("type") == "tool_use" and b.get("name") == "Monitor")
                elif '"toolUseResult"' in line and ("backgroundTaskId" in line or "isAsync" in line or "taskId" in line):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    tr = d.get("toolUseResult")
                    if d.get("isSidechain") or not isinstance(tr, dict):
                        continue
                    content = (d.get("message") or {}).get("content")
                    tuid = next((b.get("tool_use_id") for b in content if isinstance(b, dict)
                                 and b.get("type") == "tool_result"), "") if isinstance(content, list) else ""
                    tid = tr.get("backgroundTaskId") or (tr.get("agentId") if tr.get("isAsync") else "")
                    if not tid and tr.get("taskId") and (tuid in monitors or "timeoutMs" in tr):
                        tid = tr["taskId"]
                    if tid:
                        started[str(tid)] = str(tuid or "")
    except OSError:
        return None
    return [t for t, u in started.items() if t not in ended and not (u and u in ended)]


# agy transcript (~/.gemini/antigravity-cli/brain/<conversation>/.system_generated/logs/transcript.jsonl), as of 2026-11:
#   started:  a MODEL "GENERIC" step whose content has a line "Tool is running as a background task with task id: <id>"
#             (a run_command that outlived its WaitMsBeforeAsync)
#   finished: a SYSTEM "SYSTEM_MESSAGE" step '[Message] timestamp=... sender=<id> priority=... content=Task id "<id>"
#             finished with result: ...' (or '... was canceled with result ...'; a schedule timer's says '30 minutes have
#             elapsed ...'), or a manage_task kill result 'Task "<id>" cancelled.'
AGY_BG_RE = re.compile(r"^Tool is running as a background task with task id: (\S+)\s*$", re.M)
AGY_END_RE = re.compile(r'Task id "([^"]+)" (?:finished|was cancell?ed)|\bsender=(\S+/task-\d+)')
AGY_KILLED_RE = re.compile(r'^Task "([^"]+)" cancell?ed\.', re.M)


def agy_pending(task, started_at):
    """Ids of the background tasks of this run's agy conversation that haven't reported back ([] when none), or
    None when its transcript can't be found or read."""
    tp = progress.find_agy_transcript(task, started_at)
    if not tp:
        return None
    started, ended = [], set()
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "background task" not in line and "Task id" not in line and "cancel" not in line \
                        and "sender=" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                content = str(d.get("content") or "")
                if d.get("source") == "SYSTEM":
                    ended.update(t for pair in AGY_END_RE.findall(content) for t in pair if t)
                elif d.get("type") == "GENERIC":
                    started += [t for t in AGY_BG_RE.findall(content) if t not in started]
                    ended.update(AGY_KILLED_RE.findall(content))
    except OSError:
        return None
    return [t for t in started if t not in ended]


def pending_background(tool, row, session=""):
    """The background tasks a live run's agent is still waiting on: a list ([] for none), or None when unknown."""
    row = row or {}
    try:
        if tool == "agy":
            return agy_pending(row.get("task"), row.get("started"))
        return claude_pending(row.get("session_id") or session)
    except Exception:   # never break a hook over a transcript
        return None


def session_of(argv):
    """The --session-id value in a claude argv, or ''."""
    for i, a in enumerate(argv[:-1]):
        if a == "--session-id":
            return argv[i + 1]
    return ""


def final_text(tool, argv, task, started):
    if tool in ("claude", "omniroute"):
        return claude_text(session_of(argv))
    if tool == "agy":
        return agy_text(task, started)
    return ""


def append(log_path, text):
    """Add the answer to the end of the run log. Returns True when written."""
    if not text:
        return False
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n{MARK}\n{text}\n")
        return True
    except OSError:
        return False


def after_mark(text):
    """Only the answer when the log carries one, else the text unchanged."""
    i = (text or "").rfind(MARK)
    return text if i < 0 else text[i + len(MARK):].strip("\n") + "\n"
