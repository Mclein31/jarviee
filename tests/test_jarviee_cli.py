"""Tests for the bin/jarviee CLI (and the old bin/agent-hub name), project detection, PID management, and shared entry routines."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hub import entry
from hub.config import ROOT


class JarvieeCliTests(unittest.TestCase):
    def test_project_detection_from_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            proj_dir = Path(td) / "MyCoolProject"
            brain_dir = proj_dir / ".brain"
            sub_dir = proj_dir / "src" / "components"
            sub_dir.mkdir(parents=True)
            brain_dir.mkdir(parents=True)

            # Deep inside subfolder
            detected = entry.detect_project_from_dir(sub_dir)
            self.assertEqual(detected, "MyCoolProject")

            # At project root
            self.assertEqual(entry.detect_project_from_dir(proj_dir), "MyCoolProject")

            # Outside any project
            self.assertIsNone(entry.detect_project_from_dir(Path(td)))

    def test_stale_pid_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "server.pid"
            # Choose a PID that is definitely not running
            dead_pid = 99999999
            pid_file.write_text(str(dead_pid), encoding="utf-8")

            recovered = entry.clean_stale_pid(pid_file)
            self.assertEqual(recovered, dead_pid)
            self.assertFalse(pid_file.exists())

    def test_status_exit_code_when_stopped(self):
        with patch("hub.server_health.check_health_info", return_value=(False, {})):
            import runpy
            bin_path = ROOT / "bin" / "jarviee"
            mod = runpy.run_path(str(bin_path))
            code = mod["cmd_status"]({"port": 9999})
            self.assertEqual(code, 1)

    def test_status_exit_code_when_running(self):
        with patch("hub.server_health.check_health_info", return_value=(True, {"port": 7777, "pid": 1234})):
            import runpy
            bin_path = ROOT / "bin" / "jarviee"
            mod = runpy.run_path(str(bin_path))
            code = mod["cmd_status"]({"port": 7777})
            self.assertEqual(code, 0)

    def test_symlink_resolves_real_path(self):
        with tempfile.TemporaryDirectory() as td:
            symlink = Path(td) / "jarviee"
            real_bin = ROOT / "bin" / "jarviee"
            symlink.symlink_to(real_bin)

            res = subprocess.run([str(symlink), "--help"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("jarviee", res.stdout)
            self.assertIn("--no-open", res.stdout)

    def test_old_agent_hub_name_still_runs_jarviee(self):
        with tempfile.TemporaryDirectory() as td:
            symlink = Path(td) / "agent-hub"   # the ~/.local/bin link and old app point at this name
            symlink.symlink_to(ROOT / "bin" / "agent-hub")
            res = subprocess.run([str(symlink), "--help"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("usage: jarviee", res.stdout)

    def test_server_up_does_not_start_second_server(self):
        with patch("hub.entry.check_health", return_value=True), \
             patch("subprocess.Popen") as mock_popen:
            ok, started = entry.start_server_if_down({"port": 7777})
            self.assertTrue(ok)
            self.assertFalse(started)
            mock_popen.assert_not_called()

    def test_no_open_flag_does_not_open_browser(self):
        with patch("hub.entry.start_server_if_down", return_value=(True, False)), \
             patch("subprocess.run") as mock_run:
            bin_path = ROOT / "bin" / "jarviee"
            import runpy
            mod = runpy.run_path(str(bin_path))
            ret = mod["ensure_running"]({"port": 7777}, open_browser=False)
            self.assertEqual(ret, 0)
            mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
