#!/usr/bin/env python3
"""Hook reporter: forwards an agent event to the Jarviee command center.

Usage (from a hooks config):  python3 /path/to/report.py <EventName> [agy|claude]
Never blocks the agent: 2s timeout, all errors swallowed, always exits 0.
Run IDs: agents started by `hub run` inherit JARVIEE_RUN_ID (AGENT_HUB_RUN_ID before the rename), so their hook
events attach to the launched run instead of creating a second card.
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request


def env(key, default=None):
    """JARVIEE_<key>, else the old AGENT_HUB_<key> (agents started before the rename)."""
    return os.environ.get("JARVIEE_" + key, os.environ.get("AGENT_HUB_" + key, default))


def home():
    new = os.path.expanduser("~/.jarviee")
    old = os.path.expanduser("~/.agent-hub")
    return new if os.path.exists(new) or not os.path.exists(old) else old


def _port(raw):
    """A plain port number, else 7777: the value lands in the URL, and "7777@host" would send the token elsewhere."""
    raw = str(raw or "").strip()
    return raw if raw.isdigit() and 0 < int(raw) < 65536 else "7777"


PORT = _port(env("PORT", "7777"))
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
# Process lookups cost ~20 ms, so they run on session-level events only, never per tool call.
PID_EVENTS = ("SessionStart", "UserPromptSubmit", "PreInvocation", "Notification", "Stop")
AGENT_NAMES = {"claude": ("claude",), "agy": ("agy",)}
APP_HOSTS = ("language_server", "antigravity")


def find_agent(tool, start):
    """Walk up from the hook's parent to the agent process: (agent_pid, host).

    Claude Code runs the hook command through a shell, so the parent can be that shell rather
    than `claude`. Antigravity app conversations all live in the app's language_server, which
    must never be signalled; that case reports host 'antigravity-app' and no PID.
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True,
                             text=True, timeout=1).stdout
    except Exception:
        return None, ""
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()).lower())
    names = AGENT_NAMES.get(tool, (tool,))
    pid = start
    for _ in range(8):
        if pid not in procs or pid <= 1:
            break
        ppid, name = procs[pid]
        if any(name == n or name.startswith(n + "-") for n in names):
            return pid, ""
        if any(h in name for h in APP_HOSTS):
            return None, "antigravity-app"
        pid = ppid
    return None, ""


def add_title(data):
    """Short title + summary for a new prompt (hub/titles.py); silently skipped if unavailable."""
    prompt = data.get("lastUserInput") or data.get("prompt")
    if not prompt:
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from hub.titles import make_summary, make_title
        title = make_title(str(prompt))
        data["jarvieeTitle"] = title
        data["jarvieeSummary"] = make_summary(str(prompt), title)
    except Exception:
        pass


def load_token():
    tok = env("TOKEN")
    if tok:
        return tok.strip()
    try:
        p = os.path.expanduser(env("TOKEN_PATH", os.path.join(home(), "token")))
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def main():
    if env("HOOK_SKIP") == "1":   # Jarviee's own helper model calls (email gists): never a dashboard session
        return
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    tool = sys.argv[2] if len(sys.argv) > 2 else "agy"
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["jarvieeTool"] = tool
    if "pid" not in data:
        data["pid"] = os.getppid()
    run_id = env("RUN_ID")
    if event in PID_EVENTS and not run_id:
        agent_pid, host = find_agent(tool, os.getppid())
        if agent_pid:
            data["agentPid"] = agent_pid
        if host:
            data["agentHost"] = host
    add_title(data)
    if run_id:
        data["jarvieeRunId"] = run_id
    # keep the payload small: transcripts and tool outputs can be large
    def brief(a):   # file path / command head only, never file contents
        if not isinstance(a, dict):
            return None
        for key in ("file_path", "path", "command", "pattern", "url", "query", "description", "prompt"):
            v = a.get(key)
            if isinstance(v, str) and v.strip():
                return {key: " ".join(v.split())[:160]}
        return None
    ti = brief(data.get("tool_input"))
    # StopFailure's last_assistant_message is the short API error text, not model output: keep it, clipped. A
    # SubagentStop's is the subagent's closing message, shown on its row: kept clipped to the same 200 characters.
    api_error = str(data.get("last_assistant_message") or "")[:200] if event in ("StopFailure", "SubagentStop") else ""
    # a Stop's final message decides done or needs you for a live Jarviee pane: only its end matters
    if event == "Stop":
        api_error = str(data.get("last_assistant_message") or "")[-1500:]
    for k in ("transcriptPath", "transcript_path", "result", "tool_response", "tool_input", "last_assistant_message",
              "permission_suggestions", "background_tasks", "session_crons", "agent_transcript_path",
              "task_description"):
        data.pop(k, None)
    if api_error:
        data["last_assistant_message"] = api_error
    for k in ("error", "error_details", "task_subject"):   # failure text can embed command output: clip it
        if isinstance(data.get(k), str):
            data[k] = data[k][:300]
    if ti:
        data["tool_input"] = ti
    if isinstance(data.get("toolCall"), dict):
        tc = {"name": data["toolCall"].get("name")}
        if brief(data["toolCall"].get("args")):
            tc["args"] = brief(data["toolCall"].get("args"))
        data["toolCall"] = tc
    try:
        headers = {"Content-Type": "application/json"}
        tok = load_token()
        if tok:
            headers["X-Jarviee-Token"] = tok
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/hook?event={urllib.parse.quote(event, safe='')}",
            data=json.dumps(data).encode(), headers=headers)
        reply = json.loads(_OPEN(req, timeout=2).read() or b"{}")
    except Exception:
        reply = {}
    print_reply(event, tool, reply if isinstance(reply, dict) else {})


CONTEXT_EVENTS = ("UserPromptSubmit", "PostToolUse", "SessionStart")
# Observe-only events: nothing is ever printed for them. Empty stdout + exit 0 is "no decision" in Claude Code,
# whereas any JSON here (a PermissionRequest decision, a SubagentStop/TaskCompleted block) could gate the agent.
SILENT_EVENTS = ("PermissionRequest", "PostToolUseFailure", "StopFailure", "SubagentStart", "SubagentStop",
                 "TaskCompleted")


def print_reply(event, tool, reply):
    """Antigravity expects a JSON object on stdout; {} means "no change". Claude Code gets nothing, except new
    messages from other agents (hub/coord.py) as additionalContext: context for the model, never a block or a
    decision, so the hook still can't gate the agent."""
    if tool == "agy":
        print("{}")
        return
    if event in SILENT_EVENTS:
        return
    ctx = reply.get("context")
    if tool == "claude" and event in CONTEXT_EVENTS and isinstance(ctx, str) and ctx.strip():
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx[:2000]}}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
