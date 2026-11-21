"""Grid layout: one 4×2 iTerm2 window on the main screen, free panes take runs top-left first, a new grid tab only when
all eight are busy, freed panes come back; the pane font; the dashboard's own Safari window on the secondary screen."""
import json
import os
import plistlib
import pty
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path

import helpers
from fake_iterm import SCREENS, FakeITerm, make_terminal
from hub import actions, browser, displays, terminal, tiling

RUNNER = helpers.ROOT / "hooks" / "pane_runner.py"
MAIN = "0,30,1920,1080"   # the MSI's visible area in AppleScript bounds


class GridTiling(unittest.TestCase):
    def test_grid_tree_is_columns_of_stacked_panes(self):
        cells = [(c, r, f"S{c}{r}") for c in range(1, 5) for r in (1, 2)]
        tree = tiling.grid_tree(cells)
        self.assertEqual(tiling.shape(tree), (2, 2, 2, 2))
        self.assertEqual(tiling.reading_order(tree), ["S11", "S21", "S31", "S41", "S12", "S22", "S32", "S42"])

    def test_grid_split_restores_a_closed_pane_and_grows_columns_first(self):
        tree = tiling.grid_tree([(c, r, f"S{c}{r}") for c in range(1, 5) for r in (1, 2)])
        self.assertIsNone(tiling.grid_split(tree, 4, 2))                       # full
        gap = tiling.remove(tree, "S21")
        self.assertEqual(tiling.grid_split(gap, 4, 2), ("S22", "H"))           # the short column gets a pane again
        self.assertEqual(tiling.grid_split("S1", 4, 2), ("S1", "V"))
        two = tiling.split("S1", "S1", "V", "S2")
        self.assertEqual(tiling.grid_split(two, 4, 2), ("S2", "V"))            # columns before rows


class GridTerminal(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.TempDir()
        d = self.tmp.__enter__()
        self.cfg, _ = helpers.make_cfg(d)
        self.cfg["terminal"].update(layout="grid", grid=[4, 2], display="main", fullscreen=False, position="full",
                                    font_size=10, focus_on_launch=False)
        self.fake = FakeITerm()
        self.busy = set()   # ttys where something other than a free pane's waiter runs
        self.term = make_terminal(self.cfg, self.fake, d, font=lambda: "MesloLGS-NF-Regular",
                                  waiting=lambda tty: tty not in self.busy)
        self.dir = Path(d)

    def tearDown(self):
        self.tmp.__exit__()

    def test_prefill_opens_eight_free_panes_on_the_main_screen_with_the_small_font(self):
        msg = self.term.prefill()
        self.assertIn("4×2", msg)
        (args,) = self.fake.args_of("grid")
        self.assertEqual(args[0], "window")
        self.assertEqual(args[5], MAIN)                                         # normal window filling the main screen
        self.assertEqual(args[7:], ["Jarviee (window)", "4", "2"])
        self.assertTrue(args[2].endswith("--slot") and args[3].endswith("--slot"))
        self.assertIn("JARVIEE_SLOT=1", args[3])
        self.assertEqual(len(self.term.state["vacant"]), 8)
        prof = json.loads((self.dir / "jarviee.json").read_text())["Profiles"]
        self.assertEqual({p["Normal Font"] for p in prof}, {"MesloLGS-NF-Regular 10"})
        self.assertEqual(self.term.prefill(), "Jarviee window ready.")      # opening Jarviee again: same window
        self.assertEqual(len(self.fake.args_of("grid")), 1)

    def test_runs_fill_free_panes_top_left_first_and_a_new_tab_only_when_all_eight_are_busy(self):
        self.term.prefill()
        order = tiling.reading_order(self.term.state["tabs"][0])
        got = [self.term.open_pane(f"run-{i}", f"cmd run-{i}") for i in range(8)]
        self.assertEqual(got, order)
        self.assertEqual([a[2] for a in self.fake.args_of("write")], [f"@jarviee:run-{i}" for i in range(8)])
        self.assertEqual(self.fake.args_of("grid")[1:], [])                    # still one tab
        self.assertNotIn("split", self.fake.names())
        ninth = self.term.open_pane("run-8", "cmd run-8")
        (tab,) = self.fake.args_of("grid")[1:]
        self.assertEqual((tab[0], tab[2]), ("tab", "cmd run-8"))               # the run starts in the new tab's first pane
        self.assertEqual(self.term.state["runs"]["run-8"], ninth)
        self.assertEqual(len(self.term.state["vacant"]), 7)

    def test_a_freed_pane_takes_the_next_run_before_any_new_tab(self):
        self.term.prefill()
        for i in range(8):
            self.term.open_pane(f"run-{i}", f"cmd run-{i}")
        sid = self.term.state["runs"]["run-3"]
        code, body = actions.run_event({"_terminal": self.term}, {"id": "run-3", "event": "free"})
        self.assertEqual((code, body["freed"]), (200, True))
        self.assertFalse(self.term.has_pane("run-3"))
        self.assertEqual(self.term.open_pane("run-9", "cmd run-9"), sid)
        self.assertEqual(len(self.fake.args_of("grid")), 1)

    def test_never_types_into_a_pane_that_is_not_waiting(self):
        self.term.prefill()
        first = tiling.reading_order(self.term.state["tabs"][0])[0]
        self.busy.add(f"/dev/ttys{int(first[1:]):03d}")
        sid = self.term.open_pane("run-a", "cmd run-a")
        self.assertNotEqual(sid, first)
        self.assertNotIn(first, [a[1] for a in self.fake.args_of("write")])

    def test_first_run_without_a_window_builds_the_grid_around_it(self):
        sid = self.term.open_pane("run-a", "cmd run-a")
        (args,) = self.fake.args_of("grid")
        self.assertEqual(args[2], "cmd run-a")
        self.assertEqual(self.term.state["runs"]["run-a"], sid)
        self.assertEqual(len(self.term.state["vacant"]), 7)
        self.assertNotIn(sid, self.term.state["vacant"])

    def test_closing_a_pane_keeps_the_grid_whole(self):
        self.term.prefill()
        self.term.open_pane("run-a", "cmd run-a")
        self.assertTrue(self.term.close_pane("run-a"))
        split = self.fake.args_of("split")
        self.assertEqual(len(split), 1)
        self.assertTrue(split[0][3].endswith("--slot"))                     # a free pane takes its place
        self.assertEqual(len(tiling.leaves(self.term.state["tabs"][0])), 8)
        self.assertEqual(len(self.term.state["vacant"]), 8)

    def test_opening_again_moves_a_shrunk_window_back_over_the_main_screen(self):
        self.term.open_pane("run-a", "cmd run-a")
        self.term.state.update(fullscreen=True, rect=[1920, 81, 1710, 1112])   # left from the old layout
        self.fake.bounds = "0,30,640,1080"
        self.term.prefill()
        self.assertEqual(self.fake.bounds, MAIN)
        self.assertFalse(self.term.state["fullscreen"])
        self.assertEqual(self.term.state["rect"], [0, 30, 1920, 1050])

    def test_status_reports_the_layout_and_free_panes(self):
        self.term.prefill()
        self.term.open_pane("run-a", "cmd run-a")
        st = self.term.status()
        self.assertEqual((st["layout"], st["free"], st["panes"]), ("grid", 7, 1))

    def test_split_layout_keeps_its_old_command(self):
        self.cfg["terminal"]["layout"] = "split"
        cmd = terminal.pane_command(self.cfg, "run-a")
        self.assertNotIn("JARVIEE_SLOT", cmd)
        self.assertTrue(cmd.endswith("run-a"))
        self.assertTrue(terminal.pane_command(self.cfg).endswith("--idle"))


class Fonts(unittest.TestCase):
    def test_font_spec_and_default_font(self):
        self.assertIsNone(displays.font_spec(None, "X"))
        self.assertIsNone(displays.font_spec(2, "X"))
        self.assertEqual(displays.font_spec(10, "Menlo-Regular"), "Menlo-Regular 10")
        with helpers.TempDir() as d:
            p = Path(d) / "prefs.plist"
            p.write_bytes(plistlib.dumps({"Default Bookmark Guid": "B", "New Bookmarks": [
                {"Guid": "A", "Normal Font": "Monaco 12"}, {"Guid": "B", "Normal Font": "MesloLGS-NF-Regular 13"}]}))
            self.assertEqual(displays.default_font(p), "MesloLGS-NF-Regular")
            self.assertEqual(displays.default_font(Path(d) / "missing.plist"), displays.FALLBACK_FONT)

    def test_profile_without_font_inherits(self):
        self.assertTrue(all("Normal Font" not in p for p in displays.profile(1)["Profiles"]))


class DashboardWindow(unittest.TestCase):
    def setUp(self):
        self.calls, self.opened = [], []

    def fake_run(self, answer):
        def run(script, args):
            self.calls.append(args)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return run

    def test_new_safari_window_fills_the_laptop_screen(self):
        cfg = {"port": 7777, "dashboard_window": {"browser": "safari", "display": "secondary"}}
        out = browser.open_dashboard(cfg, run=self.fake_run("new"), screens=lambda full: SCREENS,
                                     fallback=self.opened.append)
        self.assertEqual(out, "new")
        self.assertEqual(self.calls, [["http://127.0.0.1:7777/", "1920,118,3630,1193"]])
        self.assertEqual(self.opened, [])

    def test_safari_refusing_falls_back_to_open(self):
        out = browser.open_dashboard({"port": 7777}, run=self.fake_run(ValueError("not authorized")),
                                     screens=lambda full: SCREENS, fallback=self.opened.append)
        self.assertEqual((out, self.opened), ("default", ["http://127.0.0.1:7777/"]))

    def test_default_browser_setting_uses_open(self):
        cfg = {"port": 7777, "dashboard_window": {"browser": "default"}}
        out = browser.open_dashboard(cfg, run=self.fake_run("new"), screens=lambda full: SCREENS,
                                     fallback=self.opened.append)
        self.assertEqual((out, self.calls), ("default", []))

    def test_one_screen_leaves_placement_to_safari(self):
        self.assertEqual(browser.bounds_for(SCREENS[:1], "secondary"), "")
        self.assertEqual(browser.bounds_for(SCREENS, "main"), MAIN)

    def test_script_reuses_only_a_window_that_holds_just_the_dashboard(self):
        self.assertIn("(count of tabs of w) is 1", browser.SAFARI)
        self.assertIn("make new document", browser.SAFARI)
        self.assertNotIn("7777", browser.SAFARI)   # values travel as arguments

    @unittest.skipUnless(Path("/usr/bin/osacompile").exists(), "macOS only")
    def test_safari_script_compiles(self):
        with helpers.TempDir() as d:
            src = Path(d) / "s.applescript"
            src.write_text(browser.SAFARI, encoding="utf-8")
            r = subprocess.run(["osacompile", "-o", str(Path(d) / "s.scpt"), str(src)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class FreePaneWaiter(unittest.TestCase):
    """The waiter in a real pty: ignores keys, starts the run it is handed, and comes back after the run ends."""

    def read_until(self, fd, text, timeout=10):
        out, end = b"", time.time() + timeout
        while time.time() < end and text.encode() not in out:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    break
        return out.decode("utf-8", "replace")

    def test_waiter_runs_the_handed_run_and_frees_the_pane_after(self):
        master, slave = pty.openpty()
        env = dict(os.environ, JARVIEE_PORT=str(helpers.free_port()), JARVIEE_TOKEN="t")   # no hub listening
        p = subprocess.Popen([sys.executable, str(RUNNER), "--slot"], stdin=slave, stdout=slave, stderr=slave,
                             env=env, start_new_session=True)
        os.close(slave)
        try:
            self.assertIn("free pane", self.read_until(master, "free pane"))
            os.write(master, b"hello\x03\x04\r")                             # stray keys, Ctrl-C, Ctrl-D: ignored
            time.sleep(0.3)
            self.assertIsNone(p.poll())
            os.write(master, b"@jarviee:run-x\r")
            seen = self.read_until(master, "can't reach the hub")           # it became run-x's runner
            self.assertIn("can't reach the hub", seen)
            self.assertIn("free pane", self.read_until(master, "free pane"))  # and the pane is free again
            self.assertIsNone(p.poll())
        finally:
            p.kill()
            p.wait(5)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
