#!/usr/bin/env python3
"""Runs one Jarviee run inside its iTerm2 pane.   Usage: pane_runner.py <run_id>  |  --idle  |  --slot

1. GET /api/run-spec?id=<run_id> once: argv, cwd, env, log file, title, close rule. The prompt
   never passes through AppleScript.
2. Runs the agent on its own pty (PtyProc, resized with the pane): a real TTY here, and the same output in the
   dashboard log (-a keeps the launcher's header line, -F flushes every write).
3. POSTs start (this pid), exit (code) and close to /api/run-event. The dashboard's Stop sends SIGTERM
   to this process, which stops the agent's process group.
4. Prints ✓ done / ✗ failed / ■ stopped · duration, then follows the close rule.
Grid layout (JARVIEE_SLOT=1): instead of closing, the pane turns back into a free pane (`--slot`): a waiter that reads
"@jarviee:<run_id>" (typed in by the hub) and becomes that run's runner.
Stdlib only.
"""
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.request
from pathlib import Path

RUN_RE = re.compile(r"^[a-z0-9-]{1,64}$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# hub/runstate.py BG_WAIT / FINISHED_UNCHECKED (this script is stdlib only and imports nothing from the hub)
BG_WAIT = "waiting for its background task"
FINISHED_UNCHECKED = "finished (background tasks not checked)"
# agy's footer while a background task runs: "1 task(s) · /tasks" (escapes taken out first, cursor moves become spaces)
AGY_TASKS_RE = re.compile(rb"\d+\s*task\(s\)")
ANSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
AGY_FOOTER_WINDOW = 15.0   # the footer must have been drawn this close to (or after) the turn's end to count
ROOT = Path(__file__).resolve().parent.parent
state = {"stopped": 0.0, "done": False}
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open   # never proxy 127.0.0.1


def env(key, default=None):
    """JARVIEE_<key>, else the old AGENT_HUB_<key> (panes opened before the rename)."""
    return os.environ.get("JARVIEE_" + key, os.environ.get("AGENT_HUB_" + key, default))


def port():
    if env("PORT"):
        return int(env("PORT"))
    try:
        return int(json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("port", 7777))
    except (OSError, ValueError):
        return 7777


def load_token():
    tok = env("TOKEN")
    if tok:
        return tok.strip()
    try:
        home = os.path.expanduser("~/.jarviee")
        if not os.path.exists(home) and os.path.exists(os.path.expanduser("~/.agent-hub")):
            home = os.path.expanduser("~/.agent-hub")
        p = os.path.expanduser(env("TOKEN_PATH", os.path.join(home, "token")))
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def call(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    tok = load_token()
    if tok:
        headers["X-Jarviee-Token"] = tok
    req = urllib.request.Request(f"http://127.0.0.1:{port()}{path}", data=data, headers=headers)
    with _open(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def report(rid, **body):
    try:
        call("/api/run-event", {"id": rid, **body})
    except (OSError, ValueError):
        pass   # the hub may be gone; the pane still finishes cleanly


def say(text=""):
    try:
        print(text, flush=True)
    except OSError:
        pass


def set_title(text):
    """Pane title via OSC 1 (iTerm2 shows it as the session name). Control characters are stripped."""
    try:
        sys.stdout.write(f"\033]1;{CTRL_RE.sub(' ', str(text))[:120]}\007")
        sys.stdout.flush()
    except OSError:
        pass


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from hub.jobstatus import (
    HEADER,
    STATUS_TO_HEADER,
    TAB_COLORS,
    header_kind_of,
    header_text_of,
    job_status,
    tab_color_of,
)

# The pane header: iTerm2's badge (status + job title, top right of the pane), the pane title bar and the tab colour.
# No row of the agent's screen is taken, so a full-screen agent can never paint over it.
header = {"job": None, "shown": None, "title": "", "polled": 0.0}


def header_text(kind, title):
    return header_text_of(kind, title)


def set_header(kind, title):
    """Show `kind` (running / waiting / done / failed / stopped) for the job `title`. Best effort, never raises."""
    import base64
    text = header_text(kind, title)
    if header["shown"] == text:
        return
    header["shown"] = text
    set_title(text)
    set_tab_color(kind if kind in TAB_COLORS else None)
    # No iTerm2 badge: the owner doesn't want the big status/title watermark over the pane. The title bar and the
    # tab colour carry the status. Clear any badge an earlier runner left.
    clear_badge()


def clear_header():
    header["shown"] = None
    clear_badge()


def clear_badge():
    try:
        sys.stdout.write("\033]1337;SetBadgeFormat=\007")
        sys.stdout.flush()
    except OSError:
        pass


def header_from_status(st, local=None):
    """The header for a /api/run-status answer {job_id, status, title}. The status always comes with its own job id,
    so a previous job's result is never shown: a new job id resets the header to running. A finish the pane saw itself
    (`local`) wins over a server status that hasn't caught up yet. Returns the kind shown, or None."""
    job = st.get("job_id") or ""
    title = st.get("title") or header["title"]
    if header["job"] is not None and job != header["job"]:
        local = None   # a new job started in this pane: forget the old one's finish
    header["job"], header["title"] = job, title
    kind = local or header_kind_of(st)
    set_header(kind, title)
    return kind


def poll_header(rid, local=None, every=2.0):
    """Refresh the header from the hub at most every `every` s. Returns the /api/run-status answer when it
    polled, else None. Never raises."""
    now = time.time()
    if now - header["polled"] < every:
        return None
    header["polled"] = now
    try:
        st = call(f"/api/run-status?id={rid}", timeout=2)
        header_from_status(st, local)
        return st
    except (OSError, ValueError):
        if local:   # the hub is gone, but this pane saw its own job finish
            set_header(local, header["title"])
    return None


def set_tab_color(run_state):
    """iTerm2 tab colour per run state (proprietary OSC 6); None resets to the default. Best effort, never raises."""
    try:
        if run_state is None:
            seq = "\033]6;1;bg;*;default\007"
        else:
            r, g, b = TAB_COLORS[run_state]
            seq = "".join(f"\033]6;1;bg;{n};brightness;{v}\007" for n, v in (("red", r), ("green", g), ("blue", b)))
        sys.stdout.write(seq)
        sys.stdout.flush()
    except Exception:
        pass


SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
QUIET_AFTER = 2.0   # seconds of agent silence before the pane shows its own status line


def status_line(elapsed, st, tick):
    """'⠋ working · 2m 10s · <activity> · 9 steps' from a /api/run-status answer (may be empty)."""
    act = CTRL_RE.sub(" ", str((st or {}).get("activity") or "")).strip()
    parts = [f"{SPIN[tick % len(SPIN)]} working", fmt(elapsed)]
    if act and act.lower() not in ("working", "starting", "opening iterm2 pane"):
        parts.append(act[:80])
    steps = int((st or {}).get("steps") or 0)
    if steps:
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    return " · ".join(parts)


def fmt(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


class PtyProc:
    """The agent on its own pseudo-terminal, sized like this pane and resized whenever the pane is.
    (`script` copies the size once at start and never follows a resize, so after a split the agent kept
    drawing for the old width and the pane filled with overlapping, duplicated text.)
    The output is copied to the pane and appended to the run log; stdin is forwarded for live sessions."""
    direct = True   # the agent is the session leader: signal its group, not its children

    def __init__(self, argv, cwd, env, log, forward_stdin):
        self.forward = forward_stdin and os.isatty(0)
        self.log = open(log, "ab", buffering=0) if log else None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:   # the agent
            try:
                if cwd:
                    os.chdir(cwd)
                # The server (often a launchd service) has no terminal env; without TERM the agent drops colors.
                full = dict(env if env is not None else os.environ)
                if full.get("TERM", "dumb") in ("", "dumb"):
                    full["TERM"] = os.environ.get("TERM") if os.environ.get("TERM", "dumb") != "dumb" else "xterm-256color"
                full.setdefault("COLORTERM", os.environ.get("COLORTERM", "truecolor"))
                for k in ("NO_COLOR", "CI"):
                    full.pop(k, None)
                os.execvpe(argv[0], argv, full)
            except OSError as e:
                os.write(2, f"couldn't start {argv[0]}: {e}\n".encode())
            os._exit(127)
        self.code = None
        self.last_input = 0.0   # when the owner last typed into this live session
        self.last_out = 0.0     # when the agent last printed anything
        self.tasks_at = 0.0     # when agy last drew its "N task(s)" footer (a background task running)
        self.tail = b""
        self.at_bol = True      # the agent's last output ended a line
        self.status_shown = False
        self.saved = None
        self.winch = False
        self._resize()
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "winch", True))
        if self.forward:
            self.saved = termios.tcgetattr(0)
            tty.setraw(0)

    def _resize(self):
        self.winch = False
        for fd in (1, 0, 2):
            try:
                size = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)
                return
            except OSError:
                continue

    def wait(self, timeout):
        """Pump input and output for up to `timeout` s. Returns the exit code once the agent is gone."""
        end = time.time() + timeout
        while self.code is None:
            if self.winch:
                self._resize()
            left = end - time.time()
            if left <= 0:
                raise subprocess.TimeoutExpired("agent", timeout)
            watch = [self.fd] + ([0] if self.forward else [])
            try:
                ready = select.select(watch, [], [], min(left, 0.2))[0]
            except (OSError, ValueError, InterruptedError):
                ready = []
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if fd == 0:
                    if data:
                        self.last_input = time.time()
                        try:
                            os.write(self.fd, data)
                        except OSError:
                            pass
                    else:
                        self.forward = False
                elif data:
                    self._out(data)
                else:   # EOF on the pty: the agent closed it
                    self._reap(block=True)
            if self.code is None:
                self._reap(block=False)
        self._drain()
        self.close()
        return self.code

    def show_status(self, text):
        """One dim, self-overwriting line while the agent is silent (pane only, never the run log)."""
        try:
            os.write(1, (b"" if self.at_bol else b"\r\n") + b"\r\033[2K\033[2m" + text.encode("utf-8", "replace")
                     + b"\033[0m")
        except OSError:
            return
        self.at_bol, self.status_shown = True, True

    def _out(self, data):
        if self.status_shown:   # the agent speaks again: wipe the status line first
            self.status_shown = False
            try:
                os.write(1, b"\r\033[2K")
            except OSError:
                pass
        self.last_out = time.time()
        self.at_bol = data.endswith((b"\n", b"\r"))
        try:
            chunk = self.tail[-64:] + data   # a footer split across two reads still counts
            self.tail = chunk[-64:]
            if b"task(s)" in chunk and AGY_TASKS_RE.search(ANSI_RE.sub(b" ", chunk)):
                self.tasks_at = self.last_out
        except Exception:
            pass
        try:
            os.write(1, data)
        except OSError:
            pass
        if self.log:
            try:
                self.log.write(data)
            except OSError:
                pass

    def _drain(self):
        while True:
            try:
                if not select.select([self.fd], [], [], 0)[0]:
                    return
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self._out(data)

    def _reap(self, block):
        try:
            pid, status = os.waitpid(self.pid, 0 if block else os.WNOHANG)
        except ChildProcessError:
            self.code = 0
            return
        if pid:
            self.code = os.waitstatus_to_exitcode(status)

    def close(self):
        if self.saved is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, self.saved)
            except termios.error:
                pass
            self.saved = None
        if self.log:
            self.log.close()
            self.log = None
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)


def signal_agent(proc, sig):
    """Signal the agent: a PtyProc agent is a session leader, so its whole group."""
    if getattr(proc, "direct", False):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            try:
                os.kill(proc.pid, sig)
            except OSError:
                pass
        return
    try:
        kids = [int(x) for x in subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True,
                                               text=True, timeout=2).stdout.split()]
    except (OSError, subprocess.SubprocessError, ValueError):
        kids = []
    for pid in kids or [proc.pid]:
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass


def wait_enter(seconds):
    """True if Enter was pressed in time (None = no limit). False on timeout or without a terminal."""
    try:
        if seconds is not None and not select.select([sys.stdin], [], [], seconds)[0]:
            return False
        return bool(sys.stdin.readline())
    except (OSError, ValueError):
        return False


def close_now(rid):
    """Ask the hub to close this pane, then wait for it (SIGHUP ends us). Exiting first would let iTerm2
    close the pane on its own, and with the last pane the whole full screen window and its Space."""
    try:
        verdict = call("/api/run-event", {"id": rid, "event": "close"}, timeout=20)
    except (OSError, ValueError):
        verdict = {}   # the hub is gone: close as before
    if verdict.get("exit"):   # the last pane of a full screen window: exiting lets iTerm2 close it, the window and its Space
        set_tab_color(None)
        return
    if verdict.get("closed") is False:   # needs you / failed for good: leave it open, say why
        set_tab_color("waiting" if verdict.get("reason") == "needs you" else "failed")
        say(f"\033[33mNot closing: {verdict.get('reason') or 'needs you'}. Press Enter to close it yourself.\033[0m")
        if wait_enter(None):
            set_tab_color(None)
            return forced_close(rid)
        return
    set_tab_color(None)
    end = time.time() + 15
    while time.time() < end:   # the hub is gone if nothing happens: then iTerm2 closes the pane
        try:                   # a closed pane reads as end-of-file (iTerm2 doesn't always send SIGHUP)
            if select.select([sys.stdin], [], [], 0.5)[0] and not sys.stdin.readline():
                return
        except (OSError, ValueError):
            return


def forced_close(rid):
    """The owner pressed Enter on a pane the hub kept open: close it now."""
    report(rid, event="close", force=True)
    end = time.time() + 15
    while time.time() < end:
        try:
            if select.select([sys.stdin], [], [], 0.5)[0] and not sys.stdin.readline():
                return
        except (OSError, ValueError):
            return


def close_rule(rid, spec):
    rule, delay = spec.get("close", "after_30s"), spec.get("close_delay")
    if rule == "immediately":
        return close_now(rid)
    if rule == "after_30s":
        secs = float(30 if delay is None else delay)
        say(f"Closing in {secs:g} s. Press Enter to keep this pane open.")
        if not wait_enter(secs):
            return close_now(rid)
        say("Kept open.")
    say("Press Enter to close this pane.")
    if wait_enter(None):
        close_now(rid)


def idle():
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))   # a clean exit lets iTerm2 close the pane
    set_title("Jarviee · idle")
    say("\033[2mJarviee · idle. New runs open here.\033[0m")
    signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
    while True:   # leave when the pane is gone (closed pane: tty lost or reparented), never linger as an orphan
        time.sleep(5)
        try:
            if os.getppid() == 1 or not os.isatty(0):
                os._exit(0)
        except OSError:
            os._exit(0)


SLOT_RE = re.compile(r"@jarviee:([a-z0-9-]{1,64})\s*$")


def slot():
    """A free grid pane: wait for the hub to type in a run id, then become that run's runner (same pane, same tty).
    Keys typed here are ignored (no echo, Ctrl-C / Ctrl-D / Ctrl-Z do nothing); closing the pane ends it."""
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(sig, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
    set_tab_color(None)
    clear_header()
    set_title("Jarviee · free")
    saved = None
    try:
        saved = termios.tcgetattr(0)
        quiet = termios.tcgetattr(0)
        quiet[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)   # read raw bytes: Ctrl-D is just a byte
        quiet[6][termios.VMIN], quiet[6][termios.VTIME] = 1, 0
        termios.tcsetattr(0, termios.TCSANOW, quiet)
    except (OSError, termios.error):
        pass
    freed = env("FREED_RUN")
    if freed and RUN_RE.match(freed):
        report(freed, event="free")   # listening now: the hub may hand this pane the next run
    say("\n\033[2m── Jarviee · free pane · the next task opens here ──\033[0m")
    buf = b""
    while True:
        try:
            chunk = os.read(0, 1024)
        except OSError:
            os._exit(0)
        if not chunk:   # the pane is gone
            os._exit(0)
        buf = (buf + chunk)[-512:]
        for line in re.split(rb"[\r\n]", buf)[:-1]:
            m = SLOT_RE.search(line.decode("utf-8", "replace"))
            if m:
                if saved is not None:
                    try:
                        termios.tcsetattr(0, termios.TCSANOW, saved)
                    except (OSError, termios.error):
                        pass
                for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(sig, signal.SIG_DFL)
                os.write(1, b"\033[2J\033[3J\033[H")   # each task starts on a clean pane
                os.environ.pop("JARVIEE_FREED_RUN", None)
                os.environ["JARVIEE_SLOT"] = "1"
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), m.group(1)])
        buf = re.split(rb"[\r\n]", buf)[-1]


def banner(spec, rid):
    """The one-shot run's first lines: its title and folder, with control characters (terminal escapes) stripped."""
    return (f"\033[1m▶ {CTRL_RE.sub(' ', str(spec.get('title') or rid))}\033[0m\n"
            f"\033[2m{CTRL_RE.sub(' ', str(spec.get('cwd') or ''))}\033[0m\n")


def background_wait(last, st, tasks_at, turn_end_at, agy):
    """Is an unattended run's ended turn waiting on a background task of its own? The hub says so (status running,
    activity BG_WAIT: it read the agent's transcript), or, for agy when the hub couldn't find its transcript
    (FINISHED_UNCHECKED), agy drew its "N task(s)" footer around the end of the turn."""
    act = str((st or {}).get("activity") or "")
    if last == "running" and act.startswith(BG_WAIT):
        return True
    return bool(agy and last == "done" and act == FINISHED_UNCHECKED and tasks_at and turn_end_at
                and tasks_at >= turn_end_at - AGY_FOOTER_WINDOW)


def back_to_slot(rid):
    """Grid layout: the run is over; this pane waits for the next one (the hub is told once the waiter listens)."""
    os.environ["JARVIEE_FREED_RUN"] = rid if RUN_RE.match(rid or "") else ""
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "--slot"])


def main(argv):
    if argv[1:] == ["--idle"]:
        return idle()
    if argv[1:] == ["--slot"]:
        return slot()
    if len(argv) != 2 or not RUN_RE.match(argv[1]):
        say("usage: pane_runner.py <run_id>   (lowercase letters, digits and dashes)")
        return 2
    rid = argv[1]
    try:
        spec = call(f"/api/run-spec?id={rid}")
    except urllib.error.HTTPError as e:
        say(f"Jarviee: run {rid} isn't waiting for a pane (HTTP {e.code}); it may have moved to the background.")
        return 1
    except (OSError, ValueError) as e:
        say(f"Jarviee: can't reach the hub on port {port()}: {e}")
        return 1
    header["title"] = spec.get("title") or rid
    set_header("running", header["title"])   # neutral until the job's real status comes in
    live = bool(spec.get("interactive"))   # the real claude / agy TUI: it gets this terminal and runs until it ends
    unattended = live and bool(spec.get("unattended"))   # a queue / pipeline run: one turn, then the pane closes
    if not live:   # a live session opens on the CLI's own start screen (logo, colours), exactly as in a shell;
        # the job title is in the pane title bar
        say(banner(spec, rid))

    def on_signal(sig, _frame):
        if state["done"]:
            set_tab_color(None)
            clear_header()
            os._exit(0)            # pane closed (SIGHUP) or stopped after the agent finished
        if not state["stopped"]:
            state["stopped"] = time.time()
            signal_agent(proc, signal.SIGTERM)

    start = time.time()
    timeout = float("inf") if live else float(spec.get("timeout") or 600.0)
    timed_out = False
    # A live session whose turn the hub judged done (hub/ingest.py: the turn's closing message is not a question or
    # a blocker) is ended after a short grace, so its window closes. Typing in the pane during the grace, or a new
    # prompt, keeps it open. A turn that needs you never closes. With `close: never` (the default since 2026-11-14:
    # the owner wants to follow up on finished tasks) an attended session is never ended or closed; a queue run still
    # ends after its turn so the queue moves on, but its pane stays until the owner closes it.
    grace = float(spec.get("live_close_delay", 10))
    done_at, keep_open, closing_done, last, ended_as = 0.0, False, False, None, "done"
    # An unattended turn that ended to wait for its own background task (a backgrounded test run...) is not the end:
    # the agent answers when the task reports back. One continuous wait is capped (`background_wait` s, from
    # guards.background_wait_minutes), after which the run ends as before; the hub caps the total wait.
    try:
        bg_cap = float(spec.get("background_wait") or 0)
    except (TypeError, ValueError):
        bg_cap = 0.0
    agy = Path(str((spec.get("argv") or [""])[0])).name.startswith("agy")
    bg_since, turn_end_at = 0.0, 0.0
    last_st, tick = {}, 0   # one-shot runs (claude -p) print nothing until they finish: the pane shows progress
    try:
        proc = PtyProc(spec["argv"], spec.get("cwd") or None, spec.get("env") if spec.get("env") is not None else None, spec.get("log"), live)
    except OSError as e:
        say(f"✗ couldn't start: {e}")
        set_header("failed", header["title"])
        report(rid, event="start", pid=os.getpid())
        report(rid, event="exit", code=127)
        return 127
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    report(rid, event="start", pid=os.getpid())
    while True:
        try:
            code = proc.wait(0.5)
            break
        except subprocess.TimeoutExpired:
            polled = poll_header(rid)
            if polled is not None:
                last, last_st = polled.get("status"), polled
            if not live and not state["stopped"] and time.time() - max(start, proc.last_out) >= QUIET_AFTER:
                tick += 1
                proc.show_status(status_line(time.time() - start, last_st, tick))
            if live and not state["stopped"] and grace >= 0 and (unattended or spec.get("close") != "never"):
                now = time.time()
                turn_over, bg_capped = last == "done" or (unattended and last == "failed"), False
                turn_end_at = (turn_end_at or now) if last == "done" else 0.0
                if unattended and bg_cap > 0 and background_wait(last, last_st, proc.tasks_at, turn_end_at, agy):
                    bg_since = bg_since or now
                    if now - bg_since < bg_cap:
                        turn_over = False
                        if polled is not None:
                            set_header("running", f"{header['title']} · waiting for its background task")
                    else:
                        turn_over, bg_capped = True, True
                else:
                    bg_since = 0.0
                if not turn_over:
                    done_at, keep_open = 0.0, False
                elif not keep_open:
                    if not done_at:
                        done_at = now
                    if proc.last_input > done_at:   # the owner is typing: leave the session alone
                        keep_open, done_at = True, 0.0
                        set_header(last, header["title"])
                    elif now - done_at >= grace:
                        closing_done, ended_as = True, ("done" if bg_capped else last)
                        state["stopped"] = now
                        signal_agent(proc, signal.SIGTERM)
                    elif spec.get("close") != "never":
                        set_header(last, f"{header['title']} · closing in {int(grace - (now - done_at)) + 1}s, type to keep it")
            if not state["stopped"] and time.time() - start > timeout:
                timed_out = True
                say(f"\n\033[31m✗ run timed out after {fmt(timeout)} with no output/exit; terminating\033[0m")
                state["stopped"] = time.time()
                signal_agent(proc, signal.SIGTERM)
            if state["stopped"] and time.time() - state["stopped"] > 8:
                signal_agent(proc, signal.SIGKILL)
    state["done"] = True
    if proc.status_shown:
        proc.status_shown = False
        try:
            os.write(1, b"\r\033[2K")
        except OSError:
            pass
    stopped = bool(state["stopped"])
    if timed_out and (code is None or code == 0):
        code = 124
    report(rid, event="exit", code=code, stopped=stopped and not timed_out)
    final = ended_as if closing_done else "stopped" if stopped and not timed_out else "done" if code == 0 and not timed_out else "failed"
    header["polled"] = 0.0
    poll_header(rid, local=final)   # this job's own finish; a live session's job id still matches
    dur = fmt(time.time() - start)
    if timed_out:
        say(f"\n\033[1m✗ timed out · {dur}\033[0m")
    else:
        say("\n\033[1m" + (f"✓ {ended_as} · {dur}" if closing_done and ended_as == "done"
                           else f"✗ {ended_as} · {dur}" if closing_done else f"■ stopped · {dur}" if stopped else f"✓ done · {dur}" if code == 0
                           else f"✗ failed (exit {code}) · {dur}") + "\033[0m")
    try:
        if env("SLOT") == "1":
            pass   # grid layout: the pane stays and takes the next run (back_to_slot, below)
        elif closing_done and spec.get("close") != "never":   # the grace was the wait: close now
            close_now(rid)
        else:
            close_rule(rid, spec)
    finally:
        set_tab_color(None)
        clear_header()
    return 0   # the agent's code is reported above; exiting cleanly lets iTerm2 close the pane if the hub is gone


if __name__ == "__main__":
    code = main(sys.argv)
    if env("SLOT") == "1" and len(sys.argv) == 2 and RUN_RE.match(sys.argv[1]):
        back_to_slot(sys.argv[1])   # never returns
    sys.exit(code)
