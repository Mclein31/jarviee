"""sync_run_progress is called for every launcher row on every /api/state poll: it must be incremental."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir
from hub import launcher, orphans, progress, runstate
from hub.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.store = Store(str(self.tmp / "db.sqlite"))
        self.log = self.tmp / "run-x.log"
        progress._state.clear()

    def tearDown(self):
        progress._state.clear()
        self._t.__exit__()

    def row(self, rid="run-x", **kw):
        fields = dict(source="launcher", tool="claude", task="do a thing here", status="running",
                      log_path=str(self.log), pid=os.getpid(), steps=0)
        fields.update(kw)
        self.store.upsert(rid, **fields)
        return self.store.get(rid)

    def sync(self, rid="run-x"):
        a = self.store.get(rid)
        progress.sync_run_progress(a, self.store)
        return self.store.get(rid)


class LogIncremental(Base):
    def test_only_new_bytes_are_read_and_steps_follow(self):
        self.log.write_text("$ claude\n\nRan tool 1\nplain line\n")
        self.row()
        self.assertEqual(self.sync()["steps"], 1)
        with mock.patch.object(progress, "_read_from", wraps=progress._read_from) as rd:
            with open(self.log, "a") as f:
                f.write("Ran tool 2\nRan tool 3\n")
            self.assertEqual(self.sync()["steps"], 3)
            offsets = [c.args[1] for c in rd.call_args_list]
        self.assertEqual(offsets, [len("$ claude\n\nRan tool 1\nplain line\n")])   # not from byte 0

    def test_half_written_line_waits_for_its_newline(self):
        self.log.write_text("$ claude\n\nRan tool 1\nRan too")
        self.row()
        self.assertEqual(self.sync()["steps"], 1)
        with open(self.log, "a") as f:
            f.write("l 2\n")
        self.assertEqual(self.sync()["steps"], 2)

    def test_output_without_tool_lines_counts_one_step(self):
        self.log.write_text("$ echo\n\nhello\n")
        self.row(tool="echo")
        self.assertEqual(self.sync()["steps"], 1)

    def test_a_big_backlog_is_one_write_and_keeps_updated(self):
        self.log.write_text("".join(f"Ran tool {i}\n" for i in range(500)))
        r = self.row(updated=1000.0)
        self.assertEqual(self.sync()["steps"], 500)
        self.assertEqual(self.store.get("run-x")["updated"], r["updated"])


class FinishedRuns(Base):
    def test_a_settled_finished_run_costs_one_stat(self):
        self.log.write_text("$ claude\n\nRan tool 1\nRan tool 2\n")
        self.row(status="done")
        self.assertEqual(self.sync()["steps"], 2)           # counted once
        with mock.patch.object(progress, "_read_from") as rd, mock.patch.object(self.store, "upsert") as up, \
                mock.patch.object(self.store, "bump_steps") as bump:
            for _ in range(5):
                self.sync()
            rd.assert_not_called()
            up.assert_not_called()
            bump.assert_not_called()

    def test_a_finished_run_the_worker_already_counted_is_not_read_at_all(self):
        self.log.write_text("Ran tool 1\n")
        self.row(status="done", steps=4)                    # e.g. the first poll after a hub restart
        with mock.patch.object(progress, "_read_from") as rd:
            self.sync()
            self.sync()
            rd.assert_not_called()

    def test_a_finished_run_whose_log_grew_is_read_again(self):
        self.log.write_text("Ran tool 1\n")
        self.row(status="done")
        self.assertEqual(self.sync()["steps"], 1)
        time.sleep(0.01)
        with open(self.log, "a") as f:
            f.write("Ran tool 2\n")
        self.assertEqual(self.sync()["steps"], 2)

    def test_a_truncated_log_does_not_lose_steps_or_raise(self):
        self.log.write_text("Ran tool 1\nRan tool 2\n")
        self.row()
        self.assertEqual(self.sync()["steps"], 2)
        self.log.write_text("x\n")
        self.assertEqual(self.sync()["steps"], 2)

    def test_missing_log_is_fine(self):
        self.row(status="done")
        self.assertEqual(self.sync()["steps"], 0)


class Transcript(Base):
    def setUp(self):
        super().setUp()
        self.tp = self.tmp / "transcript.jsonl"

    def call(self, name, n=1):
        return json.dumps({"tool_calls": [{"name": name, "args": {"CommandLine": f"run {n}"}}]}) + "\n"

    def test_searches_are_throttled_while_running_and_stop_once_found(self):
        self.row(tool="agy")
        with mock.patch.object(launcher, "_find_agy_transcript", return_value=None) as find:
            for _ in range(10):
                self.sync()
            self.assertEqual(find.call_count, 1)             # not a directory scan per poll
            progress._state["run-x"].next_search = 0         # the wait is over
            self.sync()
            self.assertEqual(find.call_count, 2)
        self.tp.write_text(self.call("run_command"))
        progress._state["run-x"].next_search = 0
        with mock.patch.object(launcher, "_find_agy_transcript", return_value=self.tp) as find:
            self.assertEqual(self.sync()["steps"], 1)
            for _ in range(5):
                self.sync()
            self.assertEqual(find.call_count, 1)             # remembered

    def test_finished_run_searches_once_then_never(self):
        self.row(tool="agy", status="done")
        with mock.patch.object(launcher, "_find_agy_transcript", return_value=None) as find:
            for _ in range(6):
                self.sync()
            self.assertEqual(find.call_count, 1)

    def test_transcript_is_read_from_its_offset_and_appended_to_the_log(self):
        self.log.write_text("$ agy\n\n")
        self.tp.write_text(self.call("run_command", 1) + self.call("view_file", 2))
        self.row(tool="agy")
        with mock.patch.object(launcher, "_find_agy_transcript", return_value=self.tp):
            self.assertEqual(self.sync()["steps"], 2)
            self.assertIn("Ran run_command: run 1", self.log.read_text())
            with mock.patch.object(progress, "_read_from", wraps=progress._read_from) as rd:
                with open(self.tp, "a") as f:
                    f.write(self.call("run_command", 3) + '{"tool_calls": [')   # the last line is half written
                row = self.sync()
                self.assertEqual(row["steps"], 3)
                self.assertEqual([c.args[1] for c in rd.call_args_list if c.args[0] == self.tp][0],
                                 len(self.call("run_command", 1) + self.call("view_file", 2)))
            with open(self.tp, "a") as f:
                f.write(']}\n')
            self.assertEqual(self.sync()["steps"], 3)         # the completed line has no tool call object here

    def test_running_row_activity_follows_the_last_tool(self):
        self.log.write_text("$ agy\n\n")
        self.tp.write_text(self.call("run_command"))
        self.row(tool="agy", activity="working")
        with mock.patch.object(launcher, "_find_agy_transcript", return_value=self.tp):
            self.assertEqual(self.sync()["activity"], "ran run_command")

    def test_find_transcript_by_task_text(self):
        brain = self.tmp / "brain"
        d = brain / "c1" / ".system_generated" / "logs"
        d.mkdir(parents=True)
        (d / "transcript.jsonl").write_text(json.dumps({"msg": "please refactor the widget module"}) + "\n")
        with mock.patch.dict(os.environ, {"JARVIEE_AGY_BRAIN_ROOT": str(brain)}):
            self.assertEqual(launcher._find_agy_transcript("refactor the widget module", time.time()),
                             d / "transcript.jsonl")
            self.assertIsNone(launcher._find_agy_transcript("something else entirely", time.time()))


class Orphans(Base):
    def test_running_row_with_a_dead_process_is_settled(self):
        self.log.write_text("Ran tool 1\n")
        self.row(pid=2 ** 22 + 12345)                       # no such process, no worker thread
        row = self.sync()
        # it had output, but no worker recorded an exit: not verifiably finished, so never "done"
        self.assertEqual((row["status"], row["activity"]), ("failed", orphans.LOST_NOTE))
        self.assertTrue(row["ended"])
        self.row("run-y", pid=2 ** 22 + 12345, log_path=str(self.tmp / "none.log"))
        row = self.sync("run-y")
        self.assertEqual((row["status"], row["activity"]), ("failed", orphans.LOST_NOTE))

    def test_a_managed_run_is_left_alone(self):
        self.row(pid=2 ** 22 + 12345)
        runstate.PROCS["run-x"] = object()
        try:
            self.assertEqual(self.sync()["status"], "running")
        finally:
            runstate.PROCS.pop("run-x", None)


class OldNamesStillWork(unittest.TestCase):
    def test_launcher_reexports_share_state_with_the_new_modules(self):
        self.assertIs(launcher._procs, runstate.PROCS)
        self.assertIs(launcher.LIVE_RUNS, runstate.LIVE_RUNS)
        self.assertIs(launcher._threads, runstate.THREADS)
        for name in ("strip_tty", "sync_run_progress", "stop_plan", "stop_status", "stop", "stop_with_reason",
                     "proc_name", "_is_agent", "closed_by_user", "auto_commit", "run_diff", "clean_commit_message",
                     "plan", "build_argv", "build_bridge_argv", "build_prompt", "readonly_args", "interactive_argv",
                     "pick_effort", "_observe_log_steps", "_find_agy_transcript", "_run_in_pane", "_pane_title",
                     "_log_note", "_git_status", "_git_env", "is_live", "launch", "wait", "prune_logs", "_finish",
                     "agy_soft_denied", "LIVE", "FINISHED_CLOSED", "LIVE_NOTE", "TURN_DONE_RE", "AGENT_NAMES",
                     "STALE_AFTER", "PLACEHOLDER", "ANSI_RE", "TOOL_LINE_RE", "AI_TERMS_RE", "CONVENTIONAL_RE"):
            self.assertTrue(hasattr(launcher, name), name)

    def test_patching_launcher_names_reaches_the_moved_code(self):
        a = {"id": "r", "source": "launcher", "status": "running", "pid": 5, "tool": "claude"}
        with mock.patch.object(launcher, "proc_name", return_value="claude"):
            self.assertEqual(launcher.stop_status(a), (True, 5, ""))
            self.assertEqual(launcher.stop_plan(a)[0], "stop")
        with mock.patch.object(launcher, "proc_name", return_value=""):
            self.assertFalse(launcher.stop_status(a)[0])


if __name__ == "__main__":
    unittest.main()
