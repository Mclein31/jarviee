"""Project maps: FLOW.json validation, load states, saved positions, the mapping brief and the HTTP routes."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from helpers import TempDir, make_cfg
from hub import auth, projmap
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

FLOW = {
    "title": "Alpha", "summary": "A web app.",
    "nodes": [
        {"id": "user", "label": "User", "kind": "actor", "layer": "frontend"},
        {"id": "ui.home", "label": "Home", "kind": "screen", "layer": "frontend", "file": "web/home.tsx"},
        {"id": "api.items", "label": "GET /items", "kind": "api", "layer": "backend"},
        {"id": "db", "label": "Postgres", "kind": "store", "layer": "backend"},
        {"id": "ui.home", "label": "duplicate", "kind": "screen", "layer": "frontend"},
        {"id": "bad id with spaces", "label": "x"},
        {"id": "pay", "label": "Stripe", "kind": "weird", "layer": "nowhere"},
    ],
    "edges": [
        {"from": "user", "to": "ui.home", "label": "opens"},
        {"from": "ui.home", "to": "api.items", "label": "fetch"},
        {"from": "api.items", "to": "db", "label": "reads"},
        {"from": "api.items", "to": "db", "label": "reads"},
        {"from": "api.items", "to": "ghost"},
        {"from": "db", "to": "db"},
    ],
    "flows": [{"name": "Browse", "steps": ["user", "ui.home", "api.items", "missing", "db"]}, {"name": "Too short", "steps": ["user"]}],
}


class ValidateTests(unittest.TestCase):
    def test_keeps_good_parts_and_says_what_it_dropped(self):
        flow, warn = projmap.validate(FLOW)
        self.assertEqual([n["id"] for n in flow["nodes"]], ["user", "ui.home", "api.items", "db", "pay"])
        pay = flow["nodes"][-1]
        self.assertEqual((pay["kind"], pay["layer"]), ("component", "backend"))   # unknown values fall back
        self.assertEqual(len(flow["edges"]), 3)                                    # duplicate, unknown and self edges dropped
        self.assertEqual(flow["flows"], [{"name": "Browse", "steps": ["user", "ui.home", "api.items", "db"]}])
        self.assertTrue(any("repeated id" in w for w in warn))
        self.assertTrue(any("unknown node" in w for w in warn))

    def test_nothing_usable_is_an_error(self):
        for raw in ([], {"nodes": []}, {"nodes": [{"id": "has space"}]}):
            with self.assertRaises(ValueError):
                projmap.validate(raw)

    def test_text_is_trimmed(self):
        flow, _ = projmap.validate({"nodes": [{"id": "a", "label": "x" * 500, "detail": "line\n\nbreaks   here"}]})
        self.assertEqual(len(flow["nodes"][0]["label"]), 80)
        self.assertEqual(flow["nodes"][0]["detail"], "line breaks here")

    def test_brief_has_no_square_brackets_and_names_the_file(self):
        b = projmap.brief("Alpha")
        self.assertNotIn("[", b)
        self.assertIn(".brain/FLOW.json", b)
        self.assertIn("frontend", b)


class LoadAndLayoutTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.flow_file = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha" / ".brain" / "FLOW.json"

    def tearDown(self):
        self._t.__exit__()

    def test_missing_invalid_and_ok(self):
        code, body = projmap.load(self.cfg, "Alpha")
        self.assertEqual((code, body.get("missing")), (200, True))
        self.flow_file.write_text("{not json", encoding="utf-8")
        code, body = projmap.load(self.cfg, "alpha")                  # project names match case-insensitively
        self.assertFalse(body["ok"])
        self.assertIn("error", body)
        self.flow_file.write_text(json.dumps(FLOW), encoding="utf-8")
        code, body = projmap.load(self.cfg, "Alpha")
        self.assertTrue(body["ok"])
        self.assertEqual(body["project"], "Alpha")
        self.assertEqual(len(body["flow"]["nodes"]), 5)
        self.assertEqual(projmap.load(self.cfg, "../Alpha")[0], 404)
        self.assertEqual(projmap.load(self.cfg, "NoBrain")[0], 404)

    def test_layout_is_sanitised_saved_per_view_and_reset(self):
        code, body = projmap.save_layout(self.cfg, {"project": "Alpha", "view": "full", "positions": {
            "user": [10, 20.26], "bad id": [1, 2], "huge": [1e9, 0], "short": [1], "text": ["a", "b"]}})
        self.assertEqual((code, body["saved"]), (200, 1))
        projmap.save_layout(self.cfg, {"project": "Alpha", "view": "backend", "positions": {"db": [5, 5]}})
        self.flow_file.write_text(json.dumps(FLOW), encoding="utf-8")
        layout = projmap.load(self.cfg, "Alpha")[1]["layout"]
        self.assertEqual(layout, {"full": {"user": [10.0, 20.3]}, "backend": {"db": [5.0, 5.0]}})
        projmap.save_layout(self.cfg, {"project": "Alpha", "view": "full", "positions": None})
        self.assertEqual(projmap.load(self.cfg, "Alpha")[1]["layout"], {"backend": {"db": [5.0, 5.0]}})
        self.assertEqual(projmap.save_layout(self.cfg, {"project": "Alpha", "view": "sideways", "positions": {}})[0], 400)
        self.assertEqual(projmap.save_layout(self.cfg, {"project": "Nope", "view": "full", "positions": {}})[0], 400)
        self.assertTrue(Path(self.cfg["flow_layout_path"]).is_file())   # never the real ~/.jarviee


class HttpTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.token = auth.get_token(self.cfg)
        (Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha" / ".brain" / "FLOW.json").write_text(json.dumps(FLOW), encoding="utf-8")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def _req(self, method, path, body=None, token=True):
        h = {"Content-Type": "application/json", **({"X-Jarviee-Token": self.token} if token else {})}
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h, method=method)
        try:
            with _OPEN(r, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_routes(self):
        code, body = self._req("GET", "/api/flow?project=Alpha")
        self.assertEqual(code, 200)
        self.assertEqual(body["flow"]["title"], "Alpha")
        code, state = self._req("GET", "/api/state")
        alpha = next(p for p in state["projects"] if p["name"] == "Alpha")
        beta = next(p for p in state["projects"] if p["name"] == "Beta Site")
        self.assertIsInstance(alpha["flow"], float)
        self.assertIsNone(beta["flow"])
        self.assertEqual(self._req("POST", "/api/flow/layout", {"project": "Alpha", "view": "full", "positions": {"user": [1, 2]}}, token=False)[0], 403)
        self.assertEqual(self._req("POST", "/api/flow/layout", {"project": "Alpha", "view": "full", "positions": {"user": [1, 2]}})[1], {"ok": True, "saved": 1})
        self.assertEqual(self._req("POST", "/api/flow/generate", {"project": "../etc"})[0], 400)
        with patch("hub.server.launcher.launch", return_value=("run-x", {"tool": "claude"})) as launch:
            code, body = self._req("POST", "/api/flow/generate", {"project": "beta site"})
        self.assertEqual((code, body["id"]), (200, "run-x"))
        task, _cfg, _store, tool, tier, project, cwd = launch.call_args[0]
        self.assertIn(".brain/FLOW.json", task)
        self.assertEqual((tier, project), ("complex", "Beta Site"))


if __name__ == "__main__":
    unittest.main()
