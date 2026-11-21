"""The slash commands of the dashboard command bar, in one registry (GET /api/commands lists it).

Each command has a name, aliases, an argument hint, a one-line summary and two flags: `launches_agents` (it starts
an agent or a team) and `may_use_many_tokens` (it can spend a lot of quota). parse(text) -> (command or None, rest).

The older commands keep their own detection and handling (hub/briefing.py, hub/daybook.py, hub/pipeline.py,
hub/orchestrate.py and routes.api_run); the registry delegates to them. The ones with `answers=True` (/help, /status,
/standup, /usage, /map) are answered here by answer(): no model calls, and only /map launches an agent.
"""
import difflib
import re
import time
from datetime import datetime, timedelta

from . import alerts, briefing, daybook, launcher, mapwatch, obsidian, orchestrate, pipeline, projmap, standup, usage

ANSWER_STATUS_WINDOW_S = 3600   # /status: "failed" counts the last hour
TOOL_LABELS = {"agy": "Antigravity", "claude": "Claude Code"}


class Command:
    def __init__(self, name, summary, args="", aliases=(), launches_agents=False, may_use_many_tokens=False,
                 answers=False, match=None):
        self.name, self.summary, self.args, self.aliases = name, summary, args, tuple(aliases)
        self.launches_agents, self.may_use_many_tokens, self.answers = launches_agents, may_use_many_tokens, answers
        self._match = match   # (text) -> end offset of the command word, or None: the older commands' own detection
        names = "|".join(re.escape(n) for n in (name,) + self.aliases)
        self._re = re.compile(rf"^\s*/(?:{names})(?![\w-])[\s:,-]*", re.I)

    def end_of(self, text):
        """Offset where the arguments start, or None when `text` does not start with this command."""
        if self._match:
            return self._match(text)
        m = self._re.match(text or "")
        return m.end() if m else None

    def info(self):
        return {"name": self.name, "aliases": list(self.aliases), "args": self.args, "summary": self.summary,
                "launches_agents": self.launches_agents, "may_use_many_tokens": self.may_use_many_tokens}


def _delegate(is_cmd, regex):
    def match(text):
        return regex.match(text).end() if is_cmd(text) else None
    return match


COMMANDS = (
    Command("help", "List every slash command with its arguments.", answers=True),
    Command("status", "The fleet right now: running, needs you, failed in the last hour and queued tasks, per tool.",
            answers=True),
    Command("standup", "What the agents did on a day (today by default), per project, with the open owner items.",
            args="[YYYY-MM-DD]", answers=True),
    Command("usage", "Claude Code and agy quota and token use, from the last reading (never waits for a new one).",
            answers=True),
    Command("map", "Start the agent that maps one project (.brain/FLOW.json). A first map reads the whole project.",
            args="<project>", launches_agents=True, may_use_many_tokens=True, answers=True),
    Command("startwork", "Your briefing for the day: calendar, mail and other connected sources. Spoken, sent to the phone.",
            aliases=("startwokr",), match=_delegate(briefing.is_startwork, briefing.STARTWORK_RE)),
    Command("endwork", "Today's report: what the agents did, what is waiting for you. Read out loud.",
            aliases=("endowrk",), match=_delegate(daybook.is_endwork, daybook.ENDWORK_RE)),
    Command("pipeline", "Run the task as research, plan, build, test and review, each phase its own agent.",
            args="<task>", launches_agents=True, may_use_many_tokens=True,
            match=lambda t: (lambda m: m.end() if m else None)(pipeline.PREFIX_RE.match(t or ""))),
    Command("orchestrate", "Opus leads a team of up to 7 workers in a new iTerm2 tab, each on the tool and model its job needs.",
            args="<task>", launches_agents=True, may_use_many_tokens=True,
            match=lambda t: (lambda m: m.end() if m else None)(orchestrate.PREFIX_RE.match(t or ""))),
)
BY_NAME = {c.name: c for c in COMMANDS}


# Typo aliases that keep working but are not listed in /help or GET /api/commands.
HIDDEN_ALIASES = frozenset({"startwokr", "endowrk"})
_WORD_RE = re.compile(r"^\s*/([A-Za-z][\w-]*)(?=\s|$)")


def _visible(c):
    return tuple(a for a in c.aliases if a not in HIDDEN_ALIASES)


def registry():
    out = []
    for c in COMMANDS:
        i = c.info()
        i["aliases"] = list(_visible(c))
        out.append(i)
    return out


def _unknown_handler(word):
    def handler(cfg, store, tasks, sched, rest):
        names = [n for c in COMMANDS for n in (c.name,) + _visible(c)]
        near = difflib.get_close_matches(word.lower(), names + list(HIDDEN_ALIASES), n=1, cutoff=0.6)
        if near and near[0] in HIDDEN_ALIASES:
            near = [c.name for c in COMMANDS if near[0] in c.aliases]
        hint = f" Did you mean /{near[0]}?" if near else ""
        return 400, {"error": f"Unknown command /{word}.{hint} Type /help for the list."}
    return handler


def _unknown(word):
    c = Command("unknown", "", answers=True)
    c.word = word
    return c


def parse(text):
    """(command, rest): the command the text starts with and the text after it, or (None, text)."""
    text = str(text or "")
    for c in COMMANDS:
        end = c.end_of(text)
        if end is not None:
            return c, text[end:].strip()
    m = _WORD_RE.match(text)
    if m:   # "/hepl": looks like a command word (a path like /src/app.py or /Users/x does not match)
        return _unknown(m.group(1)), text[m.end():].strip()
    return None, text


# ---------------------------------------------------------------- answers

def _age(seconds):
    s = max(0, int(seconds))
    if s < 90:
        return "just now" if s < 10 else f"{s} s ago"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 172800:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def _in(seconds):
    s = int(seconds)
    if s <= 0:
        return "now"
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def _tool_label(tool):
    return TOOL_LABELS.get(tool, tool or "other")


def _help(cfg, store, tasks, sched, rest):
    rows = []
    for c in COMMANDS:
        head = f"/{c.name}" + (f" {c.args}" if c.args else "")
        flag = " (starts an agent)" if c.launches_agents else ""
        rows.append((head, c.summary + flag))
    w = max(len(h) for h, _ in rows)
    text = "Slash commands:\n" + "\n".join(f"  {h.ljust(w)}  {s}" for h, s in rows)
    return 200, {"text": text, "commands": registry()}


def fleet(cfg, store, tasks=None, sched=None, now=None):
    """Counts per tool from the agents table and the queue. No model call, no file or process reads."""
    now = now if now is not None else time.time()
    per = {}

    def row(tool):
        return per.setdefault(tool or "other", {"running": 0, "needs_you": 0, "failed_1h": 0, "queued": 0})
    needs = []
    for a in store.all():
        st = a.get("status")
        if st in launcher.LIVE and a.get("source") != "launcher":   # mirrors dashstate: silent for 2 h = stale
            if now - (a.get("updated") or a.get("started") or a.get("created") or 0) > 7200:
                continue
        if st == "running":
            row(a.get("tool"))["running"] += 1
        elif st == "waiting" and alerts.section_of(st, a.get("activity")) == "needs":
            row(a.get("tool"))["needs_you"] += 1
            needs.append({"id": a.get("id"), "tool": a.get("tool"), "title": a.get("title") or (a.get("task") or "")[:60]})
        elif st == "failed" and (a.get("ended") or a.get("updated") or 0) >= now - ANSWER_STATUS_WINDOW_S:
            row(a.get("tool"))["failed_1h"] += 1
    if tasks:
        for t in tasks.list(statuses=("queued", "blocked")):
            row(t.get("tool"))["queued"] += 1
    total = {k: sum(r[k] for r in per.values()) for k in ("running", "needs_you", "failed_1h", "queued")}
    return {"total": total, "tools": per, "needs_you": needs,
            "paused": bool(sched and getattr(sched, "paused", False)),
            "paused_reason": getattr(sched, "paused_reason", "") if sched else ""}


def _status(cfg, store, tasks, sched, rest):
    f = fleet(cfg, store, tasks, sched)
    t = f["total"]
    lines = [f"Fleet now: {t['running']} running, {t['needs_you']} need you, {t['failed_1h']} failed in the last hour, "
             f"{t['queued']} queued."]
    for tool, r in sorted(f["tools"].items()):
        lines.append(f"  {_tool_label(tool)}: {r['running']} running, {r['needs_you']} need you, "
                     f"{r['failed_1h']} failed (1 h), {r['queued']} queued")
    if not f["tools"]:
        lines.append("  Nothing is running and nothing is queued.")
    if f["needs_you"]:
        lines.append("Needs you:")
        lines += [f"  - {n['title']} ({_tool_label(n['tool'])})" for n in f["needs_you"][:8]]
    if f["paused"]:
        lines.append("The queue is paused" + (f": {f['paused_reason']}" if f["paused_reason"] else "."))
    return 200, {"text": "\n".join(lines), "fleet": f}


def _standup(cfg, store, tasks, sched, rest):
    arg = rest.strip().lower()
    if arg in ("", "today"):
        date = None
    elif arg == "yesterday":
        date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
        date = arg
    else:
        return 400, {"error": "/standup takes a date as YYYY-MM-DD (or yesterday): /standup 2026-01-31"}
    try:
        s = standup.standup(cfg, store, date)
    except ValueError:
        return 400, {"error": "that is not a real date; use YYYY-MM-DD"}
    return 200, {"text": standup.render(s), "standup": s}


def _window_line(w):
    if w.get("used_pct") is None:
        return f"  {w['label']}: {w.get('note') or 'no reading'}"
    reset = f", resets in {_in(w['resets_at'] - time.time())}" if w.get("resets_at") else ""
    return f"  {w['label']}: {w['used_pct']:.0f}% used{reset}"


def _usage(cfg, store, tasks, sched, rest):
    data = usage.cached(cfg)   # never get_usage(): it can wait on `agy -p /usage`
    if not data:
        return 200, {"text": "No usage reading yet: Jarviee is still reading the transcripts. Try /usage again in a few seconds.",
                     "usage": None}
    now = time.time()
    lines = [f"Usage, read {_age(now - (data.get('generated') or now))}."]
    cl = data.get("claude") or {}
    lines.append("Claude Code:")
    if cl.get("error"):
        lines.append(f"  could not be read ({cl['error']})")
    else:
        lim = cl.get("limits") or {}
        lines += [_window_line(w) for w in lim.get("windows") or []]
        if lim.get("captured_at"):
            lines.append(f"  (limits last reported {_age(now - lim['captured_at'])} by the status line)")
        elif cl.get("limits_reason"):
            lines.append(f"  limits: {cl['limits_reason']}")
        tt = (cl.get("tokens_today") or {}).get("total")
        if tt is not None:
            cost = f", about ${cl.get('cost_today', 0):.2f}" + (" (partly unpriced)" if cl.get("cost_partial") else "")
            lines.append(f"  today: {tt:,} tokens{cost}")
        if cl.get("tokens_week") is not None:
            lines.append(f"  last 7 days: {cl['tokens_week']:,} tokens")
    ag = data.get("agy") or {}
    lines.append("agy:")
    if ag.get("error"):
        lines.append(f"  could not be read ({ag['error']})")
    else:
        q = ag.get("quota") or {}
        for g in q.get("groups") or []:
            parts = ", ".join(f"{b['label']} {b['used_pct']:.0f}% used"
                              + (f" (resets in {_in(b['resets_at'] - now)})" if b.get("resets_at") else "")
                              for b in g.get("buckets") or [])
            lines.append(f"  {g.get('name') or 'quota'}: {parts}")
        if q.get("captured_at"):
            lines.append(f"  (quota read {_age(now - q['captured_at'])})")
        elif ag.get("quota_reason"):
            lines.append(f"  quota: {ag['quota_reason']}")
        at = (ag.get("tokens_today") or {}).get("total")
        if at is not None:
            lines.append(f"  today: {at:,} tokens")
    return 200, {"text": "\n".join(lines), "usage": {"generated": data.get("generated"), "claude": cl, "agy": ag}}


def project_matches(cfg, name):
    """Project names close to `name`: substring hits first, then near spellings."""
    names = [p["name"] for p in obsidian.list_projects(cfg)]
    low, by_low = name.lower(), {n.lower(): n for n in names}
    hits = [n for k, n in by_low.items() if low and (low in k or k in low)]
    near = [by_low[k] for k in difflib.get_close_matches(low, list(by_low), n=5, cutoff=0.5)]
    out = []
    for n in hits + near:
        if n not in out:
            out.append(n)
    return out[:5], names


def generate_map(cfg, store, proj, tool=None):
    """Launch the agent that writes <project>/.brain/FLOW.json (what "Map this project" does). (run id, plan)."""
    return launcher.launch(projmap.brief(proj.name), cfg, store, tool, "complex", proj.name, None, log_runs=False,
                           on_finish=lambda r, st, code: mapwatch.finish(cfg, proj, r, st, None, "", "create"))


def _map(cfg, store, tasks, sched, rest):
    name = rest.strip()
    if not name:
        return 400, {"error": "/map needs a project: /map <project>"}
    proj = obsidian.find_project(cfg, name)
    if not proj:
        close, names = project_matches(cfg, name)
        if close:
            hint = "Did you mean: " + ", ".join(close) + "?"
        else:
            hint = ("Projects with a .brain/ folder: " + ", ".join(names[:10]) + ".") if names else "No project has a .brain/ folder yet."
        return 400, {"error": f"no project '{name}' with a .brain/ folder. {hint}", "close_matches": close}
    warn = ("A first map reads the whole project and can use many tokens. "
            "It is committed to the project's .brain/FLOW.json when it passes the checks.")
    if projmap.flow_mtime(proj, cfg):   # there is a map already: bring it up to date, as the Maps view's update button does
        ok, msg, rid = mapwatch.launch(cfg, store, proj.name, force=True)
        if not ok:
            return 200, {"text": msg, "launched": False, "project": proj.name}
        return 200, {"text": msg + " It only reads what changed, and the old map is kept if the new one is broken.",
                     "launched": True, "id": rid, "project": proj.name, "mode": "update"}
    brief = projmap.brief(proj.name)
    if any(a.get("status") in launcher.LIVE and a.get("task") == brief for a in store.all()):
        return 200, {"text": f"A map agent is already working on {proj.name}.", "launched": False, "project": proj.name}
    rid, p = generate_map(cfg, store, proj)
    return 200, {"text": f"Mapping {proj.name}: an agent is reading the project. {warn}", "warning": warn,
                 "launched": True, "id": rid, "project": proj.name, "mode": "create",
                 "tool": p.get("tool"), "model": p.get("model")}


HANDLERS = {"help": _help, "status": _status, "standup": _standup, "usage": _usage, "map": _map}


def answer(cmd, rest, cfg, store, tasks=None, sched=None):
    """(status, payload) for a command the registry answers itself. The payload carries `slash` (the name) and `text`."""
    if cmd.name == "unknown":
        return _unknown_handler(cmd.word)(cfg, store, tasks, sched, rest)
    code, out = HANDLERS[cmd.name](cfg, store, tasks, sched, rest)
    return code, ({"slash": cmd.name, **out} if code == 200 else out)
