"""One combined report per batch of agents, with an instant short line when one blocks on the owner."""
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401  (puts the repo on sys.path)
from hub import alerts, batch
from test_alerts import FakeRunner, FakeStore


def row(aid, status="running", job="j1", activity="", title=None, **kw):
    return {"id": aid, "status": status, "job_id": job, "activity": activity,
            "title": title or f"Task {aid}", **kw}


class BatcherTests(unittest.TestCase):
    def setUp(self):
        self.b = batch.Batcher()

    def kinds(self, actions):
        return [k for k, _ in actions]

    def test_waits_for_every_agent_then_reports_once(self):
        rows = {a: row(a) for a in ("a", "b", "c")}
        self.assertEqual(self.b.observe(list(rows.values())), [])
        rows["a"] = row("a", "done", files=["x.py"])
        self.assertEqual(self.b.observe(list(rows.values())), [])
        rows["b"] = row("b", "waiting", activity="Claude needs your permission to use Bash")
        acts = self.b.observe(list(rows.values()))
        self.assertEqual(self.kinds(acts), ["blocked"])                 # the instant short line, no report yet
        self.assertIn("needs you", batch.blocked_line(acts[0][1]))
        self.assertEqual(self.b.observe(list(rows.values())), [])       # the line goes out once
        rows["c"] = row("c", "failed", activity="exit code 2")
        acts = self.b.observe(list(rows.values()))
        self.assertEqual(self.kinds(acts), ["report"])
        members = acts[0][1]
        self.assertEqual(sorted(m["id"] for m in members), ["a", "b", "c"])
        text = batch.report_text(members)
        self.assertTrue(text.startswith("3 agents finished: 1 done, 1 needs you, 1 failed"))
        lines = text.splitlines()[1:]
        self.assertTrue(lines[0].startswith("Needs you: Task b"))      # what needs you comes first
        self.assertTrue(lines[1].startswith("Failed: Task c"))
        self.assertIn("changed 1 file", lines[2])
        self.assertEqual(self.b.observe(list(rows.values())), [])       # never reported twice

    def test_agents_that_start_later_go_to_the_next_report(self):
        rows = {"a": row("a")}
        self.b.observe(list(rows.values()))
        rows["d"] = row("d")                                            # starts after the batch began
        self.b.observe(list(rows.values()))
        rows["a"] = row("a", "done")
        acts = self.b.observe(list(rows.values()))
        self.assertEqual([m["id"] for m in acts[0][1]], ["a"])
        rows["d"] = row("d", "done")
        acts = self.b.observe(list(rows.values()))
        self.assertEqual([m["id"] for m in acts[0][1]], ["d"])

    def test_late_agent_that_finishes_first_still_waits_for_its_own_batch(self):
        rows = {"a": row("a")}
        self.b.observe(list(rows.values()))
        rows["e"] = row("e", "done")                                    # ran start to finish while a works
        self.assertEqual(self.b.observe(list(rows.values())), [])
        rows["a"] = row("a", "done")
        first = self.b.observe(list(rows.values()))[0][1]
        self.assertEqual([m["id"] for m in first], ["a"])
        second = self.b.observe(list(rows.values()))[0][1]
        self.assertEqual([m["id"] for m in second], ["e"])

    def test_history_at_startup_is_not_reported(self):
        old = [row("old", "done"), row("old2", "failed")]
        self.b.seed(old)
        self.assertEqual(self.b.observe(old), [])

    def test_work_in_flight_at_startup_is_still_reported(self):
        rows = [row("old", "done"), row("live")]
        self.b.seed(rows)
        self.assertEqual(self.b.observe(rows), [])
        acts = self.b.observe([row("old", "done"), row("live", "waiting", activity="turn finished, waiting for you")])
        self.assertEqual([(m["id"], m["outcome"]) for m in acts[0][1]], [("live", "replied")])

    def test_a_new_job_in_the_same_terminal_is_a_new_member(self):
        rows = {"t": row("t", job="j1"), "u": row("u")}
        self.b.observe(list(rows.values()))
        rows["t"] = row("t", "running", job="j2", title="Second job")   # same pane, new job before the report
        rows["u"] = row("u", "done")
        acts = self.b.observe(list(rows.values()))
        report = acts[0][1]
        self.assertEqual({(m["id"], m["outcome"]) for m in report}, {("t", "replied"), ("u", "done")})   # you gave it a new job: its turn was over
        rows["t"] = row("t", "waiting", job="j2", activity="turn finished, waiting for you", title="Second job")
        acts = self.b.observe(list(rows.values()))
        self.assertEqual(self.kinds(acts), ["report"])                  # replied: settles, no "needs you" line
        self.assertEqual(acts[0][1][0]["title"], "Second job")

    def test_a_long_running_agent_does_not_hold_the_report_forever(self):
        rows = {"me": row("me"), "w": row("w")}
        self.b.observe(list(rows.values()), now=0, max_wait=300)
        rows["w"] = row("w", "done")
        self.assertEqual(self.b.observe(list(rows.values()), now=10, max_wait=300), [])
        self.assertEqual(self.b.observe(list(rows.values()), now=200, max_wait=300), [])
        acts = self.b.observe(list(rows.values()), now=311, max_wait=300)
        members = acts[0][1]
        self.assertEqual({(m["id"], m["outcome"]) for m in members}, {("w", "done"), ("me", "working")})
        self.assertTrue(batch.report_text(members).startswith("1 agent finished, 1 still working: 1 done"))
        self.assertIn("Still working: Task me", batch.report_text(members))
        self.assertEqual(self.b.observe(list(rows.values()), now=320, max_wait=300), [])   # w never again
        rows["me"] = row("me", "done")
        acts = self.b.observe(list(rows.values()), now=330, max_wait=300)
        self.assertEqual([(m["id"], m["outcome"]) for m in acts[0][1]], [("me", "done")])

    def test_block_then_resume_then_finish(self):
        rows = {"a": row("a"), "b": row("b")}
        self.b.observe(list(rows.values()))
        rows["a"] = row("a", "waiting", activity="needs your input")
        self.assertEqual(self.kinds(self.b.observe(list(rows.values()))), ["blocked"])
        rows["a"] = row("a")                                            # the owner answered, it works again
        self.assertEqual(self.b.observe(list(rows.values())), [])
        rows["a"], rows["b"] = row("a", "done"), row("b", "stopped")
        report = self.b.observe(list(rows.values()))[0][1]
        self.assertEqual({(m["id"], m["outcome"]) for m in report}, {("a", "done"), ("b", "closed")})


class FakeSpeaker:
    def __init__(self):
        self.lines, self.reports = [], []

    def speak(self, phrase, key=None, event=None, row=None):
        self.lines.append(phrase)

    def say_report(self, members):
        self.reports.append(members)

    def say_status(self, row, status=None):
        raise AssertionError("combined mode must not speak one report per window")


class WatcherCombinedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = {"alerts_state_path": str(Path(self._tmp.name) / "a.json"),
                    "alerts": {"banner": True, "sound": False, "notifier": "osascript", "cooldown_s": 20}}
        self.store, self.runner, self.speaker = FakeStore(), FakeRunner(), FakeSpeaker()
        self.w = alerts.AlertWatcher(self.cfg, self.store, runner=self.runner, speaker=self.speaker)
        self.phone = []
        self.w.phone_sender = lambda cfg, title, msg, **k: self.phone.append((title, msg, k.get("priority")))
        self.idle = 600   # seconds since the owner last touched the Mac
        self.w.idle_reader = lambda: self.idle

    def tearDown(self):
        self._tmp.cleanup()

    def banners(self):
        return [c[-1] for c in self.runner.calls if c and c[0] == "osascript"]   # the banner title

    def test_one_banner_one_speech_for_the_whole_batch(self):
        for a in ("a", "b"):
            self.store.set_row(row(a))
        self.w.poll_once()
        self.store.set_row(row("a", "done"))
        self.w.poll_once()
        self.assertEqual(self.banners(), [])                            # no per-window "done" alert
        self.store.set_row(row("b", "waiting", activity="Claude needs your permission"))
        self.w.poll_once()
        self.assertEqual(self.banners(), ["Jarviee · Needs you", "Jarviee · Report"])
        self.assertEqual(len(self.speaker.lines), 1)
        self.assertEqual(len(self.speaker.reports), 1)
        self.assertEqual(sorted(m["id"] for m in self.speaker.reports[0]), ["a", "b"])
        self.assertEqual(batch.LAST["counts"], {"done": 1, "needs": 1})
        self.w.poll_once()
        self.assertEqual(len(self.banners()), 2)

    def test_muted_still_tracks_but_sends_nothing(self):
        alerts.set_muted(self.cfg, True)
        self.store.set_row(row("a"))
        self.w.poll_once()
        self.store.set_row(row("a", "failed", activity="exit code 1"))
        self.w.poll_once()
        self.assertEqual(self.banners(), [])
        self.assertEqual(self.phone, [])

    def test_combined_off_keeps_per_window_alerts(self):
        self.cfg["alerts"]["combined"] = False
        self.speaker.say_status = lambda row, status=None: self.speaker.lines.append(status)
        self.store.set_row(row("a"))
        self.w.poll_once()
        self.store.set_row(row("a", "done"))
        self.w.poll_once()
        self.assertEqual(len(self.banners()), 1)

    def test_quick_reply_is_not_pushed_to_the_phone(self):
        self.cfg["push"] = {"enabled": True}
        self.store.set_row(row("a", started=1000, updated=1000, steps=0, title="cd"))
        self.w.poll_once()
        self.store.set_row(row("a", "waiting", activity="turn finished", started=1000, updated=1004, steps=1))
        self.w.poll_once()
        self.assertEqual(self.banners(), ["Jarviee · Report"])      # the Mac still shows it
        self.assertEqual(self.phone, [])

    def test_real_work_is_pushed_at_default_priority_failure_at_high(self):
        self.cfg["push"] = {"enabled": True}
        self.store.set_row(row("a", started=1000, steps=0))
        self.w.poll_once()
        self.store.set_row(row("a", "done", started=1000, ended=1400, steps=30))
        self.w.poll_once()
        self.store.set_row(row("a", job="j2", started=2000, steps=0))
        self.w.poll_once()
        self.store.set_row(row("a", "failed", job="j2", activity="exit code 1", started=2000, ended=2005))
        self.w.poll_once()
        self.assertEqual([(t, p) for t, _, p in self.phone], [("Jarviee · Report", 3), ("Jarviee · Report", 4)])

    def test_nothing_reaches_the_phone_while_the_owner_is_at_the_mac(self):
        self.cfg["push"] = {"enabled": True}
        self.idle = 3
        self.store.set_row(row("a", started=1000))
        self.w.poll_once()
        self.store.set_row(row("a", "failed", activity="exit code 1", started=1000, ended=1900, steps=40))
        self.w.poll_once()
        self.assertEqual(self.phone, [])
        self.assertEqual(self.banners(), ["Jarviee · Report"])

    def test_needs_you_waits_for_the_owner_to_step_away(self):
        self.cfg["push"] = {"enabled": True}
        self.idle = 3
        for a in ("a", "b"):
            self.store.set_row(row(a))
        self.w.poll_once()
        self.store.set_row(row("a", "waiting", activity="Claude needs your permission"))
        self.store.set_row(row("b", "waiting", activity="Claude needs your permission"))
        self.w.poll_once()
        self.assertEqual(self.phone, [])                             # at the Mac: banner only
        self.store.set_row(row("b", "running"))                      # b answered at the terminal
        self.w.poll_once()
        self.idle = 600                                              # the owner walks away, a still waits
        self.w.poll_once()
        self.assertEqual([(t, p) for t, _, p in self.phone], [("Jarviee · Needs you", 4)])
        self.assertIn("Task a", self.phone[0][1])
        self.w.poll_once()
        self.assertEqual(len(self.phone), 1)                         # once


class LiveSessionAfterRestart(unittest.TestCase):
    """A live pane session's Stop must still mean "your turn" after the hub restarted, or the batch never settles."""

    def test_stop_settles_a_live_run_the_hub_no_longer_remembers(self):
        from helpers import TempDir, make_cfg
        from hub import launcher
        from hub.server import handle_hook
        from hub.store import Store
        with TempDir() as tmp:
            cfg, _ = make_cfg(tmp)
            store = Store(cfg["db_path"])
            new_log, old_log = Path(tmp) / "new.log", Path(tmp) / "old.log"
            new_log.write_text("$ claude\n")
            old_log.write_text("$ claude\n\n[pane] " + launcher.LIVE_NOTE + ": reply there\n")
            store.upsert("run-new", source="launcher", tool="claude", status="running", interactive=1,
                         log_path=str(new_log))
            store.upsert("run-old", source="launcher", tool="claude", status="running", log_path=str(old_log))
            store.upsert("run-print", source="launcher", tool="claude", status="running", log_path=str(new_log))
            launcher.LIVE_RUNS.clear()                                       # the restart
            for rid in ("run-new", "run-old", "run-print"):
                handle_hook("Stop", {"jarvieeRunId": rid, "jarvieeTool": "claude"}, cfg, store)
            self.assertEqual(store.get("run-new")["status"], "waiting")
            self.assertEqual(store.get("run-old")["status"], "waiting")     # from before the column existed
            self.assertEqual(store.get("run-print")["status"], "running")   # a print run is just ending
            b = batch.Batcher()
            b.observe([dict(store.get("run-new"), status="running")])
            acts = b.observe([store.get("run-new")])
            self.assertEqual([k for k, _ in acts], ["report"])

    def test_midway_prompt_while_running_does_not_fire_done_report(self):
        from helpers import make_cfg
        from hub.ingest import handle_hook
        from hub.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = make_cfg(tmp)
            store = Store(cfg["db_path"])
            b = batch.Batcher()
            sid = {"session_id": "midway-1", "jarvieeTool": "claude"}
            aid = handle_hook("UserPromptSubmit", {**sid, "prompt": "Refactor the auth module"}, cfg, store)
            job1 = store.get(aid)["job_id"]
            self.assertEqual(b.observe(store.all()), [])
            # Owner sends a follow-up prompt while the agent is still running
            handle_hook("UserPromptSubmit", {**sid, "prompt": "Also update the tests"}, cfg, store)
            self.assertEqual(store.get(aid)["job_id"], job1)
            self.assertEqual(store.get(aid)["status"], "running")
            self.assertEqual(b.observe(store.all()), [])
            # Only when the agent actually stops does the report fire
            handle_hook("Stop", sid, cfg, store)
            acts = b.observe(store.all())
            self.assertEqual([k for k, _ in acts], ["report"])


class SpeakerReportTests(unittest.TestCase):
    def test_report_is_spoken_once_even_with_done_filtered_out(self):
        from hub import voice
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"alerts_state_path": str(Path(tmp) / "a.json"), "alerts": {"cooldown_s": 20},
                   "voice": {"enabled": True, "events": ["waiting"], "tldr": False}}
            said = []
            sp = voice.Speaker(cfg, runner=lambda argv: said.append(argv[-1]), start_worker=False)
            members = [dict(row("a", "done", title="Fix the navbar"), outcome="done"),
                       dict(row("b", "failed", activity="exit code 1", title="Run the tests"), outcome="failed")]
            self.assertTrue(sp.say_report(members))
            sp.drain()
            self.assertEqual(len(said), 1)
            self.assertIn("2 agents finished", said[0])
            self.assertIn("Fix the navbar", said[0])
            self.assertIn("Run the tests", said[0])


if __name__ == "__main__":
    unittest.main()
