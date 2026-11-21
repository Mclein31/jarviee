"""Run guards: ceilings for unattended runs, all read from `config.json` "guards" with defaults in code.

  max_budget_usd  per level, `claude --max-budget-usd` (print mode only)
  max_turns       per level, `claude --max-turns`; emitted only if the installed CLI lists the flag
  max_minutes     per level, wall-clock cap for any headless run (a watchdog in hub/runjob.py)
  queue           {"max_runs_per_day", "max_cost_usd_per_day"}: the scheduler pauses itself past either
  background_wait_minutes  how long an unattended pane run whose turn ended while its own background task (a
                  backgrounded command, Monitor or background subagent) is still running is kept open for it; past it,
                  the run is finished as if nothing were pending. 0 never waits. Not affected by "enabled": false.

Every value may be a number (all levels), a {level: number} table, or 0 / null / missing to turn it off;
`"guards": {"enabled": false}` turns everything off. Interactive pane sessions never get a guard.
"""
import re
import subprocess
import threading

DEFAULTS = {
    "enabled": True,
    "max_budget_usd": {"basic": 2, "light": 5, "complex": 15, "heavy": 40},
    "max_turns": {"basic": 40, "light": 80, "complex": 150, "heavy": 300},
    "max_minutes": {"basic": 20, "light": 30, "complex": 45, "heavy": 90},
    "queue": {"max_runs_per_day": 150, "max_cost_usd_per_day": 300},
    "background_wait_minutes": 30,
}
CAP_PREFIX = "stopped: over the "
CAP_ENDS = (" min cap", " budget", " turn limit")
# the CLI's own words when it hits a ceiling (print mode): "Error: Exceeded USD budget (15)", "Error: Reached max turns (150)"
LIMIT_RE = re.compile(r"^Error: (Exceeded USD budget|Reached max turns)(?: \(([0-9.]+)\))?\s*$", re.M)
_flag_cache = {}
_flag_lock = threading.Lock()


def settings(cfg):
    g = (cfg or {}).get("guards")
    g = g if isinstance(g, dict) else {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in g.items():
        if k == "queue" and isinstance(v, dict):
            out["queue"] = {**out["queue"], **v}
        else:
            out[k] = v
    return out


def for_level(cfg, key, level):
    """The number for `key` at `level`, or None when that guard is off."""
    g = settings(cfg)
    if not g.get("enabled", True):
        return None
    v = g.get(key)
    if isinstance(v, dict):
        v = v.get(level)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def background_wait_seconds(cfg):
    """Seconds an unattended pane run may wait for its own background tasks after its turn ended (0: no wait).
    A cap, so a run can never hang on a task that never reports back; read even with "enabled": false, since
    turning the guards off must not make such a run wait forever."""
    v = settings(cfg).get("background_wait_minutes")
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = float(DEFAULTS["background_wait_minutes"])
    return v * 60.0 if v > 0 else 0.0


def _level(p):
    return (p or {}).get("level") or (p or {}).get("tier") or ""


def supports_flag(binary, flag):
    """Does `<binary> --help` list `flag`? Asked once per binary and flag (help starts no session)."""
    key = (binary, flag)
    with _flag_lock:
        if key in _flag_cache:
            return _flag_cache[key]
    try:
        out = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        ok = flag in (out.stdout + out.stderr)
    except (OSError, subprocess.SubprocessError):
        ok = False
    with _flag_lock:
        _flag_cache[key] = ok
    return ok


def headless_argv(cfg, p, argv):
    """`argv` plus the budget / turn ceilings when it is a headless Claude print run, else unchanged."""
    from .runplan import append_flags
    if not argv or (p or {}).get("tool") != "claude" or not ({"-p", "--print"} & set(argv)):
        return argv
    out = list(argv)
    budget = for_level(cfg, "max_budget_usd", _level(p))
    if budget and "--max-budget-usd" not in out:
        out = append_flags(out, ["--max-budget-usd", f"{budget:g}"])
    turns = for_level(cfg, "max_turns", _level(p))
    if turns and "--max-turns" not in out and supports_flag(out[0], "--max-turns"):
        out = append_flags(out, ["--max-turns", str(int(turns))])
    return out


def wall_clock(cfg, p):
    """(seconds, note) of the wall-clock cap for a headless run of this level, or (None, '')."""
    m = for_level(cfg, "max_minutes", _level(p))
    if not m:
        return None, ""
    return m * 60.0, f"{CAP_PREFIX}{m:g} min cap"


def is_cap_note(text):
    """A run's own ceiling (wall clock, budget, turns) ended it: no retry, bridge, fallback or escalation."""
    t = str(text or "")
    return t.startswith(CAP_PREFIX) and t.endswith(CAP_ENDS)


def limit_note(cfg, p, res=None, log_text=""):
    """The note of a run that stopped on its budget or turn ceiling, or ''. The structured result's subtype decides
    when there is one (`error_max_budget_usd`, `error_max_turns`); else the CLI's own error line at the end of the log."""
    kind = ""
    sub = str((res or {}).get("subtype") or "")
    if sub == "error_max_budget_usd":
        kind = "budget"
    elif sub == "error_max_turns":
        kind = "turns"
    amount = None
    if not kind:
        m = None
        for m in LIMIT_RE.finditer(str(log_text or "")[-800:]):
            pass
        if m is None:
            return ""
        kind = "budget" if m.group(1).startswith("Exceeded") else "turns"
        try:
            amount = float(m.group(2)) if m.group(2) else None
        except ValueError:
            amount = None
    if amount is None:
        amount = for_level(cfg, "max_budget_usd" if kind == "budget" else "max_turns", _level(p))
    if kind == "budget":
        return f"{CAP_PREFIX}${amount:g} budget" if amount else f"{CAP_PREFIX}budget"
    return f"{CAP_PREFIX}{int(amount)} turn limit" if amount else f"{CAP_PREFIX}turn limit"


def queue_limits(cfg):
    """(max queue runs per day, max Claude dollars per day); 0 means off."""
    g = settings(cfg)
    if not g.get("enabled", True):
        return 0, 0.0
    q = g.get("queue") if isinstance(g.get("queue"), dict) else {}

    def num(k):
        try:
            return max(float(q.get(k) or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0
    return int(num("max_runs_per_day")), num("max_cost_usd_per_day")


def day_runs(task_rows, midnight):
    """Queue runs started today: every task with a start time since local midnight (finished and running)."""
    return sum(1 for t in task_rows if float(t.get("started") or 0) >= midnight)


def claude_cost_today(usage_data, midnight):
    """Estimated Claude spend today from the cached usage data, or None when it is missing or from another day."""
    try:
        if not usage_data or float(usage_data.get("day_start") or 0) != float(midnight):
            return None
        return float((usage_data.get("claude") or {}).get("cost_today") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return None


def ceiling_reason(cfg, runs, cost, ack_runs=0, ack_cost=0.0):
    """Why the queue must pause, or ''. `ack_*` are the day's figures when the owner last resumed."""
    max_runs, max_cost = queue_limits(cfg)
    if max_runs and runs - ack_runs >= max_runs:
        return (f"queue ceiling: {runs - ack_runs} queue runs started today (limit {max_runs}); "
                f"resume to allow another {max_runs}")
    if max_cost and cost is not None and cost - ack_cost >= max_cost:
        return (f"queue ceiling: about ${cost - ack_cost:.0f} of Claude usage today (limit ${max_cost:g}); "
                f"resume to allow another ${max_cost:g}")
    return ""
