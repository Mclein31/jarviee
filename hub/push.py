"""Phone notifications through ntfy (https://ntfy.sh): a heads-up `lead_min` minutes before each timed calendar event
and each timed reminder, plus the day's schedule every morning at `digest_at` (08:00).

The morning schedule is automatic and separate from /startwork: it is queued on the ntfy server ahead of time for
the next `digest_at` (sequence_id `digest-YYYYMMDD-<hash>`) and replaced whenever the day's calendar changes, so it
arrives on time even while this Mac sleeps. If nothing could be queued (the Mac was off all night) it goes out as
soon as Jarviee runs, up to five hours late.

Every `check_s` the watcher reads today and the next two days from Apple Calendar / Reminders (`apple_calendar.fetch`)
and hands every alert inside the next `ahead_h` hours to the ntfy server as a scheduled message (ntfy `delay`), keyed
by a `sequence_id`. The server delivers it on time even while this Mac sleeps. When an event moves, is deleted or a
reminder is ticked off, its old scheduled message is cancelled (`DELETE /<topic>/<sequence_id>`). If the Mac was
asleep past an alert's time but the item hasn't started yet, the alert goes out at once ("starts in 6 min").

The topic is a long random name made on first use and kept in ~/.jarviee/push.json with the sent log; anyone who
knows it can read the messages, so it never goes in the repo. Standard library only. Never raises into the server.
"""
import hashlib
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import apple_calendar, brand, channels

DEFAULTS = {"enabled": False, "server": "https://ntfy.sh", "lead_min": 15, "digest_at": "08:00", "digest_enabled": True,
            "check_s": 300, "ahead_h": 36}
# One icon per kind of phone notification, so a banner says where it came from at a glance. Values are ntfy tags:
# an emoji short code becomes the emoji in front of the title on iOS and Android. `push.icons` in config overrides.
ICONS = {
    "agent_done": "robot",            # 🤖 an agent (or a batch of agents) finished
    "agent_needs": "raising_hand",    # 🙋 an agent is waiting for you
    "agent_failed": "rotating_light", # 🚨 an agent failed
    "event": "date",                  # 📅 calendar event starting soon
    "reminder": "alarm_clock",        # ⏰ reminder due
    "digest": "calendar",             # 📆 the morning calendar summary
    "briefing": "sunrise",            # 🌅 /startwork: what's ahead
    "gmail": "email",                 # 📧
    "linkedin": "briefcase",          # 💼
    "github": "octopus",              # 🐙
    "whatsapp": "speech_balloon",     # 💬
    "limit_spent": "hourglass_flowing_sand",   # ⏳ Claude 5-hour limit used up
    "limit_back": "battery",          # 🔋 limit reset
    "test": "white_check_mark",       # ✅
}
# where tapping the notification goes (ntfy `click`)
CLICKS = {"gmail": "https://mail.google.com/mail/u/0/#inbox", "linkedin": "https://www.linkedin.com/notifications/",
          "github": "https://github.com/notifications", "whatsapp": "whatsapp://",
          "event": "calshow://", "digest": "calshow://", "briefing": "calshow://",   # iPhone Calendar
          "reminder": "x-apple-reminderkit://"}                                    # iPhone Reminders
# without a click target, tapping a message in the ntfy app only copies its text


def icon(kind, cfg=None):
    """The ntfy tags for a kind of notification: ["robot"] for "agent_done", [] for an unknown kind."""
    tag = ((cfg or {}).get("push") or {}).get("icons", {}).get(kind) or ICONS.get(kind)
    return [str(tag)] if tag else []


_lock = threading.RLock()   # re-entrant: sync() holds it while publish() asks topic() for the name
_status = {}   # last sync result for the dashboard


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("push") or {})}


# ---------- state ----------
def _state_file(cfg):
    if (cfg or {}).get("push_state_path"):
        return Path(cfg["push_state_path"])
    return brand.home() / "push.json"


def _load(cfg):
    try:
        data = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cfg, data):
    f = _state_file(cfg)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)   # the topic is the only thing between these messages and anyone else
        os.replace(tmp, f)
    except OSError:
        pass


def topic(cfg):
    """The ntfy topic: `push.topic` from config, else a random one made once and kept in push.json."""
    s = settings(cfg)
    if s.get("topic"):
        return str(s["topic"])
    with _lock:
        st = _load(cfg)
        if not st.get("topic"):
            st["topic"] = "jarviee-" + secrets.token_hex(12)
            _save(cfg, st)
        return st["topic"]


# ---------- ntfy ----------
def _ssl_context():
    """Verifying TLS context. python.org builds ship no CA bundle until "Install Certificates.command" is run, so
    every HTTPS call fails with CERTIFICATE_VERIFY_FAILED; then use certifi if present, else macOS's /etc/ssl/cert.pem."""
    paths = ssl.get_default_verify_paths()
    if any(p and os.path.exists(p) for p in (paths.cafile, paths.capath, paths.openssl_cafile, os.environ.get("SSL_CERT_FILE"))):
        return ssl.create_default_context()
    try:
        import certifi   # noqa: PLC0415  optional
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for f in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem", "/usr/local/etc/ca-certificates/cert.pem"):
        if os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


def _request(req, opener):
    try:
        if opener is None:
            opener = lambda r, timeout: urllib.request.urlopen(r, timeout=timeout, context=_ssl_context())   # noqa: E731
        with opener(req, timeout=10) as r:
            return 200 <= r.status < 300, ""
    except urllib.error.HTTPError as e:
        return False, f"ntfy said {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"ntfy unreachable: {getattr(e, 'reason', e)}"


def publish(cfg, title, message, at=None, seq=None, priority=4, tags=None, opener=None, kind=None, click=None):
    """(ok, error). `at` (datetime) makes it a scheduled message; `seq` lets it be replaced or cancelled later.
    `kind` (a key of ICONS) picks the icon when no `tags` are given, and the tap target for that kind.
    Summary kinds go to WhatsApp or email instead when one is on (hub/channels.py); those can't be scheduled."""
    direct = channels.summary_sender(cfg, kind) if at is None and kind else None
    if direct:
        return direct(cfg, title, message)
    s = settings(cfg)
    body = {"topic": topic(cfg), "title": title, "message": message, "priority": priority,
            "tags": tags if tags else icon(kind, cfg)}
    click = click or CLICKS.get(kind or "")
    if click:
        body["click"] = click
    if at is not None:
        body["delay"] = str(int(at.timestamp()))
    if seq:
        body["sequence_id"] = seq
    req = urllib.request.Request(s["server"].rstrip("/") + "/", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "jarviee"})
    return _request(req, opener)


def cancel(cfg, seq, opener=None):
    s = settings(cfg)
    req = urllib.request.Request(f"{s['server'].rstrip('/')}/{topic(cfg)}/{seq}", method="DELETE",
                                 headers={"User-Agent": "jarviee"})
    return _request(req, opener)


# ---------- what to send ----------
def _item(e):
    """A timed event or reminder as {'key','seq','kind','title','start','end','where'}, or None."""
    if e.get("kind") not in ("event", "reminder") or e.get("all_day"):
        return None
    try:
        s, en = apple_calendar._local(e["start"]), apple_calendar._local(e["end"])
    except (KeyError, ValueError):
        return None
    title = (e.get("title") or "Untitled").strip()
    key = hashlib.sha1(f"{e['kind']}|{title}|{e['start']}".encode()).hexdigest()[:20]
    return {"key": key, "seq": "j" + key, "kind": e["kind"], "title": title, "start": s, "end": en,
            "where": e.get("location") or "", "list": e.get("calendar") or ""}


def _mins(delta):
    return max(0, int(round(delta.total_seconds() / 60)))


def message(it, lead_min, now=None):
    """(title, body, tags) for one alert; `now` set means it's going out late, so say how long is actually left."""
    left = f"in {lead_min} min" if now is None else (f"in {_mins(it['start'] - now)} min" if it["start"] - now >= timedelta(minutes=1) else "now")
    clock = apple_calendar._clock
    if it["kind"] == "reminder":
        return (f"Due {left}: {it['title']}", f"Reminder due at {clock(it['start'])}" + (f" · {it['list']}" if it["list"] else ""),
                icon("reminder"))
    span = clock(it["start"]) if it["end"] <= it["start"] else f"{clock(it['start'])} to {clock(it['end'])}"
    body = span + (f" · {it['where']}" if it["where"] else "")
    if now is not None:
        body += "\nSent late: this Mac was asleep when the alert was due."
    return (f"{it['title']} starts {left}", body, icon("event"))


def digest(events, now):
    """(title, body) of the schedule for `now`'s day from `now` on: events, then heads-ups (reminders and deadlines)."""
    lo = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = [e for e in apple_calendar.events_between([e for e in events if e.get("kind") == "event"], lo, lo + timedelta(days=1))
             if e["_e"] > now]
    lines = [f"{'All day' if e.get('all_day') else apple_calendar._clock(e['_s'])} · {e.get('title') or 'Untitled'}"
             + (f" ({e['location']})" if e.get("location") else "") for e in today]
    heads = apple_calendar.alert_lines(apple_calendar.alerts(events, now))   # reminders due soon are among these
    n = len(today)
    title = f"Today: {n} event{'s' if n != 1 else ''}" if n else "Today: nothing on the calendar"
    if heads:
        title += f", {len(heads)} heads-up" + ("s" if len(heads) != 1 else "")
    body = lines + ([""] if lines and heads else []) + [f"⚠ {h}" for h in heads]
    return title, "\n".join(body) or "No events, reminders or deadlines today."


def plan(events, state, now, s):
    """Pure: what to do this round. A list of ('schedule', item, at) / ('now', item) / ('cancel', key)."""
    lead, sent = timedelta(minutes=int(s["lead_min"])), state.get("sent", {})
    horizon = now + timedelta(hours=float(s["ahead_h"]))
    items, out = {}, []
    for e in events:
        it = _item(e)
        if it and now < it["start"] <= horizon:
            items[it["key"]] = it
    for key, it in sorted(items.items(), key=lambda kv: kv[1]["start"]):
        if key in sent:
            continue
        fire = it["start"] - lead
        out.append(("schedule", it, fire) if fire > now + timedelta(seconds=30) else ("now", it))
    for key, rec in sent.items():   # scheduled but no longer on the calendar (moved, deleted, ticked off)
        if key not in items and rec.get("scheduled") and rec.get("fire", 0) > now.timestamp() + 30:
            out.append(("cancel", key))
    return out


def _digest_time(now, s):
    """Today's `digest_at` as a datetime, or None when the setting is unreadable."""
    try:
        h, m = (int(x) for x in str(s["digest_at"]).split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None


def _digest_round(cfg, st, events, now, s, opener=None):
    """Queue the next morning schedule on ntfy (replacing it when the calendar changed); send today's at once if it
    is past `digest_at`, nothing was queued for it and it's not too late. Returns errors."""
    at = _digest_time(now, s)
    if at is None or not s.get("digest_enabled", True):
        return []
    errors, today = [], now.date().isoformat()
    q = st.get("digest_queued") or {}
    on_whatsapp = channels.summary_sender(cfg, "digest") is not None   # WhatsApp or email: sent at 08:00, not queued
    if on_whatsapp and q.get("seq") and q.get("fire", 0) > now.timestamp():   # moved to WhatsApp: take back ntfy's
        ok, err = cancel(cfg, q["seq"], opener)
        if ok or "404" in err:
            st.pop("digest_queued", None)
            q = {}
    if st.get("digest") != today and now >= at:
        if q.get("date") == today:   # ntfy held it and delivered it at `at`
            st["digest"] = today
        elif now < at + timedelta(hours=5):
            title, body = digest(events, now)
            ok, err = publish(cfg, title, body, priority=3, kind="digest", opener=opener)
            if ok:
                st["digest"] = today
            else:
                errors.append(err)
    target = at if now < at else at + timedelta(days=1)
    if on_whatsapp or target - now <= timedelta(seconds=30):   # WhatsApp can't hold a message for later
        return errors
    title, body = digest(events, target)
    h = hashlib.sha1(f"{title}\n{body}".encode()).hexdigest()[:10]
    day = target.date().isoformat()
    if q.get("date") == day and q.get("hash") == h:
        return errors
    if q.get("seq") and q.get("fire", 0) > now.timestamp():
        ok, err = cancel(cfg, q["seq"], opener)
        if not ok and "404" not in err:
            return errors + [err]
    seq = f"digest-{target:%Y%m%d}-{h}"
    ok, err = publish(cfg, title, body, at=target, seq=seq, priority=3, kind="digest", opener=opener)
    if ok:
        st["digest_queued"] = {"date": day, "hash": h, "seq": seq, "fire": target.timestamp()}
    else:
        st.pop("digest_queued", None)
        errors.append(err)
    return errors


def sync(cfg, now=None, fetch=None, opener=None):
    """One round: read the calendar, schedule new alerts, cancel stale ones, send the morning summary. Returns status."""
    s = settings(cfg)
    now = now or datetime.now()
    if not s["enabled"]:
        return _set_status({"enabled": False})
    if not apple_calendar.is_enabled(cfg):
        return _set_status({"enabled": True, "error": apple_calendar.DISABLED_MSG, "at": now.timestamp()})
    try:
        events = (fetch or (lambda back, ahead: apple_calendar.fetch(back, ahead, cfg=cfg)))(0, max(3, apple_calendar.ALERT_DAYS + 1))   # alerts: 3 days; summary heads-ups: up to 5
    except Exception as e:   # noqa: BLE001  CalendarError, timeouts: try again next round
        return _set_status({"enabled": True, "error": str(e), "at": now.timestamp()})
    seen = sorted({e.get("calendar") for e in events if e.get("calendar") and e.get("kind") in ("event", "reminder")})
    muted = {str(m) for m in (s.get("mute_calendars") or [])}
    events = [e for e in events if (e.get("calendar") or "") not in muted]   # muted lists: nothing sent, queued ones cancelled
    with _lock:
        st = _load(cfg)
        st["calendars"] = seen
        sent, errors = st.setdefault("sent", {}), []
        for act in plan(events, st, now, s):
            if act[0] == "cancel":
                ok, err = cancel(cfg, sent[act[1]]["seq"], opener)
                if ok or "404" in err:
                    sent.pop(act[1], None)
                else:
                    errors.append(err)
                continue
            it = act[1]
            if act[0] == "schedule":
                title, body, tags = message(it, int(s["lead_min"]))
                ok, err = publish(cfg, title, body, at=act[2], seq=it["seq"], tags=tags, opener=opener, kind=it["kind"])
            else:
                title, body, tags = message(it, int(s["lead_min"]), now=now)
                ok, err = publish(cfg, title, body, seq=it["seq"], priority=5, tags=tags, opener=opener, kind=it["kind"])
            if ok:
                sent[it["key"]] = {"seq": it["seq"], "title": it["title"], "start": it["start"].timestamp(),
                                   "fire": (act[2] if act[0] == "schedule" else now).timestamp(), "scheduled": act[0] == "schedule"}
            else:
                errors.append(err)
        errors += _digest_round(cfg, st, events, now, s, opener)
        cutoff = now.timestamp() - 86400
        st["sent"] = {k: v for k, v in sent.items() if v.get("start", 0) > cutoff}
        _save(cfg, st)
        upcoming = sorted((v for v in st["sent"].values() if v["fire"] > now.timestamp()), key=lambda v: v["fire"])
    return _set_status({"enabled": True, "at": now.timestamp(), "error": errors[0] if errors else "",
                        "queued": len(upcoming),
                        "next": {"title": upcoming[0]["title"], "fire": upcoming[0]["fire"]} if upcoming else None})


def cancel_all(cfg, opener=None, everything=False):
    """Turning notifications off: take back every alert still waiting on the ntfy server
    (`everything`: every scheduled one, whatever its time, e.g. when the topic is abandoned)."""
    with _lock:
        st = _load(cfg)
        now = time.time()
        for key, rec in list(st.get("sent", {}).items()):
            if rec.get("scheduled") and (everything or rec.get("fire", 0) > now):
                ok, err = cancel(cfg, rec["seq"], opener)
                if ok or "404" in err:
                    st["sent"].pop(key)
        q = st.get("digest_queued") or {}
        if q.get("seq") and (everything or q.get("fire", 0) > now):
            ok, err = cancel(cfg, q["seq"], opener)
            if ok or "404" in err:
                st.pop("digest_queued", None)
        _save(cfg, st)
    return _set_status({"enabled": False})


def rotate_topic(cfg, opener=None):
    """The topic leaked: take back queued alerts, make a new random name, forget the sent log. Returns the new topic."""
    cancel_all(cfg, opener, everything=True)
    with _lock:
        st = _load(cfg)
        st["topic"] = "jarviee-" + secrets.token_hex(12)
        st["sent"] = {}
        st.pop("digest", None)
        _save(cfg, st)
        (cfg.get("push") or {}).pop("topic", None)   # a pinned config topic would win over the new one
        return st["topic"]


def test(cfg, opener=None):
    s = settings(cfg)
    return publish(cfg, "Jarviee is connected",
                   f"Phone notifications work. You'll get a heads-up {s['lead_min']} min before each event and timed reminder, "
                   f"and your schedule for the day at {s['digest_at']}.", kind="test", opener=opener)


def _set_status(d):
    _status.clear()
    _status.update(d)
    return dict(_status)


def status(cfg):
    s = settings(cfg)
    return {"enabled": bool(s["enabled"]), "topic": topic(cfg), "server": s["server"], "lead_min": s["lead_min"],
            "digest_at": s["digest_at"], "limit_alerts": bool(s.get("limit_alerts", True)), "mute_calendars": list(s.get("mute_calendars") or []),
            "calendars": _load(cfg).get("calendars", []), "subscribe": f"{s['server'].rstrip('/')}/{topic(cfg)}",
            "digest_next": (_load(cfg).get("digest_queued") or {}).get("fire"),
            **{k: v for k, v in _status.items() if k != "enabled"}}


class PushWatcher(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True, name="jarviee-push")
        self.cfg, self._stop, self.kick = cfg, threading.Event(), threading.Event()

    def run(self):
        if self._stop.wait(20):   # let the server settle first
            return
        while not self._stop.is_set():
            try:
                sync(self.cfg)
            except Exception as e:   # noqa: BLE001
                print(f"[push] {e}")
            self.kick.wait(self._next_wait())
            self.kick.clear()

    def _next_wait(self, now=None):
        """The usual interval, but wake right at `digest_at` so a WhatsApp schedule (which can't be queued) is on time."""
        s = settings(self.cfg)
        wait_s = max(60, int(s["check_s"]))
        now = now or datetime.now()
        at = _digest_time(now, s)
        if at is not None:
            if at <= now:
                at += timedelta(days=1)
            wait_s = min(wait_s, max(1, int((at - now).total_seconds()) + 2))
        return wait_s

    def stop(self):
        self._stop.set()
        self.kick.set()
