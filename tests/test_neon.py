"""Gemini 4 Neon and Orchestrator (Neon) on agy (DECISIONS 2026-11-21): complex = Orchestrator (Neon),
heavy = Gemini 4 Neon, Argon kept as the separate-quota spare."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import dispatch, fallback, ingest, router, runtree, usage  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text())

_REAL_WHICH = __import__("shutil").which


def _with_agent_clis(name, *a, **k):
    """shutil.which as on a machine that has both agent CLIs (CI and fresh checkouts have neither)."""
    return f"/usr/bin/{name}" if name in ("claude", "agy") else _REAL_WHICH(name, *a, **k)


class Config(unittest.TestCase):
    def test_tiers(self):
        m = CFG["tools"]["agy"]["models"]
        self.assertEqual(m["complex"], "gemini-pro-orchestrator-neon")
        self.assertEqual(m["heavy"], "gemini-4-neon-high")
        # dispatch for complex / heavy stays on Claude
        self.assertEqual(CFG["dispatch"]["complex"]["tool"], "claude")
        self.assertEqual(CFG["dispatch"]["heavy"]["tool"], "claude")

    def test_effort_levels(self):
        self.assertEqual(router.model_efforts("agy", "gemini-4-neon-high", CFG), ["low", "medium", "high", "xhigh"])
        self.assertEqual(router.model_efforts("agy", "gemini-pro-orchestrator-neon", CFG), [])

    def test_argon_is_the_spare(self):
        self.assertIn("gemini-4-argon-{effort}", CFG["tools"]["agy"]["model_preference"])

    def test_fallback_chains(self):
        eq = CFG["fallback"]["equivalents"]
        self.assertEqual(eq["agy:gemini-pro-orchestrator-neon"], ["claude:sonnet"])
        self.assertEqual(eq["agy:gemini-4-neon-high"], ["claude:sonnet"])
        self.assertEqual(eq["claude:sonnet"][0], "agy:gemini-pro-orchestrator-neon")
        self.assertEqual(eq["claude:opus"][:2], ["claude:sonnet", "agy:gemini-4-neon-high"])
        for chain in eq.values():   # never_fallback_to_opus: no chain names Opus
            self.assertNotIn("claude:opus", chain)


class Routing(unittest.TestCase):
    def test_orchestrator_gets_no_effort(self):
        self.assertEqual(router.fit_effort("agy", "gemini-pro-orchestrator-neon", "xhigh", CFG),
                         ("gemini-pro-orchestrator-neon", ""))

    def test_neon_slug_carries_the_effort(self):
        self.assertEqual(router.fit_effort("agy", "gemini-4-neon-high", "max", CFG), ("gemini-4-neon-xhigh", "xhigh"))
        self.assertEqual(router.fit_effort("agy", "gemini-4-neon-high", "low", CFG), ("gemini-4-neon-low", "low"))

    def test_sonnet_fallback_lands_on_orchestrator(self):
        with mock.patch("shutil.which", _with_agent_clis):   # the switch to agy needs agy installed
            res = fallback.resolve_fallback("claude", "sonnet", CFG)
        self.assertTrue(res)
        self.assertEqual((res["tool"], res["model"]), ("agy", "gemini-pro-orchestrator-neon"))

    def test_level_of_model(self):
        self.assertEqual(fallback.level_of_model("agy", "gemini-pro-orchestrator-neon", CFG), "complex")
        self.assertEqual(fallback.level_of_model("agy", "gemini-4-neon-xhigh", CFG), "heavy")
        self.assertEqual(fallback.level_of_model("agy", "gemini-4-neon-medium", CFG), "complex")

    def test_dispatch_heavy_on_agy(self):
        r = dispatch.route("debug the race condition deadlock in the scheduler", CFG, tool="agy", level="heavy",
                           check_available=False)
        self.assertEqual(r["model"], "gemini-4-neon-xhigh")


class Ingest(unittest.TestCase):
    def test_tier_of_model(self):
        t = lambda m: ingest.tier_of_model(m, CFG)
        self.assertEqual(t("gemini-pro-orchestrator-neon"), "complex")
        self.assertEqual(t("gemini-4-neon-high"), "heavy")
        self.assertEqual(t("gemini-4-neon-xhigh"), "heavy")
        self.assertEqual(t("gemini-4-neon-medium"), "complex")

    def test_chip(self):
        self.assertEqual(runtree.model_chip("agy", "gemini-4-neon-xhigh"), "Neon")
        self.assertEqual(runtree.model_chip("agy", "gemini-pro-orchestrator-neon"), "Orchestrator · Neon")
        self.assertEqual(runtree.model_chip("agy", "gemini-pro-orchestrator-g1-pro"), "Orchestrator · Argon")


class Quota(unittest.TestCase):
    """agy -p /usage lists Neon and the Orchestrator in the shared "Gemini Models" group (checked 2026-11-21)."""
    def data(self, gemini_left, argon_left=50.0):
        g = lambda name, models, left: {"name": name, "models": models,
                                         "buckets": [{"id": "w", "window": "weekly", "label": "Weekly", "remaining_pct": left}]}
        return {"agy": {"quota": {"groups": [
            g("Gemini Models", "Gemini 4 Neon, Gemini 4 Hydrogen, Orchestrator (Neon), Gemini 3.8 Flash", gemini_left),
            g("Gemini 4 Argon", "For paid Pro plans, Argon quota is separate.", argon_left)]}}}

    def test_neon_and_orchestrator_share_the_gemini_bucket(self):
        for slug in ("gemini-4-neon-high", "gemini-pro-orchestrator-neon"):
            self.assertEqual(usage.agy_model_exhausted(self.data(0.0), slug), "Gemini Models", slug)
            self.assertEqual(usage.agy_model_exhausted(self.data(40.0), slug), "", slug)

    def test_argon_still_runs_when_gemini_is_out(self):
        self.assertEqual(usage.agy_model_exhausted(self.data(0.0), "gemini-4-argon-high"), "")

    def test_route_moves_to_argon_when_the_gemini_bucket_is_empty(self):
        with mock.patch("shutil.which", _with_agent_clis):   # agy installed, nothing else forcing a switch
            r = dispatch.route("debug the race condition deadlock in the scheduler", CFG, tool="agy", level="heavy",
                               check_available=True, usage=self.data(0.0))
        self.assertEqual(r["tool"], "agy", r)
        self.assertTrue(r["model"].startswith("gemini-4-argon-"), r)


class Labels(unittest.TestCase):
    def test_model_label(self):
        src = (ROOT / "hub" / "static" / "core.js").read_text()
        start = src.index("const EFFORT = {")
        end = src.index("J.effortLabel =")
        slugs = ["gemini-pro-orchestrator-neon", "gemini-pro-orchestrator-g1-pro", "gemini-4-neon-xhigh", "gemini-4-neon"]
        script = "const J={S:{}};\n%s\nconsole.log(JSON.stringify(%s.map(J.modelLabel)));" % (src[start:end], json.dumps(slugs))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.js"
            p.write_text(script)
            r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=30, cwd=d)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertEqual(json.loads(r.stdout), ["Orchestrator (Neon)", "Orchestrator (Argon)", "Gemini 4 Neon · XHigh", "Gemini 4 Neon"])

    def test_argon_is_shown_as_the_spare(self):
        from hub import dashstate
        self.assertEqual(dashstate.spare_model("agy", CFG),
                         {"model": "gemini-4-argon-high", "family": "gemini-4-argon", "levels": ["low", "medium", "high"]})
        self.assertIsNone(dashstate.spare_model("claude", CFG))
        src = (ROOT / "hub" / "static" / "fleet.js").read_text()
        self.assertIn('"spare"', src)
        self.assertIn('tier === "spare" ? "Spare"', src)

    def test_fleet_explains_neon(self):
        src = (ROOT / "hub" / "static" / "fleet.js").read_text()
        self.assertIn("MODEL_NOTES", src)
        self.assertIn("[/orchestrator.*neon/", src)
        self.assertIn("[/neon/", src)


if __name__ == "__main__":
    unittest.main()
