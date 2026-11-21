"""PID reuse and the one rule for a run whose process vanished (scheduler and progress sync agree)."""
import os
import time
import unittest
from unittest import mock

from helpers import TempDir, make_cfg
from hub import launcher, orphans, progress
from hub.scheduler import Scheduler, _alive
from hub.store import Store
from hub.tasks import Tasks

DEAD_PID = 2 ** 22 + 4321
OTHER_START = "Mon Jan  1 00:00:00 2001"


class Alive(unittest.TestCase):
    def test_own_start_time_matches(self):
        start = orphans.proc_start(os.getpid())
        self.assertTrue(start)
        self.assertTrue(orphans.alive(os.getpid(), start))
        self.assertTrue(_alive(os.getpid(), start))

    def test_a_recycled_pid_is_not_the_same_process(self):
        self.assertFalse(orphans.alive(os.getpid(), OTHER_START))
        self.assertFalse(_alive(os.getpid(), OTHER_START))

    def test_rows_without_a_start_time_are_judged_by_the_pid_alone(self):
        self.assertTrue(orphans.alive(os.getpid()))
        self.assertTrue(orphans.alive(os.getpid(), None))
        self.assertTrue(orphans.alive(os.getpid(), ""))

    def test_dead_pid_and_junk(self):
        self.assertFalse(orphans.alive(DEAD_PID, OTHER_START))
        self.assertFalse(orphans.alive(None))
        self.assertFalse(orphans.alive("x"))

    def test_unknown_start_time_does_not_kill_a_live_run(self):
        with mock.patch.object(orphans, "proc_start", return_value=""):
            self.assertTrue(orphans.alive(os.getpid(), OTHER_START))

    def test_whitespace_in_the_recorded_start_is_ignored(self):
        start = orphans.proc_start(os.getpid())
        self.assertTrue(orphans.alive(os.getpid(), "  " + start.replace(" ", "   ") + " "))

    def test_run_alive_rows(self):
        self.assertTrue(orphans.run_alive({"pid": os.getpid()}))
        self.assertFalse(orphans.run_alive({"pid": os.getpid(), "pid_start": OTHER_START}))
        self.assertFalse(orphans.run_alive({}))
        self.assertTrue(orphans.run_alive({"updated": time.time()}, nopid_grace=60))
        self.assertFalse(orphans.run_alive({"updated": time.time() - 120}, nopid_grace=60))


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        self.sched = Scheduler(self.cfg, self.store, self.tasks)
        self.sched.paused = True

    def tearDown(self):
        self._t.__exit__()

    def running_task(self, run_id, **run):
        rows = [{"id": "t1", "title": "task", "prompt": "add a tooltip", "level": "light", "tool": "claude",
                 "model": "m", "files": ["a.py"], "depends_on": []}]
        pid = self.tasks.add_plan("goal", "Alpha", rows, planner="test")
        self.tasks.approve(pid)
        tid = self.tasks.plan(pid)["tasks"][0]["id"]
        self.tasks.set(tid, status="running", run_id=run_id, started=time.time(), attempts=1)
        self.store.upsert(run_id, source="launcher", tool="claude", **run)
        return tid


class PidReuse(Base):
    def test_task_whose_pid_was_recycled_is_settled(self):
        tid = self.running_task("run-r", status="running", pid=os.getpid(), pid_start=OTHER_START, steps=3)
        self.sched._reconcile()
        self.assertNotEqual(self.tasks.get(tid)["status"], "running")
        self.assertEqual(self.store.get("run-r")["status"], "failed")

    def test_same_process_is_left_alone(self):
        tid = self.running_task("run-s", status="running", pid=os.getpid(), pid_start=orphans.proc_start(os.getpid()))
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "running")

    def test_old_row_without_a_start_time_is_left_alone(self):
        tid = self.running_task("run-o", status="running", pid=os.getpid())
        self.sched._reconcile()
        self.assertEqual(self.tasks.get(tid)["status"], "running")

    def test_launch_records_the_start_time(self):
        rid, _ = launcher.launch("echo hi", self.cfg, self.store, tool="echo", log_runs=False)
        self.assertTrue(launcher.wait(rid, 10))
        row = self.store.get(rid)
        self.assertTrue(row["pid"])
        self.assertRegex(row["pid_start"], r"\d{4}$")

    def test_progress_sync_also_sees_the_recycled_pid(self):
        self.store.upsert("run-p", source="launcher", tool="claude", status="running", pid=os.getpid(),
                          pid_start=OTHER_START, steps=2, task="do a thing")
        progress.sync_run_progress(self.store.get("run-p"), self.store)
        self.assertEqual(self.store.get("run-p")["status"], "failed")


class OneRule(Base):
    """A run whose process vanished without a recorded exit is failed, from either entry point."""

    def test_scheduler_entry_point_with_steps(self):
        tid = self.running_task("run-a", status="running", pid=DEAD_PID, steps=5)
        self.sched._reconcile()
        run = self.store.get("run-a")
        self.assertEqual((run["status"], run["activity"]), ("failed", orphans.LOST_NOTE))
        self.assertNotEqual(self.tasks.get(tid)["status"], "done")

    def test_progress_entry_point_with_steps(self):
        self.store.upsert("run-b", source="launcher", tool="claude", status="running", pid=DEAD_PID, steps=5,
                          task="do a thing")
        progress.sync_run_progress(self.store.get("run-b"), self.store)
        run = self.store.get("run-b")
        self.assertEqual((run["status"], run["activity"]), ("failed", orphans.LOST_NOTE))

    def test_dashboard_poll_first_then_scheduler_settles_the_task_as_failed_path(self):
        tid = self.running_task("run-c", status="running", pid=DEAD_PID, steps=5)
        progress.sync_run_progress(self.store.get("run-c"), self.store)   # the dashboard poll gets there first
        self.assertEqual(self.store.get("run-c")["status"], "failed")
        self.sched._reconcile()                                           # the scheduler then settles the task
        self.assertNotIn(self.tasks.get(tid)["status"], ("done", "running"))
        self.assertEqual(self.store.get("run-c")["activity"], orphans.LOST_NOTE)

    def test_claims_are_released(self):
        with mock.patch("hub.coord.release") as rel:
            self.store.upsert("run-d", source="launcher", status="running", pid=DEAD_PID, task="x")
            orphans.settle_lost(self.store, self.store.get("run-d"))
        rel.assert_called_once()


class StartTimes(unittest.TestCase):
    def test_zone_or_dst_shift_of_whole_hours_is_the_same_process(self):
        a = "Mon Nov 12 10:00:00 2026"
        self.assertTrue(orphans.same_start(a, "Mon Nov 12 11:00:00 2026"))
        self.assertTrue(orphans.same_start(a, "Mon Nov 12 08:00:00 2026"))
        self.assertTrue(orphans.same_start(a, "Mon  Nov 12 10:00:00 2026"))
        self.assertFalse(orphans.same_start(a, "Mon Nov 12 13:00:00 2026"))
        self.assertFalse(orphans.same_start(a, "Mon Nov 12 10:00:01 2026"))
        self.assertFalse(orphans.same_start(a, "Mon Nov 12 10:30:00 2026"))
        self.assertFalse(orphans.same_start(a, "garbage"))

    def test_alive_accepts_the_shifted_start(self):
        with mock.patch.object(orphans, "proc_start", return_value="Mon Nov 12 11:00:00 2026"):
            self.assertTrue(orphans.alive(os.getpid(), "Mon Nov 12 10:00:00 2026"))
            self.assertFalse(orphans.alive(os.getpid(), OTHER_START))

    def test_proc_start_is_asked_once_per_pid_within_the_ttl(self):
        orphans._starts.clear()
        with mock.patch.object(orphans, "_ps_start", return_value="Mon Nov 12 10:00:00 2026") as ps:
            orphans.proc_start(4242)
            orphans.proc_start(4242)
            self.assertEqual(ps.call_count, 1)
            orphans.proc_start(4242, fresh=True)
            self.assertEqual(ps.call_count, 2)
        orphans._starts.clear()


class HookPid(Base):
    def test_hook_never_overwrites_the_pid_of_a_launched_row(self):
        from hub.ingest import handle_hook
        self.store.upsert("run1", source="launcher", tool="claude", status="running", pid=111, pid_start=OTHER_START)
        handle_hook("PreToolUse", {"jarvieeRunId": "run1", "jarvieeTool": "claude", "pid": 222}, self.cfg, self.store)
        row = self.store.get("run1")
        self.assertEqual((row["pid"], row["pid_start"]), (111, OTHER_START))
        handle_hook("SessionStart", {"session_id": "s9", "jarvieeTool": "claude", "pid": 333}, self.cfg, self.store)
        self.assertEqual(self.store.get("claude-s9")["pid"], 333)


if __name__ == "__main__":
    unittest.main()
