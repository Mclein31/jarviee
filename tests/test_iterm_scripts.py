"""Every iTerm2 AppleScript must compile. A script that names the app "iTerm2" fails to compile on macOS
versions that only resolve the bundle file name (iTerm.app), and every run then silently drops to the background."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hub import iterm


@unittest.skipUnless(shutil.which("osacompile"), "macOS only")
class ITermScriptTests(unittest.TestCase):
    def test_scripts_address_iterm_by_bundle_id(self):
        for name in ("BOUNDS", "CLOSE", "GRID", "LIST", "OPEN", "RESIZE", "SELECT", "SPLIT", "TAB", "TTY", "WRITE"):
            self.assertNotIn('application "iTerm2"', getattr(iterm, name), name)

    @unittest.skipUnless(Path("/Applications/iTerm.app").exists() or (Path.home() / "Applications/iTerm.app").exists(), "iTerm2 not installed")
    def test_every_script_compiles(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ("BOUNDS", "CLOSE", "GRID", "LIST", "OPEN", "RESIZE", "SELECT", "SPLIT", "TAB", "TTY", "WRITE"):
                src = Path(td) / f"{name}.applescript"
                src.write_text(getattr(iterm, name), encoding="utf-8")
                r = subprocess.run(["osacompile", "-o", str(Path(td) / f"{name}.scpt"), str(src)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
