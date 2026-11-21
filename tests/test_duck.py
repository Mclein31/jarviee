"""Audio ducking while a voice report plays. Every osascript call goes to a fake Mac; nothing real is touched."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401  (puts the repo on sys.path)
from hub import duck, voice


class FakeMac:
    """A pretend macOS: output volume + mute, players, browsers with playing media per tab."""

    def __init__(self, volume=50, muted=False, playing=(), running=(), media=None, denied=(), installed=None):
        self.volume, self.muted = volume, muted
        self.playing, self.running = set(playing), set(running) | set(playing)
        self.media = dict(media or {})        # browser bundle -> playing media elements
        self.paused_media = {}
        self.denied = set(denied)
        self.app_volume = {}                  # player bundle -> its own sound volume (default 100)
        self.media_level = {}                 # browser bundle -> volume of its lowered media (1.0 = untouched)
        self.stubborn = set()                 # players that refuse to pause but still take a volume
        self.installed = installed            # None: everything installed
        self.calls = []

    def _app(self, script):
        for bundle in [b for b, _ in duck.PLAYERS + duck.UNCONTROLLABLE + duck.CHROMIUM + duck.SAFARI]:
            if f'"{bundle}"' in script:
                return bundle
        return None

    def __call__(self, script, args=()):
        self.calls.append((script, list(args)))
        if script == duck.GET_VOLUME:
            return f"{self.volume},{'true' if self.muted else 'false'}"
        if script == duck.SET_VOLUME:
            self.volume = int(args[0])
            return ""
        if script == duck.SET_MUTED:
            self.muted = args[0] == "true"
            return ""
        app = self._app(script)
        if app is None or (self.installed is not None and app not in self.installed):
            raise RuntimeError("Can't get application id")
        if script == duck.running_script(app):
            return "true" if app in self.running else "false"
        if script == duck.player_pause_script(app):
            if app == "com.apple.podcasts" or app in self.stubborn:
                raise RuntimeError("no player state")
            if app not in self.running:
                return "off"
            if app in self.playing:
                self.playing.discard(app)
                return "paused"
            return "idle"
        if script == duck.player_lower_script(app):
            if app not in self.running:
                return "off"
            if app not in self.playing:
                return "idle"
            old = self.app_volume.get(app, 100)
            self.app_volume[app] = min(old, int(args[0]))
            return str(old)
        if script == duck.player_volume_script(app):
            self.app_volume[app] = int(args[0])
            return ""
        if script == duck.player_resume_script(app):
            self.playing.add(app)
            return ""
        if script in (duck.browser_script(app, False), duck.browser_script(app, True)):
            if app not in self.running:
                return "off"
            if app in self.denied:
                return "denied"
            if "m.dataset.jarvieeVol=String(m.volume)" in args[0]:
                n = self.media.get(app, 0)
                self.media_level[app] = float(args[0].split("m.volume=")[1].split(";")[0]) if n else 1.0
                return str(n)
            if args[0] == duck.RAISE_JS:
                self.media_level[app] = 1.0
                return str(self.media.get(app, 0))
            if args[0] == duck.PAUSE_JS:
                n = self.media.pop(app, 0)
                self.paused_media[app] = n
                return str(n)
            n = self.paused_media.pop(app, 0)
            self.media[app] = self.media.get(app, 0) + n
            return str(n)
        raise AssertionError(f"unexpected script for {app}")

    def scripts(self):
        return [s for s, _ in self.calls]


class DuckerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = {"duck_state_path": str(Path(self._tmp.name) / "duck.json"), "voice": {}}

    def tearDown(self):
        self._tmp.cleanup()

    def ducker(self, mac, **voice_cfg):
        self.cfg["voice"].update(voice_cfg)
        return duck.Ducker(self.cfg, run=mac)

    def test_pause_mode_pauses_only_what_plays_and_resumes_only_that(self):
        mac = FakeMac(volume=55, playing={"com.spotify.client"}, running={"com.apple.Music", "com.google.Chrome"},
                      media={"com.google.Chrome": 2})
        d = self.ducker(mac, duck_mode="pause", report_volume=80)
        st = d.begin()
        self.assertEqual(mac.playing, set())
        self.assertEqual(mac.media, {})
        self.assertEqual(st["players"], ["com.spotify.client"])            # Music wasn't playing: not recorded
        self.assertEqual(st["browsers"], [["com.google.Chrome", False]])
        self.assertEqual((mac.volume, st["gain"], st["stuck"]), (80, 1.0, []))   # the report plays at 80% of full
        self.assertTrue(Path(self.cfg["duck_state_path"]).is_file())       # on disk while ducked
        d.end(st)
        self.assertEqual(mac.playing, {"com.spotify.client"})              # Music was never started
        self.assertEqual(mac.media, {"com.google.Chrome": 2})
        self.assertNotIn(duck.player_resume_script("com.apple.Music"), mac.scripts())
        self.assertEqual(mac.volume, 55)
        self.assertFalse(Path(self.cfg["duck_state_path"]).exists())

    def test_the_report_is_never_quieted_with_the_system_volume(self):
        # the reported bug: a browser refusing JavaScript made Jarviee drop the system volume, and the voice with it
        mac = FakeMac(volume=65, running={"com.apple.Safari", "com.google.Chrome"},
                      denied={"com.apple.Safari", "com.google.Chrome"})
        d = self.ducker(mac, duck_mode="pause", duck_volume=10, report_volume=80)
        st = d.begin()
        self.assertEqual((mac.volume, st["gain"]), (80, 1.0))
        self.assertEqual(sorted(st["stuck"]), ["Chrome", "Safari"])
        self.assertNotIn([duck.SET_VOLUME, [10]], [[sc, a] for sc, a in mac.calls])
        mac.muted = True                                                     # something toggled it meanwhile
        d.end(st)
        self.assertEqual((mac.volume, mac.muted), (65, False))              # exact previous volume and mute

    def test_after_the_report_everything_goes_back_to_where_it_was_not_to_full(self):
        mac = FakeMac(volume=37, playing={"com.spotify.client"}, running={"com.google.Chrome"},
                      media={"com.google.Chrome": 1})
        mac.app_volume["com.spotify.client"] = 45
        d = self.ducker(mac, duck_mode="volume", duck_volume=10, report_volume=80)
        st = d.begin()
        self.assertEqual((mac.volume, mac.app_volume["com.spotify.client"]), (80, 12))
        d.end(st)
        self.assertEqual((mac.volume, mac.app_volume["com.spotify.client"]), (37, 45))   # not 100, not 80
        self.assertIn("if(!isNaN(v))m.volume=v", duck.RAISE_JS)              # a tab's own level, never forced to 1

    def test_a_second_speaker_restores_the_owner_volume_not_the_report_level(self):
        Path(self.cfg["duck_state_path"]).write_text(json.dumps({"pid": 1, "at": time.time(), "volume": 37,
                                                                 "muted": False}))
        mac = FakeMac(volume=80)                                             # pid 1 is speaking at 80 right now
        d = self.ducker(mac, report_volume=80)
        st = d.begin()
        self.assertEqual(st["volume"], 37)
        mac.volume = 70                                                      # anything that differs
        d.end(st)
        self.assertEqual(mac.volume, 37)

    def test_volumes_are_shares_of_full_not_of_the_current_volume(self):
        mac = FakeMac(volume=30, running={"com.apple.podcasts"})
        st = self.ducker(mac, duck_mode="pause", duck_volume=10, report_volume=80).begin()
        self.assertEqual((mac.volume, st["ducked_volume"], st["stuck"]), (80, True, ["Podcasts"]))
        mac2 = FakeMac(volume=94)
        self.ducker(mac2, duck_mode="pause", report_volume=80).begin()
        self.assertEqual(mac2.volume, 80)

    def test_volume_mode_lowers_each_source_to_duck_volume_of_full(self):
        mac = FakeMac(volume=60, muted=False, playing={"com.apple.Music"}, running={"com.google.Chrome"},
                      media={"com.google.Chrome": 1})
        d = self.ducker(mac, duck_mode="volume", duck_volume=10, report_volume=80)
        st = d.begin()
        self.assertEqual(mac.playing, {"com.apple.Music"})                  # nothing paused
        self.assertEqual(mac.volume, 80)                                     # the report at 80% of full
        self.assertEqual(mac.app_volume["com.apple.Music"], 12)             # 12% of 80% = about 10% of full
        self.assertAlmostEqual(mac.media_level["com.google.Chrome"], .125)
        d.end(st)
        self.assertEqual((mac.volume, mac.app_volume["com.apple.Music"], mac.media_level["com.google.Chrome"]),
                         (60, 100, 1.0))

    def test_a_player_that_will_not_pause_is_lowered_instead(self):
        mac = FakeMac(volume=50, playing={"com.spotify.client"})
        mac.stubborn.add("com.spotify.client")
        d = self.ducker(mac, duck_mode="pause", duck_volume=20, report_volume=80)
        st = d.begin()
        self.assertEqual((mac.app_volume["com.spotify.client"], st["stuck"]), (25, []))
        d.end(st)
        self.assertEqual(mac.app_volume["com.spotify.client"], 100)

    def test_muted_mac_stays_muted_and_untouched(self):
        mac = FakeMac(volume=40, muted=True)
        st = self.ducker(mac, duck_mode="both", report_volume=80).begin()
        self.assertEqual((mac.volume, mac.muted, st["ducked_volume"]), (40, True, False))

    def test_both_mode_pauses_and_sets_the_report_level(self):
        mac = FakeMac(volume=50, playing={"com.apple.Music"})
        st = self.ducker(mac, duck_mode="both", report_volume=70).begin()
        self.assertEqual((mac.playing, mac.volume, st["players"]), (set(), 70, ["com.apple.Music"]))

    def test_disabled_does_nothing(self):
        mac = FakeMac(playing={"com.apple.Music"})
        self.assertIsNone(self.ducker(mac, duck_enabled=False).begin())
        self.assertEqual(mac.calls, [])

    def test_apps_and_browsers_that_are_missing_are_skipped(self):
        mac = FakeMac(volume=50, playing={"com.apple.Music"}, installed={"com.apple.Music"})
        st = self.ducker(mac, report_volume=50).begin()
        self.assertEqual((st["players"], st["stuck"]), (["com.apple.Music"], []))
        self.assertFalse(st["ducked_volume"])

    def test_settings_are_clamped(self):
        self.cfg["voice"].update(duck_mode="loud", duck_volume=150, report_volume=-3)
        s = duck.settings(self.cfg)
        self.assertEqual((s["duck_mode"], s["duck_volume"], s["report_volume"]), ("pause", 100, 0))

    def test_odd_values_never_raise(self):
        for odd in (float("inf"), float("nan"), [1], {"a": 1}, "loud", True):
            self.cfg["voice"].update(duck_mode=odd, duck_volume=odd, report_volume=odd, duck_enabled=odd)
            s = duck.settings(self.cfg)
            self.assertEqual(s["duck_mode"], "pause")
            self.assertTrue(0 <= s["duck_volume"] <= 100 and 0 <= s["report_volume"] <= 100)
        for bad in ([], "x", 5):
            self.assertEqual(duck.settings({"voice": bad})["duck_volume"], duck.DEFAULTS["duck_volume"])

    def test_crash_mid_report_is_undone_at_the_next_start(self):
        mac = FakeMac(volume=65, playing={"com.spotify.client"}, running={"com.apple.Safari"},
                      denied={"com.apple.Safari"})
        self.ducker(mac).begin()                                             # ...and Jarviee dies here
        saved = json.loads(Path(self.cfg["duck_state_path"]).read_text())
        self.assertEqual((saved["volume"], saved["players"]), (65, ["com.spotify.client"]))
        self.assertTrue(duck.recover(self.cfg, run=mac))
        self.assertEqual((mac.volume, mac.playing), (65, {"com.spotify.client"}))
        self.assertFalse(Path(self.cfg["duck_state_path"]).exists())
        self.assertFalse(duck.recover(self.cfg, run=mac))                   # nothing left to undo

    def test_recover_leaves_another_live_speaker_alone(self):
        Path(self.cfg["duck_state_path"]).write_text(json.dumps({"pid": 1, "at": time.time(), "volume": 5}))
        mac = FakeMac(volume=20)
        self.assertFalse(duck.recover(self.cfg, run=mac))                   # pid 1 is alive and it's recent
        self.assertEqual(mac.volume, 20)


class RecordingDucker:
    def __init__(self, gain=1.0):
        self.events, self.gain = [], gain

    def begin(self):
        self.events.append("begin")
        return {"gain": self.gain}

    def end(self, st):
        self.events.append("end")


class SpeakerDuckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = {"alerts_state_path": str(Path(self._tmp.name) / "a.json"), "alerts": {"cooldown_s": 0},
                    "voice": {"enabled": True, "debounce_s": 0}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_queued_reports_duck_once_and_restore_after_the_last(self):
        d, said = RecordingDucker(), []
        sp = voice.Speaker(self.cfg, runner=lambda argv: (said.append(argv[-1]), d.events.append("say")),
                           start_worker=False, ducker=d)
        for p in ("One.", "Two.", "Three."):
            sp.speak(p)
        sp.drain()
        self.assertEqual(d.events, ["begin", "say", "say", "say", "end"])
        self.assertEqual(said, ["One.", "Two.", "Three."])

    def test_the_worker_keeps_the_duck_between_close_reports(self):
        d = RecordingDucker()
        sp = voice.Speaker(self.cfg, runner=lambda argv: (time.sleep(0.05), d.events.append("say")), ducker=d)
        try:
            sp.speak("First.")
            time.sleep(0.2)                     # arrives while ducked, inside the grace period
            sp.speak("Second.")
            end = time.time() + 5
            while d.events.count("end") < 1 and time.time() < end:
                time.sleep(0.05)
        finally:
            sp.stop()
        self.assertEqual(d.events, ["begin", "say", "say", "end"])

    def test_a_failing_report_still_restores(self):
        d = RecordingDucker()

        def boom(argv):
            raise RuntimeError("speaker exploded")
        sp = voice.Speaker(self.cfg, runner=boom, start_worker=False, ducker=d)
        sp._speak_locked = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tts crashed"))
        sp.speak("Hello.")
        sp.drain()
        self.assertEqual(d.events, ["begin", "end"])

    def test_muted_report_never_ducks(self):
        d = RecordingDucker()
        sp = voice.Speaker(self.cfg, runner=lambda argv: None, start_worker=False, ducker=d)
        self.cfg["voice"]["muted"] = True
        self.assertFalse(sp.speak("Hi."))
        sp.drain()
        self.assertEqual(d.events, [])

    def test_report_volume_gain_reaches_the_tts_worker(self):
        d = RecordingDucker(gain=3.5)
        self.cfg["voice"].update(engine="piper")
        sp = voice.Speaker(self.cfg, runner=voice.default_runner, start_worker=False, ducker=d)
        got = []
        sp._piper.speak = lambda model, knobs, text: got.append(knobs) or True
        sp.speak("Report.")
        sp.drain()
        self.assertEqual(got, ["1.25;3.5"])

    def test_the_real_speaker_gets_a_real_ducker_and_test_runners_do_not(self):
        self.assertIsInstance(voice.Speaker(self.cfg, start_worker=False).ducker, duck.Ducker)
        self.assertIsNone(voice.Speaker(self.cfg, runner=lambda a: None, start_worker=False).ducker)


class TtsWorkerProtocol(unittest.TestCase):
    def test_gain_is_optional_and_parsed_off_the_speed_field(self):
        for name in ("tts_piper.py", "tts_kokoro.py"):
            src = (ROOT / "hub" / name).read_text()
            self.assertIn('speed.partition(";")', src)
            self.assertIn('["-v", str(float(gain))] if gain else []', src)


if __name__ == "__main__":
    unittest.main()
