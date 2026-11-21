"""Spoken status alerts (macOS `say`) for Jarviee. Mirrors alerts.py.

Spoken text is a short status phrase, optionally followed by a reason. The reason
comes only from a row's `activity` note, mapped to a plain phrase when known and
otherwise passed through sanitise_spoken() (no secrets/URLs/tokens, capped length).
Transcript and run output are never spoken.
"""
try:
    import fcntl
except ImportError:
    fcntl = None
import queue
import select
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import alerts

DEFAULT_EVENTS = ["waiting", "failed", "done"]
DEFAULTS = {
    "enabled": False,
    "voice": "Samantha",
    "rate": 185,
    "events": DEFAULT_EVENTS,
    "muted": False,
    "debounce_s": 3,
    "engine": "piper",           # "piper" = local JARVIS voice, "kokoro" = local neural voice (both in ~/.jarviee/tts); either falls back to say if unavailable
    "piper_model": str(Path.home() / ".jarviee" / "tts" / "jarvis" / "vr-high.onnx"),
    "piper_noise": None,   # None = the model's own setting; lower = smoother
    "piper_noise_w": None,
    "piper_speed": 1.25,   # >1 is faster: the model drawls at its natural pace
    "tldr_llm": True,    # True = a quick Claude Haiku call writes the summary (counts against your Claude plan); False = free, uses the end of the run's own output
    "tldr": True,        # summarise what the run did (a short local LLM call over the run's output) and speak that instead of the prompt
    "kokoro_voice": "bm_george",
    "kokoro_speed": 1.0,
    "duck_enabled": True,   # pause music / video (or lower the volume) while a report plays (hub/duck.py)
    "duck_mode": "pause",   # pause | volume | both
    "duck_volume": 20,      # how loud other audio plays while a report speaks (% of full)
    "report_volume": 70,    # the system volume while a report speaks (0-100)
    "defer_cooldown": True,   # a done / failed phrase that lands inside the per-agent cooldown is spoken when it ends, not dropped
    "live_reports": True,   # False = nothing is read out as jobs finish; only /endwork speaks the day (hub/daybook.py)
    "briefing": True,   # speak a short report (what it was, how long, what changed, why it failed or needs you), not just a status phrase
}

_VERBS = {
    "done": "is done",
    "waiting": "needs your reply",
    "failed": "failed",
}
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_VOICE_RE = re.compile(r"^[A-Za-z0-9 ()._-]{1,60}$")
_PRINTED_ERRORS = set()
ACTIVE = None   # the running server's Speaker, so /api/ask can speak an answer

REASON_MAX = 150
UNKNOWN_REASON = "it stopped unexpectedly"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")
_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+")
_SECRET_RES = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b[\w-]*(?:api[_-]?key|key|token|secret|passw(?:or)?d|pwd|authorization|credential)s?\s*[=:]\s*\S+"),
)
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
_WS_RE = re.compile(r"\s+")

# (pattern, plain phrase), checked in order against the raw activity note.
_REASON_MAP = (
    (re.compile(r"(?i)refused (?:a |the )?shell command|shell command.*(?:refused|denied|blocked)"),
     "it was blocked from running a shell command"),
    (re.compile(r"(?i)quota|rate[\s-]?limit|resource_exhausted|usage limit|too many requests|\b429\b"),
     "it hit a usage limit"),
    (re.compile(r"(?i)^\W*(?:\w+\s+)?(?:exited|exit(?:ed)?)\s+(?:with\s+)?(?:code|status)\s*:?\s*-?\d+\W*$"),
     "it exited with an error"),
)


def get_voice_cfg(cfg):
    user = (cfg or {}).get("voice") or {}
    out = dict(DEFAULTS)
    if isinstance(user, dict):
        out.update(user)
    out["events"] = list(out.get("events") or DEFAULT_EVENTS)
    return out


def _clean(value, limit=24):
    return _SAFE_RE.sub("", str(value or ""))[:limit]


def sanitise_spoken(text, limit=REASON_MAX):
    """Make arbitrary note text safe to speak: no control chars, URLs, secrets or long
    token-like strings; whitespace collapsed; capped at `limit` chars on a word boundary."""
    t = _CTRL_RE.sub(" ", _ANSI_RE.sub(" ", str(text or "")))
    t = _URL_RE.sub(" ", t)
    for rx in _SECRET_RES:
        t = rx.sub(" ", t)
    t = _TOKEN_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        if t[limit] != " ":
            head, sep, _ = cut.rpartition(" ")
            cut = head if sep else cut
        t = cut
    return t.strip(" ,;:-")


def build_reason(row, status=None):
    """Spoken reason for a waiting/failed agent row, read from row['activity'].

    Known raw notes map to plain phrases; otherwise a waiting row speaks its sanitised,
    capped note and a failed row with no recognisable note says 'it stopped unexpectedly'.
    Returns '' for other statuses or a waiting row with nothing to say. Pure.
    """
    status = status or (row or {}).get("status")
    if status not in ("waiting", "failed"):
        return ""
    raw = str((row or {}).get("activity") or "")
    for rx, phrase in _REASON_MAP:
        if rx.search(raw):
            return phrase
    cleaned = sanitise_spoken(raw)
    if status == "waiting":
        return cleaned
    return UNKNOWN_REASON


def build_phrase(row, status=None):
    """'Agent 1, running R2-9, is done'. Built only from agent number, task id and status."""
    status = status or row.get("status")
    verb = _VERBS.get(status)
    if verb is None:
        return None
    num = _clean(row.get("agent_no") or row.get("slot") or row.get("number"), 6)
    who = f"Agent {num}" if num else "Agent"
    task = _clean(row.get("task_id") or row.get("task"))
    mid = f", running {task}," if task else ""
    return f"{who}{mid} {verb}" if mid else f"{who} {verb}"


def _span(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    m = s // 60
    return f"{m} minute{'s' if m != 1 else ''}" if m < 60 else f"{m // 60} hour{'s' if m // 60 != 1 else ''} {m % 60} minutes"


def _files_phrase(files, names=False):
    """'It changed 3 files'. File names only for written reports (`names=True`); speech never reads paths."""
    seen = []
    for f in files or []:
        n = _clean(str(f).replace("\\", "/").rsplit("/", 1)[-1], 40)
        if n and n not in seen:
            seen.append(n)
    if not seen:
        return ""
    total = len(seen)
    head = f"It changed {total} file{'s' if total != 1 else ''}"
    if not names:
        return head
    more = f", and {total - 3} more" if total > 3 else ""
    return head + ": " + ", ".join(seen[:3]) + more


# paths and file names in summaries are noise when spoken: "updated hub/voice.py" -> "updated it"
_PATH_RE = re.compile(r"`?(?:~|\.{1,2})?(?:/?[\w.@~-]+/)+(?:[\w.@-]*[\w@-])?`?")
_FILE_RE = re.compile(r"`?\b[\w-]+(?:\.[\w-]+)*\.(?:py|js|mjs|ts|tsx|jsx|css|scss|html|md|json|jsonl|swift|sh|zsh|yml|yaml|"
                      r"toml|txt|rs|go|java|kt|rb|php|c|h|cpp|sql|plist|lock|cfg|ini|env)\b`?", re.I)


def speakable(text, limit=420):
    """Text fit to read aloud: sanitised, with file paths and file names replaced by plain words."""
    t = re.sub(r"`([^`]*)`", r"\1", str(text or ""))
    t = _FILE_RE.sub(" \u00a7 ", _PATH_RE.sub(" \u00a7 ", t))
    t = re.sub(r"\u00a7(?:\s*(?:,|and|&)\s*\u00a7)+", "\u00a7", t)                         # "a.py, b.py and c.py" -> one mention
    t = re.sub(r"\b(?:in|inside|under|to|from|at|on|of|into)\s+(?:the\s+)?\u00a7", " ", t, flags=re.I)   # "fixed it in hub/" -> "fixed it"
    t = re.sub(r"\u00a7", "the code", t)
    t = re.sub(r"\(\s*\)|\[\s*\]", " ", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,;:])(?=\s*[,.;:!?])", "", t)
    return sanitise_spoken(t, limit)


def build_tldr_briefing(row, status, tldr):
    """'Sir, <tldr>' style spoken report: the summary of what the run did, plus status/duration/files. Pure."""
    tldr = speakable(tldr, 420)
    if not tldr:
        return None
    started, ended = (row or {}).get("started"), (row or {}).get("ended")
    took = f" It took {_span(ended - started)}." if status in ("done", "failed") and started and ended else ""
    lead = {"done": "Run complete.", "failed": "A run has failed.", "waiting": "A run needs you."}.get(status)
    if lead is None:
        return None
    parts = [lead, tldr.rstrip(".") + ".", took.strip()]
    fp = _files_phrase((row or {}).get("files")) if status != "waiting" else ""
    if fp:
        parts.append(fp + ".")
    return " ".join(x for x in parts if x)


TLDR_PROMPT = ("You are JARVIS, giving the owner a spoken TLDR of a coding agent's job. Below is the job's status and the "
               "tail of what the agent said and did. Reply with 2 to 3 short plain sentences. The first sentence states the outcome "
               "in the past tense: what was built, fixed, found or decided (for example 'I rebuilt the volume ducking so your voice "
               "stays at 80 percent.'). Then the one result that matters (tests passing, what is left, or what the agent needs from "
               "the owner). Never repeat, quote or paraphrase the owner's request. Never name files, folders, paths, commands or "
               "code. No markdown, no lists.\n\nSTATUS: {status}\n\nAGENT OUTPUT TAIL:\n{tail}\n")
_TLDR_CACHE = {}


def _fallback_tldr(tail):
    """No LLM available: the last meaningful paragraph of the output, made speakable."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", tail or "") if len(p.strip()) > 30 and not p.lstrip().startswith(("|", "```", "-", "*"))]
    return speakable(paras[-1], 300) if paras else ""


def session_tail(row, limit=6000):
    """What a Claude Code session (hook row, no Jarviee log) said in its current job: the assistant text after the
    owner's last prompt, read from the tail of the session transcript. '' when unknown."""
    if (row or {}).get("tool") != "claude" or (row or {}).get("source") != "hook":
        return ""
    try:
        import json
        from . import runcost
        files = runcost.claude_files(str(row.get("id", "")).split("-", 1)[-1])
        if not files:
            return ""
        with open(files[-1], "rb") as f:
            f.seek(max(0, files[-1].stat().st_size - 1_500_000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ImportError, ValueError):
        return ""
    texts = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if e.get("type") == "user":
            # a real prompt from the owner (not a tool result) starts a new job
            if isinstance(content, str) or (isinstance(content, list) and any(
                    isinstance(c, dict) and c.get("type") == "text" for c in content)):
                texts = []
        elif e.get("type") == "assistant" and isinstance(content, list):
            texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n\n".join(t.strip() for t in texts if t and t.strip())[-limit:]


def final_answer(row):
    """The closing answer of a run's last turn, read from the agent's own transcript (Claude by session id, agy by task
    and start time). Survives a closed window: the transcript is on disk. '' when unknown."""
    row = row or {}
    try:
        from . import finaltext
        if row.get("tool") in ("claude", "omniroute") and row.get("session_id"):
            return finaltext.claude_text(row["session_id"])
        if row.get("tool") == "agy" and row.get("task"):
            return finaltext.agy_text(row.get("task"), row.get("started"))
    except Exception:
        pass
    return ""


def summarise_run(row, status, runner=None, timeout=60, cfg=None):
    """TLDR of a finished run from the tail of its log, via a quiet `claude -p` haiku call (no tools, no user hooks, so it
    never shows up as a dashboard session). Falls back to the last paragraph of the output. Cached per job + status,
    and read only from the job's own log offset: a terminal given a new job never reports on the previous one."""
    key = (row.get("id"), row.get("job_id") or "", status)
    if key in _TLDR_CACHE:
        return _TLDR_CACHE[key]
    tail, raw = "", ""
    try:
        from . import finaltext, launcher
        with open(row["log_path"], "rb") as f:
            f.seek(max(0, int(row.get("job_log_offset") or 0)))
            raw = launcher.strip_tty(f.read().decode("utf-8", errors="replace"))
        if finaltext.MARK in raw:   # the run's clean final answer, copied to its log when it ended
            tail = finaltext.after_mark(raw)[-6000:].strip()
    except (OSError, KeyError, TypeError, ImportError):
        pass
    if not tail:
        # A live pane's log is only the TUI screen (launch banner, redraws): the agent's own transcript has the answer.
        tail = final_answer(row)[-6000:].strip()
    if not tail:
        tail = raw[-6000:].strip()
    if not tail:
        tail = session_tail(row)   # a session started in Claude Code: no Jarviee log, read its own transcript
    if not tail:
        return ""
    shown = {"replied": "finished its turn and is waiting for the owner's next message", "waiting": "needs the owner",
             "done": "done", "failed": "failed",
             "stopped": "closed by the owner (its work so far still counts)"}.get(status, status)
    prompt = TLDR_PROMPT.format(status=shown, tail=tail)
    out = ""
    try:
        if runner is None and not get_voice_cfg(cfg or {}).get("tldr_llm", True):
            raise LookupError   # free mode: no model call, use the run's own last paragraph
        run = runner or (lambda argv, inp: subprocess.run(argv, input=inp, capture_output=True, text=True, timeout=timeout,
                                                          cwd=str(Path.home() / ".jarviee")))
        r = run(["claude", "-p", "--model", "haiku", "--tools", "", "--setting-sources", "local"], prompt)
        out = (r.stdout or "").strip() if getattr(r, "returncode", 1) == 0 else ""
    except LookupError:
        pass
    except Exception as e:
        print(f"[voice] tldr error: {e}", file=sys.stderr)
    out = speakable(out, 420) or _fallback_tldr(tail)
    if out:
        _TLDR_CACHE[key] = out
        if len(_TLDR_CACHE) > 200:
            _TLDR_CACHE.pop(next(iter(_TLDR_CACHE)))
    return out


def build_briefing(row, status=None):
    """A spoken report built only from row metadata: title, status, duration, steps, changed file names, and the
    sanitised/mapped reason for a failed or waiting row. Run output and transcripts are never read. Pure."""
    status = status or (row or {}).get("status")
    if status not in _VERBS:
        return None
    title = sanitise_spoken((row or {}).get("title") or (row or {}).get("task"), 80)
    if not title:
        return None   # nothing to report on: fall back to the short status phrase
    started, ended = (row or {}).get("started"), (row or {}).get("ended")
    took = f" in {_span(ended - started)}" if status == "done" and started and ended else \
        f" after {_span(ended - started)}" if status == "failed" and started and ended else ""
    head = {"done": f"{title}. It's done{took}.",
            "failed": f"{title}. It failed{took}.",
            "waiting": f"{title}. It needs you."}[status]
    parts = [head]
    steps = int((row or {}).get("steps") or 0)
    if status == "done" and steps:
        parts.append(f"It took {steps} step{'s' if steps != 1 else ''}.")
    fp = _files_phrase((row or {}).get("files")) if status != "waiting" else ""
    if fp:
        parts.append(fp + ".")
    elif status == "done":
        parts.append("It didn't change any files.")
    reason = build_reason(row, status)
    if reason:
        parts.append(("The reason: " if status == "failed" else "") + reason[0].upper() + reason[1:] + ".")
    return " ".join(parts)


def default_runner(argv):
    """Blocking, so the queue worker serialises speech."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception as e:
        key = str(argv[0]) if argv else "say"
        if key not in _PRINTED_ERRORS:
            _PRINTED_ERRORS.add(key)
            print(f"[voice] subprocess error {key}: {e}", file=sys.stderr)


def _speech_lock():
    """An exclusive flock on ~/.jarviee/speak.lock, held while speaking. None if it can't be taken (then speech is unlocked)."""
    if fcntl is None:
        return None
    try:
        path = Path.home() / ".jarviee" / "speak.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f
    except OSError:
        return None


class _Kokoro:
    """One long-lived hub/tts_kokoro.py process so the model loads once. speak() returns False when unavailable."""
    PY = Path.home() / ".jarviee" / "tts" / "venv" / "bin" / "python"
    SCRIPT = Path(__file__).with_name("tts_kokoro.py")

    def __init__(self):
        self._p = None
        self._lock = threading.Lock()

    def available(self):
        return sys.platform == "darwin" and self.PY.is_file() and (self.PY.parent.parent / "kokoro-v1.0.onnx").is_file()

    def _wait(self, timeout):
        """Block until the worker prints a line (ready / done). False on timeout or exit."""
        fd = self._p.stdout
        r, _, _ = select.select([fd], [], [], timeout)
        return bool(r) and bool(fd.readline())

    def speak(self, voice, speed, text):
        """Blocks until the phrase has finished playing, so nothing else speaks over it."""
        if not self.available():
            return False
        line = "\t".join((voice, str(speed), " ".join(str(text).split()))) + "\n"
        with self._lock:
            try:
                if self._p is None or self._p.poll() is not None:
                    self._p = subprocess.Popen([str(self.PY), str(self.SCRIPT)], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    if not self._wait(90):   # model load
                        raise RuntimeError("kokoro did not start")
                self._p.stdin.write(line)
                self._p.stdin.flush()
                if not self._wait(180):
                    raise RuntimeError("kokoro stopped answering")
                return True
            except Exception as e:
                if self._p and self._p.poll() is None:
                    self._p.terminate()
                self._p = None
                print(f"[voice] kokoro unavailable: {e}", file=sys.stderr)
                return False

    def stop(self):
        with self._lock:
            if self._p and self._p.poll() is None:
                self._p.terminate()
            self._p = None


class _Piper(_Kokoro):
    """The JARVIS voice: same persistent-worker protocol as _Kokoro, with a Piper model path instead of a voice name."""
    SCRIPT = Path(__file__).with_name("tts_piper.py")

    def available(self):
        return sys.platform == "darwin" and self.PY.is_file()

    def speak(self, model, speed, text):
        if not Path(str(model)).is_file():
            return False
        return super().speak(str(model), speed, text)


class Speaker:
    """Queues phrases and speaks them one at a time via an injectable runner.

    With start_worker=False, call drain() to process the queue synchronously (tests).
    """

    DUCK_GRACE_S = 0.8   # a report queued within this long after the last one keeps the duck: no stutter

    def __init__(self, cfg, runner=None, clock=time.time, start_worker=True, store=None, ducker=None):
        self.store = store
        self.cfg = cfg
        self.runner = runner or default_runner
        # audio ducking (hub/duck.py): only the real speaker touches the system; injected runners (tests) don't
        if ducker is None and self.runner is default_runner:
            from . import duck
            ducker = duck.Ducker(cfg)
        self.ducker = ducker
        self._duck = None   # the state while ducked: held across a run of queued reports
        self.clock = clock
        self._q = queue.Queue()
        self._last_phrase = {}   # phrase -> last accepted time (debounce)
        self._last_key = {}      # agent key -> last accepted time (alerts cooldown)
        self._deferred = {}      # agent key -> (due time, phrase, key, event, row): done / failed held back by the cooldown
        self._lock = threading.Lock()
        self._worker = None
        self._kokoro = _Kokoro()
        self._piper = _Piper()
        if start_worker:
            self._worker = threading.Thread(target=self._loop, daemon=True, name="jarviee-voice")
            self._worker.start()

    def _muted(self, vcfg):
        return bool(vcfg.get("muted")) or alerts.get_muted(self.cfg)

    def say_status(self, row, status=None):
        phrase = (build_briefing(row, status) if get_voice_cfg(self.cfg).get("briefing") else None) or build_phrase(row, status)
        if phrase is None:
            return False
        status = status or row.get("status")
        return self.speak(phrase, key=row.get("id"), event=status, row=row)

    def say_report(self, members):
        """Queue the combined report for a batch of agents (hub/batch.py). It is spoken once, whatever the
        per-status event filter says; with TLDR on, each finished member gets its own summary."""
        from . import batch
        return self.speak(batch.report_speech(members, now=self.clock()), event="report", row=members)

    def speak(self, phrase, key=None, event=None, row=None):
        """Queue a phrase if allowed; returns whether it was queued."""
        vcfg = get_voice_cfg(self.cfg)
        if not vcfg["enabled"] or self._muted(vcfg):
            return False
        if event is not None and event not in ("report", "endwork", "startwork") and event not in vcfg["events"]:
            return False
        now = self.clock()
        # /startwork is asked for out loud, often early: quiet hours don't silence it
        if event != "startwork" and alerts.is_quiet_hours(alerts.get_alerts_cfg(self.cfg).get("quiet_hours"), now):
            return False
        cooldown = float(alerts.get_alerts_cfg(self.cfg).get("cooldown_s", 20))
        if key is not None and row and row.get("job_id"):
            key_cd = f"{key}#{row['job_id']}"   # the cooldown is per job: a new job in the same pane reports at once
        else:
            key_cd = key
        if event in ("done", "failed") and key_cd is not None:
            with self._lock:
                self._deferred.pop(key_cd, None)   # a newer outcome of the same job replaces the one held back
        self._release_due(now)
        debounce = float(vcfg["debounce_s"])
        with self._lock:
            if now - self._last_phrase.get(phrase, -1e18) < debounce:
                return False
            if key_cd is not None and now - self._last_key.get(key_cd, -1e18) < cooldown:
                if event in ("done", "failed") and vcfg.get("defer_cooldown", True):
                    # the outcome of a job is news however soon it follows its last phrase: hold it until the
                    # cooldown ends (a newer outcome of the same job replaces it)
                    self._deferred[key_cd] = (self._last_key[key_cd] + cooldown, phrase, key, event, row)
                    return True
                return False
            self._last_phrase[phrase] = now
            if key_cd is not None:
                self._last_key[key_cd] = now
        self._q.put((phrase, key, event, row))
        return True

    def _release_due(self, now=None):
        """Queue the held-back done / failed phrases whose cooldown has ended (quiet hours still apply)."""
        if not self._deferred:
            return
        now = self.clock() if now is None else now
        with self._lock:
            due = [(k, v) for k, v in self._deferred.items() if v[0] <= now]
            for k, _ in due:
                del self._deferred[k]
        quiet = alerts.get_alerts_cfg(self.cfg).get("quiet_hours")
        debounce = float(get_voice_cfg(self.cfg)["debounce_s"])
        for key_cd, (_, phrase, key, event, row) in due:
            if alerts.is_quiet_hours(quiet, now):
                continue
            with self._lock:
                if now - self._last_phrase.get(phrase, -1e18) < debounce:
                    continue
                self._last_phrase[phrase] = now
                self._last_key[key_cd] = now
            self._q.put((phrase, key, event, row))

    def _argv(self, phrase):
        vcfg = get_voice_cfg(self.cfg)
        argv = ["say"]
        voice = str(vcfg.get("voice") or "")
        if _VOICE_RE.match(voice):
            argv += ["-v", voice]
        try:
            argv += ["-r", str(int(vcfg["rate"]))]
        except (TypeError, ValueError):
            pass
        return argv + ["--", phrase]

    def _speak_one(self, item):
        phrase, key, event, row = item
        vcfg = get_voice_cfg(self.cfg)
        if not vcfg["enabled"] or self._muted(vcfg):   # mute flipped while queued
            return
        if event == "report" and vcfg.get("tldr") and self.runner is default_runner:
            try:   # each finished member's own job summary, in place of its one-line detail
                from . import batch
                sums = {}
                for m in row or []:
                    if m.get("outcome") in ("done", "failed", "needs", "replied"):
                        st = {"needs": "waiting", "replied": "replied"}.get(m["outcome"], m["outcome"])
                        sums[batch.key_of(m)] = summarise_run(m, st, cfg=self.cfg)
                phrase = batch.report_speech(row, sums, now=self.clock()) or phrase
            except Exception as e:
                print(f"[voice] report tldr error: {e}", file=sys.stderr)
        elif row is not None and vcfg.get("tldr") and event in ("done", "failed", "waiting") and self.runner is default_runner:
            try:   # a real summary of what the run did, not the prompt; falls back to the status briefing on any problem
                fresh = row
                if self.store is not None and row.get("id"):
                    fresh = self.store.get(row["id"]) or row
                phrase = build_tldr_briefing(fresh, event, summarise_run(fresh, event, cfg=self.cfg)) or phrase
            except Exception as e:
                print(f"[voice] tldr error: {e}", file=sys.stderr)
        self._ensure_ducked()   # just before the first sound: a slow summary never pauses your music early
        gain = float((self._duck or {}).get("gain") or 1.0)
        lock = _speech_lock()   # one voice at a time even across several Jarviee processes
        try:
            self._speak_locked(phrase, vcfg, gain)
        finally:
            if lock:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    lock.close()
                except OSError:
                    pass

    def _ensure_ducked(self):
        if self.ducker is None or self._duck is not None:
            return
        try:
            self._duck = self.ducker.begin() or {}
        except Exception as e:   # ducking must never stop a report
            print(f"[voice] duck error: {e}", file=sys.stderr)
            self._duck = {}

    def _unduck(self):
        st, self._duck = self._duck, None
        if st and self.ducker is not None:
            try:
                self.ducker.end(st)
            except Exception as e:
                print(f"[voice] unduck error: {e}", file=sys.stderr)

    def _speak_locked(self, phrase, vcfg, gain=1.0):
        vol = f";{gain:g}" if gain and abs(gain - 1.0) > 1e-6 else ""   # afplay -v gain while the volume is ducked
        if vcfg.get("engine") == "piper" and self.runner is default_runner:
            try:
                extra = [vcfg.get("piper_noise"), vcfg.get("piper_noise_w")]
                knobs = ",".join([str(float(vcfg.get("piper_speed") or 1.0))] + ([str(float(x)) for x in extra] if None not in extra else [])) + vol
                if self._piper.speak(str(vcfg.get("piper_model") or ""), knobs, phrase):
                    return
            except (TypeError, ValueError):
                pass
        if vcfg.get("engine") in ("kokoro", "piper") and self.runner is default_runner:   # injected runners (tests) keep using say
            try:
                if self._kokoro.speak(str(vcfg.get("kokoro_voice") or "bm_george"), f"{float(vcfg.get('kokoro_speed') or 1.0)}{vol}", phrase):
                    return
            except (TypeError, ValueError):
                pass
        try:
            self.runner(self._argv(phrase))
        except Exception as e:
            print(f"[voice] speak error: {e}", file=sys.stderr)

    def _one(self, item):
        try:
            self._speak_one(item)
        except Exception as e:   # one bad item must not kill the worker (or leave audio ducked)
            print(f"[voice] speak error: {e}", file=sys.stderr)

    def drain(self):
        """Speak everything queued now (tests): ducked once for the lot, restored after the last."""
        self._release_due()
        try:
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    return
                if item is None:
                    return
                self._one(item)
        finally:
            self._unduck()

    def _loop(self):
        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                self._release_due()   # held-back done / failed phrases come due while nothing else is queued
                continue
            if item is None:
                return
            stop = False
            try:   # duck once for a run of queued reports; restore after the last, or on any failure
                while True:
                    self._one(item)
                    try:
                        item = self._q.get(timeout=self.DUCK_GRACE_S)
                    except queue.Empty:
                        break
                    if item is None:
                        stop = True
                        break
            finally:
                self._unduck()
            if stop:
                return

    def stop(self):
        self._kokoro.stop()
        self._piper.stop()
        if self._worker:
            self._q.put(None)
