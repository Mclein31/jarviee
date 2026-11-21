"""Fields the redesigned dashboard reads: per-tool availability in /api/state and the /api/activity feed."""
import json
import threading
import time
import unittest
import urllib.request

from helpers import TempDir, make_cfg
from hub import spans
from hub.server import handle_hook, make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.cfg, _ = make_cfg(self._t.__enter__())
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def get(self, path):
        with _OPEN(f"http://127.0.0.1:{self.cfg['port']}{path}", timeout=3) as r:
            return json.loads(r.read())

    def test_tool_status_reports_lanes_tiers_and_quota(self):
        ts = self.get("/api/state")["tool_status"]
        self.assertEqual(set(ts), {"agy", "claude"})
        self.assertTrue(ts["agy"]["available"])
        self.assertEqual(set(ts["agy"]["tiers"]), {"basic", "light", "complex", "heavy"})
        self.assertEqual(ts["agy"]["capacity"], 2)
        until = time.time() + 7200
        self.store.set_tool_quota("agy", "exhausted", until=until, last_error="quota", cfg=self.cfg)
        ts = self.get("/api/state")["tool_status"]
        self.assertFalse(ts["agy"]["available"])
        self.assertAlmostEqual(ts["agy"]["until"], until, delta=1)
        self.assertFalse(ts["agy"]["tiers"]["basic"]["available"])
        self.assertTrue(ts["claude"]["available"])

    def test_activity_lists_newest_tool_calls_across_runs(self):
        handle_hook("PreToolUse", {"session_id": "s1", "jarvieeTool": "claude", "tool_name": "Read",
                                   "tool_input": {"file_path": "hub/server.py"}, "tool_use_id": "c1"}, self.cfg, self.store)
        items = self.get("/api/activity?limit=5")["items"]
        self.assertTrue(items)
        self.assertEqual(items[0]["run_id"], "claude-s1")
        self.assertEqual(items[0]["kind"], "tool")
        self.assertIn("hub/server.py", items[0]["args"])
        self.assertLessEqual(len(spans.recent(self.store, 1)), 1)


if __name__ == "__main__":
    unittest.main()


class TranscriptModelTests(unittest.TestCase):
    def test_hook_session_model_comes_from_its_transcript(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from hub import server, usage
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "-Users-x-proj"
            proj.mkdir()
            sid = "1234abcd-0000-1111-2222-333344445555"
            (proj / f"{sid}.jsonl").write_text('{"type":"user"}\n{"message":{"model":"claude-sonnet-5-5","role":"assistant"}}\n'
                                               '{"message":{"model":"claude-opus-5-5","role":"assistant"}}\n')
            with patch.object(usage, "CLAUDE_PROJECTS", Path(td)):
                self.assertEqual(server.transcript_model(sid), "claude-opus-5-5")   # the latest answer wins
                self.assertEqual(server.transcript_model("not a session/../x"), "")
                with TempDir() as tmp:
                    cfg, _ = make_cfg(tmp)
                    store = Store(cfg["db_path"])
                    store.upsert(f"claude-{sid}", source="hook", tool="claude", status="running", task="x")
                    a = next(x for x in server.state(cfg, store)["agents"] if x["id"] == f"claude-{sid}")
                    self.assertEqual(a["model"], "claude-opus-5-5")
                    self.assertEqual(a["tier"], "heavy")
                    self.assertEqual(store.get(f"claude-{sid}")["model"], "claude-opus-5-5")


class LiveSessionHookTests(unittest.TestCase):
    def test_stop_from_a_live_pane_session_means_your_turn(self):
        from hub import launcher, server
        with TempDir() as tmp:
            cfg, _ = make_cfg(tmp)
            store = Store(cfg["db_path"])
            store.upsert("run-live1", source="launcher", tool="claude", status="running", task="x")
            store.upsert("run-head1", source="launcher", tool="claude", status="running", task="y")
            launcher.LIVE_RUNS.add("run-live1")
            try:
                server.handle_hook("Stop", {"session_id": "s", "jarvieeRunId": "run-live1"}, cfg, store)
                server.handle_hook("Stop", {"session_id": "t", "jarvieeRunId": "run-head1"}, cfg, store)
            finally:
                launcher.LIVE_RUNS.discard("run-live1")
            self.assertEqual(store.get("run-live1")["status"], "waiting")
            self.assertEqual(store.get("run-head1")["status"], "running")   # a print run is just ending
