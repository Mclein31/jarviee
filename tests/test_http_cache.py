"""Conditional GET, gzip, static ETags, snapshot-cache invalidation, route table and the store (upsert, migration)."""
import gzip
import http.client
import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import auth, respond, routes, runs_api
from hub.server import make_server
from hub.store import Store


class HttpTestBase(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.cfg["port"]
        self.token = auth.get_token(self.cfg)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.store.close()
        self._t.__exit__()

    def req(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if method == "POST":
            h.setdefault("Content-Type", "application/json")
            h.setdefault("X-Jarviee-Token", self.token)
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        r = conn.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        return r.status, hdrs, data


class ConditionalGetTests(HttpTestBase):
    def test_state_etag_and_304(self):
        code, h, body = self.req("GET", "/api/state")
        self.assertEqual(code, 200)
        etag = h["etag"]
        self.assertTrue(etag.startswith('"') and not etag.startswith("W/"))
        self.assertEqual(h["cache-control"], "no-cache")
        self.assertIn("now", json.loads(body))
        code, h2, body2 = self.req("GET", "/api/state", {"If-None-Match": etag})
        self.assertEqual((code, body2), (304, b""))
        self.assertEqual(h2["etag"], etag)
        code, _, _ = self.req("GET", "/api/state", {"If-None-Match": '"nope"'})
        self.assertEqual(code, 200)

    def test_etag_ignores_now_but_not_content(self):
        etag = self.req("GET", "/api/state")[1]["etag"]
        time.sleep(1.0)   # past the snapshot TTL: a fresh build with a new `now`
        self.assertEqual(self.req("GET", "/api/state")[1]["etag"], etag)
        self.store.upsert("run-aaaa1111", source="launcher", tool="echo", status="running", task="x")
        self.assertNotEqual(self.req("GET", "/api/state")[1]["etag"], etag)   # a store write shows at once

    def test_other_polled_endpoints(self):
        from unittest.mock import patch
        with patch("hub.usage.get_usage", return_value={"claude": {}, "age": 3, "generated": 1.0}):   # not the real transcripts
            self._polled()

    def _polled(self):
        for path in ("/api/usage", "/api/activity", "/api/runs"):
            code, h, _ = self.req("GET", path)
            self.assertEqual(code, 200, path)
            self.assertEqual(self.req("GET", path, {"If-None-Match": h["etag"]})[0], 304, path)

    def test_gzip_only_when_asked_and_large(self):
        code, h, raw = self.req("GET", "/api/state", {"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", h)
        code, h, zipped = self.req("GET", "/api/state", {"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(h["content-encoding"], "gzip")
        self.assertIn("accept-encoding", h["vary"].lower())
        self.assertEqual(json.loads(gzip.decompress(zipped))["default_tool"], json.loads(raw)["default_tool"])
        self.assertLess(len(zipped), len(raw))
        code, h, _ = self.req("GET", "/api/health", {"Accept-Encoding": "gzip"})   # tiny: not worth compressing
        self.assertNotIn("content-encoding", h)

    def test_dashboard_page_gzip(self):
        code, h, body = self.req("GET", "/", {"Accept-Encoding": "gzip"})
        self.assertEqual(h["content-encoding"], "gzip")
        self.assertIn(b"<head>", gzip.decompress(body))
        self.assertEqual(h["cache-control"], "no-store")   # it embeds the API token

    def test_post_invalidates_the_snapshot(self):
        voice = lambda: json.loads(self.req("GET", "/api/state")[2])["voice"]["enabled"]
        before = voice()
        self.req("POST", "/api/settings/voice", body={"enabled": not before})
        self.assertEqual(voice(), not before)   # no store write, no wait: only the POST invalidation can explain it
        self.req("POST", "/api/settings/voice", body={"enabled": before})
        self.assertEqual(voice(), before)

    def test_snapshot_is_shared_between_requests(self):
        calls = []
        orig = routes.build_state
        routes.build_state = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
        try:
            for _ in range(3):
                self.req("GET", "/api/state")
        finally:
            routes.build_state = orig
        self.assertEqual(len(calls), 1)


class StaticTests(HttpTestBase):
    def test_static_etag_304_and_edit(self):
        name = next(p.name for p in sorted(runs_api.STATIC_DIR.glob("*.js")))
        code, h, body = self.req("GET", f"/static/{name}")
        self.assertEqual(code, 200)
        self.assertIn("javascript", h["content-type"])
        self.assertEqual(self.req("GET", f"/static/{name}", {"If-None-Match": h["etag"]})[0], 304)
        code, h2, z = self.req("GET", f"/static/{name}", {"Accept-Encoding": "gzip"})
        if len(body) > 1024:
            self.assertEqual(gzip.decompress(z), body)

    def test_static_picks_up_edits(self):
        f = runs_api.STATIC_DIR / "zz-edit-test.css"
        try:
            f.write_text("a{}", encoding="utf-8")
            etag1 = self.req("GET", "/static/zz-edit-test.css")[1]["etag"]
            time.sleep(0.01)
            f.write_text("a{color:red}", encoding="utf-8")
            code, h, body = self.req("GET", "/static/zz-edit-test.css", {"If-None-Match": etag1})
            self.assertEqual((code, body), (200, b"a{color:red}"))
            self.assertNotEqual(h["etag"], etag1)
        finally:
            f.unlink()

    def test_static_traversal_still_refused(self):
        for bad in ("/static/../server.py", "/static/..%2Fserver.py", "/static/%2e%2e/store.py", "/static/x.py"):
            self.assertEqual(self.req("GET", bad)[0], 404, bad)


class RouteTableTests(HttpTestBase):
    def test_every_post_needs_the_token(self):
        for path, r in routes.POST_ROUTES.items():
            self.assertTrue(r.token, path)
        code, _, _ = self.req("POST", "/api/clear", {"X-Jarviee-Token": "wrong"})
        self.assertEqual(code, 403)
        code, _, _ = self.req("POST", "/api/nope-such-route")
        self.assertEqual(code, 404)
        code, _, _ = self.req("POST", "/api/nope-such-route", {"X-Jarviee-Token": "wrong"})
        self.assertEqual(code, 403)   # the guard still answers before the route lookup

    def test_unknown_get_is_404(self):
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)


class RespondTests(unittest.TestCase):
    def test_render_json_etag_excludes_volatile(self):
        a = respond.render_json({"x": 1, "now": 1.0, "age": 3})
        b = respond.render_json({"x": 1, "now": 2.0, "age": 9})
        c = respond.render_json({"x": 2, "now": 1.0})
        self.assertEqual(a.etag, b.etag)
        self.assertNotEqual(a.etag, c.etag)
        self.assertEqual(json.loads(a.body), {"x": 1, "now": 1.0, "age": 3})

    def test_etag_matching(self):
        self.assertTrue(respond.etag_matches('"a", W/"b"', '"b"'))
        self.assertTrue(respond.etag_matches("*", '"b"'))
        self.assertFalse(respond.etag_matches('"a"', '"b"'))
        self.assertFalse(respond.etag_matches("", '"b"'))

    def test_accepts_gzip(self):
        self.assertTrue(respond.accepts_gzip("gzip, deflate, br"))
        self.assertFalse(respond.accepts_gzip("gzip;q=0"))
        self.assertFalse(respond.accepts_gzip("identity"))
        self.assertFalse(respond.accepts_gzip(None))


class StoreTests(unittest.TestCase):
    def test_upsert_inserts_then_updates_only_given_fields(self):
        with TempDir() as tmp:
            s = Store(str(Path(tmp) / "a.db"))
            s.upsert("a", tool="echo", task="t1", status="done", cleared=1)
            row = s.get("a")
            started = row["started"]
            self.assertTrue(started)
            s.upsert("a", activity="again")
            row = s.get("a")
            self.assertEqual((row["task"], row["activity"], row["started"]), ("t1", "again", started))
            s.upsert("a", status="running")
            self.assertEqual(s.get("a")["cleared"], 0)   # a cleared row that comes back to life is listed again
            s.upsert("b", reasons=["x"], files=["f.py"])
            self.assertEqual((s.get("b")["reasons"], s.get("b")["files"]), (["x"], ["f.py"]))
            with self.assertRaises(ValueError):
                s.upsert("c", **{"status=1; DROP TABLE agents; --": 1})
            s.close()

    def test_history_matches_coalesce_semantics(self):
        with TempDir() as tmp:
            s = Store(str(Path(tmp) / "a.db"))
            s.db.execute("INSERT INTO agents (id, started, updated, ended) VALUES ('e', 1, 2, 100)")
            s.db.execute("INSERT INTO agents (id, started, updated, ended) VALUES ('u', 1, 90, NULL)")
            s.db.execute("INSERT INTO agents (id, started, updated, ended) VALUES ('s', 95, NULL, NULL)")
            s.db.execute("INSERT INTO agents (id, started, updated, ended) VALUES ('old', 1, 2, 3)")
            s.db.commit()
            self.assertEqual(sorted(r["id"] for r in s.history(50)), ["e", "s", "u"])
            s.close()

    def test_version_changes_with_writes_and_close_is_safe(self):
        with TempDir() as tmp:
            s = Store(str(Path(tmp) / "a.db"))
            v = s.version()
            s.upsert("a", task="x")
            self.assertNotEqual(s.version(), v)
            s.close()
            s.close()

    def test_old_database_migrates_in_place(self):
        with TempDir() as tmp:
            path = str(Path(tmp) / "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, source TEXT, tool TEXT, task TEXT, tier TEXT, "
                       "suggested_tier TEXT, status TEXT, started REAL, updated REAL, ended REAL)")
            db.execute("INSERT INTO agents VALUES ('run-1', 'launcher', 'agy', 'old task', 'standard', 'standard', "
                       "'done', 10, 20, 30)")
            db.commit()
            db.close()
            s = Store(path)
            row = s.get("run-1")
            self.assertEqual((row["task"], row["tier"], row["started"], row["ended"]), ("old task", "light", 10, 30))
            self.assertIn("pane_id", row)
            names = {r[1] for r in s.db.execute("PRAGMA index_list(agents)")}
            self.assertTrue({"idx_agents_list", "idx_agents_ended", "idx_agents_updated", "idx_agents_started"} <= names)
            self.assertEqual([r["id"] for r in s.all()], ["run-1"])
            s.close()
            s2 = Store(path)   # opening it again is a no-op
            self.assertEqual(s2.get("run-1")["task"], "old task")
            s2.close()


if __name__ == "__main__":
    unittest.main()
