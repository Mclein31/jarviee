"""Hook ingestion: turns an agent hook event into a store row, and the model/tier helpers it needs."""
import json
import os
import re
import time
import uuid

from . import board_api, coord, finaltext, guards, launcher, outcome, runcost, runstate, spans, subagents
from .router import classify
from .titles import make_summary, make_title

WAITING_EVENTS = ("Notification",)
NON_WAITING_NOTIFICATION_TYPES = ("auth_success", "elicitation_complete", "elicitation_response")
GENERIC_NOTIFICATION_TITLES = ("claude", "claude code", "notification", "permission", "input")
PERMISSION_PREFIX = "needs permission: "
EFFORT_SUFFIX_RE = re.compile(r"-(extra-low|low|medium|high|xhigh|max)$")


def _extract_effort(val):
    if isinstance(val, dict):
        val = val.get("level") or ""
    return str(val or "").strip().lower()


def normalize_model(model):
    """Normalize an Antigravity display name or Claude model ID into a clean slug."""
    if isinstance(model, dict):
        model = model.get("id") or model.get("display_name") or ""
    s = str(model or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"@[^\s]+", "", s)
    s = re.sub(r"-(?:20\d{6}|latest|preview|thinking)$", "", s)
    s = re.sub(r"[\s()_]+", "-", s).strip("-")
    if s.startswith("claude-"):
        s = re.sub(r"(?<=\d)\.(?=\d)", "-", s)
    return s


def tier_of_model(model, cfg, effort=""):
    """Best-effort tier for a model name (and optional effort) reported by a hook ('' if unknown)."""
    m = normalize_model(model)
    if not m or m == "auto":
        return ""
    ef = _extract_effort(effort)
    if not ef:
        match = EFFORT_SUFFIX_RE.search(m)
        if match:
            ef = match.group(1)

    entries = [
        (normalize_model(slug), tier, str((t.get("efforts") or {}).get(tier, "")).lower())
        for t in cfg["tools"].values()
        for tier, slug in t["models"].items()
    ]
    exact = [(tier, tef) for slug, tier, tef in entries if m == slug]
    if exact:
        if len(exact) > 1 and ef:
            for tier, tef in exact:
                if tef == ef:
                    return tier
            if ef in ("high", "xhigh", "max"):
                for tier, tef in exact:
                    if tef == "medium":
                        return tier
        return exact[0][0]

    if "opus" in m or "ultra" in m:
        return "heavy"
    if "haiku" in m or "flash-lite" in m:
        return "basic"
    if "flash" in m:
        return "basic" if ef in ("extra-low", "low") else "light"
    if "orchestrator" in m:                       # Neon/Argon planner + Hydrogen executor
        return "complex"
    if "neon" in m:
        return "heavy" if ef in ("high", "xhigh", "max") else "complex"
    if "hydrogen" in m:
        return "complex" if ef in ("high", "xhigh", "max") else "light"
    if "argon" in m or ("gemini" in m and "pro" in m):
        return "complex" if ef in ("high", "xhigh", "max") else "light"
    if "sonnet" in m:
        return "complex" if ef in ("medium", "high", "xhigh", "max") else "light"

    for slug, tier, _ in sorted(entries, key=lambda x: -len(x[0])):
        if slug and (slug in m or m in slug):
            return tier
    return ""


def _format_notification(data, existing):
    """Format a Claude Notification payload into a concise waiting reason."""
    ntype = str(data.get("notification_type") or "").strip().lower()
    msg = str(data.get("message") or "").strip()
    title = str(data.get("title") or "").strip()

    if not msg:
        if title and title.lower() not in GENERIC_NOTIFICATION_TITLES:
            msg = title
            title = ""
        elif ntype in ("permission_prompt", "worker_permission_prompt"):
            msg = "Claude needs your permission"
        elif ntype == "idle_prompt":
            msg = "turn finished, waiting for you"
        elif ntype in ("elicitation_dialog", "elicitation_url_dialog"):
            msg = "Claude Code needs your input"
        else:
            msg = "needs your input"

    if msg.lower() == "claude needs your permission":
        cur_act = str((existing or {}).get("activity") or "")
        if cur_act.startswith("using ") and cur_act != "using a tool":
            msg = f"Claude needs your permission to use {cur_act[6:]}"

    if title and title.lower() not in GENERIC_NOTIFICATION_TITLES and title.lower() not in msg.lower():
        msg = f"{msg} ({title})"

    return msg[:120]


def _tool_brief(data):
    """'Bash `rm -rf x`' style: the tool and the short argument the reporter kept (command, path, url...)."""
    name = str(data.get("tool_name") or "a tool")
    ti = data.get("tool_input")
    arg = ""
    if isinstance(ti, dict):
        arg = next((" ".join(v.split()) for v in ti.values() if isinstance(v, str) and v.strip()), "")
    return f"{name} {arg}".strip() if arg else name


def _api_error(data):
    """StopFailure: 'rate_limit: 429 Too Many Requests' (the error type, then the API's own details)."""
    kind = str(data.get("error") or "unknown").strip()
    detail = str(data.get("error_details") or data.get("last_assistant_message") or "").strip()
    return f"{kind}: {' '.join(detail.split())}" if detail else kind


# Text Claude Code itself injects as a "prompt" (subagent reports, task notifications, reminders, messages from other
# sessions): it isn't the owner's request, so it must not replace the session's title and task.
INJECTED_PROMPT_RE = re.compile(
    r"^\s*(\[subagent hand-back\]|\[system notification|<task-notification>|<system-reminder>|<agent-message\b|"
    r"<cross-session-message\b|another claude session sent a message)", re.I)


def new_job(existing, launched):
    """Fields that start a new job on an agent row: a fresh job id, and the previous job's counters, files and
    end time cleared, so reports cover only this job. A launched run's first prompt is its launch prompt (the
    job the launcher already opened), so it returns {}. The log offset makes reports read only this job's output."""
    n = int((existing or {}).get("job_no") or 0)
    if launched and n == 0:
        return {"job_no": 1}
    off = 0
    if existing and existing.get("log_path"):
        try:
            off = os.path.getsize(existing["log_path"])
        except OSError:
            off = 0
    return {"job_id": "job-" + uuid.uuid4().hex[:8], "job_no": n + 1, "job_log_offset": off,
            "started": time.time(), "ended": None, "steps": 0, "files": [], "verified": 0}


def _edited_path(data):
    """The file an Edit/Write-style tool call changed, from the reporter's brief tool input ('' if none)."""
    tc = data.get("toolCall") if isinstance(data.get("toolCall"), dict) else {}
    name = str(data.get("tool_name") or tc.get("name") or "").lower().replace("_", "").replace("-", "")
    if not any(name == t.replace("_", "") for t in coord.EDIT_TOOLS):
        return ""
    for src in (data.get("tool_input"), tc.get("args")):
        if isinstance(src, dict):
            for key in ("file_path", "path", "notebook_path", "TargetFile", "AbsolutePath"):
                if isinstance(src.get(key), str) and src[key].strip():
                    return src[key].strip()
    return ""


def coordinate(event, data, aid, fields, prompt, store):
    """Claims and messages for hook-reported agents (hub/coord.py). Terminal sessions are never held: an overlap
    becomes a message to them. Each job registers, every edit is announced, and the job's end releases it."""
    row = store.get(aid) or {}
    ws = row.get("workspace") or data.get("cwd") or ""
    if fields.get("job_id"):   # a new job (a launched run's launch prompt was registered by the launcher)
        co = coord.register(store, aid, ws, prompt, coord.files_in(prompt, ws), title=row.get("title") or "")
        if co["decision"] == "wait":
            coord.post(store, "jarviee", "note", "Heads-up: overlapping work is in progress (" + co["note"] + "). "
                       "Coordinate: finish after it, or leave those files alone.", ws, recipient=aid)
        elif co["decision"] == "split":
            coord.post(store, "jarviee", "note", co["note"], ws, recipient=aid)
    if event == "PostToolUse":
        path = _edited_path(data)
        if path:
            coord.touch(store, aid, path, ws)
    st = row.get("status")
    if st in ("done", "failed", "stopped") or (event in ("Stop", "SessionEnd") and st != "running"):
        coord.release(store, aid, "replied" if st == "waiting" else st or "done")


CONTEXT_EVENTS = ("UserPromptSubmit", "PostToolUse", "SessionStart")


def hook_reply(event, data, aid, store):
    """The /hook answer: the row id, plus new channel messages for a Claude agent to read (additionalContext)."""
    out = {"id": aid}
    tool = data.get("jarvieeTool") or data.get("agentHubTool") or ""
    if tool == "claude" and event in CONTEXT_EVENTS:
        try:
            row = store.get(aid) or {}
            ctx = coord.context_for(store, aid, row.get("workspace") or data.get("cwd") or "")
            if ctx:
                out["context"] = ctx
        except Exception as e:
            print(f"[coord] {e!r}")
    return out


def handle_hook(event, data, cfg, store):
    run_id = data.get("jarvieeRunId") or data.get("agentHubRunId") or ""   # agentHub*: hooks from before the rename
    tool = data.get("jarvieeTool") or data.get("agentHubTool") or "agy"
    conv = data.get("conversationId") or data.get("session_id") or "unknown"
    aid = run_id or f"{tool}-{conv}"
    existing = store.get(aid)
    launched = bool(existing and existing.get("source") == "launcher")
    fields = {}
    if not existing:
        ws = data.get("workspacePaths") or [data.get("cwd") or ""]
        fields.update(source="hook", tool=tool, status="running",
                      workspace=(ws[0] if ws else ""), task="(session started in the app)")

    if data.get("pid") and not launched:   # a launched row's pid + pid_start were recorded together at launch
        try:
            fields["pid"] = int(data["pid"])
        except (ValueError, TypeError):
            pass
    if data.get("agentPid") and not launched:
        try:
            fields["agent_pid"] = int(data["agentPid"])
        except (ValueError, TypeError):
            pass
    if data.get("agentHost") and not launched:
        fields["agent_host"] = str(data["agentHost"])[:40]

    prompt = data.get("lastUserInput") or data.get("prompt")
    raw_model = data.get("modelName") or data.get("model")
    norm_model = normalize_model(raw_model)
    raw_effort = _extract_effort(data.get("effort") or data.get("effortLevel"))

    # a live CLI session in a pane can be switched by hand (/model): follow it, a print run keeps its launch routing
    follow = not launched or launcher.is_live(run_id, store)
    if (norm_model or raw_effort) and follow:
        prev_model = (existing or {}).get("model") or ""
        if norm_model and not (norm_model == "auto" and prev_model and prev_model != "auto"):
            fields["model"] = norm_model
        eff_model = fields.get("model") or prev_model
        eff_effort = raw_effort or (existing or {}).get("effort") or ""
        if raw_effort:
            fields["effort"] = raw_effort
        if eff_model:
            tier = tier_of_model(eff_model, cfg, effort=eff_effort)
            fields["tier"] = tier
            if tier and not raw_effort and not (existing or {}).get("effort"):
                fields["effort"] = (cfg["tools"].get(tool, {}).get("efforts") or {}).get(tier, "")
    real_prompt = bool(prompt) and event in ("UserPromptSubmit", "PreInvocation") and not INJECTED_PROMPT_RE.match(str(prompt))
    if real_prompt and (not existing or existing.get("status") != "running"
                        or int(existing.get("job_no") or 0) == 0 or not existing.get("job_id")):
        # A follow-up prompt sent while the agent is still running stays on the same job, so Batcher
        # does not treat the interrupted turn as "replied / finished" while the agent is still working.
        fields.update(new_job(existing, launched))
    if prompt and (not launched or "job_id" in fields) and not INJECTED_PROMPT_RE.match(str(prompt)):
        fields["task"] = str(prompt)[:4000]
        hook_title = data.get("jarvieeTitle") or data.get("agentHubTitle")
        hook_summary = data.get("jarvieeSummary") or data.get("agentHubSummary")
        fields["title"] = str(hook_title or make_title(str(prompt)))[:60]
        fields["summary"] = str(hook_summary if hook_title else make_summary(str(prompt), fields["title"]))[:120]
        if not launched:   # a launched run keeps the routing it was launched with
            sug, reasons = classify(str(prompt), cfg)
            fields["suggested_tier"] = sug
            fields["reasons"] = json.dumps(reasons)

    tool_name = (data.get("toolCall") or {}).get("name") or data.get("tool_name")
    if event in ("SessionStart", "PreInvocation", "UserPromptSubmit"):
        fields.update(status="running", activity="thinking")
    elif event == "PreToolUse":
        fields.update(status="running", activity=f"using {tool_name or 'a tool'}")
    elif event == "PostToolUse":
        fields.update(status="running", activity=f"ran {tool_name or 'a tool'}")
        if data.get("error"):
            fields["activity"] = f"tool error: {str(data['error'])[:80]}"
    elif event == "PostInvocation":
        fields.setdefault("activity", "thinking")
    elif event == "PermissionRequest":
        # the real "needs you" signal. A print run has nobody to answer: Claude Code denies the call by itself, so
        # it is noted on the row but only a live session or an app session waits for you.
        what = f"{PERMISSION_PREFIX}{_tool_brief(data)}"[:120]
        if run_id and not launcher.is_live(run_id, store):
            fields["activity"] = what.replace(PERMISSION_PREFIX, "permission requested: ", 1)
        else:
            fields.update(status="waiting", activity=what)
    elif event == "PostToolUseFailure":
        # recorded on the trace span (spans.py); the run's status is untouched, a failed tool is not a failed run
        if (existing or {}).get("status") == "running":
            fields["activity"] = f"tool failed: {str(data.get('error') or tool_name or 'a tool')[:80]}"
    elif event == "StopFailure":
        # the turn ended on an API error (rate limit, auth, overloaded...): that is the run's failure reason. A print
        # run ends by its exit code (outcome / detector read its log), so only its activity is noted here.
        why = f"API error ({_api_error(data)})"[:120]
        if run_id and not launcher.is_live(run_id, store):
            fields["activity"] = why
        else:
            fields.update(status="failed", activity=why)
    elif event == "TaskCompleted":
        if (existing or {}).get("status") == "running" and data.get("task_subject"):
            fields["activity"] = f"task done: {str(data['task_subject'])[:80]}"
    elif event in ("SubagentStart", "SubagentStop"):
        subagents.note_event(aid, event, data)
    elif event in WAITING_EVENTS:
        ntype = str(data.get("notification_type") or "").strip().lower()
        specific = ((existing or {}).get("status") == "waiting"
                    and str((existing or {}).get("activity") or "").startswith(PERMISSION_PREFIX))
        if ntype not in NON_WAITING_NOTIFICATION_TYPES and not (specific and ntype == "permission_prompt"):
            fields.update(status="waiting", activity=_format_notification(data, existing))
    elif event == "Stop":
        raw_reason = str(data.get("terminationReason") or "").strip()
        reason = re.sub(r"^EXECUTOR_TERMINATION_REASON_", "", raw_reason)
        if data.get("error") or reason == "ERROR":
            err = data.get("error") or "stopped with error"
            fields.update(status="failed", activity=str(err)[:120])
        elif not run_id or launcher.is_live(run_id, store):   # a live pane session waits for you; a print run is just ending
            note = "turn finished, waiting for you"
            if reason and reason not in ("NO_TOOL_CALL", "UNSPECIFIED"):
                note += f" ({reason.lower().replace('_', ' ')})"
            fields.update(status="waiting", activity=note)
            turn = str(data.get("last_assistant_message") or "")
            if run_id and not turn.strip() and (existing or {}).get("interactive") != runstate.LIVE_UNATTENDED:
                turn = _transcript_turn(existing, data)   # agy's Stop carries no message: read it from its transcript
            if run_id and (existing or {}).get("interactive") == runstate.LIVE_UNATTENDED:
                # a queue / pipeline run in the TUI: its one turn is the run (agy's Stop carries no message), and the
                # pipeline judges the answer; hooks/pane_runner.py ends the session and closes the pane. Unless the
                # turn ended to wait for a background task it started: then it is still working (_background_wait).
                wait, note = _background_wait(aid, existing, data, cfg)
                if wait:
                    fields.update(status="running", activity=wait)
                else:
                    fields.update(status="done", activity=note, ended=time.time())
            elif run_id and turn.strip():
                # A Jarviee pane: the turn's own closing message decides. A real question or blocker needs you and the
                # window stays; anything else is done, and hooks/pane_runner.py ends the session and closes it.
                kind, why = outcome.classify(0, turn, extra_blocked=(cfg.get("escalation") or {}).get("blocked_patterns", []))
                if kind == outcome.NEEDS_YOU:
                    fields.update(status="waiting", activity=f"needs you: {why}")
                else:
                    fields.update(status="done", activity="finished", ended=time.time())
    elif event == "SessionEnd":
        fields.update(status="done", activity="session ended", ended=time.time())

    closed = existing and (existing.get("closed_by_user") or (
        existing.get("status") == "stopped" and str(existing.get("activity") or "").startswith("stopped by you")))
    if closed:
        if launched or event not in ("SessionStart", "UserPromptSubmit", "PreInvocation"):
            # Trailing events (Stop, SessionEnd...) from a process that was just closed must not revive or
            # re-finish the row. A launched run is never revived by hooks: only Run again / a reply restarts it.
            for k in ("status", "activity", "ended"):
                fields.pop(k, None)
        else:
            fields["closed_by_user"] = 0   # the owner typed a new prompt into that same terminal session
    store.upsert(aid, **fields)
    if fields.get("status") and fields["status"] != (existing or {}).get("status"):
        board_api.notify_run(cfg, aid, fields["status"])   # a pane run's card advances (never raises)
    if event == "PostToolUse":
        store.bump_steps(aid)
    try:
        coordinate(event, data, aid, fields, str(prompt or ""), store)
    except Exception as e:   # coordination never breaks a hook
        print(f"[coord] {e!r}")
    spans.record(store, aid, event, data)      # trace spans for the Orchestration view; never raises
    return aid


_BG_SINCE = {}   # run id -> when its turns first ended with a background task pending (the total-wait cap)


def _background_wait(aid, row, data, cfg):
    """(activity, '') while an unattended run's ended turn left a background task of its own running (a backgrounded
    command, Monitor or background subagent, read from the agent's transcript: hub/finaltext.py), so the run is not
    over yet: the agent takes another turn when the task reports back. ('', finish note) when nothing is pending, the
    transcript is unknown, or guards.background_wait_minutes has passed since its turns first ended waiting (then it
    finishes as before; hooks/pane_runner.py also caps one continuous wait). Never raises."""
    try:
        cap = guards.background_wait_seconds(cfg)
        row = row or {}
        tool = row.get("tool") or data.get("jarvieeTool") or ""
        pending = finaltext.pending_background(tool, row, data.get("session_id") or "") if cap else []
    except Exception:
        cap, tool, pending = 0, "", []
    if not pending:
        _BG_SINCE.pop(aid, None)
        # agy without a transcript: the pane checks agy's own "N task(s)" footer instead
        return "", (runstate.FINISHED_UNCHECKED if pending is None and tool == "agy" else "finished")
    since = _BG_SINCE.setdefault(aid, time.time())
    if time.time() - since >= cap:
        _BG_SINCE.pop(aid, None)
        return "", "finished"
    return f"{runstate.BG_WAIT} ({len(pending)} running)", ""


def _transcript_turn(row, data, tries=4, wait=0.4):
    """The closing message of a live pane's turn when the Stop hook did not carry it (agy), else ''.

    agy writes its last response to the transcript a moment around Stop, so an empty read is retried briefly.
    """
    row = row or {}
    agy = row.get("tool") == "agy" or data.get("jarvieeTool") == "agy"
    for i in range(tries if agy else 1):
        try:
            if agy:
                text = finaltext.agy_text(row.get("task"), row.get("started"))
            else:
                text = finaltext.claude_text(row.get("session_id") or data.get("session_id"))
        except Exception:
            text = ""
        if text:
            return text[-1500:]
        if agy and i < tries - 1:
            time.sleep(wait)
    return ""


_MODEL_CACHE = {}
TRANSCRIPT_MODEL_RE = re.compile(rb'"model":"(claude-[A-Za-z0-9.\-]+)"')


def transcript_model(session):
    """The model a Claude Code session last answered with, read from its transcript's tail ('' if unknown).

    Hook events don't carry the model, so sessions started in the app would otherwise show no model or tier.
    Cached per file modification time; reads at most the last 256 KB.
    """
    files = runcost.claude_files(session)
    if not files:
        return ""
    p = files[-1]
    try:
        st = p.stat()
        hit = _MODEL_CACHE.get(str(p))
        if hit and hit[0] == st.st_mtime:
            return hit[1]
        with open(p, "rb") as f:
            f.seek(max(0, st.st_size - 262144))
            found = TRANSCRIPT_MODEL_RE.findall(f.read())
    except OSError:
        return ""
    model = found[-1].decode() if found else ""
    _MODEL_CACHE[str(p)] = (st.st_mtime, model)
    return model
