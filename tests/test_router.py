import copy
import time
import unittest

from helpers import ROOT  # noqa: F401
from hub.config import load_config
from hub.router import assess, classify

BORDER = "add an endpoint"


class LlmFallbackTests(unittest.TestCase):
    base = load_config(ROOT / "config.json")

    def cfg(self, enabled=True, timeout=0.2):
        c = copy.deepcopy(self.base)
        c["classifier"]["llm_fallback"] = {"enabled": enabled, "timeout_s": timeout}
        return c

    def test_default_off(self):
        self.assertFalse(self.base["classifier"]["llm_fallback"]["enabled"])

    def test_precondition_borderline(self):
        a = assess(BORDER, self.cfg(False))
        self.assertTrue(a["borderline"], a)

    def test_flag_off_ignores_llm(self):
        calls = []
        a = assess(BORDER, self.cfg(False), llm=lambda *x: calls.append(x) or "light")
        self.assertEqual(a["level"], assess(BORDER, self.cfg(False))["level"])
        self.assertEqual(calls, [])

    def test_borderline_uses_llm(self):
        a = assess(BORDER, self.cfg(), llm=lambda t, lvl, to: "light")
        self.assertEqual(a["level"], "light")
        self.assertIn("llm fallback", " ".join(a["reasons"]))
        self.assertEqual(classify(BORDER, self.cfg(), llm=lambda *x: "heavy")[0], "heavy")

    def test_not_borderline_skips_llm(self):
        calls = []
        a = assess("rename getUser to fetchUser", self.cfg(), llm=lambda *x: calls.append(1) or "heavy")
        self.assertEqual(a["level"], "basic")
        self.assertEqual(calls, [])

    def test_bad_answers_fall_back(self):
        want = assess(BORDER, self.cfg(False))["level"]

        def boom(*a):
            raise RuntimeError("no network")
        for fn in (boom, lambda *a: "banana", lambda *a: None):
            self.assertEqual(assess(BORDER, self.cfg(), llm=fn)["level"], want)

    def test_timeout_falls_back(self):
        want = assess(BORDER, self.cfg(False))["level"]
        t0 = time.time()
        a = assess(BORDER, self.cfg(timeout=0.1), llm=lambda *x: time.sleep(1) or "heavy")
        self.assertEqual(a["level"], want)
        self.assertLess(time.time() - t0, 0.8)


if __name__ == "__main__":
    unittest.main()
