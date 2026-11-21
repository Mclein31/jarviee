"""Acceptance checks for the Gemini 4 Hydrogen routing work."""
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from hub import ingest, rundemo, runtree, usage
from hub.router import classify, pick_model

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "hub" / "static"
CFG = json.loads((ROOT / "config.json").read_text())


def _slice(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def run_node(script):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.js"
        p.write_text(script)
        r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=30, cwd=d)
    assert r.returncode == 0, r.stderr[-600:]
    return json.loads(r.stdout)


class ConfigRouting(unittest.TestCase):
    def test_agy_tiers(self):
        m = CFG["tools"]["agy"]["models"]
        self.assertEqual(m["light"], "gemini-4-hydrogen-medium")
        self.assertEqual(m["complex"], "gemini-pro-orchestrator-neon")   # 2026-11-21: see test_neon.py
        self.assertEqual(m["basic"], "gemini-3.8-flash-low")
        self.assertEqual(CFG["dispatch"]["light"], {"tool": "agy", "model": "gemini-4-hydrogen-medium"})
        self.assertEqual(CFG["tools"]["agy"]["model_preference"][0], "gemini-4-hydrogen-{effort}")

    def test_light_task_routes_to_hydrogen(self):
        level, _ = classify("add a small helper function to utils.py", CFG)
        self.assertEqual(level, "light")
        self.assertEqual(pick_model("agy", level, CFG), "gemini-4-hydrogen-medium")

    def test_fallback_equivalents(self):
        eq = CFG["fallback"]["equivalents"]
        self.assertEqual(eq["agy:gemini-4-hydrogen-medium"], ["claude:sonnet"])
        self.assertEqual(eq["agy:gemini-4-hydrogen-high"], ["claude:sonnet"])
        self.assertIn("agy:gemini-4-hydrogen-medium", eq["claude:sonnet"])


class TierOfModel(unittest.TestCase):
    def test_hydrogen(self):
        self.assertEqual(ingest.tier_of_model("gemini-4-hydrogen-medium", CFG), "light")
        self.assertEqual(ingest.tier_of_model("gemini-4-hydrogen-high", CFG), "complex")

    def test_others_unchanged(self):
        t = lambda m: ingest.tier_of_model(m, CFG)
        self.assertEqual(t("gemini-4-argon-high"), "complex")   # no longer a tier slug since Neon took heavy (2026-11-21)
        self.assertEqual(t("gemini-4-argon-medium"), "light")
        self.assertEqual(t("gemini-3.8-flash-low"), "basic")
        self.assertEqual(t("gemini-3.8-flash-medium"), "light")
        self.assertEqual(t("sonnet"), ingest.tier_of_model("sonnet", CFG))
        self.assertEqual(t("claude-opus-5-5-high"), "heavy")


class Chip(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(runtree.model_chip("agy", "gemini-4-hydrogen-medium"), "Hydrogen")
        self.assertEqual(runtree.model_chip("agy", "gemini-4-argon-high"), "Argon")
        self.assertEqual(runtree.model_chip("agy", "gemini-3.8-flash-low"), "3.8 Flash")
        self.assertEqual(runtree.model_chip("claude", "opus"), "Opus")
        self.assertEqual(runtree.model_chip("agy", "sonnet"), "Sonnet · agy")


def _q(name, models, remaining):
    return {"name": name, "models": models, "buckets": [{"window": "5h", "label": "5h", "remaining_pct": remaining}]}


class QuotaGroup(unittest.TestCase):
    def data(self, hydro_left, argon_left=50.0, flash_left=50.0):
        return {"agy": {"quota": {"groups": [
            _q("Gemini Models", "Gemini 4 Hydrogen, Gemini 3.8 Flash", hydro_left),
            _q("Gemini 4 Argon", "Gemini 4 Argon", argon_left),
            _q("Claude and GPT", "Claude Opus 5.5, GPT-OSS", 80.0)]}}}

    def test_hydrogen_returns_group_only_when_zero(self):
        self.assertEqual(usage.agy_model_exhausted(self.data(0.0), "gemini-4-hydrogen-medium"), "Gemini Models")
        self.assertEqual(usage.agy_model_exhausted(self.data(30.0), "gemini-4-hydrogen-medium"), "")

    def test_hydrogen_unaffected_by_other_groups_at_zero(self):
        self.assertEqual(usage.agy_model_exhausted(self.data(30.0, argon_left=0.0), "gemini-4-hydrogen-high"), "")

    def test_argon_and_flash_unchanged(self):
        self.assertEqual(usage.agy_model_exhausted(self.data(30.0, argon_left=0.0), "gemini-4-argon-high"), "Gemini 4 Argon")
        self.assertEqual(usage.agy_model_exhausted(self.data(30.0), "gemini-4-argon-high"), "")
        self.assertEqual(usage.agy_model_exhausted(self.data(0.0), "gemini-3.8-flash-low"), "Gemini Models")
        self.assertEqual(usage.agy_model_exhausted(self.data(30.0, argon_left=0.0), "gemini-3.8-flash-low"), "")

    def test_own_group_named_hydrogen(self):
        d = {"agy": {"quota": {"groups": [_q("Gemini 4 Hydrogen", "", 0.0), _q("Gemini 3.8 Flash", "", 90.0)]}}}
        self.assertEqual(usage.agy_model_exhausted(d, "gemini-4-hydrogen-medium"), "Gemini 4 Hydrogen")
        self.assertEqual(usage.agy_model_exhausted(d, "gemini-3.8-flash-low"), "")

    def test_missing_data(self):
        self.assertEqual(usage.agy_model_exhausted(None, "gemini-4-hydrogen-medium"), "")
        self.assertEqual(usage.agy_model_exhausted({}, "gemini-4-hydrogen-medium"), "")

    def test_real_agy_usage_payload_windows_and_exhaustion(self):
        payload = {"command": {"name": "usage", "data": {"groups": [
            {"name": "Gemini Models", "description": "Models within this group: Gemini 4 Hydrogen, Gemini 3.8 Flash",
             "buckets": [
                 {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.58, "reset_time": "2026-11-21T05:33:49Z"},
                 {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.76, "reset_time": "2026-11-16T18:32:46Z"},
             ]},
            {"name": "Claude and GPT models", "description": "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
             "buckets": [
                 {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.0, "reset_time": "2026-11-23T05:48:24Z"},
                 {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.0, "reset_time": "2026-11-16T19:08:00Z"},
             ]},
            {"name": "Gemini 4 Argon", "description": "For paid Pro plans, Argon quota is separate. Higher Argon quota (included as part of the larger Gemini quota bucket) is available on Ultra plans.",
             "buckets": [
                 {"id": "gemini-argon-wtu-weekly", "window": "weekly", "remaining_fraction": 0.0, "reset_time": "2026-11-23T10:05:51Z"},
             ]},
        ]}}}
        groups = usage._parse_agy_usage(json.dumps(payload))
        self.assertEqual([b["window"] for b in groups[0]["buckets"]], ["weekly", "5h"])
        self.assertEqual(groups[0]["models"], "Gemini 4 Hydrogen, Gemini 3.8 Flash")
        data = {"agy": {"quota": {"groups": groups}}}
        self.assertEqual(usage.agy_model_exhausted(data, "gemini-4-hydrogen-medium"), "")
        self.assertEqual(usage.agy_model_exhausted(data, "gemini-4-argon-high"), "Gemini 4 Argon")
        for idx in (0, 1):
            exhausted = json.loads(json.dumps(payload))
            exhausted["command"]["data"]["groups"][0]["buckets"][idx]["remaining_fraction"] = 0.0
            edata = {"agy": {"quota": {"groups": usage._parse_agy_usage(json.dumps(exhausted))}}}
            self.assertEqual(usage.agy_model_exhausted(edata, "gemini-4-hydrogen-medium"), "Gemini Models")
            self.assertEqual(usage.agy_model_exhausted(edata, "gemini-4-hydrogen-high"), "Gemini Models")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardJs(unittest.TestCase):
    def bucket_out(self, groups, slugs):
        fleet = (STATIC / "fleet.js").read_text()
        fn = _slice(fleet, "function bucketOut", "function model(")
        script = ("const J={U:{agy:{quota:{groups:%s}}},now:()=>1000};\n%s\n"
                  "console.log(JSON.stringify(%s.map(s=>{const r=bucketOut('agy',s);return r&&r.reason})));"
                  % (json.dumps(groups), fn, json.dumps(slugs)))
        return run_node(script)

    @staticmethod
    def grp(name, models, left):
        return {"name": name, "models": models, "buckets": [{"label": "5h", "remaining_pct": left, "resets_at": 2000}]}

    SLUGS = ["gemini-4-hydrogen-medium", "gemini-4-argon-high", "gemini-3.8-flash-low", "claude-opus-5-5-high", "gpt-oss-120b"]

    def test_hydrogen_matches_own_group_not_flash(self):
        g = [self.grp("Gemini 3.8 Flash", "", 0), self.grp("Gemini 4 Hydrogen", "", 90),
             self.grp("Gemini 4 Argon", "", 90), self.grp("Claude and GPT", "", 90)]
        r = self.bucket_out(g, self.SLUGS)
        self.assertIsNone(r[0])             # Flash at 0 must not take Hydrogen out
        self.assertIn("Gemini 3.8 Flash", r[2])
        g[1]["buckets"][0]["remaining_pct"] = 0
        g[0]["buckets"][0]["remaining_pct"] = 90
        r = self.bucket_out(g, self.SLUGS)
        self.assertIn("Gemini 4 Hydrogen", r[0])
        self.assertIsNone(r[2])
        self.assertIsNone(r[1])

    def test_argon_claude_gpt_groups_unchanged(self):
        g = [self.grp("Gemini 3.8 Flash", "", 90), self.grp("Gemini 4 Hydrogen", "", 90),
             self.grp("Gemini 4 Argon", "", 0), self.grp("Claude and GPT", "", 0)]
        r = self.bucket_out(g, self.SLUGS)
        self.assertIn("Gemini 4 Argon", r[1])
        self.assertIn("Claude and GPT", r[3])
        self.assertIn("Claude and GPT", r[4])
        self.assertIsNone(r[0])
        self.assertIsNone(r[2])

    def test_hydrogen_in_shared_gemini_group(self):
        g = [self.grp("Gemini Models", "Gemini 4 Hydrogen, Gemini 3.8 Flash", 0), self.grp("Gemini 4 Argon", "", 90)]
        r = self.bucket_out(g, self.SLUGS)
        self.assertIn("Gemini Models", r[0])
        self.assertIn("Gemini Models", r[2])
        self.assertIsNone(r[1])

    def test_command_center_strip_keeps_hydrogen_separate(self):
        src = (STATIC / "command.js").read_text()
        self.assertIn("isHydro", src)
        self.assertIn('["Hydrogen"', src)
        strip_logic = _slice(src, "const gs =", "const seg =")
        shared = [
            {"name": "Claude and GPT models", "models": "Claude Opus 5.5",
             "buckets": [{"window": "5h", "remaining_pct": 100}, {"window": "weekly", "remaining_pct": 100}]},
            {"name": "Gemini Models", "models": "Gemini 4 Hydrogen, Gemini 3.8 Flash",
             "buckets": [{"window": "5h", "remaining_pct": 75.8}, {"window": "weekly", "remaining_pct": 57.7}]},
            {"name": "Gemini 4 Argon", "models": "For paid Pro plans",
             "buckets": [{"window": "weekly", "remaining_pct": 4.4}]},
        ]
        separate = [
            {"name": "Gemini 3.8 Flash", "models": "", "buckets": [{"window": "5h", "remaining_pct": 90}]},
            {"name": "Gemini 4 Hydrogen", "models": "", "buckets": [{"window": "5h", "remaining_pct": 40}, {"window": "weekly", "remaining_pct": 60}]},
            {"name": "Gemini 4 Argon", "models": "", "buckets": [{"window": "weekly", "remaining_pct": 10}]},
        ]
        script = (
            "function pick(groups){\n  const c={}, a={quota:{groups}};\n  %s\n  return rows;\n}\n"
            "console.log(JSON.stringify({shared: pick(%s), separate: pick(%s)}));"
            % (strip_logic, json.dumps(shared), json.dumps(separate))
        )
        out = run_node(script)
        s_rows = dict(out["shared"])
        self.assertEqual([r[0] for r in out["shared"]], ["Claude", "agy", "Argon"])
        self.assertEqual((s_rows["agy"]["5h"]["remaining_pct"], s_rows["agy"]["wk"]["remaining_pct"]), (75.8, 57.7))
        self.assertEqual((s_rows["Argon"].get("5h"), s_rows["Argon"]["wk"]["remaining_pct"]), (None, 4.4))
        sep_rows = dict(out["separate"])
        self.assertEqual([r[0] for r in out["separate"]], ["Claude", "agy", "Argon", "Hydrogen"])
        self.assertEqual((sep_rows["Hydrogen"]["5h"]["remaining_pct"], sep_rows["Hydrogen"]["wk"]["remaining_pct"]), (40, 60))

    def test_model_label(self):
        core = (STATIC / "core.js").read_text()
        body = "const EFFORT" + _slice(core, "const EFFORT", "J.effortLabel")[len("const EFFORT"):]
        slugs = ["gemini-4-hydrogen-medium", "gemini-4-hydrogen-high", "gemini-4-argon-high", "gemini-3.8-flash-low",
                 "claude-opus-5-5-high", "sonnet"]
        script = "const J={S:{}};\n%s\nconsole.log(JSON.stringify(%s.map(J.modelLabel)));" % (body, json.dumps(slugs))
        r = run_node(script)
        self.assertEqual(r[0], "Gemini 4 Hydrogen · Medium")
        self.assertEqual(r[1], "Gemini 4 Hydrogen · High")
        self.assertEqual(r[2:], ["Gemini 4 Argon · High", "Gemini 3.8 Flash · Low", "Claude Opus 5.5 · High", "Sonnet"])


class DemoData(unittest.TestCase):
    def test_one_hydrogen_run(self):
        d = rundemo.data(time.time())
        runs = [r["model"] for r in d["runs"].values()]
        self.assertEqual(runs.count("gemini-4-hydrogen-medium"), 1, f"runs use {runs}")


class Docs(unittest.TestCase):
    def test_readme_tables_match_config(self):
        readme = (ROOT / "README.md").read_text()
        m = CFG["tools"]["agy"]["models"]
        for tier in ("basic", "light", "complex", "heavy"):
            self.assertIn(f"| {tier} | `{m[tier]}` |", readme)
        self.assertIn("| light | small single-file feature or fix, docs, simple tests | agy · `gemini-4-hydrogen-medium` |", readme)


if __name__ == "__main__":
    unittest.main()
