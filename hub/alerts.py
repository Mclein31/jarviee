"""Ambient alerts (macOS banner + sound) for Jarviee."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from . import brand

DEFAULT_EVENTS = ["waiting", "failed", "done"]
DEFAULT_SOUNDS = {
    "waiting": "Glass",
    "responded": "Pop",
    "failed": "Basso",
    "done": "Hero",
}

# Each alert lands in one section. "waiting" splits in two: the agent finished its turn and replied
# (your move, nothing blocked) vs. it is stuck on you (a permission, a question, a choice).
SECTIONS = {
    "needs": "Needs your response",
    "responded": "Responded",
    "done": "Done",
    "failed": "Failed",
}
REPLIED_RE = re.compile(r"^(turn finished|claude is waiting for your input)", re.I)


def section_of(status, activity=""):
    """'needs' | 'responded' | 'done' | 'failed' (or the raw status) for one agent row."""
    if status == "waiting":
        return "responded" if REPLIED_RE.match(str(activity or "").strip()) else "needs"
    return status

DEFAULTS = {
    "enabled": True,
    "banner": True,
    "sound": True,
    "events": DEFAULT_EVENTS,
    "sounds": DEFAULT_SOUNDS,
    "cooldown_s": 20,
    "quiet_hours": None,
    "poll_s": 1.5,
    "combined": True,   # one report per batch of agents (hub/batch.py); False: one alert per window
    "report_max_wait_s": 300,   # report finished agents after this long even if another is still working
    "plan_reports": True,   # a pipeline plan (research, build, test, review) is reported once, when it ends, not once per phase
    # Phone (ntfy) pushes for agents. The Mac banner and sound are unchanged; the phone only hears about what matters
    # while the owner is away from the Mac (see phone_rank):
    "phone_away_s": 120,     # push only after this long without keyboard/mouse input on the Mac (0: push even while at it)
    "phone_min_s": 120,      # a finished job is pushed only if it ran at least this long,
    "phone_min_steps": 5,    # or took this many steps, or changed a file. A quick reply ("cd", a question) never is.
}

PHONE_PRIORITY = {"high": 4, "normal": 3}   # ntfy priorities: 4 = high (failed, blocked on you), 3 = default (work done)


def mac_idle_s():
    """Seconds since the last keyboard or mouse input on this Mac (IOKit HIDIdleTime), or None when unknown."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r", "-k", "HIDIdleTime"],
                             capture_output=True, text=True, timeout=3).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out or "")
        return int(m.group(1)) / 1e9 if m else None
    except Exception:
        return None


def phone_rank(member, acfg):
    """How one reported job ranks for the phone: "high" (failed, or blocked on the owner), "normal" (real work
    finished) or None (not worth a push: a quick reply, a session opened and closed, a job closed by the owner)."""
    out = member.get("outcome")
    if out in ("failed", "needs"):
        return "high"
    if out not in ("done", "replied"):
        return None
    if member.get("plan"):
        return "normal"
    try:
        started = float(member.get("started") or 0)
        end = float(member.get("ended") or member.get("updated") or 0)
    except (TypeError, ValueError):
        started = end = 0
    took = end - started if started and end >= started else 0
    if took >= float(acfg.get("phone_min_s", 120)) or int(member.get("steps") or 0) >= int(acfg.get("phone_min_steps", 5)) \
            or member.get("files"):
        return "normal"
    return None


def report_rank(members, acfg):
    """The highest phone rank among a report's members (None: the report stays on the Mac)."""
    ranks = {phone_rank(m, acfg) for m in members}
    return "high" if "high" in ranks else "normal" if "normal" in ranks else None

_PRINTED_ERRORS = set()


def get_alerts_cfg(cfg):
    user = (cfg or {}).get("alerts") or {}
    out = dict(DEFAULTS)
    out.update(user)
    sounds = dict(DEFAULT_SOUNDS)
    if isinstance(user.get("sounds"), dict):
        sounds.update(user["sounds"])
    out["sounds"] = sounds
    return out


def _alerts_state_file(cfg):
    if (cfg or {}).get("alerts_state_path"):
        return Path(cfg["alerts_state_path"])
    return brand.home() / "alerts.json"


def get_muted(cfg) -> bool:
    p = _alerts_state_file(cfg)
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return bool(data.get("muted", False))
    except Exception:
        pass
    return False


def set_muted(cfg, muted: bool) -> bool:
    p = _alerts_state_file(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"muted": bool(muted)}), encoding="utf-8")
    except Exception as e:
        print(f"[alerts] failed to write alerts state: {e}", file=sys.stderr)
    return bool(muted)


def status(cfg):
    acfg = get_alerts_cfg(cfg)
    return {
        "enabled": bool(acfg.get("enabled", True)),
        "muted": get_muted(cfg),
        "banner": bool(acfg.get("banner", True)),
        "sound": bool(acfg.get("sound", True)),
    }


def is_quiet_hours(quiet_hours, now_ts):
    if not quiet_hours or not isinstance(quiet_hours, (list, tuple)) or len(quiet_hours) != 2:
        return False
    try:
        sh, sm = [int(x) for x in quiet_hours[0].split(":", 1)]
        eh, em = [int(x) for x in quiet_hours[1].split(":", 1)]
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        lt = time.localtime(now_ts)
        cur_min = lt.tm_hour * 60 + lt.tm_min
        if start_min <= end_min:
            return start_min <= cur_min < end_min
        else:
            return cur_min >= start_min or cur_min < end_min
    except Exception:
        return False


def default_runner(argv):
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        key = " ".join(str(x) for x in argv[:2])
        if key not in _PRINTED_ERRORS:
            _PRINTED_ERRORS.add(key)
            print(f"[alerts] subprocess error {key}: {e}", file=sys.stderr)


def push_icon(kind):
    from . import push
    return push.icon(kind)


class AlertWatcher(threading.Thread):
    @property
    def speaker(self):
        """The speaker, unless live reports are off (voice.live_reports false): then jobs are not read out as they end."""
        if self._speaker is not None and not (self.cfg.get("voice") or {}).get("live_reports", True):
            return None
        return self._speaker

    @speaker.setter
    def speaker(self, value):
        self._speaker = value

    def __init__(self, cfg, store, runner=None, clock=time.time, speaker=None):
        super().__init__(daemon=True, name="jarviee-alerts")
        self.cfg = cfg
        self.store = store
        self.runner = runner
        self.clock = clock
        self._speaker = speaker   # optional voice.Speaker: spoken results on the same transitions
        self._halt = threading.Event()
        self._last_status = {}
        self._last_alert = {}
        self.phone_sender = None   # tests inject a recorder
        self.idle_reader = mac_idle_s   # seconds since the owner last touched the Mac (tests inject a number)
        self._phone_pending = {}   # job key -> blocked row whose phone push waits for the owner to step away
        self._seeded = False
        from .batch import Batcher, PlanWatch
        self.batch = Batcher()
        self.plans = PlanWatch(self._plan_brief)
        self.seed()

    def seed(self):
        rows = self.store.all()
        for row in rows:
            aid = row.get("id")
            if aid:
                self._last_status[aid] = row.get("status")
        self.batch.seed(rows)
        self.plans.seed(rows)
        self._seeded = True

    def _plan_brief(self, plan_id):
        """{"status", "goal", "pipeline"} of a plan from the queue tables, or None (no such plan / no queue tables)."""
        db = getattr(self.store, "db", None)
        if db is None:
            return None
        from .store import _lock
        try:
            with _lock:
                r = db.execute("SELECT status, goal, pipeline FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not r:
                return None
            return {"status": r["status"], "goal": r["goal"] or "", "pipeline": json.loads(r["pipeline"]) if r["pipeline"] else None}
        except Exception:   # sqlite3.Error, a bad JSON column: the plan is simply not known
            return None

    def start(self):
        if not self._seeded:
            self.seed()
        super().start()
        return self

    def stop(self):
        self._halt.set()

    def run(self):
        while not self._halt.is_set():
            acfg = get_alerts_cfg(self.cfg)
            poll_s = float(acfg.get("poll_s", 1.5))
            try:
                self.poll_once()
            except Exception as e:
                print(f"[alerts] poll error: {e}", file=sys.stderr)
            self._halt.wait(poll_s)

    def _run_cmd(self, argv):
        if self.runner is not None:
            self.runner(argv)
        else:
            default_runner(argv)

    def poll_once(self):
        acfg = get_alerts_cfg(self.cfg)
        alerts_on = bool(acfg.get("enabled", True))
        if not alerts_on and self.speaker is None:
            return

        events = set(acfg.get("events") or DEFAULT_EVENTS)
        cooldown_s = float(acfg.get("cooldown_s", 20))
        quiet_hours = acfg.get("quiet_hours")
        banner_enabled = bool(acfg.get("banner", True))
        sound_enabled = bool(acfg.get("sound", True))
        sounds_map = acfg.get("sounds") or DEFAULT_SOUNDS

        now = self.clock()
        muted = get_muted(self.cfg)
        self._status_light(acfg)

        by_plan = bool(acfg.get("plan_reports", True))
        if acfg.get("combined", True):
            rows = self.store.all()
            for row in rows:
                if row.get("id"):
                    self._last_status[row["id"]] = row.get("status")
            plan_members = []
            if by_plan:   # a pipeline's phases are one piece of work: they report once, when the plan ends
                rows, plan_members = self.plans.split(rows)
            return self._combined(rows, alerts_on and not muted, banner_enabled, sound_enabled,
                                  quiet_hours, sounds_map, now, plan_members)

        all_rows = self.store.all()
        if by_plan and self.speaker is not None:
            for member in self.plans.split(all_rows)[1]:
                try:
                    self.speaker.say_report([member])
                except Exception as e:
                    print(f"[alerts] voice error: {e}", file=sys.stderr)
        for row in all_rows:
            aid = row.get("id")
            if not aid:
                continue

            status = row.get("status")
            cleared = bool(row.get("cleared"))

            is_new = aid not in self._last_status
            prev = self._last_status.get(aid)
            self._last_status[aid] = status

            if cleared or status == "stopped":
                continue

            changed = (status in ("waiting", "failed")) if is_new else (status != prev)
            if changed and self.speaker is not None and not (by_plan and self.plans.is_pipeline_row(row)):
                try:   # Speaker applies its own enabled/mute/event/quiet-hours/debounce rules
                    self.speaker.say_status(row, status)
                except Exception as e:
                    print(f"[alerts] voice error: {e}", file=sys.stderr)

            if not alerts_on:
                continue

            should_alert = False
            if is_new:
                if status in ("waiting", "failed") and status in events:
                    should_alert = True
            else:
                if status != prev and status in events:
                    should_alert = True

            if not should_alert:
                continue

            akey = f"{aid}#{row.get('job_id') or ''}"   # per job: a new job in the same terminal alerts at once
            last_alert_time = self._last_alert.get(akey, 0.0)
            if now - last_alert_time < cooldown_s:
                continue

            self._last_alert[akey] = now

            if muted:
                continue

            self._fire_alert(row, status, banner_enabled, sound_enabled, quiet_hours, sounds_map, now)

    def _combined(self, rows, notify, banner_enabled, sound_enabled, quiet_hours, sounds_map, now, plan_members=()):
        """Combined mode: a short line the moment a job blocks on the owner, one report when the batch settles
        (and one for each pipeline plan that just ended)."""
        from . import batch
        wait = float(get_alerts_cfg(self.cfg).get("report_max_wait_s", batch.MAX_WAIT_S) or 0)
        actions = self.batch.observe(rows, now=now, max_wait=wait) + [("report", [m]) for m in plan_members]
        for kind, payload in actions:
            if kind == "blocked":
                line = batch.blocked_line(payload, now)
                if self.speaker is not None:
                    try:
                        self.speaker.speak(line, event="waiting")
                    except Exception as e:
                        print(f"[alerts] voice error: {e}", file=sys.stderr)
                if notify:   # the banner now; the phone only if it is still blocked once the owner is away
                    self._deliver("Jarviee · Needs you", line, "needs", payload.get("id"), banner_enabled,
                                  sound_enabled, quiet_hours, sounds_map, now, phone=False)
                    self._phone_pending[batch.key_of(payload)] = line
                continue
            members = payload
            text = batch.report_text(members, now=now)
            batch.LAST.clear()
            batch.LAST.update(at=now, text=text, counts=batch.counts(members),
                              agents=[{"id": m.get("id"), "job_id": m.get("job_id"), "title": m.get("title"),
                                       "outcome": m["outcome"]} for m in members])
            if self.speaker is not None:
                try:
                    self.speaker.say_report(members)
                except Exception as e:
                    print(f"[alerts] voice error: {e}", file=sys.stderr)
            if notify:
                c = batch.counts(members)
                section = "needs" if c.get("needs") else "failed" if c.get("failed") else "done"
                rank = report_rank(members, get_alerts_cfg(self.cfg))
                phone = bool(rank) and self._away()
                if phone:   # this report covers their "needs you" too: one push, not two
                    for m in members:
                        self._phone_pending.pop(batch.key_of(m), None)
                self._deliver("Jarviee · Report", text, section, members[0].get("id") if len(members) == 1 and not members[0].get("plan") else "",
                              banner_enabled, sound_enabled, quiet_hours, sounds_map, now,
                              phone=phone, phone_priority=PHONE_PRIORITY.get(rank, 3),
                              tags=push_icon({"failed": "agent_failed", "needs": "agent_needs"}.get(section, "agent_done")))
        self._flush_phone_pending(rows, notify, quiet_hours, now)

    def _away(self):
        """True when phone pushes are on and the owner has not touched the Mac for `phone_away_s` (or the idle
        time can't be read)."""
        from . import push
        if not push.settings(self.cfg).get("enabled"):
            return False
        away_s = float(get_alerts_cfg(self.cfg).get("phone_away_s", 120) or 0)
        if away_s <= 0:
            return True
        idle = self.idle_reader() if self.idle_reader else None
        return idle is None or idle >= away_s

    def _flush_phone_pending(self, rows, notify, quiet_hours, now):
        """Push each job still blocked on the owner once they are away from the Mac; forget the ones answered."""
        if not self._phone_pending:
            return
        if not notify:
            self._phone_pending.clear()
            return
        from . import batch
        by_key = {batch.key_of(r): r for r in rows if r.get("id")}
        for k in list(self._phone_pending):
            if batch.outcome_of(by_key.get(k)) != "needs":   # answered, moved on or closed: nothing to push
                self._phone_pending.pop(k)
        if self._phone_pending and self._away():
            for k, line in list(self._phone_pending.items()):
                self._phone_pending.pop(k)
                self._send_phone("Jarviee · Needs you", line, quiet_hours, now, priority=PHONE_PRIORITY["high"],
                                 tags=push_icon("agent_needs"))

    def _send_phone(self, title, text, quiet_hours, now, priority=3, tags=None):
        if is_quiet_hours(quiet_hours, now):
            return
        try:
            from . import push
            if push.settings(self.cfg).get("enabled"):
                sender = self.phone_sender or (lambda *a, **k: threading.Thread(
                    target=push.publish, args=a, kwargs=k, daemon=True).start())
                sender(self.cfg, title, text, priority=priority, tags=tags or [])
        except Exception as e:
            print(f"[alerts] phone error: {e}", file=sys.stderr)

    def _deliver(self, title, text, section, aid, banner_enabled, sound_enabled, quiet_hours, sounds_map, now,
                 phone=True, phone_priority=3, tags=None):
        """One banner, one sound and (when `phone`) one phone push for a combined-mode message."""
        head, _, body = text.partition("\n")
        if phone:
            self._send_phone(title, text, quiet_hours, now, priority=phone_priority, tags=tags)
        if banner_enabled:
            message = (head + (" · " + body.replace("\n", " · ") if body else ""))[:240]
            notifier = self._notifier()
            if notifier:
                url = f"http://127.0.0.1:{int((self.cfg or {}).get('port', 7777))}/" + (
                    f"#/agents/{aid}?focus=1" if aid else "")
                self._run_cmd([notifier, "-title", title, "-message", message, "-open", url,
                               "-group", f"jarviee-{aid or 'report'}"])
            else:
                self._run_cmd(["osascript", "-e", "on run argv",
                               "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                               "-e", "end run", message, title])
        if sound_enabled and not is_quiet_hours(quiet_hours, now):
            snd_name = sounds_map.get(section)
            if snd_name and isinstance(snd_name, str) and re.match(r"^[A-Za-z]+$", snd_name):
                snd_path = f"/System/Library/Sounds/{snd_name}.aiff"
                if os.path.isfile(snd_path):
                    self._run_cmd(["afplay", snd_path])

    def _notifier(self):
        """terminal-notifier when installed (alerts.notifier "auto"), else None for the osascript banner."""
        choice = str(((self.cfg or {}).get("alerts") or {}).get("notifier", "auto"))
        if choice == "osascript":
            return None
        return shutil.which("terminal-notifier") or next(
            (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if os.path.isfile(p)), None)

    def _status_light(self, acfg):
        """Run `alerts.status_cmd` (argv list or shell-free string) with the overall state appended whenever it
        changes: needs | failed | running | idle. For a USB light, a Shortcut, anything."""
        cmd = acfg.get("status_cmd")
        if not cmd:
            return
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd).split()
        if not argv:
            return
        sections = set()
        for r in self.store.all():
            if r.get("cleared") or r.get("status") in ("stopped", "done"):
                continue
            st = r.get("status")
            sections.add(section_of(st, r.get("activity")) if st == "waiting" else st)
        state = ("needs" if "needs" in sections else "failed" if "failed" in sections
                 else "running" if sections else "idle")
        if state == getattr(self, "_light", None):
            return
        first = not hasattr(self, "_light")
        self._light = state
        if first and state == "idle":
            return
        self._run_cmd([str(a) for a in argv] + [state])

    def _phone(self, row, status, section, quiet_hours, now):
        """Push "needs you" / "failed" to the phone (ntfy) while the owner is away from the Mac; a reply that just
        means "your turn" stays on the Mac."""
        if section not in ("needs", "failed") or not self._away():
            return
        ws = os.path.basename(str(row.get("workspace") or "").rstrip("/\\"))
        title = f"Jarviee · {SECTIONS.get(section, str(status).capitalize())}"
        msg = f"{str(row.get('title') or '')[:60]} ({ws})" if ws else str(row.get("title") or "")[:60]
        self._send_phone(title, msg or title, quiet_hours, now, priority=PHONE_PRIORITY["high"],
                         tags=push_icon("agent_failed" if status == "failed" else "agent_needs"))

    def _fire_alert(self, row, status, banner_enabled, sound_enabled, quiet_hours, sounds_map, now):
        section = section_of(status, row.get("activity"))
        self._phone(row, status, section, quiet_hours, now)
        if banner_enabled:
            title = f"Jarviee · {SECTIONS.get(section, str(status).capitalize())}"

            ws = row.get("workspace") or ""
            ws_base = os.path.basename(ws.rstrip("/\\")) if ws else ""
            tool = str(row.get("tool") or "")
            subtitle = f"{ws_base} · {tool}"

            row_title = str(row.get("title") or "")[:60]
            activity = str(row.get("activity") or "")[:80]
            if section == "responded":
                activity = "replied, your turn"
            message = f"{row_title} — {activity}"

            notifier = self._notifier()
            if notifier:   # its banners can carry a click action: open this agent in Jarviee
                url = f"http://127.0.0.1:{int((self.cfg or {}).get('port', 7777))}/#/agents/{row.get('id', '')}?focus=1"
                self._run_cmd([notifier, "-title", title, "-subtitle", subtitle, "-message", message or title,
                               "-open", url, "-group", f"jarviee-{row.get('id', '')}"])
                banner_argv = None
            else:   # osascript banners belong to Script Editor: clicking one opens an empty Script Editor window
                banner_argv = [
                "osascript",
                "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
                "-e", "end run",
                message,
                title,
                subtitle,
            ]
            if banner_argv:
                self._run_cmd(banner_argv)

        if sound_enabled and not is_quiet_hours(quiet_hours, now):
            snd_name = sounds_map.get(section) or sounds_map.get(status)
            if snd_name and isinstance(snd_name, str) and re.match(r"^[A-Za-z]+$", snd_name):
                snd_path = f"/System/Library/Sounds/{snd_name}.aiff"
                if os.path.isfile(snd_path):
                    sound_argv = ["afplay", snd_path]
                    self._run_cmd(sound_argv)
