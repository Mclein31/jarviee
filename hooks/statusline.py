#!/usr/bin/env python3
"""Claude Code status line that also saves your plan's rate limits for Jarviee.

Claude Code passes `rate_limits` (5-hour and 7-day windows with `used_percentage` and
`resets_at`) only to status-line scripts. This one writes them to
~/.jarviee/claude-limits.json, which hub/usage.py reads for the Usage card, and prints a
short status line. Enable it in ~/.claude/settings.json:

  "statusLine": {"type": "command", "command": "python3 /path/to/agent-hub/hooks/statusline.py"}

Never fails loudly: any error still prints a line and exits 0.
"""
import json
import os
import sys
import tempfile
import time
from urllib.parse import urlparse

_HOME = os.path.expanduser("~/.jarviee")
if not os.path.exists(_HOME) and os.path.exists(os.path.expanduser("~/.agent-hub")):
    _HOME = os.path.expanduser("~/.agent-hub")   # not migrated yet (the server moves it on its next start)
OUT = os.environ.get("JARVIEE_CLAUDE_LIMITS", os.environ.get("AGENT_HUB_CLAUDE_LIMITS", os.path.join(_HOME, "claude-limits.json")))


def _is_anthropic_host(url):
    u = str(url or "").strip()
    if not u:
        return True
    if "://" not in u:
        u = "//" + u
    host = (urlparse(u).hostname or "").lower()
    if not host:
        return False
    return host == "anthropic.com" or host.endswith(".anthropic.com")


def is_omniroute_session(environ=None):
    """True when this Claude Code session is routed through OmniRoute or another non-Anthropic host."""
    env = os.environ if environ is None else environ
    if (env.get("JARVIEE_TOOL") or "").strip().lower() == "omniroute":
        return True
    base = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    return bool(base) and not _is_anthropic_host(base)


def _reset(w):
    v = (w or {}).get("resets_at")
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


FRESH_S = 120   # a session whose transcript changed this recently has just talked to the API


def fresh(d, now=None):
    """True when this status-line call comes from a session that is working right now (its transcript was
    written in the last FRESH_S seconds), so its numbers are the API's current ones."""
    p = (d or {}).get("transcript_path")
    try:
        return bool(p) and (now or time.time()) - os.path.getmtime(p) <= FRESH_S
    except (OSError, TypeError):
        return False


def merge(old, new, is_fresh=False):
    """Every Claude Code session runs this script, and an idle one keeps re-sending the numbers it last
    got. So per window: a later reset time wins (new window), an earlier one is stale and ignored, and within
    the same window the higher percentage wins, unless the report comes from a session working right now
    (`is_fresh`): that is the current figure even when it is lower. Without this a single 100% stuck for the rest
    of the week after the real usage went down (limit raised or reset early; owner, 2026-11-15)."""
    out = dict(old or {})
    for key, w in (new or {}).items():
        if not isinstance(w, dict):
            continue
        cur = out.get(key)
        if not isinstance(cur, dict) or cur.get("used_percentage") is None:
            out[key] = w
            continue
        rn, rc = _reset(w), _reset(cur)
        if rn > rc + 60:
            out[key] = w
        elif abs(rn - rc) <= 60 and (is_fresh or (w.get("used_percentage") or 0) > (cur.get("used_percentage") or 0)):
            out[key] = w
    return out


def save(rate_limits, is_fresh=False):
    if is_omniroute_session():
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f).get("rate_limits")
    except (OSError, ValueError, AttributeError):
        old = None
    merged = merge(old, rate_limits, is_fresh)
    if old == merged and old is not None:
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix=".limits-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"captured_at": time.time(), "rate_limits": merged}, f)
    os.replace(tmp, OUT)


def bar(left, width=10):
    n = max(0, min(width, round(width * left / 100)))
    return "▰" * n + "▱" * (width - n)


def _seg(label, used):
    left = 100 - round(float(used))
    return f"{label} {bar(left)} {left}%"


def agy_groups():
    """agy quota groups from the running Jarviee server (cached 30 s in a file); [] when unavailable."""
    cache = os.path.join(_HOME, "statusline-agy.json")
    try:
        if time.time() - os.path.getmtime(cache) < 30:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    groups = []
    try:
        import urllib.request
        port = os.environ.get("JARVIEE_PORT", "7777")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/usage", timeout=0.6) as r:
            groups = (((json.load(r).get("agy") or {}).get("quota") or {}).get("groups")) or []
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(groups, f)
    except Exception:
        try:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return groups


def _group_line(label, g):
    segs = []
    for win, short in (("5h", "5h"), ("weekly", "wk")):
        for b in g.get("buckets") or []:
            if b.get("window") == win and b.get("used_pct") is not None:
                segs.append(_seg(short, b["used_pct"]))
    return f"{label}  " + "  ".join(segs) if segs else ""


def line(d):
    """Remaining quota as line bars: Claude, agy (default) and agy Argon, 5-hour and weekly. Model shown first."""
    model = (d.get("model") or {}).get("display_name") or ""
    out = [model] if model else []
    rl = d.get("rate_limits") or {}
    if not is_omniroute_session():
        segs = [_seg(short, rl[key]["used_percentage"]) for key, short in (("five_hour", "5h"), ("seven_day", "wk"))
                if isinstance(rl.get(key), dict) and rl[key].get("used_percentage") is not None]
        if segs:
            out.append("Claude  " + "  ".join(segs))
    groups = agy_groups()
    default = next((g for g in groups if "argon" not in (g.get("name") or "").lower()), None)
    argon = next((g for g in groups if "argon" in (g.get("name") or "").lower()), None)
    for label, g in (("agy", default), ("argon", argon)):
        if g and _group_line(label, g):
            out.append(_group_line(label, g))
    return "\n".join(out)


def main():
    try:
        d = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        d = {}
    if not is_omniroute_session() and isinstance(d.get("rate_limits"), dict) and d["rate_limits"]:
        try:
            save(d["rate_limits"], fresh(d))
        except OSError:
            pass
    print(line(d))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("")
    sys.exit(0)
