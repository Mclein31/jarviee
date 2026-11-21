"""Summaries by email, through the Gmail account the briefing already reads (same App Password, SMTP).

Owner, 2026-11-14: summaries go to Gmail (CallMeBot's WhatsApp relay was full). Kinds: `digest` (the 08:00 schedule),
`briefing` (/startwork), `summary` (/endwork's day report). Quick alerts stay on ntfy.

The mail goes from the owner's Gmail to itself, so no new third party sees it, and the /startwork email can carry the
full briefing including email gists. Every Jarviee email has "Jarviee" in its subject and an `X-Jarviee` header, and the
briefing's inbox search leaves those out, so Jarviee never reads its own reports back as new mail.
"""
import html as _html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

DEFAULTS = {"enabled": False, "to": "", "kinds": ["digest", "briefing", "summary"]}   # turned on by `hub.py summaries email`


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("email_send") or {})}


def _account(cfg):
    from . import briefing
    return briefing.gmail_account(cfg)


def handles(cfg, kind, account=None):
    s = settings(cfg)
    if not (s["enabled"] and kind in s["kinds"]):
        return False
    return bool((account or _account(cfg))[1])


def subject_of(title):
    t = (title or "").strip()
    for lead in ("Jarviee · ", "Jarviee: "):
        if t.startswith(lead):
            t = t[len(lead):]
    return f"Jarviee · {t}"


def card_html(heading, items_html, badge=""):
    """One separated project/source card for a Jarviee summary email."""
    badge_html = (f'<span style="float:right;font-size:12px;font-weight:500;color:#64748b;'
                  f'background:#f1f5f9;padding:2px 8px;border-radius:999px;">{_html.escape(badge)}</span>'
                  if badge else "")
    return (
        '<div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;'
        'padding:14px 16px;margin-bottom:14px;">'
        f'<div style="font-size:14px;font-weight:600;color:#0f172a;margin:0 0 10px 0;'
        f'padding-bottom:8px;border-bottom:1px solid #f1f5f9;">{badge_html}{_html.escape(heading)}</div>'
        f'{items_html}'
        '</div>'
    )


def wrap_html(title, lead, sections_html, footer=""):
    """Clean, readable HTML shell for Jarviee summary emails."""
    lead_html = (f'<p style="margin:6px 0 18px 0;font-size:14px;line-height:1.5;color:#475569;">'
                 f'{_html.escape(lead)}</p>' if lead else "")
    footer_html = (f'<p style="margin:14px 0 0 0;font-size:13px;color:#64748b;">'
                   f'{_html.escape(footer)}</p>' if footer else "")
    return (
        '<!doctype html><html><body style="margin:0;padding:20px 12px;background:#f8fafc;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">'
        '<div style="max-width:620px;margin:0 auto;">'
        f'<div style="font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#64748b;">Jarviee</div>'
        f'<h1 style="margin:4px 0 0 0;font-size:20px;font-weight:700;color:#0f172a;">{_html.escape(title)}</h1>'
        f'{lead_html}'
        f'{sections_html}'
        f'{footer_html}'
        '</div></body></html>'
    )


def build(sender, to, title, body, html=None):
    msg = EmailMessage()
    msg["Subject"] = subject_of(title)
    msg["From"] = formataddr(("Jarviee", sender))
    msg["To"] = to
    msg["X-Jarviee"] = "summary"
    msg["Message-ID"] = make_msgid(domain="jarviee.local")
    msg.set_content(body or title)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _ctx():
    from . import push
    try:
        return push._ssl_context()
    except Exception:   # noqa: BLE001
        return ssl.create_default_context()


def send(cfg, title, body, account=None, smtp_factory=None, html=None, **_):
    """(ok, error). Never raises."""
    addr, pw = account or _account(cfg)
    if not pw:
        return False, "Gmail isn't connected (python3 hub.py connect-gmail you@gmail.com)"
    to = settings(cfg)["to"] or addr
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20, context=_ctx()))
    try:
        with factory() as s:
            s.login(addr, pw)
            s.send_message(build(addr, to, title, body, html=html))
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "Gmail refused the app password for sending."
    except (smtplib.SMTPException, OSError) as e:
        return False, f"Gmail: {e}"
