"""Tests for Jarviee ambient alerts (macOS banner + sound)."""
import json
import os
from pathlib import Path
import threading
import time
import unittest
import urllib.error
import urllib.request

from helpers import TempDir, free_port, make_cfg
from hub import alerts, auth
from hub.alerts import AlertWatcher, get_muted, set_muted
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))


class FakeStore:
    def __init__(self, rows=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}

    def all(self):
        return [dict(r) for r in self._rows.values()]

    def set_row(self, row):
        self._rows[row["id"]] = dict(row)

    def upsert(self, aid, **fields):
        r = self._rows.setdefault(aid, {"id": aid})
        r.update(fields)


class AlertsWatcherTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.state_file = Path(self.tmp) / "alerts.json"
        self.cfg = {
            "alerts_state_path": str(self.state_file),
            "alerts": {
                "combined": False,   # per-window alerts; the combined report has its own tests (test_batch_report)
                "enabled": True,
                "banner": True,
                "sound": True,
                "notifier": "osascript",   # these tests check the osascript banner; don't depend on terminal-notifier being installed
                "cooldown_s": 20,
                "poll_s": 1.5,
            },
        }
        self.runner = FakeRunner()
        self.current_time = 1700000000.0
        self.clock = lambda: self.current_time

    def tearDown(self):
        self._t.__exit__()

    def test_sections_split_replied_from_blocked(self):
        self.assertEqual(alerts.section_of("waiting", "turn finished, waiting for you"), "responded")
        self.assertEqual(alerts.section_of("waiting", "Claude is waiting for your input"), "responded")
        self.assertEqual(alerts.section_of("waiting", "Claude needs your permission to use Bash"), "needs")
        self.assertEqual(alerts.section_of("waiting", ""), "needs")
        self.assertEqual(alerts.section_of("done", "session ended"), "done")
        self.assertEqual(alerts.section_of("failed", "boom"), "failed")

        store = FakeStore([{"id": "r1", "status": "running", "title": "Refactor", "activity": "thinking",
                            "workspace": "/x/webapp", "tool": "claude"}])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()
        store.set_row({"id": "r1", "status": "waiting", "title": "Refactor", "activity": "turn finished, waiting for you",
                       "workspace": "/x/webapp", "tool": "claude"})
        watcher.poll_once()
        banner, sound = self.runner.calls[0], self.runner.calls[1]
        self.assertEqual(banner[7], "Refactor — replied, your turn")
        self.assertEqual(banner[8], "Jarviee · Responded")
        self.assertEqual(sound, ["afplay", "/System/Library/Sounds/Pop.aiff"])

        self.current_time += 100
        store.set_row({"id": "r1", "status": "done", "title": "Refactor", "activity": "session ended",
                       "workspace": "/x/webapp", "tool": "claude"})
        watcher.poll_once()
        self.assertEqual(self.runner.calls[2][8], "Jarviee · Done")

    def test_phone_push_for_waiting_and_failed_only(self):
        sent = []
        self.cfg["push"] = {"enabled": True}
        store = FakeStore([{"id": "p1", "status": "running", "title": "Fix", "activity": "x", "workspace": "/a/web"}])
        w = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        w.phone_sender = lambda cfg, title, msg, **k: sent.append((title, msg))
        w.idle_reader = lambda: 600   # the owner is away from the Mac
        w.poll_once()
        store.set_row({"id": "p1", "status": "waiting", "title": "Fix", "activity": "needs permission", "workspace": "/a/web"})
        w.poll_once()
        self.current_time += 100
        store.set_row({"id": "p1", "status": "done", "title": "Fix", "activity": "end", "workspace": "/a/web"})
        w.poll_once()
        self.assertEqual(sent, [("Jarviee · Needs your response", "Fix (web)")])

    def test_status_light_runs_on_state_change(self):
        self.cfg["alerts"] = {"status_cmd": ["light"], "banner": False, "sound": False, "combined": False}
        store = FakeStore([{"id": "l1", "status": "running", "title": "T", "activity": "x"}])
        w = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        w.poll_once()
        w.poll_once()
        store.set_row({"id": "l1", "status": "waiting", "title": "T", "activity": "needs permission"})
        w.poll_once()
        store.set_row({"id": "l1", "status": "done", "title": "T", "activity": "end"})
        w.poll_once()
        lights = [c for c in self.runner.calls if c[0] == "light"]
        self.assertEqual(lights, [["light", "running"], ["light", "needs"], ["light", "idle"]])

    def test_phone_push_off_by_default(self):
        sent = []
        store = FakeStore([{"id": "p1", "status": "running", "title": "Fix", "activity": "x"}])
        w = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        w.phone_sender = lambda *a, **k: sent.append(a)
        w.poll_once()
        store.set_row({"id": "p1", "status": "failed", "title": "Fix", "activity": "boom"})
        w.poll_once()
        self.assertEqual(sent, [])

    def test_seeded_rows_do_not_alert(self):
        store = FakeStore([
            {"id": "a1", "status": "waiting", "title": "T1", "activity": "prompt"},
            {"id": "a2", "status": "failed", "title": "T2", "activity": "err"},
            {"id": "a3", "status": "running", "title": "T3", "activity": "thinking"},
        ])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 0)

    def test_running_to_waiting_alerts_once_with_banner_and_glass(self):
        store = FakeStore([
            {
                "id": "a1",
                "status": "running",
                "title": "Fix login bug",
                "activity": "thinking",
                "workspace": "/Users/alex/projects/webapp",
                "tool": "claude",
            }
        ])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 0)

        # Transition into waiting
        store.set_row({
            "id": "a1",
            "status": "waiting",
            "title": "Fix login bug",
            "activity": "Claude needs your permission",
            "workspace": "/Users/alex/projects/webapp",
            "tool": "claude",
        })
        watcher.poll_once()

        self.assertEqual(len(self.runner.calls), 2)
        banner_cmd = self.runner.calls[0]
        self.assertEqual(banner_cmd[0], "osascript")
        self.assertEqual(banner_cmd[1:7], [
            "-e", "on run argv",
            "-e", "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
            "-e", "end run",
        ])
        self.assertEqual(banner_cmd[7], "Fix login bug — Claude needs your permission")
        self.assertEqual(banner_cmd[8], "Jarviee · Needs your response")
        self.assertEqual(banner_cmd[9], "webapp · claude")

        sound_cmd = self.runner.calls[1]
        self.assertEqual(sound_cmd, ["afplay", "/System/Library/Sounds/Glass.aiff"])

        # Second poll without changes should not alert again
        self.runner.calls.clear()
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 0)

    def test_cooldown_suppresses_repeat(self):
        store = FakeStore([{"id": "a1", "status": "running"}])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()

        # Fire first alert at t=1700000000
        store.set_row({"id": "a1", "status": "waiting", "title": "Task 1", "activity": "act"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 2)
        self.runner.calls.clear()

        # Flip to running then back to waiting at t + 10s (within 20s cooldown)
        self.current_time += 10.0
        store.set_row({"id": "a1", "status": "running"})
        watcher.poll_once()
        store.set_row({"id": "a1", "status": "waiting", "title": "Task 1", "activity": "act"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 0)

        # Advance past cooldown (> 20s)
        self.current_time += 15.0  # now 25s after initial alert
        store.set_row({"id": "a1", "status": "running"})
        watcher.poll_once()
        store.set_row({"id": "a1", "status": "waiting", "title": "Task 1", "activity": "act"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 2)

    def test_quiet_hours_drops_sound_only(self):
        self.cfg["alerts"]["quiet_hours"] = ["22:00", "08:00"]
        # Construct timestamp corresponding to 23:30 local time
        now_lt = time.localtime(self.current_time)
        t_2330 = time.mktime((
            now_lt.tm_year, now_lt.tm_mon, now_lt.tm_mday,
            23, 30, 0,
            now_lt.tm_wday, now_lt.tm_yday, now_lt.tm_isdst,
        ))
        self.current_time = t_2330

        store = FakeStore([{"id": "a1", "status": "running"}])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()

        store.set_row({"id": "a1", "status": "waiting", "title": "Task Q", "activity": "waiting"})
        watcher.poll_once()

        # Banner fires, sound is dropped
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.runner.calls[0][0], "osascript")

    def test_muted_drops_everything(self):
        set_muted(self.cfg, True)
        self.assertTrue(get_muted(self.cfg))

        store = FakeStore([{"id": "a1", "status": "running"}])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()

        store.set_row({"id": "a1", "status": "failed", "title": "Task Fail", "activity": "crashed"})
        watcher.poll_once()

        self.assertEqual(len(self.runner.calls), 0)

    def test_injection_safety(self):
        store = FakeStore([{"id": "a1", "status": "running"}])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()

        malicious_title = 'Title " with quotes and "; do shell script "x'
        malicious_activity = 'Act " with quotes and "; do shell script "y'
        store.set_row({
            "id": "a1",
            "status": "waiting",
            "title": malicious_title,
            "activity": malicious_activity,
            "workspace": "/ws",
            "tool": "claude",
        })
        watcher.poll_once()

        self.assertGreaterEqual(len(self.runner.calls), 1)
        banner_cmd = self.runner.calls[0]
        self.assertEqual(banner_cmd[0], "osascript")
        self.assertEqual(banner_cmd[1:7], [
            "-e", "on run argv",
            "-e", "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
            "-e", "end run",
        ])
        for script_arg in (banner_cmd[2], banner_cmd[4], banner_cmd[6]):
            self.assertNotIn("shell script", script_arg)
            self.assertNotIn("quotes", script_arg)
        self.assertEqual(banner_cmd[7], f"{malicious_title} — {malicious_activity}")

    def test_row_first_appears_waiting_or_failed_alerts(self):
        store = FakeStore([])
        watcher = AlertWatcher(self.cfg, store, runner=self.runner, clock=self.clock)
        watcher.poll_once()

        # New row appears waiting
        store.set_row({"id": "w1", "status": "waiting", "title": "New Wait", "activity": "prompt"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 2)
        self.runner.calls.clear()

        # New row appears failed
        store.set_row({"id": "f1", "status": "failed", "title": "New Fail", "activity": "err"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.runner.calls[1], ["afplay", "/System/Library/Sounds/Basso.aiff"])
        self.runner.calls.clear()

        # New row appears stopped -> never alerts
        store.set_row({"id": "s1", "status": "stopped", "title": "Stopped", "activity": "stopped"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 0)

        # New row with cleared=1 -> skipped
        store.set_row({"id": "c1", "status": "waiting", "cleared": 1, "title": "Cleared", "activity": "waiting"})
        watcher.poll_once()
        self.assertEqual(len(self.runner.calls), 0)


class AlertsServerApiTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, self.cfg_path = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.token = auth.get_token(self.cfg)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def _req(self, method="GET", path="/", body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with _OPEN(r, timeout=3) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw.decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_get_and_post_alerts_endpoint(self):
        # Initial GET
        code, body = self._req("GET", "/api/alerts")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"enabled": True, "muted": False, "banner": True, "sound": True})

        # POST without token should be 403
        code, body = self._req("POST", "/api/alerts", body={"muted": True})
        self.assertEqual(code, 403)

        # POST with token to mute
        code, body = self._req("POST", "/api/alerts", body={"muted": True}, headers={"X-Jarviee-Token": self.token})
        self.assertEqual(code, 200)
        self.assertEqual(body, {"enabled": True, "muted": True, "banner": True, "sound": True})

        # GET reflects muted state
        code, body = self._req("GET", "/api/alerts")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"enabled": True, "muted": True, "banner": True, "sound": True})

        # POST with token to unmute
        code, body = self._req("POST", "/api/alerts", body={"muted": False}, headers={"X-Jarviee-Token": self.token})
        self.assertEqual(code, 200)
        self.assertEqual(body, {"enabled": True, "muted": False, "banner": True, "sound": True})


if __name__ == "__main__":
    unittest.main()
