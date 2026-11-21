import sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from helpers import TempDir, make_cfg
from hub import launcher, outcome, server
from hub import store as hub_store


class Classify(unittest.TestCase):
    def test_done_failed_needs_you(self):
        c = outcome.classify
        self.assertEqual(c(0, "all good\nTests pass.\n")[0], outcome.DONE)
        self.assertEqual(c(1, "boom")[0], outcome.FAILED)
        self.assertEqual(c(0, "Changed two files.\nWould you like me to also update the docs?")[0], outcome.DONE)
        self.assertEqual(c(0, "Which approach do you prefer?")[0], outcome.NEEDS_YOU)
        self.assertEqual(c(0, "I'm blocked: permission denied on /etc")[0], outcome.NEEDS_YOU)
        self.assertEqual(c(0, "", empty_output=True)[0], outcome.DONE)
        self.assertEqual(c(0, "x", soft_denied=True)[0], outcome.FAILED)
        self.assertEqual(c(0, "done", stopped=True)[0], outcome.FAILED)

    def test_false_alarms_are_done(self):
        c = outcome.classify
        for log in ("Implemented the feature. Tests pass.\nLet me know if you want more.",
                    "Fixed the bug and committed.\nShould I also add a changelog entry?",
                    "Summary of changes: refactored x.\nWant me to run the full suite?",
                    "The task was completed.\nI refused nothing; unable to complete none."):
            self.assertEqual(c(0, log)[0], outcome.DONE, log)
        self.assertEqual(c(0, "")[0], outcome.DONE)

    def test_real_blockers_need_you(self):
        c = outcome.classify
        for log in ("Run this command?\nPermission required: approve this command to continue.",
                    "Which database should I use, sqlite or postgres?",
                    "I can't continue without the API key. Please log in.",
                    "Implemented most of it. I'm blocked: need your decision on the schema."):
            self.assertEqual(c(0, log)[0], outcome.NEEDS_YOU, log)

    def test_old_question_is_not_the_closing_message(self):
        log = "Should I use sqlite? (answered by the brief)\n" + "working\n" * 20 + "Finished the migration."
        self.assertEqual(outcome.classify(0, log)[0], outcome.DONE)

    def test_may_close(self):
        self.assertTrue(outcome.may_close("done"))
        self.assertTrue(outcome.may_close("failed", "retrying (1/3): exit code 1"))
        self.assertFalse(outcome.may_close("failed", "failed after 3 retries: exit code 1"))
        self.assertFalse(outcome.may_close("waiting", "needs you: asked you something"))
        self.assertFalse(outcome.may_close("running"))
        self.assertTrue(outcome.may_close("stopped"))


class Flow(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = hub_store.Store(str(Path(self.tmp) / "hub.db"))

    def tearDown(self):
        self._t.__exit__()

    def runs(self):
        return sorted(self.store.all(), key=lambda a: a.get("started") or 0)

    def test_failure_retries_three_times_then_reports(self):
        rid, _ = launcher.launch("FAKE_EXIT=3 do a thing", self.cfg, self.store, tool="agy", tier="basic", cwd=self.tmp)
        for _ in range(100):
            time.sleep(0.3)
            rows = [r for r in self.store.all() if r.get("task", "").startswith("FAKE_EXIT=3")]
            if len(rows) == 4 and all(r["status"] != "running" for r in rows):
                break
        notes = sorted(r["activity"] for r in rows)
        self.assertEqual(len(rows), 4, notes)
        self.assertEqual(sum(1 for n in notes if n.startswith("retrying")), 3)
        self.assertEqual(sum(1 for n in notes if n.startswith("failed after 3 retries")), 1)

    def test_question_exit_zero_is_waiting_not_done(self):
        self.cfg["tools"]["agy"]["command"] = [sys.executable, "-c",
            "print('Edited two files.'); print('Permission required: approve this command to continue.')", "agy", "{prompt}", "{model}"]
        rid, _ = launcher.launch("update things", self.cfg, self.store, tool="agy", tier="basic", cwd=self.tmp)
        launcher.wait(rid, 20)
        row = self.store.get(rid)
        self.assertEqual(row["status"], "waiting", row)
        self.assertTrue(row["activity"].startswith("needs you"))
        self.assertFalse(server.close_verdict(self.store, rid, wait_s=0.1)[0])


if __name__ == "__main__":
    unittest.main()
