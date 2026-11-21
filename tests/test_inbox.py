"""Review inbox: listing, accept / dismiss, one-step revert (success and every refusal) and the test gate.
All git work happens in throwaway repos under a temp dir."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg  # noqa: F401  (helpers puts the repo root on sys.path)
from hub import gitops, inbox
from test_http_cache import HttpTestBase


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class InboxBase(HttpTestBase):
    def setUp(self):
        super().setUp()
        inbox.invalidate()
        self.repo = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha"
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "owner@example.com")
        git(self.repo, "config", "user.name", "Owner")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "app.py").write_text("print('one')\n", encoding="utf-8")
        (self.repo / "other.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: start")
        self.n = 0

    def make_run(self, commit=True, status="done", task="Fix the greeting in app.py", files=("app.py",),
                 content="print('two')\nprint('three')\n", **extra):
        """A finished launched run: edits `files`, commits like the run worker does, records the row."""
        self.n += 1
        rid = f"run-{self.n:08x}"
        base = git(self.repo, "rev-parse", "HEAD")
        for f in files:
            (self.repo / f).write_text(content, encoding="utf-8")
        if commit:
            self.assertTrue(gitops.auto_commit(str(self.repo), task, files=list(files)))
        fields = dict(source="launcher", tool="claude", model="sonnet", tier="light", status=status, task=task,
                      project="Alpha", workspace=str(self.repo), files=list(files), base_head=base,
                      started=time.time() - 90, ended=time.time() - 30, activity="finished")
        fields.update(extra)
        self.store.upsert(rid, **fields)
        return rid

    def items(self):
        code, _, body = self.req("GET", "/api/inbox")
        self.assertEqual(code, 200)
        return json.loads(body)["items"]

    def post(self, path, rid):
        code, _, body = self.req("POST", path, body={"id": rid})
        return code, json.loads(body)


class ListingTests(InboxBase):
    def test_lists_finished_run_with_counts_commit_and_safe_revert(self):
        rid = self.make_run()
        (item,) = self.items()
        self.assertEqual(item["id"], rid)
        self.assertEqual((item["project"], item["tool"], item["model"], item["level"], item["status"]),
                         ("Alpha", "claude", "sonnet", "light", "done"))
        self.assertAlmostEqual(item["duration"], 60, delta=1)
        self.assertEqual(item["files"], [{"path": "app.py", "added": 2, "removed": 1, "binary": False}])
        self.assertEqual((item["added"], item["removed"]), (2, 1))
        self.assertTrue(item["committed"])
        self.assertEqual(len(item["commits"]), 1)
        self.assertEqual(item["commits"][0]["sha"], git(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(item["revert"]["ok"])
        self.assertFalse(item["check"]["configured"])

    def test_uncommitted_new_file_is_counted(self):
        self.make_run(commit=False, status="failed", files=("fresh.py",), content="a\nb\nc\n")
        (item,) = self.items()
        self.assertEqual(item["files"], [{"path": "fresh.py", "added": 3, "removed": 0, "binary": False}])
        self.assertFalse(item["committed"])

    def test_only_finished_launched_runs_with_files_and_no_decision(self):
        a = self.make_run()
        self.make_run(source="hook", files=("other.py",))                     # not launched from Jarviee
        self.store.upsert("run-running", source="launcher", status="running", files=["app.py"], task="t")
        self.store.upsert("run-nofiles", source="launcher", status="done", task="t")
        self.store.upsert("run-retry", source="launcher", status="failed", files=["app.py"], task="t",
                          activity="retrying (1/3): boom")
        b = self.make_run(files=("other.py",), content="x = 2\n")
        self.assertEqual([i["id"] for i in self.items()], [b, a])             # newest first
        self.assertEqual(json.loads(self.req("GET", "/api/inbox")[2])["count"], 2)

    def test_conditional_get(self):
        self.make_run()
        code, h, _ = self.req("GET", "/api/inbox")
        self.assertEqual(code, 200)
        code, _, body = self.req("GET", "/api/inbox", {"If-None-Match": h["etag"]})
        self.assertEqual((code, body), (304, b""))


class DecisionTests(InboxBase):
    def test_accept_leaves_the_inbox(self):
        rid = self.make_run()
        code, out = self.post("/api/inbox/accept", rid)
        self.assertEqual((code, out["ok"], out["reviewed"]), (200, True, inbox.ACCEPTED))
        self.assertEqual(self.items(), [])
        self.assertEqual(self.store.get(rid)["reviewed"], inbox.ACCEPTED)
        self.assertEqual(self.post("/api/inbox/accept", rid)[0], 409)   # already decided

    def test_dismiss_and_unknown_run(self):
        rid = self.make_run()
        self.assertEqual(self.post("/api/inbox/dismiss", rid)[1]["reviewed"], inbox.DISMISSED)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.post("/api/inbox/accept", "run-nope")[0], 404)

    def test_posts_need_the_token(self):
        rid = self.make_run()
        for path in ("accept", "dismiss", "revert", "check"):
            code, _, _ = self.req("POST", f"/api/inbox/{path}", {"X-Jarviee-Token": "wrong"}, {"id": rid})
            self.assertEqual(code, 403, path)
        self.assertEqual(len(self.items()), 1)


class RevertTests(InboxBase):
    def test_revert_makes_one_new_commit_and_restores_the_file(self):
        rid = self.make_run()
        before = git(self.repo, "rev-list", "--count", "HEAD")
        run_commit = git(self.repo, "rev-parse", "HEAD")
        code, out = self.post("/api/inbox/revert", rid)
        self.assertEqual(code, 200, out)
        self.assertEqual(out["reverted"], run_commit)
        self.assertEqual(int(git(self.repo, "rev-list", "--count", "HEAD")), int(before) + 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), out["commit"])
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "print('one')\n")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        msg = git(self.repo, "log", "-1", "--format=%B")
        self.assertNotIn("Co-authored", msg)
        self.assertNotRegex(msg.lower(), r"claude|anthropic|\bai\b|agy|antigravity|jarviee")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%an <%ae>"), "Owner <owner@example.com>")
        self.assertEqual(git(self.repo, "cat-file", "-t", run_commit), "commit")   # history is untouched
        self.assertEqual(self.store.get(rid)["reviewed"], inbox.REVERTED)
        self.assertEqual(self.items(), [])

    def refused(self, rid, code):
        status, out = self.post("/api/inbox/revert", rid)
        self.assertEqual((status, out["code"]), (409, code), out)
        self.assertTrue(out["error"])
        return out

    def listed_reason(self, rid):
        item = next(i for i in self.items() if i["id"] == rid)
        self.assertFalse(item["revert"]["ok"])
        return item["revert"]

    def test_refuses_when_the_run_did_not_commit(self):
        rid = self.make_run(commit=False)
        head = git(self.repo, "rev-parse", "HEAD")
        self.refused(rid, "no_commit")
        self.assertEqual(self.listed_reason(rid)["code"], "no_commit")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_refuses_a_failed_run_with_its_own_reason(self):
        rid = self.make_run(commit=False, status="failed")
        out = self.refused(rid, "no_commit")
        self.assertIn("failed run", out["error"])

    def test_refuses_with_uncommitted_changes_to_the_files(self):
        rid = self.make_run()
        (self.repo / "app.py").write_text("print('mine')\n", encoding="utf-8")
        out = self.refused(rid, "dirty")
        self.assertIn("app.py", out["error"])
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "print('mine')\n")   # untouched

    def test_uncommitted_changes_elsewhere_do_not_block(self):
        rid = self.make_run()
        (self.repo / "other.py").write_text("x = 99\n", encoding="utf-8")
        self.assertEqual(self.post("/api/inbox/revert", rid)[0], 200)
        self.assertEqual((self.repo / "other.py").read_text(encoding="utf-8"), "x = 99\n")

    def test_refuses_when_a_later_commit_touched_the_same_files(self):
        rid = self.make_run()
        (self.repo / "app.py").write_text("print('later')\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "feat: later change")
        head = git(self.repo, "rev-parse", "HEAD")
        out = self.refused(rid, "later_commit")
        self.assertIn("later change", out["error"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_a_later_commit_on_other_files_does_not_block(self):
        rid = self.make_run()
        (self.repo / "other.py").write_text("x = 5\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "feat: elsewhere")
        self.assertEqual(self.post("/api/inbox/revert", rid)[0], 200)

    def test_refuses_when_the_commit_is_no_longer_on_the_branch(self):
        rid = self.make_run()
        run_commit = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "elsewhere", "HEAD~1")
        out = self.refused(rid, "unreachable")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "elsewhere")
        self.assertEqual(git(self.repo, "cat-file", "-t", run_commit), "commit")

    def test_refuses_when_history_was_rewritten(self):
        rid = self.make_run()
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")   # the owner dropped the run's commit
        self.refused(rid, "no_commit")

    def test_refuses_without_a_recorded_start_point(self):
        rid = self.make_run(base_head="")
        self.refused(rid, "no_base")

    def test_refuses_more_than_one_matching_commit(self):
        rid = self.make_run()
        (self.repo / "app.py").write_text("print('again')\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", gitops.clean_commit_message("Fix the greeting in app.py"))
        self.refused(rid, "many_commits")

    def test_double_submit_makes_exactly_one_revert_commit(self):
        import threading
        rid = self.make_run()
        results = []

        def go():
            try:
                results.append(("ok", inbox.revert(self.cfg, self.store, rid)))
            except inbox.InboxError as e:
                results.append((e.code, None))
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r[0] for r in results), ["not_pending", "ok"])
        self.assertEqual(git(self.repo, "log", "--format=%s", "-3").count("Revert"), 1)
        self.assertEqual(self.store.get(rid)["reviewed"], inbox.REVERTED)

    def test_refuses_during_a_revert_in_progress(self):
        rid = self.make_run()
        (Path(git(self.repo, "rev-parse", "--absolute-git-dir")) / "REVERT_HEAD").write_text("x\n")
        self.refused(rid, "in_progress")

    def test_refuses_a_missing_folder(self):
        rid = self.make_run(workspace=str(self.repo / "gone"))
        self.refused(rid, "no_repo")

    def test_unrelated_commit_with_the_same_subject_is_not_the_runs(self):
        rid = self.make_run(commit=False)
        (self.repo / "app.py").write_text("print('typed by hand')\n", encoding="utf-8")
        (self.repo / "other.py").write_text("x = 7\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", gitops.clean_commit_message("Fix the greeting in app.py"))
        self.refused(rid, "no_commit")   # touched a file the run never recorded


class FactsCacheTests(InboxBase):
    def count_git(self, fn):
        real, calls = inbox._git, []
        inbox._git = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
        try:
            fn()
        finally:
            inbox._git = real
        return len(calls)

    def test_listing_twice_runs_git_once_and_a_change_or_decision_recomputes(self):
        rid = self.make_run()
        listing = lambda: inbox.list_items(self.cfg, self.store)
        first = self.count_git(listing)
        self.assertGreater(first, 3)
        self.assertEqual(self.count_git(listing), 3)   # only the per-request repo reads (root, HEAD, status)
        (self.repo / "app.py").write_text("print('changed after')\n", encoding="utf-8")   # file mtime / size moved
        self.assertGreater(self.count_git(listing), 3)
        self.assertEqual(self.count_git(listing), 3)
        self.post("/api/inbox/accept", rid)
        self.assertEqual(listing(), [])

    def test_anywhere_lookup_only_for_the_opened_run(self):
        self.make_run(commit=False)
        def log_calls(open_id):
            real, calls = inbox._git, []
            inbox._git = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
            try:
                inbox.list_items(self.cfg, self.store, open_id)
            finally:
                inbox._git = real
            return [a for a in calls if "--branches" in a]
        self.assertEqual(log_calls(None), [])
        inbox.invalidate()
        self.assertEqual(len(log_calls(self.store.all()[0]["id"])), 1)


class CheckTests(InboxBase):
    def test_no_command_is_said_plainly_and_never_guessed(self):
        rid = self.make_run()
        (self.repo / "package.json").write_text("{}", encoding="utf-8")
        (self.repo / "tests").mkdir()
        code, out = self.post("/api/inbox/check", rid)
        self.assertEqual((code, out["code"]), (409, "no_command"))
        self.assertIn("No test command", out["error"])
        self.assertEqual(self.items()[0]["check"], {"configured": False, "command": "", "status": "", "output": "", "at": None})

    def test_config_command_pass_and_fail_are_stored(self):
        rid = self.make_run()
        self.cfg["checks"] = {"Alpha": f"{sys.executable} -c \"print('all good')\""}
        code, out = self.post("/api/inbox/check", rid)
        self.assertEqual(code, 200, out)
        self.assertEqual(out["check"]["status"], "pass")
        self.assertIn("all good", out["check"]["output"])
        (item,) = self.items()
        self.assertEqual((item["check"]["status"], item["check"]["configured"]), ("pass", True))
        self.cfg["checks"] = {"Alpha": f"{sys.executable} -c \"import sys; print('boom'); sys.exit(3)\""}
        out = self.post("/api/inbox/check", rid)[1]
        self.assertEqual(out["check"]["status"], "fail")
        self.assertIn("boom", out["check"]["output"])
        self.assertIn("[exit 3]", out["check"]["output"])
        self.assertEqual(self.store.get(rid)["check_status"], "fail")

    def test_brain_file_names_the_command_and_runs_in_the_project_folder(self):
        rid = self.make_run()
        (self.repo / ".brain" / "CHECK_COMMAND").write_text(
            f"# how this project is tested\n{sys.executable} -c \"import os; print(os.getcwd())\"\n", encoding="utf-8")
        code, out = self.post("/api/inbox/check", rid)   # a file the run could have written: not run unconfirmed
        self.assertEqual((code, out["code"]), (409, "untrusted"))
        self.assertEqual(self.items()[0]["check"]["trusted"], False)
        self.assertEqual(self.items()[0]["check"]["source"], "file")
        cmd = self.items()[0]["check"]["command"]
        code, out = self.req("POST", "/api/inbox/check", body={"id": rid, "trust_command": cmd})[::2]
        out = json.loads(out)
        self.assertEqual(out["check"]["status"], "pass")
        self.assertIn(os.path.realpath(self.repo), os.path.realpath(out["check"]["output"].splitlines()[1]))
        self.assertEqual(self.post("/api/inbox/check", rid)[0], 200)   # confirmed once: no second question
        (self.repo / ".brain" / "CHECK_COMMAND").write_text("rm -rf /tmp/nothing\n", encoding="utf-8")   # a changed file
        code, out = self.post("/api/inbox/check", rid)
        self.assertEqual((code, out["code"]), (409, "untrusted"))
        code, out = self.req("POST", "/api/inbox/check", body={"id": rid, "trust_command": "something else"})[::2]
        self.assertEqual(code, 409)   # the confirmation must be for exactly the text shown

    def test_check_runs_clean_env_in_its_own_group_and_kills_the_group_on_timeout(self):
        rid = self.make_run()
        marker = Path(self.tmp) / "child.pid"
        script = ("import os, subprocess, sys, time; "
                  "print('TOK=' + os.environ.get('JARVIEE_TEST_TOKEN', 'absent') + ' ' + os.environ.get('MY_TOKEN', 'absent'), flush=True); "
                  f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); open({str(marker)!r}, 'w').write(str(c.pid)); "
                  "time.sleep(60) if len(sys.argv) > 1 else None")
        os.environ["JARVIEE_TEST_TOKEN"] = os.environ["MY_TOKEN"] = "secret"
        self.addCleanup(os.environ.pop, "JARVIEE_TEST_TOKEN", None)
        self.addCleanup(os.environ.pop, "MY_TOKEN", None)
        self.cfg["checks"] = {"Alpha": f"{sys.executable} -c \"{script}\" slow", "timeout_s": 2}
        out = self.post("/api/inbox/check", rid)[1]
        self.assertIn("timed out", out["check"]["output"])
        self.assertIn("TOK=absent absent", out["check"]["output"])
        child = int(marker.read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_two_checks_for_one_run_do_not_both_start(self):
        rid = self.make_run()
        inbox._checking.add(rid)
        self.addCleanup(inbox._checking.discard, rid)
        self.cfg["checks"] = {"Alpha": f"{sys.executable} -c \"print(1)\""}
        code, out = self.post("/api/inbox/check", rid)
        self.assertEqual((code, out["code"]), (409, "busy"))

    def test_timeout_and_missing_program_fail_cleanly(self):
        rid = self.make_run()
        self.cfg["checks"] = {"Alpha": f"{sys.executable} -c \"import time; time.sleep(30)\"", "timeout_s": 1}
        out = self.post("/api/inbox/check", rid)[1]
        self.assertEqual(out["check"]["status"], "fail")
        self.assertIn("timed out", out["check"]["output"])
        self.cfg["checks"] = {"Alpha": "definitely-not-a-program --x"}
        out = self.post("/api/inbox/check", rid)[1]
        self.assertEqual(out["check"]["status"], "fail")
        self.assertIn("could not run", out["check"]["output"])


if __name__ == "__main__":
    unittest.main()
