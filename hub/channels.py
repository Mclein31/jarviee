"""Where a summary goes: WhatsApp (hub/wamsg.py) or email (hub/mailout.py) when one is switched on, else ntfy.

Only summary kinds (`digest`, `briefing`, `summary`) are ever redirected; quick alerts always use ntfy.
"""


def summary_sender(cfg, kind):
    """send(cfg, title, body) for this kind, or None to use ntfy."""
    from . import mailout, wamsg
    if wamsg.handles(cfg, kind):
        return wamsg.send
    if mailout.handles(cfg, kind):
        return mailout.send
    return None


def name(sender):
    from . import mailout, wamsg
    return {wamsg.send: "whatsapp", mailout.send: "email"}.get(sender, "ntfy")
