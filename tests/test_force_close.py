"""Force-closing a job: the process is killed, the row is closed by the owner, and nothing restarts it on its own.

The bug: the dying agent's own hooks (SessionEnd/Stop) and a nonzero exit made a stopped run look failed, so the
retry branch relaunched it in a new pane.
"""
import os
import signal
import threading
import time
import unittest

from fake_iterm import FakeITerm, make_terminal
from helpers import TempDir, make_cfg
from hub import launcher, panes
from hub.server import handle_hook, make_server
from hub.store import Store
from hub.tasks import Tasks
import test_pane_runner as tpr


class ForceCloseInPane(unittest.TestCase):
    # the pane harness from test_pane_runner, without inheriting its tests
    tearDown, req, launch, until, finished = (tpr.PaneRuns.tearDown, tpr.PaneRuns.req, tpr.PaneRuns.launch,
                                              tpr.PaneRuns.until, tpr.PaneRuns.finished)

    def setUp(self):
        tpr.PaneRuns.setUp(self)
        self.cfg["retry"] = {"enabled": True, "max": 3, "retry_timeouts": True}   # the restart path is armed

    def runs(self):
        return [a for a in self.store.all() if a["id"].startswith("run-")]

    def assert_no_restart(self, rid, wait=2.5):
        time.sleep(wait)   # long enough for a retry or a revived row to show up
        self.assertEqual([a["id"] for a in self.runs()], [rid], "a new run was launched after the force close")
        a = self.store.get(rid)
        self.assertEqual(a["status"], "stopped")
        self.assertTrue(a["closed_by_user"])
        return a

    def test_stop_kills_marks_closed_and_never_restarts(self):
        rid = self.launch("long FAKE_SLEEP=30")
        self.until(lambda: rid in launcher._procs)
        self.assertTrue(self.req("/api/stop", {"id": rid})[1]["ok"])
        a = self.finished(rid)
        runner = self.fake.procs.get(a["pane_id"]) or self.fake.procs["S1"]
        self.until(lambda: runner.poll() is not None, timeout=12)        # the pane runner (and agent) are gone
        # the dying agent's trailing hooks carry the same run id: they must not re-finish or revive it
        for ev in ("Stop", "SessionEnd", "PostToolUse"):
            handle_hook(ev, {"jarvieeRunId": rid, "jarvieeTool": "claude"}, self.cfg, self.store)
        a = self.assert_no_restart(rid)
        self.assertEqual(a["activity"], "stopped by you")

    def test_closing_the_pane_itself_is_a_close_not_a_failure(self):
        rid = self.launch("long FAKE_SLEEP=30")
        self.until(lambda: rid in launcher._procs)
        os.kill(launcher._procs[rid].pid, signal.SIGHUP)                  # the owner closed the iTerm2 pane
        self.finished(rid)
        a = self.assert_no_restart(rid)
        self.assertTrue(a["activity"].startswith("stopped"))

    def test_closing_a_live_pane_after_its_turn_is_done_not_stopped(self):
        rid = self.launch("long FAKE_SLEEP=30")
        self.until(lambda: rid in launcher._procs)
        self.store.upsert(rid, status="waiting", activity="turn finished, waiting for you")   # its Stop hook
        os.kill(launcher._procs[rid].pid, signal.SIGHUP)                  # you close the pane when it's done
        launcher.wait(rid, 15)
        a = self.store.get(rid)
        self.assertEqual((a["status"], a["activity"]), ("done", launcher.FINISHED_CLOSED))
        time.sleep(1.5)
        self.assertEqual([x["id"] for x in self.runs()], [rid])           # and never restarted

    def test_run_again_is_still_allowed_by_hand(self):
        rid = self.launch("long FAKE_SLEEP=30")
        self.until(lambda: rid in launcher._procs)
        self.req("/api/stop", {"id": rid})
        self.finished(rid)
        code, body = self.req("/api/rerun", {"id": rid})
        self.assertEqual(code, 200, body)
        self.assertNotEqual(body["id"], rid)


class ForceCloseQueueTask(unittest.TestCase):
    """A queue task whose card is force-closed is cancelled; the scheduler never escalates or requeues it."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        from hub.scheduler import Scheduler
        self.sched = Scheduler(self.cfg, self.store, self.tasks)

    def tearDown(self):
        self._t.__exit__()

    def test_closed_run_cancels_its_task(self):
        pid = self.tasks.add_plan("g", "", [{"id": "a", "title": "A", "prompt": "do a FAKE_SLEEP=30",
                                             "level": "basic", "tool": "agy", "model": "", "files": ["x.py"]}])
        self.tasks.approve(pid)
        tid = self.tasks.plan(pid)["tasks"][0]["id"]
        self.sched.tick()
        t = self.tasks.get(tid)
        self.assertEqual(t["status"], "running")
        end = time.time() + 10
        while t["run_id"] not in launcher._procs and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(launcher.stop_with_reason(t["run_id"], self.store)[0])
        launcher.wait(t["run_id"], 10)
        for _ in range(3):
            self.sched.tick()
        t = self.tasks.get(tid)
        self.assertEqual(t["status"], "cancelled")
        self.assertEqual(int(t["attempts"]), 1)
        self.assertEqual(len([a for a in self.store.all() if a["id"].startswith("run-")]), 1)


class HookGuard(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        self._t.__exit__()

    def test_launched_closed_run_ignores_every_hook(self):
        self.store.upsert("run-abc", source="launcher", tool="claude", status="stopped",
                          activity="stopped by you", closed_by_user=1)
        for ev in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            handle_hook(ev, {"jarvieeRunId": "run-abc", "jarvieeTool": "claude", "prompt": "x"}, self.cfg, self.store)
        self.assertEqual(self.store.get("run-abc")["status"], "stopped")

    def test_terminal_session_comes_back_only_on_a_new_prompt(self):
        aid = handle_hook("UserPromptSubmit", {"session_id": "s1", "jarvieeTool": "claude", "prompt": "one"},
                          self.cfg, self.store)
        self.store.upsert(aid, status="stopped", activity="stopped by you", closed_by_user=1)
        handle_hook("SessionEnd", {"session_id": "s1", "jarvieeTool": "claude"}, self.cfg, self.store)
        self.assertEqual(self.store.get(aid)["status"], "stopped")
        handle_hook("UserPromptSubmit", {"session_id": "s1", "jarvieeTool": "claude", "prompt": "two"},
                    self.cfg, self.store)
        a = self.store.get(aid)
        self.assertEqual((a["status"], a["closed_by_user"]), ("running", 0))


if __name__ == "__main__":
    unittest.main()
