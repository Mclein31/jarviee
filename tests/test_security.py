import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import TempDir, free_port, make_cfg
from hub import auth
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, self.cfg_path = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.token = auth.get_token(self.cfg)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def _req(self, method="GET", path="/", body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with _OPEN(r, timeout=3) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw.decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_health_endpoint(self):
        code, body = self._req("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "ok")
        self.assertEqual(body.get("port"), self.cfg["port"])

    def test_refuse_bind_to_non_localhost(self):
        bad_cfg = dict(self.cfg, host="0.0.0.0")
        with self.assertRaises(AssertionError) as ctx:
            make_server(bad_cfg, self.store, scheduler=False)
        self.assertIn("Refusing to bind to 0.0.0.0", str(ctx.exception))

    def test_wrong_host_rejected_with_403(self):
        code, body = self._req("GET", "/", headers={"Host": "attacker.com"})
        self.assertEqual(code, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_foreign_origin_post_rejected_with_403(self):
        code, body = self._req(
            "POST", "/api/classify",
            body={"task": "fix typo"},
            headers={"Origin": "http://evil.com", "X-Jarviee-Token": self.token}
        )
        self.assertEqual(code, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_missing_token_post_rejected_with_403(self):
        code, body = self._req(
            "POST", "/api/classify",
            body={"task": "fix typo"},
            headers={}
        )
        self.assertEqual(code, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_wrong_token_post_rejected_with_403(self):
        code, body = self._req(
            "POST", "/api/classify",
            body={"task": "fix typo"},
            headers={"X-Jarviee-Token": "invalid-token-123"}
        )
        self.assertEqual(code, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_valid_dashboard_request_success(self):
        # 1. GET / gets HTML with token injected
        code, html = self._req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(f'window.JARVIEE_TOKEN = "{self.token}"', html)
        self.assertIn(f'<meta name="jarviee-token" content="{self.token}">', html)

        # 2. POST with same-origin and valid token works
        origin = f"http://127.0.0.1:{self.cfg['port']}"
        code, res = self._req(
            "POST", "/api/classify",
            body={"task": "fix typo in README"},
            headers={"Origin": origin, "X-Jarviee-Token": self.token}
        )
        self.assertEqual(code, 200)
        self.assertEqual(res.get("tier"), "basic")

    def test_hook_post_works_with_token(self):
        code, res = self._req(
            "POST", "/hook?event=SessionStart",
            body={"hook_event_name": "SessionStart", "modelName": "gemini-3.8-flash"},
            headers={"X-Jarviee-Token": self.token}
        )
        self.assertEqual(code, 200)
        self.assertTrue(res.get("id"))

    def test_wrong_json_types_return_400_instead_of_dropping_connection(self):
        tok = {"X-Jarviee-Token": self.token}
        cases = [
            ("/api/classify", {"task": None}),
            ("/api/classify", {"task": 123}),
            ("/api/classify", {"task": ["a"]}),
            ("/api/classify", {"task": "fix typo", "project": ["Alpha"]}),
            ("/api/classify", {"task": "fix typo", "tool": ["agy"]}),
            ("/api/run", {"task": ["a"]}),
            ("/api/run", {"task": None}),
            ("/api/run", {"task": 123}),
            ("/api/run", {"task": "fix typo", "project": ["Alpha"]}),
            ("/api/run", {"task": "fix typo", "cwd": ["x"]}),
            ("/api/stop", {"id": ["x"]}),
            ("/api/stop", {"id": 123}),
            ("/hook?event=PostToolUse", {"toolCall": "x"}),
            ("/hook?event=SessionStart", {"workspacePaths": "x"}),
            ("/hook?event=SessionStart", {"jarvieeRunId": ["x"]}),
            ("/api/plan", {"goal": None, "planner": "mock"}),
            ("/api/plan", {"goal": ["x"], "planner": "mock"}),
            ("/api/classify", ["not", "an", "object"]),
        ]
        for path, payload in cases:
            code, body = self._req("POST", path, body=payload, headers=tok)
            self.assertEqual(code, 400, f"{path} with {payload!r} returned {code}: {body}")
            self.assertIn("error", body)

        # Invalid raw JSON body returns 400 too
        r = urllib.request.Request(
            self.base + "/api/classify",
            data=b"{not-json",
            headers={"Content-Type": "application/json", **tok},
            method="POST",
        )
        try:
            with _OPEN(r, timeout=3) as resp:
                self.fail(f"expected 400, got {resp.status}")
        except urllib.error.HTTPError as e:
            with e:
                self.assertEqual(e.code, 400)
