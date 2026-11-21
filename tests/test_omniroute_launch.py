"""Tests for OmniRoute config loading, child process env wiring, pane specs, fail-fast, and secret hygiene."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from fake_iterm import FakeITerm, make_terminal
from helpers import ROOT, TempDir, make_cfg
from hub import auth, finaltext, ingest, launcher, omniroute, panes, panerun, runplan
from hub.config import load_config
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
TEST_KEY = "test-omniroute-token-abc123"


class ConfigTests(unittest.TestCase):
    def test_config_json_loads_omniroute_without_routing_changes(self):
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertIn("omniroute", raw)
        self.assertIn("omniroute", raw["tools"])
        self.assertNotIn("omniroute", raw.get("lanes", {}))
        self.assertNotIn("omniroute", [v.get("tool") for v in raw.get("dispatch", {}).values()])
        self.assertNotIn("omniroute", raw.get("fallback", {}).get("equivalents", {}))

        cfg = load_config(ROOT / "config.json")
        tcfg = cfg["tools"]["omniroute"]
        self.assertEqual(tcfg["env_from"], "omniroute")
        self.assertEqual(tcfg["models"]["complex"], "claude/combo/jarviee-complex")
        self.assertNotIn("effort_args", tcfg)

        argv = runplan.build_argv(tcfg, "fix bug", tcfg["models"]["complex"], "medium", session="sess-1")
        self.assertEqual(
            argv,
            ["claude", "-p", "fix bug", "--model", "claude/combo/jarviee-complex",
             "--permission-mode", "auto", "--session-id", "sess-1"],
        )
        self.assertEqual(runplan.readonly_args("omniroute"), ["--permission-mode", "plan"])
        live = runplan.interactive_argv("omniroute", argv)
        self.assertNotIn("-p", live)
        self.assertIn("--permission-mode", live)


class LaunchEnvTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["retry"] = {"enabled": False}
        self.store = Store(self.cfg["db_path"])
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self.store.close()
        self._t.__exit__()

    def _add_omniroute_probe(self, out_file, echo_secret=False):
        script = (
            "import json, os, sys; "
            f"open({str(out_file)!r}, 'w').write(json.dumps({{"
            "'base': os.environ.get('ANTHROPIC_BASE_URL'), "
            "'auth': os.environ.get('ANTHROPIC_AUTH_TOKEN'), "
            "'api_key': os.environ.get('ANTHROPIC_API_KEY'), "
            "'tool': os.environ.get('JARVIEE_TOOL'), "
            "'argv': sys.argv[1:]"
            "})); "
            + ("print('leaked:', os.environ.get('ANTHROPIC_AUTH_TOKEN', '')); " if echo_secret else "")
            + "print('probe-done')"
        )
        self.cfg["tools"]["omniroute"] = {
            "command": [sys.executable, "-c", script, "{model}", "{prompt}"],
            "env_from": "omniroute",
            "models": dict(self.cfg["tools"]["omniroute"]["models"]),
            "efforts": dict(self.cfg["tools"]["omniroute"]["efforts"]),
            "effort_levels": dict(self.cfg["tools"]["omniroute"]["effort_levels"]),
        }

    def test_omniroute_run_gets_env_and_pops_anthropic_api_key(self):
        probe_out = Path(self.tmp) / "probe.json"
        self._add_omniroute_probe(probe_out)
        os.environ["OMNIROUTE_API_KEY"] = TEST_KEY
        os.environ["ANTHROPIC_API_KEY"] = "personal-api-key-should-not-leak"

        rid, p = launcher.launch("do work", self.cfg, self.store, tool="omniroute", tier="complex", cwd=self.tmp)
        self.assertTrue(launcher.wait(rid, timeout=10))
        self.assertEqual(p["tool"], "omniroute")

        seen = json.loads(probe_out.read_text(encoding="utf-8"))
        self.assertEqual(seen["base"], "http://127.0.0.1:20128")
        self.assertEqual(seen["auth"], TEST_KEY)
        self.assertIsNone(seen["api_key"])
        self.assertEqual(seen["tool"], "omniroute")
        self.assertFalse(any(TEST_KEY in str(a) for a in seen["argv"]))

    def test_claude_and_agy_runs_do_not_get_omniroute_env(self):
        os.environ["OMNIROUTE_API_KEY"] = TEST_KEY
        os.environ["JARVIEE_TOOL"] = "omniroute"
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

        for tool in ("claude", "agy"):
            probe_out = Path(self.tmp) / f"probe-{tool}.json"
            script = (
                "import json, os; "
                f"open({str(probe_out)!r}, 'w').write(json.dumps({{"
                "'base': os.environ.get('ANTHROPIC_BASE_URL'), "
                "'auth': os.environ.get('ANTHROPIC_AUTH_TOKEN'), "
                "'tool': os.environ.get('JARVIEE_TOOL')"
                "})); print('ok')"
            )
            self.cfg["tools"][tool]["command"] = [sys.executable, "-c", script, "{model}", "{prompt}"]
            rid, _ = launcher.launch("do work", self.cfg, self.store, tool=tool, tier="basic", cwd=self.tmp)
            self.assertTrue(launcher.wait(rid, timeout=10))
            seen = json.loads(probe_out.read_text(encoding="utf-8"))
            self.assertIsNone(seen["base"])
            self.assertIsNone(seen["auth"])
            self.assertIsNone(seen["tool"])

    def test_missing_key_fails_fast_without_spawning_or_recording(self):
        os.environ.pop("OMNIROUTE_API_KEY", None)
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "stale-token-must-be-cleared"
        probe_out = Path(self.tmp) / "should_not_exist.json"
        self._add_omniroute_probe(probe_out)

        with mock.patch.object(omniroute, "api_key", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                launcher.launch("do work", self.cfg, self.store, tool="omniroute", tier="basic", cwd=self.tmp)

        self.assertIn("OmniRoute has no API key", str(ctx.exception))
        self.assertIn("python3 hub.py omniroute connect", str(ctx.exception))
        self.assertFalse(probe_out.exists())
        self.assertEqual(self.store.all(), [])
        self.assertEqual(list(Path(self.cfg["logs_dir"]).glob("run-*.log")), [])

    def test_env_for_requires_base_url_and_rejects_unknown_env_from(self):
        with self.assertRaises(ValueError):
            runplan.env_for({"env_from": "unknown-provider"}, self.cfg, {})
        with mock.patch.object(omniroute, "apply_env", return_value={"ANTHROPIC_AUTH_TOKEN": TEST_KEY}):
            with self.assertRaises(ValueError):
                runplan.env_for({"env_from": "omniroute"}, self.cfg, {})

    def test_secret_absent_from_header_log_brain_and_state(self):
        probe_out = Path(self.tmp) / "probe-leak.json"
        self._add_omniroute_probe(probe_out, echo_secret=True)
        os.environ["OMNIROUTE_API_KEY"] = TEST_KEY

        rid, p = launcher.launch("secret check", self.cfg, self.store, tool="omniroute", tier="basic", project="Alpha")
        self.assertTrue(launcher.wait(rid, timeout=10))

        row = self.store.get(rid)
        log_text = Path(row["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn(TEST_KEY, log_text)
        self.assertIn("***", log_text)
        self.assertNotIn(TEST_KEY, json.dumps(row))
        self.assertNotIn(TEST_KEY, panerun.pane_title(p, "secret check", self.tmp))

        runs_md = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha" / ".brain" / "AGENT_RUNS.md"
        self.assertTrue(runs_md.is_file())
        self.assertNotIn(TEST_KEY, runs_md.read_text(encoding="utf-8"))

    def test_custom_tool_with_env_from_gets_env_and_retry_preserves_it(self):
        probe_out = Path(self.tmp) / "probe-retry.json"
        attempt_log = Path(self.tmp) / "attempts.txt"
        script = (
            "import json, os, pathlib; "
            f"p = pathlib.Path({str(attempt_log)!r}); "
            "n = len(p.read_text().splitlines()) if p.exists() else 0; "
            "p.write_text((p.read_text() if p.exists() else '') + f'try-{n}\\n'); "
            f"open({str(probe_out)!r}, 'w').write(json.dumps({{"
            "'base': os.environ.get('ANTHROPIC_BASE_URL'), "
            "'auth': os.environ.get('ANTHROPIC_AUTH_TOKEN'), "
            "'tool': os.environ.get('JARVIEE_TOOL'), "
            "'attempt': n"
            "})); "
            "import sys; sys.exit(1 if n == 0 else 0)"
        )
        self.cfg["retry"] = {"enabled": True, "max": 2}
        self.cfg["tools"]["fake_gw"] = {
            "command": [sys.executable, "-c", script, "{model}", "{prompt}"],
            "env_from": "omniroute",
            "models": {"basic": "gw-small", "light": "gw-mid", "complex": "gw-large", "heavy": "gw-max"},
            "efforts": {"basic": "low", "light": "low", "complex": "medium", "heavy": "high"},
        }
        os.environ["OMNIROUTE_API_KEY"] = TEST_KEY

        rid, _ = launcher.launch("retry me", self.cfg, self.store, tool="fake_gw", tier="basic", cwd=self.tmp)
        self.assertTrue(launcher.wait(rid, timeout=10))
        with self.store.db as db:
            child = db.execute("SELECT * FROM agents WHERE parent_run_id=?", (rid,)).fetchone()
        self.assertIsNotNone(child)
        self.assertTrue(launcher.wait(child["id"], timeout=10))
        self.assertEqual(self.store.get(child["id"])["status"], "done")
        seen = json.loads(probe_out.read_text(encoding="utf-8"))
        self.assertEqual(seen["attempt"], 1)
        self.assertEqual(seen["base"], "http://127.0.0.1:20128")
        self.assertEqual(seen["auth"], TEST_KEY)
        self.assertEqual(seen["tool"], "omniroute")

    def test_panerun_registers_spec_with_omniroute_env(self):
        self._add_omniroute_probe(Path(self.tmp) / "unused.json")
        os.environ["OMNIROUTE_API_KEY"] = TEST_KEY
        os.environ["ANTHROPIC_API_KEY"] = "personal-api-key"

        captured = {}

        class StubTerm:
            def open_pane(self, rid, cmd):
                captured["spec"] = panes.claim(rid)
                panes.event(rid, {"event": "start", "pid": os.getpid()})
                panes.event(rid, {"event": "exit", "code": 0})
                return "S-stub"

            def close_pane(self, rid):
                pass

        log_path = Path(self.tmp) / "pane-stub.log"
        log_path.write_text("", encoding="utf-8")
        env = runplan.env_for(self.cfg["tools"]["omniroute"], self.cfg)
        panerun.run_in_pane(
            "run-stub1", StubTerm(), self.cfg, self.store,
            {"offset": 0, "log_steps": 0, "has_output": False},
            {"tool": "omniroute", "tier": "basic", "model": "claude/combo/jarviee-basic", "project": ""},
            "stub", self.cfg["tools"]["omniroute"]["command"], self.tmp, env, log_path,
        )
        spec = captured["spec"]
        self.assertEqual(spec["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:20128")
        self.assertEqual(spec["env"]["ANTHROPIC_AUTH_TOKEN"], TEST_KEY)
        self.assertEqual(spec["env"]["JARVIEE_TOOL"], "omniroute")
        self.assertNotIn("ANTHROPIC_API_KEY", spec["env"])
        self.assertFalse(any(TEST_KEY in str(a) for a in spec["argv"]))
        panes.drop("run-stub1")

    def test_finaltext_reads_claude_transcript_for_omniroute(self):
        proj_dir = Path(self.tmp) / ".claude" / "projects" / "testproj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "sess-omni-1.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "OmniRoute answer."}]}}) + "\n",
            encoding="utf-8",
        )
        with mock.patch.object(finaltext.Path, "home", return_value=Path(self.tmp)):
            out = finaltext.final_text("omniroute", ["claude", "hi", "--session-id", "sess-omni-1"], "hi", 0)
        self.assertEqual(out, "OmniRoute answer.")

    def test_ingest_keeps_omniroute_tool_on_claude_hooks(self):
        self.store.upsert("run-ing1", source="launcher", tool="omniroute", task="work", status="running",
                          model="claude/combo/jarviee-complex", tier="complex", workspace=self.tmp)
        for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            ingest.handle_hook(ev, {"jarvieeRunId": "run-ing1", "jarvieeTool": "claude",
                                    "tool_name": "Read", "session_id": "s-1"}, self.cfg, self.store)
        row = self.store.get("run-ing1")
        self.assertEqual(row["tool"], "omniroute")


class PaneSpecTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.fake = FakeITerm(spawn=True, env=dict(os.environ, JARVIEE_PORT=str(self.cfg["port"])))
        self.cfg["_terminal"] = make_terminal(self.cfg, self.fake, self.tmp)
        self.cfg["terminal"]["close_on_finish"] = "immediately"
        self.cfg["retry"] = {"enabled": False}
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.saved = dict(panes.CLOSE_RULES)
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        panes.CLOSE_RULES.update(self.saved)
        self.fake.stop_all()
        self.srv.scheduler.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def req(self, path, body=None):
        headers = {"Content-Type": "application/json", "X-Jarviee-Token": auth.get_token(self.cfg)}
        r = urllib.request.Request(f"http://127.0.0.1:{self.cfg['port']}{path}", headers=headers,
                                   data=None if body is None else json.dumps(body).encode())
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def until(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            v = cond()
            if v:
                return v
            time.sleep(0.1)
        self.fail("timed out")

    def test_pane_run_gets_omniroute_env_and_state_never_exposes_key(self):
        probe_out = Path(self.tmp) / "pane-probe.json"
        script = (
            "import json, os; "
            f"open({str(probe_out)!r}, 'w').write(json.dumps({{"
            "'base': os.environ.get('ANTHROPIC_BASE_URL'), "
            "'auth': os.environ.get('ANTHROPIC_AUTH_TOKEN'), "
            "'api_key': os.environ.get('ANTHROPIC_API_KEY'), "
            "'tool': os.environ.get('JARVIEE_TOOL')"
            "})); print('pane-omni-ok')"
        )
        self.cfg["tools"]["omniroute"] = {
            "command": [sys.executable, "-c", script, "{model}", "{prompt}"],
            "env_from": "omniroute",
            "models": dict(self.cfg["tools"]["omniroute"]["models"]),
            "efforts": dict(self.cfg["tools"]["omniroute"]["efforts"]),
            "effort_levels": dict(self.cfg["tools"]["omniroute"]["effort_levels"]),
        }
        os.environ["OMNIROUTE_API_KEY"] = TEST_KEY
        os.environ["ANTHROPIC_API_KEY"] = "personal-key-must-be-popped"

        code, res = self.req("/api/run", {"task": "pane omniroute check", "tool": "omniroute", "tier": "basic", "project": "Alpha"})
        self.assertEqual(code, 200)
        rid = res["id"]

        row = self.until(lambda: (a := self.store.get(rid)) and a["status"] in ("done", "failed", "stopped") and a)
        self.assertEqual(row["status"], "done")
        seen = json.loads(probe_out.read_text(encoding="utf-8"))
        self.assertEqual(seen["base"], "http://127.0.0.1:20128")
        self.assertEqual(seen["auth"], TEST_KEY)
        self.assertIsNone(seen["api_key"])
        self.assertEqual(seen["tool"], "omniroute")

        # Spec was one-shot claimed and secret is absent from /api/state and /api/log
        self.assertEqual(self.req(f"/api/run-spec?id={rid}")[0], 410)
        st_code, st = self.req("/api/state")
        self.assertEqual(st_code, 200)
        self.assertNotIn(TEST_KEY, json.dumps(st))
        self.assertNotIn(TEST_KEY, Path(row["log_path"]).read_text(encoding="utf-8"))

    def test_api_run_fails_fast_400_when_key_missing(self):
        os.environ.pop("OMNIROUTE_API_KEY", None)
        real_which = shutil.which   # OmniRoute runs through the claude CLI: pretend it is installed (CI has none)
        with mock.patch.object(omniroute, "api_key", return_value=None), \
                mock.patch("shutil.which", lambda n, *a, **k: "/usr/bin/claude" if n == "claude" else real_which(n, *a, **k)):
            code, res = self.req("/api/run", {"task": "no key", "tool": "omniroute", "tier": "basic"})
        self.assertEqual(code, 400)
        self.assertIn("OmniRoute has no API key", res.get("error", ""))

    def test_panes_claim_is_one_shot_and_clears_spec(self):
        spec = {"run_id": "run-test1", "argv": ["claude"], "env": {"ANTHROPIC_AUTH_TOKEN": TEST_KEY}}
        r = panes.register("run-test1", spec)
        claimed = panes.claim("run-test1")
        self.assertEqual(claimed["env"]["ANTHROPIC_AUTH_TOKEN"], TEST_KEY)
        self.assertIsNone(r.spec)
        self.assertIsNone(panes.claim("run-test1"))

    def test_run_spec_requires_token_without_consuming_spec(self):
        spec = {"run_id": "run-tok1", "argv": ["claude"], "env": {"ANTHROPIC_AUTH_TOKEN": TEST_KEY}}
        panes.register("run-tok1", spec)
        self.addCleanup(panes.drop, "run-tok1")

        def raw_get(headers=None):
            r = urllib.request.Request(f"http://127.0.0.1:{self.cfg['port']}/api/run-spec?id=run-tok1",
                                       headers=headers or {})
            try:
                with _OPEN(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read() or b"{}")

        self.assertEqual(raw_get()[0], 403)
        self.assertEqual(raw_get({"X-Jarviee-Token": "wrong-token"})[0], 403)
        # Spec was NOT consumed by rejected requests
        self.assertFalse(panes.get("run-tok1").claimed)
        self.assertIsNotNone(panes.get("run-tok1").spec)

        code, body = raw_get({"X-Jarviee-Token": auth.get_token(self.cfg)})
        self.assertEqual(code, 200)
        self.assertEqual(body["env"]["ANTHROPIC_AUTH_TOKEN"], TEST_KEY)
        self.assertEqual(raw_get({"X-Jarviee-Token": auth.get_token(self.cfg)})[0], 410)

    def test_report_script_claude_hook_preserves_omniroute_tool_on_row(self):
        self.store.upsert("run-rep1", source="launcher", tool="omniroute", task="work", status="running",
                          model="claude/combo/jarviee-complex", tier="complex", workspace=self.tmp)
        env = dict(os.environ, JARVIEE_PORT=str(self.cfg["port"]),
                   JARVIEE_TOKEN_PATH=str(self.cfg["token_path"]),
                   JARVIEE_RUN_ID="run-rep1")
        for ev, payload in (("SessionStart", {"session_id": "s-rep1"}),
                            ("PostToolUse", {"session_id": "s-rep1", "tool_name": "Edit"})):
            p = subprocess.run([sys.executable, str(ROOT / "hooks" / "report.py"), ev, "claude"],
                               input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(p.returncode, 0)
        row = self.store.get("run-rep1")
        self.assertEqual(row["tool"], "omniroute")
        self.assertEqual(row["steps"], 1)


if __name__ == "__main__":
    unittest.main()
