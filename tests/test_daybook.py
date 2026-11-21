"""/endwork: jobs are summarised quietly as they finish and read out once, on request (hub/daybook.py)."""
import time
import unittest

from helpers import TempDir, make_cfg
from hub import alerts, daybook
from hub.store import Store


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def speak(self, text, **kw):
        self.said.append((text, kw))
        return True


class DayBook(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        now = time.time()
        self.store.upsert("run-a", tool="claude", project="agent-hub", title="Fix status", status="done", started=now - 600, ended=now - 60, job_id="j1")
        self.store.upsert("run-b", tool="agy", project="agent-hub", title="Add chip", status="failed", started=now - 300, ended=now - 30, job_id="j2")
        self.store.upsert("run-c", tool="claude", project="other", title="Still going", status="running", started=now - 10)
        self.store.upsert("run-d", tool="claude", project="other", title="Needs input", status="waiting", started=now - 10)
        self.calls = []
        self.book = daybook.Daybook(self.cfg, self.store, summarise=self._sum)

    def _sum(self, row, status):
        self.calls.append(row["id"])
        return f"I did {row['title']}. Extra detail here."

    def tearDown(self):
        self._t.__exit__()

    def test_only_finished_jobs_are_summarised_once(self):
        self.assertEqual(self.book.sweep(), 2)
        self.assertEqual(self.book.sweep(), 0)
        self.assertEqual(sorted(self.calls), ["run-a", "run-b"])

    def test_report_covers_the_day_and_speaks_once(self):
        sp = FakeSpeaker()
        rep = self.book.endwork(speaker=sp)
        self.assertEqual((rep["done"], rep["failed"], rep["waiting"]), (1, 1, 1))
        self.assertIn("I did Fix status.", rep["text"])
        self.assertNotIn("Extra detail", rep["text"])           # one sentence per job when spoken
        self.assertIn("It failed: I did Add chip.", rep["text"])
        self.assertIn("1 still needs you", rep["text"])
        self.assertEqual(len(sp.said), 1)
        self.assertEqual(sp.said[0][1]["event"], "endwork")

    def test_nothing_finished(self):
        other = daybook.Daybook(self.cfg, Store(self.tmp + "/x.db"), summarise=self._sum)
        self.assertEqual(other.report()["jobs"], 0)

    def test_command_matching(self):
        for t in ("/endwork", "/endowrk", " /end work", "/end-work now"):
            self.assertTrue(daybook.is_endwork(t), t)
        for t in ("endwork", "/endworks", "fix /endwork"):
            self.assertFalse(daybook.is_endwork(t), t)

    def test_live_reports_off_silences_job_speech_but_not_endwork(self):
        sp = FakeSpeaker()
        self.cfg["voice"] = {"live_reports": False}
        w = alerts.AlertWatcher(self.cfg, self.store, runner=lambda argv: None, speaker=sp)
        self.assertIsNone(w.speaker)
        self.cfg["voice"] = {"live_reports": True}
        self.assertIs(w.speaker, sp)
        self.cfg["voice"] = {"live_reports": False}
        self.assertTrue(self.book.endwork(speaker=sp)["spoken"])

    def test_email_separates_projects_cleanly(self):
        now = time.time()
        self.store.upsert("run-e", tool="claude", project="alpha", title="Ship login", status="done",
                          started=now - 200, ended=now - 10, job_id="j3")
        self.book.flush()
        rep = self.book.report()
        txt = daybook.email_text(rep)
        self.assertIn("── agent-hub (2) ──", txt)
        self.assertIn("── alpha (1) ──", txt)
        self.assertIn("✅ Fix status", txt)
        self.assertIn("❌ Add chip", txt)
        self.assertIn("⏳ 1 still needs you.", txt)
        html_out = daybook.email_html(rep)
        self.assertIn("agent-hub", html_out)
        self.assertIn("alpha", html_out)
        self.assertIn("2 jobs", html_out)
        self.assertIn("1 job", html_out)
        sent = []
        self.assertTrue(self.book.send_summary(rep, send=lambda *a, **kw: sent.append((a, kw))))
        self.assertEqual(sent[0][0][2], txt)


class ClosedWindows(unittest.TestCase):
    """Closing a CLI window (a done one, or a false "needs you") must never erase the work from the day's record."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.now = time.time()
        self.book = daybook.Daybook(self.cfg, self.store, summarise=lambda row, st: f"Did {row['title']}.")

    def tearDown(self):
        self._t.__exit__()

    def _row(self, rid, **kw):
        base = dict(tool="claude", project="agent-hub", title=rid, started=self.now - 300, ended=self.now - 20,
                    job_id="j-" + rid, steps=12)
        base.update(kw)
        self.store.upsert(rid, **base)

    def test_a_window_closed_by_hand_is_still_in_the_day(self):
        self._row("run-closed", status="stopped", closed_by_user=1, activity="stopped (closed in its pane)")
        self.assertEqual(self.book.sweep(), 1)
        rep = self.book.report()
        self.assertEqual((rep["done"], rep["closed"]), (0, 1))
        self.assertIn("Did run-closed.", rep["text"])
        self.assertIn("1 closed early", rep["head"])
        self.assertIn("⏹ run-closed", daybook.email_text(rep))
        self.assertIn("Closed", daybook.email_html(rep))

    def test_a_false_needs_you_closed_by_hand_is_counted_and_not_still_waiting(self):
        self._row("run-false", status="waiting", closed_by_user=1, ended=None, updated=self.now - 5,
                  activity="needs you: asked a question")
        self.book.sweep()
        rep = self.book.report()
        self.assertEqual(rep["closed"], 1)
        self.assertEqual(rep["waiting"], 0)                      # a closed window is history, not a request
        self.assertNotIn("still need", rep["text"])

    def test_a_still_open_needs_you_is_not_a_finished_job(self):
        self._row("run-open", status="waiting", ended=None, updated=self.now - 5)
        self.assertEqual(self.book.sweep(), 0)
        self.assertEqual(self.book.report()["waiting"], 1)

    def test_a_closed_window_with_no_work_is_noise(self):
        self._row("run-empty", status="stopped", closed_by_user=1, steps=0)
        self.assertEqual(self.book.sweep(), 0)

    def test_final_answer_is_kept_and_stands_in_for_a_missing_summary(self):
        self._row("run-live", status="stopped", closed_by_user=1, session_id="s-1")
        book = daybook.Daybook(self.cfg, self.store, summarise=lambda row, st: "")
        from unittest import mock
        with mock.patch("hub.voice.final_answer", return_value="Fixed the day book. It now keeps closed windows."):
            book.sweep()
        entry = next(iter(book._load(time.strftime("%Y-%m-%d")).values()))
        self.assertEqual(entry["summary"], "Fixed the day book.")
        self.assertIn("keeps closed windows", entry["final"])
        self.assertTrue(entry["closed"])

    def test_summary_reads_the_transcript_not_the_tui_screen(self):
        from hub import voice
        log = self.tmp + "/run.log"
        with open(log, "w") as f:
            f.write("▐▛███▜▌ Claude Code\n> Misting…\n")
        seen = []
        from unittest import mock
        with mock.patch("hub.voice.final_answer", return_value="I rewrote the nav."):
            voice.summarise_run({"id": "run-x", "log_path": log, "tool": "claude", "session_id": "s"}, "stopped",
                                runner=lambda argv, inp: seen.append(inp) or type("R", (), {"returncode": 0, "stdout": "ok"})())
        self.assertIn("I rewrote the nav.", seen[0])
        self.assertNotIn("Misting", seen[0])

    def test_git_commits_of_the_day_are_in_the_report(self):
        import subprocess
        from hub import obsidian
        proj = obsidian.workspace(self.cfg) / "gitproj"
        proj.mkdir(exist_ok=True)
        env = dict(__import__("os").environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", str(proj)], check=True, env=env)
        (proj / "a.txt").write_text("x")
        subprocess.run(["git", "-C", str(proj), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(proj), "commit", "-qm", "feat: ship the thing"], check=True, env=env)
        rep = self.book.report()
        self.assertEqual(rep["commits"]["gitproj"], ["feat: ship the thing"])
        self.assertIn("Git recorded 1 commit", rep["text"])         # even with no agent job on record
        self.assertIn("• feat: ship the thing", daybook.email_text(rep))
        self.assertTrue(self.book.send_summary(rep, send=lambda *a, **kw: None))
