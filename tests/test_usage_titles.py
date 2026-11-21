"""Titles, soft clear, usage sources and hook PID capture (dashboard rework, 2026-11-09)."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT, make_cfg

from hub import usage
from hub.server import handle_hook
from hub.store import Store
from hub.titles import make_summary, make_title

sys.path.insert(0, str(ROOT / "hooks"))
import report  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_titles_strip_markdown_and_fit(self):
        long = ("Fix the agent-hub dashboard layout symmetry, enable multi-line text wrapping for long tasks, "
                "and ensure the stop button works. ### Context & Target Files - `hub/dashboard.html`")
        self.assertEqual(make_title(long), "Fix the agent-hub dashboard layout symmetry")
        self.assertTrue(make_summary(long).startswith("enable multi-line text wrapping"))
        self.assertEqual(make_title("# Rework the Agent Hub command center: cleaner layout, more data\n\nBody"),
                         "Rework the Agent Hub command center")
        self.assertEqual(make_title("### Task\n**Fix** the `stop` button. More."), "Fix the stop button")
        self.assertEqual(make_title("(session started in the app)"), "Session started in the app")
        self.assertEqual(make_title(""), "")
        for text in ("a" * 300, "word " * 80, "x, " * 50, "```\ncode\n```\n---\n- [ ] " + "long words here " * 20):
            t = make_title(text)
            self.assertLessEqual(len(t), 60, t)
            self.assertNotIn("\n", t)
            self.assertNotRegex(t, r"[#`*]")
        self.assertLessEqual(len(make_summary("Do it. " + "more detail " * 40)), 120)


class StoreTests(unittest.TestCase):
    def test_clear_hides_but_keeps_history_and_revives(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "hub.db"))
            store.upsert("a", status="done", ended=time.time(), tool="agy", started=time.time() - 60)
            store.upsert("b", status="running", tool="claude")
            store.clear_finished()
            self.assertEqual([r["id"] for r in store.all()], ["b"])
            self.assertEqual({r["id"] for r in store.history(time.time() - 3600)}, {"a", "b"})
            store.upsert("a", status="running")          # the session came back: listed again
            self.assertIn("a", [r["id"] for r in store.all()])


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        now = time.time()
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        proj = root / "claude" / "-x"
        proj.mkdir(parents=True)
        line = lambda mid, t, model="claude-opus-5-5": json.dumps({"type": "assistant", "requestId": "r" + mid, "timestamp": iso(t),
            "message": {"id": mid, "model": model, "usage": {"input_tokens": 1000, "output_tokens": 2000,
            "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 500, "cache_creation": {"ephemeral_1h_input_tokens": 500}}}})
        # m1 twice (deduped), one unpriced model, one line from 10 days ago (ignored)
        (proj / "s.jsonl").write_text("\n".join([line("m1", now - 60), line("m1", now - 60), line("m2", now - 30, "claude-mystery-9"),
                                                 line("m3", now - 10 * 86400), '{"type":"user"}']) + "\n")
        agy = root / "agy" / "conv1" / ".system_generated" / "logs"
        agy.mkdir(parents=True)
        (agy / "transcript.jsonl").write_text(json.dumps({"step_index": 1, "created_at": iso(now - 30), "input_tokens": 700,
                                                          "output_tokens": 70, "cache_read_tokens": 7000}) + "\n")
        self.patches = [patch.object(usage, "CLAUDE_PROJECTS", root / "claude"), patch.object(usage, "AGY_BRAIN", root / "agy"),
                        patch.object(usage, "LIMITS_FILE", root / "limits.json"),
                        patch.object(usage, "_claude_scan", usage._Scanner(usage._claude_line)),
                        patch.object(usage, "_agy_scan", usage._Scanner(usage._agy_line))]
        for p in self.patches:
            p.start()
        self.root, self.now = root, now

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_claude_tokens_cost_and_missing_limits(self):
        c = usage.claude_section(self.now)
        self.assertEqual(c["tokens_today"]["output"], 4000)              # m1 once + m2
        self.assertTrue(c["cost_partial"])                               # claude-mystery-9 has no price
        expected = (1000 * 4 + 2000 * 20 + 10000 * 0.20 + 500 * 4 * 2) / 1e6
        self.assertAlmostEqual(c["cost_today"], round(expected, 4), places=4)
        self.assertIsNone(c["limits"])                                   # never invented
        self.assertIn("status-line", c["limits_reason"])
        self.assertEqual(c["by_model"][0]["label"], "Opus 5.5")

    def test_limits_from_status_line_file(self):
        (self.root / "limits.json").write_text(json.dumps({"captured_at": self.now, "rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": self.now + 600},
            "seven_day": {"used_percentage": 80, "resets_at": self.now - 5}}}))
        limits, _ = usage.claude_limits(self.now)
        five, week = limits["windows"]
        self.assertEqual((five["id"], five["used_pct"]), ("five_hour", 42.0))
        self.assertIsNone(week["used_pct"])                              # window already reset: unknown, not 80

    def test_agy_tokens_and_usage_parse(self):
        with patch.object(usage, "agy_quota", return_value=(None, "The agy CLI isn't on PATH.")):
            a = usage.agy_section(self.now)
        self.assertEqual(a["tokens_today"]["input"], 700)
        self.assertIsNone(a["quota"])
        groups = usage._parse_agy_usage(json.dumps({"command": {"data": {"groups": [{"name": "Gemini Models",
            "description": "Models within this group: Gemini Flash", "buckets": [{"id": "g5", "window": "5h",
            "remaining_fraction": 0.25, "reset_time": "2026-11-08T17:27:21Z"}]}]}}}))
        b = groups[0]["buckets"][0]
        self.assertEqual((b["label"], b["used_pct"], b["remaining_pct"]), ("5-hour", 75.0, 25.0))
        self.assertEqual(groups[0]["models"], "Gemini Flash")


class HookPidTests(unittest.TestCase):
    def test_find_agent_walks_past_the_shell_and_refuses_the_app(self):
        ps = "\n".join(["  100     1 /usr/bin/login", "  200   100 claude", "  300   200 /bin/zsh",
                        "  400     1 /Applications/Antigravity.app/Contents/Resources/bin/language_server", "  500   400 /bin/sh"])
        with patch("subprocess.run") as run:
            run.return_value.stdout = ps
            self.assertEqual(report.find_agent("claude", 300), (200, ""))
            self.assertEqual(report.find_agent("agy", 500), (None, "antigravity-app"))

    def test_hook_stores_agent_pid_host_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = make_cfg(tmp)
            store = Store(cfg["db_path"])
            aid = handle_hook("UserPromptSubmit", {"session_id": "s1", "jarvieeTool": "claude", "pid": 300, "agentPid": 200,
                                                   "prompt": "## Fix the **stop** button\nso it works for every agent"}, cfg, store)
            row = store.get(aid)
            self.assertEqual((row["pid"], row["agent_pid"], row["title"]), (300, 200, "Fix the stop button"))
            self.assertEqual(row["summary"], "so it works for every agent")
            aid2 = handle_hook("SessionStart", {"conversationId": "c1", "jarvieeTool": "agy", "agentHost": "antigravity-app"}, cfg, store)
            self.assertEqual(store.get(aid2)["agent_host"], "antigravity-app")


if __name__ == "__main__":
    unittest.main()


class WaitingStopTests(unittest.TestCase):
    def test_waiting_agents_stop_or_dismiss(self):
        import signal
        from hub import launcher
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "hub.db"))
            # a waiting Claude Code session with a verified process is really stopped
            store.upsert("w1", source="hook", tool="claude", status="waiting", agent_pid=4242)
            with patch("hub.launcher.proc_name", return_value="claude"), patch("os.kill") as kill:
                self.assertEqual(launcher.stop_plan(store.get("w1"))[0], "stop")
                self.assertTrue(launcher.stop("w1", store))
                kill.assert_called_once_with(4242, signal.SIGTERM)
            self.assertEqual(store.get("w1")["activity"], "stopped by you")
            # a waiting Antigravity-app conversation can't be signalled: dismissed, never killed
            store.upsert("w2", source="hook", tool="agy", status="waiting", agent_host="antigravity-app")
            with patch("os.kill") as kill:
                kind, pid, why = launcher.stop_plan(store.get("w2"))
                self.assertEqual((kind, pid), ("dismiss", None))
                self.assertIn("Antigravity app", why)
                self.assertTrue(launcher.stop("w2", store))
                kill.assert_not_called()
            row = store.get("w2")
            self.assertEqual(row["status"], "stopped")
            self.assertIn("dismissed", row["activity"])
            # a running app conversation still refuses (it would come straight back)
            store.upsert("r1", source="hook", tool="agy", status="running", agent_host="antigravity-app")
            self.assertEqual(launcher.stop_plan(store.get("r1"))[0], None)
            self.assertFalse(launcher.stop("r1", store))
