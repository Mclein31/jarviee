"""Tests for launchd background service and Jarviee.app launcher."""
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub import app_launcher, apple_calendar, service


class ServiceAppTests(unittest.TestCase):
    def test_resolve_path_dirs_includes_python(self):
        with tempfile.TemporaryDirectory() as td:
            py_dir = Path(td) / "pybin"
            py_dir.mkdir()
            fake_exe = py_dir / "python3"
            with patch.object(service, "CANONICAL_PYTHON", str(Path(td) / "missing" / "python3")), \
                 patch.object(service.sys, "executable", str(fake_exe)):
                paths = service.resolve_path_dirs()
                self.assertIsInstance(paths, str)
                self.assertIn(":", paths)
                self.assertEqual(paths.split(":")[0], str(py_dir))

            canon_dir = Path(td) / "canonbin"
            canon_dir.mkdir()
            canon_exe = canon_dir / "python3"
            canon_exe.write_text("")
            canon_exe.chmod(0o755)
            with patch.object(service, "CANONICAL_PYTHON", str(canon_exe)), \
                 patch.object(service.sys, "executable", "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"):
                paths = service.resolve_path_dirs()
                self.assertEqual(paths.split(":")[0], str(canon_dir))

    def test_generate_plist_dict(self):
        d = service.generate_plist_dict({"port": 7777})
        self.assertEqual(d["Label"], service.PLIST_LABEL)
        self.assertTrue(d["RunAtLoad"])
        self.assertTrue(d["KeepAlive"])
        self.assertTrue(d["ProgramArguments"][1].endswith("hub.py"))
        self.assertEqual(d["ProgramArguments"][2], "serve")
        self.assertIn("PATH", d["EnvironmentVariables"])
        self.assertEqual(d["EnvironmentVariables"]["PYTHONUNBUFFERED"], "1")

    def test_render_plist_xml_is_valid_plist(self):
        xml_str = service.render_plist_xml()
        parsed = plistlib.loads(xml_str.encode("utf-8"))
        self.assertEqual(parsed["Label"], service.PLIST_LABEL)
        self.assertEqual(parsed["WorkingDirectory"], str(service.ROOT.resolve()))

    def test_app_info_plist_valid(self):
        info = app_launcher.generate_info_plist()
        self.assertEqual(info["CFBundleName"], "Jarviee")
        self.assertEqual(info["CFBundleIdentifier"], "dev.jarviee.app")
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        self.assertEqual(info["CFBundleExecutable"], "jarviee-launcher")
        self.assertEqual(info["CFBundleIconFile"], "Jarviee")
        self.assertIn("<string>dev.jarviee.calendar</string>", apple_calendar._PLIST)

    def test_install_removes_legacy_labels_and_plists(self):
        self.assertEqual(service.PLIST_LABEL, "dev.jarviee.server")
        self.assertIn("com.mclein.jarviee", service.LEGACY_LABELS)
        self.assertIn("com.mclein.agenthub", service.LEGACY_LABELS)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["launchctl", "print"]:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as td:
            plist_dir = Path(td) / "LaunchAgents"
            plist_dir.mkdir()
            log_path = Path(td) / "logs" / "server.log"
            for legacy in service.LEGACY_LABELS:
                (plist_dir / f"{legacy}.plist").write_text("<plist/>", encoding="utf-8")

            with patch.object(service, "DEFAULT_PLIST_DIR", plist_dir), \
                 patch.object(service, "DEFAULT_LOG_PATH", log_path), \
                 patch.object(service, "get_uid", return_value=501), \
                 patch.object(service.subprocess, "run", side_effect=fake_run):
                ok = service.install()

            self.assertTrue(ok)
            for legacy in service.LEGACY_LABELS:
                self.assertFalse((plist_dir / f"{legacy}.plist").exists())
                self.assertIn(["launchctl", "bootout", f"gui/501/{legacy}"], calls)
            self.assertTrue((plist_dir / "dev.jarviee.server.plist").is_file())
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist_dir / "dev.jarviee.server.plist")], calls)

    def test_app_launcher_script_invokes_jarviee_bin(self):
        script = app_launcher.generate_launcher_script({"port": 8888})
        self.assertIn("bin/jarviee", script)
        self.assertIn('exec "', script)

    def test_dry_run_does_not_modify_filesystem(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(service, "DEFAULT_PLIST_DIR", Path(td)):
                ok = service.install(dry_run=True)
                self.assertTrue(ok)
                self.assertFalse((Path(td) / service.PLIST_FILENAME).exists())

        with tempfile.TemporaryDirectory() as td:
            with patch.object(app_launcher, "APP_PATH", Path(td) / "Jarviee.app"):
                ok = app_launcher.install(dry_run=True)
                self.assertTrue(ok)
                self.assertFalse((Path(td) / "Jarviee.app").exists())

    def test_app_install_to_custom_dir(self):
        with tempfile.TemporaryDirectory() as td:
            custom_app = Path(td) / "Jarviee.app"
            with patch.object(app_launcher, "APP_PATH", custom_app), \
                 patch("hub.icon.build_icns", return_value=None):
                ok = app_launcher.install({"port": 7777}, dry_run=False)
                self.assertTrue(ok)
                self.assertTrue(custom_app.is_dir())
                launcher_bin = custom_app / "Contents" / "MacOS" / "jarviee-launcher"
                self.assertTrue(launcher_bin.is_file())
                # Verify executable bit
                mode = launcher_bin.stat().st_mode
                self.assertTrue(bool(mode & stat.S_IXUSR))
                # Verify Info.plist
                info_file = custom_app / "Contents" / "Info.plist"
                self.assertTrue(info_file.is_file())
                data = plistlib.loads(info_file.read_bytes())
                self.assertEqual(data["CFBundleName"], "Jarviee")


class FrameworkPython(unittest.TestCase):
    def test_framework_stub_maps_to_python_app(self):
        import tempfile
        from hub import service
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp) / "Python.framework" / "Versions" / "3.14"
            (v / "bin").mkdir(parents=True)
            app = v / "Resources" / "Python.app" / "Contents" / "MacOS"
            app.mkdir(parents=True)
            (v / "bin" / "python3").write_text("")
            (app / "Python").write_text("")
            (app / "Python").chmod(0o755)
            self.assertEqual(service.service_python_exe(str(v / "bin" / "python3")), str(app / "Python"))
            self.assertEqual(service.service_python_exe("/usr/bin/python3"), "/usr/bin/python3")
            (app / "Python").unlink()
            self.assertEqual(service.service_python_exe(str(v / "bin" / "python3")), str(v / "bin" / "python3"))


if __name__ == "__main__":
    unittest.main()
