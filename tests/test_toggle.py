"""jarviee --disable / --enable (hub/toggle.py): every hook-in is removed and put back; nothing real is touched."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT  # noqa: F401  (puts the repo on sys.path)
from hub import entry, install, toggle

THEIR_HOOK = {"matcher": "*", "hooks": [{"type": "command", "command": "echo mine"}]}


class FakeService:
    def __init__(self, installed=True):
        self.installed, self.calls = installed, []

    def is_installed(self):
        return self.installed

    def uninstall(self):
        self.calls.append("uninstall")
        self.installed = False
        return True

    def install(self, cfg=None):
        self.calls.append("install")
        self.installed = True
        return True


class FakeEntry:
    def __init__(self):
        self.calls = []

    def stop_server(self, cfg=None):
        self.calls.append("stop")

    def start_server_if_down(self, cfg=None):
        self.calls.append("start")
        return True, True


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.claude, self.agy = d / "claude.json", d / "hooks.json"
        self.ours_sl = {"type": "command", "command": f"python3 {ROOT}/hooks/statusline.py", "refreshInterval": 60}
        base = {"theme": "dark", "statusLine": self.ours_sl, "hooks": {"PreToolUse": [THEIR_HOOK]}}
        self.claude.write_text(json.dumps(base))
        self.agy.write_text("{}")
        self.env = mock.patch.dict(os.environ, {"JARVIEE_CLAUDE_SETTINGS": str(self.claude),
                                                "JARVIEE_AGY_HOOKS": str(self.agy)})
        self.env.start()
        with redirect_stdout(io.StringIO()):
            install.run("claude"), install.run("agy")
        self.cfg = {"disabled_marker_path": str(d / "disabled.json"), "port": 7777}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_quiet(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            code = fn(self.cfg, **kw)
        return code, out.getvalue()

    def claude_settings(self):
        return json.loads(self.claude.read_text())

    def ours_present(self):
        hooks = self.claude_settings().get("hooks", {})
        return any(install.is_ours(h) for groups in hooks.values() for g in groups for h in g.get("hooks", []))

    def test_disable_takes_out_everything_and_keeps_the_owners_own_settings(self):
        svc, ent = FakeService(), FakeEntry()
        self.assertTrue(self.ours_present())
        code, out = self.run_quiet(toggle.disable, service=svc, entry=ent)
        self.assertEqual(code, 0)
        s = self.claude_settings()
        self.assertFalse(self.ours_present())
        self.assertNotIn("statusLine", s)
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["hooks"]["PreToolUse"], [THEIR_HOOK])           # the owner's own hook stays
        self.assertNotIn(install.HOOK_NAME, json.loads(self.agy.read_text()))
        self.assertEqual((svc.calls, ent.calls), (["uninstall"], ["stop"]))
        self.assertTrue(toggle.is_disabled(self.cfg))
        self.assertIn("jarviee --enable", out)

    def test_enable_puts_everything_back(self):
        self.run_quiet(toggle.disable, service=FakeService(), entry=FakeEntry())
        svc, ent = FakeService(installed=False), FakeEntry()
        code, out = self.run_quiet(toggle.enable, service=svc, entry=ent)
        self.assertEqual(code, 0)
        self.assertTrue(self.ours_present())
        self.assertEqual(self.claude_settings()["statusLine"], self.ours_sl)
        self.assertIn(install.HOOK_NAME, json.loads(self.agy.read_text()))
        self.assertEqual((svc.calls, ent.calls), (["install"], ["start"]))
        self.assertFalse(toggle.is_disabled(self.cfg))

    def test_enable_never_replaces_a_status_line_set_meanwhile(self):
        self.run_quiet(toggle.disable, service=FakeService(), entry=FakeEntry())
        s = self.claude_settings()
        s["statusLine"] = {"type": "command", "command": "my-own-line"}
        self.claude.write_text(json.dumps(s))
        self.run_quiet(toggle.enable, service=FakeService(False), entry=FakeEntry())
        self.assertEqual(self.claude_settings()["statusLine"]["command"], "my-own-line")

    def test_disable_leaves_a_status_line_that_is_not_ours(self):
        s = self.claude_settings()
        s["statusLine"] = {"type": "command", "command": "my-own-line"}
        self.claude.write_text(json.dumps(s))
        self.run_quiet(toggle.disable, service=FakeService(), entry=FakeEntry())
        self.assertEqual(self.claude_settings()["statusLine"]["command"], "my-own-line")

    def test_disabling_twice_keeps_the_saved_status_line_and_service_flag(self):
        self.run_quiet(toggle.disable, service=FakeService(), entry=FakeEntry())
        self.run_quiet(toggle.disable, service=FakeService(installed=False), entry=FakeEntry())
        saved = json.loads(Path(self.cfg["disabled_marker_path"]).read_text())
        self.assertEqual(saved["statusLine"], self.ours_sl)
        self.assertTrue(saved["service"])
        self.run_quiet(toggle.enable, service=FakeService(False), entry=FakeEntry())
        self.assertEqual(self.claude_settings()["statusLine"], self.ours_sl)

    def test_service_is_uninstalled_before_the_server_is_stopped(self):
        order = []
        svc, ent = FakeService(), FakeEntry()
        svc.uninstall = lambda: order.append("uninstall")
        ent.stop_server = lambda cfg=None: order.append("stop")
        self.run_quiet(toggle.disable, service=svc, entry=ent)
        self.assertEqual(order, ["uninstall", "stop"])

    def test_enable_installs_the_service_only_when_it_was_installed_or_there_is_no_marker(self):
        self.run_quiet(toggle.disable, service=FakeService(installed=False), entry=FakeEntry())   # was started by hand
        svc, ent = FakeService(installed=False), FakeEntry()
        self.run_quiet(toggle.enable, service=svc, entry=ent)
        self.assertEqual((svc.calls, ent.calls), ([], ["start"]))
        svc2 = FakeService(installed=False)   # no marker at all: install as before
        self.run_quiet(toggle.enable, service=svc2, entry=FakeEntry())
        self.assertEqual(svc2.calls, ["install"])

    def test_dry_run_changes_nothing(self):
        before = (self.claude.read_text(), self.agy.read_text())
        svc, ent = FakeService(), FakeEntry()
        code, out = self.run_quiet(toggle.disable, dry_run=True, service=svc, entry=ent)
        self.assertEqual(code, 0)
        self.assertEqual((self.claude.read_text(), self.agy.read_text()), before)
        self.assertEqual((svc.calls, ent.calls), ([], []))
        self.assertFalse(toggle.is_disabled(self.cfg))
        self.assertIn("would", out)

    def test_nothing_starts_the_server_while_disabled(self):
        self.run_quiet(toggle.disable, service=FakeService(), entry=FakeEntry())
        with mock.patch.object(entry, "check_health", side_effect=AssertionError("must not even check")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(entry.start_server_if_down(self.cfg), (False, False))

    def test_cli_flags_reach_the_toggle_and_block_start_while_disabled(self):
        bin_path = str(Path(ROOT) / "bin" / "jarviee")
        env = dict(os.environ, JARVIEE_CONFIG=str(Path(self.tmp.name) / "cfg.json"))
        real = json.loads((Path(ROOT) / "config.json").read_text())
        real.update(disabled_marker_path=self.cfg["disabled_marker_path"], db_path=str(Path(self.tmp.name) / "t.db"),
                    logs_dir=str(Path(self.tmp.name) / "logs"))
        Path(env["JARVIEE_CONFIG"]).write_text(json.dumps(real))
        Path(self.cfg["disabled_marker_path"]).write_text("{}")
        r = subprocess.run([sys.executable, bin_path, "--no-open"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn("jarviee --enable", r.stderr)
        r = subprocess.run([sys.executable, bin_path, "--disable", "--dry-run"], capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Dry run", r.stdout)


if __name__ == "__main__":
    unittest.main()
