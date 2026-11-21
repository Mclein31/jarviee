"""Structured Claude Code events: the extra hooks (install, reporter, ingest, spans, subagents) and the headless JSON
result (parser, log settling, outcome, exact cost). Payloads follow code.claude.com/docs/en/hooks."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempDir, make_cfg
from hub import claudejson, install, launcher, outcome, runcost, runplan, spans, subagents, usage
from hub.ingest import handle_hook
from hub.store import Store

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "hooks" / "report.py"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NEW_EVENTS = ("PermissionRequest", "PostToolUseFailure", "StopFailure", "SubagentStart", "SubagentStop",
              "TaskCompleted")
PAYLOADS = {
    "PermissionRequest": {"tool_name": "Bash", "tool_input": {"command": "rm -rf node_modules", "description": "x"},
                          "permission_suggestions": [{"type": "addRules"}], "permission_mode": "default"},
    "PostToolUseFailure": {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_use_id": "toolu_01",
                           "error": "Exit code 1\nError: Cannot find module 'express'", "is_interrupt": False,
                           "duration_ms": 4187},
    "StopFailure": {"error": "rate_limit", "error_details": "429 Too Many Requests",
                    "last_assistant_message": "API Error: Rate limit reached"},
    "SubagentStart": {"agent_id": "agent-abc123", "agent_type": "Explore"},
    "SubagentStop": {"agent_id": "abc123", "agent_type": "Explore", "stop_hook_active": False,
                     "agent_transcript_path": "~/.claude/projects/x/s/subagents/agent-abc123.jsonl",
                     "last_assistant_message": "Analysis complete.", "background_tasks": [], "session_crons": []},
    "TaskCompleted": {"task_id": "task-001", "task_subject": "Implement auth", "task_description": "secret detail"},
}


def hook(event, store, cfg, extra=None, **base):
    data = {"jarvieeTool": "claude", "session_id": SID, "cwd": "/Users/alex/proj", "hook_event_name": event,
            **PAYLOADS.get(event, {}), **(extra or {}), **base}
    return handle_hook(event, data, cfg, store)


class InstallEvents(unittest.TestCase):
    def test_new_events_registered_without_matcher(self):
        m = install.merge_claude({})
        for ev in NEW_EVENTS:
            self.assertIn(ev, install.CLAUDE_EVENTS)
            self.assertEqual(len(m["hooks"][ev]), 1)
            self.assertNotIn("matcher", m["hooks"][ev][0])
            self.assertTrue(m["hooks"][ev][0]["hooks"][0]["command"].endswith(f"report.py {ev} claude"))
            self.assertEqual(m["hooks"][ev][0]["hooks"][0]["timeout"], install.TIMEOUT)

    def test_existing_install_upgrades_once_and_uninstalls_clean(self):
        old = install.merge_claude({"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})
        for ev in NEW_EVENTS:   # an install from before these events existed
            old["hooks"].pop(ev)
        up = install.merge_claude(old)
        for ev in NEW_EVENTS:
            self.assertEqual(len(up["hooks"][ev]), 1)
        self.assertEqual(len(up["hooks"]["Stop"]), 2)            # foreign + ours, not duplicated
        self.assertEqual(install.merge_claude(up), up)           # idempotent
        gone = install.merge_claude(up, uninstall=True)
        self.assertEqual(gone, {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})

    def test_run_with_override_file(self):
        with TempDir() as tmp:
            f = Path(tmp) / "settings.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install.run("claude", path=f))
            self.assertIn("PermissionRequest", json.loads(f.read_text())["hooks"])


class ReporterIsSilent(unittest.TestCase):
    def test_print_reply_prints_nothing_for_new_events_even_with_context(self):
        sys.path.insert(0, str(ROOT / "hooks"))
        import report
        for ev in NEW_EVENTS:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                report.print_reply(ev, "claude", {"context": "a message from another agent", "id": "x"})
            self.assertEqual(buf.getvalue(), "", ev)

    def test_reporter_process_prints_nothing_and_exits_zero(self):
        env = dict(os.environ, JARVIEE_PORT="1", AGENT_HUB_PORT="1")   # nothing listens: the post fails fast
        for ev in NEW_EVENTS:
            t0 = time.time()
            r = subprocess.run([sys.executable, str(REPORT), ev, "claude"], input=json.dumps(PAYLOADS[ev]),
                               capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual((r.returncode, r.stdout), (0, ""), ev)
            self.assertLess(time.time() - t0, 2.5, ev)

    def test_payload_is_trimmed(self):
        sys.path.insert(0, str(ROOT / "hooks"))
        import report
        sent = []

        class Resp:
            def read(self):
                return b"{}"

        def fake_open(req, timeout=0):
            sent.append(json.loads(req.data))
            return Resp()

        big = dict(PAYLOADS["PostToolUseFailure"], error="e" * 5000)
        with patch.object(report, "_OPEN", fake_open), patch.object(sys, "stdin", io.StringIO(json.dumps(big))), \
                patch.object(sys, "argv", ["report.py", "PostToolUseFailure", "claude"]), \
                contextlib.redirect_stdout(io.StringIO()):
            report.main()
        self.assertEqual(len(sent[0]["error"]), 300)
        sent.clear()
        with patch.object(report, "_OPEN", fake_open), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(PAYLOADS["SubagentStop"]))), \
                patch.object(sys, "argv", ["report.py", "SubagentStop", "claude"]), \
                contextlib.redirect_stdout(io.StringIO()):
            report.main()
        self.assertNotIn("agent_transcript_path", sent[0])
        self.assertEqual(sent[0]["last_assistant_message"], PAYLOADS["SubagentStop"]["last_assistant_message"][:200])
        sent.clear()
        long = dict(PAYLOADS["SubagentStop"], last_assistant_message="x" * 5000)
        with patch.object(report, "_OPEN", fake_open), patch.object(sys, "stdin", io.StringIO(json.dumps(long))), \
                patch.object(sys, "argv", ["report.py", "SubagentStop", "claude"]), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            report.main()
        self.assertEqual((len(sent[0]["last_assistant_message"]), printed.getvalue()), (200, ""))   # clipped; nothing printed
        self.assertNotIn("agent_transcript_path", sent[0])
        sent.clear()
        with patch.object(report, "_OPEN", fake_open), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(PAYLOADS["StopFailure"]))), \
                patch.object(sys, "argv", ["report.py", "StopFailure", "claude"]), \
                contextlib.redirect_stdout(io.StringIO()):
            report.main()
        self.assertEqual(sent[0]["last_assistant_message"], "API Error: Rate limit reached")


class IngestEvents(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.aid = f"claude-{SID}"
        subagents._events.clear()

    def tearDown(self):
        self._t.__exit__()

    def start(self):
        hook("UserPromptSubmit", self.store, self.cfg, prompt="Fix the build", model="claude-sonnet-5-5")

    def row(self):
        return self.store.get(self.aid)

    def test_permission_request_waits_and_names_the_tool(self):
        self.start()
        hook("PermissionRequest", self.store, self.cfg)
        r = self.row()
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["activity"], "needs permission: Bash rm -rf node_modules")
        # the generic Notification that follows must not replace the specific reason
        hook("Notification", self.store, self.cfg, extra={"notification_type": "permission_prompt",
                                                          "message": "Claude needs your permission"})
        self.assertEqual(self.row()["activity"], "needs permission: Bash rm -rf node_modules")
        hook("PostToolUse", self.store, self.cfg, extra={"tool_name": "Bash"})
        self.assertEqual(self.row()["status"], "running")
        evs = [s for s in spans.for_run(self.store, self.aid) if s["tool"] == "needs_you"]
        self.assertTrue(any("Bash: rm -rf node_modules" in e["args"] or e["args"].startswith("Bash") for e in evs))

    def test_permission_request_from_a_print_run_does_not_wait(self):
        self.start()
        self.store.upsert("run-p1", source="launcher", tool="claude", status="running", activity="working",
                          workspace="/x", task="t")
        handle_hook("PermissionRequest", {"jarvieeTool": "claude", "jarvieeRunId": "run-p1", "session_id": SID,
                                          **PAYLOADS["PermissionRequest"]}, self.cfg, self.store)
        r = self.store.get("run-p1")
        self.assertEqual(r["status"], "running")    # nobody can answer a print run: Claude denies it itself
        self.assertTrue(r["activity"].startswith("permission requested: Bash"))

    def test_tool_failure_marks_the_span_and_keeps_the_status(self):
        self.start()
        hook("PreToolUse", self.store, self.cfg, extra={"tool_name": "Bash", "tool_input": {"command": "npm test"},
                                                        "tool_use_id": "toolu_01"})
        hook("PostToolUseFailure", self.store, self.cfg)
        r = self.row()
        self.assertEqual(r["status"], "running")
        self.assertTrue(r["activity"].startswith("tool failed: Exit code 1"))
        tool = [s for s in spans.for_run(self.store, self.aid) if s["kind"] == "tool"]
        self.assertEqual(len(tool), 1)
        self.assertEqual(tool[0]["status"], "failed")
        self.assertIsNotNone(tool[0]["end"])
        self.assertIn("error: Exit code 1", tool[0]["args"])
        self.assertTrue(tool[0]["args"].startswith("npm test"))
        self.assertLessEqual(len(tool[0]["args"]), 160)

    def test_tool_failure_without_a_pre_event_still_gets_a_failed_span(self):
        self.start()
        hook("PostToolUseFailure", self.store, self.cfg)
        tool = [s for s in spans.for_run(self.store, self.aid) if s["kind"] == "tool"]
        self.assertEqual([t["status"] for t in tool], ["failed"])

    def test_tool_failure_leaves_a_waiting_row_alone(self):
        self.start()
        hook("PermissionRequest", self.store, self.cfg)
        hook("PostToolUseFailure", self.store, self.cfg)
        self.assertEqual(self.row()["status"], "waiting")

    def test_stop_failure_is_the_failure_reason(self):
        self.start()
        hook("StopFailure", self.store, self.cfg)
        r = self.row()
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["activity"], "API error (rate_limit: 429 Too Many Requests)")
        failed = [s for s in spans.for_run(self.store, self.aid) if s["tool"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("rate_limit", failed[0]["args"])
        turn = [s for s in spans.for_run(self.store, self.aid) if s["kind"] == "turn"]
        self.assertEqual(turn[0]["status"], "failed")

    def test_stop_failure_of_a_print_run_only_notes_the_activity(self):
        self.store.upsert("run-p2", source="launcher", tool="claude", status="running", activity="working",
                          workspace="/x", task="t")
        handle_hook("StopFailure", {"jarvieeTool": "claude", "jarvieeRunId": "run-p2", "session_id": SID,
                                    **PAYLOADS["StopFailure"]}, self.cfg, self.store)
        r = self.store.get("run-p2")
        self.assertEqual(r["status"], "running")      # the exit code decides; runjob reads the log
        self.assertIn("rate_limit", r["activity"])

    def test_task_completed_sets_the_activity_only(self):
        self.start()
        hook("TaskCompleted", self.store, self.cfg)
        r = self.row()
        self.assertEqual((r["status"], r["activity"]), ("running", "task done: Implement auth"))

    def test_subagent_events_make_rows_exact(self):
        self.start()
        hook("SubagentStart", self.store, self.cfg)
        parent = self.row()
        with patch.object(usage, "CLAUDE_PROJECTS", Path(self.tmp) / "none"):
            subagents._cache.clear(); subagents._dirs.clear()
            rows = subagents.for_session(parent)
            self.assertEqual([(r["id"], r["status"], r["agent_type"]) for r in rows], [("sub-abc123", "running", "Explore")])
            hook("SubagentStop", self.store, self.cfg)
            rows = subagents.for_session(self.row())
            self.assertEqual([(r["id"], r["status"], r["activity"]) for r in rows], [("sub-abc123", "done", "finished")])

    def test_events_override_the_transcript_mtime_guess(self):
        projects = Path(self.tmp) / "projects"
        folder = projects / "-Users-alex" / SID / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "Scan"}))
        jl = folder / "agent-abc123.jsonl"
        jl.write_text('{"timestamp": "2026-11-11T03:00:00Z"}\n')
        old = time.time() - 3600
        os.utime(jl, (old, old))
        self.start()
        with patch.object(usage, "CLAUDE_PROJECTS", projects):
            subagents._cache.clear(); subagents._dirs.clear()
            self.assertEqual(subagents.for_session(self.row())[0]["status"], "stopped")   # fallback: stale transcript
            hook("SubagentStart", self.store, self.cfg)
            rows = subagents.for_session(self.row())
            self.assertEqual((len(rows), rows[0]["status"]), (1, "running"))               # the event wins, no duplicate row
            hook("SubagentStop", self.store, self.cfg)
            self.assertEqual(subagents.for_session(self.row())[0]["status"], "done")

    def test_unfinished_subagent_stops_with_its_session(self):
        self.start()
        hook("SubagentStart", self.store, self.cfg)
        self.store.upsert(self.aid, status="done")
        with patch.object(usage, "CLAUDE_PROJECTS", Path(self.tmp) / "none"):
            subagents._cache.clear(); subagents._dirs.clear()
            self.assertEqual(subagents.for_session(self.row())[0]["status"], "stopped")

    def test_internal_agent_without_type_makes_no_row(self):
        self.start()
        hook("SubagentStop", self.store, self.cfg, extra={"agent_id": "zzzz9999", "agent_type": ""})
        with patch.object(usage, "CLAUDE_PROJECTS", Path(self.tmp) / "none"):
            subagents._cache.clear(); subagents._dirs.clear()
            self.assertEqual(subagents.for_session(self.row()), [])


RESULT_OK = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200, "num_turns": 3,
             "result": "Implemented it. Tests pass.\nVERDICT: PASS", "session_id": SID, "total_cost_usd": 0.0421,
             "usage": {}, "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 100, "outputTokens": 50,
                                                               "cacheReadInputTokens": 10,
                                                               "cacheCreationInputTokens": 5, "costUSD": 0.0421}},
             "permission_denials": []}


class JsonParser(unittest.TestCase):
    def test_success_object(self):
        res = claudejson.parse(json.dumps(RESULT_OK))
        self.assertEqual(res["num_turns"], 3)
        self.assertEqual(claudejson.verdict(res), (outcome.DONE, "finished"))
        self.assertEqual(claudejson.result_text(res), RESULT_OK["result"])

    def test_header_stderr_and_trailing_noise(self):
        log = "$ claude -p x\n[launcher] note\n" + json.dumps(RESULT_OK) + "\nwarning: something on stderr\n"
        res, start = claudejson.find(log)
        self.assertEqual(res["session_id"], SID)
        self.assertTrue(log[:start].endswith("[launcher] note\n"))

    def test_array_form_takes_the_result_element(self):
        arr = [{"type": "system", "subtype": "init"}, {"type": "assistant"}, RESULT_OK]
        self.assertEqual(claudejson.parse(json.dumps(arr))["total_cost_usd"], 0.0421)

    def test_error_results_fail(self):
        for sub in ("error_max_turns", "error_during_execution", "error_max_budget_usd"):
            res = dict(RESULT_OK, subtype=sub, is_error=True, errors=["boom happened"])
            res.pop("result")
            kind, why = claudejson.verdict(claudejson.parse(json.dumps(res)))
            self.assertEqual(kind, outcome.FAILED)
            self.assertIn(sub, why)
        kind, why = claudejson.verdict(dict(RESULT_OK, is_error=True, result="Invalid API key"))
        self.assertEqual(kind, outcome.FAILED)
        self.assertIn("Invalid API key", why)

    def test_permission_denials_need_you(self):
        res = dict(RESULT_OK, permission_denials=[{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "x"}}])
        kind, why = claudejson.verdict(res)
        self.assertEqual(kind, outcome.NEEDS_YOU)
        self.assertIn("Bash", why)

    def test_truncated_and_non_json_give_nothing(self):
        text = json.dumps(RESULT_OK)
        for bad in (text[:len(text) // 2], "", "plain prose\nDone.", "[pane] x\n{not json}", '{"type": "assistant"}',
                    '[1, 2, 3]', "null", None):
            self.assertIsNone(claudejson.parse(bad), repr(bad)[:40])

    def test_enabled_defaults_off(self):
        self.assertFalse(claudejson.enabled({}))
        self.assertFalse(claudejson.enabled({"tools": {"claude": {}}}))
        self.assertTrue(claudejson.enabled({"tools": {"claude": {"json_output": True}}}))
        self.assertFalse(claudejson.enabled({"tools": {"claude": {"json_output": True}}}, "agy"))


class JsonFlagAndLog(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)

    def tearDown(self):
        self._t.__exit__()

    def on(self):
        self.cfg["tools"]["claude"]["json_output"] = True

    def test_flag_only_for_headless_claude_when_on(self):
        argv = ["claude", "-p", "hi", "--model", "sonnet"]
        self.assertEqual(runplan.headless_json_argv(argv, "claude", self.cfg), argv)             # off by default
        self.on()
        self.assertEqual(runplan.headless_json_argv(argv, "claude", self.cfg), argv + ["--output-format", "json"])
        self.assertEqual(runplan.headless_json_argv(argv, "agy", self.cfg), argv)                 # agy never
        live = ["claude", "hi", "--model", "sonnet"]
        self.assertEqual(runplan.headless_json_argv(live, "claude", self.cfg), live)              # no -p: a live session
        fixed = argv + ["--output-format", "stream-json"]
        self.assertEqual(runplan.headless_json_argv(fixed, "claude", self.cfg), fixed)            # never override a choice
        self.assertEqual(argv, ["claude", "-p", "hi", "--model", "sonnet"])                       # input not mutated

    def test_settle_rewrites_log_to_prose_and_saves_the_result(self):
        log = Path(self.tmp) / "run.log"
        log.write_text("# header\n" + json.dumps(RESULT_OK) + "\n", encoding="utf-8")
        res = claudejson.settle(log, self.cfg)
        self.assertEqual(res["session_id"], SID)
        text = log.read_text(encoding="utf-8")
        self.assertEqual(text, "# header\nImplemented it. Tests pass.\nVERDICT: PASS\n")
        from hub.pipeline import parse_verdict
        self.assertEqual(parse_verdict(text)[0].lower(), "pass")
        saved = claudejson.load_exact(self.cfg, SID)
        self.assertEqual(saved["total_cost_usd"], 0.0421)
        self.assertNotIn("result", saved)

    def test_settle_leaves_prose_and_truncated_logs_alone(self):
        log = Path(self.tmp) / "run.log"
        for body in ("plain text\nfinished\n", json.dumps(RESULT_OK)[:50]):
            log.write_text(body, encoding="utf-8")
            self.assertIsNone(claudejson.settle(log, self.cfg))
            self.assertEqual(log.read_text(encoding="utf-8"), body)
        self.assertIsNone(claudejson.settle(Path(self.tmp) / "missing.log", self.cfg))

    def test_exact_cost_replaces_the_estimate(self):
        projects = Path(self.tmp) / "projects"
        f = projects / "-x" / f"{SID}.jsonl"
        f.parent.mkdir(parents=True)
        line = {"type": "assistant", "requestId": "r1", "timestamp": "2026-11-12T03:00:00Z", "message": {"id": "m1", "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0}}}
        f.write_text(json.dumps(line) + "\n")
        runcost._cache.clear()
        with patch.object(usage, "CLAUDE_PROJECTS", projects):
            est = runcost.run_usage(self.cfg, "claude", {SID})
            self.assertEqual(est["source"], "claude transcript")
            log = Path(self.tmp) / "r.log"
            log.write_text(json.dumps(RESULT_OK), encoding="utf-8")
            claudejson.settle(log, self.cfg)
            ex = runcost.run_usage(self.cfg, "claude", {SID})
            self.assertEqual((ex["cost"], ex["source"]), (0.0421, "claude result (exact)"))
            self.assertEqual(ex["tokens"], est["tokens"])                      # tokens still come from the transcript
            both = runcost.run_usage(self.cfg, "claude", {SID, "other-session-1"})
            self.assertEqual(both["source"], "claude transcript")               # a session without a result: estimate
        runcost._cache.clear()

    def test_exact_cost_without_a_transcript_uses_model_usage(self):
        log = Path(self.tmp) / "r.log"
        log.write_text(json.dumps(RESULT_OK), encoding="utf-8")
        claudejson.settle(log, self.cfg)
        with patch.object(usage, "CLAUDE_PROJECTS", Path(self.tmp) / "none"):
            ex = runcost.run_usage(self.cfg, "claude", {SID})
        self.assertEqual(ex["cost"], 0.0421)
        self.assertEqual(ex["tokens"], 165)


FAKE_CLAUDE = r"""
import json, sys
body = json.loads(sys.argv[1])
if "--output-format" in sys.argv:
    print(json.dumps(body["json"]))
else:
    print(body["prose"])
sys.exit(body.get("exit", 0))
"""


class JsonRunEndToEnd(unittest.TestCase):
    """A real launcher run with a fake `claude -p` that prints the JSON result when asked."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["tools"]["claude"]["command"] = [sys.executable, "-c", FAKE_CLAUDE, "{prompt}", "-p"]
        self.cfg["retry"] = {"enabled": False}
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        self._t.__exit__()

    def run_it(self, json_result, prose="plain prose", code=0, on=True):
        self.cfg["tools"]["claude"]["json_output"] = on
        body = json.dumps({"json": json_result, "prose": prose, "exit": code})
        rid, _ = launcher.launch(body, self.cfg, self.store, tool="claude", tier="basic", cwd=self.tmp)
        for _ in range(100):
            time.sleep(0.1)
            r = self.store.get(rid)
            if r and r["status"] not in ("running", "queued"):
                break
        return self.store.get(rid), Path(self.cfg["logs_dir"]) / f"{rid}.log"

    def test_success_is_done_and_the_log_is_prose(self):
        r, log = self.run_it(RESULT_OK)
        self.assertEqual(r["status"], "done")
        text = log.read_text(encoding="utf-8")
        self.assertIn("VERDICT: PASS", text)
        self.assertNotIn('"total_cost_usd"', text)

    def test_error_result_on_exit_zero_fails(self):
        r, _ = self.run_it(dict(RESULT_OK, subtype="error_max_turns", is_error=True, errors=["Reached max turns"]))
        self.assertEqual(r["status"], "failed")
        self.assertIn("turn limit", r["activity"])   # a ceiling stop: the run's own limit, no retry

    def test_denials_need_you_even_when_the_text_sounds_finished(self):
        den = [{"tool_name": "Bash", "tool_use_id": "t", "tool_input": {"command": "rm x"}}]
        r, _ = self.run_it(dict(RESULT_OK, permission_denials=den))
        self.assertEqual(r["status"], "waiting")
        self.assertIn("denied", r["activity"])

    def test_plain_success_still_gets_phrase_matching(self):
        r, _ = self.run_it(dict(RESULT_OK, result="I can't proceed without the API key. Please log in."))
        self.assertEqual(r["status"], "waiting")

    def test_unparseable_output_falls_back_to_phrases(self):
        r, log = self.run_it("not an object", prose="x")
        self.assertEqual(r["status"], "done")

    def test_switch_off_keeps_prose_and_flag_out(self):
        r, log = self.run_it(RESULT_OK, prose="all done, tests pass", on=False)
        self.assertEqual(r["status"], "done")
        self.assertIn("all done, tests pass", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
