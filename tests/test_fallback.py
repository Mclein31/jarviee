import unittest
import time
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import fallback, store as hub_store


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = hub_store.Store(str(Path(self.tmp) / "test.db"))

    def tearDown(self):
        self._t.__exit__()

    def test_equivalence_mapping_basic_light(self):
        # agy flash -> claude haiku
        res = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store)
        self.assertIsNotNone(res)
        self.assertEqual(res["tool"], "claude")
        self.assertEqual(res["model"], "haiku")
        self.assertEqual(res["level"], "basic")

        # agy argon -> claude sonnet
        res2 = fallback.resolve_fallback("agy", "gemini-4-argon-medium", self.cfg, store=self.store)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["tool"], "claude")
        self.assertEqual(res2["model"], "sonnet")

    def test_never_drop_more_than_one_level(self):
        # heavy (rank 3) attempting to drop to basic (rank 0)
        # Even if flash is in equivalents, it must be rejected because 3 - 0 = 3 > 1
        cfg = dict(self.cfg)
        cfg["fallback"] = {
            "enabled": True,
            "never_fallback_to_opus": False,
            "use_available_tool": False,   # this test is about the equivalence chain alone
            "max_hops": 2,
            "equivalents": {
                "claude:opus": ["agy:gemini-3.8-flash-low"]
            }
        }
        res = fallback.resolve_fallback("claude", "opus", cfg, store=self.store)
        # Should not pick flash because heavy drops more than one level
        self.assertTrue(res.get("waiting") or res.get("tool") is None)

    def test_never_fallback_to_opus_protection(self):
        # claude:sonnet -> claude:opus if configured, but default never_fallback_to_opus = True
        cfg = dict(self.cfg)
        cfg["fallback"] = {
            "enabled": True,
            "never_fallback_to_opus": True,
            "max_hops": 2,
            "equivalents": {
                "agy:gemini-4-argon-high": ["claude:opus", "claude:sonnet"]
            }
        }
        res = fallback.resolve_fallback("agy", "gemini-4-argon-high", cfg, store=self.store)
        self.assertIsNotNone(res)
        self.assertEqual(res["tool"], "claude")
        self.assertEqual(res["model"], "sonnet")   # skipped opus!

    def test_two_hop_cap(self):
        # 1st hop: hops=0 -> hops=1
        res1 = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, hops=0)
        self.assertEqual(res1["hops"], 1)

        # 2nd hop: hops=1 -> hops=2
        res2 = fallback.resolve_fallback("claude", "haiku", self.cfg, store=self.store, hops=1)
        self.assertEqual(res2["hops"], 2)

        # 3rd hop: hops=2 -> exceeded max_hops 2, returns None
        res3 = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, hops=2)
        self.assertIsNone(res3)

    def test_preflight_skips_exhausted_and_expiry_restores(self):
        now = 1700000000.0
        # Mark claude:haiku exhausted until now + 3600
        self.store.set_quota("claude", "haiku", "exhausted", until=now + 3600, last_error="429")

        # Fallback from agy:flash normally goes to claude:haiku
        # But haiku is exhausted, so fallback should see it as unavailable
        res = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, now=now)
        self.assertTrue(res.get("waiting") or res.get("tool") is None)
        self.assertEqual(res.get("earliest_until"), now + 3600)

        # When now >= now + 3600, cooldown expires and auto-restores
        res_restored = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, now=now + 3601)
        self.assertIsNotNone(res_restored)
        self.assertEqual(res_restored["tool"], "claude")
        self.assertEqual(res_restored["model"], "haiku")

    def test_all_exhausted_waiting_for_quota(self):
        now = 1700000000.0
        self.store.set_quota("claude", "haiku", "exhausted", until=now + 1000, last_error="Quota")
        res = fallback.resolve_fallback("agy", "gemini-3.8-flash-low", self.cfg, store=self.store, now=now)
        self.assertTrue(res.get("waiting"))
        self.assertEqual(res.get("earliest_until"), now + 1000)


if __name__ == "__main__":
    unittest.main()
