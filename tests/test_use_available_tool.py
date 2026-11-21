"""The "Use whichever tool is available" switch: when one tool is out, route to the other one."""
import json
import time
import unittest

from helpers import TempDir, make_cfg
from hub import config, dispatch, fallback
from hub.store import Store


class UseAvailableToolTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, self.cfg_path = make_cfg(self.tmp)
        self.cfg["fallback"]["equivalents"] = {}   # only the switch can help
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        self._t.__exit__()

    def out(self, tool):
        self.store.set_tool_quota(tool, "exhausted", until=time.time() + 3600, last_error="quota", cfg=self.cfg)

    def test_default_is_on(self):
        self.assertTrue(fallback.use_available_tool(self.cfg))

    def test_auto_route_moves_to_the_available_tool(self):
        self.out("claude")
        r = dispatch.route("refactor the scheduler into lanes", self.cfg, level="complex", store=self.store)
        self.assertEqual(r["tool"], "agy")
        self.assertFalse(r["waiting_for_quota"])
        self.assertTrue(any(x.startswith("fallback:") for x in r["reasons"]))

    def test_switch_rescues_a_task_the_chain_would_park(self):
        self.out("agy")   # agy's heavy model has no equivalent and Claude's opus is blocked by never-opus
        r = dispatch.route("redesign the architecture", self.cfg, level="heavy", tool="agy", store=self.store)
        self.assertEqual((r["tool"], r["model"], r["level"]), ("claude", "sonnet", "complex"))
        self.assertTrue(any("use available tool" in x for x in r["reasons"]))

    def test_switch_off_without_a_chain_waits_instead(self):
        self.out("claude")
        self.cfg["fallback"]["use_available_tool"] = False
        self.cfg["fallback"]["enabled"] = False
        res = fallback.resolve_fallback("claude", "sonnet", self.cfg, store=self.store, original_level="complex")
        self.assertIsNone(res)

    def test_manual_tool_pick_is_switched_only_when_on(self):
        self.out("agy")
        r = dispatch.route("fix a typo", self.cfg, level="basic", tool="agy", store=self.store)
        self.assertEqual(r["tool"], "claude")
        self.cfg["fallback"]["use_available_tool"] = False
        r = dispatch.route("fix a typo", self.cfg, level="basic", tool="agy", store=self.store)
        self.assertEqual(r["tool"], "agy")   # off: a manual choice always wins

    def test_heavy_never_lands_on_opus_when_never_opus_is_on(self):
        self.out("agy")
        res = fallback.resolve_fallback("agy", self.cfg["tools"]["agy"]["models"]["heavy"], self.cfg,
                                        store=self.store, original_level="heavy")
        self.assertEqual((res["tool"], res["model"]), ("claude", self.cfg["tools"]["claude"]["models"]["complex"]))

    def test_both_tools_out_still_waits(self):
        self.out("agy")
        self.out("claude")
        res = fallback.resolve_fallback("claude", "sonnet", self.cfg, store=self.store, original_level="complex")
        self.assertTrue(res["waiting"])
        self.assertIsNone(res["tool"])

    def test_settings_survive_a_reload(self):
        self.cfg["fallback"]["use_available_tool"] = False
        config.save_settings(self.cfg)
        saved = json.loads((config.settings_file(self.cfg)).read_text())
        self.assertFalse(saved["fallback"]["use_available_tool"])
        self.assertFalse(config.load_config(self.cfg_path)["fallback"]["use_available_tool"])


if __name__ == "__main__":
    unittest.main()
