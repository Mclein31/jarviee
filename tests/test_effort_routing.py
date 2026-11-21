"""Effort routing: the effort is chosen per task and then fitted to the model that runs it.
agy carries the effort in the model slug (gemini-4-hydrogen-<effort>), claude takes it as --effort."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import dispatch, router  # noqa: E402
from hub.runplan import build_argv  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text())
ALL = ["low", "medium", "high", "xhigh", "max"]


def route(task, tool, level=None, model=None):
    return dispatch.route(task, CFG, tool=tool, level=level, model=model, check_available=False)


class EffortLevels(unittest.TestCase):
    def test_each_agy_model_has_its_own_levels(self):
        self.assertEqual(router.model_efforts("agy", "gemini-3.8-flash-low", CFG), ["low", "medium", "high"])
        self.assertEqual(router.model_efforts("agy", "gemini-4-hydrogen-medium", CFG), ["low", "medium", "high", "xhigh"])
        self.assertEqual(router.model_efforts("agy", "gemini-4-argon-high", CFG), ["low", "medium", "high"])

    def test_every_claude_model_takes_all_five(self):
        for m in ("haiku", "sonnet", "opus", "claude-fable-5-2"):
            self.assertEqual(router.model_efforts("claude", m, CFG), ALL, m)

    def test_list_form_and_missing_config(self):
        cfg = {"tools": {"x": {"effort_levels": ["high", "low"]}, "y": {}}}
        self.assertEqual(router.model_efforts("x", "anything", cfg), ["low", "high"])
        self.assertEqual(router.model_efforts("y", "anything", cfg), ALL)

    def test_clamp_goes_to_the_nearest_supported_level(self):
        self.assertEqual(router.clamp_effort("max", ["low", "medium", "high"]), "high")
        self.assertEqual(router.clamp_effort("xhigh", ["low", "medium", "high", "xhigh"]), "xhigh")
        self.assertEqual(router.clamp_effort("low", ["medium"]), "medium")
        self.assertEqual(router.clamp_effort("high", []), "")


class FitEffort(unittest.TestCase):
    def test_agy_slug_follows_the_effort(self):
        self.assertEqual(router.fit_effort("agy", "gemini-4-hydrogen-medium", "xhigh", CFG), ("gemini-4-hydrogen-xhigh", "xhigh"))
        self.assertEqual(router.fit_effort("agy", "gemini-4-argon-high", "max", CFG), ("gemini-4-argon-high", "high"))
        self.assertEqual(router.fit_effort("agy", "gemini-3.8-flash-low", "medium", CFG), ("gemini-3.8-flash-medium", "medium"))

    def test_agy_model_picked_by_name_keeps_its_slug(self):
        self.assertEqual(router.fit_effort("agy", "gemini-4-hydrogen-low", "high", CFG, keep_model=True),
                         ("gemini-4-hydrogen-low", "low"))

    def test_agy_slug_without_effort_is_left_alone(self):
        self.assertEqual(router.fit_effort("agy", "gemini-pro-orchestrator-g1-pro", "high", CFG),
                         ("gemini-pro-orchestrator-g1-pro", ""))

    def test_claude_keeps_the_model_and_clamps_the_flag(self):
        self.assertEqual(router.fit_effort("claude", "opus", "xhigh", CFG), ("opus", "xhigh"))
        cfg = {"tools": {"claude": {"effort_levels": {"haiku": ["low", "medium"]}}}}
        self.assertEqual(router.fit_effort("claude", "haiku", "max", cfg), ("haiku", "medium"))
        self.assertEqual(router.fit_effort("claude", "sonnet", "", CFG), ("sonnet", ""))


class RouteAndArgv(unittest.TestCase):
    def test_agy_task_routes_effort_into_the_slug(self):
        r = route("debug the flaky login test", "agy", "light")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-hydrogen-high", "high"))
        r = route("rename a variable", "agy", "light")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-hydrogen-low", "low"))

    def test_complex_on_agy_is_the_neon_orchestrator_without_effort(self):
        # agy complex since 2026-11-21 (DECISIONS): Neon plans, Hydrogen executes; agy fixes the effort itself
        for task in ("debug the race condition in the scheduler", "rename a variable"):
            r = route(task, "agy", "complex")
            self.assertEqual((r["model"], r["effort"]), ("gemini-pro-orchestrator-neon", ""))

    def test_heavy_on_agy_is_neon_up_to_xhigh(self):
        r = route("debug the race condition deadlock in the scheduler", "agy", "heavy")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-neon-xhigh", "xhigh"))
        r = route("rename a variable across the codebase", "agy", "heavy")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-neon-high", "high"))

    def test_argon_never_goes_past_high(self):
        self.assertEqual(router.fit_effort("agy", "gemini-4-argon-medium", "xhigh", CFG), ("gemini-4-argon-high", "high"))

    def test_claude_task_routes_effort_into_the_flag(self):
        r = route("debug the race condition in the scheduler", "claude", "complex")
        self.assertEqual((r["model"], r["effort"]), ("sonnet", "high"))
        r = route("rename a variable", "claude", "heavy")
        self.assertEqual((r["model"], r["effort"]), ("opus", "medium"))
        r = route("fix typo in README", "claude", "basic")
        self.assertEqual((r["model"], r["effort"]), ("haiku", "low"))

    def test_command_lines_agree_with_the_route(self):
        r = route("debug the race condition in the scheduler", "agy", "heavy")
        argv = build_argv(CFG["tools"]["agy"], "P", r["model"], r["effort"])
        self.assertIn("gemini-4-neon-xhigh", argv)
        self.assertNotIn("--effort", argv)                    # the slug already says it
        r = route("debug the race condition in the scheduler", "agy", "complex")
        argv = build_argv(CFG["tools"]["agy"], "P", r["model"], r["effort"])
        self.assertIn("gemini-pro-orchestrator-neon", argv)
        self.assertNotIn("--effort", argv)                    # Orchestrator takes no effort
        r = route("debug the race condition in the scheduler", "claude", "complex")
        argv = build_argv(CFG["tools"]["claude"], "P", r["model"], r["effort"])
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_model_named_by_the_owner_is_not_rewritten(self):
        r = route("debug the race condition in the scheduler", "agy", "complex", model="gemini-4-hydrogen-low")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-hydrogen-low", "low"))


if __name__ == "__main__":
    unittest.main()
