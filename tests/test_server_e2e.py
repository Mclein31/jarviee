import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import ROOT, TempDir, free_port, make_cfg
from hub.server import make_server, normalize_model, tier_of_model
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, self.cfg_path = make_cfg(self.tmp)
        self.cfg["tools"]["slow"] = {"command": [sys.executable, "-c", "import time; time.sleep(30)", "{model}"],
                                     "models": {"basic": "s", "standard": "s", "heavy": "s"}}
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.ws = Path(self.cfg["obsidian"]["workspace_root"])

    def tearDown(self):
        if self.srv.scheduler:
            self.srv.scheduler.stop()
            self.srv.scheduler._thread.join(3)
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    # -- helpers --
    def req(self, path, body=None, headers=None):
        from hub import auth
        h = {"Content-Type": "application/json", **(headers or {})}
        if body is not None and "X-Jarviee-Token" not in h and "no-token" not in (headers or {}):
            h["X-Jarviee-Token"] = auth.get_token(self.cfg)
        h.pop("no-token", None)
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with _OPEN(r, timeout=5) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers["Content-Type"] else raw.decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def hook(self, event, payload):
        return self.req(f"/hook?event={event}", payload)[1]["id"]

    def agent(self, aid):
        return next((a for a in self.req("/api/state")[1]["agents"] if a["id"] == aid), None)

    def wait_status(self, rid, statuses=("done", "failed", "stopped"), timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            a = self.agent(rid)
            if a and a["status"] in statuses:
                return a
            time.sleep(0.2)
        self.fail(f"{rid} did not reach {statuses}")

    # -- tests --
    def test_dashboard_and_state(self):
        code, html = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("<title>Jarviee</title>", html)
        self.assertNotIn("__SCRIPT", html)
        st = self.req("/api/state")[1]
        self.assertTrue(st["brain"]["ok"] and st["brain"]["status_ok"])
        self.assertEqual([p["name"] for p in st["projects"]], ["Alpha", "Beta Site"])
        self.assertEqual(len(st["status_rows"]), 3)
        self.assertIn("echo", st["tools"])

    def test_rejects_cross_site_requests(self):
        self.assertEqual(self.req("/api/state", headers={"Host": "evil.example:7777"})[0], 403)
        self.assertEqual(self.req("/api/run", {"task": "x"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/run", {"task": "x"}, {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.req("/api/state")[1]["agents"], [])

    def test_agy_hook_lifecycle_and_overkill(self):
        common = {"conversationId": "c1", "workspacePaths": [str(self.ws / "Alpha")],
                  "modelName": "claude-opus-5-5-high"}
        aid = self.hook("SessionStart", common)
        self.assertEqual(aid, "agy-c1")
        self.hook("PreInvocation", {**common, "lastUserInput": "rename foo to bar"})
        for i in range(2):
            self.hook("PostToolUse", {**common, "toolCall": {"name": "run_command"}, "stepIdx": i})
        a = self.agent(aid)
        self.assertEqual((a["status"], a["tier"], a["suggested_tier"], a["steps"]), ("running", "heavy", "basic", 2))
        self.assertEqual(a["activity"], "ran run_command")
        self.hook("Stop", {**common, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True})
        self.assertEqual(self.agent(aid)["status"], "waiting")
        self.hook("Stop", {**common, "terminationReason": "ERROR", "error": "boom"})
        self.assertEqual(self.agent(aid)["status"], "failed")

    def test_claude_hook_lifecycle(self):
        c = {"session_id": "s1", "cwd": "/tmp/x", "jarvieeTool": "claude"}
        aid = self.hook("UserPromptSubmit", {**c, "prompt": "debug the memory leak in the worker"})
        self.assertEqual(aid, "claude-s1")
        self.hook("PreToolUse", {**c, "tool_name": "Bash"})
        self.assertEqual(self.agent(aid)["activity"], "using Bash")
        self.hook("Notification", {**c, "message": "Claude needs your permission to use Bash"})
        a = self.agent(aid)
        self.assertEqual((a["status"], a["suggested_tier"]), ("waiting", "heavy"))
        self.hook("SessionEnd", c)
        self.assertEqual(self.agent(aid)["status"], "done")

    def test_report_script_posts_and_never_blocks(self):
        env = dict(os.environ, JARVIEE_PORT=str(self.cfg["port"]),
                   JARVIEE_TOKEN_PATH=str(self.cfg["token_path"]))
        env.pop("JARVIEE_RUN_ID", None)
        p = subprocess.run([sys.executable, str(ROOT / "hooks/report.py"), "SessionStart", "agy"],
                           input=json.dumps({"conversationId": "r1", "transcriptPath": "/big"}),
                           capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "{}"))
        self.assertIsNotNone(self.agent("agy-r1"))
        # server down, garbage input: still exit 0, fast
        env["JARVIEE_PORT"] = str(free_port())
        t = time.time()
        p = subprocess.run([sys.executable, str(ROOT / "hooks/report.py"), "Stop", "claude"],
                           input="not json", capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertLess(time.time() - t, 4)

    def test_e2e_echo_run_logs_to_brain(self):
        code, r = self.req("/api/run", {"task": "rename getUser to fetchUser", "tool": "echo", "project": "Alpha"})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["tier"], r["model"], r["effort"]), ("basic", "fake-small", "low"))
        # a hook from inside the launched agent attaches to the same card
        self.hook("PostToolUse", {"jarvieeRunId": r["id"], "conversationId": "zz",
                                  "modelName": "something-else", "toolCall": {"name": "edit"}})
        a = self.wait_status(r["id"])
        self.assertEqual((a["status"], a["model"], a["workspace"]), ("done", "fake-small", str(self.ws / "Alpha")))
        self.assertIsNone(self.agent("agy-zz"))
        log = self.req("/api/log?id=" + r["id"])[1]
        self.assertIn("fake agent ran with model fake-small", log)
        self.assertIn("Project brain notes for 'Alpha'", log)
        runs = (self.ws / "Alpha/.brain/AGENT_RUNS.md").read_text(encoding="utf-8")
        self.assertIn("echo / fake-small (basic) · done · rename getUser to fetchUser", runs)

    def test_run_validation_and_stop(self):
        self.assertEqual(self.req("/api/run", {"task": "  "})[0], 400)
        self.assertEqual(self.req("/api/run", {"task": "x", "project": "../Alpha"})[0], 400)
        self.assertEqual(self.req("/api/run", {"task": "x", "tool": "nope"})[0], 400)
        # M6: cwd must stay inside workspace_root
        self.assertEqual(self.req("/api/run", {"task": "fix typo", "tool": "echo", "cwd": str(self.tmp)})[0], 400)
        self.assertEqual(self.req("/api/run", {"task": "fix typo", "tool": "echo", "cwd": "/etc"})[0], 400)
        self.assertEqual(self.req("/api/run", {"task": "fix typo", "tool": "echo", "cwd": "../outside"})[0], 400)
        self.assertEqual(self.req("/api/run", {"task": "/pipeline fix typo", "cwd": "/etc"})[0], 400)
        code, ok_cwd = self.req("/api/run", {"task": "fix typo", "tool": "echo", "cwd": str(self.ws / "Alpha")})
        self.assertEqual(code, 200, ok_cwd)
        self.assertEqual(Path(self.wait_status(ok_cwd["id"])["workspace"]).resolve(), (self.ws / "Alpha").resolve())
        r = self.req("/api/run", {"task": "long job", "tool": "slow"})[1]
        self.assertEqual(Path(self.agent(r["id"])["workspace"]).resolve(), self.ws.resolve())
        self.wait_status(r["id"], ("running",))
        time.sleep(0.3)
        self.assertTrue(self.req("/api/stop", {"id": r["id"]})[1]["ok"])
        self.assertEqual(self.wait_status(r["id"])["status"], "stopped")
        self.req("/api/clear", {})
        self.assertIsNone(self.agent(r["id"]))

    def test_plan_approve_and_queue_via_api(self):
        code, plan = self.req("/api/plan", {"goal": "fix the README typos and add a small tooltip component",
                                             "project": "Alpha", "planner": "mock"})
        self.assertEqual(code, 200, plan)
        self.assertEqual([t["status"] for t in plan["tasks"]], ["draft", "draft"])
        self.assertIn("level", plan["table"])
        self.assertEqual(self.req("/api/state")[1]["queue"]["plans"][0]["status"], "draft")
        self.req("/api/plan/approve", {"id": plan["id"]})
        end = time.time() + 20
        while time.time() < end:
            q = self.req("/api/state")[1]["queue"]
            if all(t["status"] == "done" for t in q["plans"][0]["tasks"]):
                break
            time.sleep(0.2)
        self.assertEqual([t["status"] for t in q["plans"][0]["tasks"]], ["done", "done"])
        self.assertEqual(set(q["lanes"]), {"agy", "claude"})
        self.assertEqual(self.req("/api/task/retry", {"id": plan["id"] + ".t1"})[0], 400)   # done tasks can't retry

    def test_plan_cancel_and_edit_via_api(self):
        code, plan = self.req("/api/plan", {"goal": "initial goal", "project": "Alpha", "planner": "mock"})
        self.assertEqual(code, 200)
        pid = plan["id"]
        # Edit plan goal
        code, edited = self.req("/api/plan/edit", {"id": pid, "goal": "updated goal"})
        self.assertEqual(code, 200)
        self.assertEqual(edited["goal"], "updated goal")
        # Edit a draft task: level and tool change, and the model follows them
        tid = edited["tasks"][0]["id"]
        code, edited = self.req("/api/plan/edit", {"id": pid, "tasks": [{"id": tid, "level": "heavy", "tool": "claude", "title": "renamed"}]})
        self.assertEqual(code, 200)
        t0 = edited["tasks"][0]
        self.assertEqual((t0["level"], t0["tool"], t0["title"]), ("heavy", "claude", "renamed"))
        self.assertEqual(t0["model"], self.cfg["tools"]["claude"]["models"]["heavy"])
        self.assertEqual(self.req("/api/plan/edit", {"id": pid, "tasks": [{"id": tid, "tier": "light"}]})[1]["tasks"][0]["level"], "light")
        self.assertEqual(self.req("/api/plan/edit", {"id": pid, "tasks": [{"id": tid, "level": "nope"}]})[0], 400)
        self.assertEqual(self.req("/api/plan/edit", {"id": pid, "tasks": [{"id": "plan-other.t1", "level": "light"}]})[0], 400)
        # Cancel plan via /api/plan/cancel
        code, cancelled = self.req("/api/plan/cancel", {"id": pid})
        self.assertEqual(code, 200)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(all(t["status"] == "cancelled" for t in cancelled["tasks"]))

    def test_run_diff_and_stale_detection(self):
        # 1. Stale detection for hook session inactive >2h
        old_time = time.time() - 8000
        self.store.upsert("hook-old", source="hook", status="running", task="old background task", started=old_time, updated=old_time)
        st = self.req("/api/state")[1]
        a = next(x for x in st["agents"] if x["id"] == "hook-old")
        self.assertEqual(a["status"], "stale")

        # 2. Diff endpoint
        code, diff_text = self.req("/api/diff?id=hook-old")
        self.assertEqual(code, 200)
        self.assertIn("No directory", diff_text)

    def test_cli_inline_run_and_classify(self):
        env = dict(os.environ, JARVIEE_CONFIG=str(self.cfg_path), JARVIEE_PORT=str(free_port()))
        p = subprocess.run([sys.executable, str(ROOT / "hub.py"), "run", "--project", "Alpha",
                            "fix typo in readme"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("fake agy ran with model gemini-3.8-flash-low", p.stdout)   # dispatch: basic -> agy
        self.assertIn("fix typo in readme", (self.ws / "Alpha/.brain/AGENT_RUNS.md").read_text(encoding="utf-8"))
        p = subprocess.run([sys.executable, str(ROOT / "hub.py"), "classify", "--json", "migrate the database schema"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(json.loads(p.stdout)["tier"], "heavy")


if __name__ == "__main__":
    unittest.main()
