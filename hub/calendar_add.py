"""Add a calendar event from the command bar ("add a Data Science quiz Friday 3 pm").

`parse` is deterministic (no model, no network) and strict: it returns None unless the text is clearly an
add-event request, so ordinary tasks keep launching agents. Nothing here writes by itself: `parse_request`
only reads the calendar names, `add` writes one validated event through `apple_calendar.apply`, and `undo`
removes only an event this process created a moment ago (single-use token, short lifetime).
"""
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from . import apple_calendar

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
          "oct": 10, "nov": 11, "dec": 12, "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
          "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3,
            "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_WD_RE = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

_VERB_RE = re.compile(r"(?i)^(?:please\s+|hey\s+|jarviee[,\s]+)*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
                      r"(?P<verb>add|create|schedule|put|book|pencil\s+in|remind\s+me(?:\s+to|\s+about|\s+of)?)\b[\s:,]*")
_RECUR_RE = re.compile(r"(?i)\b(every|each|daily|weekly|monthly|yearly|annually|repeat\w*|recurring|weekdays|weekends)\b")
# subjects that mean "build this", not "put this in my calendar"
_CODE_RE = re.compile(r"(?i)\b(code|bugs?|features?|functions?|endpoints?|api|routes?|buttons?|pages?|components?|modules?|"
                      r"files?|repos?|branch|commits?|database|schema|css|html|python|script|config|ui|ux|dark mode|"
                      r"login|auth|dashboard|test suite|unit tests?|e2e|refactor\w*|dependenc\w+|server|agents?|"
                      r"tasks?|prompts?|models?|workflows?|notifications?|parser|command bar|integration|toggle|"
                      r"endpoint|handler|logging|logs?|migration|deploy\w*)\b")
_CAL_WORD_RE = re.compile(r"(?i)\b(calendar|event|appointment|meeting|reminder)\b")
_TIME_PREFIX = r"(?:\b(?:at|from|@)\s*)?"
_MER = r"(a\.?m\.?|p\.?m\.?)"
_NUM = r"(?<![\d:/.])(\d{1,2})(?::(\d{2}))?"


class AddError(ValueError):
    pass


def _mer(s):
    return None if not s else ("am" if s.lower().startswith("a") else "pm")


def _h24(h, mer, default_pm_guess=False):
    """12 h clock + optional meridiem → 24 h. With no meridiem: 12 noon, 1-6 afternoon, 7-11 morning, 0 / 13-23 as is."""
    if h > 23:
        return None
    if mer:
        if h < 1 or h > 12:
            return None
        return (h % 12) + (12 if mer == "pm" else 0)
    if h == 0 or h >= 13:
        return h
    if h == 12:
        return 12
    return h + 12 if h <= 6 else h


def _cut(s, m):
    return s[:m.start()] + " " + s[m.end():]


def _times(s):
    """((h1, m1), (h2, m2) | None, remaining text) or None. Ranges, '3 pm', '3:30pm', '15:00', 'noon', 'at 3'."""
    # ranges: 3-4 pm, 3 pm - 4:30 pm, from 3 to 4:30, 15:00-16:30
    for m in re.finditer(_TIME_PREFIX + _NUM + r"\s*" + _MER + r"?\s*(?:-|–|—|to|until|till)\s*" + _NUM + r"\s*" + _MER + r"?(?![\w:])", s, re.I):
        h1, m1, a1, h2, m2, a2 = int(m.group(1)), m.group(2), _mer(m.group(3)), int(m.group(4)), m.group(5), _mer(m.group(6))
        pref = m.group(0).strip().lower()
        has_clock = bool(m1 or m2)
        if not (a1 or a2 or (has_clock and (m1 and m2)) or pref.startswith(("from", "at", "@"))):
            continue   # "3-4" on its own could be anything
        if a2 and not a1:   # 3-4 pm → both pm; 11-1 pm → 11 am to 1 pm
            a1 = a2
            if _h24(h1, a1) is not None and _h24(h2, a2) is not None and _h24(h1, a1) > _h24(h2, a2):
                a1 = "am"
        elif a1 and not a2:
            a2 = a1
        e = _h24(h2, a2)
        b = _h24(h1, a1)
        if a1 is None and a2 is None:
            if m1 and m2 and (h1 > 12 or h2 > 12 or h1 == 0):
                b, e = h1, h2
            else:
                b, e = _h24(h1, None), _h24(h2, None)
                if e is not None and b is not None and e <= b and h2 <= 12:
                    e = e + 12 if e < 12 else e
        if b is None or e is None or int(m1 or 0) > 59 or int(m2 or 0) > 59:
            continue
        return (b, int(m1 or 0)), (e, int(m2 or 0)), _cut(s, m)
    m = re.search(_TIME_PREFIX + _NUM + r"\s*" + _MER + r"(?![a-z])", s, re.I)
    if m:
        h = _h24(int(m.group(1)), _mer(m.group(3)))
        if h is not None and int(m.group(2) or 0) <= 59:
            return (h, int(m.group(2) or 0)), None, _cut(s, m)
    m = re.search(_TIME_PREFIX + r"(?<![\d:/.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])", s)
    if m:
        return (int(m.group(1)), int(m.group(2))), None, _cut(s, m)
    m = re.search(r"(?i)" + _TIME_PREFIX + r"\b(noon|midnight)\b", s)
    if m:
        return (12 if m.group(1).lower() == "noon" else 0, 0), None, _cut(s, m)
    m = re.search(r"(?i)\b(?:at|@)\s*(\d{1,2})(?![\d:/.]|\s*(?:st|nd|rd|th)\b)", s)   # "at 3"
    if m and 1 <= int(m.group(1)) <= 12:
        return (_h24(int(m.group(1)), None), 0), None, _cut(s, m)
    return None


def _duration(s):
    m = re.search(r"(?i)\bfor\s+(\d+(?:\.\d+)?|an?|half an?|one|two|three|four)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", s)
    if not m:
        return None, s
    word = m.group(1).lower()
    n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "half a": 0.5, "half an": 0.5}.get(word)
    if n is None:
        n = float(word)
    mins = int(round(n * (60 if m.group(2).lower().startswith("h") else 1)))
    return (mins if 5 <= mins <= 24 * 60 else None), _cut(s, m)


def _next_year_safe(y, mo, d):   # a real date or None (Feb 30)
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def _dates(s, now):
    """(date at 00:00, stated_text_cut) or None. Also returns the matched kind."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def roll(mo, d, y=None):
        if y is not None:
            return _next_year_safe(y if y > 99 else 2000 + y, mo, d)
        c = _next_year_safe(today.year, mo, d)
        if c is None:
            return None
        if c < today:
            c = _next_year_safe(today.year + 1, mo, d)
        return c
    m = re.search(r"(?i)(?:\bon\s+)?\b(" + _MONTH_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?::|am|pm|a\.m|p\.m))(?:,?\s+(\d{4}))?", s)
    if m and 1 <= int(m.group(2)) <= 31:
        c = roll(MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if c:
            return c, _cut(s, m)
    m = re.search(r"(?i)(?:\bon\s+)?(?:\bthe\s+)?\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_RE + r")\b\.?(?:,?\s+(\d{4}))?", s)
    if m and 1 <= int(m.group(1)) <= 31:
        c = roll(MONTHS[m.group(2).lower()], int(m.group(1)), int(m.group(3)) if m.group(3) else None)
        if c:
            return c, _cut(s, m)
    m = re.search(r"(?:\bon\s+)?(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        mo, d = (b, a) if a > 12 and b <= 12 else (a, b)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            c = roll(mo, d, int(m.group(3)) if m.group(3) else None)
            if c:
                return c, _cut(s, m)
    m = re.search(r"(?i)(?:\bon\s+)?\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b|\bon\s+(\d{1,2})(?:st|nd|rd|th)\b|(?<![\d/])\b(\d{1,2})(?:st|nd|rd|th)\b", s)
    if m:
        d = int(next(g for g in m.groups() if g))
        if 1 <= d <= 31:
            c = _next_year_safe(today.year, today.month, d)
            if c is None or c < today:
                y, mo = (today.year + (today.month == 12), today.month % 12 + 1)
                c = _next_year_safe(y, mo, d)
            if c:
                return c, _cut(s, m)
    m = re.search(r"(?i)\bday after tomorrow\b", s)
    if m:
        return today + timedelta(days=2), _cut(s, m)
    m = re.search(r"(?i)\b(today|tonight|tomorrow|tmrw)\b", s)
    if m:
        w = m.group(1).lower()
        return today + timedelta(days=1 if w in ("tomorrow", "tmrw") else 0), _cut(s, m)
    m = re.search(r"(?i)(?:\bon\s+)?\b(?:(this|next)\s+)?(" + _WD_RE + r")\b\.?", s)
    if m:
        wd, which = WEEKDAYS[m.group(2).lower()], (m.group(1) or "").lower()
        delta = (wd - today.weekday()) % 7
        if which == "next":
            delta = delta or 7
            if today.weekday() + delta < 7:   # still inside this Monday-Sunday week: "next" means the following week
                delta += 7
        return today + timedelta(days=delta), _cut(s, m)
    return None


def _clean(s):
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _match_calendar(name, calendars):
    n = name.strip().lower()
    if not n:
        return None
    for c in calendars:
        if c.lower() == n:
            return c
    hits = [c for c in calendars if c.lower().startswith(n) or n in c.lower()]
    return hits[0] if len(hits) == 1 else None


def _calendar(s, calendars):
    """(calendar | None, hint | None, text without the calendar clause). `hint` is a stated name that matched nothing."""
    cands = list(re.finditer(r"(?i)(?=\b(?:in|on|to|into|under)\s+(?:(?:my|the)\s+)?((?:[\w&+'.-]+\s+){0,2}[\w&+'.-]+)\s+calendar\b)", s))
    if cands:
        m = cands[-1]
        name = m.group(1)
        full = re.compile(r"(?i)\b(?:in|on|to|into|under)\s+(?:(?:my|the)\s+)?" + re.escape(name) + r"\s+calendar\b").match(s, m.start())
        cal = _match_calendar(name, calendars)
        if name.lower() in ("my", "the", "a", "this", "that", "your"):
            cal = None
        else:
            return cal, (None if cal else name), _cut(s, full)
    for c in sorted(calendars, key=len, reverse=True):
        m = re.search(r"(?i)\b(?:in|on|to|into|under)\s+(?:my\s+)?" + re.escape(c) + r"\b(?!\s+(?:room|hall|building))", s)
        if m:
            return c, None, _cut(s, m)
    return None, None, s


def _tidy_title(t):
    t = _clean(t)
    t = re.sub(r"(?i)\b(?:to|on|in|into)\s+(?:my\s+|the\s+)?calendar\b", " ", t)
    t = re.sub(r"(?i)^(?:(?:an?|the|my|me)\s+)+", "", t)
    t = re.sub(r"(?i)^(?:new\s+)?(?:event|appointment|reminder)\s*(?:called|named|titled|for|:)?\s+", "", t)
    t = re.sub(r"(?i)^(?:called|named|titled|for)\s+", "", t)
    t = re.sub(r"(?i)^(?:(?:an?|the|my)\s+)+", "", t)
    t = _clean(t)
    t = re.sub(r"(?i)(?:[\s,.;:\-–]+(?:on|at|in|to|for|from|and|by|the|my|this|next|until|till|of))+[\s,.;:]*$", "", t)
    t = re.sub(r"[\s,.;:\-–]+$", "", t)
    t = re.sub(r"^[\s,.;:\-–]+", "", t)
    return t


_NOT_PLACE_BEFORE = re.compile(r"(?i)\b(check|log|sign|drop|plug|zoom|dive|hand|turn|fill|tune|stay|move)\s*$")


def _place(t):
    """Split 'lunch at Cafe Nero' into (title, place)."""
    best = None
    for m in re.finditer(r"(?i)\s(?:at|@)\s+(\S.*)$", t):
        best = m
        break
    m2 = re.search(r"(?i)\s+in\s+((?:the|room|rm|building|bldg|hall|lab|library|my|[A-Z0-9])\S*.*)$", t)
    if m2 and (best is None or m2.start() < best.start()):
        if not _NOT_PLACE_BEFORE.search(t[:m2.start()]) and re.match(r"(?i)(the|room|rm|building|bldg|hall|lab|library|my)\b|[A-Z0-9]", m2.group(1)):
            best = m2
    if not best:
        return t, ""
    title, place = t[:best.start()].strip(), _clean(best.group(1))
    place = re.sub(r"[\s,.;:]+$", "", place)
    if not title or not place:
        return t, ""
    return title, place


def _label_day(d):
    return f"{DAY_NAMES[d.weekday()]}, {MON_NAMES[d.month - 1]} {d.day}"


def _label_time(d):
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def parse(text, now=None, calendars=()):
    """A dict describing the event, or None unless `text` clearly asks to add one. See the module docstring."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    calendars = [c for c in (calendars or ()) if c]
    t = _clean(str(text or ""))
    if not t or len(t) > 200 or "\n" in str(text or ""):
        return None
    vm = _VERB_RE.match(t)
    if not vm:
        return None
    verb = re.sub(r"\s+", " ", vm.group("verb").lower())
    rest = t[vm.end():]
    if not rest or _RECUR_RE.search(rest):
        return None
    mentions_cal = bool(re.search(r"(?i)\bcalendar\b", rest))
    if verb == "put" and not mentions_cal:
        return None
    cal, hint, rest = _calendar(rest, calendars)
    dur, rest = _duration(rest)
    tm = _times(rest)
    if tm:
        rest = tm[2]
    dd = _dates(rest, now)
    if dd:
        rest = dd[1]
    if not tm and not dd:
        return None
    start_t, end_t = (tm[0], tm[1]) if tm else (None, None)
    title, place = _place(_tidy_title(rest))
    title = _tidy_title(title)
    if not title or len(title) > 120 or len(title.split()) > 10:
        return None
    if not verb.startswith("remind") and (_CODE_RE.search(title) or _CODE_RE.search(place)):
        return None
    if re.search(r"(?i)\b(?:me|you|it|them)\s+(?:when|if|before|after|that)\b|^(?:when|if|that|how|why|whether)\b", title):
        return None
    day_stated = dd is not None
    today = now.replace(hour=0, minute=0)
    day = dd[0] if dd else today
    note = ""
    if start_t is None:
        all_day, start, end = True, day, day.replace(hour=23, minute=59)
    else:
        all_day = False
        start = day.replace(hour=start_t[0], minute=start_t[1])
        if not day_stated and start <= now:   # "add dentist at 9 am" typed at 2 pm means tomorrow
            start += timedelta(days=1)
        elif start < now - timedelta(minutes=1):
            note = "That time has already passed today."
        if end_t:
            end = start.replace(hour=end_t[0], minute=end_t[1])
            if end <= start:
                end += timedelta(days=1)
        else:
            end = start + timedelta(minutes=dur or 60)
    if cal is None and len(calendars) == 1 and not hint:
        cal = calendars[0]
    title = title[0].upper() + title[1:]
    return {
        "title": title, "calendar": cal, "calendar_hint": hint, "all_day": all_day, "location": place,
        "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M"),
        "day_label": _label_day(start), "day_stated": day_stated, "note": note,
        "start_label": "" if all_day else _label_time(start), "end_label": "" if all_day else _label_time(end),
        "end_day_label": _label_day(end) if (not all_day and end.date() != start.date()) else "",
        "year": start.year,
    }


# ---------- real calendars (read once, cached) ----------
_cache = {"at": 0.0, "names": [], "fail_at": 0.0, "fail": ""}
_LOCK = threading.Lock()
CACHE_S = 300
FAIL_CACHE_S = 60   # a failed read (no access, reader missing) is not retried on every keystroke


def _parse_fetch(back, ahead, cfg=None):
    return apple_calendar.fetch(back, ahead, build=False, cfg=cfg)   # a parse request never builds the reader app


def real_calendars(refresh=False, fetch=None, cfg=None):
    """Titles of the calendars that have events around now (the reader reports calendars only through their events).
    A failure of the real reader is remembered for FAIL_CACHE_S and raised again from memory."""
    if not apple_calendar.is_enabled(cfg):
        raise apple_calendar.CalendarError(apple_calendar.DISABLED_MSG)
    is_real = fetch in (None, _parse_fetch)
    with _LOCK:
        if not refresh and _cache["names"] and time.time() - _cache["at"] < CACHE_S:
            return list(_cache["names"])
        if not refresh and is_real and _cache["fail"] and time.time() - _cache["fail_at"] < FAIL_CACHE_S:
            raise apple_calendar.CalendarError(_cache["fail"])
    try:
        if fetch is _parse_fetch:
            raw = _parse_fetch(30, 90, cfg=cfg)
        elif fetch is not None:
            raw = fetch(30, 90)
        else:
            raw = apple_calendar.fetch(30, 90, cfg=cfg)
        names = sorted({e.get("calendar") for e in raw
                        if e.get("kind") == "event" and e.get("calendar")}, key=str.lower)
    except Exception as e:
        if is_real:
            with _LOCK:
                _cache.update(fail_at=time.time(), fail=str(e) or "the calendar could not be read")
        raise
    with _LOCK:
        _cache.update(at=time.time(), names=names, fail="", fail_at=0.0)
    return list(names)


def parse_request(text, now=None, fetch=None, cfg=None):
    """What the parse route answers. Reads calendar names only when the text parsed as an add-event request."""
    ev = parse(text, now)
    if ev is None:
        return {"matched": False}
    if not apple_calendar.is_enabled(cfg):
        return {"matched": True, "event": ev, "calendars": [], "calendars_error": apple_calendar.DISABLED_MSG}
    names, err = [], ""
    try:
        names = real_calendars(fetch=fetch or _parse_fetch, cfg=cfg)
    except (apple_calendar.CalendarError, OSError, ValueError) as e:
        err = str(e) or "the calendar could not be read"
    except Exception as e:   # the reader also raises subprocess timeouts
        err = str(e) or "the calendar took too long"
    ev = parse(text, now, names) or ev
    return {"matched": True, "event": ev, "calendars": names, "calendars_error": err}


# ---------- write + undo ----------
_PENDING = {}   # undo token -> (calendar, title, expires)
UNDO_S = 60
_FMT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _clip(v, n):
    return " ".join(str(v or "").split())[:n]


def add(body, runner=None, fetch=None, now=None, cfg=None):
    """Write exactly one confirmed event. Returns (http status, json). Only called by the explicit Add action."""
    if not apple_calendar.is_enabled(cfg):
        return 200, {"ok": False, "error": apple_calendar.DISABLED_MSG}
    ev = body.get("event") if isinstance(body.get("event"), dict) else {}
    title, cal, loc = _clip(ev.get("title"), 200), _clip(ev.get("calendar"), 200), _clip(ev.get("location"), 200)
    start, end, all_day = str(ev.get("start") or ""), str(ev.get("end") or ""), bool(ev.get("all_day"))
    if not title:
        return 400, {"error": "the event needs a title"}
    if not (_FMT.match(start) and _FMT.match(end)) or end < start:
        return 400, {"error": "the event needs a start and an end (yyyy-mm-ddThh:mm)"}
    try:
        datetime.strptime(start, "%Y-%m-%dT%H:%M")
        datetime.strptime(end, "%Y-%m-%dT%H:%M")
    except ValueError:
        return 400, {"error": "that date is not valid"}
    try:
        names = real_calendars(refresh=bool(not fetch), fetch=fetch, cfg=cfg)
    except Exception as e:
        return 200, {"ok": False, "error": str(e) or "the calendar could not be read"}
    if cal not in names:
        return 400, {"error": "pick one of your calendars", "calendars": names}
    # Undo deletes by calendar + title, so it is offered only when no other event in that calendar shares the title.
    undo_ok, why = True, ""
    try:
        others = [e for e in (fetch or (lambda back, ahead: apple_calendar.fetch(back, ahead, cfg=cfg)))(360, 1000)
                  if e.get("kind") == "event" and e.get("calendar") == cal and e.get("title") == title]
        if others:
            undo_ok, why = False, "another event with this title is already in that calendar, so Undo is off to protect it"
    except Exception:
        undo_ok, why = False, "the calendar could not be checked, so Undo is off"
    spec = {"events": [{"calendar": cal, "title": title, "start": start, "end": end, "all_day": all_day, "location": loc}]}
    try:
        rep = apple_calendar.apply(spec, runner=runner, cfg=cfg)
    except Exception as e:
        return 200, {"ok": False, "error": str(e) or "the calendar writer failed"}
    errs = rep.get("errors") or []
    created, skipped = title in (rep.get("created") or []), title in (rep.get("skipped") or [])
    if errs or not (created or skipped):
        return 200, {"ok": False, "error": "; ".join(errs) or "the calendar did not confirm the event"}
    token = ""
    if created and undo_ok:
        token = secrets.token_urlsafe(12)
        with _LOCK:
            now_t = time.time()
            for k in [k for k, v in _PENDING.items() if v[2] < now_t]:
                del _PENDING[k]
            _PENDING[token] = (cal, title, now_t + UNDO_S)
    return 200, {"ok": True, "created": created, "skipped": skipped, "event": {**spec["events"][0]},
                 "undo": token, "undo_s": UNDO_S if token else 0, "undo_note": "" if (token or skipped) else why,
                 "message": "That event was already there." if skipped else ""}


def undo(body, runner=None, cfg=None):
    """Delete the event a recent `add` created. Needs the single-use token that `add` returned."""
    if not apple_calendar.is_enabled(cfg):
        return 200, {"ok": False, "error": apple_calendar.DISABLED_MSG}
    token = str(body.get("undo") or "")
    with _LOCK:
        item = _PENDING.pop(token, None)
    if not token or not item or item[2] < time.time():
        return 410, {"error": "Undo is no longer available. Delete it in Calendar."}
    try:
        rep = apple_calendar.apply({"delete_events": [{"calendar": item[0], "title": item[1]}]}, runner=runner, cfg=cfg)
    except Exception as e:
        return 200, {"ok": False, "error": str(e) or "the calendar writer failed"}
    if rep.get("errors"):
        return 200, {"ok": False, "error": "; ".join(rep["errors"])}
    return 200, {"ok": True, "deleted": rep.get("deleted") or []}
