import json
import tempfile
import time
import unittest
from pathlib import Path

from hub import runcost, rundemo, runs_api, runtrace, runtree, runview, spans
from hub.store import Store


class SpansTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = Store(self.tmp.name)
        spans.ensure(self.store)

    def tearDown(self):
        self.tmp.close()

    def test_ops_nesting_and_unclosed(self):
        t0 = 1000.0
        open_spans = []

        # Turn starts
        ops1 = spans.ops_for("UserPromptSubmit", {"prompt": "do something"}, open_spans, t0)
        self.assertEqual(len(ops1), 1)
        self.assertEqual(ops1[0][0], "open")
        turn_span = ops1[0][1]
        self.assertEqual(turn_span["kind"], "turn")
        open_spans.append(turn_span)

        # PreToolUse
        t1 = 1002.0
        ops2 = spans.ops_for("PreToolUse", {"tool_name": "bash", "tool_input": {"command": "ls -l"}, "tool_use_id": "call-1"}, open_spans, t1)
        self.assertEqual(len(ops2), 1)
        self.assertEqual(ops2[0][0], "open")
        tool_span = ops2[0][1]
        self.assertEqual(tool_span["parent"], turn_span["id"])
        self.assertEqual(tool_span["call_id"], "call-1")
        open_spans.append(tool_span)

        # PostToolUse closes tool
        t2 = 1005.0
        ops3 = spans.ops_for("PostToolUse", {"tool_name": "bash", "tool_use_id": "call-1"}, open_spans, t2)
        self.assertEqual(len(ops3), 1)
        self.assertEqual(ops3[0][0], "close")
        self.assertEqual(ops3[0][1], tool_span["id"])
        self.assertEqual(ops3[0][2], t2)
        self.assertEqual(ops3[0][3], "done")
        open_spans.remove(tool_span)

        # Notification
        t3 = 1006.0
        ops4 = spans.ops_for("Notification", {"notification_type": "ask_user", "message": "need input"}, open_spans, t3)
        self.assertEqual(len(ops4), 1)
        self.assertEqual(ops4[0][1]["status"], "needs_you")

        # Turn end closes turn
        t4 = 1010.0
        ops5 = spans.ops_for("Stop", {}, open_spans, t4)
        self.assertTrue(any(op[0] == "close" and op[1] == turn_span["id"] for op in ops5))

    def test_record_into_store(self):
        spans.record(self.store, "run-123", "UserPromptSubmit", {"prompt": "hello"})
        records = spans.for_run(self.store, "run-123")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "turn")


class CostAndUsageTests(unittest.TestCase):
    def test_cost_calculation_from_pricing(self):
        cfg = {
            "pricing": {
                "models": [
                    {"match": "sonnet", "input": 3.0, "output": 15.0, "cache_read": 0.3, "tool": "claude"}
                ]
            }
        }
        tokens = {"input": 10000, "output": 2000, "cache_read": 50000}
        c = runcost.cost(cfg, "claude", "claude-sonnet-4-6", tokens)
        # 10k * 3 / 1e6 = 0.03
        # 2k * 15 / 1e6 = 0.03
        # 50k * 0.3 / 1e6 = 0.015
        # Total = 0.075
        self.assertAlmostEqual(c, 0.075, places=4)

    def test_claude_jsonl_parsing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f:
            line1 = json.dumps({
                "type": "assistant",
                "timestamp": "2026-11-09T06:00:00.000Z",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 500, "output_tokens": 100, "cache_read_input_tokens": 2000}
                },
                "requestId": "req_1"
            })
            f.write(line1 + "\n")
            f.flush()

            entries = runcost._entries(Path(f.name), runcost.usage._claude_line)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["input"], 500)
            self.assertEqual(entries[0]["output"], 100)


class TreeLayoutTests(unittest.TestCase):
    def test_deterministic_layout_no_overlaps(self):
        now = 1700000000.0
        data = rundemo.data(now)
        v = runview.build(data, now)

        self.assertIn("nodes", v)
        self.assertIn("edges", v)
        self.assertIn("columns", v)
        self.assertTrue(len(v["nodes"]) >= 5)

        # Check no card overlaps
        nodes = v["nodes"]
        for i in range(len(nodes)):
            n1 = nodes[i]
            r1 = (n1["x"], n1["y"], n1["x"] + n1["w"], n1["y"] + n1["h"])
            for j in range(i + 1, len(nodes)):
                n2 = nodes[j]
                r2 = (n2["x"], n2["y"], n2["x"] + n2["w"], n2["y"] + n2["h"])
                # Check bounding box collision
                intersect = not (r1[2] <= r2[0] or r1[0] >= r2[2] or r1[3] <= r2[1] or r1[1] >= r2[3])
                self.assertFalse(intersect, f"Nodes {n1['id']} and {n2['id']} overlap: {r1} vs {r2}")

    def test_cards_carry_the_brief_and_live_activity(self):
        """The side panel reads the planner's polished brief (Tasks) and what the agent is doing (Context)."""
        now = 1700000000.0
        v = runview.build(rundemo.data(now), now)
        coder = next(n for n in v["nodes"] if n["id"] == "demo-plan.t5")
        self.assertTrue(coder["prompt"].startswith("Lay out the run tree"))
        self.assertIn("node --check", coder["acceptance"])
        self.assertEqual(coder["depends_on"], ["demo-plan.t4"])
        self.assertIn("workflows.js", coder["activity"])
        self.assertIn("hub/static/workflows.js", coder["files"])
        orch = v["nodes"][0]
        self.assertEqual(orch["role"], "orchestrator")
        self.assertEqual(orch["prompt"], v["title"])

    def test_demo_clock_moves_so_running_cards_tick(self):
        a, b = rundemo.data(1700000000.0), rundemo.data(1700000030.0)
        self.assertEqual(a["runs"]["demo-r5"]["started"], b["runs"]["demo-r5"]["started"])
        self.assertGreater(b["usage"]["demo-r5"]["tokens"], a["usage"]["demo-r5"]["tokens"])

    def test_trace_generation(self):
        now = 1700000000.0
        data = rundemo.data(now)
        tree = runview.build(data, now)
        tr = runtrace.trace(data, tree, now)
        self.assertIn("rows", tr)
        self.assertTrue(len(tr["rows"]) > 0)


class RunsApiSecurityTests(unittest.TestCase):
    def test_security_bad_run_ids(self):
        self.assertIsNone(runs_api.get("/api/run/../../etc/passwd/tree", "", {}, None, None, None))
        status, payload = runs_api.get("/api/run/invalid;id/tree", "", {}, None, None, None)
        self.assertEqual(status, 400)

    def test_static_security_whitelist(self):
        self.assertEqual(runs_api.static("../server.py")[0], 404)
        self.assertEqual(runs_api.static("workflows.js")[0], 200)
        self.assertEqual(runs_api.static("graph.css")[0], 200)
        self.assertEqual(runs_api.static("Core.JS")[0], 404)

    def test_demo_endpoint(self):
        status, payload = runs_api.get("/api/run/demo/tree", "", {}, None, None, None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], "demo")

    def test_plan_task_of_handles_list_and_str_reasons(self):
        self.assertEqual(runs_api._plan_task_of({"reasons": ["plan task t-1", "other"]}), "t-1")
        self.assertEqual(runs_api._plan_task_of({"reasons": '["plan task t-2"]'}), "t-2")
        self.assertEqual(runs_api._plan_task_of({"reasons": None}), "")
        self.assertEqual(runs_api._plan_task_of({}), "")
        self.assertEqual(runs_api._plan_task_of({"reasons": "invalid json"}), "")

    def test_single_agent_run_api_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            st = Store(Path(td) / "test.db")
            st.upsert("claude-sess1", tool="claude", status="running", reasons=["plan task t-abc"])
            status, payload = runs_api.get("/api/run/claude-sess1/tree", "", {}, st, None, None)
            self.assertEqual(status, 200)
            self.assertIn("nodes", payload)


if __name__ == "__main__":
    unittest.main()


class PipelineViewTests(unittest.TestCase):
    def test_phase_of_prefers_task_phase(self):
        for ph in ("research", "build", "test", "review", "fix"):
            self.assertEqual(runtree.phase_of({"title": "Review things", "prompt": "", "phase": ph}), ph)
        self.assertEqual(runtree.phase_of({"title": "Review the diff", "prompt": "", "phase": ""}), "review")
        self.assertEqual(runtree.phase_of({"title": "Write code", "prompt": ""}), "build")

    def _data(self, pipeline):
        tasks = [{"id": "plan-aaaa.research", "title": "Research", "prompt": "", "phase": "research", "status": "done"},
                 {"id": "plan-aaaa.t1", "title": "Implement", "prompt": "", "phase": "build", "status": "done"},
                 {"id": "plan-aaaa.test-0", "title": "Test", "prompt": "", "phase": "test", "status": "queued"}]
        return {"kind": "plan", "id": "plan-aaaa", "title": "g", "status": "running", "created": 1000.0, "tasks": tasks,
                "runs": {}, "attempts": {}, "pipeline": pipeline, "review_enabled": True}

    def test_pipeline_columns_and_no_synthetic_review(self):
        pl = {"phase": "test", "round": 0, "max_rounds": 2, "rounds": [], "note": ""}
        tree = runview.build(self._data(pl), 2000.0)
        phases = [c["phase"] for c in tree["columns"]]
        self.assertEqual(phases, ["research", "plan", "build", "test", "brain"])   # the order the pipeline runs
        self.assertEqual(tree["pipeline"], pl)
        plain = runview.build(self._data(None), 2000.0)
        self.assertIn("review", [c["phase"] for c in plain["columns"]])

    def test_plan_card_follows_pipeline_phase(self):
        d = self._data({"phase": "planning"})
        d["tasks"] = d["tasks"][:1]
        tree = runview.build(d, 2000.0)
        card = next(n for n in tree["nodes"] if n["id"] == "hub-plan")
        self.assertEqual(card["status"], "running")

    def test_planner_card_is_a_hub_card_in_plan_column(self):
        tree = runview.build(self._data({"phase": "build", "round": 0, "max_rounds": 2, "rounds": []}), 2000.0)
        card = next(n for n in tree["nodes"] if n["id"] == "hub-plan")
        self.assertEqual((card["phase"], card["kind"], card["status"]), ("plan", "hub", "done"))
        self.assertIn("x", card)
        self.assertNotIn("fix", [c["phase"] for c in tree["columns"]])
        self.assertEqual(card["summary"], "Split the goal into 1 task")   # build tasks only, not research/test

    def test_ended_plan_is_never_live(self):
        """A failed or cancelled plan showed 'running' for days: its Brain sync / Planner / Review cards stayed running."""
        for status in ("failed", "cancelled"):
            for pipe in ({"phase": "planning"}, None):
                with self.subTest(status=status, pipeline=bool(pipe)):
                    d = self._data(pipe)
                    d["status"] = status
                    d["tasks"] = [dict(t, status="done") for t in d["tasks"]]
                    tree = runview.build(d, 9e9)
                    self.assertFalse(tree["live"])
                    hub = [n for n in tree["nodes"] if n.get("kind") == "hub"]
                    self.assertTrue(hub)
                    self.assertEqual([n["status"] for n in hub if n["status"] in ("running", "needs_you")], [])
        running = runview.build(self._data(None), 2000.0)   # a running plan still shows its pending brain sync as queued
        self.assertEqual(next(n for n in running["nodes"] if n["id"] == "hub-brain")["status"], "queued")

    def test_planner_card_fails_when_planning_failed_after_research(self):
        d = self._data({"phase": "failed", "note": "planning failed: boom"})
        d["tasks"] = d["tasks"][:1]   # only the research task exists
        card = next(n for n in runview.build(d, 2000.0)["nodes"] if n["id"] == "hub-plan")
        self.assertEqual(card["status"], "failed")
