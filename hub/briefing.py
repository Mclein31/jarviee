"""/startwork: the "what's ahead" briefing, spoken, shown in the command bar and sent to the phone.

The morning twin of /endwork (hub/daybook.py). Modelled on OpenJarvis's morning digest (connectors -> one briefing),
but local and standard library only, with no LLM: every line is built from what each source returns.

Sources, each read-only and each optional (a source that isn't set up says so and is skipped when spoken):
  calendar  Apple Calendar + Reminders (hub/apple_calendar.py): what's left today, tomorrow's first event, heads-ups.
  gmail     unread mail in the Primary inbox, over IMAP with a Google App Password kept in the macOS Keychain
            (service "jarviee-gmail"; `python3 hub.py connect-gmail you@gmail.com`). Never marked read. Each email
            gets a one-line gist and a to-do from a small tool-less model (hub/mailsum.py).
  linkedin  LinkedIn has no API for personal notifications, so it is read from LinkedIn's own emails in that Gmail.
  github    unread notifications through the `gh` CLI you're already signed in to.
  whatsapp  WhatsApp has no API for personal accounts, so unread chats are read from WhatsApp Desktop's local
            database (a copy, opened read-only). Chat names and counts only; message text is never spoken or sent.
  agents    Jarviee's own agents that are waiting for you.

What goes to the phone (ntfy, a third-party server) is deliberately thin: counts, sender / chat / repo names, event
titles. Never an email subject, a message body or a code.
"""
import email.header
import email.parser
import email.utils
import html as _html
import imaplib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

from . import apple_calendar, mailsum, voice

STARTWORK_RE = re.compile(r"^\s*/start[\s_-]*(work|wokr)\b\s*", re.I)
SOURCES = ("calendar", "gmail", "linkedin", "github", "whatsapp", "agents")
DEFAULTS = {"sources": list(SOURCES), "phone": True, "timeout_s": 15,
            "gmail": {"address": "", "host": "imap.gmail.com", "days": 2, "max": 30,
                      "summaries": True, "summary_wait_s": 40, "warm_s": 600, "spoken": 5},
            "github": {"max": 50},
            "whatsapp": {"db": "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
                         "days": 7}}
KEYCHAIN_SERVICE = "jarviee-gmail"
APPLE_EPOCH = 978307200   # Core Data timestamps count from 2001-01-01


def is_startwork(text):
    return bool(STARTWORK_RE.match(text or ""))


def settings(cfg):
    user = (cfg or {}).get("briefing") or {}
    out = {**DEFAULTS, **user}
    for k in ("gmail", "github", "whatsapp"):
        out[k] = {**DEFAULTS[k], **(user.get(k) or {})}
    return out


def _section(ok=True, connected=True, error="", **kw):
    return {"ok": ok, "connected": connected, "error": error, **kw}


def _plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _names(names, limit=3):
    names = [n for n in dict.fromkeys(names) if n][:limit]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


# ---------------------------------------------------------------- calendar
def read_calendar(cfg, now=None, fetch=None):
    now = now or datetime.now()
    if not apple_calendar.is_enabled(cfg):
        return _section(False, connected=False, error=apple_calendar.DISABLED_MSG)
    try:
        events = (fetch or (lambda back, ahead: apple_calendar.fetch(back, ahead, cfg=cfg)))(0, apple_calendar.ALERT_DAYS + 1)
    except Exception as e:   # noqa: BLE001  CalendarError, timeouts
        return _section(False, error=str(e))
    lo = now.replace(hour=0, minute=0, second=0, microsecond=0)
    evs = [e for e in events if e.get("kind") == "event"]
    today = [e for e in apple_calendar.events_between(evs, lo, lo + timedelta(days=1)) if e["_e"] > now]
    tomorrow = apple_calendar.events_between(evs, lo + timedelta(days=1), lo + timedelta(days=2))

    def item(e):
        return {"title": (e.get("title") or "Untitled").strip(), "all_day": bool(e.get("all_day")),
                "start": e["_s"].isoformat(), "time": "all day" if e.get("all_day") else apple_calendar._clock(e["_s"]),
                "ongoing": e["_s"] <= now < e["_e"] and not e.get("all_day"), "where": e.get("location") or ""}
    return _section(today=[item(e) for e in today], tomorrow=[item(e) for e in tomorrow],
                    heads_up=apple_calendar.alert_lines(apple_calendar.alerts(events, now)))


# ---------------------------------------------------------------- gmail + linkedin
def _keychain(args, runner=None):
    runner = runner or subprocess.run   # looked up at call time so tests and other platforms can swap it
    try:
        r = runner(["/usr/bin/security", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def gmail_account(cfg, runner=None):
    """(address, app password) from config + Keychain, or (address or "", None) when not connected."""
    addr = settings(cfg)["gmail"]["address"]
    if not addr:
        out = _keychain(["find-generic-password", "-s", KEYCHAIN_SERVICE], runner) or ""
        m = re.search(r'"acct"<blob>="([^"]+)"', out)
        addr = m.group(1) if m else ""
    if not addr:
        return "", None
    pw = _keychain(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", addr, "-w"], runner)
    pw = (pw or "").strip().replace(" ", "")   # Google shows it in groups of four; IMAP wants it without the spaces
    return addr, pw or None


def _decode(raw):
    try:
        return str(email.header.make_header(email.header.decode_header(raw or ""))).strip()
    except Exception:   # noqa: BLE001  broken encodings: keep what is readable
        return str(raw or "").strip()


def _parse_fetch(data, with_body=False):
    """IMAP FETCH response -> [{'from','addr','subject','important'[,'msgid','date','body']}], newest last."""
    out = []
    for part in data or []:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        meta = part[0].decode("utf-8", "replace") if isinstance(part[0], bytes) else str(part[0])
        labels = re.search(r"X-GM-LABELS \((.*?)\)", meta)
        h = email.parser.BytesHeaderParser().parsebytes(part[1] if isinstance(part[1], bytes) else str(part[1]).encode())
        name, addr = email.utils.parseaddr(_decode(h.get("From")))
        m = {"from": name or addr, "addr": addr.lower(), "subject": _decode(h.get("Subject")) or "(no subject)",
             "important": bool(labels and "\\Important" in labels.group(1))}
        if with_body:
            raw = part[1] if isinstance(part[1], bytes) else str(part[1]).encode()
            try:
                m["body"] = mailsum.body_text(email.message_from_bytes(raw))
            except Exception:   # noqa: BLE001  a malformed message just has no gist
                m["body"] = ""
            m.update(msgid=str(h.get("Message-ID") or "").strip(), date=str(h.get("Date") or ""))
        out.append(m)
    return out


def fetch_mail(cfg, password, imap_factory=None):
    """{'gmail': [...primary unread...], 'linkedin': [...unread LinkedIn mail...]} over one read-only IMAP session."""
    g = settings(cfg)["gmail"]
    addr = g["address"] or gmail_account(cfg)[0]
    days, cap = int(g["days"]), int(g["max"])
    factory = imap_factory or (lambda host: imaplib.IMAP4_SSL(host, 993, timeout=12))
    m = factory(g["host"])
    try:
        m.login(addr, password)
        m.select("INBOX", readonly=True)   # read-only: nothing is marked read
        gmail_host = "gmail" in g["host"] or "google" in g["host"]
        queries = {"gmail": f"is:unread category:primary newer_than:{days}d -subject:jarviee",   # not our own reports
                   "linkedin": f"is:unread from:linkedin.com newer_than:{days}d"}
        out = {}
        for key, q in queries.items():
            if gmail_host:
                typ, data = m.uid("SEARCH", "X-GM-RAW", f'"{q}"')
            else:
                since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
                typ, data = m.uid("SEARCH", "UNSEEN", "SINCE", since, *(["FROM", "linkedin.com"] if key == "linkedin" else []))
            uids = (data[0] or b"").split() if typ == "OK" and data else []
            total = len(uids)
            uids = uids[-cap:]
            msgs = []
            if uids:
                # the gmail section reads the first 60 KB of each message (for its gist); LinkedIn only headers
                what = "BODY.PEEK[]<0.60000>" if key == "gmail" and g.get("summaries", True) else "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]"
                parts = f"(X-GM-LABELS {what})" if gmail_host else f"({what})"
                typ, data = m.uid("FETCH", b",".join(uids).decode(), parts)
                msgs = _parse_fetch(data, with_body=what.startswith("BODY.PEEK[]")) if typ == "OK" else []
            if key == "gmail":   # LinkedIn mail has its own section even when it lands in Primary
                msgs = [x for x in msgs if not x["addr"].endswith("linkedin.com")]
            out[key] = {"total": total, "messages": list(reversed(msgs))}
        return out
    finally:
        try:
            m.logout()
        except Exception:   # noqa: BLE001
            pass


LINKEDIN_KINDS = (   # (kind, pattern over sender + subject), first match wins
    ("message", r"messages-noreply|\bmessage|messaged|sent you a"),
    ("invitation", r"invitations@|invitation|wants to connect|join your network|accepted your"),
    ("mention", r"mentioned|commented|tagged|replied|reacted|endorsed"),
    ("job", r"jobs-|jobalerts|job alert|is hiring|\bjobs?\b|applied|application|recruiter"),
    ("profile view", r"viewed your profile|profile views|appeared in"),
)


def linkedin_kind(m):
    text = f"{m.get('addr', '')} {m.get('subject', '')}".lower()
    for kind, rx in LINKEDIN_KINDS:
        if re.search(rx, text):
            return kind
    return "update"


def read_mail(cfg, account=None, imap_factory=None, summarise_s=None, runners=None):
    """(gmail section, linkedin section) from one IMAP session; each Gmail message gets a gist (hub/mailsum.py),
    asked for at most `summarise_s` seconds (cached ones are instant). Email text never leaves this function."""
    addr, pw = account or gmail_account(cfg)
    if not pw:
        if sys.platform != "darwin" and account is None:
            hint = "Not available on this platform (Gmail uses the macOS Keychain)."
        else:
            hint = "Not connected: make a Google App Password, then run `python3 hub.py connect-gmail you@gmail.com`."
        off = _section(False, connected=False, error=hint)
        return off, _section(False, connected=False, error="Reads LinkedIn's emails, so it needs Gmail connected first.")
    try:
        mail = fetch_mail({**(cfg or {}), "briefing": {**settings(cfg), "gmail": {**settings(cfg)["gmail"], "address": addr}}},
                          pw, imap_factory)
    except (imaplib.IMAP4.error, OSError, ValueError) as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg.upper() or "invalid credentials" in msg.lower():
            msg = "Gmail refused the app password. Make a new one and run `python3 hub.py connect-gmail` again."
        return _section(False, error=f"Gmail: {msg}"), _section(False, error=f"Gmail: {msg}")
    g, li = mail["gmail"], mail["linkedin"]
    gs = settings(cfg)["gmail"]
    if gs.get("summaries", True) and g["messages"]:
        wait_s = float(gs["summary_wait_s"] if summarise_s is None else summarise_s)
        mailsum.summarise(cfg, g["messages"], deadline=time.time() + wait_s, runners=runners)
    for x in g["messages"]:
        x.pop("body", None)   # the text itself stays out of the API, the speech and the phone
    kinds = {}
    for x in li["messages"]:
        x["kind"] = linkedin_kind(x)
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    return (_section(address=addr, unread=g["total"], messages=g["messages"], spoken=int(gs.get("spoken", 5))),
            _section(unread=li["total"], kinds=kinds, messages=li["messages"]))


# ---------------------------------------------------------------- github
GH_REASONS = {"review_requested": "review request", "mention": "mention", "team_mention": "mention",
              "assign": "assignment", "security_alert": "security alert", "ci_activity": "CI run",
              "invitation": "invitation", "author": "reply", "comment": "reply", "state_change": "status change",
              "manual": "update", "subscribed": "update", "approval_requested": "approval request"}
GH_ORDER = ("security alert", "review request", "approval request", "mention", "assignment", "CI run", "invitation", "reply")


def _gh_bin():
    return shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None)


def read_github(cfg, runner=subprocess.run):
    gh = _gh_bin()
    if not gh:
        return _section(False, connected=False, error="Not connected: install the GitHub CLI and run `gh auth login`.")
    cap = int(settings(cfg)["github"]["max"])
    try:
        r = runner([gh, "api", f"notifications?per_page={cap}"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _section(False, error=f"GitHub: {e}")
    if r.returncode:
        err = (r.stderr or "").strip().splitlines()[-1:] or ["gh failed"]
        connected = "auth login" not in err[0]
        return _section(False, connected=connected, error=("Not connected: run `gh auth login`." if not connected else f"GitHub: {err[0]}"))
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return _section(False, error="GitHub: unreadable reply from gh")
    items, kinds = [], {}
    for n in rows if isinstance(rows, list) else []:
        kind = GH_REASONS.get(n.get("reason"), "update")
        kinds[kind] = kinds.get(kind, 0) + 1
        items.append({"kind": kind, "repo": ((n.get("repository") or {}).get("name") or ""),
                      "title": ((n.get("subject") or {}).get("title") or "").strip(), "type": (n.get("subject") or {}).get("type") or ""})
    rank = {k: i for i, k in enumerate(GH_ORDER)}
    items.sort(key=lambda x: rank.get(x["kind"], 99))
    return _section(unread=len(items), kinds=kinds, items=items)


# ---------------------------------------------------------------- whatsapp
def read_whatsapp(cfg, now=None):
    w = settings(cfg)["whatsapp"]
    db = Path(os.path.expanduser(w["db"]))
    if not db.exists():
        return _section(False, connected=False, error="Not connected: install WhatsApp Desktop from the App Store and link it to your phone.")
    now = now or datetime.now()
    tmp = tempfile.mkdtemp(prefix="jarviee-wa-")
    try:   # work on a copy: WhatsApp keeps the live file open and in WAL mode
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(db) + suffix)
                if src.exists():
                    shutil.copy2(src, Path(tmp) / ("chat.sqlite" + suffix))
        except PermissionError as e:
            return _section(False, error="macOS blocked access to WhatsApp's data. Give Python.app "
                                         "(/Library/Frameworks/Python.framework/Versions/…/Resources/Python.app) Full Disk Access "
                                         f"in System Settings → Privacy & Security. ({Path(e.filename or '').name}: {e.strerror})")
        con = sqlite3.connect(f"file:{Path(tmp) / 'chat.sqlite'}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(ZWACHATSESSION)")}
            if not {"ZPARTNERNAME", "ZUNREADCOUNT"} <= cols:
                return _section(False, error="WhatsApp: this version's database layout isn't known yet.")
            pick = ["ZPARTNERNAME", "ZUNREADCOUNT"] + [c for c in ("ZLASTMESSAGEDATE", "ZCONTACTJID", "ZARCHIVED") if c in cols]
            rows = [dict(zip(pick, r)) for r in con.execute(f"SELECT {', '.join(pick)} FROM ZWACHATSESSION WHERE ZUNREADCOUNT > 0")]
        finally:
            con.close()
    except sqlite3.Error as e:
        return _section(False, error=f"WhatsApp: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    cutoff = now.timestamp() - int(w["days"]) * 86400
    chats = []
    for r in rows:
        jid = str(r.get("ZCONTACTJID") or "")
        when = (r.get("ZLASTMESSAGEDATE") or 0) + APPLE_EPOCH if r.get("ZLASTMESSAGEDATE") else None
        if r.get("ZARCHIVED") or "broadcast" in jid or (when and when < cutoff):
            continue
        chats.append({"name": str(r.get("ZPARTNERNAME") or "Unknown"), "unread": int(r["ZUNREADCOUNT"]),
                      "group": jid.endswith("@g.us"), "when": when or 0})
    chats.sort(key=lambda c: (c["group"], -c["when"]))   # people before groups, newest first
    return _section(chats=chats, unread=sum(c["unread"] for c in chats))


# ---------------------------------------------------------------- agents
def read_agents(store):
    if store is None:
        return _section(waiting=[])
    rows = [r for r in store.all() if r.get("status") == "waiting" and not r.get("cleared")]
    return _section(waiting=[{"id": r.get("id"), "title": r.get("title") or (r.get("task") or "")[:60],
                              "project": r.get("project") or ""} for r in rows])


# ---------------------------------------------------------------- building
def gather(cfg, store=None, readers=None, now=None):
    """Every enabled source, read in parallel; a slow one is reported as timed out, never waited on for long."""
    s = settings(cfg)
    on = [k for k in SOURCES if k in s["sources"]]
    mail = {}
    mail_lock = threading.Lock()

    def mail_part(key):
        with mail_lock:   # gmail and linkedin share one IMAP session
            if "pair" not in mail:
                mail["pair"] = read_mail(cfg)
        return mail["pair"][0 if key == "gmail" else 1]

    default = {"calendar": lambda: read_calendar(cfg, now), "gmail": lambda: mail_part("gmail"),
               "linkedin": lambda: mail_part("linkedin"), "github": lambda: read_github(cfg),
               "whatsapp": lambda: read_whatsapp(cfg, now), "agents": lambda: read_agents(store)}
    fns = {k: (readers or {}).get(k) or default[k] for k in on}
    out = {}
    pool = ThreadPoolExecutor(max_workers=len(fns) or 1, thread_name_prefix="jarviee-brief")
    futs = {k: pool.submit(f) for k, f in fns.items()}
    base = float(s["timeout_s"])
    mail_s = base + (float(s["gmail"]["summary_wait_s"]) + 10 if s["gmail"].get("summaries", True) else 0)
    limits = {k: mail_s if k in ("gmail", "linkedin") else base for k in futs}
    start = time.time()
    for k, f in futs.items():
        wait([f], timeout=max(0.0, start + limits[k] - time.time()))
    for k, f in futs.items():
        if not f.done():
            out[k] = _section(False, error=f"{k}: no answer within {limits[k]:g} s")
            continue
        try:
            out[k] = f.result()
        except Exception as e:   # noqa: BLE001  one broken source never sinks the briefing
            out[k] = _section(False, error=f"{k}: {e}")
    pool.shutdown(wait=False)
    return out


def _greeting(now):
    return "Good morning." if now.hour < 12 else "Good afternoon." if now.hour < 18 else "Good evening."


def _sp(text, limit=90):
    return voice.sanitise_spoken(text, limit)


def _gmail_speech(g):
    """Who wrote and what it says, important and to-do mail first; the rest only counted."""
    msgs = sorted(g["messages"], key=lambda m: (not m.get("important"), not m.get("action")))
    head = f"In Gmail, {_plural(g['unread'], 'unread email')} in your primary inbox."
    if not any(m.get("gist") for m in msgs):
        return head[:-1] + ", from " + _names([_sp(m["from"], 40) for m in msgs]) + "."
    said = []
    for m in msgs[:max(0, int(g.get("spoken", 5)))]:
        gist = _sp(m.get("gist") or m["subject"], 200).rstrip(".")
        todo = _sp(m.get("action") or "", 100).rstrip(".")
        said.append(f"{_sp(m['from'], 40)}: {gist}." + (f" To do: {todo}." if todo else ""))
    rest = g["unread"] - len(said)
    return " ".join([head] + said + ([f"And {_plural(rest, 'more email')}."] if rest > 0 else []))


def speech(sections, now=None):
    """What Jarviee says. Only sources that answered; names and counts, never message text."""
    now = now or datetime.now()
    parts = [_greeting(now) + " Here's what's ahead."]
    c = sections.get("calendar") or {}
    if c.get("ok"):
        today = [e for e in c["today"] if not e["all_day"]]
        allday = [e for e in c["today"] if e["all_day"]]
        if today:
            first, rest = today[0], today[1:4]
            head = (f"Right now you're in {_sp(first['title'])}" if first["ongoing"]
                    else f"Next up is {_sp(first['title'])} at {first['time']}")
            parts.append(f"You have {_plural(len(today), 'more event')} today. {head}"
                         + (", then " + ", ".join(f"{_sp(e['title'])} at {e['time']}" for e in rest) if rest else "")
                         + ("." if len(today) <= 4 else f", and {len(today) - 4} more."))
        elif not allday:
            parts.append("Nothing else on your calendar today.")
        if allday:
            parts.append("All day: " + _names([_sp(e["title"], 60) for e in allday]) + ".")
        timed_tomorrow = [e for e in c["tomorrow"] if not e["all_day"]]
        if timed_tomorrow:
            parts.append(f"Tomorrow starts with {_sp(timed_tomorrow[0]['title'])} at {timed_tomorrow[0]['time']}.")
        if c.get("heads_up"):
            parts.append("Heads up: " + " ".join(_sp(h, 120) for h in c["heads_up"][:3]))
    g = sections.get("gmail") or {}
    if g.get("ok"):
        if g["unread"]:
            parts.append(_gmail_speech(g))
        else:
            parts.append("Your primary inbox is clear.")
    li = sections.get("linkedin") or {}
    if li.get("ok") and li["unread"]:
        parts.append("On LinkedIn, " + _names([_plural(n, k) for k, n in sorted(li["kinds"].items(), key=lambda kv: -kv[1])], 4) + ".")
    gh = sections.get("github") or {}
    if gh.get("ok") and gh["unread"]:
        top = gh["items"][0]
        kinds = _names([_plural(n, k) for k, n in sorted(gh["kinds"].items(), key=lambda kv: GH_ORDER.index(kv[0]) if kv[0] in GH_ORDER else 99)], 4)
        parts.append(f"On GitHub, {kinds}" + (f". The first is a {top['kind']} on {_sp(top['repo'], 40)}" if top["repo"] else "") + ".")
    wa = sections.get("whatsapp") or {}
    if wa.get("ok") and wa["chats"]:
        n = len(wa["chats"])
        parts.append(f"On WhatsApp, {_plural(n, 'chat')} with unread messages, from " + _names([_sp(ch["name"], 40) for ch in wa["chats"]]) + ".")
    ag = sections.get("agents") or {}
    if ag.get("ok") and ag["waiting"]:
        parts.append(f"{_plural(len(ag['waiting']), 'agent')} {'is' if len(ag['waiting']) == 1 else 'are'} waiting for you.")
    if len(parts) == 1:
        parts.append("Nothing on your plate yet.")
    return " ".join(parts)


ICON = {"calendar": "📅", "gmail": "📧", "linkedin": "💼", "github": "🐙", "whatsapp": "💬", "agents": "🤖"}
LABEL = {"calendar": "Calendar", "gmail": "Gmail", "linkedin": "LinkedIn", "github": "GitHub", "whatsapp": "WhatsApp", "agents": "Agents"}


def lines(sections):
    """{source: [display lines]} for the command bar (subjects shown on screen, never spoken or pushed)."""
    out = {}
    for k, sec in sections.items():
        if not sec.get("ok"):
            out[k] = [sec.get("error") or "Unavailable"]
            continue
        if k == "calendar":
            ls = [f"{e['time']} · {e['title']}" + (" (now)" if e["ongoing"] else "") + (f" · {e['where']}" if e["where"] else "") for e in sec["today"]]
            ls = ls or ["Nothing else today"]
            ls += [f"Tomorrow {e['time']} · {e['title']}" for e in sec["tomorrow"][:2]]
            ls += [f"⚠ {h}" for h in sec["heads_up"]]
        elif k == "gmail":
            ls = []
            for m in sec["messages"][:12]:
                ls.append(f"{'★ ' if m['important'] else ''}{m['from']} · {m['subject']}")
                if m.get("gist"):
                    ls.append(f"    {m['gist']}")
                if m.get("action"):
                    ls.append(f"    → {m['action']}")
            ls = ls or ["Inbox clear"]
            if sec["unread"] > 12:
                ls.append(f"…and {sec['unread'] - 12} more")
        elif k == "linkedin":
            ls = [f"{m['kind']} · {m['subject']}" for m in sec["messages"][:6]] or ["Nothing new"]
        elif k == "github":
            ls = [f"{i['kind']} · {i['repo']} · {i['title']}" for i in sec["items"][:8]] or ["No unread notifications"]
        elif k == "whatsapp":
            ls = [f"{c['name']} · {c['unread']} unread" + (" (group)" if c["group"] else "") for c in sec["chats"][:8]] or ["No unread chats"]
        else:
            ls = [f"{a['title']}" + (f" ({a['project']})" if a["project"] else "") for a in sec["waiting"]] or ["Nobody is waiting on you"]
        out[k] = ls
    return out


def phone_messages(sections):
    """[(kind, title, body)] for ntfy, one per source that has something; thin on purpose (see module doc)."""
    msgs = []
    c = sections.get("calendar") or {}
    if c.get("ok") and (c["today"] or c["heads_up"]):
        n = len(c["today"])
        body = [f"{e['time']} · {e['title']}" for e in c["today"][:6]] + [f"⚠ {h}" for h in c["heads_up"][:3]]
        msgs.append(("briefing", f"Today: {_plural(n, 'event')} left" if n else "Today: nothing left on the calendar", "\n".join(body)))
    g = sections.get("gmail") or {}
    if g.get("ok") and g["unread"]:
        msgs.append(("gmail", f"Gmail: {_plural(g['unread'], 'unread email')}",
                     "From " + _names([m["from"] for m in g["messages"]], 5)))
    li = sections.get("linkedin") or {}
    if li.get("ok") and li["unread"]:
        msgs.append(("linkedin", f"LinkedIn: {_plural(li['unread'], 'update')}",
                     ", ".join(_plural(n, k) for k, n in li["kinds"].items())))
    gh = sections.get("github") or {}
    if gh.get("ok") and gh["unread"]:
        repos = _names([i["repo"] for i in gh["items"]], 4)
        msgs.append(("github", f"GitHub: {_plural(gh['unread'], 'notification')}",
                     ", ".join(_plural(n, k) for k, n in gh["kinds"].items()) + (f"\n{repos}" if repos else "")))
    wa = sections.get("whatsapp") or {}
    if wa.get("ok") and wa["chats"]:
        msgs.append(("whatsapp", f"WhatsApp: {_plural(len(wa['chats']), 'unread chat')}",
                     _names([ch["name"] for ch in wa["chats"]], 6)))
    ag = sections.get("agents") or {}
    if ag.get("ok") and ag["waiting"]:
        msgs.append(("agent_needs", f"Jarviee · {_plural(len(ag['waiting']), 'agent')} waiting",
                     _names([a["title"][:50] for a in ag["waiting"]], 4)))
    return msgs


def build(cfg, store=None, readers=None, now=None):
    now = now or datetime.now()
    sections = gather(cfg, store, readers, now)
    return {"at": now.timestamp(), "text": speech(sections, now), "sections": sections, "lines": lines(sections),
            "order": [k for k in SOURCES if k in sections], "icons": ICON, "labels": LABEL}


def whatsapp_text(msgs):
    """The per-source phone messages as one WhatsApp message, each with its icon."""
    from . import push
    emoji = {"briefing": "🌅", "gmail": "📧", "linkedin": "💼", "github": "🐙", "whatsapp": "💬", "agent_needs": "🙋"}
    return "\n\n".join(f"{emoji.get(k, '')} *{t}*\n{b}".strip() for k, t, b in msgs)


def email_text(brief):
    """The whole briefing for the owner's own inbox: what was said, then every source's lines (email gists included,
    since this goes from the owner's Gmail to itself)."""
    out = [brief["text"], ""]
    for k in brief.get("order") or list(brief["sections"]):
        out.append(f"{ICON.get(k, '')} {LABEL.get(k, k)}")
        out += [f"   {l}" for l in (brief.get("lines") or {}).get(k, [])]
        out.append("")
    return "\n".join(out).strip()


def email_html(brief):
    """HTML version of the whole briefing, with each source in its own card."""
    from . import mailout
    cards = []
    for k in brief.get("order") or list(brief.get("sections") or {}):
        lines = (brief.get("lines") or {}).get(k, [])
        if not lines:
            continue
        rows = []
        for l in lines:
            indented = l.startswith("    ")
            style = ("font-size:13px;line-height:1.45;color:#475569;padding:2px 0 2px 12px;"
                     if indented else "font-size:14px;font-weight:600;color:#0f172a;padding:6px 0 2px 0;")
            rows.append(f'<div style="{style}">{_html.escape(l.strip())}</div>')
        heading = f"{ICON.get(k, '')} {LABEL.get(k, k)}".strip()
        cards.append(mailout.card_html(heading, "".join(rows)))
    return mailout.wrap_html("What's ahead", brief.get("text") or "", "".join(cards))


def send_to_phone(cfg, brief, publish=None, wa_send=None):
    """WhatsApp on: one combined (thin) message there. Email on: the whole briefing to your own inbox. Otherwise one
    ntfy message per source, each with its own icon. Returns how many were sent."""
    from . import channels, mailout, push
    if not settings(cfg)["phone"]:
        return 0
    msgs = phone_messages(brief["sections"])
    direct = wa_send or channels.summary_sender(cfg, "briefing")
    if direct is mailout.send:
        ok, _ = direct(cfg, "What's ahead", email_text(brief), html=email_html(brief))
        return int(bool(ok))
    if direct is not None:
        if not msgs:
            return 0
        ok, _ = direct(cfg, "What's ahead", whatsapp_text(msgs))
        return int(bool(ok))
    if not push.settings(cfg).get("enabled"):
        return 0
    publish = publish or push.publish
    sent = 0
    for kind, title, body in msgs:
        ok, _ = publish(cfg, title, body or title, priority=3, kind=kind)
        sent += bool(ok)
    return sent


def startwork(cfg, store=None, speaker=None, readers=None, publish=None, now=None):
    """Build the briefing, speak it, send it to the phone (in the background). Returns the briefing."""
    brief = build(cfg, store, readers, now)
    speaker = speaker if speaker is not None else voice.ACTIVE
    brief["spoken"] = bool(speaker and speaker.speak(brief["text"], event="startwork"))
    if publish is not None:
        brief["phone"] = send_to_phone(cfg, brief, publish)
    else:
        threading.Thread(target=send_to_phone, args=(cfg, brief), daemon=True, name="jarviee-brief-push").start()
    return brief


def connect_gmail(address, runner=subprocess.run, check=True):
    """Store a Google App Password in the login Keychain. `security` prompts for it itself, so it never
    appears in a command line or in this process's arguments. Then try one read-only login."""
    if sys.platform != "darwin":
        return False, "Gmail Keychain storage is not available on this platform (macOS only)."
    address = address.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return False, "That doesn't look like an email address."
    print("Paste the 16-letter Google App Password (spaces are fine). It goes into your macOS Keychain.")
    try:
        r = runner(["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", address,
                    "-l", "Jarviee Gmail (IMAP app password)", "-w"])
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Keychain is not available on this platform ({e})."
    if r.returncode:
        return False, "Keychain didn't save it."
    if not check:
        return True, "Saved."
    account = gmail_account({"briefing": {"gmail": {"address": address}}}, runner)
    if not account[1]:
        return False, "Saved, but it can't be read back from the Keychain."
    g, li = read_mail({}, account)
    return (True, f"Connected: {g['unread']} unread in Primary, {li['unread']} from LinkedIn.") if g["ok"] else (False, g["error"])
