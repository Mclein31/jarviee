import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import auth, detector, fallback, launcher
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class CrossToolFallbackServerTests(unittest.TestCase):
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

    def _post(self, path, body):
        h = {"Content-Type": "application/json", "X-Jarviee-Token": self.token}
        data = json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST")
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode() or "{}")

    def _get(self, path):
        h = {"Content-Type": "application/json", "X-Jarviee-Token": self.token}
        r = urllib.request.Request(self.base + path, headers=h, method="GET")
        with _OPEN(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_approval_prompts_rejected_with_400(self):
        prompts = [
            "Approve running python3 hub.py install-hooks (writes ~/.gemini/config/hooks.json)",
            "Restart hub.py serve and the agent sessions so hooks start reporting the agent PID",
            "Confirm gemini-4-argon-medium as the agy standard tier",
            "Restart python3 hub.py serve to start opening runs in iTerm2 panes",
            "Decide whether to add agent-hub to _BRAIN/PROJECT_INDEX.md",
        ]
        for p in prompts:
            code, body = self._post("/api/run", {"task": p})
            self.assertEqual(code, 400, f"Expected 400 for prompt: {p}")
            self.assertIn("Approval prompts", body.get("error", ""))

    def test_preflight_routing_in_classify(self):
        # Default routing for basic is agy:gemini-3.8-flash-low
        code, body = self._post("/api/classify", {"task": "fix typo in readme"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "agy")

        # Mark agy:gemini-3.8-flash-low exhausted
        now = time.time()
        self.store.set_quota("agy", "gemini-3.8-flash-low", "exhausted", until=now + 7200, last_error="429")

        # Pre-flight classify should route straight to equivalent (claude:haiku)
        code2, body2 = self._post("/api/classify", {"task": "fix typo in readme"})
        self.assertEqual(code2, 200)
        self.assertEqual(body2["tool"], "claude")
        self.assertEqual(body2["model"], "haiku")
        self.assertIn("exhausted", body2.get("fallback", ""))

    def test_quota_probe_action(self):
        # Mark tool exhausted
        self.store.set_quota("claude", "haiku", "exhausted", until=time.time() + 3600, last_error="Limit")
        q_before = self.store.get_quota("claude", "haiku")
        self.assertEqual(q_before["status"], "exhausted")

        # Trigger probe on claude
        code, body = self._post("/api/quota/probe", {"tool": "claude", "model": "haiku"})
        self.assertEqual(code, 200)
        # In test environment, claude command is python fake executable which succeeds
        self.assertTrue(body.get("ok"))
        q_after = self.store.get_quota("claude", "haiku")
        self.assertEqual(q_after["status"], "ok")

    def test_settings_fallback_toggles(self):
        code, body = self._post("/api/settings/fallback", {"enabled": False, "never_fallback_to_opus": False})
        self.assertEqual(code, 200)
        self.assertFalse(self.cfg["fallback"]["enabled"])
        self.assertFalse(self.cfg["fallback"]["never_fallback_to_opus"])

        # Re-enable
        code2, body2 = self._post("/api/settings/fallback", {"enabled": True, "never_fallback_to_opus": True})
        self.assertEqual(code2, 200)
        self.assertTrue(self.cfg["fallback"]["enabled"])
        self.assertTrue(self.cfg["fallback"]["never_fallback_to_opus"])


if __name__ == "__main__":
    unittest.main()
