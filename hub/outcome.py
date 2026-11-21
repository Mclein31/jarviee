"""Decide how a finished run really ended: done, failed or needs_you. Pure, no I/O.

An agent that exits 0 is not always done: it may have stopped to ask a question, wait for an
approval, or say it is blocked. Closing that pane would hide a run that needs the owner, so any
doubt resolves to needs_you (pane stays open), never to done.
"""
import re

DONE, FAILED, NEEDS_YOU = "done", "failed", "needs_you"

# Real blockers only: a permission/approval prompt, a decision or input the agent cannot continue
# without, or a credential/login request. Polite sign-offs ("let me know if...", "should I also...?")
# are deliberately NOT here.
ASK_PATTERNS = [
    r"\bpermission (denied|required)\b", r"\bauto-denied\b", r"\b(waiting|awaiting) (for )?(your|you|approval|confirmation)\b",
    r"\bneed(s)? (your|you to) (approval|input|decision|confirmation|answer|permission)\b",
    r"\bplease (confirm|approve|choose|decide|clarify)\b", r"\bapprove (this|the|these)\b",
    r"\bdo you want (to|me to) proceed\b", r"\b(should|shall|can) i (go ahead|proceed|continue)\b",
    r"\bwhich (\w+ ){0,2}(should i|do you (want|prefer)|would you (like|prefer))\b", r"\bshould i (use|pick|choose|go with)\b",
    r"\bdo you want to proceed\b", r"\(y/n\)", r"\bbefore i (proceed|continue|go ahead)\b",
    r"\bi('m| am) (blocked|stuck)\b", r"\bcan(no|')t (proceed|continue) without\b",
    r"\bcannot proceed\b", r"\bcan't proceed\b", r"\bneed more information\b",
    r"\b(please )?(log|sign) in\b", r"\b(login|authentication|credentials?|api key) (is |are )?(required|missing|needed)\b",
    r"\bnot (logged|signed) in\b", r"\bneed(s)? (your |a )?(credentials|api key|password|token)\b",
]
_ASK_RE = re.compile("|".join(ASK_PATTERNS), re.I)
# an optional follow-up offer, never a blocker
_OPTIONAL_Q = re.compile(r"\b(want me to|would you like me to|should i also|shall i also|anything else|"
                         r"would you like (to|any|more)|let me know)\b", re.I)
_COMPLETION = re.compile(r"\b(done|completed?|finished|implemented|fixed|tests? (pass|passed|passing)|"
                         r"committed|summary|all set|changed|updated|added|created|edited|refactored)\b", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
TAIL_CHARS = 1200   # only the closing message counts, not a question asked mid-run


def closing_message(log_text):
    """The last chunk of agent output, ANSI stripped, runner/hub bookkeeping lines removed."""
    text = _ANSI.sub("", log_text or "").replace("\r", "")
    lines = [l for l in text.splitlines()
             if l.strip() and not l.startswith(("[pane]", "[agy]", "$ ", "✓", "✗", "■"))]
    return "\n".join(lines)[-TAIL_CHARS:]


def asks_owner(log_text):
    """The matched phrase when the closing message hands the turn back to the owner, else ''."""
    tail = closing_message(log_text)
    last = [l for l in tail.splitlines() if l.strip()][-6:]     # the final few lines decide
    m = _ASK_RE.search("\n".join(last))
    if m:
        return m.group(0)
    if last and last[-1].rstrip().endswith("?"):
        body = " ".join(re.split(r"(?<=[.!?:])\s+|\n", tail.strip())[:-1])    # everything before the closing sentence
        if _COMPLETION.search(body):
            return ""       # reported finished work, then a polite or optional question: not a blocker
        return "ends with a blocking question"     # a question with nothing finished before it, even "want me to...?"
    return ""


def classify(code, log_text="", stopped=False, soft_denied=False, empty_output=False, extra_blocked=()):
    """(kind, reason). Order matters: stopped by the owner is never reported as done.

    - nonzero exit            -> failed
    - exit 0 but soft denied  -> failed (agy refused a command and changed nothing)
    - exit 0 but a real blocker (permission, blocking question, login) -> needs_you
    - exit 0 with no output   -> done
    - otherwise               -> done
    """
    if stopped:
        return FAILED, "stopped"
    if code not in (0, None):
        return FAILED, f"exit code {code}"
    if soft_denied:
        return FAILED, "agy refused a shell command in print mode and changed no files"
    low = (log_text or "").lower()
    for pat in extra_blocked or ():
        if pat and pat.lower() in closing_message(low):
            return NEEDS_YOU, f"reported it was blocked ('{pat}')"
    hit = asks_owner(log_text)
    if hit:
        return NEEDS_YOU, f"asked you something ({hit})"
    return DONE, "finished"


def may_close(status, note=""):
    """True only for a pane whose run is really finished: done, stopped by the owner, or a failure handed to a retry."""
    note = str(note or "")
    if status in ("done", "stopped"):   # stopped = the owner pressed Stop: they already know
        return True
    if status == "failed":
        return note.lower().startswith("retrying")
    return False   # running, waiting (needs you), queued, final failure: never closed automatically
