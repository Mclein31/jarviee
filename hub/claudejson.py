"""Structured results of headless Claude Code runs (`claude -p --output-format json`).

Opt-in: `cfg["tools"]["claude"]["json_output"]` (default off, see `enabled`). When on, the run's log holds one JSON
result object instead of prose. After the run, `settle` reads that object, saves it as `<logs_dir>/results/<session>.json`
(exact cost for hub/runcost.py), and rewrites the log to the result text, so the dashboard's log tail, the spoken
summary, the pipeline's `VERDICT: PASS|FAIL` parsing and the phrase matching in hub/outcome.py keep reading prose.

Result fields used (docs: code.claude.com/docs/en/agent-sdk/typescript, SDKResultMessage): `type: "result"`, `subtype`
(`success`, `error_max_turns`, `error_during_execution`, `error_max_budget_usd`, `error_max_structured_output_retries`),
`is_error`, `result` (success only), `errors` (error variants), `num_turns`, `total_cost_usd`, `session_id`,
`permission_denials` [{tool_name, tool_use_id, tool_input}], `modelUsage`. Pure parsing; never raises.
"""
import json
import os
import re
import tempfile
from pathlib import Path

from . import outcome

FLAGS = ["--output-format", "json"]
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")


def enabled(cfg, tool="claude"):
    """The switch: tools.claude.json_output. Off unless set true in config (the default lives here)."""
    if tool != "claude":
        return False
    try:
        return bool(((cfg or {}).get("tools") or {}).get("claude", {}).get("json_output", False))
    except AttributeError:
        return False


def _as_result(obj):
    """The result object in a parsed value: the object itself, or the last one in a list (verbose json)."""
    if isinstance(obj, dict) and obj.get("type") == "result":
        return obj
    if isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    return None


def find(text):
    """(result dict, start offset of the JSON in `text`), or (None, -1) for prose, truncated or unrelated JSON.

    The log may start with the launcher's header and carry stderr lines, so every line that begins with `{` or `[`
    is tried, last one first; a pretty-printed object is also tried from its first brace on."""
    text = text or ""
    starts, pos = [], 0
    for line in text.splitlines(keepends=True):
        if line[:1] in ("{", "["):
            starts.append(pos)
        pos += len(line)
    dec = json.JSONDecoder()
    for st in reversed(starts):
        try:
            obj, _ = dec.raw_decode(text[st:])
        except ValueError:
            continue
        res = _as_result(obj)
        if res is not None:
            return res, st
    return None, -1


def parse(text):
    return find(text)[0]


def result_text(res):
    """The prose of a result: its `result` text, else the error list, else a one-line summary."""
    txt = res.get("result")
    if isinstance(txt, str) and txt.strip():
        return txt
    errs = res.get("errors")
    if isinstance(errs, list) and errs:
        return "\n".join(str(e) for e in errs)
    return ""


def denials(res):
    d = res.get("permission_denials")
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def verdict(res):
    """(kind, reason) from a result object: failed on an error result, needs_you when a tool call was denied for
    lack of permission, else done. The caller still runs the phrase matching on the text for a plain success,
    because a question to the owner is not a structured field."""
    sub = str(res.get("subtype") or "")
    if res.get("is_error") or (sub and sub != "success"):
        why = sub if sub and sub != "success" else "error result"
        text = " ".join(result_text(res).split())[:100]
        return outcome.FAILED, f"{why}: {text}" if text else why
    den = denials(res)
    if den:
        names = sorted({str(d.get("tool_name") or "a tool") for d in den})
        return outcome.NEEDS_YOU, f"{len(den)} tool call(s) denied for permission ({', '.join(names)[:60]})"
    return outcome.DONE, "finished"


def summary(res):
    """Small record kept per session: what runcost and the dashboard need, without the result text."""
    mu = res.get("modelUsage") if isinstance(res.get("modelUsage"), dict) else {}
    return {"session_id": str(res.get("session_id") or ""), "subtype": str(res.get("subtype") or ""),
            "is_error": bool(res.get("is_error")), "num_turns": res.get("num_turns"),
            "total_cost_usd": res.get("total_cost_usd"), "duration_ms": res.get("duration_ms"),
            "permission_denials": len(denials(res)), "modelUsage": mu}


def results_dir(cfg):
    return Path((cfg or {}).get("logs_dir") or ".") / "results"


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def settle(log_path, cfg):
    """Read the finished run's log. When it holds a JSON result: save its summary per session, replace the JSON in
    the log with the result text (everything before it, such as the launcher header, stays), and return the result.
    None when the log has no usable result (the phrase matching then decides alone). Never raises."""
    try:
        path = Path(log_path)
        text = path.read_text(encoding="utf-8", errors="replace")
        res, start = find(text)
        if res is None:
            return None
        sid = str(res.get("session_id") or "")
        if SESSION_RE.match(sid):
            _atomic_write(results_dir(cfg) / f"{sid}.json", json.dumps(summary(res)))
        prose = result_text(res)
        _atomic_write(path, text[:start] + prose + ("" if prose.endswith("\n") else "\n"))
        return res
    except Exception as e:
        print(f"[claudejson] {e!r}")
        return None


def load_exact(cfg, session):
    """The saved summary for a Claude session (exact cost), or None."""
    if not SESSION_RE.match(session or ""):
        return None
    try:
        d = json.loads((results_dir(cfg) / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("total_cost_usd"), (int, float)) else None
