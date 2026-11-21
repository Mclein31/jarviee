"""Tests that /pipeline and review only name agent skills when installed for the target tool."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import pipeline, planner, review
from hub.scheduler import Scheduler
from hub.store import Store
from hub.tasks import Tasks


class SkillInstalledTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.claude_skills = self.tmp / "claude_skills"
        self.claude_commands = self.tmp / "claude_commands"
        self.agy_skills = self.tmp / "agy_skills"
        for d in (self.claude_skills, self.claude_commands, self.agy_skills):
            d.mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {
            "JARVIEE_CLAUDE_SKILL_DIRS": f"{self.claude_skills}{os.pathsep}{self.claude_commands}",
            "JARVIEE_AGY_SKILL_DIRS": str(self.agy_skills),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._t.__exit__()

    def test_claude_and_agy_isolation_via_env(self):
        self.assertFalse(review.skill_installed("researcher", tool="claude"))
        self.assertFalse(review.skill_installed("researcher", tool="agy"))

        (self.claude_skills / "researcher").mkdir()
        (self.claude_commands / "code-reviewer.md").write_text("# review\n", encoding="utf-8")
        self.assertTrue(review.skill_installed("researcher", tool="claude"))
        self.assertTrue(review.skill_installed("code-reviewer", tool="claude"))
        self.assertFalse(review.skill_installed("researcher", tool="agy"))
        self.assertFalse(review.skill_installed("code-reviewer", tool="agy"))

        (self.agy_skills / "coder" / "SKILL.md").parent.mkdir()
        (self.agy_skills / "coder" / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
        self.assertTrue(review.skill_installed("coder", tool="agy"))
        self.assertFalse(review.skill_installed("coder", tool="claude"))
        self.assertFalse(review.skill_installed("coder", tool="echo"))

    def test_empty_env_and_explicit_skill_dirs_param(self):
        (self.claude_skills / "coder").mkdir()
        with mock.patch.dict(os.environ, {"JARVIEE_CLAUDE_SKILL_DIRS": "", "JARVIEE_AGY_SKILL_DIRS": ""}):
            self.assertFalse(review.skill_installed("coder", tool="claude"))
            self.assertFalse(review.skill_installed("coder", tool="agy"))
            self.assertTrue(review.skill_installed("coder", tool="claude", skill_dirs=[self.claude_skills]))
            self.assertFalse(review.skill_installed("coder", tool="agy", skill_dirs=[self.agy_skills]))
            self.assertTrue(review.skill_installed("coder", tool="claude", skill_dirs={"claude": [self.claude_skills]}))

    def test_invalid_skill_names_are_rejected(self):
        for bad in ("", "   ", ".", "..", "../coder", "a/b"):
            self.assertFalse(review.skill_installed(bad, tool="claude", skill_dirs=[self.claude_skills]))


class SkillLineTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.claude_dir = self.tmp / "claude"
        self.agy_dir = self.tmp / "agy"
        self.claude_dir.mkdir()
        self.agy_dir.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "JARVIEE_CLAUDE_SKILL_DIRS": str(self.claude_dir),
            "JARVIEE_AGY_SKILL_DIRS": str(self.agy_dir),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._t.__exit__()

    def test_missing_vs_installed_for_both_tools(self):
        self.assertEqual(pipeline.skill_line({}, "research", tool="claude"), "")
        self.assertEqual(pipeline.skill_line({}, "research", tool="agy"), "")
        self.assertEqual(pipeline.skill_line({}, "build", tool="claude"), "")
        self.assertEqual(pipeline.skill_line({}, "build", tool="agy"), "")

        (self.claude_dir / "researcher").mkdir()
        (self.agy_dir / "coder").mkdir()
        self.assertEqual(pipeline.skill_line({}, "research", tool="claude"), "Use the researcher skill.")
        self.assertEqual(pipeline.skill_line({}, "research", tool="agy"), "")
        self.assertEqual(pipeline.skill_line({}, "build", tool="agy"), "Use the coder skill.")
        self.assertEqual(pipeline.skill_line({}, "build", tool="claude"), "")

    def test_skill_line_uses_configured_phase_tool_when_tool_omitted(self):
        cfg = {
            "dispatch": {"complex": {"tool": "claude"}, "light": {"tool": "agy"}},
            "pipeline": {"planner": "claude", "skills": {"analysis": "analyst", "fact_check": "checker"}},
        }
        (self.claude_dir / "researcher").mkdir()
        (self.claude_dir / "project-manager").mkdir()
        (self.agy_dir / "analyst").mkdir()
        (self.agy_dir / "checker").mkdir()
        (self.agy_dir / "coder").mkdir()

        self.assertEqual(pipeline.skill_line(cfg, "research"), "Use the researcher skill.")
        self.assertEqual(pipeline.skill_line(cfg, "plan"), "Use the project-manager skill.")
        self.assertEqual(pipeline.skill_line(cfg, "analysis"), "Use the analyst skill.")
        self.assertEqual(pipeline.skill_line(cfg, "fact_check"), "Use the checker skill.")
        self.assertEqual(pipeline.skill_line(cfg, "build"), "Use the coder skill.")

        # If complex tasks dispatch to agy instead, claude-only researcher must not be named
        cfg_agy = {"dispatch": {"complex": {"tool": "agy"}, "light": {"tool": "agy"}}}
        self.assertEqual(pipeline.skill_line(cfg_agy, "research"), "")


class PipelinePromptTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.claude_dir = self.tmp / "claude"
        self.agy_dir = self.tmp / "agy"
        self.claude_dir.mkdir()
        self.agy_dir.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "JARVIEE_CLAUDE_SKILL_DIRS": str(self.claude_dir),
            "JARVIEE_AGY_SKILL_DIRS": str(self.agy_dir),
        })
        self._env.start()
        self.cfg, _ = make_cfg(str(self.tmp))
        self.cfg["pipeline"].update(
            enabled=True,
            planner="claude",
            skills={"research": "researcher", "analysis": "analyst", "plan": "project-manager",
                    "fact_check": "checker", "build": "coder", "review": "code-reviewer"},
        )
        self.cfg["pipeline"]["analysis"]["enabled"] = True
        self.cfg["pipeline"]["fact_check"]["enabled"] = True
        self.cfg["pipeline"]["test"]["enabled"] = False
        self.cfg["pipeline"]["review"]["enabled"] = True
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks, interval=0.05)

    def tearDown(self):
        self._env.stop()
        self._t.__exit__()

    def test_phases_omit_missing_skills_and_include_installed_ones_per_tool(self):
        # Install skills ONLY for claude, NOT for agy
        for name in ("researcher", "analyst", "project-manager", "checker", "coder", "code-reviewer"):
            (self.claude_dir / name).mkdir()

        # 1. Research: defaults to claude -> skill present; pinned to agy -> skill omitted
        p_claude = self.sched.pipeline.start("build feature", "Alpha", {"level": "complex"})
        self.assertIn("Use the researcher skill.", p_claude["tasks"][0]["prompt"])

        p_agy = self.sched.pipeline.start("build feature", "Alpha", {"level": "complex"}, tool="agy")
        self.assertEqual(p_agy["tasks"][0]["tool"], "agy")
        self.assertNotIn("Use the researcher skill.", p_agy["tasks"][0]["prompt"])

        # 2. Analysis (level=light -> agy) -> omitted when only in claude
        self.sched.pipeline._queue_analysis(p_claude, "notes")
        analysis_task = next(t for t in self.tasks.plan(p_claude["id"])["tasks"] if t["phase"] == "analysis")
        self.assertEqual(analysis_task["tool"], "agy")
        self.assertNotIn("Use the analyst skill.", analysis_task["prompt"])

        # 3. Fact-check (level=light -> agy) -> omitted when only in claude; present when installed in agy
        fc_missing = self.sched.pipeline._fact_check_task(p_claude, p_claude["pipeline"], 0)
        self.assertEqual(fc_missing["tool"], "agy")
        self.assertNotIn("Use the checker skill.", fc_missing["prompt"])
        (self.agy_dir / "checker").mkdir()
        fc_present = self.sched.pipeline._fact_check_task(p_claude, p_claude["pipeline"], 0)
        self.assertIn("Use the checker skill.", fc_present["prompt"])

        # 4. Plan + Build: planner runs on claude -> project-manager present;
        # build task t1 (light -> agy) omits coder, build task t2 (complex -> claude) includes coder
        seen_asks = []
        plan_json = json.dumps([
            {"id": "t1", "title": "Small tweak", "prompt": "add a tooltip", "level": "light", "files": [],
             "depends_on": [], "acceptance": ""},
            {"id": "t2", "title": "Core API", "prompt": "refactor the API endpoint", "level": "complex", "files": [],
             "depends_on": [], "acceptance": ""},
        ])

        real_make_plan = planner.make_plan

        def fake_make_plan(ask, cfg, project=None, **kw):
            seen_asks.append(ask)
            return real_make_plan("goal", cfg, project, runner=lambda _p: plan_json)

        pid = self.tasks.add_plan("goal", "Alpha", [], planner="pipeline", status="approved",
                                  pipeline={"phase": "planning", "level": "complex", "round": 0, "max_rounds": 1,
                                            "research": "", "analysis": "", "rounds": [], "cwd": "", "tool": ""})
        with mock.patch.object(planner, "make_plan", side_effect=fake_make_plan):
            self.sched.pipeline._plan_worker(pid, "goal", "Alpha", "")
        self.assertIn("Use the project-manager skill.", seen_asks[0])
        built = {t["id"].split(".", 1)[1]: t for t in self.tasks.plan(pid)["tasks"]}
        self.assertEqual(built["t1"]["tool"], "agy")
        self.assertNotIn("Use the coder skill.", built["t1"]["prompt"])
        self.assertEqual(built["t2"]["tool"], "claude")
        self.assertIn("Use the coder skill.", built["t2"]["prompt"])

        # 5. Pinned agy build picks up coder once installed in agy
        (self.agy_dir / "coder").mkdir()
        pid_agy = self.tasks.add_plan("goal", "Alpha", [], planner="pipeline", status="approved",
                                      pipeline={"phase": "planning", "level": "complex", "round": 0, "max_rounds": 1,
                                                "research": "", "analysis": "", "rounds": [], "cwd": "", "tool": "agy"})
        with mock.patch.object(planner, "make_plan", side_effect=fake_make_plan):
            self.sched.pipeline._plan_worker(pid_agy, "goal", "Alpha", "")
        built_agy = self.tasks.plan(pid_agy)["tasks"]
        self.assertTrue(all(t["tool"] == "agy" and "Use the coder skill." in t["prompt"] for t in built_agy))

        # 6. Review prompt respects tool
        plan_obj = {"project": "", "goal": "g", "tasks": []}
        self.assertIn("Use the code-reviewer skill.", review.review_prompt(self.cfg, plan_obj, tool="claude"))
        self.assertNotIn("Use the code-reviewer skill.", review.review_prompt(self.cfg, plan_obj, tool="agy"))

        # 7. Fix task uses the tool that will run the fix
        (self.agy_dir / "coder").rmdir()
        p_fix_agy = self.tasks.add_plan("goal", "Alpha", [], planner="pipeline", status="approved",
                                        pipeline={"phase": "review", "level": "complex", "round": 0, "max_rounds": 2,
                                                  "research": "", "rounds": [], "tool": "agy",
                                                  "_test": {"verdict": "fail", "detail": "boom"}})
        pl = self.tasks.plan(p_fix_agy)
        self.sched.pipeline._decide(pl, pl["pipeline"], "pass", "")
        fix_task = self.tasks.plan(p_fix_agy)["tasks"][0]
        self.assertEqual(fix_task["tool"], "agy")
        self.assertNotIn("Use the coder skill.", fix_task["prompt"])


if __name__ == "__main__":
    unittest.main()
