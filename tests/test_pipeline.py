import json
import os
import sqlite3
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import launcher, pipeline, planner, queue_api
from hub.scheduler import Scheduler
from hub.server import make_server
from hub.store import Store
from hub.tasks import Tasks

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

# A fake agent that prints its argv tail and answers the test/review prompts with a verdict:
# PASS_FROM=<n> in the goal makes round n and later pass; BUILD_FAIL / RESEARCH_FAIL make those runs exit 1.
FAKE = r"""
import sys, re, time, os
p = sys.argv[2]
time.sleep(0.1)
print('fake', sys.argv[1], 'ARGV', sys.argv[4:])
print('CWD', os.getcwd())
print(p[:300])
is_test = 'You are the test agent' in p
is_fact_check = 'You are the fact-checker' in p
is_review = 'This is a read-only review' in p
is_research = p.startswith('Research this goal')
is_analysis = p.startswith('Analyze the goal')
if is_test or is_fact_check or is_review:
    r = int(re.search(r'(?:test|fact-check|Review) round (\d+)', p).group(1))
    m = re.search(r'PASS_FROM=(\d+)', p)
    print('VERDICT: PASS' if r >= int(m[1] if m else 0) else 'VERDICT: FAIL boom at round %d' % r)
if is_review and 'REVIEW_FAIL' in p:
    sys.exit(1)
once = re.search(r'REVIEW_ONCE=(\S+)', p)
if is_review and once and not os.path.exists(once[1]):
    open(once[1], 'w').close()
    sys.exit(1)
if is_research and 'RESEARCH_FAIL' in p:
    sys.exit(1)
if 'BUILD_FAIL' in p and not (is_test or is_review or is_research):
    sys.exit(1)
"""


def crafted(tasks):
    """A planner.make_plan stand-in returning fixed tasks (routed to agy)."""
    rows = [{"id": i, "title": i, "prompt": pr, "level": "light", "files": [], "depends_on": deps,
             "acceptance": f"{i} works", "planner_level": "light", "router_level": "light", "tool": "agy",
             "model": "m", "effort": "", "route_note": ""} for i, pr, deps in tasks]
    return lambda *a, **k: {"goal": "g", "project": "Alpha", "planner": "crafted", "notes": [], "tasks": rows}


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self._skill_env = mock.patch.dict(os.environ, {"JARVIEE_CLAUDE_SKILL_DIRS": "", "JARVIEE_AGY_SKILL_DIRS": ""})
        self._skill_env.start()
        self.addCleanup(self._skill_env.stop)
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["pipeline"].update(enabled=True, planner="mock")
        self.cfg["pipeline"]["analysis"]["enabled"] = False
        self.cfg["pipeline"]["fact_check"]["enabled"] = False
        self.cfg["pipeline"]["test"]["enabled"] = True     # the fix-round tests need test and review on
        self.cfg["pipeline"]["review"]["enabled"] = True
        for tool in ("agy", "claude"):
            self.cfg["tools"][tool]["command"] = [sys.executable, "-c", FAKE, tool, "{prompt}", "{model}"]
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks, interval=0.05)

    def tearDown(self):
        self._t.__exit__()

    def run_pipeline(self, goal, timeout=40, project="Alpha"):
        pid = self.sched.pipeline.start(goal, project, {"level": "complex"})["id"]
        return self.drive(pid, timeout)

    def drive(self, pid, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            self.sched.tick()
            plan = self.tasks.plan(pid)
            if plan["status"] in ("done", "failed", "cancelled"):
                time.sleep(0.3)   # let finished-callbacks settle
                return self.tasks.plan(pid)
            time.sleep(0.05)
        self.fail(f"pipeline stuck: {self.tasks.plan(pid)['pipeline']}")

    @staticmethod
    def ids(plan):
        return [t["id"].split(".", 1)[1] for t in plan["tasks"]]

    def task(self, plan, name):
        return next(t for t in plan["tasks"] if t["id"].endswith("." + name))

    def log(self, plan, name):
        return Path(self.store.get(self.task(plan, name)["run_id"])["log_path"]).read_text()


class VerdictAndConfig(unittest.TestCase):
    def test_parse_verdict(self):
        self.assertEqual(pipeline.parse_verdict("ok\nVERDICT: PASS\n"), ("pass", ""))
        v, d = pipeline.parse_verdict("x\nVERDICT: FAIL\n- test_a broke\n- test_b broke")
        self.assertEqual(v, "fail")
        self.assertIn("test_a broke", d)
        self.assertEqual(pipeline.parse_verdict("no verdict here"), ("unknown", ""))
        self.assertEqual(pipeline.parse_verdict("**VERDICT: PASS**")[0], "pass")
        self.assertEqual(pipeline.parse_verdict("  verdict: fail extra")[0], "fail")
        # last one wins
        self.assertEqual(pipeline.parse_verdict("VERDICT: FAIL\nfixed\nVERDICT: PASS")[0], "pass")

    def test_prompt_echo_with_inline_tokens_is_not_a_verdict(self):
        echo = "prompt: " + pipeline.VERDICT_HINT + "\nmore text"
        self.assertEqual(pipeline.parse_verdict(echo), ("unknown", ""))

    def test_applies_only_when_asked(self):
        cfg = {"pipeline": {"enabled": True}}
        self.assertFalse(pipeline.applies(cfg, {"level": "heavy"}, {"task": "rework the scheduler"}))   # level alone: direct
        self.assertTrue(pipeline.applies(cfg, {"level": "light"}, {"task": "/pipeline rework the scheduler"}))
        self.assertTrue(pipeline.applies(cfg, None, {"task": "x", "pipeline": True}))
        self.assertTrue(pipeline.applies({}, None, {}, "  /PIPELINE: x"))
        self.assertFalse(pipeline.applies(cfg, None, {"task": "/pipeline x", "single": True}))
        self.assertFalse(pipeline.applies({"pipeline": {"enabled": False}}, None, {"task": "/pipeline x"}))
        self.assertFalse(pipeline.applies(cfg, None, {"task": "/pipelines x"}))
        self.assertFalse(pipeline.applies(cfg, None, {"task": "run /pipeline later"}))

    def test_requested_strips_the_prefix(self):
        self.assertEqual(pipeline.requested("/pipeline  fix the login flow"), (True, "fix the login flow"))
        self.assertEqual(pipeline.requested("fix it"), (False, "fix it"))
        self.assertEqual(pipeline.requested("fix it", {"pipeline": True}), (True, "fix it"))

    def test_default_phases_are_researcher_planner_coder(self):
        self.assertEqual(pipeline.phase_names({}), ["research", "plan", "build"])
        with TempDir() as tmp, mock.patch.dict(os.environ, {"JARVIEE_CLAUDE_SKILL_DIRS": "", "JARVIEE_AGY_SKILL_DIRS": ""}):
            self.assertEqual(pipeline.skill_line({}, "research"), "")
            self.assertEqual(pipeline.skill_line({}, "plan"), "")
            self.assertEqual(pipeline.skill_line({}, "build"), "")
            claude_dir, agy_dir = Path(tmp) / "claude", Path(tmp) / "agy"
            for name in ("researcher", "project-manager"):
                (claude_dir / name).mkdir(parents=True)
            (agy_dir / "coder").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"JARVIEE_CLAUDE_SKILL_DIRS": str(claude_dir),
                                              "JARVIEE_AGY_SKILL_DIRS": str(agy_dir)}):
                self.assertEqual(pipeline.skill_line({}, "research"), "Use the researcher skill.")
                self.assertEqual(pipeline.skill_line({}, "plan"), "Use the project-manager skill.")
                self.assertEqual(pipeline.skill_line({}, "build"), "Use the coder skill.")
                self.assertEqual(pipeline.skill_line({"pipeline": {"skills": {"build": ""}}}, "build"), "")


class Migration(unittest.TestCase):
    def test_old_database_gets_the_new_columns(self):
        with TempDir() as tmp:
            store = Store(str(Path(tmp) / "hub.db"))
            store.db.executescript("""
            CREATE TABLE plans (id TEXT PRIMARY KEY, goal TEXT, project TEXT, status TEXT, planner TEXT,
              notes TEXT, review TEXT, created REAL, updated REAL);
            CREATE TABLE tasks (id TEXT PRIMARY KEY, plan_id TEXT, project TEXT, title TEXT, prompt TEXT,
              acceptance TEXT, level TEXT, planner_level TEXT, router_level TEXT, tool TEXT, model TEXT,
              effort TEXT, status TEXT, depends_on TEXT, files TEXT, attempts INTEGER DEFAULT 0,
              escalated INTEGER DEFAULT 0, route_note TEXT, run_id TEXT, summary TEXT,
              created REAL, updated REAL, started REAL, ended REAL);
            INSERT INTO plans VALUES ('plan-old','g','Alpha','done','claude','[]','',1,1);
            INSERT INTO tasks (id, plan_id, title, prompt, level, tool, model, status, depends_on, files)
              VALUES ('plan-old.t1','plan-old','t','p','light','agy','m','done','[]','[]');""")
            store.db.commit()
            tasks = Tasks(store)
            Tasks(store)    # idempotent
            plan = tasks.plan("plan-old")
            self.assertIsNone(plan["pipeline"])
            self.assertEqual((plan["tasks"][0]["phase"], plan["tasks"][0]["read_only"]), ("", False))
            pid = tasks.add_plan("g", "", [], planner="pipeline", status="approved", pipeline={"phase": "research"})
            tasks.add_tasks(pid, "", [{"id": "t1", "title": "x", "prompt": "p", "level": "light", "tool": "agy",
                                       "model": "m", "phase": "build", "read_only": True, "depends_on": []},
                                      {"id": "t2", "title": "y", "prompt": "p", "level": "light", "tool": "agy",
                                       "model": "m", "depends_on": ["t1"]}])
            plan = tasks.plan(pid)
            self.assertEqual(plan["pipeline"]["phase"], "research")
            t1, t2 = plan["tasks"]
            self.assertTrue(t1["read_only"] and t1["phase"] == "build" and t1["status"] == "queued")
            self.assertEqual(t2["depends_on"], [f"{pid}.t1"])


class Phases(Base):
    def test_pass_path_reaches_done(self):
        with mock.patch.object(launcher, "auto_commit") as commit:
            plan = self.run_pipeline("add pagination to the list and update the client")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(self.ids(plan), ["research", "t1", "t2", "test-0", "review-0"])
        self.assertEqual([t["phase"] for t in plan["tasks"]], ["research", "build", "build", "test", "review"])
        self.assertTrue(all(t["status"] == "done" for t in plan["tasks"]))
        pl = plan["pipeline"]
        self.assertEqual((pl["phase"], pl["round"]), ("done", 0))
        self.assertEqual(pl["rounds"], [{"round": 0, "test": "pass", "review": "pass", "note": ""}])
        self.assertIn("VERDICT: PASS", plan["review"])
        self.assertIn("Research notes", self.task(plan, "t1")["prompt"])
        # read-only runs get plan-mode argv and never auto-commit; writable ones commit
        for name in ("research", "review-0"):
            self.assertTrue(self.task(plan, name)["read_only"])
            self.assertIn("'--permission-mode', 'plan'", self.log(plan, name))
        self.assertTrue(self.log(plan, "t1").startswith("$"))
        self.assertIn("ARGV []", self.log(plan, "t1").split("Research notes")[0])
        committed = [c.args[1] for c in commit.call_args_list]
        self.assertEqual(sorted(committed), sorted(t["title"] for t in plan["tasks"] if not t["read_only"]))

    def test_fail_then_fix_round_then_pass(self):
        plan = self.run_pipeline("add pagination PASS_FROM=1")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(self.ids(plan)[-5:], ["test-0", "review-0", "fix-1", "test-1", "review-1"])
        fix = self.task(plan, "fix-1")
        self.assertEqual(fix["phase"], "fix")
        self.assertIn("boom at round 0", fix["prompt"])
        self.assertIn("Fix only the problems", fix["prompt"])
        rounds = plan["pipeline"]["rounds"]
        self.assertEqual([(r["round"], r["test"], r["review"]) for r in rounds], [(0, "fail", "fail"), (1, "pass", "pass")])
        self.assertEqual(plan["pipeline"]["round"], 1)

    def test_still_failing_after_max_rounds_fails(self):
        self.cfg["pipeline"]["max_fix_rounds"] = 1
        plan = self.run_pipeline("add pagination PASS_FROM=99")
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["pipeline"]["phase"], "failed")
        self.assertIn("still failing after 1 fix rounds", plan["pipeline"]["note"])
        self.assertEqual(len(plan["pipeline"]["rounds"]), 2)
        self.assertNotIn("fix-2", self.ids(plan))

    def test_failed_research_still_plans(self):
        plan = self.run_pipeline("add pagination RESEARCH_FAIL")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(self.task(plan, "research")["status"], "failed")
        self.assertEqual(plan["pipeline"]["research"], "")
        self.assertNotIn("Research notes", self.task(plan, "t1")["prompt"])

    def test_disabled_phases_are_skipped(self):
        self.cfg["pipeline"]["research"]["enabled"] = False
        self.cfg["pipeline"]["test"]["enabled"] = False
        self.cfg["pipeline"]["review"]["enabled"] = False
        plan = self.run_pipeline("add pagination to the list")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(self.ids(plan), ["t1"])
        self.assertEqual(plan["pipeline"]["rounds"][0]["test"], "skipped")
        self.assertEqual(plan["pipeline"]["rounds"][0]["review"], "skipped")

    def test_planner_error_fails_the_pipeline(self):
        with mock.patch.object(planner, "make_plan", side_effect=planner.PlanError("bad json")):
            plan = self.run_pipeline("add pagination")
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["pipeline"]["phase"], "failed")
        self.assertIn("bad json", plan["pipeline"]["note"])

    def test_cancel_during_planning_adds_no_tasks(self):
        self.cfg["pipeline"]["research"]["enabled"] = False
        gate, entered = threading.Event(), threading.Event()

        def slow(*a, **k):
            entered.set()
            gate.wait(5)
            return crafted([("t1", "do it", [])])()
        with mock.patch.object(planner, "make_plan", side_effect=slow):
            pid = self.sched.pipeline.start("goal", "Alpha", {"level": "complex"})["id"]
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.tasks.plan(pid)["pipeline"]["phase"], "planning")
            queue_api.post("/api/plan/cancel", {"id": pid}, self.cfg, self.tasks, self.sched)
            gate.set()
            for th in threading.enumerate():
                if th.name == f"jarviee-pipeline-{pid}":
                    th.join(5)
        plan = self.tasks.plan(pid)
        self.assertEqual((plan["status"], plan["tasks"]), ("cancelled", []))

    def test_no_build_task_done_fails(self):
        with mock.patch.object(planner, "make_plan", crafted([("t1", "BUILD_FAIL one", [])])):
            plan = self.run_pipeline("goal")
        self.assertEqual(plan["status"], "failed")
        self.assertIn("no build task finished", plan["pipeline"]["note"])
        self.assertNotIn("test-0", self.ids(plan))

    def test_blocked_dependents_are_cancelled_and_the_rest_continues(self):
        specs = [("t1", "BUILD_FAIL one", []), ("t2", "two", ["t1"]), ("t3", "three", [])]
        with mock.patch.object(planner, "make_plan", crafted(specs)):
            plan = self.run_pipeline("goal")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(self.task(plan, "t1")["status"], "failed")
        t2 = self.task(plan, "t2")
        self.assertEqual((t2["status"], t2["route_note"]), ("cancelled", "skipped: dependency failed"))
        self.assertEqual(self.task(plan, "t3")["status"], "done")
        self.assertIn("test-0", self.ids(plan))

    def test_planner_id_collisions_get_a_suffix(self):
        with mock.patch.object(planner, "make_plan", crafted([("research", "one", []), ("t2", "two", ["research"])])):
            plan = self.run_pipeline("goal")
        self.assertIn("research-2", self.ids(plan))
        self.assertEqual(self.task(plan, "t2")["depends_on"], [plan["id"] + ".research-2"])


class LeadFixes(Base):
    def test_an_old_pipeline_keeps_advancing_behind_newer_plans(self):
        pid = self.sched.pipeline.start("add pagination to the list", "Alpha", {"level": "complex"})["id"]
        for i in range(25):   # newer plans push it out of the newest 20
            time.sleep(0.001)
            self.tasks.add_plan(f"other {i}", "Alpha", [], status="draft")
        self.assertNotIn(pid, [p["id"] for p in self.tasks.plans(limit=20)])
        self.assertEqual(self.drive(pid)["status"], "done")

    def test_review_diffs_from_the_commit_the_pipeline_started_at(self):
        import subprocess
        from hub import review
        proj = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha"
        git = lambda *a: subprocess.run(["git", "-C", str(proj), *a], check=True, capture_output=True,
                                        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "PATH": "/usr/bin:/bin",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "HOME": self.tmp})
        git("init", "-q")
        (proj / "app.py").write_text("x = 1\n")
        git("add", "-A"); git("commit", "-qm", "base")
        base = pipeline._git_head(self.cfg, "Alpha")
        self.assertRegex(base, r"^[0-9a-f]{40}$")
        (proj / "app.py").write_text("x = 2  # changed by a build task\n")
        git("commit", "-qam", "auto-commit after a build task")
        plan = {"project": "Alpha", "tasks": [], "goal": "g"}
        self.assertNotIn("changed by a build task", review.combined_changes(self.cfg, plan))
        self.assertIn("changed by a build task", review.combined_changes(self.cfg, {**plan, "base": base}))


class ServerPipeline(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        claude_skills = Path(self.tmp) / "claude_skills"
        agy_skills = Path(self.tmp) / "agy_skills"
        (claude_skills / "researcher").mkdir(parents=True)
        (claude_skills / "coder").mkdir(parents=True)
        (agy_skills / "coder").mkdir(parents=True)
        self._skill_env = mock.patch.dict(os.environ, {
            "JARVIEE_CLAUDE_SKILL_DIRS": str(claude_skills),
            "JARVIEE_AGY_SKILL_DIRS": str(agy_skills),
        })
        self._skill_env.start()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["pipeline"].update(enabled=True, planner="mock")
        self.cfg["pipeline"]["skills"].update(research="researcher", plan="project-manager", build="coder",
                                              review="code-reviewer")   # empty in the public config.json
        self.cfg["pipeline"]["analysis"]["enabled"] = False
        self.cfg["pipeline"]["fact_check"]["enabled"] = False
        self.cfg["pipeline"]["test"]["enabled"] = True     # the fix-round tests need test and review on
        self.cfg["pipeline"]["review"]["enabled"] = True
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, interval=0.1)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"

    def tearDown(self):
        self.srv.scheduler.stop()
        self.srv.scheduler._thread.join(3)
        self.srv.shutdown()
        self.srv.server_close()
        self._skill_env.stop()
        self._t.__exit__()

    def req(self, path, body=None):
        from hub import auth
        h = {"Content-Type": "application/json"}
        if body is not None:
            h["X-Jarviee-Token"] = auth.get_token(self.cfg)
        data = None if body is None else json.dumps(body).encode()
        try:
            with _OPEN(urllib.request.Request(self.base + path, data=data, headers=h), timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    COMPLEX = "add pagination to the projects API endpoint and update the client"

    def test_run_starts_a_pipeline_that_reaches_done(self):
        code, r = self.req("/api/run", {"task": "/pipeline " + self.COMPLEX, "project": "Alpha"})
        self.assertEqual(code, 200)
        self.assertTrue(r["pipeline"])
        self.assertEqual(r["id"], r["plan_id"])
        self.assertEqual(r["phases"], ["research", "plan", "build", "test", "review"])
        end = time.time() + 40
        while time.time() < end:
            plan = self.req(f"/api/plan?id={r['plan_id']}")[1]
            if plan["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(plan["pipeline"]["phase"], "done")
        self.assertEqual(plan["goal"], self.COMPLEX)                                   # the prefix is not the goal
        self.assertIn("test-0", [t["id"].split(".", 1)[1] for t in plan["tasks"]])
        self.assertIn("Use the researcher skill.", next(t for t in plan["tasks"] if t["phase"] == "research")["prompt"])
        self.assertTrue(all("Use the coder skill." in t["prompt"] for t in plan["tasks"] if t["phase"] == "build"))
        queue = self.req("/api/state")[1]["queue"]
        self.assertEqual(queue["plans"][0]["pipeline"]["phase"], "done")

    def test_without_the_prefix_even_complex_runs_direct(self):
        code, r = self.req("/api/run", {"task": self.COMPLEX, "project": "Alpha"})
        self.assertEqual(code, 200)
        self.assertNotIn("pipeline", r)
        self.assertTrue(r["id"].startswith("run-"))
        self.assertEqual(self.req("/api/run", {"task": "/pipeline   ", "project": "Alpha"})[0], 400)

    def test_single_opts_out_and_light_tasks_are_unchanged(self):
        code, r = self.req("/api/run", {"task": "/pipeline " + self.COMPLEX, "single": True, "project": "Alpha"})
        self.assertEqual(code, 200)
        self.assertNotIn("pipeline", r)
        self.assertTrue(r["id"].startswith("run-"))
        code, r = self.req("/api/run", {"task": "rename getUser to fetchUser", "project": "Alpha"})
        self.assertNotIn("pipeline", r)

    def test_unknown_project_is_a_400(self):
        code, r = self.req("/api/run", {"task": "/pipeline " + self.COMPLEX, "project": "Nope"})
        self.assertEqual(code, 400)

    def test_classify_reports_the_pipeline(self):
        r = self.req("/api/classify", {"task": self.COMPLEX})[1]
        self.assertFalse(r["pipeline"])                                                  # no prefix: direct
        r = self.req("/api/classify", {"task": "/pipeline " + self.COMPLEX})[1]
        self.assertTrue(r["pipeline"])
        self.assertEqual(r["phases"], ["research", "plan", "build", "test", "review"])
        r = self.req("/api/classify", {"task": "/pipeline " + self.COMPLEX, "single": True})[1]
        self.assertEqual((r["pipeline"], r["phases"]), (False, []))
        r = self.req("/api/classify", {"task": "rename getUser to fetchUser"})[1]
        self.assertFalse(r["pipeline"])

    def test_get_plan(self):
        pid = self.srv.tasks.add_plan("g", "", [], planner="pipeline", status="approved",
                                      pipeline={"phase": "planning"})
        code, plan = self.req(f"/api/plan?id={pid}")
        self.assertEqual((code, plan["pipeline"]["phase"]), (200, "planning"))
        self.assertEqual(self.req("/api/plan?id=plan-nope")[0], 404)


class ReadOnlyArgv(unittest.TestCase):
    def test_readonly_args_and_launch(self):
        self.assertEqual(launcher.readonly_args("claude"), ["--permission-mode", "plan"])
        self.assertEqual(launcher.readonly_args("agy"), ["--mode", "plan"])
        self.assertEqual(launcher.readonly_args("echo"), [])
        with TempDir() as tmp:
            cfg, _ = make_cfg(tmp)
            cfg["tools"]["claude"]["command"] = [sys.executable, "-c", "import sys; print('ARGV', sys.argv[2:])",
                                                 "{prompt}", "--permission-mode", "auto"]
            store = Store(cfg["db_path"])
            p = launcher.plan("look around", cfg, tool="claude", tier="complex")
            rid, _ = launcher.launch("look around", cfg, store, p=dict(p, read_only=True))
            launcher.wait(rid, 10)
            log = Path(store.get(rid)["log_path"]).read_text()
            self.assertIn("'--permission-mode', 'plan'", log)
            self.assertNotIn("auto", log)


class Regressions(Base):
    def test_cwd_and_pinned_tool_are_kept_for_the_whole_pipeline(self):
        work = Path(self.tmp) / "work"
        work.mkdir()
        seen = {}
        inner = crafted([("t1", "build it", [])])

        def spy(*a, **k):
            seen.update(k)
            return inner()
        with mock.patch.object(planner, "make_plan", side_effect=spy):
            pid = self.sched.pipeline.start("goal", "", {"level": "complex"}, cwd=str(work), tool="agy")["id"]
            plan = self.drive(pid)
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(seen["cwd"], str(work))
        self.assertEqual((plan["pipeline"]["cwd"], plan["pipeline"]["tool"]), (str(work), "agy"))
        for t in plan["tasks"]:
            self.assertEqual(t["tool"], "claude" if t["phase"] == "review" else "agy", t["id"])
            self.assertIn("CWD " + str(work.resolve()), self.log(plan, t["id"].split(".", 1)[1]))
        with self.assertRaises(ValueError):
            self.sched.pipeline.start("goal", "", {"level": "complex"}, cwd=str(work / "nope"))
        # a project wins over cwd
        st = self.sched.pipeline.start("goal", "Alpha", {"level": "complex"}, cwd=str(work))["pipeline"]
        self.assertEqual(st["cwd"], "")

    def test_tasks_of_a_cancelled_plan_never_start(self):
        pid = self.tasks.add_plan("g", "", [], status="cancelled")
        self.tasks.add_tasks(pid, "", [{"id": "t1", "title": "x", "prompt": "p", "level": "light", "tool": "agy",
                                        "model": "m"}])
        self.sched.tick()
        t = self.tasks.get(pid + ".t1")
        self.assertEqual((t["status"], t["route_note"]), ("cancelled", "plan cancelled"))

    def test_plan_cancel_also_cancels_waiting_tasks(self):
        pid = self.tasks.add_plan("g", "", [{"id": "t1", "title": "x", "prompt": "p", "level": "light",
                                             "tool": "agy", "model": "m"}], status="approved")
        self.tasks.set(pid + ".t1", status="waiting")
        queue_api.post("/api/plan/cancel", {"id": pid}, self.cfg, self.tasks, self.sched)
        self.assertEqual(self.tasks.get(pid + ".t1")["status"], "cancelled")

    def test_planning_without_a_thread_restarts_then_gives_up(self):
        pid = self.tasks.add_plan("goal", "Alpha", [], planner="pipeline", status="approved", pipeline={
            "phase": "planning", "level": "complex", "round": 0, "max_rounds": 1, "research": "", "rounds": [],
            "note": "x"})
        with mock.patch.object(planner, "make_plan", crafted([("t1", "one", [])])):
            plan = self.drive(pid)
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(plan["pipeline"]["planning_restarts"], 1)
        pid = self.tasks.add_plan("goal", "Alpha", [], planner="pipeline", status="approved", pipeline={
            "phase": "planning", "planning_restarts": 2, "level": "complex", "round": 0, "max_rounds": 1,
            "research": "", "rounds": [], "note": "x"})
        self.sched.tick()
        plan = self.tasks.plan(pid)
        self.assertEqual(plan["status"], "failed")
        self.assertIn("restarted twice", plan["pipeline"]["note"])

    def test_tolerant_verdicts(self):
        for line, want in [("**VERDICT:** FAIL", "fail"), ("> VERDICT: FAIL", "fail"), ("- VERDICT: FAIL", "fail"),
                           ("Verdict \u2014 FAIL", "fail"), ("VERDICT = PASS", "pass"), ("vErDiCt: pass", "pass"),
                           ("## **Verdict: PASS**", "pass")]:
            self.assertEqual(pipeline.parse_verdict("text\n" + line + "\n")[0], want, line)
        for echo in ("must end with VERDICT: PASS or VERDICT: FAIL", "the VERDICT = PASS line"):
            self.assertEqual(pipeline.parse_verdict(echo)[0], "unknown", echo)

    def test_review_that_never_runs_is_retried_once_then_fails_the_plan(self):
        plan = self.run_pipeline("add pagination REVIEW_FAIL")
        self.assertEqual(plan["status"], "failed")
        self.assertIn("review-0-retry", self.ids(plan))
        self.assertEqual(plan["pipeline"]["note"], "review could not run; changes are unreviewed")

    def test_review_retry_can_succeed(self):
        flag = Path(self.tmp) / "once"
        plan = self.run_pipeline(f"add pagination REVIEW_ONCE={flag}")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        self.assertEqual(self.task(plan, "review-0")["status"], "failed")
        self.assertEqual(self.task(plan, "review-0-retry")["status"], "done")

    def test_read_only_task_on_a_tool_without_a_read_only_mode_is_rerouted(self):
        pid = self.tasks.add_plan("g", "", [{"id": "t1", "title": "x", "prompt": "look", "level": "light",
                                             "tool": "echo", "model": "m", "read_only": True}])
        self.tasks.approve(pid)
        self.sched.tick()
        t = self.tasks.get(pid + ".t1")
        self.assertIn(t["tool"], ("claude", "agy"))
        self.assertIn("no read-only mode", t["route_note"])
        with self.assertRaises(ValueError):
            launcher.launch("x", self.cfg, self.store, p=dict(launcher.plan("x", self.cfg, tool="echo"), read_only=True))
        end = time.time() + 10
        while time.time() < end and self.tasks.get(pid + ".t1")["status"] == "running":
            time.sleep(0.1)

    def test_research_not_started_when_the_plan_was_cancelled(self):
        pid = self.sched.pipeline.start("goal", "Alpha", {"level": "complex"})["id"]
        plan = self.tasks.plan(pid)
        self.tasks.set(pid + ".research", status="done")
        plan = self.tasks.plan(pid)
        self.tasks.set_plan(pid, status="cancelled")
        with mock.patch.object(planner, "make_plan") as mp:
            self.sched.pipeline._after_research(plan, plan["pipeline"])
        mp.assert_not_called()
        self.assertEqual(self.sched.pipeline._threads, {})

    def test_state_payload_hides_internals_and_trims_research(self):
        pid = self.tasks.add_plan("g", "", [], planner="pipeline", status="approved",
                                  pipeline={"phase": "test", "research": "r" * 2000, "_test": {"verdict": "pass"}})
        q = queue_api.queue_state(self.cfg, self.tasks, self.sched)
        pl = next(p for p in q["plans"] if p["id"] == pid)["pipeline"]
        self.assertNotIn("_test", pl)
        self.assertEqual(len(pl["research"]), 500)
        self.assertEqual(len(self.tasks.plan(pid)["pipeline"]["research"]), 2000)


class SixWorkPipelineTests(Base):
    def test_six_work_phases_and_execution(self):
        self.cfg["pipeline"]["analysis"]["enabled"] = True
        self.cfg["pipeline"]["fact_check"]["enabled"] = True
        self.cfg["pipeline"]["test"]["enabled"] = False
        self.cfg["pipeline"]["review"]["enabled"] = True
        self.assertEqual(pipeline.phase_names(self.cfg),
                         ["research", "analysis", "plan", "build", "fact_check", "review"])
        plan = self.run_pipeline("implement feature X")
        self.assertEqual(plan["status"], "done", plan["pipeline"])
        ids = self.ids(plan)
        for expected in ("research", "analysis", "fact_check-0", "review-0"):
            self.assertIn(expected, ids)

    def test_prune_old_plans_removes_only_terminal_older_than_7_days(self):
        now = time.time()
        old_done = self.tasks.add_plan("old done", "Alpha", [], planner="pipeline", status="done")
        old_run = self.tasks.add_plan("old running", "Alpha", [], planner="pipeline", status="running")
        recent_done = self.tasks.add_plan("recent done", "Alpha", [], planner="pipeline", status="done")
        old_ts = now - 8 * 86400
        self.store.db.execute("UPDATE plans SET created=?, updated=? WHERE id IN (?,?)",
                              (old_ts, old_ts, old_done, old_run))
        self.store.db.commit()
        code, res = queue_api.post("/api/plan/prune", {"days": 7}, self.cfg, self.tasks, self.sched)
        self.assertEqual(code, 200)
        self.assertEqual(res["removed"], 1)
        self.assertEqual(res["ids"], [old_done])
        self.assertIsNone(self.tasks.plan(old_done))
        self.assertIsNotNone(self.tasks.plan(old_run))
        self.assertIsNotNone(self.tasks.plan(recent_done))


if __name__ == "__main__":
    unittest.main()
