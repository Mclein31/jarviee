"""Hook events stored as spans (one row per model turn, tool call or notable event), per agent/run id.

`ops_for` is pure: it turns one hook event plus the run's open spans into insert/close operations.
`record` applies them; it is called from server.handle_hook and must never raise.
Only short argument summaries are stored, never file contents or full commands' output.
"""
import json
import time
import uuid

from .store import _lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
  id TEXT PRIMARY KEY,
  run_id TEXT,          -- agent id: run-<hex> for launched runs, claude-<sid> / agy-<cid> for hook sessions
  parent TEXT,          -- span id, or NULL for top-level spans of the run
  kind TEXT,            -- turn | tool | event
  agent TEXT,           -- tool name: claude | agy
  tool TEXT,            -- tool called (kind=tool) or event type (kind=event)
  args TEXT,            -- short summary, max 160 chars
  start REAL, end REAL, -- end NULL = still open
  status TEXT,          -- running | done | failed | needs_you | denied
  model TEXT, session TEXT,
  call_id TEXT          -- Claude tool_use_id, pairs PreToolUse with PostToolUse
);
CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id, start);
CREATE INDEX IF NOT EXISTS spans_start ON spans(start);
"""
TURN_START = ("UserPromptSubmit", "PreInvocation")
TURN_END = ("PostInvocation", "Stop", "StopFailure")
ARG_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "TargetFile",
            "CommandLine", "AbsolutePath", "SearchPath", "Query")
def ensure(store):
    if getattr(store, "_spans_ready", False):
        return
    with _lock:
        store.db.executescript(SCHEMA)
        store.db.commit()
    store._spans_ready = True


def summarize_args(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args[:160]
    if not isinstance(args, dict):
        return ""
    for k in ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:160]
    return ""


def _new(kind, parent, start, end=None, status="running", **kw):
    return {"id": uuid.uuid4().hex[:12], "kind": kind, "parent": parent, "start": start, "end": end,
            "status": status, "tool": kw.get("tool", ""), "args": kw.get("args", "")[:160]}


def ops_for(event, data, open_spans, now, last_ts=None):
    """Operations for one hook event: [("open", span)] / [("close", id, end, status)]."""
    turns = [s for s in open_spans if s["kind"] == "turn"]
    tools = [s for s in open_spans if s["kind"] == "tool"]
    turn = turns[-1] if turns else None
    call = data.get("toolCall") or {}
    name = call.get("name") or data.get("tool_name") or ""
    args = summarize_args(call.get("args") if call else data.get("tool_input"))
    ops = []
    if event in TURN_START:
        if turn:
            ops.append(("close", turn["id"], now, "done"))
        prompt = data.get("lastUserInput") or data.get("prompt") or ""
        ops.append(("open", _new("turn", None, now, tool="turn", args=summarize_args({"prompt": prompt}))))
    elif event == "PreToolUse":
        span = _new("tool", turn and turn["id"], now, tool=name, args=args)
        span["call_id"] = data.get("tool_use_id") or ""
        ops.append(("open", span))
    elif event == "PostToolUse":
        err = data.get("error") or (isinstance(data.get("tool_response"), dict) and data["tool_response"].get("error"))
        status = "failed" if err else "done"
        cid = data.get("tool_use_id") or ""
        match = next((s for s in reversed(tools) if cid and s.get("call_id") == cid), None) \
            or next((s for s in reversed(tools) if s["tool"] == name), None)
        if match:
            ops.append(("close", match["id"], now, status))
        else:   # agy reports no PreToolUse: the call ran since the previous event
            start = last_ts if last_ts and last_ts <= now else now
            ops.append(("open", _new("tool", turn and turn["id"], start, now, status, tool=name, args=args)))
    elif event == "PostToolUseFailure":   # the tool call failed: mark its span, with the error in its summary
        err = " ".join(str(data.get("error") or "tool failed").split())
        cid = data.get("tool_use_id") or ""
        match = next((s for s in reversed(tools) if cid and s.get("call_id") == cid), None) \
            or next((s for s in reversed(tools) if s["tool"] == name), None)
        shown = (args[:70] + " | " if args else "") + "error: " + err
        shown = shown[:160] if len(shown) <= 160 else shown[:157] + "..."
        if match:
            ops.append(("close", match["id"], now, "failed", shown))
        else:
            ops.append(("open", _new("tool", turn and turn["id"], now, now, "failed", tool=name, args=shown)))
    elif event == "PermissionRequest":    # the real "needs you" signal: names the tool and what it wants
        ops.append(("open", _new("event", turn and turn["id"], now, now, "needs_you", tool="needs_you",
                                 args=(f"{name}: {args}" if args else name or "permission needed"))))
    elif event == "Notification":
        ntype = str(data.get("notification_type") or "").lower()
        if ntype not in ("auth_success", "elicitation_complete", "elicitation_response"):
            msg = str(data.get("message") or "needs your input")
            ops.append(("open", _new("event", turn and turn["id"], now, now, "needs_you", tool="needs_you", args=msg)))
    if event in TURN_END:
        failed = bool(data.get("error")) or str(data.get("terminationReason") or "").endswith("ERROR")
        for s in tools:
            ops.append(("close", s["id"], now, "failed" if failed else "done"))
        if turn and not any(o[0] == "close" and o[1] == turn["id"] for o in ops):
            ops.append(("close", turn["id"], now, "failed" if failed else "done"))
        if failed:
            why = str(data.get("error") or "stopped with an error")
            if event == "StopFailure":   # error type, then the API's own text
                why = f"{why}: {data.get('error_details') or data.get('last_assistant_message') or ''}".rstrip(": ")
            ops.append(("open", _new("event", None, now, now, "failed", tool="failed", args=why)))
    elif event == "SessionEnd":
        for s in open_spans:
            ops.append(("close", s["id"], now, "done"))
    return ops


def record(store, aid, event, data, now=None):
    try:
        ensure(store)
        now = now or time.time()
        with _lock:
            rows = store.db.execute("SELECT * FROM spans WHERE run_id=? AND end IS NULL ORDER BY start",
                                    (aid,)).fetchall()
            last = store.db.execute("SELECT MAX(COALESCE(end, start)) FROM spans WHERE run_id=?", (aid,)).fetchone()[0]
        open_spans = [dict(r) for r in rows]
        ops = ops_for(event, data, open_spans, now, last)
        if not ops:
            return
        agent = data.get("jarvieeTool") or data.get("agentHubTool") or ("claude" if data.get("session_id") else "agy")
        session = str(data.get("session_id") or data.get("conversationId") or "")[:64]
        model = str(data.get("modelName") or data.get("model") or "")[:60]
        with _lock:
            for op in ops:
                if op[0] == "open":
                    s = op[1]
                    store.db.execute("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (s["id"], aid, s["parent"], s["kind"], agent, s["tool"], s["args"], s["start"],
                                      s["end"], s["status"], model, session, s.get("call_id") or ""))
                else:
                    store.db.execute("UPDATE spans SET end=?, status=? WHERE id=? AND end IS NULL",
                                     (op[2], op[3], op[1]))
                    if len(op) > 4:   # a close that also replaces the summary (tool failure)
                        store.db.execute("UPDATE spans SET args=? WHERE id=?", (op[4][:160], op[1]))
            store.db.commit()
    except Exception as e:     # hooks must never fail because of tracing
        print(f"[spans] {e!r}")


def recent(store, limit=40):
    """Newest tool calls and notable events across every run, for the live activity feed."""
    ensure(store)
    limit = max(1, min(int(limit or 40), 200))
    with _lock:
        rows = store.db.execute("SELECT id, run_id, kind, agent, tool, args, start, end, status, model FROM spans "
                                "WHERE kind IN ('tool', 'event') ORDER BY start DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def for_run(store, aid):
    ensure(store)
    with _lock:
        rows = store.db.execute("SELECT * FROM spans WHERE run_id=? ORDER BY start", (aid,)).fetchall()
    return [dict(r) for r in rows]
