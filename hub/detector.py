"""Detect agent failure types: QUOTA, TRANSIENT, AUTH, and TASK_FAILURE.

Parses error output from agy and Claude Code to classify failures and extract reset timestamps.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import re
import time
from typing import Optional

QUOTA = "quota"
TRANSIENT = "transient"
AUTH = "auth"
TASK_FAILURE = "task_failure"

# Regex for "Resets in 151h36m47s", "resets in 2h 15m", "resets in 45m"
_RE_IN_DURATION = re.compile(
    r"resets?\s+in\s+(?:(?P<days>\d+)\s*d(?:ays?)?\s*)?"
    r"(?:(?P<hours>\d+)\s*h(?:ours?|rs?)?\s*)?"
    r"(?:(?P<mins>\d+)\s*m(?:inutes?|ins?)?\s*)?"
    r"(?:(?P<secs>\d+)\s*s(?:econds?|ecs?)?)?",
    re.IGNORECASE
)

# Regex for "resets at 3pm", "resets at 3:30 PM", "resets at 15:45"
_RE_AT_TIME = re.compile(
    r"resets?\s+at\s+(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)?",
    re.IGNORECASE
)

TRANSIENT_PATTERNS = [
    r"\b529\s+overloaded\b",
    r"\b50[0234]\b",
    r"\binternal\s+server\s+error\b",
    r"\bbad\s+gateway\b",
    r"\bservice\s+unavailable\b",
    r"\bgateway\s+timeout\b",
    r"\bconnection\s+reset\b",
    r"\bsocket\s+timeout\b",
    r"\bnetwork\s+timeout\b",
    r"\btimed?\s*out\b",
]

AUTH_PATTERNS = [
    r"\bnot\s+logged\s+in\b",
    r"\blogin\s+required\b",
    r"\bplease\s+login\b",
    r"\bauth(?:entication)?\s+failed\b",
    r"\bunauthenticated\b",
    r"\binvalid\s+api\s+key\b",
    r"\bnot\s+found\s+on\s+path\b",
    r"\bbinary\s+missing\b",
    r"\bno\s+such\s+file\s+or\s+directory\b",
]

QUOTA_PATTERNS = [
    r"resource_exhausted",
    r"individual\s+quota\s+reached",
    r"quota\s+(?:reached|exceeded)",
    r"(?:reached\s+(?:your\s+)?)?usage\s+limit",
    r"plan-limit",
    r"rate_limit_error",
    r"429\s+(?:too\s+many\s+requests|rate_limit|resource_exhausted)",
    r"5-hour\s+limit",
    r"hit\s+your\b",
    r"out\s+of\s+credits",
    r"insufficient\s+credits",
]

OMNIROUTE_QUOTA_PATTERNS = [
    r"all\s+providers\s+failed",
    r"maximum\s+combo\s+retry\s+limit",
    r"all_targets_skipped",
    r"all\s+upstream\s+accounts\s+are\s+inactive",
    r"rate\s+limit\s+reached",
    r"api\s+error[:\s]+429\b",
    r"http\s+429\b",
    r"status(?:\s*code)?[:\s]+429\b",
    r'"status"\s*:\s*429\b',
    r"\b429\b[^\n]{0,40}(?:too\s+many\s+requests|\brate[\s_-]?limit)",
]


@dataclass
class FailureDetection:
    kind: str                        # QUOTA | TRANSIENT | AUTH | TASK_FAILURE
    scope: str = "model"             # "tool" (account-wide) or "model" (model-specific)
    reset_time: Optional[float] = None
    message: str = ""
    retryable: bool = False
    tool: str = ""


def parse_reset_time(text: str, now: Optional[float] = None) -> Optional[float]:
    """Parse relative or clock reset times into an absolute epoch timestamp."""
    if not text:
        return None
    ref = now if now is not None else time.time()

    m_in = _RE_IN_DURATION.search(text)
    if m_in and any(m_in.group(k) for k in ("days", "hours", "mins", "secs")):
        d = int(m_in.group("days") or 0)
        h = int(m_in.group("hours") or 0)
        m = int(m_in.group("mins") or 0)
        s = int(m_in.group("secs") or 0)
        delta = timedelta(days=d, hours=h, minutes=m, seconds=s).total_seconds()
        if delta > 0:
            return ref + delta

    m_at = _RE_AT_TIME.search(text)
    if m_at:
        hr = int(m_at.group("hour"))
        mn = int(m_at.group("min") or 0)
        ampm = (m_at.group("ampm") or "").lower()
        if ampm == "pm" and hr < 12:
            hr += 12
        elif ampm == "am" and hr == 12:
            hr = 0
        dt = datetime.fromtimestamp(ref)
        target = dt.replace(hour=hr, minute=mn, second=0, microsecond=0)
        if target.timestamp() <= ref:
            target += timedelta(days=1)
        return target.timestamp()

    return None


def detect_failure(tool: str, exit_code: Optional[int], log_text: str,
                   now: Optional[float] = None) -> Optional[FailureDetection]:
    """Detect failure type and details from exit code and log text."""
    if exit_code in (0, None):
        return None

    ref = now if now is not None else time.time()
    low = (log_text or "").lower()

    # 1. Check for QUOTA (account or model level)
    quota_matched = any(re.search(pat, low) for pat in QUOTA_PATTERNS)
    if tool == "omniroute" and not quota_matched:
        quota_matched = any(re.search(pat, low) for pat in OMNIROUTE_QUOTA_PATTERNS)
    # Check for agy JSON error payload
    agy_err = None
    if "agy_error" in low:
        m = re.search(r"AGY_ERROR:\s*(\{.*\})", log_text)
        if m:
            try:
                agy_err = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

    if quota_matched or (agy_err and agy_err.get("status") == "RESOURCE_EXHAUSTED"):
        is_account = bool(re.search(r"\b(?:individual\s+quota|5-hour\s+limit|hit\s+your\b|usage\s+limit|plan-limit)\b", low) or
                          (agy_err and "individual quota" in (agy_err.get("short_error") or "").lower()))
        if tool == "omniroute" and re.search(r"all\s+upstream\s+accounts\s+are\s+inactive", low):
            is_account = True
        scope = "tool" if is_account else "model"
        reset_time = parse_reset_time(log_text, now=ref)
        # Default cooldown if not parsed: 1 hour for Claude/OmniRoute, 6 hours for agy
        if reset_time is None:
            reset_time = ref + (21600.0 if tool == "agy" else 3600.0)
        if tool == "omniroute":
            # Gateway errors can look like Anthropic errors; always attribute to omniroute, never claude.
            msg = "omniroute quota/rate limit reached"
        else:
            msg = "Individual quota reached" if is_account else f"{tool} quota/rate limit reached"
        return FailureDetection(kind=QUOTA, scope=scope, reset_time=reset_time,
                                message=msg, retryable=False, tool=tool)

    # 2. Check for AUTH or binary missing
    if exit_code == 127 or any(re.search(pat, low) for pat in AUTH_PATTERNS):
        return FailureDetection(kind=AUTH, scope="tool", message=f"{tool} authentication or binary missing",
                                retryable=False, tool=tool)

    # 3. Check for TRANSIENT errors (5xx, 529, socket/network timeouts)
    if any(re.search(pat, low) for pat in TRANSIENT_PATTERNS):
        return FailureDetection(kind=TRANSIENT, scope="model",
                                reset_time=ref + 30.0,
                                message=f"{tool} transient error (overloaded/network)", retryable=True, tool=tool)

    # 4. Normal task failure after real work
    return FailureDetection(kind=TASK_FAILURE, scope="model", message=f"{tool} exited with code {exit_code}",
                            retryable=False, tool=tool)
