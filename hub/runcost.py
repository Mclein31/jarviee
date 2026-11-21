"""Tokens and cost per run, read from the agents' own local logs. Never estimates missing numbers.

Claude: ~/.claude/projects/*/<session_id>.jsonl (message.usage, deduped on message.id + requestId).
agy:    ~/.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/logs/transcript.jsonl (per-step tokens).
Prices: config.json "pricing" (USD per million tokens), falling back to usage.PRICES for Claude models.
agy tokens are only priced when "pricing" has an entry with "tool": "agy" (agy runs on plan quota).
"""
import re

from . import claudejson, usage

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_cache = {}   # path -> (mtime, size, entries)


def price_table(cfg):
    rows = (cfg.get("pricing") or {}).get("models") if isinstance(cfg.get("pricing"), dict) else None
    if rows:
        return [r for r in rows if isinstance(r, dict) and r.get("match")]
    return [{"match": k, "input": i, "output": o, "cache_read": r, "tool": "claude"} for k, i, o, r in usage.PRICES]


def price_for(cfg, tool, model):
    m = str(model or "").lower()
    for r in price_table(cfg):
        if str(r["match"]).lower() in m and (r.get("tool") or "claude") == tool:
            return float(r.get("input", 0)), float(r.get("output", 0)), float(r.get("cache_read", 0))
    return None


def cost(cfg, tool, model, t):
    p = price_for(cfg, tool, model)
    if not p:
        return None
    i, o, r = p
    return round((t["input"] * i + t["output"] * o + t["cache_read"] * r
                  + t.get("write_5m", 0) * i * 1.25 + t.get("write_1h", 0) * i * 2.0) / 1e6, 6)


def _entries(path, parse):
    """Parsed, deduplicated usage lines. Transcripts only grow, so only new complete lines are read."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    c = _cache.get(str(path))
    if not c or size < c["offset"]:
        c = _cache[str(path)] = {"offset": 0, "entries": [], "seen": set()}
    if size == c["offset"]:
        return c["entries"]
    try:
        with open(path, "rb") as f:
            f.seek(c["offset"])
            chunk = f.read(size - c["offset"])
    except OSError:
        return c["entries"]
    end = chunk.rfind(b"\n") + 1          # leave a half-written last line for next time
    for raw in chunk[:end].splitlines():
        e = parse(raw, path)
        if e and e["key"] not in c["seen"]:
            c["seen"].add(e["key"])
            c["entries"].append(e)
    c["offset"] += end
    return c["entries"]


def claude_files(session):
    if not SESSION_RE.match(session or ""):
        return []
    return sorted(usage.CLAUDE_PROJECTS.glob(f"*/{session}.jsonl"))


def agy_file(session):
    if not SESSION_RE.match(session or ""):
        return None
    p = usage.AGY_BRAIN / session / ".system_generated" / "logs" / "transcript.jsonl"
    return p if p.is_file() else None


def _add(models, model, e):
    m = models.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "write_5m": 0, "write_1h": 0})
    for k in m:
        m[k] += int(e.get(k) or 0)


def _exact_results(cfg, tool, sessions):
    """{session: saved result summary} when EVERY session of a Claude run has one (hub/claudejson.py), else {}.
    Mixing exact and estimated sessions would give a total that is neither."""
    if tool != "claude":
        return {}
    wanted = sorted({s for s in sessions if s})
    got = {s: claudejson.load_exact(cfg, s) for s in wanted}
    return got if wanted and all(got.values()) else {}


def run_usage(cfg, tool, sessions, model_hint=""):
    """{"models": {model: {tokens..., "cost"}}, "tokens", "cost", "source", "note"} for one run."""
    models = {}
    found = False
    for s in sorted({s for s in sessions if s}):
        if tool == "claude":
            for p in claude_files(s):
                found = True
                for e in _entries(p, usage._claude_line):
                    _add(models, e["model"], e)
        elif tool == "agy":
            p = agy_file(s)
            if p:
                found = True
                for e in _entries(p, usage._agy_line):
                    _add(models, model_hint or "agy", e)
    exact = _exact_results(cfg, tool, sessions)
    if exact and not models:   # no transcript here: the result's per-model usage still gives tokens
        for r in exact.values():
            for name, u in (r.get("modelUsage") or {}).items():
                if isinstance(u, dict):
                    _add(models, name, {"input": u.get("inputTokens"), "output": u.get("outputTokens"),
                                        "cache_read": u.get("cacheReadInputTokens"),
                                        "write_5m": u.get("cacheCreationInputTokens")})
        found = True
    if not found:
        why = "no session reported by hooks yet" if not any(sessions) else f"no local {tool} log for this session"
        return {"models": {}, "tokens": None, "cost": None, "source": "", "note": why}
    total, total_cost, priced = 0, 0.0, True
    for name, t in models.items():
        t["cache_write"] = t["write_5m"] + t["write_1h"]
        t["total"] = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
        t["cost"] = cost(cfg, tool, name, t)
        total += t["total"]
        if t["cost"] is None:
            priced = False
        else:
            total_cost += t["cost"]
    if exact:   # the Claude CLI's own total beats pricing a transcript ourselves
        return {"models": models, "tokens": total, "cost": round(sum(r["total_cost_usd"] for r in exact.values()), 6),
                "source": "claude result (exact)", "note": ""}
    note = "" if priced else ("agy runs on plan quota; add a \"tool\": \"agy\" price in config pricing to estimate"
                              if tool == "agy" else "no price for this model in config pricing")
    return {"models": models, "tokens": total, "cost": round(total_cost, 6) if priced else None,
            "source": "claude transcript" if tool == "claude" else "agy transcript", "note": note}
