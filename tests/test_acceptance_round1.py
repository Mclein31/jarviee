"""Round-1 acceptance checks: template API round trip + command-bar insertion, tab-colour sequences per state."""
import json
import urllib.error
import re
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

from helpers import TempDir, make_cfg
from hub.server import make_server
from hub.store import Store

ROOT = Path(__file__).resolve().parent.parent
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class TemplatesOverHttp(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.cfg, _ = make_cfg(self._t.__enter__())
        self.cfg["templates_path"] = str(Path(self.cfg["db_path"]).parent / "tpl.json")
        self.srv = make_server(self.cfg, Store(self.cfg["db_path"]))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"

    def tearDown(self):
        if self.srv.scheduler:
            self.srv.scheduler.stop()
        self.srv.shutdown(); self.srv.server_close(); self._t.__exit__()

    def call(self, path, body=None):
        from hub import auth
        h = {"Content-Type": "application/json", "X-Jarviee-Token": auth.get_token(self.cfg)}
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_save_list_delete(self):
        self.assertEqual(self.call("/api/templates")[1]["templates"], [])
        s, r = self.call("/api/templates", {"name": "Fix", "body": "Fix {project}"})
        self.assertEqual(s, 200)
        self.assertEqual(self.call("/api/templates")[1]["templates"][0]["body"], "Fix {project}")
        self.assertEqual(self.call("/api/templates", {"name": "", "body": "x"})[0], 400)
        self.assertEqual(self.call("/api/templates/delete", {"name": "Fix"})[1]["templates"], [])
        self.assertEqual(self.call("/api/templates/delete", {"name": "Fix"})[0], 404)


class TemplateInsertWiring(unittest.TestCase):
    js = (ROOT / "hub" / "static" / "composer.js").read_text()

    def test_picker_inserts_into_command_bar_without_launching(self):
        a = self.js.index('q("#tpl").addEventListener("change"')
        b = self.js.index('q("#tpl-save")')
        block = self.js[a:b]
        self.assertIn("el.value =", block)
        self.assertIn("t.body", block)
        for bad in ("launch(", "requestSubmit"):
            self.assertNotIn(bad, block)


class TabColourAndExit(unittest.TestCase):
    def test_each_state_colour_is_stable_and_runner_wires_all_states(self):
        sys.path.insert(0, str(ROOT / "hooks"))
        import io
        import pane_runner
        outs = {}
        for st in ("running", "waiting", "done", "failed"):
            buf, old = io.StringIO(), sys.stdout
            sys.stdout = buf
            try:
                pane_runner.set_tab_color(st)
            finally:
                sys.stdout = old
            outs[st] = buf.getvalue()
            self.assertEqual(len(re.findall(r"\033\]6;1;bg;(?:red|green|blue);brightness;\d+\007", outs[st])), 3, st)
        self.assertEqual(len(set(outs.values())), 4)
        # every state reaches the tab colour through the pane header (set_header → set_tab_color)
        for st in ("running", "waiting", "done", "failed"):
            kind = pane_runner.STATUS_TO_HEADER[st]
            self.assertIn(kind, pane_runner.TAB_COLORS, st)
            pane_runner.header.update(shown=None)
            buf, old = io.StringIO(), sys.stdout
            sys.stdout = buf
            try:
                pane_runner.set_header(kind, "T")
            finally:
                sys.stdout = old
            self.assertIn(outs[st], buf.getvalue(), st)


if __name__ == "__main__":
    unittest.main()
