"""Acceptance: the speech module turns a row's result-summary fields (title, status, activity, steps, files)
into a short, sanitised spoken string. The helpers are pure: no I/O, no process calls, no mutation of the input."""
import copy
import subprocess
import subprocess as _sp
import unittest
from unittest import mock

from hub import voice

SECRETS = [
    "api_key=sk-ABCDEF1234567890ABCDEF1234567890",
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "ghp_" + "a" * 36,
    "token=abc123secret",
    "password: hunter2",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
]
LONG = "Which branch should I use for the release? " + "and then some more words " * 200
HUGE_BLOB = "x" * 5000
SECRET_FRAGMENTS = ("sk-ABCDEF", "sk-ant", "ghp_", "abc123secret", "hunter2", "eyJhbGci", "payload.sig")


class ImportAndPurity(unittest.TestCase):
    def test_module_imports_in_a_fresh_interpreter(self):
        r = _sp.run(["python3", "-c", "import hub.voice"], capture_output=True, text=True,
                    cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_helpers_exist_and_are_callable(self):
        for name in ("sanitise_spoken", "build_reason", "build_briefing", "build_phrase"):
            self.assertTrue(callable(getattr(voice, name, None)), name)

    def test_helpers_are_pure(self):
        row = {"title": "Fix router", "status": "failed", "activity": SECRETS[0] + " " + LONG,
               "steps": 3, "files": ["a/b.py"], "started": 1, "ended": 70}
        before = copy.deepcopy(row)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("popen")), \
                mock.patch("builtins.open", side_effect=AssertionError("file io")), \
                mock.patch("time.time", side_effect=AssertionError("clock")):
            a1 = voice.build_briefing(row)
            a2 = voice.build_briefing(row)
            r1 = voice.build_reason(row)
            r2 = voice.build_reason(row)
            s1 = voice.sanitise_spoken(row["activity"])
            s2 = voice.sanitise_spoken(row["activity"])
        self.assertEqual(row, before, "input row was mutated")
        self.assertEqual((a1, r1, s1), (a2, r2, s2), "not deterministic")


class SanitisedAndShort(unittest.TestCase):
    def test_secret_looking_activity_is_never_spoken(self):
        for sec in SECRETS:
            for status in ("waiting", "failed"):
                row = {"title": "Fix router", "status": status, "activity": f"stopped: {sec}"}
                for out in (voice.build_reason(row), voice.build_briefing(row)):
                    self.assertIsInstance(out, str)
                    for frag in SECRET_FRAGMENTS:
                        self.assertNotIn(frag, out, f"{status}: leaked {frag!r} from {sec!r}")

    def test_very_long_activity_is_capped(self):
        for blob in (LONG, HUGE_BLOB, LONG + SECRETS[0]):
            s = voice.sanitise_spoken(blob)
            self.assertLessEqual(len(s), voice.REASON_MAX)
            self.assertIsInstance(s, str)
            row = {"title": "Fix router", "status": "waiting", "activity": blob}
            self.assertLessEqual(len(voice.build_reason(row)), voice.REASON_MAX)
            self.assertLessEqual(len(voice.build_briefing(row)), 80 + voice.REASON_MAX + 60)

    def test_long_title_is_capped_too(self):
        out = voice.build_briefing({"title": LONG, "status": "done"})
        self.assertLess(len(out), 200)

    def test_long_failed_activity_never_reads_the_raw_note(self):
        out = voice.build_reason({"status": "failed", "activity": LONG})
        self.assertLessEqual(len(out), voice.REASON_MAX)
        self.assertEqual(out, voice.UNKNOWN_REASON)

    def test_waiting_long_activity_cut_on_a_word_boundary(self):
        out = voice.build_reason({"status": "waiting", "activity": LONG})
        self.assertTrue(out.startswith("Which branch should I use"))
        self.assertTrue(LONG.startswith(out), "cut must not split a word")
        self.assertNotIn("\n", out)

    def test_control_chars_urls_and_whitespace_are_removed(self):
        s = voice.sanitise_spoken("fail\x1b[31m red\x1b[0m\n\n at   https://evil.example/x?a=1 ​done\x00")
        self.assertEqual(s, "fail red at done")

    def test_unusable_inputs_give_short_strings_not_errors(self):
        for v in (None, "", "   ", 0, [], {}, b"", "\x00\x01"):
            self.assertEqual(voice.sanitise_spoken(v), "")
        self.assertEqual(voice.build_reason({}), "")
        self.assertEqual(voice.build_reason(None), "")
        self.assertIsNone(voice.build_briefing({}))
        self.assertIsNone(voice.build_briefing({"title": "x", "status": "running"}))


class SummaryFields(unittest.TestCase):
    def test_done_summary_carries_title_duration_steps_files(self):
        t = voice.build_briefing({"title": "Fix router", "status": "done", "started": 100, "ended": 190, "steps": 1,
                                  "files": ["hub/router.py"]})
        for want in ("Fix router", "done", "1 minute", "1 step.", "It changed 1 file."):
            self.assertIn(want, t)
        self.assertNotIn("router dot py", t)   # file names are never read out


class SpokenReports(unittest.TestCase):
    """The owner's complaint (2026-11-13): the report read the prompt back and read out file paths."""

    def test_paths_and_file_names_are_never_spoken(self):
        said = voice.speakable("I updated `hub/voice.py` and tests/test_voice.py, then fixed router.py in hub/. Tests pass.")
        for bad in ("/", ".py", "hub", "dot"):
            self.assertNotIn(bad, said)
        self.assertTrue(said.endswith("Tests pass."))
        self.assertEqual(voice.speakable("Version 3.5 runs 2.5x faster, e.g. on M5."), "Version 3.5 runs 2.5x faster, e.g. on M5.")

    def test_a_summary_replaces_the_prompt_in_the_spoken_report(self):
        from hub import batch
        m = [{"id": "run-1", "job_id": "", "outcome": "done", "workspace": "/x/agent-hub", "files": ["hub/a.py"],
              "title": "Do a deep refactor on this project make it efficient and improve it", "started": 0, "ended": 60}]
        spoken = batch.report_speech(m, {"run-1#": "I split the server into small modules; all tests pass."})
        self.assertIn("Done in agent-hub: I split the server into small modules; all tests pass.", spoken)
        self.assertNotIn("deep refactor", spoken)
        no_summary = batch.report_speech(m)
        self.assertNotIn("a.py", no_summary)
        self.assertIn("changed 1 file", no_summary)
        self.assertIn("a.py", batch.report_text(m))   # the written report keeps the names

    def test_a_claude_session_is_summarised_from_its_own_transcript(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from hub import runcost
        lines = [{"type": "user", "message": {"role": "user", "content": "old job"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "Old answer."}]}},
                 {"type": "user", "message": {"role": "user", "content": "fix the volume"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."}, {"type": "tool_use"}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "I fixed the volume; tests pass."}]}}]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            f.write_text("\n".join(json.dumps(x) for x in lines))
            with mock.patch.object(runcost, "claude_files", return_value=[f]):
                tail = voice.session_tail({"id": "claude-abc", "tool": "claude", "source": "hook"})
        self.assertIn("I fixed the volume; tests pass.", tail)
        self.assertIn("Looking.", tail)
        self.assertNotIn("Old answer", tail)                # only the current job
        self.assertEqual(voice.session_tail({"id": "run-1", "tool": "claude", "source": "launcher"}), "")

    def test_done_with_no_files_says_so(self):
        self.assertIn("didn't change any files", voice.build_briefing({"title": "Fix", "status": "done"}))

    def test_failed_reason_is_mapped_to_plain_phrases(self):
        cases = {"agy refused a shell command: rm": "blocked from running a shell command",
                 "quota exceeded": "usage limit",
                 "exited with code 2": "exited with an error"}
        for note, phrase in cases.items():
            t = voice.build_briefing({"title": "Fix", "status": "failed", "activity": note})
            self.assertIn("It failed", t)
            self.assertIn(phrase, t)

    def test_waiting_summary_says_it_needs_you_with_reason(self):
        t = voice.build_briefing({"title": "Fix", "status": "waiting", "activity": "Which branch?"})
        self.assertIn("needs you", t)
        self.assertIn("Which branch?", t)

    def test_phrase_stays_metadata_only(self):
        p = voice.build_phrase({"agent_no": 2, "task_id": "R1-1", "status": "failed", "activity": SECRETS[0]})
        self.assertEqual(p, "Agent 2, running R1-1, failed")


if __name__ == "__main__":
    unittest.main()
