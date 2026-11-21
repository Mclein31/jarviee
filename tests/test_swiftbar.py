"""The SwiftBar menu-bar plugin (extras/swiftbar/jarviee.5s.py): canned glance payloads in, menu lines out."""
import importlib.util
import io
import os
import socket
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent / "extras" / "swiftbar" / "jarviee.5s.py"


def load():
    spec = importlib.util.spec_from_file_location("jarviee_swiftbar", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GLANCE = {"counts": {"waiting": 2, "running": 1, "failed_recent": 0, "done_today": 3, "queued": 0},
          "level": "needs_you", "color": "#f5a524", "label": "2 need you · 1 running",
          "oldest_waiting": {"id": "run-1", "title": "Fix login | copy", "project": "Alpha", "since": 185,
                             "activity": "needs you: permission"},
          "quota": {"claude_5h_used": 10, "claude_week_used": 91.4, "agy": []}, "paused": False, "updated": 1}


class Plugin(unittest.TestCase):
    def setUp(self):
        self.p = load()

    def test_is_executable(self):
        self.assertTrue(os.access(PLUGIN, os.X_OK))

    def test_title_shows_waiting_count_and_worst_state(self):
        lines = self.p.render(GLANCE, "/x/jarviee")
        self.assertEqual(lines[0], "● 2 · needs you | color=#f5a524")
        self.assertEqual(lines[1], "---")

    def test_menu_items_call_the_cli(self):
        text = "\n".join(self.p.render(GLANCE, "/x/jarviee"))
        self.assertIn("Open dashboard | bash=\"/x/jarviee\" param1=open terminal=false", text)
        self.assertIn("Focus next waiting agent | bash=\"/x/jarviee\" param1=next terminal=false refresh=true", text)
        self.assertIn("Pause queue | bash=\"/x/jarviee\" param1=pause terminal=false refresh=true", text)
        self.assertNotIn("Resume queue", text)
        self.assertIn("Claude week 91% used", text)
        self.assertIn("Oldest waiting: Fix login / copy (Alpha), 3 min", text)   # a pipe would break SwiftBar's params

    def test_a_path_with_spaces_or_quotes_is_quoted_for_swiftbar(self):
        lines = self.p.render(GLANCE, "/Users/alex/My Repo/bin/jarviee")
        self.assertIn('Open dashboard | bash="/Users/alex/My Repo/bin/jarviee" param1=open terminal=false', lines)
        odd = self.p.render(GLANCE, '/a "b"/jarviee')
        self.assertIn('bash="/a \\"b\\"/jarviee"', "\n".join(odd))

    def test_paused_offers_resume_and_nothing_waiting_hides_focus(self):
        g = dict(GLANCE, paused=True, level="idle", color="#6b7280", label="idle",
                 counts={"waiting": 0}, oldest_waiting=None)
        lines = self.p.render(g, "/x/jarviee")
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith("⏸ 0 · idle"))
        self.assertIn("Resume queue | bash=\"/x/jarviee\" param1=resume", text)
        self.assertNotIn("Focus next", text)

    def test_hub_down_prints_a_quiet_line(self):
        self.assertEqual(self.p.render(None, "/x/jarviee"), ["Jarviee off | color=" + self.p.GRAY])
        with socket.socket() as s:   # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertIsNone(self.p.fetch(port, timeout=0.5))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JARVIEE_PORT": str(port)}), redirect_stdout(out):
            self.p.main()
        self.assertEqual(out.getvalue().strip(), "Jarviee off | color=" + self.p.GRAY)

    def test_garbage_payloads_never_raise(self):
        for bad in ({}, {"counts": "x", "level": 5, "color": 3, "oldest_waiting": "y", "quota": []},
                    {"counts": {"waiting": "many"}, "oldest_waiting": {"since": "soon"}}, [], "text"):
            lines = self.p.render(bad, "/x/jarviee")
            self.assertTrue(lines and isinstance(lines[0], str))
        with mock.patch.object(self.p, "render", side_effect=RuntimeError("boom")):
            out = io.StringIO()
            with redirect_stdout(out):
                self.p.main()
            self.assertTrue(out.getvalue().startswith("Jarviee off"))


if __name__ == "__main__":
    unittest.main()
