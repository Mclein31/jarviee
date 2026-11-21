"""The slash-command registry (hub/slash.py), its answers through POST /api/run, and GET /api/commands."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import auth, slash, usage
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class ParseTests(unittest.TestCase):
    def test_every_command_and_alias_parses(self):
        for c in slash.COMMANDS:
            for n in (c.name,) + c.aliases:
                with self.subTest(n=n):
                    got, rest = slash.parse(f"/{n} some words")
                    self.assertIs(got, c)
                    self.assertEqual(rest, "some words")
                    self.assertIs(slash.parse(f"  /{n.upper()}")[0], c)

    def test_older_commands_keep_their_own_detection(self):
        self.assertEqual(slash.parse("/start work")[0].name, "startwork")
        self.assertEqual(slash.parse("/end-work")[0].name, "endwork")
        self.assertEqual(slash.parse("/endowrk")[0].name, "endwork")
        c, rest = slash.parse("/pipeline: fix the login bug")
        self.assertEqual((c.name, rest), ("pipeline", "fix the login bug"))
        c, rest = slash.parse("/orchestrate  build it")
        self.assertEqual((c.name, rest), ("orchestrate", "build it"))

    def test_arguments(self):
        self.assertEqual(slash.parse("/map Beta Site")[1], "Beta Site")
        self.assertEqual(slash.parse("/standup 2026-01-31")[1], "2026-01-31")
        self.assertEqual(slash.parse("/help")[1], "")

    def test_plain_text_and_look_alikes_are_not_commands(self):
        for t in ("fix the login", "", "/", "please /help me", "/src/app.py fix it"):
            with self.subTest(t=t):
                self.assertEqual(slash.parse(t), (None, t))

    def test_registry_shape(self):
        reg = slash.registry()
        self.assertEqual([c["name"] for c in reg],
                         ["help", "status", "standup", "usage", "map", "startwork", "endwork", "pipeline", "orchestrate"])
        for c in reg:
            self.assertEqual(set(c), {"name", "aliases", "args", "summary", "launches_agents", "may_use_many_tokens"})
            self.assertTrue(c["summary"])
            self.assertIsInstance(c["aliases"], list)
        by = {c["name"]: c for c in reg}
        self.assertEqual(by["map"]["args"], "<project>")
        self.assertTrue(by["map"]["launches_agents"] and by["map"]["may_use_many_tokens"])
        self.assertFalse(any(by[n]["launches_agents"] for n in ("help", "status", "standup", "usage", "startwork", "endwork")))
        self.assertNotIn("endowrk", by["endwork"]["aliases"])   # typo aliases work but are hidden
        self.assertIs(slash.parse("/endowrk")[0].name == "endwork", True)


class ServerBase(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def call(self, path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        data = None
        if body is not None:
            h.setdefault("X-Jarviee-Token", auth.get_token(self.cfg))
            data = json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with _OPEN(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def run_cmd(self, text):
        return self.call("/api/run", {"task": text})

    def agents(self):
        return self.store.all()


class CommandsEndpointTests(ServerBase):
    def test_lists_the_registry(self):
        code, out = self.call("/api/commands")
        self.assertEqual(code, 200)
        self.assertEqual(out, {"commands": slash.registry()})
        self.assertIn("help", [c["name"] for c in out["commands"]])

    def test_same_origin_checks_as_other_gets(self):
        self.assertEqual(self.call("/api/commands", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("/api/commands", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("/api/commands", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)


class AnswerTests(ServerBase):
    def test_help_lists_every_command_and_launches_nothing(self):
        code, out = self.run_cmd("/help")
        self.assertEqual(code, 200)
        self.assertEqual(out["slash"], "help")
        for c in slash.registry():
            self.assertIn("/" + c["name"], out["text"])
        self.assertIn("/map <project>", out["text"])
        self.assertEqual(out["commands"], slash.registry())
        self.assertEqual(self.agents(), [])

    def test_status_counts_the_fleet_per_tool(self):
        now = time.time()
        st = self.store
        st.upsert("c-run", tool="claude", status="running", source="hook", updated=now, title="build it")
        st.upsert("c-need", tool="claude", status="waiting", source="hook", updated=now, activity="Permission needed", title="needs an ok")
        st.upsert("c-replied", tool="claude", status="waiting", source="hook", updated=now, activity="Turn finished", title="replied")
        st.upsert("c-stale", tool="claude", status="running", source="hook", updated=now - 8000)
        st.upsert("a-fail", tool="agy", status="failed", source="hook", updated=now, ended=now - 60)
        st.upsert("a-old", tool="agy", status="failed", source="hook", updated=now - 7200, ended=now - 7200)
        st.upsert("a-done", tool="agy", status="done", source="hook", updated=now, ended=now - 10)
        plan = self.srv.tasks.add_plan("g", "Alpha", [
            {"id": "1", "title": "t1", "prompt": "p", "level": "light", "tool": "agy", "model": "m"},
            {"id": "2", "title": "t2", "prompt": "p", "level": "light", "tool": "agy", "model": "m"}])
        self.srv.tasks.approve(plan)
        code, out = self.run_cmd("/status")
        self.assertEqual(code, 200)
        f = out["fleet"]
        self.assertEqual(f["total"], {"running": 1, "needs_you": 1, "failed_1h": 1, "queued": 2})
        self.assertEqual(f["tools"]["claude"], {"running": 1, "needs_you": 1, "failed_1h": 0, "queued": 0})
        self.assertEqual(f["tools"]["agy"], {"running": 0, "needs_you": 0, "failed_1h": 1, "queued": 2})
        self.assertEqual([n["id"] for n in f["needs_you"]], ["c-need"])
        self.assertIn("1 running, 1 need you, 1 failed in the last hour, 2 queued", out["text"])
        self.assertIn("Claude Code", out["text"])
        self.assertIn("needs an ok", out["text"])

    def test_status_on_an_empty_fleet(self):
        code, out = self.run_cmd("/status")
        self.assertEqual(code, 200)
        self.assertIn("0 running", out["text"])
        self.assertIn("Nothing is running", out["text"])

    def test_standup_matches_the_api(self):
        now = time.time()
        self.store.upsert("r1", project="Alpha", status="done", tool="claude", started=now - 600, ended=now - 60, title="shipped")
        code, out = self.run_cmd("/standup")
        self.assertEqual(code, 200)
        api = self.call("/api/standup")[1]
        self.assertEqual(out["standup"], api)
        self.assertIn("1 done", out["text"])
        self.assertIn("shipped", out["text"])

    def test_standup_with_a_date_and_bad_dates(self):
        code, out = self.run_cmd("/standup 2020-01-02")
        self.assertEqual(code, 200)
        self.assertEqual(out["standup"]["date"], "2020-01-02")
        self.assertEqual(out["standup"], self.call("/api/standup?date=2020-01-02")[1])
        self.assertEqual(self.run_cmd("/standup yesterday")[0], 200)
        for bad in ("/standup tomorrow-ish", "/standup 2026-13-45", "/standup 31/01/2026"):
            with self.subTest(bad=bad):
                code, out = self.run_cmd(bad)
                self.assertEqual(code, 400)
                self.assertIn("YYYY-MM-DD", out["error"])

    def test_usage_without_a_reading_does_not_wait_or_call_agy(self):
        usage._cache["data"] = None
        with mock.patch.object(usage, "get_usage", side_effect=AssertionError("must use the cache")), \
                mock.patch.object(usage, "_refresh_agy_quota", side_effect=AssertionError("must not run agy")):
            code, out = self.run_cmd("/usage")
        self.assertEqual(code, 200)
        self.assertIsNone(out["usage"])
        self.assertIn("No usage reading yet", out["text"])

    def test_usage_reports_cached_values_and_when_they_were_read(self):
        now = time.time()
        usage._cache["data"] = {
            "generated": now - 300,
            "claude": {"tokens_today": {"total": 1234567}, "cost_today": 3.5, "cost_partial": False, "tokens_week": 9000000,
                       "limits": {"captured_at": now - 120, "windows": [
                           {"id": "seven_day", "label": "Weekly", "used_pct": 42.0, "resets_at": now + 2 * 86400 + 600, "note": ""},
                           {"id": "five_hour", "label": "5-hour session", "used_pct": None, "resets_at": None, "note": "reset since"}]},
                       "limits_reason": ""},
            "agy": {"tokens_today": {"total": 500}, "quota_reason": "",
                    "quota": {"captured_at": now - 200, "groups": [{"name": "Gemini 4", "models": "x", "buckets": [
                        {"label": "Weekly", "used_pct": 10.0, "remaining_pct": 90.0, "resets_at": now + 3600}]}]}},
        }
        try:
            with mock.patch.object(usage, "get_usage", side_effect=AssertionError("must use the cache")):
                code, out = self.run_cmd("/usage")
        finally:
            usage._cache["data"] = None
        self.assertEqual(code, 200)
        t = out["text"]
        self.assertIn("read 5 min ago", t)
        self.assertIn("Weekly: 42% used, resets in 2d", t)
        self.assertIn("5-hour session: reset since", t)
        self.assertIn("1,234,567 tokens", t)
        self.assertIn("Gemini 4: Weekly 10% used", t)
        self.assertIn("quota read 3 min ago", t)

    def test_usage_with_a_broken_section(self):
        usage._cache["data"] = {"generated": time.time(), "claude": {"error": "OSError: nope"}, "agy": {"error": "boom"}}
        try:
            code, out = self.run_cmd("/usage")
        finally:
            usage._cache["data"] = None
        self.assertEqual(code, 200)
        self.assertIn("could not be read (OSError: nope)", out["text"])


class MapTests(ServerBase):
    def test_unknown_project_lists_close_matches(self):
        code, out = self.run_cmd("/map Alpa")
        self.assertEqual(code, 400)
        self.assertIn("no project 'Alpa'", out["error"])
        self.assertIn("Did you mean: Alpha", out["error"])
        self.assertEqual(out["close_matches"], ["Alpha"])
        self.assertEqual(self.agents(), [])

    def test_unknown_project_without_matches_lists_the_projects(self):
        code, out = self.run_cmd("/map zzzzzz")
        self.assertEqual(code, 400)
        self.assertEqual(out["close_matches"], [])
        self.assertIn("Alpha", out["error"])
        self.assertIn("Beta Site", out["error"])

    def test_a_folder_without_a_brain_is_not_a_project(self):
        code, out = self.run_cmd("/map NoBrain")
        self.assertEqual(code, 400)
        self.assertNotIn("NoBrain,", out["error"])

    def test_needs_a_project(self):
        code, out = self.run_cmd("/map")
        self.assertEqual(code, 400)
        self.assertIn("/map <project>", out["error"])

    def test_first_map_launches_the_map_agent_and_warns(self):
        code, out = self.run_cmd("/map alpha")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["launched"])
        self.assertEqual((out["slash"], out["project"], out["mode"]), ("map", "Alpha", "create"))
        self.assertIn("many tokens", out["warning"])
        rid = out["id"]
        end = time.time() + 20
        row = self.store.get(rid)
        while row["status"] not in ("done", "failed", "stopped") and time.time() < end:
            time.sleep(0.2)
            row = self.store.get(rid)
        self.assertEqual(row["project"], "Alpha")
        self.assertIn("FLOW.json", row["task"])
        # the fake agent wrote no map: it is not accepted and nothing is committed
        self.assertFalse((Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha" / ".brain" / "FLOW.json").exists())

    def test_a_map_agent_already_running_is_not_started_twice(self):
        from hub import projmap
        self.store.upsert("running-map", status="running", source="launcher", task=projmap.brief("Alpha"), updated=time.time())
        code, out = self.run_cmd("/map Alpha")
        self.assertEqual(code, 200)
        self.assertFalse(out["launched"])
        self.assertIn("already working", out["text"])
        self.assertEqual([a["id"] for a in self.agents()], ["running-map"])

    def test_an_existing_map_is_updated_not_rebuilt(self):
        flow = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha" / ".brain" / "FLOW.json"
        flow.write_text("{}", encoding="utf-8")
        with mock.patch.object(slash.mapwatch, "launch", return_value=(True, "Updating the Alpha map.", "run-x")) as m:
            code, out = self.run_cmd("/map Alpha")
        self.assertEqual(code, 200)
        self.assertEqual((out["mode"], out["id"], out["launched"]), ("update", "run-x", True))
        self.assertEqual(m.call_args.args[2], "Alpha")
        self.assertTrue(m.call_args.kwargs["force"])


class OlderCommandsStillWork(ServerBase):
    def test_pipeline_prefix_still_reaches_the_normal_launch(self):
        code, out = self.run_cmd("/pipeline fix a typo")
        self.assertEqual(code, 200)
        self.assertNotIn("slash", out)
        self.assertIn("id", out)

    def test_orchestrate_without_a_task_is_still_refused(self):
        code, out = self.run_cmd("/orchestrate")
        self.assertEqual(code, 400)
        self.assertIn("needs a task", out["error"])


if __name__ == "__main__":
    unittest.main()
