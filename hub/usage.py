"""Usage and health data for the dashboard: GET /api/usage, cached for 60 s.

Every number comes from a local source. A value that can't be read is returned as
null together with a reason, never estimated into a limit.

Sources
- Claude tokens and est. cost: Claude Code transcripts, ~/.claude/projects/**/*.jsonl
  (assistant lines carry message.usage; deduplicated by message id + request id, like ccusage).
- Claude 5-hour and weekly limits: Claude Code hands `rate_limits` only to status-line
  scripts; hooks/statusline.py saves them to ~/.jarviee/claude-limits.json.
- agy quota: `agy -p /usage --output-format json` (remaining fraction and reset per bucket).
- agy tokens: Antigravity transcripts, ~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl
- Activity: the agents table (finished runs, including ones hidden by Clear finished).
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brand

CACHE_TTL = 60
WINDOW_DAYS = 8                  # transcripts older than this are never read
FIVE_HOURS = 5 * 3600
CLAUDE_PROJECTS = Path(brand.env("CLAUDE_PROJECTS") or Path.home() / ".claude" / "projects")
AGY_BRAIN = Path(brand.env("AGY_BRAIN") or Path.home() / ".gemini" / "antigravity-cli" / "brain")
LIMITS_FILE = Path(brand.env("CLAUDE_LIMITS") or brand.home() / "claude-limits.json")

# USD per million tokens: input, output, cache read. Cache writes bill at 1.25x input (5 min TTL)
# and 2x input (1 h TTL). First-party API rates; on a subscription this is an equivalent, not a bill.
PRICES = [
    ("fable-5-2", 8.0, 40.0, 0.20), ("fable-5-1", 10.0, 50.0, 0.25), ("mythos-5-1", 10.0, 50.0, 0.25),
    ("fable-5", 10.0, 50.0, 1.00), ("mythos-5", 10.0, 50.0, 1.00),
    ("opus-5-5", 4.0, 20.0, 0.20), ("opus-5", 5.0, 25.0, 0.50),
    ("opus-4", 5.0, 25.0, 0.50),
    ("sonnet-5", 2.0, 10.0, 0.20), ("sonnet-4", 3.0, 15.0, 0.30),
    ("haiku-5", 0.10, 0.50, 0.01), ("haiku-4", 1.0, 5.0, 0.10),
]


def price_for(model):
    m = str(model or "").lower()
    for key, i, o, r in PRICES:
        if key in m:
            return i, o, r
    return None


def model_label(model):
    """claude-opus-5-5 -> Opus 5.5; unknown names pass through."""
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", str(model or ""))
    if not m:
        return str(model or "unknown")
    return f"{m.group(1).capitalize()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def local_midnight(now=None):
    d = datetime.fromtimestamp(now or time.time())
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class _Scanner:
    """Incremental JSONL reader: remembers each file's byte offset and the entries it parsed."""

    def __init__(self, parse_line):
        self.parse_line = parse_line
        self.files = {}          # path -> {"offset": int, "entries": [..]}
        self.lock = threading.Lock()

    def scan(self, paths, since):
        with self.lock:
            seen = set()
            for path in paths:
                seen.add(path)
                st = self.files.get(path)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if st is None or size < st["offset"]:
                    st = self.files[path] = {"offset": 0, "entries": []}
                if size == st["offset"]:
                    continue
                try:
                    with open(path, "rb") as f:
                        f.seek(st["offset"])
                        chunk = f.read(size - st["offset"])
                except OSError:
                    continue
                end = chunk.rfind(b"\n")
                if end < 0:
                    continue
                st["offset"] += end + 1
                for raw in chunk[:end].splitlines():
                    e = self.parse_line(raw, path)
                    if e and e["ts"] >= since:
                        st["entries"].append(e)
            for path in list(self.files):
                if path not in seen:
                    del self.files[path]
                else:
                    self.files[path]["entries"] = [e for e in self.files[path]["entries"] if e["ts"] >= since]
            return [e for st in self.files.values() for e in st["entries"]]


def _claude_line(raw, path):
    if b'"usage"' not in raw or b'"assistant"' not in raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    u = msg.get("usage")
    ts = _ts(d.get("timestamp"))
    model = msg.get("model") or ""
    if not isinstance(u, dict) or ts is None or not model or model.startswith("<"):
        return None
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    write_1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    write_all = int(u.get("cache_creation_input_tokens") or 0)
    return {
        "ts": ts, "model": model, "key": f'{msg.get("id")}:{d.get("requestId")}',
        "input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "write_5m": max(write_all - write_1h, 0), "write_1h": write_1h,
    }


def _agy_line(raw, path):
    if b'"input_tokens"' not in raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    ts = _ts(d.get("created_at"))
    if ts is None:
        return None
    return {"ts": ts, "key": f'{path.parent.parent.parent.name}:{d.get("step_index")}',
            "input": int(d.get("input_tokens") or 0), "output": int(d.get("output_tokens") or 0),
            "cache_read": int(d.get("cache_read_tokens") or 0)}


_claude_scan = _Scanner(_claude_line)
_agy_scan = _Scanner(_agy_line)


def _recent(paths, since):
    out = []
    for p in paths:
        try:
            if p.stat().st_mtime >= since:
                out.append(p)
        except OSError:
            pass
    return out


def _dedupe(entries):
    seen, out = set(), []
    for e in sorted(entries, key=lambda x: x["ts"]):
        if e["key"] in seen:
            continue
        seen.add(e["key"])
        out.append(e)
    return out


def cost_of(e):
    p = price_for(e.get("model"))
    if not p:
        return None
    i, o, r = p
    return (e["input"] * i + e["output"] * o + e["cache_read"] * r
            + e.get("write_5m", 0) * i * 1.25 + e.get("write_1h", 0) * i * 2.0) / 1e6


def _sum(entries):
    t = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for e in entries:
        t["input"] += e["input"]
        t["output"] += e["output"]
        t["cache_read"] += e["cache_read"]
        t["cache_write"] += e.get("write_5m", 0) + e.get("write_1h", 0)
    t["total"] = sum(t.values())
    return t


def _blocks(entries, now):
    """ccusage-style 5-hour blocks: a block starts on the hour of its first message and lasts 5 h."""
    blocks = []
    for e in entries:
        b = blocks[-1] if blocks else None
        if b is None or e["ts"] >= b["start"] + FIVE_HOURS or e["ts"] - b["last"] >= FIVE_HOURS:
            start = e["ts"] - (e["ts"] % 3600)
            b = {"start": start, "last": e["ts"], "entries": []}
            blocks.append(b)
        b["last"] = e["ts"]
        b["entries"].append(e)
    if blocks and now < blocks[-1]["start"] + FIVE_HOURS:
        return blocks[-1]
    return None


LIMIT_LABELS = {"five_hour": "5-hour session", "seven_day": "Weekly",
                "seven_day_opus": "Weekly · Opus", "seven_day_sonnet": "Weekly · Sonnet",
                "seven_day_fable": "Weekly · Fable"}


def _epoch(v):
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    return _ts(v)


def claude_limits(now):
    """Windows saved by hooks/statusline.py, or (None, reason)."""
    try:
        saved = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ("Claude Code only gives rate limits to status-line scripts. "
                      "Enable hooks/statusline.py to read them (see README).")
    except (OSError, ValueError):
        return None, f"Couldn't read {LIMITS_FILE}."
    rl = saved.get("rate_limits") if isinstance(saved, dict) else None
    if not isinstance(rl, dict):
        return None, "The status line hasn't reported rate limits yet (they appear for claude.ai plans)."
    captured = float(saved.get("captured_at") or 0)
    windows = []
    for key, w in rl.items():
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        resets = _epoch(w.get("resets_at"))
        used, note = float(w["used_percentage"]), ""
        if resets and resets < now:
            used, note = None, "This window reset after the last reading; no new reading yet."
        windows.append({"id": key, "label": LIMIT_LABELS.get(key, key.replace("_", " ").capitalize()),
                        "used_pct": used, "resets_at": resets, "note": note})
    order = list(LIMIT_LABELS)
    windows.sort(key=lambda w: order.index(w["id"]) if w["id"] in order else 99)
    if not windows:
        return None, "The status line reported no rate-limit windows."
    return {"source": "Claude Code status line", "captured_at": captured, "windows": windows}, ""


def claude_section(now):
    since = now - WINDOW_DAYS * 86400
    paths = _recent(CLAUDE_PROJECTS.glob("**/*.jsonl"), since) if CLAUDE_PROJECTS.is_dir() else []
    entries = _dedupe(_claude_scan.scan(paths, since))
    midnight = local_midnight(now)
    today = [e for e in entries if e["ts"] >= midnight]
    week = [e for e in entries if e["ts"] >= now - 7 * 86400]
    by_model = {}
    for e in today:
        m = by_model.setdefault(e["model"], {"model": e["model"], "label": model_label(e["model"]),
                                             "tokens": 0, "cost": 0.0, "priced": True})
        m["tokens"] += e["input"] + e["output"] + e["cache_read"] + e["write_5m"] + e["write_1h"]
        c = cost_of(e)
        if c is None:
            m["priced"] = False
        else:
            m["cost"] += c
    costs = [cost_of(e) for e in today]
    block = _blocks(entries, now)
    limits, why = claude_limits(now)
    return {
        "available": CLAUDE_PROJECTS.is_dir(),
        "source": str(CLAUDE_PROJECTS),
        "tokens_today": _sum(today),
        "cost_today": round(sum(c for c in costs if c is not None), 4) if today else 0.0,
        "cost_partial": any(c is None for c in costs),
        "by_model": sorted(by_model.values(), key=lambda m: -m["tokens"]),
        "tokens_week": _sum(week)["total"],
        "block": None if not block else {
            "start": block["start"], "resets_at": block["start"] + FIVE_HOURS,
            "tokens": _sum(block["entries"])["total"],
            "cost": round(sum(c for c in map(cost_of, block["entries"]) if c is not None), 4)},
        "limits": limits, "limits_reason": why,
        "hourly": _hourly(entries, now),
    }


def _hourly(entries, now):
    """Tokens per hour for the last 24 h (oldest first)."""
    top = now - (now % 3600)
    buckets = [0] * 24
    for e in entries or []:
        i = 23 - int((top - (e["ts"] - e["ts"] % 3600)) // 3600)
        if 0 <= i < 24:
            buckets[i] += e["input"] + e["output"]
    return buckets


# ---- agy: quota via `agy -p /usage`, tokens from Antigravity transcripts ----
_agy_quota = {"at": 0.0, "data": None, "reason": "Reading agy /usage…", "thread": None}
_agy_lock = threading.Lock()


def _parse_agy_usage(out):
    d = json.loads(out)
    groups = []
    for g in ((d.get("command") or {}).get("data") or {}).get("groups") or []:
        buckets = []
        for b in g.get("buckets") or []:
            frac = b.get("remaining_fraction")
            if frac is None:
                continue
            window = b.get("window") or ""
            buckets.append({
                "id": b.get("id") or "", "window": window,
                "label": {"weekly": "Weekly", "5h": "5-hour"}.get(window, b.get("name") or window),
                "remaining_pct": round(float(frac) * 100, 1),
                "used_pct": round((1 - float(frac)) * 100, 1),
                "resets_at": _ts(b.get("reset_time")),
            })
        if buckets:
            models = re.sub(r"^Models within this group:\s*", "", g.get("description") or "")
            groups.append({"name": g.get("name") or "", "models": models[:120], "buckets": buckets})
    return groups


def _refresh_agy_quota(cmd="agy"):
    env = dict(os.environ, JARVIEE_PORT="1", AGENT_HUB_PORT="1")   # any hook the command fires reports nowhere
    env.pop("JARVIEE_RUN_ID", None)
    env.pop("AGENT_HUB_RUN_ID", None)
    try:
        r = subprocess.run([cmd, "-p", "/usage", "--output-format", "json"], capture_output=True,
                           text=True, timeout=20, stdin=subprocess.DEVNULL, env=env)
        groups = _parse_agy_usage(r.stdout)
        data = {"source": "agy -p /usage", "captured_at": time.time(), "groups": groups} if groups else None
        reason = "" if groups else "agy /usage returned no quota buckets."
    except FileNotFoundError:
        data, reason = None, "The agy CLI isn't on PATH."
    except subprocess.TimeoutExpired:
        data, reason = None, "agy /usage didn't answer within 20 s."
    except (OSError, ValueError) as e:
        data, reason = None, f"Couldn't read agy /usage ({type(e).__name__})."
    with _agy_lock:
        if data or not _agy_quota["data"]:
            _agy_quota["data"] = data
        _agy_quota["reason"] = reason
        _agy_quota["at"] = time.time()


def agy_quota(wait=0.0):
    """Cached quota; refreshes in the background when older than CACHE_TTL."""
    with _agy_lock:
        stale = time.time() - _agy_quota["at"] > CACHE_TTL
        t = _agy_quota["thread"]
        if stale and not (t and t.is_alive()):
            t = threading.Thread(target=_refresh_agy_quota, daemon=True)
            _agy_quota["thread"] = t
            t.start()
    if wait and t and t.is_alive() and not _agy_quota["data"]:
        t.join(wait)
    with _agy_lock:
        return _agy_quota["data"], _agy_quota["reason"]


def agy_section(now):
    since = now - WINDOW_DAYS * 86400
    paths = _recent(AGY_BRAIN.glob("*/.system_generated/logs/transcript.jsonl"), since) if AGY_BRAIN.is_dir() else []
    entries = _dedupe(_agy_scan.scan(paths, since))
    today = [e for e in entries if e["ts"] >= local_midnight(now)]
    quota, why = agy_quota(wait=8.0)
    return {"available": AGY_BRAIN.is_dir(), "source": str(AGY_BRAIN),
            "tokens_today": _sum(today), "tokens_week": _sum([e for e in entries if e["ts"] >= now - 7 * 86400])["total"],
            "quota": quota, "quota_reason": why, "hourly": _hourly(entries, now)}


# ---- activity from the agents table ----
FINISHED = ("done", "failed", "stopped")


def activity(store, now):
    rows = store.history(now - 24 * 3600) if store else []
    top = now - (now % 3600)
    hours = [{"t": top - (23 - i) * 3600, "agy": 0, "claude": 0, "other": 0} for i in range(24)]
    finished_today, durations = 0, []
    for a in rows:
        end = a.get("ended")
        if a.get("status") not in FINISHED or not end:
            continue
        if end >= local_midnight(now):
            finished_today += 1
        if end >= now - 24 * 3600:
            i = 23 - int((top - (end - end % 3600)) // 3600)
            if 0 <= i < 24:
                tool = a.get("tool") if a.get("tool") in ("agy", "claude") else "other"
                hours[i][tool] += 1
            if a.get("status") == "done" and a.get("started"):
                durations.append(end - a["started"])
    return {"hours": hours, "finished_today": finished_today,
            "avg_finish_s": round(sum(durations) / len(durations)) if durations else None,
            "finished_24h": sum(h["agy"] + h["claude"] + h["other"] for h in hours)}


# ---- the endpoint ----
_cache = {"at": 0.0, "data": None}
_compute_lock = threading.Lock()


def get_usage(cfg=None, store=None, force=False):
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < CACHE_TTL:
        return {**_cache["data"], "age": round(now - _cache["at"])}
    with _compute_lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < CACHE_TTL:
            return {**_cache["data"], "age": round(time.time() - _cache["at"])}
        now = time.time()
        data = {"generated": now, "ttl": CACHE_TTL, "day_start": local_midnight(now),
                "claude": _safe(claude_section, now), "agy": _safe(agy_section, now),
                "activity": _safe(activity, store, now)}
        _cache.update(at=now, data=data)
        return {**data, "age": 0}


def lane_pressure(data, cfg=None):
    """{tool: reason} for lanes close to their limit, from a get_usage()/cached() dict.

    Claude: a weekly window at or above `usage_routing.claude_weekly_pct` (default 95).
    agy: every quota group has a bucket with nothing left. Missing or unreadable data = no pressure.
    """
    out = {}
    if not isinstance(data, dict):
        return out
    limit = float(((cfg or {}).get("usage_routing") or {}).get("claude_weekly_pct", 95))
    limits = (data.get("claude") or {}).get("limits") or {}
    for w in limits.get("windows") or []:
        used = w.get("used_pct")
        if str(w.get("id", "")).startswith("seven_day") and used is not None and used >= limit:
            out["claude"] = f"{w.get('label') or w['id']} at {used:.0f}%"
            break
    groups = ((data.get("agy") or {}).get("quota") or {}).get("groups") or []
    if groups and all(any(b.get("remaining_pct", 100) <= 0 for b in g.get("buckets") or []) for g in groups):
        out["agy"] = "quota bucket at 0%"
    return out


def agy_model_exhausted(data, model):
    """The quota-group name when `model` belongs to an agy group with a bucket at 0%, else ''.

    A group matches when its name, lower-cased and dashed ("Gemini 4 Argon" -> "gemini-4-argon"),
    appears in the model slug. Missing or unreadable data = not exhausted.
    Hydrogen has no group of its own: agy lists it in "Gemini Models" ("Gemini 4 Hydrogen, Gemini 3.8 Flash").
    """
    if not isinstance(data, dict) or not model:
        return ""
    low = model.lower()
    for g in ((data.get("agy") or {}).get("quota") or {}).get("groups") or []:
        key = re.sub(r"[^a-z0-9.]+", "-", (g.get("name") or "").lower()).strip("-")
        names = [re.sub(r"[^a-z0-9.]+", " ", n.lower()).split() for n in (g.get("models") or "").split(",") if n.strip()]
        hit = (key and key in low) or any(w and all(x in low for x in w) for w in names)
        if hit and any(b.get("remaining_pct", 100) <= 0 for b in g.get("buckets") or []):
            return g.get("name") or key
    return ""


def cached(cfg=None):
    """The last computed usage data, or None. Never reads transcripts or runs agy."""
    return _cache["data"]


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:   # one broken source must not take the endpoint down
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def prewarm(cfg, store):
    """Read the transcripts once in the background so the first /api/usage is quick."""
    threading.Thread(target=lambda: get_usage(cfg, store), daemon=True).start()
