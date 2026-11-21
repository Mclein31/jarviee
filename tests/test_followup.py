import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import followup, launcher
from hub.launcher import build_argv
from hub.server import make_server
from hub.store import Store


class ArgvTests(unittest.TestCase):
    def test_session_and_resume_args(self):
        t = {"command": ["claude", "-p", "{prompt}", "--model", "{model}"],
             "session_args": ["--session-id", "{session}"], "resume_args": ["--resume", "{session}"]}
        self.assertEqual(build_argv(t, "hi", "sonnet", "", session="abc")[-2:], ["--session-id", "abc"])
        self.assertEqual(build_argv(t, "hi", "sonnet", "", session="abc", resume=True)[-2:], ["--resume", "abc"])
        self.assertNotIn("--session-id", build_argv(t, "hi", "sonnet", ""))
        self.assertNotIn("--session-id", build_argv({"command": ["agy", "-p", "{prompt}"]}, "hi", "m", "", session="abc"))


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["tools"]["claude"]["session_args"] = ["--session-id", "{session}"]
        self.cfg["tools"]["claude"]["resume_args"] = ["--resume", "{session}"]
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        self._t.__exit__()

    def run_and_wait(self, task, **kw):
        rid, p = launcher.launch(task, self.cfg, self.store, project="Alpha", **kw)
        launcher.wait(rid, 20)
        return rid, p

    def log(self, rid):
        return Path(self.store.get(rid)["log_path"]).read_text(encoding="utf-8")

    def test_claude_gets_a_session_id_and_reply_resumes_it(self):
        rid, _ = self.run_and_wait("fix typo in readme", tool="claude", tier="light")
        row = self.store.get(rid)
        self.assertTrue(row["session_id"])
        self.assertEqual(row["status"], "done")
        rid2, p, mode = followup.start(row, self.cfg, self.store, reply="also fix the footer")
        launcher.wait(rid2, 20)
        self.assertEqual(mode, "resume")
        row2 = self.store.get(rid2)
        self.assertEqual(row2["session_id"], row["session_id"])
        self.assertEqual(row2["parent_run_id"], rid)
        self.assertEqual((row2["tool"], row2["model"]), (row["tool"], row["model"]))
        # the fake agent echoes its prompt: only the reply, not the whole brief, goes into the conversation
        self.assertIn("also fix the footer", self.log(rid2))
        self.assertNotIn("fix typo in readme", self.log(rid2).split("\n", 3)[-1])

    def test_agy_reply_reruns_with_brief_log_tail_and_reply(self):
        rid, _ = self.run_and_wait("fix typo in readme FAKE_SAY=\"printed marker\"", tool="agy", tier="basic")
        row = self.store.get(rid)
        self.assertFalse(row["session_id"])
        rid2, p, mode = followup.start(row, self.cfg, self.store, reply="use British spelling")
        launcher.wait(rid2, 20)
        self.assertEqual(mode, "reply")
        text = self.log(rid2)
        for needle in ("fix typo in readme", "printed marker", "use British spelling"):
            self.assertIn(needle, text)
        self.assertEqual(self.store.get(rid2)["parent_run_id"], rid)

    def test_run_again_same_brief_other_level_and_tool(self):
        rid, _ = self.run_and_wait("fix typo in readme", tool="agy", tier="basic")
        row = self.store.get(rid)
        rid2, p, mode = followup.start(row, self.cfg, self.store)
        launcher.wait(rid2, 20)
        self.assertEqual((mode, p["tool"], p["tier"]), ("rerun", "agy", "basic"))
        rid3, p3, _ = followup.start(row, self.cfg, self.store, tool="claude", tier="complex")
        launcher.wait(rid3, 20)
        self.assertEqual((p3["tool"], p3["tier"]), ("claude", "complex"))
        self.assertEqual(self.store.get(rid3)["task"], "fix typo in readme")

    def test_omniroute_tool_allowed_and_reply_resumes_session(self):
        fake = ("import sys; print('fake omniroute ran', sys.argv[2][:80])")
        self.cfg["tools"]["omniroute"]["command"] = [sys.executable, "-c", fake, "{model}", "{prompt}"]
        with mock.patch.dict(os.environ, {"OMNIROUTE_API_KEY": "test-followup-key"}):
            rid, _ = self.run_and_wait("fix typo in readme", tool="omniroute", tier="basic")
            row = self.store.get(rid)
            self.assertEqual(row["tool"], "omniroute")
            self.assertTrue(row["session_id"])

            rid2, p2, mode = followup.start(row, self.cfg, self.store, reply="also check tests")
            launcher.wait(rid2, 20)
            self.assertEqual(mode, "resume")
            row2 = self.store.get(rid2)
            self.assertEqual((row2["tool"], row2["model"], row2["session_id"]),
                             ("omniroute", row["model"], row["session_id"]))

            # Explicit tool="omniroute" rerun from another tool is also accepted by _check
            agy_rid, _ = self.run_and_wait("fix typo in readme", tool="agy", tier="basic")
            rid3, p3, mode3 = followup.start(self.store.get(agy_rid), self.cfg, self.store, tool="omniroute", tier="light")
            launcher.wait(rid3, 20)
            self.assertEqual((mode3, p3["tool"], p3["tier"]), ("rerun", "omniroute", "light"))

    def test_rejects_bad_requests(self):
        rid, _ = self.run_and_wait("fix typo in readme", tool="agy", tier="basic")
        row = self.store.get(rid)
        with self.assertRaises(followup.FollowupError):
            followup.start(None, self.cfg, self.store)
        with self.assertRaises(followup.FollowupError):
            followup.start(dict(row, status="running"), self.cfg, self.store)
        with self.assertRaises(followup.FollowupError):
            followup.start(dict(row, source="hook"), self.cfg, self.store)
        with self.assertRaises(followup.FollowupError):
            followup.start(row, self.cfg, self.store, tool="rm")
        with self.assertRaises(followup.FollowupError):
            followup.start(row, self.cfg, self.store, tier="godmode")

    def test_api_rerun(self):
        import json, urllib.request
        from hub import auth
        srv = make_server(self.cfg, self.store)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rid, _ = self.run_and_wait("fix typo in readme", tool="agy", tier="basic")

            def post(body):
                r = urllib.request.Request(f"http://127.0.0.1:{self.cfg['port']}/api/rerun", method="POST",
                                           data=json.dumps(body).encode(),
                                           headers={"Content-Type": "application/json", "X-Jarviee-Token": auth.get_token(self.cfg)})
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    with op.open(r, timeout=10) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, json.loads(e.read())
            code, out = post({"id": rid})
            self.assertEqual((code, out["mode"]), (200, "rerun"))
            launcher.wait(out["id"], 20)
            self.assertEqual(post({"id": "nope"})[0], 400)
            self.assertEqual(post({"id": rid, "tier": "bogus"})[0], 400)
        finally:
            if srv.scheduler:
                srv.scheduler.stop()
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
