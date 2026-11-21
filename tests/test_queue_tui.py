"""Queue and pipeline runs open the real claude / agy TUI in their pane, not a blank one-shot print run.

The owner's report (2026-11-14): pipeline panes sat blank (`claude -p` prints nothing until it exits) and plain,
not the claude / agy terminal they know. Now every pane run is a live session; a queue run is `unattended`: its one
turn is the run, the pane closes itself after it, and the pipeline reads the clean answer from the agent's transcript.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import finaltext, launcher, runstate
from hub.server import handle_hook
from hub.store import Store


class InteractiveArgv(unittest.TestCase):
    def test_unattended_read_only_claude_cannot_stop_on_the_plan_dialog(self):
        argv = ["claude", "-p", "research it", "--permission-mode", "plan"]
        self.assertEqual(launcher.interactive_argv("claude", argv, unattended=True),
                         ["claude", "research it", "--permission-mode", "plan", "--disallowedTools", "ExitPlanMode"])
        self.assertEqual(launcher.interactive_argv("claude", argv), ["claude", "research it", "--permission-mode", "plan"])

    def test_unattended_writable_runs_are_unchanged(self):
        argv = ["claude", "-p", "fix it", "--permission-mode", "auto"]
        self.assertEqual(launcher.interactive_argv("claude", argv, unattended=True),
                         ["claude", "fix it", "--permission-mode", "auto"])


class FinalText(unittest.TestCase):
    def test_claude_answer_is_the_text_after_the_last_tool_result(self):
        with TempDir() as tmp:
            d = Path(tmp) / "-Users-me-proj"
            d.mkdir()
            rows = [
                {"type": "user", "message": {"content": "do it"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."},
                                                              {"type": "tool_use", "name": "Read"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "## Findings"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "All good."}]}},
                {"type": "system", "subtype": "stop"},
            ]
            (d / "abc-123.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            self.assertEqual(finaltext.claude_text("abc-123", projects_dir=tmp), "## Findings\n\nAll good.")
            self.assertEqual(finaltext.claude_text("missing", projects_dir=tmp), "")

    def test_agy_answer_is_the_last_model_response(self):
        with TempDir() as tmp:
            tp = Path(tmp) / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in [
                {"source": "USER", "type": "USER_INPUT", "content": "do it"},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "first"},
                {"source": "MODEL", "type": "GENERIC", "content": "Created file."},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Done: added X."},
            ]), encoding="utf-8")
            with mock.patch.object(finaltext.progress, "find_agy_transcript", return_value=tp):
                self.assertEqual(finaltext.final_text("agy", ["agy", "-i", "do it"], "do it", 0), "Done: added X.")

    def test_session_id_and_the_log_marker(self):
        self.assertEqual(finaltext.session_of(["claude", "x", "--session-id", "s-1", "--model", "m"]), "s-1")
        with TempDir() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("\x1b[2J╭─ Claude Code ─╮ lots of screen redraws\n", encoding="utf-8")
            self.assertTrue(finaltext.append(log, "The answer."))
            self.assertEqual(finaltext.after_mark(log.read_text(encoding="utf-8")), "The answer.\n")
        self.assertEqual(finaltext.after_mark("plain print output\n"), "plain print output\n")


class UnattendedStop(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.store.upsert("run-q1", source="launcher", tool="agy", task="research", status="running",
                          interactive=runstate.LIVE_UNATTENDED, job_id="job-1", job_no=1)

    def tearDown(self):
        self._t.__exit__()

    def test_a_queue_turn_is_done_even_with_a_question_or_no_message(self):
        for msg in (None, "Which database should I use, Postgres or SQLite?"):
            self.store.upsert("run-q1", status="running", activity="working")
            data = {"jarvieeRunId": "run-q1", "jarvieeTool": "agy"}
            if msg is not None:
                data["last_assistant_message"] = msg
            handle_hook("Stop", data, self.cfg, self.store)
            row = self.store.get("run-q1")
            # no agy transcript here, so its background tasks weren't checked (the pane checks its footer instead)
            self.assertEqual((row["status"], row["activity"]), ("done", runstate.FINISHED_UNCHECKED), msg)


if __name__ == "__main__":
    unittest.main()
