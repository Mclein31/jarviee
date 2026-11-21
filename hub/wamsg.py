"""Summaries to the owner's WhatsApp through CallMeBot (https://www.callmebot.com/blog/free-api-whatsapp-messages/).

Owner's choice (2026-11-14): summaries go to WhatsApp, quick alerts stay on ntfy. Kinds sent here:
  digest    the 08:00 schedule (hub/push.py)
  briefing  /startwork, one combined message (hub/briefing.py)
  summary   work summaries: /endwork's day report, "today's work" (hub/daybook.py)

CallMeBot is a free third-party relay: it sees the text and limits how often it sends, so messages are kept short and
carry the same thin content as the phone notifications (counts and names, never email text). Messages can't be
queued for later the way ntfy's can, so the 08:00 schedule goes out when Jarviee is running at 08:00 (or as soon as
the Mac wakes, up to five hours late).

The API key is kept in the macOS Keychain (service "jarviee-callmebot", account = the phone number), never in
config or the repo. `python3 hub.py connect-whatsapp +63…` stores it and sends a test.
"""
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

KEYCHAIN_SERVICE = "jarviee-callmebot"
API = "https://api.callmebot.com/whatsapp.php"
DEFAULTS = {"enabled": False,   # turned on by connect() (saved in ~/.jarviee/settings.json)
             "kinds": ["digest", "briefing", "summary"], "max_chars": 1500}
_PHONE_RE = re.compile(r"^\+\d{7,15}$")


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("whatsapp_send") or {})}


def _security(args, runner=subprocess.run):
    try:
        r = runner(["/usr/bin/security", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def account(runner=subprocess.run):
    """(phone, api key) from the Keychain, or ("", None) when WhatsApp isn't connected."""
    out = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE], runner) or ""
    m = re.search(r'"acct"<blob>="([^"]+)"', out)
    if not m:
        return "", None
    key = (_security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", m.group(1), "-w"], runner) or "").strip()
    return m.group(1), key or None


def handles(cfg, kind, runner=subprocess.run):
    """True when messages of this kind go to WhatsApp instead of ntfy."""
    s = settings(cfg)
    return bool(s["enabled"] and kind in s["kinds"] and account(runner)[1])


def text_of(title, body, limit):
    t = f"*{title.strip()}*\n{(body or '').strip()}".strip()
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def _ctx():
    from . import push   # same CA fallback as ntfy (python.org builds ship without one)
    return push._ssl_context()


def send(cfg, title, body, opener=None, acct=None):
    """(ok, error). Never raises."""
    phone, key = acct or account()
    if not key:
        return False, "WhatsApp isn't connected (python3 hub.py connect-whatsapp +<number>)"
    q = urllib.parse.urlencode({"phone": phone, "text": text_of(title, body, int(settings(cfg)["max_chars"])), "apikey": key})
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "jarviee"})
    try:
        if opener is None:
            opener = lambda r, timeout: urllib.request.urlopen(r, timeout=timeout, context=_ctx())   # noqa: E731
        with opener(req, timeout=20) as r:
            reply = r.read(2000).decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        return False, f"CallMeBot said {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"CallMeBot unreachable: {getattr(e, 'reason', e)}"
    plain = " ".join(re.sub(r"<[^>]+>", " ", reply).split())
    if status == 200 and re.search(r"(?i)\b(queued|sent)\b", plain) and not re.search(r"(?i)\b(error|invalid)\b", plain):
        return True, ""
    return False, "CallMeBot: " + (plain[:160] or f"HTTP {status}")


def connect(phone, cfg=None, runner=subprocess.run, check=True, opener=None):
    """Store the CallMeBot API key in the Keychain (`security` prompts for it, so it is never in argv), then test."""
    if sys.platform != "darwin":
        return False, "WhatsApp Keychain storage is not available on this platform (macOS only)."
    phone = re.sub(r"[\s()-]", "", phone or "")
    if not _PHONE_RE.match(phone):
        return False, "Give the number in international form, e.g. +639171234567."
    old = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE], runner) or ""
    m = re.search(r'"acct"<blob>="([^"]+)"', old)
    if m and m.group(1) != phone:   # one number only
        _security(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", m.group(1)], runner)
    print("Paste the API key CallMeBot sent you on WhatsApp. It goes into your macOS Keychain.")
    try:
        r = runner(["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", phone,
                    "-l", "Jarviee WhatsApp (CallMeBot API key)", "-w"])
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Keychain is not available on this platform ({e})."
    if r.returncode:
        return False, "Keychain didn't save it."
    if cfg is not None:   # summaries now go here (restart Jarviee to pick it up)
        from . import config
        cfg.setdefault("whatsapp_send", {})["enabled"] = True
        config.save_settings(cfg)
    if not check:
        return True, "Saved."
    ok, err = send({}, "Jarviee is connected", "Summaries will arrive here: your 8:00 schedule, /startwork and the end of your workday.",
                   opener=opener, acct=account(runner))
    return (True, "Connected: a test message is on its way to your WhatsApp.") if ok else (False, err)
