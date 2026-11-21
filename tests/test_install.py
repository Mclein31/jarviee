import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from helpers import TempDir
from hub import install

FOREIGN_CLAUDE = {
    "theme": "dark",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "~/guard.sh"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
}
FOREIGN_AGY = {"lint-checker": {"PostToolUse": [{"matcher": "run_command",
                                                 "hooks": [{"command": "./lint.sh"}]}]}}


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())

    def tearDown(self):
        self._t.__exit__()

    def test_claude_merge_keeps_foreign_hooks(self):
        m = install.merge_claude(FOREIGN_CLAUDE)
        self.assertEqual(m["theme"], "dark")
        self.assertEqual(m["hooks"]["PreToolUse"][0], FOREIGN_CLAUDE["hooks"]["PreToolUse"][0])
        self.assertEqual(len(m["hooks"]["PreToolUse"]), 2)
        self.assertTrue(install.is_ours(m["hooks"]["PreToolUse"][1]["hooks"][0]))
        self.assertEqual(set(m["hooks"]), set(install.CLAUDE_EVENTS))
        self.assertEqual(install.merge_claude(m), m)                     # idempotent
        self.assertEqual(install.merge_claude(m, uninstall=True), FOREIGN_CLAUDE)
        self.assertNotIn("hooks", install.merge_claude({"x": 1}, uninstall=True))

    def test_agy_merge_named_hook(self):
        m = install.merge_agy(FOREIGN_AGY)
        self.assertEqual(m["lint-checker"], FOREIGN_AGY["lint-checker"])
        spec = m[install.HOOK_NAME]
        self.assertNotIn("PreToolUse", spec)              # never gate tool calls
        self.assertEqual(spec["PostToolUse"][0]["matcher"], "*")
        self.assertIn("command", spec["Stop"][0])          # flat events list handlers directly
        self.assertTrue(spec["Stop"][0]["command"].endswith("report.py Stop agy"))
        self.assertEqual(install.merge_agy(m, uninstall=True), FOREIGN_AGY)

    def test_run_writes_backup_and_keeps_mode(self):
        f = self.tmp / "settings.json"
        f.write_text(json.dumps(FOREIGN_CLAUDE), encoding="utf-8")
        os.chmod(f, 0o600)
        self.assertTrue(quiet(install.run, "claude", path=f))
        backups = list(self.tmp.glob("settings.json.jarviee-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), FOREIGN_CLAUDE)
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertTrue(quiet(install.run, "claude", path=f))           # no change → no new backup
        self.assertEqual(len(list(self.tmp.glob("settings.json.jarviee-backup-*"))), 1)
        self.assertTrue(quiet(install.run, "claude", path=f, uninstall=True))
        self.assertEqual(json.loads(f.read_text()), FOREIGN_CLAUDE)
        self.assertEqual(len(list(self.tmp.glob("settings.json.jarviee-backup-*"))), 2)

    def test_dry_run_writes_nothing(self):
        f = self.tmp / "hooks.json"
        self.assertTrue(quiet(install.run, "agy", path=f, dry_run=True))
        self.assertFalse(f.exists())
        self.assertTrue(quiet(install.run, "agy", path=f))
        self.assertIn(install.HOOK_NAME, json.loads(f.read_text()))

    def test_invalid_json_left_untouched(self):
        f = self.tmp / "settings.json"
        f.write_text("{ not json", encoding="utf-8")
        self.assertFalse(quiet(install.run, "claude", path=f))
        self.assertEqual(f.read_text(), "{ not json")
        self.assertEqual(list(self.tmp.glob("*.jarviee-backup-*")), [])


if __name__ == "__main__":
    unittest.main()
