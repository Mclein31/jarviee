import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401
from hub import config, setup_wizard


def det(**kw):
    d = {"os": "Darwin", "mac": True, "python": "3.12", "python_ok": True, "claude": True, "agy": True,
         "codex": False, "gemini": False, "iterm2": True, "terminal_notifier": False}
    d.update(kw)
    return d


class Wizard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ov = os.path.join(self.tmp, "ov", "config.json")
        self.ws = os.path.join(self.tmp, "ws")
        os.mkdir(self.ws)
        p = mock.patch.dict(os.environ, {"JARVIEE_USER_CONFIG": self.ov})
        p.start()
        self.addCleanup(p.stop)
        self.out = []

    def run_wiz(self, d, **kw):
        with mock.patch("hub.install.run") as inst:
            ok = setup_wizard.run(det=d, workspace=self.ws, out=self.out.append, **kw)
        return ok, inst

    def test_detect_uses_which(self):
        d = setup_wizard.detect(which=lambda n: "/x/" + n if n in ("claude", "codex") else None,
                                system="Linux", exists=lambda p: True)
        self.assertTrue(d["claude"] and d["codex"] and not d["agy"])
        self.assertFalse(d["iterm2"])

    def test_both_tools_keeps_defaults(self):
        o = setup_wizard.build_overlay(det(), ["claude", "agy"], "/w")
        self.assertEqual(o, {"obsidian": {"workspace_root": "/w"}})

    def test_claude_only(self):
        o = setup_wizard.build_overlay(det(), ["claude"], "/w")
        self.assertEqual([o["dispatch"][l]["model"] for l in ("basic", "light", "complex", "heavy")],
                         ["haiku", "sonnet", "sonnet", "opus"])
        self.assertEqual(o["lanes"]["agy"], 0)

    def test_agy_only_and_no_iterm(self):
        o = setup_wizard.build_overlay(det(iterm2=False), ["agy"], "/w")
        self.assertTrue(all(v["tool"] == "agy" for v in o["dispatch"].values()))
        self.assertEqual(o["lanes"]["claude"], 0)
        self.assertEqual(o["terminal"]["mode"], "background")

    def test_yes_writes_overlay_only_and_installs_hooks(self):
        ok, inst = self.run_wiz(det(claude=False), yes=True)
        self.assertTrue(ok)
        data = json.loads(Path(self.ov).read_text())
        self.assertEqual(data["obsidian"]["workspace_root"], self.ws)
        self.assertEqual(oct(os.stat(self.ov).st_mode & 0o777), "0o600")
        inst.assert_called_once_with("agy", dry_run=False)
        self.assertTrue(any("service install" in l for l in self.out))

    def test_dry_run_writes_nothing(self):
        ok, inst = self.run_wiz(det(), yes=True, dry_run=True)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.ov))
        self.assertTrue(all(c.kwargs["dry_run"] for c in inst.call_args_list))

    def test_interactive_answers(self):
        answers = iter([self.ws, "claude", "n", "n", "n"])
        ok, inst = self.run_wiz(det(), inp=lambda p: next(answers))
        self.assertTrue(ok)
        data = json.loads(Path(self.ov).read_text())
        self.assertEqual(data["dispatch"]["heavy"]["model"], "opus")
        inst.assert_not_called()

    def test_old_python_stops(self):
        ok, _ = self.run_wiz(det(python_ok=False), yes=True)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.ov))


if __name__ == "__main__":
    unittest.main()
