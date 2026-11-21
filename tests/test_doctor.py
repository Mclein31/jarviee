import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401
from hub import doctor, install


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.ws = self.tmp / "ws"
        (self.ws / "p" / ".brain").mkdir(parents=True)
        self.ov = self.tmp / "ov.json"
        self.ov.write_text(json.dumps({"obsidian": {"workspace_root": str(self.ws)}, "terminal": {"mode": "background"}}))
        self.data = self.tmp / "data"
        self.data.mkdir(mode=0o700)
        env = {"JARVIEE_USER_CONFIG": str(self.ov), "JARVIEE_CLAUDE_SETTINGS": str(self.tmp / "s.json"),
               "JARVIEE_AGY_HOOKS": str(self.tmp / "h.json")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch("hub.brand.home", return_value=self.data)
        q.start()
        self.addCleanup(q.stop)
        t = mock.patch("hub.auth.token_path", return_value=self.data / "token")
        t.start()
        self.addCleanup(t.stop)

    def go(self, which=lambda n: "/bin/" + n, health=lambda c: True):
        runner = mock.Mock(return_value=mock.Mock(stdout="1.0\n", stderr=""))
        return doctor.checks(which=which, runner=runner, health=health)

    def levels(self, res):
        return {l for l, *_ in res}

    def test_missing_hooks_warn_not_fail(self):
        res = self.go()
        self.assertNotIn("fail", self.levels(res))
        self.assertTrue(any("Hooks not installed for claude" in t for _, t, _h in res))
        self.assertTrue(any("1 project" in t for _, t, _h in res))

    def test_detects_installed_hooks(self):
        install.run("claude", path=self.tmp / "s.json")
        install.run("agy", path=self.tmp / "h.json")
        self.assertTrue(doctor.hooks_installed("claude"))
        self.assertTrue(doctor.hooks_installed("agy"))

    def test_missing_workspace_fails(self):
        self.ov.write_text(json.dumps({"obsidian": {"workspace_root": str(self.tmp / "gone")}}))
        res = self.go()
        self.assertIn("fail", self.levels(res))
        self.assertFalse(doctor.run(out=lambda *_: None, which=lambda n: None, health=lambda c: False))

    def test_server_down_and_missing_cli_are_warnings(self):
        res = self.go(which=lambda n: None, health=lambda c: False)
        self.assertNotIn("fail", self.levels(res))
        self.assertTrue(any("not reachable" in t for _, t, _h in res))

    def test_loose_token_fails(self):
        tok = self.data / "token"
        tok.write_text("x")
        os.chmod(tok, 0o644)
        self.assertIn("fail", self.levels(self.go()))

    def test_ok_exit(self):
        lines = []
        os.chmod(self.data, 0o700)
        self.assertTrue(doctor.run(out=lines.append, which=lambda n: "/bin/" + n,
                                   runner=mock.Mock(return_value=mock.Mock(stdout="1\n", stderr="")),
                                   health=lambda c: True))
        self.assertTrue(any(l.startswith("✓") for l in lines))


if __name__ == "__main__":
    unittest.main()
