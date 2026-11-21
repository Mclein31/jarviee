"""Every dashboard script must parse: one syntax error stops the whole dashboard from loading."""
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "hub" / "static"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class StaticScriptsParse(unittest.TestCase):
    def test_every_script_parses(self):
        for f in sorted(STATIC.glob("*.js")):
            with self.subTest(script=f.name):
                r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, r.stderr[-500:])

    def test_render_functions_avoid_insert_adjacent_html(self):
        """insertAdjacentHTML inside render loops causes repeated duplicate options on every poll."""
        for f in sorted(STATIC.glob("*.js")):
            with self.subTest(script=f.name):
                content = f.read_text(encoding="utf-8")
                self.assertNotIn("insertAdjacentHTML", content, f"{f.name} should use J.setHTML instead of insertAdjacentHTML")


class CalendarWriteNeedsSeenPreview(unittest.TestCase):
    def test_launch_writes_only_the_preview_already_on_screen(self):
        """Cmd+Enter writes a calendar event only when the preview for exactly this text was rendered before."""
        src = (STATIC / "composer.js").read_text(encoding="utf-8")
        self.assertIn("a.shown = true;", src)   # renderAdd marks the reading as on screen
        self.assertIn("addEv && addEv.shown && addEv.text === text", src)
        self.assertIn("if (seen && a.cal) await addToCalendar(a)", src)
        self.assertNotIn("if (a.cal) await addToCalendar(a)", src)


if __name__ == "__main__":
    unittest.main()
