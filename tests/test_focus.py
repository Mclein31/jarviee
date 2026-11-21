"""Jump to an agent's window: process-tree walk to the hosting app and the per-host routes."""
import unittest
from unittest.mock import patch

from hub import focus

TREE = {300: (200, "ttys004", "/Users/alex/.local/bin/claude"), 200: (150, "ttys004", "-zsh"),
        150: (1, "??", "/Applications/iTerm.app/Contents/MacOS/iTerm2"),
        900: (800, "??", "/Users/alex/.vscode/extensions/anthropic.claude-code/resources/native-binary/claude"),
        800: (700, "??", "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin)")}


class FocusTests(unittest.TestCase):
    def test_host_of_finds_the_app_and_the_tty(self):
        with patch.object(focus, "_ps", side_effect=lambda pid: TREE.get(pid)):
            self.assertEqual(focus.host_of(300), ("/Applications/iTerm.app", "ttys004"))
            self.assertEqual(focus.host_of(900), ("/Applications/Visual Studio Code.app", ""))
            self.assertEqual(focus.host_of(12345), ("", ""))

    def test_iterm_session_is_selected_by_tty_and_editor_opens_the_folder(self):
        with patch.object(focus, "_ps", side_effect=lambda pid: TREE.get(pid)), \
                patch.object(focus, "bundle_id", side_effect=lambda app: focus.ITERM if "iTerm" in app else "com.microsoft.VSCode"), \
                patch.object(focus, "_osa", return_value="ok") as osa, \
                patch.object(focus, "_open_app", return_value=(True, "Showing Visual Studio Code.")) as opener:
            ok, msg = focus.focus({"id": "claude-a", "source": "hook", "agent_pid": 300}, {})
            self.assertTrue(ok)
            self.assertEqual(osa.call_args[0][1], "ttys004")          # the tty travels as an argument
            ok, _ = focus.focus({"id": "claude-b", "source": "hook", "agent_pid": 900, "workspace": "/tmp"}, {})
            self.assertTrue(ok)
            self.assertEqual(opener.call_args[0][1], "/tmp")

    def test_iterm_session_server_and_framework_apps(self):
        tree = {**TREE,
                400: (390, "ttys007", "/Users/alex/.local/bin/claude"), 390: (380, "ttys007", "-zsh"),
                380: (370, "ttys007", "/usr/bin/login"),
                370: (1, "??", "/Users/alex/Library/Application Support/iTerm2/iTermServer-3.5.14"),
                500: (300, "??", "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python")}
        with patch.object(focus, "_ps", side_effect=lambda pid: tree.get(pid)):
            self.assertEqual(focus.host_of(400), (focus.ITERM_SERVER, "ttys007"))
            self.assertEqual(focus.host_of(500), ("/Applications/iTerm.app", "ttys004"))   # skips Python.app
            with patch.object(focus, "_osa", return_value="ok") as osa, patch.object(focus, "bundle_id") as bid:
                ok, _ = focus.focus({"id": "claude-d", "source": "hook", "agent_pid": 400}, {})
                self.assertTrue(ok)
                self.assertEqual(osa.call_args[0][1], "ttys007")
                bid.assert_not_called()

    def test_headless_launcher_run_has_no_window(self):
        with patch.object(focus, "host_of") as walk:
            ok, msg = focus.focus({"id": "r1", "source": "launcher", "pid": 123}, {})
        self.assertFalse(ok)
        self.assertIn("headless", msg)
        walk.assert_not_called()

    def test_session_without_a_process_explains(self):
        ok, msg = focus.focus({"id": "claude-c", "source": "hook"}, {})
        self.assertFalse(ok)
        self.assertIn("process", msg)

    def test_scripts_never_embed_values(self):
        for script in (focus.ITERM_SELECT, focus.TERMINAL_SELECT):
            self.assertIn("item 1 of argv", script)


if __name__ == "__main__":
    unittest.main()
