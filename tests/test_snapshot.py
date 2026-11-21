import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the repo on sys.path)
from hub import launcher, snapshot


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Jane")
        git(self.repo, "config", "user.email", "jane@example.com")
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "other.txt").write_text("other\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(snapshot.take(d))
            self.assertEqual(snapshot.changed(None, None), [])

    def test_changed_ignores_preexisting_dirt(self):
        (self.repo / "other.txt").write_text("someone else's edit\n")   # dirty before the run starts
        before = snapshot.take(self.repo)
        (self.repo / "a.txt").write_text("changed\n")
        (self.repo / "new.txt").write_text("new\n")
        after = snapshot.take(self.repo)
        self.assertEqual(snapshot.changed(before, after), ["a.txt", "new.txt"])

    def test_edit_of_already_dirty_file_counts(self):
        (self.repo / "other.txt").write_text("v1\n")
        before = snapshot.take(self.repo)
        (self.repo / "other.txt").write_text("v2\n")
        self.assertEqual(snapshot.changed(before, snapshot.take(self.repo)), ["other.txt"])

    def test_run_that_commits_its_own_work(self):
        before = snapshot.take(self.repo)
        (self.repo / "a.txt").write_text("changed\n")
        git(self.repo, "commit", "-qam", "by the agent")
        self.assertEqual(snapshot.changed(before, snapshot.take(self.repo)), ["a.txt"])

    def test_auto_commit_takes_only_the_runs_files(self):
        before = snapshot.take(self.repo)
        (self.repo / "other.txt").write_text("another agent's work in progress\n")
        (self.repo / "a.txt").write_text("this run\n")
        files = ["a.txt"]
        self.assertTrue(launcher.auto_commit(str(self.repo), "Update a", files=files))
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), ["a.txt"])
        self.assertIn("other.txt", git(self.repo, "status", "--porcelain"))   # still uncommitted
        self.assertFalse(launcher.auto_commit(str(self.repo), "Nothing", files=[]))
        self.assertIsNotNone(before)

    def test_auto_commit_from_a_subfolder_and_deletion(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "b.txt").write_text("b\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "sub")
        before = snapshot.take(self.repo / "sub")
        (self.repo / "sub" / "b.txt").unlink()
        files = snapshot.changed(before, snapshot.take(self.repo / "sub"))
        self.assertEqual(files, ["sub/b.txt"])
        self.assertTrue(launcher.auto_commit(str(self.repo / "sub"), "Remove b", files=files))
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")


class RunDiffTests(SnapshotTests):
    def test_diff_shows_committed_modified_and_new_files_for_the_run_only(self):
        base = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "other.txt").write_text("another agent\n")
        (self.repo / "a.txt").write_text("a\nmore\n")
        (self.repo / "fresh.txt").write_text("brand new\n")
        git(self.repo, "commit", "-qm", "run commit", "--", "a.txt")
        agent = {"workspace": str(self.repo), "files": ["a.txt", "fresh.txt"], "base_head": base}
        out = launcher.run_diff(agent, {})
        self.assertIn("+more", out)
        self.assertIn("+brand new", out)
        self.assertNotIn("another agent", out)
        self.assertIn("no changes", launcher.run_diff(dict(agent, files=["nothing.txt"]), {}).lower())

    def test_diff_without_file_list_is_whole_tree(self):
        (self.repo / "a.txt").write_text("edit\n")
        self.assertIn("+edit", launcher.run_diff({"workspace": str(self.repo)}, {}))


class SoftDenyTests(unittest.TestCase):
    def test_detects_soft_deny_since_start(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "cli-20261111_100000.log"
            log.write_text('I1111 tool_confirmation_manager.go:221] Print mode: soft-denying tool confirmation "RunCommand" at step 3\n')
            self.assertTrue(launcher.agy_soft_denied(time.time() - 5, log_dir=d))
            self.assertFalse(launcher.agy_soft_denied(time.time() + 60, log_dir=d))   # older than the run
            log.write_text("all fine\n")
            self.assertFalse(launcher.agy_soft_denied(time.time() - 5, log_dir=d))
            self.assertFalse(launcher.agy_soft_denied(0, log_dir=Path(d) / "missing"))


if __name__ == "__main__":
    unittest.main()
