"""Apple Calendar answers for JARVIS ("what's on today?", "what's coming next week?").

Events come from hub/calendar_events.swift (EventKit), built once into ~/.jarviee/JarvieeCalendar.app.
Read-only. Only titles, times and places are read, and nothing leaves the machine.
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).with_name("calendar_events.swift")

_SCHEDULE_RE = re.compile(r"(?i)\b(schedule|calendar|agenda|appointments?|meetings?|classes|class|events?|(?:what'?s|what is|what are)(?: \w+){0,2} (?:on|up|coming|happening)|coming up|upcoming|am i (?:free|busy)|do i have)\b")
# requests to build or change something that merely mention the calendar ("notify me 15 minutes before a
# schedule") go to an agent, not to the calendar reader
_NOT_CODE_RE = re.compile(r"(?i)\b(fix|implement|refactor|add|build|write|create|bug|test|deploy|automate|"
                          r"notif\w*|send|set ?up|make|change|update|plan how|i want|i'?d like)\b")


class CalendarError(RuntimeError):
    pass


DEFAULTS = {"enabled": False}
DISABLED_MSG = "not connected (turn it on: calendar.enabled)"


def is_enabled(cfg=None):
    """Apple Calendar / Reminders is opt-in (calendar.enabled in config)."""
    if cfg is None:
        try:
            from . import config
            base = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = config.deep_merge(base, config.read_user_config())
        except Exception:
            return False
    cal = (cfg or {}).get("calendar")
    return bool(isinstance(cal, dict) and cal.get("enabled", False))


def is_schedule_question(text):
    t = str(text or "").strip()
    return bool(t) and len(t) < 200 and bool(_SCHEDULE_RE.search(t)) and not _NOT_CODE_RE.search(t)


def window(question, now=None):
    """(start, end, label): only today and tomorrow are ever read out ("tomorrow" alone gives just tomorrow)."""
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "tomorrow" in str(question or "").lower():
        return today + timedelta(days=1), today + timedelta(days=2), "tomorrow"
    return today, today + timedelta(days=2), "today and tomorrow"


_DUE_RE = re.compile(r"(?i)\b(assign|homework|\bhw\b|due|submit|submission|deadline|project|paper|essay|report|lab|quiz|exam|defen[cs]e|activity|case study)")
_SUB_RE = re.compile(r"(?i)(subscri|renew|expire|expiry|trial|billing|membership|premium|plan\b|payment|\bbill\b|netflix|spotify|icloud|youtube|chatgpt|claude|github|adobe)")
DUE_DAYS, SUB_DAYS = 3, 5   # assignments due within 3 days, subscriptions ending within 5
ALERT_DAYS = max(DUE_DAYS, SUB_DAYS)   # how far ahead to read so every heads-up is seen


_SESSION_RE = re.compile(r"(?i)\((?:lab|lecture|lec|laboratory)\)")
_VERB_RE = re.compile(r"(?i)\b(renews?|renewal|expires?|ends?|due|payment|bill)\b")


def alerts(events, now=None):
    """Assignments due within 3 days and subscriptions ending within 2 days (from Reminders, or calendar events
    that read like them). Overdue ones are included. Sorted soonest first. Each is {'kind','title','when','late'}."""
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for e in events:
        if e.get("kind") not in ("reminder", "event"):
            continue
        try:
            d = _local(e["start"])
        except (KeyError, ValueError):
            continue
        title = e.get("title") or ""
        days = (d.replace(hour=0, minute=0) - today).days
        sub = bool(_SUB_RE.search(title + " " + (e.get("calendar") or "")))
        # a class session's "(Lab)" / "(Lecture)" tag is not an assignment
        if e.get("kind") == "event" and not (_DUE_RE.search(_SESSION_RE.sub("", title)) or sub):
            continue   # ordinary classes are not alerts
        kind = "subscription" if sub else "due"
        if days > (SUB_DAYS if sub else DUE_DAYS) or days < -7:   # stale leftovers older than a week stay quiet
            continue
        when = "OVERDUE" if days < 0 else "today" if days == 0 else "tomorrow" if days == 1 else d.strftime("on %A") + f" (in {days} days)"
        if e.get("kind") == "reminder" and not e.get("all_day") and days >= 0:
            when += f" at {_clock(d)}"
        out.append({"kind": kind, "title": title, "when": when, "late": days < 0, "days": days})
    return sorted(out, key=lambda a: a["days"])


def alert_lines(items):
    lines = []
    for a in items:
        if a["kind"] == "subscription":
            if _VERB_RE.search(a["title"]):   # "Claude subscription renews", "Spotify renewal": the title already says what happens
                lines.append(f"{a['title']} {a['when']}." if not a["late"] else f"{a['title']}: that date has passed.")
                continue
            name = a["title"] if re.search(r"(?i)subscri", a["title"]) else f"{a['title']} subscription"   # not "Netflix subscription subscription"
            lines.append(f"Your {name} ends {a['when']}." if not a["late"] else f"{name} has ended.")
        else:
            lines.append(f"{a['title']} is due {a['when']}." if not a["late"] else f"{a['title']} is OVERDUE.")
    return lines


APP = Path.home() / ".jarviee" / "JarvieeCalendar.app"
_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.jarviee.calendar</string>
<key>CFBundleName</key><string>Jarviee Calendar</string>
<key>CFBundleExecutable</key><string>calendar_events</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSCalendarsUsageDescription</key><string>Jarviee reads your calendar to answer questions about your schedule.</string>
<key>NSCalendarsFullAccessUsageDescription</key><string>Jarviee reads your calendar to answer questions about your schedule.</string>
<key>NSRemindersUsageDescription</key><string>Jarviee reads your reminders to warn you about assignments and subscriptions that are due soon.</string>
<key>NSRemindersFullAccessUsageDescription</key><string>Jarviee reads your reminders to warn you about assignments and subscriptions that are due soon.</string>
</dict></plist>
"""


def _src_hash():
    return hashlib.sha256(SRC.read_bytes()).hexdigest()


_BUILD_LOCK = threading.Lock()


def build_app():
    """Wrap the reader in its own .app so macOS asks for Calendar access for it (not for whatever launched the service).

    Rebuilt only when the Swift source's *content* changes: every rebuild is ad-hoc signed with a new code hash, which
    macOS treats as a new app and asks for Calendar and Reminders again. An mtime check rebuilt on git checkouts too."""
    with _BUILD_LOCK:  # the push watcher and /api/ask can both arrive right after the source changes
        exe = APP / "Contents" / "MacOS" / "calendar_events"
        stamp = APP / "Contents" / "Resources" / "source.sha256"
        want = _src_hash()
        if exe.is_file() and stamp.is_file() and stamp.read_text().strip() == want:
            return
        if not shutil.which("swiftc"):
            raise CalendarError("swiftc is missing, so the calendar reader can't be built")
        exe.parent.mkdir(parents=True, exist_ok=True)
        (APP / "Contents" / "Info.plist").write_text(_PLIST)
        r = subprocess.run(["swiftc", "-O", str(SRC), "-o", str(exe)], capture_output=True, text=True, timeout=180)
        if r.returncode:
            raise CalendarError("could not build the calendar reader")
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(want)
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(APP)], capture_output=True, timeout=60)


def _run_app(argv):
    """`open -W` makes the app its own responsible process, so its own Calendar permission applies."""
    back, ahead = argv[1], argv[2]
    with tempfile.TemporaryDirectory(prefix="jarviee-cal-") as d:
        out = Path(d) / "out.json"
        subprocess.run(["open", "-W", "-n", "-a", str(APP), "--args", back, ahead, str(out)], capture_output=True, timeout=120)
        text = out.read_text() if out.is_file() else ""
    denied = text.strip().startswith("{")
    return SimpleNamespace(returncode=2 if denied else (0 if text else 1), stdout=text)


def apply(spec, runner=None, cfg=None):
    """Write to Calendar through the same app (same permission): create calendars / events, set how long series repeat.
    See `applySpec` in calendar_events.swift for the spec. Returns the app's report."""
    if not is_enabled(cfg):
        raise CalendarError(DISABLED_MSG)
    if runner is None:
        build_app()
        runner = _run_apply
    return runner(spec)


def _run_apply(spec):
    with tempfile.TemporaryDirectory(prefix="jarviee-cal-") as d:
        sp, out = Path(d) / "spec.json", Path(d) / "out.json"
        sp.write_text(json.dumps(spec))
        subprocess.run(["open", "-W", "-n", "-a", str(APP), "--args", "apply", str(sp), str(out)], capture_output=True, timeout=300)
        try:
            return json.loads(out.read_text())
        except (OSError, ValueError):
            raise CalendarError("the calendar writer returned nothing")


def app_built():
    return (APP / "Contents" / "MacOS" / "calendar_events").is_file()


def fetch(days_back=0, days_ahead=15, runner=None, build=True, cfg=None):
    """`build=False` (the parse request) never runs swiftc: a missing reader is reported instead."""
    if not is_enabled(cfg):
        raise CalendarError(DISABLED_MSG)
    if runner is None:
        if build:
            build_app()
        elif not app_built():
            raise CalendarError("the calendar reader is not built yet (it is built the first time you add an event or ask about your schedule)")
        runner = _run_app
    r = runner([str(APP), str(days_back), str(days_ahead)])
    if r.returncode == 2:
        raise CalendarError("Calendar access is off for Jarviee Calendar. Allow it in System Settings → Privacy & Security → Calendars.")
    if r.returncode:
        raise CalendarError("the calendar reader failed")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise CalendarError("the calendar reader returned something unreadable")


def _local(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=__import__("datetime").timezone.utc).astimezone().replace(tzinfo=None)


def events_between(events, lo, hi):
    out = []
    for e in events:
        try:
            s, en = _local(e["start"]), _local(e["end"])
        except (KeyError, ValueError):
            continue
        if s < hi and en > lo and not (en == lo):
            out.append({**e, "_s": s, "_e": en})
    return sorted(out, key=lambda e: e["_s"])


def _clock(d):
    h = d.hour % 12 or 12
    mer = "am" if d.hour < 12 else "pm"
    return f"{h} {mer}" if d.minute == 0 else f"{h}:{d.minute:02d} {mer}"


def render(events, label, lo, now=None):
    """Events that have already ended are left out; one that has started but not ended is read as ongoing."""
    now = now or datetime.now()
    events = [e for e in events if e["_e"] > now or e.get("all_day") and e["_e"].date() > now.date()]
    if not events:
        return f"Nothing left on your calendar for {label}."
    days = {}
    for e in events:
        days.setdefault(max(e["_s"], lo).date(), []).append(e)
    lines = []
    for day, evs in days.items():
        lines.append(("Today" if day == now.date() else "Tomorrow" if day == now.date() + timedelta(days=1) else day.strftime("%A")) + ":")
        for e in evs:
            where = f", at {e['location']}" if e.get("location") else ""
            title = e.get("title") or "Untitled"
            if e.get("all_day"):
                lines.append(f"{title}, all day{where}.")
            elif e["_s"] <= now < e["_e"]:
                lines.append(f"{title} is ongoing, until {_clock(e['_e'])}{where}.")
            else:
                lines.append(f"{title}, {_clock(e['_s'])} to {_clock(e['_e'])}{where}.")
    return "\n".join(lines)


def answer_parts(question, now=None, runner=None, cfg=None):
    """(alerts, schedule_text). Alerts come first and are always included, whatever day was asked about."""
    now = now or datetime.now()
    lo, hi, label = window(question, now)
    events = fetch(0, ALERT_DAYS + 1, runner, cfg=cfg)
    sched = render(events_between([e for e in events if e.get("kind") == "event"], lo, hi), label, lo, now)
    lines = alert_lines(alerts(events, now))
    if any(e.get("kind") == "reminders_denied" for e in events):
        sched += "\nI can't see your Reminders yet, so I can't check assignments or subscriptions. Allow Jarviee Calendar under System Settings → Privacy & Security → Reminders."
    return lines, sched


def answer(question, now=None, runner=None, cfg=None):
    al, sched = answer_parts(question, now, runner, cfg=cfg)
    head = ("HEADS UP: " + " ".join(al) + "\n\n") if al else ""
    return head + sched
