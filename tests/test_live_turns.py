"""A live CLI window's turn: done closes it, needs you keeps it (hub/ingest.py Stop, hooks/report.py, hooks/pane_runner.py).

The owner's report (2026-11-13): finished CLI windows never closed, and done vs needs you was not told apart: every
turn end of a live pane was "turn finished, waiting for you", and the session never exits on its own.
"""
import io
import json
import sys
import unittest
from pathlib import Path

from helpers import ROOT, TempDir, make_cfg
from hub import launcher
from hub.server import handle_hook
from hub.store import Store


class LiveTurnEnd(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.store.upsert("run-live1", source="launcher", tool="claude", task="fix the login button", status="running",
                          interactive=1, job_id="job-1", job_no=1)
        launcher.LIVE_RUNS.add("run-live1")

    def tearDown(self):
        launcher.LIVE_RUNS.discard("run-live1")
        self._t.__exit__()

    def stop(self, msg=None, rid="run-live1", **extra):
        data = {"jarvieeRunId": rid, "jarvieeTool": "claude", **extra}
        if msg is not None:
            data["last_assistant_message"] = msg
        handle_hook("Stop", data, self.cfg, self.store)
        return self.store.get(rid)

    def test_a_finished_turn_is_done(self):
        row = self.stop("I fixed the login button and added a test. All 42 tests pass.")
        self.assertEqual((row["status"], row["activity"]), ("done", "finished"))
        self.assertTrue(row["ended"])

    def test_a_polite_sign_off_question_is_still_done(self):
        row = self.stop("Fixed the button; tests pass. Want me to also tidy the CSS?")
        self.assertEqual(row["status"], "done")

    def test_a_real_question_or_blocker_needs_you(self):
        for msg in ("Which database should I use for this, Postgres or SQLite?",
                    "I need your approval before I proceed with deleting the old table.",
                    "I'm blocked: the API key is missing."):
            self.store.upsert("run-live1", status="running", activity="working")
            row = self.stop(msg)
            self.assertEqual(row["status"], "waiting", msg)
            self.assertTrue(row["activity"].startswith("needs you"), row["activity"])

    def test_no_final_message_keeps_the_old_safe_behaviour(self):
        row = self.stop(None)
        self.assertEqual((row["status"], row["activity"]), ("waiting", "turn finished, waiting for you"))

    def test_a_session_started_in_claude_code_is_never_closed_this_way(self):
        handle_hook("Stop", {"session_id": "s1", "jarvieeTool": "claude", "last_assistant_message": "All done."},
                    self.cfg, self.store)
        self.assertEqual(self.store.get("claude-s1")["status"], "waiting")   # only Jarviee panes close

    def test_a_new_prompt_after_done_starts_the_next_job(self):
        self.stop("Done, tests pass.")
        handle_hook("UserPromptSubmit", {"jarvieeRunId": "run-live1", "jarvieeTool": "claude",
                                         "prompt": "now fix the signup page"}, self.cfg, self.store)
        row = self.store.get("run-live1")
        self.assertEqual(row["status"], "running")              # the pane sees this and cancels its close


class StopAfterFinish(unittest.TestCase):
    def test_stopping_a_finished_run_keeps_its_result(self):
        from hub import stopper
        with TempDir() as tmp:
            cfg, _ = make_cfg(tmp)
            store = Store(cfg["db_path"])
            store.upsert("run-fin1", source="launcher", tool="claude", status="done", activity="finished")
            self.assertEqual(stopper.stop_with_reason("run-fin1", store), (True, "already finished"))
            self.assertEqual((store.get("run-fin1")["status"], store.get("run-fin1")["activity"]), ("done", "finished"))


class ReporterKeepsTheClosingMessage(unittest.TestCase):
    def test_stop_keeps_the_end_of_the_final_message_and_prints_nothing(self):
        import contextlib
        from unittest.mock import patch
        sys.path.insert(0, str(Path(ROOT) / "hooks"))
        import report
        sent = []

        class Resp:
            def read(self):
                return b"{}"

        def fake_open(req, timeout=0):
            sent.append(json.loads(req.data))
            return Resp()

        payload = {"session_id": "s", "hook_event_name": "Stop", "last_assistant_message": "x" * 3000 + " All done."}
        with patch.object(report, "_OPEN", fake_open), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                patch.object(sys, "argv", ["report.py", "Stop", "claude"]), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            report.main()
        msg = sent[0]["last_assistant_message"]
        self.assertTrue(msg.endswith("All done."))
        self.assertEqual(len(msg), 1500)
        self.assertEqual(printed.getvalue(), "")


if __name__ == "__main__":
    unittest.main()


class AgyLiveTurn(unittest.TestCase):
    """agy's Stop hook has no last_assistant_message: the closing message comes from its transcript (2026-11-14: a
    'reply with test 1' run answered 'test 1' but stayed 'needs you', so its window never closed)."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.store.upsert("run-agy1", source="launcher", tool="agy", task="reply with test 1", status="running",
                          interactive=1, started=1.0)
        launcher.LIVE_RUNS.add("run-agy1")

    def tearDown(self):
        launcher.LIVE_RUNS.discard("run-agy1")
        self._t.__exit__()

    def stop(self, reply):
        from unittest.mock import patch
        with patch("hub.ingest.finaltext.agy_text", return_value=reply), patch("hub.ingest.time.sleep"):
            handle_hook("Stop", {"jarvieeRunId": "run-agy1", "jarvieeTool": "agy", "terminationReason": "NO_TOOL_CALL"},
                        self.cfg, self.store)
        return self.store.get("run-agy1")

    def test_a_plain_answer_is_done(self):
        self.assertEqual(self.stop("test 1")["status"], "done")

    def test_a_real_question_needs_you(self):
        row = self.stop("Which colour do you prefer for the dashboard, red or blue?")
        self.assertEqual(row["status"], "waiting")
        self.assertTrue(row["activity"].startswith("needs you"))

    def test_no_transcript_text_stays_safe(self):
        self.assertEqual(self.stop("")["status"], "waiting")


class ClosingMessageJudgement(unittest.TestCase):
    def kind(self, text):
        from hub import outcome
        return outcome.classify(0, text)[0]

    def test_done(self):
        for m in ("test 1", "Done. Want me to also add tests?", "I've updated it.\nShould I commit?",
                  "Fixed the bug and all tests pass. Anything else?"):
            self.assertEqual(self.kind(m), "done", m)

    def test_needs_you(self):
        for m in ("Red or blue?", "Do you want me to delete the old table?", "What would you like to do next?",
                  "The file is fixed.\n\nShould I proceed with the migration?", "Updated. Which database should I use?"):
            self.assertEqual(self.kind(m), "needs_you", m)
