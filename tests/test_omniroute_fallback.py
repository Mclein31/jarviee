"""Tests for OmniRoute last-resort fallback, scheduler lane handling, detector attribution, and statusline isolation."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, TempDir, make_cfg
from hub import actions, detector, dispatch, fallback, launcher, limitwatch, omniroute
from hub.scheduler import Scheduler
from hub.store import Store
from hub.tasks import Tasks

_SL_SPEC = importlib.util.spec_from_file_location("statusline_mod", ROOT / "hooks" / "statusline.py")
statusline = importlib.util.module_from_spec(_SL_SPEC)
_SL_SPEC.loader.exec_module(statusline)


def _fake_python_cmd(tool_name):
    script = (
        "import sys, time, re; "
        "p = sys.argv[2]; "
        "time.sleep(float((re.search(r'FAKE_SLEEP=([0-9.]+)', p) or [0, '0.05'])[1])); "
        "print('fake', sys.argv[1], 'ran with model', sys.argv[3]); "
        "m = re.search(r'FAKE_SAY=\"([^\"]+)\"', p); m and print(m[1]); "
        "e = re.search(r'FAKE_EXIT=(\\d+)', p); "
        "sys.exit(int(e[1]) if e else 0)"
    )
    return [sys.executable, "-c", script, tool_name, "{prompt}", "{model}"]


class OmniRouteFallbackTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["omniroute"]["enabled"] = True   # off in the public config.json
        self.cfg["tools"]["omniroute"]["command"] = _fake_python_cmd("omniroute")
        self.store = Store(str(Path(self.tmp) / "hub.db"))
        omniroute.reset_cache()

    def tearDown(self):
        omniroute.reset_cache()
        self._t.__exit__()

    def _exhaust_both(self, now, agy_until=None, claude_until=None):
        self.store.set_tool_quota("agy", "exhausted", until=agy_until or (now + 3600), last_error="429", cfg=self.cfg)
        self.store.set_tool_quota("claude", "exhausted", until=claude_until or (now + 3600), last_error="5h limit", cfg=self.cfg)

    def test_omniroute_chosen_only_when_agy_and_claude_both_out(self):
        now = 1700000000.0
        # Only agy is out -> Claude sonnet wins, not OmniRoute.
        self.store.set_tool_quota("agy", "exhausted", until=now + 3600, last_error="429", cfg=self.cfg)
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res1 = fallback.resolve_fallback("agy", "gemini-4-hydrogen-medium", self.cfg, store=self.store, now=now)
        self.assertEqual((res1["tool"], res1["model"]), ("claude", "sonnet"))

        # Now Claude is also out -> OmniRoute is chosen as last resort.
        self.store.set_tool_quota("claude", "exhausted", until=now + 3600, last_error="limit", cfg=self.cfg)
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res2 = fallback.resolve_fallback("agy", "gemini-4-hydrogen-medium", self.cfg, store=self.store, now=now)
        self.assertEqual(res2["tool"], "omniroute")
        self.assertEqual(res2["model"], "claude/combo/jarviee-light")
        self.assertEqual(res2["level"], "light")
        self.assertEqual(res2["hops"], 1)
        self.assertEqual(res2["why"], "OmniRoute (agy and Claude are out)")

    def test_not_chosen_when_disabled(self):
        now = 1700000000.0
        self._exhaust_both(now)
        self.cfg["omniroute"]["enabled"] = False
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, now=now)
        self.assertTrue(res.get("waiting"))
        self.assertIsNone(res.get("tool"))

    def test_not_chosen_when_unhealthy(self):
        now = 1700000000.0
        self._exhaust_both(now, claude_until=now + 1200)
        with patch.object(omniroute, "health", return_value=(False, "gateway not running")):
            res = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, now=now)
        self.assertTrue(res.get("waiting"))
        self.assertIsNone(res.get("tool"))
        self.assertEqual(res.get("earliest_until"), now + 1200)

    def test_not_chosen_when_exhausted_and_earliest_until_unchanged(self):
        now = 1700000000.0
        self._exhaust_both(now, agy_until=now + 2400, claude_until=now + 1800)
        # Shorter cooldown on omniroute must NOT alter earliest_until of waiting result.
        self.store.set_tool_quota("omniroute", "exhausted", until=now + 60, last_error="all providers failed", cfg=self.cfg)
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, now=now)
        self.assertTrue(res.get("waiting"))
        self.assertIsNone(res.get("tool"))
        self.assertEqual(res.get("earliest_until"), now + 1800)

    def test_never_offered_when_failing_tool_is_omniroute(self):
        now = 1700000000.0
        self._exhaust_both(now)
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res = fallback.resolve_fallback(
                "omniroute", "claude/combo/jarviee-complex", self.cfg, store=self.store, now=now
            )
        self.assertTrue(res.get("waiting"))
        self.assertIsNone(res.get("tool"))

    def test_max_hops_enforced(self):
        now = 1700000000.0
        self._exhaust_both(now)
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res = fallback.resolve_fallback(
                "agy", "gemini-3.8-flash-low", self.cfg, store=self.store, hops=2, now=now
            )
        self.assertIsNone(res)

    def test_heavy_level_preserved_and_opus_rule_ignored(self):
        now = 1700000000.0
        self._exhaust_both(now)
        self.cfg["fallback"]["never_fallback_to_opus"] = True
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            res = fallback.resolve_fallback(
                "claude", "opus", self.cfg, store=self.store, now=now, original_level="heavy"
            )
        self.assertEqual(res["tool"], "omniroute")
        self.assertEqual(res["model"], "claude/combo/jarviee-heavy")
        self.assertEqual(res["level"], "heavy")
        self.assertEqual(res["effort"], "high")

    def test_dispatch_route_falls_back_to_omniroute(self):
        now = time.time()
        self._exhaust_both(now)
        with patch.object(omniroute, "health", return_value=(True, "ok")):
            r = dispatch.route("refactor the scheduler", self.cfg, level="complex", store=self.store, now=now)
        self.assertEqual(r["tool"], "omniroute")
        self.assertEqual(r["model"], "claude/combo/jarviee-complex")
        self.assertEqual(r["level"], "complex")
        self.assertFalse(r["waiting_for_quota"])


class OmniRouteSchedulerTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["omniroute"]["enabled"] = True   # off in the public config.json
        self.cfg["tools"]["omniroute"]["command"] = _fake_python_cmd("omniroute")
        self.cfg["tools"]["omniroute"].pop("session_args", None)
        self.cfg["tools"]["omniroute"].pop("resume_args", None)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks, interval=0.1)
        now = time.time()
        self.store.set_tool_quota("agy", "exhausted", until=now + 3600, last_error="429", cfg=self.cfg)
        self.store.set_tool_quota("claude", "exhausted", until=now + 3600, last_error="5h", cfg=self.cfg)

    def tearDown(self):
        self._t.__exit__()

    def test_queued_task_falling_back_to_omniroute_runs_without_lane(self):
        self.assertNotIn("omniroute", self.cfg.get("lanes", {}))
        pid = self.tasks.add_plan("goal", "", [{
            "id": "t1", "title": "t1", "prompt": "do task 1",
            "level": "complex", "tool": "claude", "model": "sonnet",
        }])
        self.tasks.approve(pid)
        tid = self.tasks.plan(pid)["tasks"][0]["id"]

        with patch.dict(os.environ, {"OMNIROUTE_API_KEY": "test-dummy-key"}), \
             patch.object(omniroute, "health", return_value=(True, "ok")):
            self.sched.tick()
            row = self.tasks.get(tid)
            self.assertEqual(row["tool"], "omniroute")
            self.assertEqual(row["model"], "claude/combo/jarviee-complex")
            self.assertEqual(row["status"], "running")
            self.assertTrue(launcher.wait(row["run_id"], timeout=10))
            self.assertEqual(self.tasks.get(tid)["status"], "done")

    def test_second_omniroute_task_respects_implicit_cap_1(self):
        pid = self.tasks.add_plan("goal", "", [
            {"id": "t1", "title": "t1", "prompt": "FAKE_SLEEP=0.4 task 1",
             "level": "basic", "tool": "agy", "model": "gemini-3.8-flash-low"},
            {"id": "t2", "title": "t2", "prompt": "FAKE_SLEEP=0.1 task 2",
             "level": "light", "tool": "agy", "model": "gemini-4-hydrogen-medium"},
        ])
        self.tasks.approve(pid)
        t1, t2 = [t["id"] for t in self.tasks.plan(pid)["tasks"]]

        with patch.dict(os.environ, {"OMNIROUTE_API_KEY": "test-dummy-key"}), \
             patch.object(omniroute, "health", return_value=(True, "ok")):
            self.sched.tick()
            r1, r2 = self.tasks.get(t1), self.tasks.get(t2)
            self.assertEqual(r1["status"], "running")
            self.assertEqual(r1["tool"], "omniroute")
            self.assertEqual(r2["status"], "queued")
            self.assertEqual(r2["route_note"], "omniroute lane full")
            self.assertTrue(launcher.wait(r1["run_id"], timeout=10))
            self.sched.tick()
            r2_after = self.tasks.get(t2)
            self.assertEqual(r2_after["status"], "running")
            self.assertTrue(launcher.wait(r2_after["run_id"], timeout=10))
            self.assertEqual(self.tasks.get(t2)["status"], "done")


class OmniRouteDetectorAndIsolationTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["omniroute"]["enabled"] = True   # off in the public config.json
        self.cfg["tools"]["omniroute"]["command"] = _fake_python_cmd("omniroute")
        self.cfg["tools"]["omniroute"].pop("session_args", None)
        self.cfg["tools"]["omniroute"].pop("resume_args", None)
        self.cfg["limit_state_path"] = str(Path(self.tmp) / "limit-watch.json")
        self.cfg["push"] = {"enabled": True, "limit_alerts": True}
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks, interval=0.1)

    def tearDown(self):
        self._t.__exit__()

    def test_detector_omniroute_gateway_errors_and_no_regression_for_agy(self):
        now = 1700000000.0
        for text in (
            "API error: 503 Service Unavailable: all providers failed",
            "maximum combo retry limit reached",
            "ALL_TARGETS_SKIPPED: no healthy provider",
            "API error: 429 rate limit reached",
            'API Error: 429 {"error":{"type":"rate_limit_error"}}',
        ):
            det = detector.detect_failure("omniroute", 1, text, now=now)
            self.assertEqual(det.kind, detector.QUOTA, text)
            self.assertEqual(det.tool, "omniroute", text)
            self.assertIn("omniroute", det.message)

        # agy 503 stays TRANSIENT
        det_agy = detector.detect_failure("agy", 1, "HTTP 503 Service Unavailable: backend gateway connection reset.", now=now)
        self.assertEqual(det_agy.kind, detector.TRANSIENT)

    def test_detector_ignores_incidental_numbers_and_bare_503_is_transient(self):
        now = 1700000000.0
        incidental = (
            "Connected to localhost:5030\n"
            "Traceback at file.py line 429\n"
            "@@ -1,2 +1,2 @@\n"
            "+    return 503\n"
        )
        det = detector.detect_failure("omniroute", 1, incidental, now=now)
        self.assertNotEqual(det.kind, detector.QUOTA)

        bare_503 = detector.detect_failure("omniroute", 1, "API Error: 503 Service Unavailable", now=now)
        self.assertEqual(bare_503.kind, detector.TRANSIENT)

    def test_probe_tool_quota_omniroute_checks_health_not_claude_version(self):
        model = "claude/combo/jarviee-complex"
        self.store.set_quota("omniroute", model, "exhausted", until=time.time() + 3600, last_error="503")

        with patch.object(omniroute, "reset_cache") as mock_reset, \
             patch.object(omniroute, "health", return_value=(False, "gateway not running at http://127.0.0.1:20128")):
            ok, reason = actions.probe_tool_quota("omniroute", model, self.cfg, self.store)
        mock_reset.assert_called_once_with()
        self.assertFalse(ok)
        self.assertIn("gateway not running", reason)
        self.assertEqual(self.store.get_quota("omniroute", model)["status"], "exhausted")

        with patch.object(omniroute, "reset_cache") as mock_reset, \
             patch.object(omniroute, "health", return_value=(True, "ok")):
            ok2, _ = actions.probe_tool_quota("omniroute", model, self.cfg, self.store)
        mock_reset.assert_called_once_with()
        self.assertTrue(ok2)
        self.assertEqual(self.store.get_quota("omniroute", model)["status"], "ok")

    def test_anthropic_style_error_on_omniroute_recorded_under_omniroute_never_claude_or_limitwatch(self):
        pid = self.tasks.add_plan("goal", "", [{
            "id": "t1",
            "title": "t1",
            "prompt": 'FAKE_SAY="You\'ve reached your usage limit. Resets in 1h." FAKE_EXIT=1 do work',
            "level": "complex",
            "tool": "omniroute",
            "model": "claude/combo/jarviee-complex",
        }])
        self.tasks.approve(pid)
        tid = self.tasks.plan(pid)["tasks"][0]["id"]

        with patch.dict(os.environ, {"OMNIROUTE_API_KEY": "test-dummy-key"}), \
             patch.object(omniroute, "health", return_value=(True, "ok")):
            self.sched.tick()
            rid = self.tasks.get(tid)["run_id"]
            self.assertTrue(launcher.wait(rid, timeout=10))

        self.assertEqual(self.store.get_quota("omniroute", "claude/combo/jarviee-complex")["status"], "exhausted")
        self.assertEqual(self.store.get_quota("claude", "sonnet")["status"], "ok")
        self.assertEqual(self.store.get_quota("claude", "*")["status"], "ok")

        # LimitWatcher must NOT fire "Claude is spent" because of omniroute exhaustion.
        sent = []
        res = limitwatch.check(
            self.cfg,
            now=time.time(),
            store=self.store,
            limits={"windows": []},
            publish=lambda *a, **k: (sent.append(a), (True, ""))[1],
        )
        self.assertEqual(res, [])
        self.assertEqual(sent, [])


class OmniRouteStatuslineTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.limits_path = Path(self.tmp) / "claude-limits.json"

    def tearDown(self):
        self._t.__exit__()

    def _run_statusline_subprocess(self, extra_env):
        payload = {
            "model": {"display_name": "claude/combo/jarviee-complex"},
            "rate_limits": {
                "five_hour": {"used_percentage": 100.0, "resets_at": time.time() + 3600},
                "seven_day": {"used_percentage": 99.0, "resets_at": time.time() + 86400},
            },
        }
        env = dict(os.environ, JARVIEE_CLAUDE_LIMITS=str(self.limits_path))
        env.pop("JARVIEE_TOOL", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "statusline.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def test_statusline_ignores_jarviee_tool_omniroute(self):
        r = self._run_statusline_subprocess({"JARVIEE_TOOL": "omniroute"})
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.limits_path.exists())
        self.assertNotIn("Claude", r.stdout)

    def test_statusline_ignores_non_anthropic_base_url(self):
        r = self._run_statusline_subprocess({"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128"})
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.limits_path.exists())
        self.assertNotIn("Claude", r.stdout)

    def test_statusline_saves_for_normal_anthropic_session(self):
        with patch.object(statusline, "OUT", str(self.limits_path)), \
             patch.object(statusline, "agy_groups", return_value=[]), \
             patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, clear=False):
            os.environ.pop("JARVIEE_TOOL", None)
            payload = {
                "model": {"display_name": "Sonnet 4"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 42.0, "resets_at": time.time() + 3600},
                    "seven_day": {"used_percentage": 20.0, "resets_at": time.time() + 86400},
                },
            }
            with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                 patch("sys.stdout", new_callable=io.StringIO) as out:
                statusline.main()
                stdout = out.getvalue()
        self.assertTrue(self.limits_path.exists())
        saved = json.loads(self.limits_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["rate_limits"]["five_hour"]["used_percentage"], 42.0)
        self.assertIn("Claude", stdout)


if __name__ == "__main__":
    unittest.main()
