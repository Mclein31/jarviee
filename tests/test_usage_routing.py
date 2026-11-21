"""R2-3: dispatch steers away from a lane near its limit, using fake usage data."""
import unittest

from helpers import TempDir, make_cfg
from hub import dispatch, usage

CLAUDE_FULL = {"claude": {"limits": {"windows": [
    {"id": "five_hour", "label": "5-hour session", "used_pct": 10.0},
    {"id": "seven_day", "label": "Weekly", "used_pct": 98.0}]}}}
AGY_EMPTY = {"agy": {"quota": {"groups": [{"name": "g", "buckets": [
    {"window": "weekly", "remaining_pct": 0.0, "used_pct": 100.0}]}]}}}


class LanePressureTests(unittest.TestCase):
    def test_flags_claude_weekly_and_agy_zero(self):
        self.assertIn("claude", usage.lane_pressure(CLAUDE_FULL))
        self.assertIn("agy", usage.lane_pressure(AGY_EMPTY))

    def test_quiet_when_healthy_or_missing(self):
        ok = {"claude": {"limits": {"windows": [{"id": "seven_day", "used_pct": 50.0}]}},
              "agy": {"quota": {"groups": [{"buckets": [{"remaining_pct": 40.0}]}]}}}
        self.assertEqual(usage.lane_pressure(ok), {})
        self.assertEqual(usage.lane_pressure(None), {})
        self.assertEqual(usage.lane_pressure({"claude": {"error": "x"}}), {})

    def test_five_hour_window_alone_is_ignored(self):
        d = {"claude": {"limits": {"windows": [{"id": "five_hour", "used_pct": 100.0}]}}}
        self.assertEqual(usage.lane_pressure(d), {})


class RouteTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.cfg, _ = make_cfg(self._t.__enter__())
        self.cfg["fallback"]["never_fallback_to_opus"] = True

    def tearDown(self):
        self._t.__exit__()

    def test_claude_near_weekly_limit_reroutes_to_agy(self):
        r = dispatch.route("refactor the scheduler", self.cfg, level="complex", usage=CLAUDE_FULL)
        self.assertEqual(r["tool"], "agy")
        self.assertIn("Weekly at 98%", r["usage_warning"])

    def test_agy_empty_reroutes_to_claude_never_opus(self):
        # heavy is dispatched to claude by default, so force agy via the table
        self.cfg["dispatch"]["heavy"] = {"tool": "agy", "model": self.cfg["tools"]["agy"]["models"]["heavy"]}
        r = dispatch.route("redesign the architecture", self.cfg, level="heavy", usage=AGY_EMPTY)
        self.assertEqual((r["tool"], r["level"]), ("claude", "complex"))
        self.assertNotEqual(r["model"], "opus")

    def test_both_lanes_pressured_stays_and_warns(self):
        both = {**CLAUDE_FULL, **AGY_EMPTY}
        r = dispatch.route("refactor the scheduler", self.cfg, level="complex", usage=both)
        self.assertEqual(r["tool"], r["original_tool"])
        self.assertIn("and so is", r["usage_warning"])

    def test_manual_tool_only_warns(self):
        r = dispatch.route("refactor", self.cfg, level="complex", tool="claude", usage=CLAUDE_FULL)
        self.assertEqual(r["tool"], "claude")
        self.assertIn("near its limit", r["usage_warning"])

    def test_disabled_and_healthy_do_nothing(self):
        self.cfg["usage_routing"] = {"enabled": False}
        r = dispatch.route("refactor", self.cfg, level="complex", usage=CLAUDE_FULL)
        self.assertEqual((r["tool"], r["usage_warning"]), (r["original_tool"], ""))


if __name__ == "__main__":
    unittest.main()
