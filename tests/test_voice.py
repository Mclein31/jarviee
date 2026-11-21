"""Tests for Jarviee spoken status alerts."""
from pathlib import Path
import threading
import time
import unittest

from helpers import TempDir
from hub import voice
from hub.alerts import AlertWatcher, set_muted
from hub.voice import Speaker, build_phrase, get_voice_cfg


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg = {
            "alerts_state_path": str(Path(self.tmp) / "alerts.json"),
            "alerts": {"cooldown_s": 20},
            "voice": {"enabled": True, "debounce_s": 3},
        }
        self.runner = FakeRunner()
        self.now = 1700000000.0
        self.sp = Speaker(self.cfg, runner=self.runner, clock=lambda: self.now, start_worker=False)

    def tearDown(self):
        self._t.__exit__()

    def row(self, **kw):
        r = {"id": "a1", "agent_no": 1, "task_id": "R2-9", "status": "done"}
        r.update(kw)
        return r

    def test_cfg_defaults_and_merge(self):
        c = get_voice_cfg({})
        self.assertFalse(c["enabled"])
        self.assertEqual(c["events"], ["waiting", "failed", "done"])
        c = get_voice_cfg({"voice": {"rate": 200, "enabled": True}})
        self.assertEqual(c["rate"], 200)
        self.assertTrue(c["enabled"])
        self.assertIn("voice", c)

    def test_phrases(self):
        self.assertEqual(build_phrase(self.row()), "Agent 1, running R2-9, is done")
        self.assertEqual(build_phrase(self.row(status="waiting")), "Agent 1, running R2-9, needs your reply")
        self.assertEqual(build_phrase(self.row(status="failed")), "Agent 1, running R2-9, failed")
        self.assertIsNone(build_phrase(self.row(status="running")))

    def test_never_speaks_transcript_or_output(self):
        r = self.row(activity="rm -rf secrets", title="leak me", output="token abc",
                     task_id='R2-9"; say hi')
        phrase = build_phrase(r)
        for bad in ("rm -rf", "leak", "token", '"', ";"):
            self.assertNotIn(bad, phrase)

    def test_say_argv_list(self):
        self.assertTrue(self.sp.say_status(self.row()))
        self.sp.drain()
        self.assertEqual(self.runner.calls,
                         [["say", "-v", "Samantha", "-r", "185", "--", "Agent 1, running R2-9, is done"]])

    def test_disabled_and_event_filter(self):
        self.cfg["voice"]["enabled"] = False
        self.assertFalse(self.sp.say_status(self.row()))
        self.cfg["voice"] = {"enabled": True, "events": ["failed"]}
        self.assertFalse(self.sp.say_status(self.row()))
        self.assertTrue(self.sp.say_status(self.row(status="failed")))

    def test_muted_flag(self):
        set_muted(self.cfg, True)
        self.assertFalse(self.sp.say_status(self.row()))
        set_muted(self.cfg, False)
        self.cfg["voice"]["muted"] = True
        self.assertFalse(self.sp.say_status(self.row()))

    def test_mute_while_queued_drops(self):
        self.assertTrue(self.sp.say_status(self.row()))
        set_muted(self.cfg, True)
        self.sp.drain()
        self.assertEqual(self.runner.calls, [])

    def test_debounce_and_cooldown(self):
        self.assertTrue(self.sp.speak("Agent 1 is done"))
        self.now += 1
        self.assertFalse(self.sp.speak("Agent 1 is done"))   # debounced
        self.now += 5
        self.assertTrue(self.sp.speak("Agent 1 is done"))
        self.assertTrue(self.sp.say_status(self.row(id="b")))
        self.now += 10   # past debounce, inside the 20s alerts cooldown
        self.assertTrue(self.sp.say_status(self.row(id="b", status="failed")))   # held back, not dropped (test_speech_plan)
        self.sp.drain()
        self.assertFalse(any("failed" in c[-1] for c in self.runner.calls))      # nothing yet: the cooldown runs
        self.now += 15
        self.assertTrue(self.sp.say_status(self.row(id="b", status="failed")))

    def test_quiet_hours(self):
        self.cfg["alerts"]["quiet_hours"] = ["22:00", "08:00"]
        lt = time.localtime(self.now)
        self.now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 30, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
        self.assertFalse(self.sp.say_status(self.row()))

    def test_burst_is_serialised(self):
        active, overlap, spoken = [0], [False], []
        lock = threading.Lock()

        def slow(argv):
            with lock:
                active[0] += 1
                overlap[0] = overlap[0] or active[0] > 1
            time.sleep(0.03)
            spoken.append(argv[-1])
            with lock:
                active[0] -= 1

        sp = Speaker(self.cfg, runner=slow, clock=lambda: self.now)
        for i in range(4):
            sp.say_status(self.row(id=f"x{i}", agent_no=i))
        deadline = time.time() + 3
        while len(spoken) < 4 and time.time() < deadline:
            time.sleep(0.01)
        sp.stop()
        self.assertEqual(len(spoken), 4)
        self.assertFalse(overlap[0])

    def test_voice_name_not_injectable(self):
        self.cfg["voice"]["voice"] = "x; rm -rf /"
        self.sp.say_status(self.row())
        self.sp.drain()
        self.assertNotIn("-v", self.runner.calls[0])


class _Store:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}

    def all(self):
        return [dict(r) for r in self.rows.values()]


class WatcherSpeaksTests(unittest.TestCase):
    """The AlertWatcher drives the Speaker on real status transitions."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg = {
            "alerts_state_path": str(Path(self.tmp) / "alerts.json"),
            "alerts": {"enabled": True, "banner": False, "sound": False, "cooldown_s": 0, "combined": False},
            "voice": {"enabled": True, "debounce_s": 0},
        }
        self.said = FakeRunner()
        self.now = 1700000000.0
        self.sp = Speaker(self.cfg, runner=self.said, clock=lambda: self.now, start_worker=False)

    def tearDown(self):
        self._t.__exit__()

    def phrases(self):
        self.sp.drain()
        return [c[-1] for c in self.said.calls]

    def test_transition_is_spoken_and_seeded_rows_are_not(self):
        store = _Store([{"id": "a1", "status": "running"}, {"id": "a2", "status": "waiting"}])
        w = AlertWatcher(self.cfg, store, runner=FakeRunner(), clock=lambda: self.now, speaker=self.sp)
        w.poll_once()
        self.assertEqual(self.phrases(), [])
        store.rows["a1"]["status"] = "done"
        w.poll_once()
        w.poll_once()   # no further change, no repeat
        self.assertEqual(self.phrases(), ["Agent is done"])

    def test_speaks_even_when_banner_alerts_disabled(self):
        self.cfg["alerts"]["enabled"] = False
        store = _Store([{"id": "a1", "status": "running", "agent_no": 2, "task_id": "R2-9"}])
        runner = FakeRunner()
        w = AlertWatcher(self.cfg, store, runner=runner, clock=lambda: self.now, speaker=self.sp)
        store.rows["a1"]["status"] = "failed"
        w.poll_once()
        self.assertEqual(self.phrases(), ["Agent 2, running R2-9, failed"])
        self.assertEqual(runner.calls, [])

    def test_new_row_waiting_is_spoken_but_cleared_is_not(self):
        store = _Store([])
        w = AlertWatcher(self.cfg, store, runner=FakeRunner(), clock=lambda: self.now, speaker=self.sp)
        store.rows["n1"] = {"id": "n1", "status": "waiting", "agent_no": 3}
        store.rows["n2"] = {"id": "n2", "status": "failed", "cleared": True}
        w.poll_once()
        self.assertEqual(self.phrases(), ["Agent 3 needs your reply"])



class BriefingTests(unittest.TestCase):
    def test_done_briefing_has_title_duration_steps_and_files(self):
        t = voice.build_briefing({"title": "Fix router", "status": "done", "started": 100, "ended": 352, "steps": 12,
                                  "files": ["hub/router.py", "tests/test_router.py"]})
        self.assertIn("Fix router", t)
        self.assertIn("4 minutes", t)
        self.assertIn("12 steps", t)
        self.assertIn("2 files", t)

    def test_failed_briefing_gives_reason_and_never_reads_urls(self):
        t = voice.build_briefing({"title": "Fix router", "status": "failed", "started": 0, "ended": 0,
                                  "activity": "hit quota see https://x.example/secret"})
        self.assertIn("failed", t)
        self.assertIn("usage limit", t)
        self.assertNotIn("http", t)

    def test_no_title_falls_back_to_none(self):
        self.assertIsNone(voice.build_briefing({"status": "done"}))


class SanitiseAndReasonTests(unittest.TestCase):
    def test_sanitise_strips_urls_secrets_tokens_and_control_chars(self):
        t = voice.sanitise_spoken("fail \x1b[31mred\x1b[0m at https://a.example/x token=abc123 "
                                  "Bearer zzz AAAAAAAAAAAAAAAAAAAAAAAA\u200bok")
        for bad in ("http", "abc123", "zzz", "AAAAAAAA", "\x1b", "\u200b"):
            self.assertNotIn(bad, t)
        self.assertIn("fail", t)

    def test_sanitise_caps_length_on_a_word_boundary(self):
        t = voice.sanitise_spoken("word " * 100, limit=22)
        self.assertLessEqual(len(t), 22)
        self.assertTrue(t.endswith("word"))
        self.assertEqual(voice.sanitise_spoken(None), "")

    def test_build_reason_maps_known_notes(self):
        self.assertEqual(voice.build_reason({"status": "failed", "activity": "429 too many requests"}),
                         "it hit a usage limit")
        self.assertEqual(voice.build_reason({"status": "failed", "activity": "Refused a shell command"}),
                         "it was blocked from running a shell command")
        self.assertEqual(voice.build_reason({"status": "failed", "activity": "claude exited with code 1"}),
                         "it exited with an error")

    def test_build_reason_failed_unknown_and_waiting_and_other(self):
        self.assertEqual(voice.build_reason({"status": "failed", "activity": ""}), voice.UNKNOWN_REASON)
        self.assertEqual(voice.build_reason({"status": "waiting", "activity": "Which branch? http://x.io"}),
                         "Which branch?")
        self.assertEqual(voice.build_reason({"status": "waiting"}), "")
        self.assertEqual(voice.build_reason({"status": "done", "activity": "x"}), "")

    def test_waiting_and_failed_briefings_speak_the_reason(self):
        w = voice.build_briefing({"title": "Fix router", "status": "waiting", "activity": "Which branch?"})
        self.assertIn("needs you", w)
        self.assertIn("Which branch?", w)
        f = voice.build_briefing({"title": "Fix router", "status": "failed"})
        self.assertIn("The reason: It stopped unexpectedly.", f)


if __name__ == "__main__":
    unittest.main()


class SpeechTests(unittest.TestCase):
    def test_speaking_marks_no_pane(self):
        """The pane aura / highlight was removed: speaking only speaks."""
        with TempDir() as tmp:
            cfg = {"alerts_state_path": str(Path(tmp) / "a.json"), "alerts": {"cooldown_s": 20},
                   "voice": {"enabled": True, "debounce_s": 0}}
            said = []
            sp = Speaker(cfg, runner=lambda argv: said.append(argv[-1]), start_worker=False)
            self.assertTrue(sp.speak("Fix router. It's done.", key="r1", event="done"))
            sp.drain()
            self.assertEqual(said, ["Fix router. It's done."])
            self.assertFalse(hasattr(sp, "highlight"))


class TldrBriefingTests(unittest.TestCase):
    def test_briefing_speaks_summary_not_prompt(self):
        row = {"title": "Fix the login bug please", "started": 100, "ended": 190, "files": ["a/b.py"]}
        out = voice.build_tldr_briefing(row, "done", "I fixed the login redirect and the tests pass.")
        self.assertIn("I fixed the login redirect", out)
        self.assertNotIn("Fix the login bug", out)
        self.assertIn("1 minute", out)

    def test_empty_summary_gives_none(self):
        self.assertIsNone(voice.build_tldr_briefing({}, "done", ""))

    def test_summarise_run_uses_runner_and_falls_back(self):
        import tempfile, types
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("working\n\nAll done: added the export button and wired it up.\n")
        row = {"id": "t-1", "task": "x", "log_path": f.name}
        ok = lambda argv, inp: types.SimpleNamespace(returncode=0, stdout="I added the export button.")
        self.assertEqual(voice.summarise_run(row, "done", runner=ok), "I added the export button.")
        row2 = dict(row, id="t-2")
        bad = lambda argv, inp: types.SimpleNamespace(returncode=1, stdout="")
        self.assertIn("export button", voice.summarise_run(row2, "done", runner=bad))
