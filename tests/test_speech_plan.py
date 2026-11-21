"""The speech plan: done / failed are never lost to the cooldown, a pipeline plan speaks once, wording says more."""
import time
import unittest
from pathlib import Path

from helpers import TempDir
from hub import alerts, batch, voice
from hub.store import Store
from hub.tasks import Tasks
from hub.voice import Speaker


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv[-1])


class CooldownDefer(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg = {"alerts_state_path": str(Path(self.tmp) / "a.json"), "alerts": {"cooldown_s": 20},
                    "voice": {"enabled": True, "debounce_s": 3, "briefing": False}}
        self.runner = Runner()
        self.now = 1700000000.0
        self.sp = Speaker(self.cfg, runner=self.runner, clock=lambda: self.now, start_worker=False)

    def tearDown(self):
        self._t.__exit__()

    def row(self, status, **kw):
        return dict({"id": "a1", "agent_no": 1, "status": status}, **kw)

    def test_done_inside_the_cooldown_is_spoken_when_it_ends(self):
        self.assertTrue(self.sp.say_status(self.row("waiting")))
        self.now += 5
        self.assertTrue(self.sp.say_status(self.row("done")))          # accepted, held back
        self.sp.drain()
        self.assertEqual(self.runner.calls, ["Agent 1 needs your reply"])
        self.now += 14
        self.sp.drain()
        self.assertEqual(self.runner.calls, ["Agent 1 needs your reply"])   # 19 s: still inside the cooldown
        self.now += 2
        self.sp.drain()
        self.assertEqual(self.runner.calls, ["Agent 1 needs your reply", "Agent 1 is done"])

    def test_failed_is_held_the_same_way_and_a_newer_outcome_replaces_it(self):
        self.sp.say_status(self.row("waiting"))
        self.now += 5
        self.sp.say_status(self.row("failed"))
        self.now += 1
        self.sp.say_status(self.row("done"))                          # the same job ended differently: newest wins
        self.now += 30
        self.sp.drain()
        self.assertEqual(self.runner.calls, ["Agent 1 needs your reply", "Agent 1 is done"])

    def test_waiting_inside_the_cooldown_is_still_dropped(self):
        self.sp.say_status(self.row("failed"))
        self.now += 5
        self.assertFalse(self.sp.say_status(self.row("waiting")))     # a repeat nag, not an outcome

    def test_switch_off_restores_the_old_drop(self):
        self.cfg["voice"]["defer_cooldown"] = False
        self.sp.say_status(self.row("waiting"))
        self.now += 5
        self.assertFalse(self.sp.say_status(self.row("done")))

    def test_quiet_hours_still_win_when_the_held_phrase_comes_due(self):
        lt = time.localtime(self.now)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 21, 59, 50, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
        self.now = start
        self.cfg["alerts"]["quiet_hours"] = ["22:00", "08:00"]
        self.sp.say_status(self.row("waiting"))
        self.now += 5
        self.assertTrue(self.sp.say_status(self.row("done")))         # 21:59:55, not quiet yet
        self.now += 40                                               # 22:00:35: quiet
        self.sp.drain()
        self.assertEqual(self.runner.calls, ["Agent 1 needs your reply"])

    def test_muting_while_held_back_silences_it(self):
        self.sp.say_status(self.row("waiting"))
        self.sp.drain()
        self.now += 5
        self.sp.say_status(self.row("done"))
        self.cfg["voice"]["muted"] = True
        self.now += 30
        self.sp.drain()
        self.assertEqual(self.runner.calls, ["Agent 1 needs your reply"])

    def test_each_job_has_its_own_cooldown(self):
        self.sp.say_status(self.row("waiting", job_id="j1"))
        self.now += 5
        self.sp.say_status(self.row("done", job_id="j2"))             # a new job in the same pane reports at once
        self.sp.drain()
        self.assertEqual(len(self.runner.calls), 2)


def pipe_row(rid, plan, status="done", **kw):
    return dict({"id": rid, "status": status, "job_id": "j", "title": f"run {rid}", "workspace": "/w/Alpha",
                 "reasons": [f"plan task {plan}.{rid}"]}, **kw)


class PlanWatchTests(unittest.TestCase):
    def setUp(self):
        self.briefs = {"plan-aaa111": {"status": "running", "goal": "Add dark mode to the settings page",
                                       "pipeline": {"round": 1, "note": ""}},
                       "plan-bbb222": {"status": "running", "goal": "x", "pipeline": None}}
        self.pw = batch.PlanWatch(lambda pid: self.briefs.get(pid))

    def test_plan_of_reads_the_scheduler_note(self):
        self.assertEqual(batch.plan_of({"reasons": ["plan task plan-2c40f6.fix-1", "x"]}), "plan-2c40f6")
        self.assertEqual(batch.plan_of({"reasons": '["plan task plan-2c40f6.t3"]'}), "plan-2c40f6")
        self.assertIsNone(batch.plan_of({"reasons": ["router: complex"]}))
        self.assertIsNone(batch.plan_of({}))

    def test_phases_are_held_until_the_plan_ends_then_reported_once(self):
        rows = [pipe_row("research", "plan-aaa111", started=100, ended=160, steps=3, files=[]),
                pipe_row("build", "plan-aaa111", started=170, ended=400, steps=9, files=["a.py", "b.py"])]
        kept, ended = self.pw.split(rows)
        self.assertEqual((kept, ended), ([], []))                    # still running: nothing for the batches, no report
        self.briefs["plan-aaa111"]["status"] = "done"
        rows.append(pipe_row("review", "plan-aaa111", started=410, ended=460, steps=2, files=["a.py"]))
        kept, ended = self.pw.split(rows)
        self.assertEqual(kept, [])
        self.assertEqual(len(ended), 1)
        m = ended[0]
        self.assertEqual((m["id"], m["outcome"], m["files"], m["steps"]), ("plan-aaa111", "done", ["a.py", "b.py"], 14))
        self.assertEqual((m["started"], m["ended"], m["plan"]["runs"]), (100, 460, 3))
        self.assertEqual(self.pw.split(rows), ([], []))              # never twice

    def test_ordinary_and_unknown_runs_stay_with_the_batches(self):
        plain = {"id": "p", "status": "done", "reasons": ["router: light"]}
        other = pipe_row("t1", "plan-bbb222")                         # a plan that is not a pipeline
        unknown = pipe_row("t1", "plan-ccc333")                       # not in the database
        kept, ended = self.pw.split([plain, other, unknown])
        self.assertEqual([r["id"] for r in kept], ["p", "t1", "t1"])
        self.assertEqual(ended, [])

    def test_plans_already_finished_at_startup_are_history(self):
        self.briefs["plan-aaa111"]["status"] = "failed"
        rows = [pipe_row("research", "plan-aaa111")]
        self.pw.seed(rows)
        self.assertEqual(self.pw.split(rows), ([], []))

    def test_failed_and_cancelled_plans_have_their_own_outcome(self):
        for status, outcome in (("failed", "failed"), ("cancelled", "closed")):
            pw = batch.PlanWatch(lambda pid, s=status: {"status": s, "goal": "g", "pipeline": {"note": "build failed twice"}})
            _, ended = pw.split([pipe_row("t", "plan-aaa111", "failed")])
            self.assertEqual(ended[0]["outcome"], outcome)

    def test_a_broken_lookup_never_raises(self):
        def boom(pid):
            raise RuntimeError("db gone")
        self.assertEqual(batch.PlanWatch(boom).split([pipe_row("t", "plan-aaa111")])[0][0]["id"], "t")


class Wording(unittest.TestCase):
    def test_done_says_how_long_how_much_and_what_changed(self):
        m = dict(id="a", job_id="j", outcome="done", title="Fix router", workspace="/w/Alpha",
                 started=100, ended=352, steps=12, files=["hub/router.py"])
        line = batch.report_lines([m])[0]
        self.assertEqual(line, "Done: Fix router (Alpha), in 4 minutes, 12 steps, changed 1 file: router.py")
        self.assertIn("changed no files", batch.report_lines([dict(m, files=[])])[0])

    def test_failed_says_after_how_long_and_why(self):
        m = dict(id="a", job_id="j", outcome="failed", title="Fix router", started=100, ended=195,
                 activity="exit code 2 (quota)", files=["a.py"])
        line = batch.report_lines([m])[0]
        self.assertTrue(line.startswith("Failed: Fix router, after 1 minute, it hit a usage limit"), line)
        self.assertIn("it had changed 1 file", line)

    def test_needs_you_says_how_long_it_has_waited(self):
        m = dict(id="a", job_id="j", outcome="needs", title="Fix router", activity="Which branch?", updated=1000)
        self.assertIn("waiting 5 minutes", batch.report_lines([m], now=1300)[0])
        self.assertNotIn("waiting", batch.report_lines([m], now=1030)[0])
        self.assertNotIn("waiting", batch.report_lines([m])[0])

    def test_blocked_line_names_the_folder(self):
        row = dict(id="a", title="Fix router", workspace="/w/Alpha", status="waiting", activity="Which branch?")
        self.assertEqual(batch.blocked_line(row), "Fix router in Alpha needs you: Which branch?.")
        self.assertEqual(batch.blocked_line(dict(row, workspace="")), "Fix router needs you: Which branch?.")

    def test_a_plan_report_reads_as_one_pipeline(self):
        m = {"id": "plan-aaa111", "job_id": "", "title": "Add dark mode", "outcome": "done", "files": ["a.py"],
             "steps": 14, "started": 100, "ended": 700, "workspace": "/w/Alpha",
             "plan": {"runs": 6, "rounds": 1, "note": ""}}
        speech = batch.report_speech([m])
        self.assertTrue(speech.startswith("1 pipeline finished: 1 done."), speech)
        self.assertIn("Done: Add dark mode in Alpha, in 10 minutes, 6 agent runs, 1 fix round, changed 1 file.", speech)
        self.assertNotIn("step", speech)

    def test_only_status_metadata_is_spoken(self):
        m = dict(id="a", job_id="j", outcome="failed", title="Fix", activity="sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 token=hunter2")
        text = batch.report_speech([m])
        self.assertNotIn("hunter2", text)
        self.assertNotIn("ABCDEFGHIJ", text)


class FakeSpeaker:
    def __init__(self):
        self.reports, self.statuses = [], []

    def speak(self, phrase, key=None, event=None, row=None):
        pass

    def say_report(self, members):
        self.reports.append(members)

    def say_status(self, row, status=None):
        self.statuses.append((row["id"], status))


class PipelineSpeaksOnce(unittest.TestCase):
    """AlertWatcher with a real store and queue tables: each phase is a run, the plan is reported when it ends."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg = {"alerts_state_path": str(Path(self.tmp) / "a.json"),
                    "alerts": {"banner": False, "sound": False, "cooldown_s": 0}}
        self.store = Store(str(Path(self.tmp) / "db.sqlite"))
        self.tasks = Tasks(self.store)
        self.pid = self.tasks.add_plan("Add dark mode to the settings page", "Alpha", [], planner="pipeline",
                                       status="approved", pipeline={"phase": "research", "round": 0, "note": ""})
        self.speaker = FakeSpeaker()
        self.w = alerts.AlertWatcher(self.cfg, self.store, runner=lambda argv: None, speaker=self.speaker)
        self.w.phone_sender = lambda *a, **k: None

    def tearDown(self):
        self._t.__exit__()

    def run_row(self, name, status, **kw):
        self.store.upsert(f"run-{name}", source="launcher", tool="claude", title=f"{name} phase", status=status,
                          workspace="/w/Alpha", job_id=f"job-{name}", started=100.0, ended=200.0,
                          reasons=[f"plan task {self.pid}.{name}"], **kw)

    def test_one_report_for_the_whole_plan(self):
        for name in ("research", "plan", "build"):
            self.run_row(name, "running")
        self.w.poll_once()
        self.run_row("research", "done")
        self.w.poll_once()
        self.run_row("plan", "done")
        self.run_row("build", "done", files=["a.py"])
        self.w.poll_once()
        self.assertEqual(self.speaker.reports, [])                   # the plan is still open (test / review to come)
        self.run_row("review", "running")
        self.w.poll_once()
        self.run_row("review", "done")
        self.w.poll_once()
        self.assertEqual(self.speaker.reports, [])
        self.tasks.set_plan(self.pid, status="done")
        self.w.poll_once()
        self.assertEqual(len(self.speaker.reports), 1)
        (member,) = self.speaker.reports[0]
        self.assertEqual((member["id"], member["outcome"], member["plan"]["runs"]), (self.pid, "done", 4))
        self.assertEqual(member["files"], ["a.py"])
        self.assertEqual(batch.LAST["counts"], {"done": 1})
        for _ in range(3):
            self.w.poll_once()
        self.assertEqual(len(self.speaker.reports), 1)

    def test_a_failed_phase_does_not_speak_until_the_plan_fails(self):
        self.run_row("build", "running")
        self.w.poll_once()
        self.run_row("build", "failed")
        self.w.poll_once()
        self.assertEqual(self.speaker.reports, [])
        self.tasks.set_plan(self.pid, status="failed")
        self.w.poll_once()
        self.assertEqual(self.speaker.reports[0][0]["outcome"], "failed")

    def test_ordinary_runs_still_report_as_a_batch(self):
        self.store.upsert("run-solo", source="launcher", tool="claude", title="Solo", status="running", job_id="j")
        self.w.poll_once()
        self.store.upsert("run-solo", status="done")
        self.w.poll_once()
        self.assertEqual([m["id"] for m in self.speaker.reports[0]], ["run-solo"])

    def test_plan_reports_can_be_switched_off(self):
        self.cfg["alerts"]["plan_reports"] = False
        self.run_row("build", "running")
        self.w.poll_once()
        self.run_row("build", "done")
        self.w.poll_once()
        self.assertEqual([m["id"] for m in self.speaker.reports[0]], ["run-build"])   # one report per run, as before

    def test_per_window_mode_skips_phase_speech_and_speaks_the_plan(self):
        self.cfg["alerts"]["combined"] = False
        self.run_row("build", "running")
        self.w.poll_once()
        self.run_row("build", "done")
        self.w.poll_once()
        self.assertEqual(self.speaker.statuses, [])
        self.tasks.set_plan(self.pid, status="done")
        self.w.poll_once()
        self.assertEqual(len(self.speaker.reports), 1)


class SpeakerReportWording(unittest.TestCase):
    def test_say_report_speaks_the_plan_once_through_the_real_speaker(self):
        with TempDir() as tmp:
            cfg = {"alerts_state_path": str(Path(tmp) / "a.json"), "alerts": {"cooldown_s": 20},
                   "voice": {"enabled": True, "debounce_s": 3, "tldr": False}}
            runner = Runner()
            sp = Speaker(cfg, runner=runner, clock=lambda: 1700000000.0, start_worker=False)
            m = {"id": "plan-aaa111", "job_id": "", "title": "Add dark mode", "outcome": "done", "files": [],
                 "steps": 0, "started": 100, "ended": 220, "workspace": "/w/Alpha",
                 "plan": {"runs": 5, "rounds": 0, "note": ""}}
            self.assertTrue(sp.say_report([m]))
            sp.drain()
            self.assertEqual(len(runner.calls), 1)
            self.assertIn("5 agent runs", runner.calls[0])


if __name__ == "__main__":
    unittest.main()
