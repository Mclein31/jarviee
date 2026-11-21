"""First-run polish: unknown slash commands, setup with no CLI, data dir mode, plan errors, route notes, banner."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
from helpers import TempDir, make_cfg
from hub import brand, config, dispatch, planner, setup_wizard, slash
from hub.store import Store
from test_setup_wizard import det


class UnknownSlash(unittest.TestCase):
    def test_unknown_word_is_answered_with_400_and_suggestion(self):
        cmd, rest = slash.parse("/hepl")
        self.assertTrue(cmd and cmd.answers)
        code, out = slash.answer(cmd, rest, {}, None)
        self.assertEqual(code, 400)
        self.assertEqual(out["error"], "Unknown command /hepl. Did you mean /help? Type /help for the list.")

    def test_no_suggestion_when_nothing_is_close(self):
        code, out = slash.answer(*slash.parse("/zzzzqq now"), {}, None)
        self.assertEqual(code, 400)
        self.assertEqual(out["error"], "Unknown command /zzzzqq. Type /help for the list.")

    def test_paths_still_run_as_tasks(self):
        for t in ("/src/app.py fix it", "/Users/x/proj/main.py", "/etc/hosts", "/ lonely", "fix /bogus"):
            with self.subTest(t=t):
                self.assertEqual(slash.parse(t), (None, t))

    def test_typo_aliases_work_but_are_hidden(self):
        self.assertEqual(slash.parse("/startwokr")[0].name, "startwork")
        self.assertEqual(slash.parse("/endowrk")[0].name, "endwork")
        for c in slash.registry():
            self.assertNotIn("startwokr", c["aliases"])
            self.assertNotIn("endowrk", c["aliases"])

    def test_through_the_endpoint(self):
        from test_slash import ServerBase

        class T(ServerBase):
            def runTest(s):
                code, out = s.run_cmd("/hepl")
                s.assertEqual(code, 400)
                s.assertIn("Did you mean /help?", out["error"])
                s.assertEqual(s.agents(), [])
        t = T()
        t.setUp()
        try:
            t.runTest()
        finally:
            t.tearDown()


class SetupNoCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ov = os.path.join(self.tmp, "ov", "config.json")
        self.ws = os.path.join(self.tmp, "ws")
        os.mkdir(self.ws)
        p = mock.patch.dict(os.environ, {"JARVIEE_USER_CONFIG": self.ov, "JARVIEE_PORT": "7790"})
        p.start()
        self.addCleanup(p.stop)
        self.out = []

    def test_no_zero_lanes_and_clear_message(self):
        with mock.patch("hub.install.run"):
            ok = setup_wizard.run(det=det(claude=False, agy=False), workspace=self.ws, yes=True, out=self.out.append)
        self.assertTrue(ok)
        data = json.loads(Path(self.ov).read_text())
        self.assertNotIn("lanes", data)
        self.assertIn("No agent CLI found: install Claude Code or Antigravity, then run setup again.", self.out)

    def test_next_steps_use_the_configured_port(self):
        with mock.patch("hub.install.run"):
            setup_wizard.run(det=det(), workspace=self.ws, yes=True, out=self.out.append)
        self.assertTrue(any("http://127.0.0.1:7790" in l for l in self.out))
        self.assertFalse(any("7777" in l for l in self.out))


class DataDirMode(unittest.TestCase):
    def test_new_home_is_0700_and_existing_is_untouched(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t) / ".jarviee"
            with mock.patch.object(brand, "HOME", home):
                config._mkdir_private(home / "logs")
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            other = Path(t) / "mine" / ".jarviee"
            other.mkdir(parents=True)
            other.chmod(0o755)
            with mock.patch.object(brand, "HOME", other):
                config._mkdir_private(other / "logs")
            self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o755)


class PlanError(unittest.TestCase):
    def test_plan_dry_run_prints_one_line_error(self):
        from hub import cli_queue
        a = mock.Mock(goal="x", project="", planner="claude", dry_run=True, json=False)
        err = io.StringIO()
        with mock.patch.object(planner, "make_plan", side_effect=planner.PlanError("claude not found on PATH")), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            cli_queue.cmd_plan(a, {})
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("error: claude not found on PATH", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class RouteNotes(unittest.TestCase):
    def route(self, which):
        cfg = json.loads((helpers.ROOT / "config.json").read_text(encoding="utf-8"))   # real agy / claude commands
        with mock.patch("shutil.which", side_effect=which):
            return dispatch.route("rename a variable", cfg, check_available=True, quota={}, usage={})

    def test_nothing_installed(self):
        r = self.route(lambda n: None)
        self.assertIn("no agent CLI found: run python3 hub.py doctor", r["fallback"])
        self.assertNotIn("waiting for quota", r["fallback"])

    def test_only_one_installed(self):
        r = self.route(lambda n: "/bin/claude" if n == "claude" else None)
        self.assertIn("is not installed; routed to", r["fallback"])
        self.assertNotIn("waiting for quota", r["fallback"])


class Banner(unittest.TestCase):
    def test_global_brain_line_only_when_it_exists(self):
        from hub import server
        for ok, expect in ((False, False), (True, True)):
            out = io.StringIO()
            with mock.patch.object(server, "make_server") as ms, \
                    mock.patch("hub.obsidian.global_ok", return_value=ok), \
                    mock.patch("hub.obsidian.global_brain", return_value="/x/_BRAIN"), \
                    mock.patch("hub.obsidian.workspace", return_value="/x"), \
                    mock.patch("hub.obsidian.brain_ok", return_value=True), \
                    contextlib.redirect_stdout(out):
                ms.return_value.serve_forever.side_effect = KeyboardInterrupt
                server.serve({"port": 1}, mock.Mock())
            self.assertEqual("Global brain:" in out.getvalue(), expect)
            self.assertNotIn("(not found)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
