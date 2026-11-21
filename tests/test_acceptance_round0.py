"""Round-0 acceptance checks: Fable 5.2 registration, mic button fallback."""
import json
import unittest
from pathlib import Path

from hub import usage

ROOT = Path(__file__).resolve().parent.parent


class FableRegistration(unittest.TestCase):
    def test_config_lists_fable(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        self.assertIn("claude-fable-5-2", json.dumps(cfg))   # still registered (efforts, fallback chain)
        self.assertEqual(cfg["planner"]["model"], "opus")     # the owner's call: Opus orchestrates the workflow

    def test_cost_table_has_fable_5_2(self):
        self.assertEqual(usage.price_for("claude-fable-5-2"), (8.0, 40.0, 0.20))
        self.assertEqual(usage.price_for("claude-fable-5-1")[0], 10.0)


class MicButton(unittest.TestCase):
    js = (ROOT / "hub" / "static" / "composer.js").read_text()

    def test_unsupported_browser_shows_disabled_tooltip(self):
        self.assertRegex(self.js, r"window\.SpeechRecognition \|\| window\.webkitSpeechRecognition")
        self.assertIn('b.setAttribute("aria-disabled", "true"); b.title = MIC_OFF', self.js)
        self.assertRegex(self.js, r"if \(MIC_OFF && !localStt\) \{[^}]*\breturn;")  # no listeners attached

    def test_dictation_fills_but_never_auto_runs(self):
        a = self.js.index("const SR =")
        b = self.js.index("function micInit")
        block = self.js[a:b]
        self.assertIn("onresult", block)
        for bad in ("launch(", "requestSubmit", ".submit(", "api("):
            self.assertNotIn(bad, block)


if __name__ == "__main__":
    unittest.main()
