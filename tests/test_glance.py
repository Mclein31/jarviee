"""Tests for the glance summary and the /api/glance + /api/action endpoints."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import TempDir, make_cfg
from hub import auth, glance, usage
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
NOW = 1_700_000_000.0


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return [dict(r) for r in self.rows]


def row(i, status, **kw):
    return {"id": i, "status": status, "updated": NOW - 10, "workspace": "/w/Alpha", **kw}


class GlanceTests(unittest.TestCase):
    def g(self, rows, **kw):
        return glance.glance({}, FakeStore(rows), now=NOW, **kw)

    def test_idle_and_priority(self):
        self.assertEqual(self.g([])["level"], "idle")
        self.assertEqual(self.g([])["label"], "idle")
        run = row("r", "running")
        fail = row("f", "failed", ended=NOW - 60)
        wait = row("w", "waiting")
        self.assertEqual(self.g([run])["level"], "running")
        self.assertEqual(self.g([run, fail])["level"], "failed")
        g = self.g([run, fail, wait])
        self.assertEqual(g["level"], "needs_you")
        self.assertEqual(g["color"], "#f5a524")
        self.assertIn("1 need you", g["label"])

    def test_old_failures_do_not_count(self):
        g = self.g([row("f", "failed", ended=NOW - 3600)])
        self.assertEqual(g["counts"]["failed_recent"], 0)
        self.assertEqual(g["level"], "idle")

    def test_oldest_waiting(self):
        g = self.g([row("new", "waiting", updated=NOW - 5, title="B"),
                    row("old", "waiting", updated=NOW - 500, title="A", activity="needs permission")])
        o = g["oldest_waiting"]
        self.assertEqual((o["id"], o["title"], o["project"], o["since"]), ("old", "A", "Alpha", 500))
        self.assertIsNone(self.g([row("r", "running")])["oldest_waiting"])

    def test_cleared_rows_ignored(self):
        g = self.g([row("w", "waiting", cleared=1), row("r", "running", cleared=0)])
        self.assertEqual(g["counts"]["waiting"], 0)
        self.assertEqual(g["level"], "running")

    def test_quota_from_cache(self):
        data = {"claude": {"limits": {"windows": [{"id": "five_hour", "used_pct": 12.0},
                                                  {"id": "seven_day", "used_pct": 91.0}]}},
                "agy": {"quota": {"groups": [{"name": "Pro", "buckets": [
                    {"window": "weekly", "remaining_pct": 40.0}]}]}}}
        q = self.g([], usage_data=data)["quota"]
        self.assertEqual((q["claude_5h_used"], q["claude_week_used"]), (12.0, 91.0))
        self.assertEqual(q["agy"], [{"group": "Pro", "window": "weekly", "remaining_pct": 40.0}])
        q = self.g([], usage_data=None)["quota"]
        self.assertEqual((q["claude_5h_used"], q["claude_week_used"], q["agy"]), (None, None, []))

    def test_never_spawns_a_subprocess(self):
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            self.g([row("w", "waiting")], usage_data=usage.cached({}))
        run.assert_not_called()
        popen.assert_not_called()


class GlanceApiTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=True, interval=3600)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.token = auth.get_token(self.cfg)

    def tearDown(self):
        self.srv.scheduler.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def req(self, method, path, headers=None, token=False):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["X-Jarviee-Token"] = self.token
        data = b"{}" if method == "POST" else None
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with _OPEN(r, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_glance_needs_no_token(self):
        self.store.upsert("a1", source="hook", status="waiting", workspace="/w/Alpha", updated=time.time())
        code, body = self.req("GET", "/api/glance")
        self.assertEqual(code, 200)
        self.assertEqual(body["level"], "needs_you")
        self.assertEqual(body["oldest_waiting"]["id"], "a1")

    def test_wrong_host_forbidden(self):
        code, _ = self.req("GET", "/api/glance", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_pause_needs_token(self):
        code, _ = self.req("POST", "/api/action/pause")
        self.assertEqual(code, 403)
        self.assertFalse(self.srv.scheduler.paused)
        code, body = self.req("POST", "/api/action/pause", token=True)
        self.assertEqual((code, body["ok"]), (200, True))
        self.assertTrue(self.srv.scheduler.paused)
        self.assertTrue(self.req("GET", "/api/glance")[1]["paused"])
        self.req("POST", "/api/action/resume", token=True)
        self.assertFalse(self.srv.scheduler.paused)

    def test_unknown_action_404(self):
        code, body = self.req("POST", "/api/action/nope", token=True)
        self.assertEqual(code, 404)
        self.assertIn("error", body)
        self.assertEqual(self.req("POST", "/api/action/Bad_Name", token=True)[0], 404)


if __name__ == "__main__":
    unittest.main()
