"""Claude Code subagents: read from the parent session's transcript folder, shown in state and in its workflow;
injected prompts (subagent hand-backs, notifications) never replace a session's title."""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from helpers import TempDir, make_cfg
from hub import auth, subagents, usage
from hub.server import handle_hook, make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
SID = "11111111-2222-3333-4444-555555555555"


def write_sub(folder, aid, meta, age=0, lines=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"agent-{aid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    jl = folder / f"agent-{aid}.jsonl"
    rows = lines or [{"timestamp": "2026-11-11T03:00:00Z", "type": "user"}]
    jl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    t = time.time() - age
    os.utime(jl, (t, t))


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.cfg, _ = make_cfg(str(self.tmp))
        self.projects = self.tmp / "claude-projects"
        self.sub_dir = self.projects / "-Users-alex-proj" / SID / "subagents"
        write_sub(self.sub_dir, "aaaa1111", {"agentType": "general-purpose", "description": "Map Alpha project", "model": "sonnet", "finished": True})
        tool_line = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Grep", "input": {}}]}}
        write_sub(self.sub_dir, "bbbb2222", {"agentType": "Explore", "description": "Find the auth code"},
                  lines=[{"timestamp": "2026-11-11T03:01:00Z"}, tool_line])
        write_sub(self.sub_dir, "cccc3333", {"agentType": "coder", "description": "Old work", "finished": False}, age=3600)
        (self.sub_dir / "notes.txt").write_text("ignored", encoding="utf-8")
        self.patcher = patch.object(usage, "CLAUDE_PROJECTS", self.projects)
        self.patcher.start()
        subagents._cache.clear(); subagents._dirs.clear(); subagents._parent.clear()
        self.store = Store(self.cfg["db_path"])
        handle_hook("UserPromptSubmit", {"jarvieeTool": "claude", "session_id": SID, "prompt": "Map every project",
                                         "cwd": "/Users/alex/proj", "model": "claude-opus-5-5"}, self.cfg, self.store)

    def tearDown(self):
        self.patcher.stop()
        self._t.__exit__()

    def test_rows_from_the_transcript_folder(self):
        parent = self.store.get(f"claude-{SID}")
        rows = {r["id"]: r for r in subagents.for_session(parent, lambda m: "light" if m == "sonnet" else "")}
        self.assertEqual(set(rows), {"sub-aaaa1111", "sub-bbbb2222", "sub-cccc3333"})
        done, live, old = rows["sub-aaaa1111"], rows["sub-bbbb2222"], rows["sub-cccc3333"]
        self.assertEqual((done["status"], done["title"], done["model"], done["tier"]), ("done", "Map Alpha project", "sonnet", "light"))
        self.assertEqual((live["status"], live["activity"]), ("running", "using Grep"))
        self.assertEqual(live["model"], parent["model"])                  # no model in meta: inherits the parent's
        self.assertEqual(old["status"], "stopped")                        # unfinished and silent for an hour
        self.assertEqual(done["parent"], f"claude-{SID}")
        self.assertEqual(subagents.parent_of("sub-bbbb2222"), f"claude-{SID}")
        self.assertEqual(subagents.parent_of("sub-zzzz9999"), "")
        self.assertEqual(subagents.for_session({"id": "run-abc", "source": "launcher"}), [])

    def test_injected_prompts_keep_the_title(self):
        before = self.store.get(f"claude-{SID}")["title"]
        for injected in ("[Subagent hand-back] The text below is the final report of a subagent",
                         "<task-notification><task-id>x</task-id></task-notification>",
                         "Another Claude session sent a message: hi"):
            handle_hook("UserPromptSubmit", {"jarvieeTool": "claude", "session_id": SID, "prompt": injected}, self.cfg, self.store)
        row = self.store.get(f"claude-{SID}")
        self.assertEqual(row["title"], before)
        self.assertEqual(row["task"], "Map every project")
        handle_hook("UserPromptSubmit", {"jarvieeTool": "claude", "session_id": SID, "prompt": "Now add a right-click menu"}, self.cfg, self.store)
        self.assertEqual(self.store.get(f"claude-{SID}")["task"], "Now add a right-click menu")

    def test_state_workflow_and_focus_over_http(self):
        srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base, tok = f"http://127.0.0.1:{self.cfg['port']}", auth.get_token(self.cfg)

        def req(path, body=None):
            r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                       headers={"Content-Type": "application/json", "X-Jarviee-Token": tok})
            try:
                with _OPEN(r, timeout=3) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read() or b"{}")
        try:
            _, state = req("/api/state")
            subs = [a for a in state["agents"] if a.get("source") == "subagent"]
            self.assertEqual(len(subs), 3)
            self.assertTrue(all(s["stoppable"] is False for s in subs))
            _, tree = req(f"/api/run/claude-{SID}/tree")
            names = [n["name"] for n in tree["nodes"]]
            self.assertIn("Claude · main session", names)
            self.assertIn("Subagent · Explore", names)
            self.assertEqual(tree["summary"]["total"], 4)
            code, focused = req("/api/run/sub-bbbb2222/tree")            # a subagent opens its parent's workflow
            self.assertEqual((code, focused["focus"], focused["id"]), (200, "sub-bbbb2222", f"claude-{SID}"))
            with patch("hub.server.focus.focus", return_value=(True, "shown")) as fo:
                code, body = req("/api/focus", {"id": "sub-bbbb2222"})
            self.assertEqual((code, fo.call_args[0][0]["id"]), (200, f"claude-{SID}"))
        finally:
            srv.shutdown(); srv.server_close()


class EventsThreadSafety(unittest.TestCase):
    def test_reading_rows_while_hook_threads_add_events_never_raises(self):
        parent = f"claude-{SID}"
        row = {"id": parent, "source": "hook", "status": "running"}
        errors, stop = [], threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                i += 1
                subagents.note_event(parent, "SubagentStart", {"agent_id": f"w{i % 400:04d}", "agent_type": "Explore"})

        with TempDir() as tmp, patch.object(usage, "CLAUDE_PROJECTS", Path(tmp) / "none"):
            subagents._events.clear()
            t = threading.Thread(target=writer)
            t.start()
            try:
                for _ in range(400):
                    subagents._cache.clear()
                    try:
                        subagents.for_session(row)
                    except RuntimeError as e:   # dictionary changed size during iteration
                        errors.append(e)
            finally:
                stop.set()
                t.join()
            subagents._events.clear()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
