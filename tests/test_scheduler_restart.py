"""Queue stuck after a service restart: every way a `running` task can be orphaned must settle."""
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import launcher, tasks as tasklocks
from hub.runjob import RunJob
from hub.scheduler import Scheduler
from hub.store import Store
from hub.tasks import Tasks

DEAD_PID = 2 ** 22 + 4321   # above the pid range of this machine: no such process


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.ws = Path(self.cfg["obsidian"]["workspace_root"])
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks)
        self.sched.paused = True      # these tests call _reconcile / tick directly and must not launch anything

    def tearDown(self):
        self._t.__exit__()

    def running_task(self, run_id=None, files=("a.py",), lock=True, **run):
        rows = [{"id": "t1", "title": "task", "prompt": "add a tooltip", "level": "light", "tool": "claude",
                 "model": "m", "files": list(files), "depends_on": []}]
        pid = self.tasks.add_plan("goal", "Alpha", rows, planner="test")
        self.tasks.approve(pid)
        tid = self.tasks.plan(pid)["tasks"][0]["id"]
        self.tasks.set(tid, status="running", run_id=run_id, started=time.time(), attempts=1)
        if run_id:
            self.store.upsert(run_id, source="launcher", tool="claude", **run)
        if lock:
            tasklocks.claim(self.cfg, "Alpha", tid, list(files))
        return tid

    def lock_file(self, tid):
        return self.ws / "Alpha" / ".brain" / ".locks" / f"{tid}.lock"


class Reconcile(Base):
    def test_run_finished_while_the_service_was_down(self):
        tid = self.running_task("run-done", status="done", ended=time.time())
        self.assertTrue(self.lock_file(tid).exists())
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "done")
        self.assertFalse(self.lock_file(tid).exists())
        self.assertEqual(self.store.get("run-done")["status"], "done")

    def test_run_stopped_while_the_service_was_down_is_cancelled(self):
        tid = self.running_task("run-stop", status="stopped", closed_by_user=1)
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "cancelled")
        self.assertFalse(self.lock_file(tid).exists())

    def test_run_row_still_running_with_a_dead_pid(self):
        tid = self.running_task("run-dead", status="running", pid=DEAD_PID)
        self.sched._reconcile()
        t = self.tasks.get(tid)
        self.assertNotEqual(t["status"], "running")             # failed, or re-queued by the escalation rule
        self.assertFalse(self.lock_file(tid).exists())
        run = self.store.get("run-dead")
        self.assertEqual(run["status"], "failed")                # the row is settled too, not left "running" for ever
        self.assertIn("restart", run["activity"])
        self.assertTrue(run["ended"])

    def test_run_row_running_without_a_pid_is_settled(self):
        tid = self.running_task("run-nopid", status="running")
        self.sched._reconcile()
        self.assertNotEqual(self.tasks.get(tid)["status"], "running")
        self.assertEqual(self.store.get("run-nopid")["status"], "failed")

    def test_run_that_is_still_alive_is_left_alone(self):
        tid = self.running_task("run-alive", status="running", pid=os.getpid())
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "running")
        self.assertTrue(self.lock_file(tid).exists())
        self.assertEqual(self.store.get("run-alive")["status"], "running")

    def test_task_without_a_run_is_requeued_and_unlocked(self):
        tid = self.running_task(None)
        self.sched._reconcile()
        t = self.tasks.get(tid)
        self.assertEqual(t["status"], "queued")
        self.assertIn("lost in a restart", t["route_note"])
        self.assertFalse(self.lock_file(tid).exists())

    def test_task_whose_run_row_is_gone_is_requeued(self):
        tid = self.running_task("run-vanished", lock=True)
        self.store.db.execute("DELETE FROM agents WHERE id='run-vanished'")
        self.store.db.commit()
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "queued")
        self.assertFalse(self.lock_file(tid).exists())

    def test_ended_run_whose_callback_never_came_is_settled_after_a_grace_period(self):
        tid = self.running_task("run-lost", status="done", ended=time.time() - 60)
        self.sched._live.add(tid)                                # launched by this process, callback never fired
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "done")
        self.assertNotIn(tid, self.sched._live)

    def test_ended_run_is_not_settled_while_its_callback_is_due(self):
        tid = self.running_task("run-fresh", status="done", ended=time.time())
        self.sched._live.add(tid)
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "running")

    def test_a_live_task_is_never_touched_while_its_run_runs(self):
        tid = self.running_task("run-mine", status="running", pid=DEAD_PID)
        self.sched._live.add(tid)                                # this process owns the worker thread
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "running")


class LockSweep(Base):
    def test_leftover_locks_of_tasks_that_are_not_running_are_removed(self):
        stuck = self.running_task("run-x", status="done", ended=time.time(), lock=True)
        self.tasks.set(stuck, status="done")                     # settled by some other path, lock left behind
        live = self.running_task("run-live", status="running", pid=os.getpid(), lock=True, files=("b.py",))
        locks = self.ws / "Alpha" / ".brain" / ".locks"
        (locks / "someone-elses.lock").write_text("x\n")         # not a task id: never touched
        self.sched.tick()
        self.assertFalse(self.lock_file(stuck).exists())
        self.assertTrue(self.lock_file(live).exists())
        self.assertTrue((locks / "someone-elses.lock").exists())

    def test_sweep_is_throttled(self):
        calls = []
        with mock.patch.object(Scheduler, "_sweep_locks", side_effect=lambda: calls.append(1)):
            for _ in range(5):
                self.sched.tick()
        self.assertEqual(len(calls), 1)

    def test_reconcile_asks_only_for_running_tasks(self):
        with mock.patch.object(self.tasks, "list", wraps=self.tasks.list) as lst:
            self.sched._reconcile()
        self.assertEqual(lst.call_args_list[0].kwargs, {"statuses": ["running"]})


class WorkerCrash(Base):
    def test_a_crashing_worker_still_ends_the_run_and_calls_back(self):
        calls = []
        with mock.patch.object(RunJob, "run", side_effect=RuntimeError("boom")):
            rid, _ = launcher.launch("echo hi", self.cfg, self.store, tool="echo", project="Alpha",
                                     on_finish=lambda r, s, c: calls.append((r, s, c)), log_runs=False)
            self.assertTrue(launcher.wait(rid, 10))
        self.assertEqual(calls, [(rid, "failed", None)])
        row = self.store.get(rid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("launcher error", row["activity"])

    def test_a_crash_after_the_callback_does_not_call_back_twice(self):
        calls = []

        def boom(self):
            self._notify("done", 0)
            raise RuntimeError("late")
        with mock.patch.object(RunJob, "run", boom):
            rid, _ = launcher.launch("echo hi", self.cfg, self.store, tool="echo", project="Alpha",
                                     on_finish=lambda r, s, c: calls.append((r, s, c)), log_runs=False)
            launcher.wait(rid, 10)
        self.assertEqual(calls, [(rid, "done", 0)])


class EndToEnd(Base):
    def test_restart_mid_run_then_the_queue_moves_on(self):
        """The TODO's case: a finished run holds the lane; after reconcile the next task starts."""
        rows = [{"id": "a", "title": "first", "prompt": "add a tooltip", "level": "light", "tool": "claude",
                 "model": "m", "files": ["a.py"], "depends_on": []},
                {"id": "b", "title": "second", "prompt": "add a footer", "level": "light", "tool": "claude",
                 "model": "m", "files": ["b.py"], "depends_on": []}]
        pid = self.tasks.add_plan("goal", "Alpha", rows, planner="test")
        self.tasks.approve(pid)
        first, second = [t["id"] for t in self.tasks.plan(pid)["tasks"]]
        self.tasks.set(first, status="running", run_id="run-old", started=time.time())
        self.store.upsert("run-old", source="launcher", tool="claude", status="done", ended=time.time())
        tasklocks.claim(self.cfg, "Alpha", first, ["a.py"])
        self.cfg["lanes"] = {"claude": 1, "agy": 1}
        self.sched.paused = False
        end = time.time() + 30
        while time.time() < end:
            self.sched.tick()
            if all(self.tasks.get(t)["status"] in ("done", "failed", "cancelled") for t in (first, second)):
                break
            time.sleep(0.05)
        self.assertEqual(self.tasks.get(first)["status"], "done")
        self.assertEqual(self.tasks.get(second)["status"], "done")
        self.assertFalse(self.lock_file(first).exists())


if __name__ == "__main__":
    unittest.main()
