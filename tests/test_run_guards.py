"""Run guards: claude budget / turn flags, the wall-clock cap, and the queue ceiling."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import guards, launcher, queue_api, usage
from hub.scheduler import Scheduler
from hub.store import Store
from hub.tasks import Tasks

CLAUDE_P = ["claude", "-p", "do it", "--model", "sonnet"]


class Settings(unittest.TestCase):
    def test_defaults_without_a_config_block(self):
        self.assertEqual(guards.for_level({}, "max_budget_usd", "heavy"), 40)
        self.assertEqual(guards.for_level({}, "max_minutes", "complex"), 45)
        self.assertEqual(guards.queue_limits({}), (150, 300.0))

    def test_overrides_merge_and_zero_or_null_turn_a_guard_off(self):
        cfg = {"guards": {"max_minutes": {"heavy": 120}, "max_budget_usd": None, "max_turns": 7,
                          "queue": {"max_runs_per_day": 0}}}
        self.assertEqual(guards.for_level(cfg, "max_minutes", "heavy"), 120)
        self.assertEqual(guards.for_level(cfg, "max_minutes", "basic"), None)   # a table replaces the default
        self.assertIsNone(guards.for_level(cfg, "max_budget_usd", "light"))
        self.assertEqual(guards.for_level(cfg, "max_turns", "light"), 7)
        self.assertEqual(guards.queue_limits(cfg), (0, 300.0))

    def test_enabled_false_turns_everything_off(self):
        cfg = {"guards": {"enabled": False}}
        self.assertIsNone(guards.for_level(cfg, "max_budget_usd", "light"))
        self.assertEqual(guards.wall_clock(cfg, {"level": "light"}), (None, ""))
        self.assertEqual(guards.queue_limits(cfg), (0, 0.0))
        self.assertEqual(guards.headless_argv(cfg, {"tool": "claude", "level": "light"}, CLAUDE_P), CLAUDE_P)

    def test_cap_note_round_trip(self):
        secs, note = guards.wall_clock({}, {"level": "complex"})
        self.assertEqual((secs, note), (2700.0, "stopped: over the 45 min cap"))
        self.assertTrue(guards.is_cap_note(note))
        self.assertFalse(guards.is_cap_note("stopped by you"))


class Flags(unittest.TestCase):
    def setUp(self):
        guards._flag_cache.clear()

    def test_budget_flag_is_added_to_a_headless_claude_run(self):
        out = guards.headless_argv({}, {"tool": "claude", "level": "light"}, CLAUDE_P)
        self.assertEqual(out[:len(CLAUDE_P)], CLAUDE_P)
        self.assertEqual(out[out.index("--max-budget-usd") + 1], "5")

    def test_turn_flag_only_when_the_cli_lists_it(self):
        with mock.patch.object(guards, "supports_flag", return_value=False):
            self.assertNotIn("--max-turns", guards.headless_argv({}, {"tool": "claude", "level": "light"}, CLAUDE_P))
        with mock.patch.object(guards, "supports_flag", return_value=True):
            out = guards.headless_argv({}, {"tool": "claude", "level": "light"}, CLAUDE_P)
            self.assertEqual(out[out.index("--max-turns") + 1], "80")

    def test_never_for_interactive_other_tools_or_a_flag_already_set(self):
        live = ["claude", "do it", "--model", "sonnet"]   # no -p: an interactive session
        self.assertEqual(guards.headless_argv({}, {"tool": "claude", "level": "light"}, live), live)
        self.assertEqual(guards.headless_argv({}, {"tool": "agy", "level": "light"}, ["agy", "-p", "x"]),
                         ["agy", "-p", "x"])
        own = CLAUDE_P + ["--max-budget-usd", "1"]
        out = guards.headless_argv({}, {"tool": "claude", "level": "light"}, own)
        self.assertEqual(out.count("--max-budget-usd"), 1)

    def test_supports_flag_reads_help_once(self):
        fake = mock.Mock(stdout="  --max-turns <n>  limit\n", stderr="")
        with mock.patch.object(guards.subprocess, "run", return_value=fake) as run:
            self.assertTrue(guards.supports_flag("claude", "--max-turns"))
            self.assertTrue(guards.supports_flag("claude", "--max-turns"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["claude", "--help"])
        with mock.patch.object(guards.subprocess, "run", side_effect=OSError):
            self.assertFalse(guards.supports_flag("nope", "--max-turns"))


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self._t.__exit__()


class HeadlessRun(Base):
    def test_a_claude_print_run_gets_the_ceiling_flags(self):
        script = "import sys; print('ARGV ' + ' '.join(sys.argv[1:]))"
        self.cfg["tools"]["claude"]["command"] = [sys.executable, "-c", script, "-p", "{prompt}"]
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="claude", tier="light", log_runs=False)
        self.assertTrue(launcher.wait(rid, 15))
        log = Path(self.store.get(rid)["log_path"]).read_text()
        self.assertIn("--max-budget-usd 5", log)

    def test_guards_off_means_no_flags(self):
        script = "import sys; print('ARGV ' + ' '.join(sys.argv[1:]))"
        self.cfg["tools"]["claude"]["command"] = [sys.executable, "-c", script, "-p", "{prompt}"]
        self.cfg["guards"] = {"enabled": False}
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="claude", tier="light", log_runs=False)
        self.assertTrue(launcher.wait(rid, 15))
        self.assertNotIn("--max-budget-usd", Path(self.store.get(rid)["log_path"]).read_text())


class WallClock(Base):
    def capped_cfg(self):
        self.cfg["guards"] = {"max_minutes": {lv: 0.005 for lv in ("basic", "light", "complex", "heavy")}}
        self.cfg["tools"]["echo"]["command"][2] = "import time; print('working'); time.sleep(30)"

    def test_queue_run_over_the_cap_is_stopped_and_failed_without_fallback(self):
        self.capped_cfg()
        calls = []
        t0 = time.time()
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="echo", tier="light", log_runs=False,
                                 on_finish=lambda r, s, c: calls.append((r, s, c)))
        self.assertTrue(launcher.wait(rid, 20))
        self.assertLess(time.time() - t0, 20)
        row = self.store.get(rid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["activity"], "stopped: over the 0.005 min cap")
        self.assertFalse(row["closed_by_user"])
        self.assertEqual([(r, s) for r, s, _ in calls], [(rid, "failed")])
        self.assertEqual(len(self.store.all()), 1)   # no retry, no fallback run

    def test_direct_run_over_the_cap_is_not_retried_even_with_retry_timeouts(self):
        self.capped_cfg()
        self.cfg["retry"] = {"enabled": True, "max": 3, "retry_timeouts": True}
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="echo", tier="light", log_runs=False)
        self.assertTrue(launcher.wait(rid, 20))
        rows = self.store.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["activity"]), ("failed", "stopped: over the 0.005 min cap"))

    def test_a_run_under_the_cap_is_untouched(self):
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="echo", tier="light", log_runs=False)
        self.assertTrue(launcher.wait(rid, 15))
        self.assertEqual(self.store.get(rid)["status"], "done")

    def test_user_stop_still_ends_as_stopped(self):
        self.cfg["tools"]["echo"]["command"][2] = "import time; print('working'); time.sleep(30)"
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="echo", tier="light", log_runs=False)
        end = time.time() + 10
        while time.time() < end and not self.store.get(rid).get("pid"):
            time.sleep(0.05)
        launcher.stop(rid, self.store)
        self.assertTrue(launcher.wait(rid, 15))
        self.assertEqual(self.store.get(rid)["status"], "stopped")

    def test_scheduler_does_not_escalate_a_capped_task(self):
        sched = Scheduler(self.cfg, self.store, self.tasks)
        sched.paused = True
        rows = [{"id": "t1", "title": "task", "prompt": "add a tooltip", "level": "light", "tool": "agy",
                 "model": "m", "files": [], "depends_on": []}]
        pid = self.tasks.add_plan("goal", "", rows, planner="test")
        self.tasks.approve(pid)
        tid = self.tasks.plan(pid)["tasks"][0]["id"]
        self.tasks.set(tid, status="running", run_id="run-cap", started=time.time())
        self.store.upsert("run-cap", source="launcher", tool="agy", status="failed",
                          activity="stopped: over the 30 min cap", ended=time.time())
        sched.finished(tid, "run-cap", "failed", 127)
        t = self.tasks.get(tid)
        self.assertEqual((t["status"], t["escalated"]), ("failed", 0))
        self.assertEqual(t["route_note"], "stopped: over the 30 min cap")


class CapRaces(unittest.TestCase):
    def job(self, minutes=30):
        import threading
        from hub.runjob import RunJob
        j = RunJob.__new__(RunJob)
        j.cfg = {"guards": {"max_minutes": {"light": minutes}}}
        j.p = {"tool": "claude", "level": "light"}
        j.run_id, j.capped = "run-capx", ""
        j._cap_timer, j._cap_off, j._cap_lock = None, True, threading.Lock()
        j.store = mock.Mock()
        return j

    def test_each_attempt_gets_its_own_clock(self):
        j = self.job()
        j._arm_cap()
        first = j._cap_timer
        self.assertFalse(j._cap_off)
        j._disarm_cap()
        self.assertTrue(j._cap_off)
        j._arm_cap()   # the bridge retry
        self.assertIsNot(j._cap_timer, first)
        self.assertFalse(j._cap_off)
        j._disarm_cap()

    def test_a_process_that_already_exited_normally_is_not_reported_capped(self):
        from hub import runstate
        j = self.job()
        proc = mock.Mock()
        proc.poll.return_value = 0
        runstate.PROCS["run-capx"] = proc
        try:
            j._cap_off = False
            j._cap_fired("stopped: over the 30 min cap")
        finally:
            runstate.PROCS.pop("run-capx", None)
        self.assertEqual(j.capped, "")
        proc.terminate.assert_not_called()

    def test_a_disarmed_watchdog_never_fires(self):
        from hub import runstate
        j = self.job()
        proc = mock.Mock()
        proc.poll.return_value = None
        runstate.PROCS["run-capx"] = proc
        try:
            j._arm_cap()
            j._disarm_cap()
            j._cap_fired("stopped: over the 30 min cap")
        finally:
            runstate.PROCS.pop("run-capx", None)
        self.assertEqual(j.capped, "")
        proc.terminate.assert_not_called()

    def test_a_running_process_over_the_cap_is_stopped_once(self):
        from hub import runstate
        j = self.job()
        proc = mock.Mock()
        proc.poll.return_value = None
        runstate.PROCS["run-capx"] = proc
        try:
            j._cap_off = False
            j._cap_fired("stopped: over the 30 min cap")
            j._cap_fired("stopped: over the 30 min cap")
        finally:
            runstate.PROCS.pop("run-capx", None)
        self.assertEqual(j.capped, "stopped: over the 30 min cap")
        proc.terminate.assert_called_once()


class CeilingStop(Base):
    """A budget / turn ceiling stop is the run's own limit: failed with a plain note, never retried or handed on."""
    def claude_says(self, script):
        self.cfg["tools"]["claude"]["command"] = [sys.executable, "-c", script, "-p", "{prompt}"]
        self.cfg["retry"] = {"enabled": True, "max": 3}

    def test_budget_error_line_in_the_log_ends_the_run_without_retry(self):
        self.claude_says("import sys; print('partial work'); print('Error: Exceeded USD budget (15)'); sys.exit(1)")
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="claude", tier="complex", log_runs=False)
        self.assertTrue(launcher.wait(rid, 20))
        rows = self.store.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["activity"]), ("failed", "stopped: over the $15 budget"))

    def test_turn_error_line_for_a_queue_run_is_failed_without_fallback(self):
        self.claude_says("import sys; print('Error: Reached max turns (150)'); sys.exit(1)")
        calls = []
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="claude", tier="complex", log_runs=False,
                                 on_finish=lambda r, s, c: calls.append(s))
        self.assertTrue(launcher.wait(rid, 20))
        self.assertEqual(calls, ["failed"])
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.get(rid)["activity"], "stopped: over the 150 turn limit")

    def test_structured_result_subtype_decides(self):
        self.cfg["tools"]["claude"]["json_output"] = True
        self.claude_says("import sys, json; print(json.dumps({'type': 'result', 'subtype': 'error_max_budget_usd', "
                         "'is_error': True, 'session_id': 'sess-12345', 'errors': []})); sys.exit(1)")
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="claude", tier="light", log_runs=False)
        self.assertTrue(launcher.wait(rid, 20))
        rows = self.store.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["activity"], "stopped: over the $5 budget")

    def test_notes_are_cap_notes_and_ordinary_failures_are_not(self):
        self.assertTrue(guards.is_cap_note("stopped: over the $15 budget"))
        self.assertTrue(guards.is_cap_note("stopped: over the 150 turn limit"))
        self.assertFalse(guards.is_cap_note("exit code 1"))
        self.assertEqual(guards.limit_note({}, {"level": "light"}, None, "all fine\n"), "")

    def test_a_plain_failure_is_still_retried(self):
        self.claude_says("import sys; print('boom'); sys.exit(1)")
        rid, _ = launcher.launch("add a tooltip", self.cfg, self.store, tool="claude", tier="light", log_runs=False)
        self.assertTrue(launcher.wait(rid, 20))
        end = time.time() + 20
        while time.time() < end and len(self.store.all()) < 2:
            time.sleep(0.1)
        self.assertGreaterEqual(len(self.store.all()), 2)


class QueueCeiling(Base):
    def setUp(self):
        super().setUp()
        self.sched = Scheduler(self.cfg, self.store, self.tasks)
        self.n = 0

    def started_today(self, k):
        for _ in range(k):
            self.n += 1
            pid = self.tasks.add_plan("g", "", [{"id": f"t{self.n}", "title": "t", "prompt": "p", "level": "light",
                                                 "tool": "echo", "model": "m", "files": [], "depends_on": []}])
            self.tasks.approve(pid)
            tid = self.tasks.plan(pid)["tasks"][0]["id"]
            self.tasks.set(tid, status="done", started=time.time(), ended=time.time())

    def test_run_count_pauses_with_a_reason_and_stays_paused(self):
        self.cfg["guards"] = {"queue": {"max_runs_per_day": 3, "max_cost_usd_per_day": 0}}
        self.started_today(2)
        self.sched.tick()
        self.assertFalse(self.sched.paused)
        self.started_today(1)
        self.sched.tick()
        self.assertTrue(self.sched.paused)
        self.assertIn("queue ceiling", self.sched.paused_reason)
        state = queue_api.queue_state(self.cfg, self.tasks, self.sched)
        self.assertTrue(state["paused"])
        self.assertIn("3 queue runs", state["paused_reason"])
        for _ in range(3):   # never lifts by itself
            self.sched.tick()
        self.assertTrue(self.sched.paused)

    def test_resume_allows_another_full_allowance_and_then_pauses_again(self):
        self.cfg["guards"] = {"queue": {"max_runs_per_day": 2, "max_cost_usd_per_day": 0}}
        self.started_today(2)
        self.sched.tick()
        self.assertTrue(self.sched.paused)
        self.sched.resume()
        self.assertEqual(self.sched.paused_reason, "")
        self.sched.tick()
        self.assertFalse(self.sched.paused)         # the owner's resume is respected
        self.started_today(2)
        self.sched.tick()
        self.assertTrue(self.sched.paused)

    def test_estimated_cost_ceiling(self):
        self.cfg["guards"] = {"queue": {"max_runs_per_day": 0, "max_cost_usd_per_day": 40}}
        data = {"day_start": usage.local_midnight(), "claude": {"cost_today": 55.0}}
        with mock.patch.object(usage, "cached", return_value=data):
            self.sched.tick()
            self.assertTrue(self.sched.paused)
            self.assertIn("$55", self.sched.paused_reason)
            self.sched.resume()
            self.sched.tick()
            self.assertFalse(self.sched.paused)     # $55 is accepted; another $40 is allowed
            data["claude"]["cost_today"] = 100.0
            self.sched.tick()
            self.assertTrue(self.sched.paused)

    def test_stale_or_missing_usage_data_never_pauses(self):
        self.cfg["guards"] = {"queue": {"max_runs_per_day": 0, "max_cost_usd_per_day": 1}}
        with mock.patch.object(usage, "cached", return_value=None):
            self.sched.tick()
        with mock.patch.object(usage, "cached", return_value={"day_start": 1.0, "claude": {"cost_today": 999}}):
            self.sched.tick()
        self.assertFalse(self.sched.paused)

    def test_ceiling_can_be_turned_off(self):
        self.cfg["guards"] = {"enabled": False}
        self.started_today(500)
        self.sched.tick()
        self.assertFalse(self.sched.paused)

    def test_owner_pause_and_resume_reasons(self):
        self.sched.pause()
        self.assertEqual(queue_api.queue_state(self.cfg, self.tasks, self.sched)["paused_reason"], "paused by you")
        self.sched.resume()
        self.assertEqual(queue_api.queue_state(self.cfg, self.tasks, self.sched)["paused_reason"], "")


if __name__ == "__main__":
    unittest.main()
