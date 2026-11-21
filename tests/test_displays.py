"""Screen choice, window bounds, the dynamic profile and the macOS checks (pure functions)."""
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from hub import displays

# What this Mac reported (2026-11-09), in Cocoa coordinates: external main + built-in to the right.
COCOA = [
    {"index": 0, "name": "MSI MP251L E2", "builtin": False, "frame": [0, 0, 1920, 1080], "visible": [0, 0, 1920, 1050]},
    {"index": 1, "name": "Built-in Retina Display", "builtin": True, "frame": [1920, -113, 1710, 1112],
     "visible": [1920, -113, 1710, 1074.5], "scale": 2},
]
PROFILER = {"SPDisplaysDataType": [{"spdisplays_ndrvs": [
    {"_name": "MSI MP251L E2", "spdisplays_main": "spdisplays_yes"},
    {"_name": "Color LCD", "spdisplays_connection_type": "spdisplays_internal"}]}]}


def screen(i, name, builtin=False, w=1920, h=1080):
    return {"index": i, "name": name, "builtin": builtin, "frame": [0, 0, w, h], "visible": [0, 25, w, h - 25]}


class ScreenChoice(unittest.TestCase):
    def test_external_main_and_builtin_secondary_picks_the_laptop(self):
        screens = displays.to_top_left(COCOA)
        s, why = displays.pick_screen(screens)
        self.assertEqual((s["index"], s["name"], why), (1, "Built-in Retina Display", ""))
        self.assertEqual(s["frame"], [1920, 81, 1710, 1112])     # top-left space: 1080 - (-113 + 1112)

    def test_single_screen_uses_it(self):
        s, why = displays.pick_screen([screen(0, "Built-in Retina Display", True)])
        self.assertEqual(s["index"], 0)
        self.assertIn("only one screen", why)
        self.assertEqual(displays.pick_screen([]), (None, "macOS reported no screens"))

    def test_three_screens_prefers_the_builtin_among_secondaries(self):
        screens = [screen(0, "Main"), screen(1, "Left Dell"), screen(2, "Built-in", True)]
        self.assertEqual(displays.pick_screen(screens)[0]["index"], 2)
        no_builtin = [screen(0, "Main"), screen(1, "Left Dell"), screen(2, "Right Dell")]
        self.assertEqual(displays.pick_screen(no_builtin)[0]["index"], 1)

    def test_config_overrides(self):
        screens = [screen(0, "Main"), screen(1, "Built-in", True), screen(2, "Side")]
        self.assertEqual(displays.pick_screen(screens, "main")[0]["index"], 0)
        self.assertEqual(displays.pick_screen(screens, "builtin")[0]["index"], 1)
        self.assertEqual(displays.pick_screen(screens, 2)[0]["index"], 2)
        self.assertEqual(displays.pick_screen(screens, "2")[0]["index"], 2)
        s, why = displays.pick_screen(screens, 7)
        self.assertEqual((s["index"], why), (1, "no screen 7; using the secondary screen"))
        s, why = displays.pick_screen([screen(0, "Main"), screen(1, "Side")], "builtin")
        self.assertEqual(s["index"], 0)
        self.assertIn("no built-in", why)
        # the built-in screen as main (external is secondary): "secondary" → the external one
        self.assertEqual(displays.pick_screen([screen(0, "Built-in", True), screen(1, "Dell")])[0]["index"], 1)

    def test_system_profiler_cross_check(self):
        flags_missing = [dict(s, builtin=False) for s in displays.to_top_left(COCOA)]
        flags_missing[1]["name"] = "Built-in Retina Display"
        screens, note = displays.cross_check(flags_missing, PROFILER)
        self.assertEqual([s["builtin"] for s in screens], [False, True])   # name fallback ("built-in")
        self.assertEqual(note, "")
        _, note = displays.cross_check(displays.to_top_left(COCOA), {"SPDisplaysDataType": []})
        self.assertIn("1 built-in", note)


class WindowBounds(unittest.TestCase):
    def test_right_60_percent_on_different_screens(self):
        self.assertEqual(displays.window_bounds([0, 30, 1920, 1050]), [768, 30, 1920, 1080])
        self.assertEqual(displays.window_bounds([0, 25, 1440, 875], "right", 60), [576, 25, 1440, 900])
        self.assertEqual(displays.window_bounds([1920, 119, 1710, 1074], "right", 60), [2604, 119, 3630, 1193])
        self.assertEqual(displays.window_bounds([-2560, 0, 2560, 1415], "left", 50), [-2560, 0, -1280, 1415])
        self.assertEqual(displays.window_bounds([0, 25, 1440, 875], "full", 60), [0, 25, 1440, 900])


class DynamicProfile(unittest.TestCase):
    def test_profile_keys_and_codes(self):
        full, window = displays.profile(1)["Profiles"]
        self.assertEqual((full["Name"], full["Window Type"], full["Screen"]), ("Jarviee", 4, 1))  # LION_FULL_SCREEN
        self.assertEqual((window["Name"], window["Window Type"], window["Screen"]), ("Jarviee (window)", 0, -1))
        for p in (full, window):
            self.assertEqual((p["Custom Window Title"], p["Use Custom Window Title"]), ("Jarviee", True))
        self.assertNotEqual(full["Guid"], window["Guid"])

    def test_written_file_is_a_valid_property_list_and_rewritten_only_on_change(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "DynamicProfiles" / "jarviee.json"
            self.assertTrue(displays.install_profile(displays.profile(1), path))
            self.assertFalse(displays.install_profile(displays.profile(1), path))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["Profiles"][0]["Guid"], displays.PROFILE_GUID)
            # iTerm2 parses dynamic profiles as property lists: every value must be plist-encodable
            plistlib.loads(plistlib.dumps(data))
            self.assertEqual(displays.current_screen_index(path), 1)
            self.assertTrue(displays.install_profile(displays.profile(2), path))

    def test_inside(self):
        self.assertTrue(displays.inside([1920, 119, 3630, 1193], [1920, 81, 1710, 1112]))
        self.assertFalse(displays.inside([226, 488, 893, 950], [1920, 81, 1710, 1112]))   # smoke-test bug


class MacChecks(unittest.TestCase):
    def test_requirements(self):
        self.assertEqual(displays.problems(None, None), [])       # both unset = macOS / iTerm2 defaults (on)
        self.assertEqual(displays.problems("1", "0"), [])
        lion_off, spans = displays.problems("0", "1")
        self.assertIn("Native full screen windows", lion_off)
        self.assertIn("Displays have separate Spaces", spans)


if __name__ == "__main__":
    unittest.main()
