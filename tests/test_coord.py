"""Agent coordination: claims, the message channel, wait / split / go, and releasing claims."""
import json
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import coord, launcher
from hub.server import handle_hook, hook_reply
from hub.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["retry"] = {"enabled": False}
        self.store = Store(self.cfg["db_path"])
        self.ws = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha"
        (self.ws / "hub").mkdir()
        (self.ws / "hub" / "server.py").write_text("x\n")
        (self.ws / "hub" / "store.py").write_text("x\n")
        (self.ws / "README.md").write_text("x\n")

    def tearDown(self):
        self._t.__exit__()

    def live(self, aid, **kw):
        self.store.upsert(aid, status="running", workspace=str(self.ws), **kw)


class ClaimsAndDecisions(Base):
    def test_files_named_in_a_task_are_found(self):
        self.assertEqual(coord.files_in("fix hub/server.py and hub/ then README.md, not nope/x.py", self.ws),
                         ["hub/server.py", "hub/", "README.md"])

    def test_a_prompt_that_lists_the_repo_claims_nothing_up_front(self):
        many = " ".join(f"hub/f{i}.py" for i in range(12))
        for i in range(12):
            (self.ws / "hub" / f"f{i}.py").write_text("x")
        self.assertEqual(coord.files_in("You are the planner. Files: " + many, self.ws), [])

    def test_go_split_wait(self):
        self.live("a")
        a = coord.register(self.store, "a", self.ws, "fix the login form validation", ["hub/server.py"])
        self.assertEqual(a["decision"], "go")
        self.live("b")
        b = coord.register(self.store, "b", self.ws, "edit the store", ["hub/server.py"])
        self.assertEqual((b["decision"], [c["agent_id"] for c in b["with"]]), ("wait", ["a"]))
        self.assertIn("shares hub/server.py", b["note"])
        self.live("c")
        c = coord.register(self.store, "c", self.ws, "write unit tests for the login form", ["hub/store.py"])
        self.assertEqual(c["decision"], "split")
        self.assertIn("hub/server.py", c["note"])                    # told which files the other claimed
        self.live("d")
        d = coord.register(self.store, "d", self.ws, "fix the login form validation please", [])
        self.assertEqual(d["decision"], "wait")                      # nearly the same task: hand-off
        self.assertIn("nearly the same task", d["note"])

    def test_folder_claim_covers_files_and_other_workspaces_dont_count(self):
        self.live("a")
        coord.register(self.store, "a", self.ws, "restyle", ["hub/"])
        self.live("b")
        self.assertEqual(coord.register(self.store, "b", self.ws, "x", ["hub/store.py"])["decision"], "wait")
        self.live("c")
        self.assertEqual(coord.register(self.store, "c", self.tmp, "x", ["hub/store.py"])["decision"], "go")

    def test_release_and_sweep(self):
        self.live("a")
        coord.register(self.store, "a", self.ws, "t", ["README.md"])
        self.assertEqual(coord.still_blocking(self.store, ["a"]), ["a"])
        self.assertTrue(coord.release(self.store, "a", "done", "updated the readme"))
        self.assertFalse(coord.release(self.store, "a", "done"))     # idempotent
        self.assertEqual(coord.still_blocking(self.store, ["a"]), [])
        self.assertIn("updated the readme", coord.handoff_note(self.store, ["a"]))
        self.live("gone")
        coord.register(self.store, "gone", self.ws, "t", ["hub/store.py"])
        self.store.upsert("gone", status="failed")                    # died without releasing
        self.assertEqual(coord.sweep(self.store), 1)
        self.assertEqual(coord.active(self.store), [])


class Channel(Base):
    def test_claim_change_finish_and_inboxes(self):
        self.live("a")
        coord.register(self.store, "a", self.ws, "task a", ["README.md"])
        self.live("b")
        coord.register(self.store, "b", self.ws, "task b", ["hub/store.py"])
        coord.touch(self.store, "b", str(self.ws / "hub" / "store.py"), self.ws)   # already claimed: no news
        coord.touch(self.store, "b", str(self.ws / "hub" / "server.py"), self.ws)
        self.assertIn("hub/server.py", coord.get(self.store, "b")["files"])
        coord.release(self.store, "b", "done")
        kinds = [m["kind"] for m in coord.messages(self.store, self.ws)]
        self.assertEqual(kinds, ["claim", "claim", "change", "finish"])
        a_box = [m["body"] for m in coord.inbox(self.store, "a", self.ws)]
        self.assertTrue(any("changing hub/server.py" in b for b in a_box))
        self.assertTrue(any(b.startswith("b done") for b in a_box))
        self.assertEqual(coord.inbox(self.store, "a", self.ws), [])  # read once
        self.assertFalse(any(m["sender"] == "b" for m in coord.inbox(self.store, "b", self.ws)))

    def test_private_note_reaches_only_its_recipient(self):
        coord.register(self.store, "a", self.ws, "t", [])
        coord.register(self.store, "b", self.ws, "t2", [])
        coord.inbox(self.store, "a", self.ws)
        coord.inbox(self.store, "b", self.ws)
        coord.post(self.store, "jarviee", "note", "only for b", self.ws, recipient="b")
        self.assertEqual(coord.inbox(self.store, "a", self.ws), [])
        self.assertEqual([m["body"] for m in coord.inbox(self.store, "b", self.ws)], ["only for b"])


class Hooks(Base):
    def hook(self, event, sid, **data):
        body = {"session_id": sid, "jarvieeTool": "claude", "cwd": str(self.ws), **data}
        return hook_reply(event, body, handle_hook(event, body, self.cfg, self.store), self.store)

    def test_terminal_sessions_register_announce_edits_get_messages_and_release(self):
        a = self.hook("UserPromptSubmit", "s1", prompt="refactor hub/server.py")["id"]
        self.assertEqual(coord.get(self.store, a)["files"], ["hub/server.py"])
        reply = self.hook("UserPromptSubmit", "s2", prompt="fix a bug in hub/server.py routing")
        b = reply["id"]
        self.assertEqual(self.store.get(b)["status"], "running")      # a session you started is never held
        self.assertIn("Heads-up", reply.get("context", ""))           # ...but it is told, in its prompt's hook reply
        self.assertNotIn("context", self.hook("PostToolUse", "s2", tool_name="Read"))   # once
        self.hook("PostToolUse", "s1", tool_name="Edit", tool_input={"file_path": str(self.ws / "hub" / "store.py")})
        reply = self.hook("PostToolUse", "s2", tool_name="Read")
        self.assertIn("changing hub/store.py", reply["context"])
        self.hook("Stop", "s1")                                        # turn finished: the job's claim is released
        self.assertEqual(coord.get(self.store, a)["status"], "released")
        self.assertIn("replied", self.hook("PostToolUse", "s2", tool_name="Read")["context"])

    def test_agy_never_gets_context(self):
        body = {"conversationId": "c", "jarvieeTool": "agy", "lastUserInput": "x", "cwd": str(self.ws)}
        aid = handle_hook("PreInvocation", body, self.cfg, self.store)
        coord.post(self.store, "other", "note", "hello", self.ws)
        self.assertNotIn("context", hook_reply("PostToolUse", body, aid, self.store))


class ReporterOutput(unittest.TestCase):
    """hooks/report.py prints new messages only as additionalContext: context, never a block or a decision."""

    def out(self, event, tool, reply):
        import contextlib
        import importlib.util
        import io
        from helpers import ROOT
        spec = importlib.util.spec_from_file_location("report_hook", ROOT / "hooks" / "report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.print_reply(event, tool, reply)
        return buf.getvalue()

    def test_claude_gets_context_and_nothing_that_gates(self):
        out = json.loads(self.out("PostToolUse", "claude", {"id": "x", "context": "b is changing a.py"}))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                      "additionalContext": "b is changing a.py"}})
        self.assertEqual(self.out("Stop", "claude", {"context": "x"}), "")      # Stop output could block: never
        self.assertEqual(self.out("PostToolUse", "claude", {}), "")
        self.assertEqual(self.out("PostToolUse", "agy", {"context": "x"}).strip(), "{}")


class LaunchedRuns(Base):
    def wait_for(self, rid, timeout=20):
        launcher.wait(rid, timeout)   # the worker thread is done: claims released, hand-off posted
        end = time.time() + timeout
        while time.time() < end:
            a = self.store.get(rid)
            if a and a["status"] in ("done", "failed", "stopped"):
                return a
            time.sleep(0.05)
        self.fail(f"{rid} didn't finish")

    def test_overlapping_run_waits_then_starts_with_the_hand_off(self):
        first, _ = launcher.launch("update README.md FAKE_SLEEP=1.5 FAKE_SAY=\"wrote the intro\"", self.cfg,
                                   self.store, tool="agy", tier="basic", project="Alpha")
        time.sleep(0.3)
        second, _ = launcher.launch("fix typos in README.md", self.cfg, self.store, tool="agy", tier="basic",
                                    project="Alpha")
        time.sleep(0.3)
        row = self.store.get(second)
        self.assertTrue(row["activity"].startswith(f"waiting for {first}"), row["activity"])
        a = self.wait_for(first)
        b = self.wait_for(second)
        self.assertLess(a["ended"], b["ended"])
        log = Path(b["log_path"]).read_text()
        self.assertIn("Hand-off", log)
        self.assertIn(first, log)
        self.assertEqual(coord.active(self.store), [])                # both claims released

    def test_split_run_starts_at_once_and_is_told_the_other_files(self):
        first, _ = launcher.launch("improve login form validation in hub/server.py FAKE_SLEEP=1", self.cfg,
                                   self.store, tool="agy", tier="basic", project="Alpha")
        time.sleep(0.2)
        second, _ = launcher.launch("improve login form validation messages in hub/store.py", self.cfg,
                                    self.store, tool="agy", tier="basic", project="Alpha")
        time.sleep(0.3)
        self.assertFalse(self.store.get(second)["activity"].startswith("waiting for"))   # runs alongside, not after
        b = self.wait_for(second)
        log = Path(b["log_path"]).read_text()
        self.assertIn("Coordination:", log)
        self.assertIn("hub/server.py", log)
        self.wait_for(first)

    def test_stopping_a_waiting_run_releases_it_and_never_starts_it(self):
        first, _ = launcher.launch("update README.md FAKE_SLEEP=2", self.cfg, self.store, tool="agy",
                                   tier="basic", project="Alpha")
        time.sleep(0.2)
        second, _ = launcher.launch("rewrite README.md", self.cfg, self.store, tool="agy", tier="basic",
                                    project="Alpha")
        time.sleep(0.3)
        self.assertTrue(launcher.stop_with_reason(second, self.store)[0])
        b = self.wait_for(second)
        self.assertEqual(b["status"], "stopped")
        self.assertNotIn("fake agy ran", Path(b["log_path"]).read_text())
        self.assertNotIn(second, [c["agent_id"] for c in coord.active(self.store)])
        self.wait_for(first)


if __name__ == "__main__":
    unittest.main()
