"""hub/jobs_api.py: GET /api/jobs, GET /api/jobs/<id>, the complex-job rule from config, and closed jobs never returning."""
import time
import unittest

from helpers import TempDir, make_cfg
from hub import jobs, jobs_api, jobstatus, routes
from hub.store import Store
from hub.tasks import Tasks


def _task(tid, title, tool="claude"):
    return {"id": tid, "title": title, "tool": tool, "model": "sonnet", "level": "light", "prompt": "p"}


class Base(unittest.TestCase):
    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self._td.__exit__()

    def plan(self, goal, specs, status="running"):
        pid = self.tasks.add_plan(goal, "Alpha", specs, planner="claude", status=status)
        return pid, self.tasks.list(plan_id=pid)

    def run_for(self, rid, task_row, **kw):
        fields = dict(source="launcher", tool=task_row.get("tool") or "claude", status="running", steps=1,
                      reasons=f'["plan task {task_row["id"]}"]', started=time.time() - 20)
        fields.update(kw)
        self.store.upsert(rid, **fields)
        self.tasks.set(task_row["id"], run_id=rid)

    def listing(self, **kw):
        return jobs_api.listing(self.cfg, self.store, self.tasks, **kw)["jobs"]


class Listing(Base):
    def test_each_plan_is_its_own_job_with_the_asked_fields(self):
        pa, ta = self.plan("Alpha goal", [_task("t1", "Research"), _task("t2", "Build", "agy")])
        pb, tb = self.plan("Beta goal", [_task("t1", "Other"), _task("t2", "Other 2")])
        self.run_for("run-a1", ta[0], status="done", steps=3, ended=time.time())
        self.run_for("run-b1", tb[0], steps=3)
        self.tasks.set(ta[0]["id"], status="done")
        self.run_for("run-a2", ta[1], tool="agy", steps=2)
        self.tasks.set(ta[1]["id"], status="running")
        by_id = {j["id"]: j for j in self.listing()}
        self.assertEqual(set(by_id), {pa, pb})
        a = by_id[pa]
        for key in ("id", "title", "agent", "status", "pct", "done", "total", "started", "current_step", "key", "header"):
            self.assertIn(key, a)
        self.assertEqual((a["done"], a["total"], a["pct"]), (1, 2, 50))
        self.assertEqual(set(a["agents"]), {"Claude", "agy"})
        self.assertEqual(a["agent"], " + ".join(a["agents"]))
        self.assertEqual(a["current_step"], "Build")
        self.assertEqual(a["title"], "Alpha goal")
        self.assertEqual(by_id[pb]["done"], 0)   # B's tasks are not mixed into A's progress

    def test_status_and_colour_come_from_jobstatus(self):
        pid, rows = self.plan("Goal", [_task("t1", "A"), _task("t2", "B"), _task("t3", "C")])
        self.run_for("run-x", rows[0], status="waiting", steps=4)
        self.tasks.set(rows[0]["id"], status="running")
        job = self.listing()[0]
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(job["header"], jobstatus.header_kind_of({"status": job["status"]}))
        self.assertEqual(tuple(job["color"]), jobstatus.TAB_COLORS["waiting"])

    def test_complex_rule_is_config_driven(self):
        now = time.time()
        self.store.upsert("claude-simple1", source="hook", tool="claude", status="running", steps=1, started=now - 5)
        self.store.upsert("claude-busy001", source="hook", tool="claude", status="running", steps=1, started=now - 500)
        self.assertEqual([j["id"] for j in self.listing()], ["claude-busy001"])   # longer than the default 60s
        self.cfg["complex_job_duration_seconds"] = 10_000
        self.assertEqual(self.listing(), [])
        self.cfg["complex_job_min_steps"] = 0   # more steps than 0
        self.assertEqual({j["id"] for j in self.listing()}, {"claude-simple1", "claude-busy001"})
        self.assertEqual({j["id"] for j in self.listing(all_jobs=True)}, {"claude-simple1", "claude-busy001"})

    def test_all_flag_lists_non_complex_jobs_too(self):
        self.store.upsert("claude-simple2", source="hook", tool="claude", status="running", steps=1, started=time.time() - 3)
        self.assertEqual(self.listing(), [])
        self.assertEqual([j["id"] for j in self.listing(all_jobs=True)], ["claude-simple2"])


class Detail(Base):
    def test_returns_only_this_jobs_stages_and_steps(self):
        pa, ta = self.plan("Alpha goal", [_task("t1", "Alpha one"), _task("t2", "Alpha two")])
        pb, tb = self.plan("Beta goal", [_task("t1", "Beta one"), _task("t2", "Beta two")])
        self.run_for("run-a1", ta[0], steps=4)
        code, body = jobs_api.detail(self.cfg, self.store, self.tasks, pa)
        self.assertEqual(code, 200)
        self.assertEqual(body["job"]["id"], pa)
        names = [s["name"] for st in body["stages"] for s in st["steps"]]
        self.assertTrue(names)
        ids = {s["id"] for st in body["stages"] for s in st["steps"]}
        self.assertTrue({t["id"] for t in ta} <= ids)
        self.assertFalse(ids & {t["id"] for t in tb})
        self.assertTrue(all("label" in st and "phase" in st for st in body["stages"]))

    def test_bad_and_unknown_ids(self):
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, "../etc")[0], 400)
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, "plan-abcd1234")[0], 404)

    def test_pane_reused_for_a_new_job_does_not_resolve_the_old_one(self):
        self.store.upsert("claude-pane0001", source="hook", tool="claude", status="running", steps=5,
                          started=time.time() - 90, job_id="job-old")
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, "claude-pane0001", "job-old")[0], 200)
        self.store.upsert("claude-pane0001", job_id="job-new", steps=0, started=time.time())
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, "claude-pane0001", "job-old")[0], 404)
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, "claude-pane0001", "job-new")[0], 200)


class ClosedNeverReturns(Base):
    def test_closed_run_leaves_the_list_and_stays_gone(self):
        self.store.upsert("claude-close001", source="hook", tool="claude", status="running", steps=9, started=time.time() - 200)
        self.assertEqual([j["id"] for j in self.listing()], ["claude-close001"])
        self.store.upsert("claude-close001", status="stopped", closed_by_user=1, ended=time.time())
        for _ in range(3):   # every later poll
            self.assertEqual(self.listing(), [])
            self.assertEqual(self.listing(all_jobs=True), [])
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, "claude-close001")[0], 410)

    def test_a_late_status_update_does_not_reopen_a_closed_job(self):
        self.store.upsert("claude-close002", source="hook", tool="claude", status="stopped", closed_by_user=1,
                          steps=9, started=time.time() - 200, ended=time.time())
        self.store.upsert("claude-close002", steps=10)   # a straggling hook event
        self.assertEqual(self.listing(), [])

    def test_force_closed_plan_leaves_the_list(self):
        pid, rows = self.plan("Goal", [_task("t1", "A"), _task("t2", "B")])
        self.run_for("run-c1", rows[0], steps=4)
        self.assertEqual([j["id"] for j in self.listing()], [pid])
        self.store.upsert("run-c1", status="stopped", closed_by_user=1)
        self.assertEqual(self.listing(), [])
        self.assertEqual(jobs_api.detail(self.cfg, self.store, self.tasks, pid)[0], 410)

    def test_cancelled_plan_without_runs_has_no_tab(self):
        self.plan("Goal", [_task("t1", "A"), _task("t2", "B")], status="cancelled")
        self.assertEqual(self.listing(), [])


class Routing(Base):
    def test_routes_resolve_and_serve(self):
        self.assertIsNotNone(routes.resolve("GET", "/api/jobs"))
        self.assertIsNotNone(routes.resolve("GET", "/api/jobs/plan-abcd1234"))
        self.assertIs(routes.resolve("GET", "/api/jobs/x").fn, routes.resolve("GET", "/api/jobs").fn)
        self.assertEqual(jobs_api.get("/api/jobs", "", self.cfg, self.store, self.tasks, None)[0], 200)
        self.assertEqual(jobs_api.get("/api/jobs/bad%20id", "", self.cfg, self.store, self.tasks, None)[0], 400)
        self.assertIsNone(jobs_api.get("/api/runs", "", self.cfg, self.store, self.tasks, None))


if __name__ == "__main__":
    unittest.main()
