import json
import os
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import dispatch, planner
from hub.router import assess, normalize
from hub.scheduler import Scheduler, overlap
from hub.store import Store
from hub.tasks import Tasks


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.ws = Path(self.cfg["obsidian"]["workspace_root"])

    def tearDown(self):
        self._t.__exit__()


class LevelsAndDispatch(Base):
    def test_one_example_per_level(self):
        cases = {"basic": "rename getUser to fetchUser",
                 "light": "add a small tooltip component to the header",
                 "complex": "add pagination to the projects API endpoint and update the client",
                 "heavy": "migrate the auth layer to a new session model across the codebase"}
        for level, task in cases.items():
            self.assertEqual(assess(task, self.cfg)["level"], level, task)
        self.assertEqual(normalize("standard"), "light")

    def test_negations_and_long_planner_prompts(self):
        self.assertEqual(assess("fix the login copy but do not refactor anything", self.cfg)["level"], "basic")
        long_prompt = "Fix the typo in the README heading. " + "Keep the wording otherwise identical. " * 12
        self.assertEqual(assess(long_prompt, self.cfg)["level"], "light")              # 80+ words bump
        self.assertEqual(assess(long_prompt, self.cfg, brief=False)["level"], "basic")  # planner check

    def test_dispatch_table(self):
        want = {"basic": ("agy", "gemini-3.8-flash-low"), "light": ("agy", "gemini-4-hydrogen-medium"),
                "complex": ("claude", "sonnet"), "heavy": ("claude", "opus")}
        for level, (tool, model) in want.items():
            r = dispatch.route("do the thing", self.cfg, level=level)
            self.assertEqual((r["tool"], r["model"], r["level"]), (tool, model, level))

    def test_complex_task_dispatches_to_claude_regression(self):
        """Regression test for Bug 6: complex tasks must route to claude, not agy."""
        task = "Approve running python3 hub.py install-hooks (writes ~/.gemini/config/hooks.json and ~/.claude/settings.json, with backups)"
        a = assess(task, self.cfg)
        self.assertEqual(a["level"], "complex")
        # Default config has prefer_agy_on_borderline = False
        r = dispatch.route(task, self.cfg)
        self.assertEqual(r["tool"], "claude")
        self.assertEqual(r["model"], "sonnet")

    def test_borderline_prefers_agy_only_when_enabled(self):
        task = "debug the settings page"           # one multi-part word: complex, borderline
        a = assess(task, self.cfg)
        self.assertEqual((a["level"], a["borderline"]), ("complex", True))
        # By default prefer_agy_on_borderline is False -> routes to claude
        r = dispatch.route(task, self.cfg)
        self.assertEqual((r["tool"], r["model"], r["effort"]), ("claude", "sonnet", "high"))  # "debug" raises effort one step
        # When explicitly enabled, prefer agy
        self.cfg["prefer_agy_on_borderline"] = True
        r2 = dispatch.route(task, self.cfg)
        self.assertEqual((r2["tool"], r2["model"], r2["effort"]), ("agy", "gemini-pro-orchestrator-neon", ""))  # agy complex = Orchestrator (Neon), no effort
        self.cfg["prefer_agy_on_borderline"] = False

    def test_manual_override_wins(self):
        r = dispatch.route("rename x to y", self.cfg, tool="claude")
        self.assertEqual((r["tool"], r["model"], r["level"]), ("claude", "haiku", "basic"))
        r = dispatch.route("rename x to y", self.cfg, level="heavy")
        self.assertEqual((r["tool"], r["level"], r["suggested_level"]), ("claude", "heavy", "basic"))
        self.assertEqual(dispatch.route("debug the settings page", self.cfg, tool="claude")["tool"], "claude")

    def test_fallback_when_binary_missing_or_quota_out(self):
        self.cfg["tools"]["agy"]["command"][0] = "definitely-not-installed-xyz"
        r = dispatch.route("rename x to y", self.cfg)
        self.assertEqual(r["tool"], "claude")
        self.assertIn("not installed", r["fallback"])
        self.cfg, _ = make_cfg(self.tmp + "/b")
        r = dispatch.route("rename x to y", self.cfg, quota={"agy": "quota exceeded"})
        self.assertEqual(r["tool"], "claude")

    def test_escalation_reason(self):
        self.assertIn("code 1", dispatch.escalation_reason("agy", 1, "", self.cfg))
        self.assertIn("blocked", dispatch.escalation_reason("agy", 0, "Sorry, I'm blocked on X", self.cfg))
        self.assertEqual(dispatch.escalation_reason("agy", 0, "all good", self.cfg), "")
        self.assertEqual(dispatch.escalation_reason("claude", 1, "", self.cfg), "")       # already escalated tool
        self.assertEqual(dispatch.escalation_reason("agy", 1, "", self.cfg, attempts_escalated=1), "")

    def test_headless_edit_permissions_and_read_only_strip(self):
        from hub.config import ROOT, load_config
        from hub.launcher import build_argv, launch, wait
        real = load_config(ROOT / "config.json")
        agy_argv = build_argv(real["tools"]["agy"], "do work", "gemini-4-argon", "high")
        claude_argv = build_argv(real["tools"]["claude"], "do work", "sonnet", "medium")
        self.assertIn("--mode", agy_argv)
        self.assertEqual(agy_argv[agy_argv.index("--mode") + 1], "auto")
        self.assertIn("--effort", agy_argv)
        self.assertEqual(agy_argv[agy_argv.index("--effort") + 1], "high")
        self.assertNotIn("--effort", build_argv(real["tools"]["agy"], "x", "gemini-3.8-flash-high", "medium"))   # slug already has an effort
        self.assertIn("--permission-mode", claude_argv)
        self.assertEqual(claude_argv[claude_argv.index("--permission-mode") + 1], "auto")
        # Read-only planner/review calls strip edit-mode flags before adding plan mode
        ro_agy = build_argv(real["tools"]["agy"], "plan", "gemini-4-argon-medium", "", read_only=True)
        ro_claude = build_argv(real["tools"]["claude"], "plan", "sonnet", "", read_only=True)
        self.assertNotIn("--mode", ro_agy)
        self.assertNotIn("--permission-mode", ro_claude)

        # Live step tracking fallback bumps steps from log tool lines without hook events
        store = Store(self.cfg["db_path"])
        rid, p = launch('FAKE_SAY="Running read_file on hub/dispatch.py" debug the settings page',
                        self.cfg, store, project="Alpha", tool="agy", tier="complex")
        wait(rid, 10)
        agent = store.get(rid)
        self.assertEqual(agent["status"], "done")
        self.assertEqual((agent["model"], agent["effort"]), ("gemini-pro-orchestrator-neon", ""))   # agy complex = Orchestrator (Neon); agy sets the effort
        self.assertGreaterEqual(agent["steps"], 1)

    def test_agy_empty_output_falls_back_to_headless_bridge(self):
        import sys
        from hub.config import ROOT, load_config
        from hub.launcher import build_bridge_argv, launch, wait
        real = load_config(ROOT / "config.json")
        self.assertEqual(real["tools"]["agy"]["bridge_command"][0], "agy-headless-bridge")
        # When bridge_command points to an available executable, empty agy -p output triggers bridge fallback
        bridge_script = "import sys; print('fake agy-headless-bridge ran with model', sys.argv[2]); print(sys.argv[1][:120])"
        self.cfg["tools"]["agy"]["bridge_command"] = [
            sys.executable, "-c", bridge_script, "{prompt}", "{model}", "--mode", "auto"
        ]
        ro_bridge = build_bridge_argv(self.cfg["tools"]["agy"], "plan", "gemini-4-argon-medium", "", read_only=True)
        self.assertIsNotNone(ro_bridge)
        self.assertNotIn("--mode", ro_bridge)

        store = Store(self.cfg["db_path"])
        rid, _ = launch("FAKE_SILENT_AGY=1 rename foo to bar", self.cfg, store, project="Alpha")
        wait(rid, 10)
        agent = store.get(rid)
        self.assertEqual(agent["status"], "done")
        self.assertEqual(agent["activity"], "finished (via agy-headless-bridge)")
        self.assertGreaterEqual(agent["steps"], 1)
        log_text = Path(agent["log_path"]).read_text(encoding="utf-8")
        self.assertIn("agy-headless-bridge (fallback: agy -p produced no output on non-TTY stdout)", log_text)
        self.assertIn("fake agy-headless-bridge ran with model gemini-3.8-flash-low", log_text)


class PlannerJSON(Base):
    GOOD = [{"id": "t1", "title": "Fix typos", "prompt": "fix the README typos", "level": "basic",
             "files": ["README.md"], "depends_on": [], "acceptance": "no typos"},
            {"id": "t2", "title": "Toggle", "prompt": "add a small dark mode toggle", "level": "standard",
             "files": ["hub/dashboard.html"], "depends_on": ["t1"], "acceptance": "toggle works"}]

    def test_bad_json_is_retried_once(self):
        answers = ["Sure! Here is the plan:", "```json\n" + json.dumps(self.GOOD) + "\n```"]
        prompts = []
        plan = planner.make_plan("goal", self.cfg, "Alpha", runner=lambda p: prompts.append(p) or answers[len(prompts) - 1])
        self.assertEqual(len(prompts), 2)
        self.assertIn("previous answer was rejected", prompts[1])
        self.assertIn("Alpha is a web app.", prompts[0])                  # .brain context included
        self.assertEqual([t["level"] for t in plan["tasks"]], ["basic", "light"])  # 'standard' accepted
        self.assertEqual(plan["tasks"][1]["depends_on"], ["t1"])

    def test_still_bad_after_retry_raises(self):
        with self.assertRaises(planner.PlanError):
            planner.make_plan("goal", self.cfg, None, runner=lambda p: "[{\"id\": 1}]")

    def test_validation_errors(self):
        bad = [
            ([], "non-empty"), ([{**self.GOOD[0], "level": "huge"}], "level"),
            ([self.GOOD[0], {**self.GOOD[0]}], "unique"),
            ([{**self.GOOD[0], "depends_on": ["nope"]}], "unknown"),
            ([{**self.GOOD[0], "depends_on": ["t2"]}, {**self.GOOD[1], "depends_on": ["t1"]}], "cycle"),
            ([{**self.GOOD[0], "depends_on": None}], "depends_on"),
            ([{**self.GOOD[0], "depends_on": "t2"}], "depends_on"),
            ([{**self.GOOD[0], "depends_on": 123}], "depends_on"),
            ([{**self.GOOD[0], "depends_on": [None]}], "depends_on"),
        ]
        for data, word in bad:
            with self.assertRaises(planner.PlanError) as cm:
                planner.validate(data)
            self.assertIn(word, str(cm.exception))

    def test_depends_on_null_triggers_retry_and_raises_plan_error(self):
        bad = json.dumps([{**self.GOOD[0], "depends_on": None}])
        good = json.dumps(self.GOOD)
        prompts = []
        plan = planner.make_plan("goal", self.cfg, None, runner=lambda p: prompts.append(p) or (bad if len(prompts) == 1 else good))
        self.assertEqual(len(prompts), 2)
        self.assertIn("depends_on", prompts[1])
        self.assertEqual(len(plan["tasks"]), 2)
        with self.assertRaises(planner.PlanError):
            planner.make_plan("goal", self.cfg, None, runner=lambda p: bad)

    def test_router_disagreement_uses_higher(self):
        data = [{**self.GOOD[0], "prompt": "migrate the auth layer across the codebase", "level": "light"}]
        plan = planner.make_plan("goal", self.cfg, None, runner=lambda p: json.dumps(data))
        t = plan["tasks"][0]
        self.assertEqual((t["planner_level"], t["router_level"], t["level"], t["tool"]),
                         ("light", "heavy", "heavy", "claude"))

    def test_borderline_complex_uses_chosen_tool_complex_model(self):
        self.cfg["prefer_agy_on_borderline"] = True
        self.cfg["tools"]["agy"]["models"]["light"] = "gemini-3.8-flash-medium"
        self.cfg["dispatch"]["light"]["model"] = "gemini-3.8-flash-medium"
        data = [
            {**self.GOOD[0], "id": "t1", "prompt": "add a helper module", "level": "complex"},
            {**self.GOOD[0], "id": "t2", "prompt": "debug the settings page", "level": "complex", "depends_on": []},
        ]
        plan = planner.make_plan("goal", self.cfg, None, runner=lambda p: json.dumps(data))
        t1, t2 = plan["tasks"]
        self.assertEqual((t1["tool"], t1["model"], t1["effort"]), ("agy", "gemini-pro-orchestrator-neon", ""))
        self.assertEqual((t2["tool"], t2["model"], t2["effort"]), ("agy", "gemini-pro-orchestrator-neon", ""))

class SchedulerRuns(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks)

    def queue(self, specs, project="Alpha"):
        rows = [{"id": i, "title": f"task {i}", "prompt": prompt, "level": level, "tool": tool,
                 "model": "m-" + tool, "files": files, "depends_on": deps}
                for i, prompt, level, tool, files, deps in specs]
        pid = self.tasks.add_plan("goal", project, rows, planner="test")
        self.tasks.approve(pid)
        return pid

    def drain(self, pid, timeout=30, during=None):
        end = time.time() + timeout
        while time.time() < end:
            self.sched.tick()
            if during:
                during()
            ts = self.tasks.plan(pid)["tasks"]
            if all(t["status"] in ("done", "failed", "cancelled") for t in ts):
                return {t["id"].split(".")[-1]: t for t in ts}
            time.sleep(0.05)
        self.fail("plan did not finish: " + str([(t["id"], t["status"]) for t in self.tasks.plan(pid)["tasks"]]))

    def test_restart_mid_run_settles_orphaned_task(self):
        pid = self.queue([("t1", "add a tooltip", "light", "claude", ["a.py"], [])])
        tid = self.tasks.plan(pid)["tasks"][0]["id"]
        self.tasks.set(tid, status="running", run_id="run-gone", started=time.time())
        self.sched.tick()                       # no run row: requeued, then relaunched
        self.assertNotEqual(self.tasks.get(tid)["run_id"], "run-gone")
        self.drain(pid)
        # a run that finished while the service was down
        pid2 = self.queue([("t1", "add a tooltip", "light", "claude", ["b.py"], [])], project="Beta")
        tid2 = self.tasks.plan(pid2)["tasks"][0]["id"]
        rid = "run-old"
        self.store.upsert(rid, status="done", tool="claude")
        self.tasks.set(tid2, status="running", run_id=rid, started=time.time())
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid2)["status"], "done")

    def test_failed_agy_run_escalates_once_to_claude(self):
        pid = self.queue([("t1", "FAKE_EXIT_AGY=1 add a tooltip", "light", "agy", ["a.py"], [])])
        t = self.drain(pid)["t1"]
        self.assertEqual((t["status"], t["tool"], t["level"], t["escalated"], t["attempts"]),
                         ("done", "claude", "complex", True, 2))
        self.assertIn("previous attempt with agy", t["prompt"])
        runs = (self.ws / "Alpha/.brain/AGENT_RUNS.md").read_text(encoding="utf-8")
        self.assertIn("agy / m-agy (light) · escalated", runs)
        self.assertIn("(complex) · done · escalated", runs)

    def test_silent_agy_run_without_bridge_escalates_to_claude(self):
        pid = self.queue([("t1", "FAKE_SILENT_AGY=1 add a tooltip", "light", "agy", ["a.py"], [])])
        t = self.drain(pid)["t1"]
        self.assertEqual((t["status"], t["tool"], t["level"], t["escalated"], t["attempts"]),
                         ("done", "claude", "complex", True, 2))
        self.assertIn("no output produced", t["route_note"])

    def test_escalation_happens_only_once(self):
        pid = self.queue([("t1", "FAKE_EXIT=1 add a tooltip", "light", "agy", [], [])])
        t = self.drain(pid)["t1"]
        self.assertEqual((t["status"], t["tool"], t["attempts"]), ("failed", "claude", 2))

    def test_claude_quota_exhaustion_falls_back_to_agy(self):
        prompt = 'FAKE_EXIT_CLAUDE=1 FAKE_SAY="You\'ve hit your 5-hour limit" implement feature'
        pid = self.queue([("t1", prompt, "complex", "claude", ["a.py"], [])])
        t = self.drain(pid)["t1"]
        self.assertEqual((t["status"], t["tool"], t["escalated"], t["attempts"]),
                         ("done", "agy", True, 2))
        self.assertIn("5-hour limit", self.sched.quota.get("claude", ""))
        self.assertIn("routed to agy", t["route_note"])

    def test_dependencies_run_in_order(self):
        pid = self.queue([("t1", "FAKE_SLEEP=0.5 first", "basic", "agy", ["a.py"], []),
                          ("t2", "second", "basic", "agy", ["b.py"], ["t1"])])
        t = self.drain(pid)
        self.assertGreaterEqual(t["t2"]["started"], t["t1"]["ended"])

    def test_file_overlap_guard_and_locks(self):
        seen_lock = []
        lockdir = self.ws / "Alpha/.brain/.locks"
        pid = self.queue([("a", "FAKE_SLEEP=1 one", "basic", "agy", ["hub/x.py"], []),
                          ("b", "FAKE_SLEEP=1 two", "basic", "agy", ["hub/x.py"], []),
                          ("c", "FAKE_SLEEP=1 three", "basic", "claude", ["hub/y.py"], [])])
        t = self.drain(pid, during=lambda: seen_lock.extend(p.read_text() for p in lockdir.glob("*.lock")))
        a, b, c = t["a"], t["b"], t["c"]
        first, second = sorted([a, b], key=lambda x: x["started"])
        self.assertGreaterEqual(second["started"], first["ended"])      # shared file: never together
        self.assertLess(c["started"], first["ended"])                    # other lane/file ran in parallel
        self.assertIn("hub/x.py\n", seen_lock)
        self.assertEqual(list(lockdir.glob("*.lock")), [])              # released on finish

    def test_prompt_lists_forbidden_files(self):
        pid = self.queue([("a", "FAKE_SLEEP=1 one", "basic", "agy", ["hub/x.py"], []),
                          ("b", "two", "basic", "claude", ["hub/y.py"], [])])
        self.drain(pid)
        log = Path(self.cfg["logs_dir"])
        texts = [p.read_text() for p in log.glob("*.log")]
        self.assertTrue(any("Do NOT modify" in x and "hub/x.py" in x for x in texts))
        self.assertTrue(all("summarise what changed in 5 lines" in x for x in texts))

    def test_overlap_helper(self):
        self.assertEqual(overlap(["hub/"], ["hub/x.py"]), {"hub/"})
        self.assertEqual(overlap(["a.py"], ["b.py"]), set())

    def test_controls(self):
        pid = self.queue([("t1", "FAKE_EXIT=1 x", "complex", "claude", [], [])])
        self.drain(pid)
        tid = pid + ".t1"
        self.assertEqual(self.sched.reroute(tid, "agy")["tool"], "agy")
        self.sched.pause()
        self.sched.tick()
        self.assertEqual(self.tasks.get(tid)["status"], "queued")       # paused: nothing starts
        self.sched.resume()
        with self.assertRaises(ValueError):
            self.sched.retry(tid)                                          # queued, not failed


if __name__ == "__main__":
    unittest.main()
