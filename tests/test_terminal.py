"""The iTerm2 controller against a fake osascript: generated scripts, arguments, tiling, fallbacks."""
import json
import unittest
from pathlib import Path

import helpers
from fake_iterm import FakeITerm, make_terminal
from hub import iterm, terminal

CMD = "/usr/bin/env JARVIEE_PORT=7777 python3 pane_runner.py"
LAPTOP = "1920,81,3630,1193"


class Controller(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.TempDir()
        d = self.tmp.__enter__()
        self.cfg, _ = helpers.make_cfg(d)
        self.fake = FakeITerm()
        self.term = make_terminal(self.cfg, self.fake, d)
        self.dir = Path(d)

    def tearDown(self):
        self.tmp.__exit__()

    def test_create_split_close_select(self):
        s1 = self.term.open_pane("run-a", CMD + " run-a")
        self.assertEqual(self.fake.args_of("open"), [[CMD + " run-a", "0", "", LAPTOP, "Jarviee"]])
        prof = json.loads((self.dir / "jarviee.json").read_text())["Profiles"][0]
        self.assertEqual((prof["Window Type"], prof["Screen"]), (4, 1))    # native full screen on the laptop
        s2 = self.term.open_pane("run-b", CMD + " run-b")
        self.assertEqual(self.fake.args_of("split"), [["100", s1, "V", CMD + " run-b", "0", LAPTOP]])
        self.term.open_pane("run-c", CMD + " run-c")
        self.assertEqual(self.fake.args_of("split")[-1][1:3], [s2, "H"])   # one left + two stacked right
        self.assertTrue(self.term.close_pane("run-b"))
        self.assertEqual(self.fake.args_of("close"), [["100", s2]])
        self.assertEqual(self.term.select_pane("run-a"), (True, ""))
        self.assertEqual(self.fake.args_of("select"), [["100", s1]])
        self.assertEqual(json.loads((self.dir / "terminal.json").read_text())["runs"], {"run-a": s1, "run-c": "S3"})
        self.assertNotIn("resize", self.fake.names())        # full screen: splits stay even, no resizing

    def test_scripts_are_fixed_and_take_values_as_arguments(self):
        names = ("OPEN", "SPLIT", "TAB", "CLOSE", "SELECT", "LIST", "RESIZE", "BOUNDS")
        for name in names:
            src = getattr(iterm, name)
            self.assertIn("on run argv", src)
            self.assertNotIn("run-", src)
        self.term.open_pane("run-a", CMD + " run-a")
        self.term.open_pane("run-b", CMD + " run-b")
        for _, _, script in self.fake.calls:                  # only the fixed scripts are ever sent
            self.assertTrue(any(script is getattr(iterm, n) for n in names))

    def test_seventh_pane_opens_a_tab_and_the_last_full_screen_pane_leaves_nothing_behind(self):
        for i in range(7):
            self.term.open_pane(f"run-{i}", f"{CMD} run-{i}")
        self.assertEqual(self.fake.names().count("tab"), 1)
        self.assertEqual([len(t) for t in self.fake.tabs], [6, 1])
        for i in range(6):
            self.term.close_pane(f"run-{i}")
        last = self.term.state["runs"]["run-6"]
        closes = len(self.fake.args_of("close"))
        self.assertTrue(self.term.close_pane("run-6"))
        # the full screen window's last pane: no idle placeholder, no scripted close (its runner exits instead)
        self.assertEqual(len(self.fake.args_of("close")), closes)
        self.assertIsNone(self.term.state["idle"])
        self.assertIsNone(self.term.state["window"])
        self.assertFalse(any(a[3].endswith("--idle") for a in self.fake.args_of("split")))
        self.assertIn(last, sum(self.fake.tabs, []))   # still there until its runner exits

    def test_release_last_only_for_the_only_full_screen_pane(self):
        self.term.open_pane("run-a", CMD + " run-a")
        self.term.open_pane("run-b", CMD + " run-b")
        self.assertFalse(self.term.release_last("run-a"))     # another pane is still open
        self.term.close_pane("run-a")
        self.assertTrue(self.term.release_last("run-b"))
        self.assertIsNone(self.term.state["window"])
        self.assertFalse(self.term.has_pane("run-b"))
        self.assertFalse(self.term.release_last("run-b"))

    def test_unreadable_listing_never_opens_a_second_window(self):
        # Found in the smoke test: `tab` inside an iTerm2 tell block is the tab class, so a listing came
        # back as "1tabFBB9…tab212tab62"; read as "no panes", every run opened a new full screen window.
        self.assertNotIn("& tab &", iterm.LIST)
        self.term.open_pane("run-a", CMD + " run-a")
        real = self.fake.__call__
        self.term.run = lambda s, a=(): "1tabS1tab212tab62" if s is iterm.LIST else real(s, a)
        with self.assertRaises(iterm.TerminalError):
            self.term.open_pane("run-b", CMD + " run-b")
        self.assertEqual(self.fake.names(), ["open"])
        self.assertEqual(self.term.state["window"], 100)

    def test_window_closed_by_hand_is_recreated(self):
        self.term.open_pane("run-a", CMD + " run-a")
        self.fake.tabs = []                                   # owner closed the window
        self.term.open_pane("run-b", CMD + " run-b")
        self.assertEqual(self.fake.names(), ["open", "open"])
        self.assertEqual(self.term.state["runs"], {"run-b": "S2"})

    def test_single_screen_opens_a_normal_window_on_the_right(self):
        self.term.read_screens = lambda *a: [{"index": 0, "name": "Built-in", "builtin": True,
                                              "frame": [0, 0, 1710, 1112], "visible": [0, 38, 1710, 1074]}]
        self.term.open_pane("run-a", CMD + " run-a")
        self.assertEqual(self.fake.args_of("open")[0][2:], ["684,38,1710,1112", "0,0,0,0", "Jarviee (window)"])
        self.assertFalse(self.term.state["fullscreen"])
        self.assertIn("only one screen", self.term.status()["notices"][0]["text"])

    def test_normal_window_never_rewrites_the_full_screen_profile(self):
        self.term.open_pane("run-a", CMD + " run-a")
        path = self.dir / "jarviee.json"
        before = path.read_text()
        self.fake.tabs = []                                   # window gone; now only one screen
        self.term.read_screens = lambda *a: [{"index": 0, "name": "Built-in", "builtin": True,
                                              "frame": [0, 0, 1710, 1112], "visible": [0, 38, 1710, 1074]}]
        self.term.open_pane("run-b", CMD + " run-b")
        self.assertEqual(path.read_text(), before)            # no rewrite → no reload race next time

    def test_full_screen_window_in_the_wrong_place_is_moved_and_explained(self):
        # Found in the smoke test: a window opened right after a profile rewrite used the stale profile
        # and landed on the main monitor as a normal 667x462 window.
        self.fake.bounds = "226,488,893,950"
        self.term.open_pane("run-a", CMD + " run-a")
        self.assertEqual(self.fake.args_of("bounds")[-1], ["100", "1920,118,3630,1193"])   # below the menu bar
        self.assertFalse(self.term.state["fullscreen"])
        self.assertIn("didn't open the Jarviee window in full screen", self.term.status()["notices"][0]["text"])

    def test_focus_goes_back_to_the_app_in_front(self):
        fronts, woken = iter(["com.apple.Safari", "com.googlecode.iterm2", "com.apple.Safari", "com.apple.Safari"]), []
        self.term.front, self.term.activate = lambda: next(fronts), woken.append
        self.term.open_pane("run-a", CMD + " run-a")          # iTerm2 grabbed focus: Safari gets it back
        self.term.open_pane("run-b", CMD + " run-b")          # nothing moved: nothing to do
        self.assertEqual(woken, ["com.apple.Safari"])
        self.term.t["focus_on_launch"] = True
        self.term.front = lambda: self.fail("focus_on_launch: the owner wants the pane in front")
        self.term.open_pane("run-c", CMD + " run-c")

    def test_unplugged_display_keeps_the_window_and_explains(self):
        self.term.open_pane("run-a", CMD + " run-a")
        self.term.read_screens = lambda *a: [   # the laptop is gone: only the external monitor is left
            {"index": 0, "name": "MSI MP251L E2", "builtin": False, "frame": [0, 0, 1920, 1080]}]
        self.term.open_pane("run-b", CMD + " run-b")          # same window, new pane, no crash
        self.assertEqual(self.fake.names(), ["open", "split"])
        self.assertIn("isn't connected", self.term.status()["notices"][0]["text"])

    def test_permission_denied_raises_and_explains(self):
        self.fake.fail = iterm.PermissionDenied("Not authorized to send Apple events to iTerm2. (-1743)")
        with self.assertRaises(iterm.PermissionDenied):
            self.term.open_pane("run-a", CMD + " run-a")
        notes = self.term.status()["notices"]
        self.assertIn("Privacy & Security › Automation", notes[0]["text"])
        self.term.dismiss(notes[0]["key"])
        self.assertEqual(self.term.status()["notices"], [])

    def test_settings_toggle_and_mode(self):
        self.assertTrue(self.term.enabled())
        self.term.set_enabled(False)
        self.assertFalse(self.term.enabled())
        self.assertIsNone(terminal.active(self.cfg))          # not serving, and switched off
        self.assertEqual(terminal.pane_command({"port": 7777}, "run-ab12")[-8:], "run-ab12")
        with self.assertRaises(ValueError):
            terminal.pane_command({"port": 7777}, 'run-x" & do shell script "id')


if __name__ == "__main__":
    unittest.main()
