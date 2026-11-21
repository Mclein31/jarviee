"""Opt-in worktree runs (hub/worktrees.py): create, run, commit, bring back (fast-forward / merge), refusals that keep
the branch, cleanup, the startup sweep, agy never getting one, and the feature being off by default.
Every repository here is a throwaway one in a temp folder."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import coord, gitops, inbox, launcher, orphans, worktrees
from hub.store import Store

# the fake claude: writes WRITE=<file>:<text> into its working directory, sleeps SLEEP=<s>, exits EXIT=<n>
FAKE = ("import sys,re,time; p=sys.argv[1]; "
        "[open(m[1],'w').write(m[2]+'\\n') for m in re.finditer(r'WRITE=(\\S+?):(\\S+)',p)]; "
        "s=re.search(r'SLEEP=([0-9.]+)',p); s and time.sleep(float(s[1])); "
        "print('fake ran'); e=re.search(r'EXIT=(\\d+)',p); sys.exit(int(e[1]) if e else 0)")


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.cfg["retry"] = {"enabled": False}
        self.cfg["worktree"] = {"enabled": True, "when": "always", "dir": str(Path(self.tmp) / "wts")}
        self.cfg["tools"]["claude"]["command"] = [sys.executable, "-c", FAKE, "{prompt}"]
        self.store = Store(self.cfg["db_path"])
        self.ws = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha"
        (self.ws / "a.txt").write_text("one\ntwo\nthree\n")
        (self.ws / "b.txt").write_text("b\n")
        git(self.ws, "init", "-q", "-b", "main")
        git(self.ws, "config", "user.name", "Owner")
        git(self.ws, "config", "user.email", "owner@example.com")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-q", "-m", "chore: start")
        self.base = git(self.ws, "rev-parse", "HEAD")

    def tearDown(self):
        self._t.__exit__()

    def run_task(self, task, tool="claude", tier="complex", wait=True, **kw):
        rid, _ = launcher.launch(task, self.cfg, self.store, tool=tool, tier=tier, project="Alpha", **kw)
        if wait:
            self.assertTrue(launcher.wait(rid, 30))
        return rid

    def wait_for(self, rid, key, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if (self.store.get(rid) or {}).get(key):
                return
            time.sleep(0.05)
        self.fail(f"{key} never set")

    def wts(self):
        d = Path(self.cfg["worktree"]["dir"])
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


class Settings(Base):
    def test_off_by_default_with_the_documented_defaults(self):
        s = worktrees.settings({})
        self.assertEqual((s["enabled"], s["levels"], s["when"]), (False, ["complex", "heavy"], "overlap"))
        self.assertEqual(worktrees.settings({"worktree": {"when": "bogus"}})["when"], "overlap")

    def test_a_run_gets_no_worktree_unless_enabled(self):
        self.cfg["worktree"]["enabled"] = False
        rid = self.run_task("WRITE=new.txt:hi")
        row = self.store.get(rid)
        self.assertFalse(row.get("worktree_path"))
        self.assertEqual((self.ws / "new.txt").read_text().strip(), "hi")   # ran in the project itself
        self.assertEqual(self.wts(), [])

    def test_eligibility(self):
        claude = {"tool": "claude", "level": "complex"}
        el = lambda p=claude, d="go", ro=False, live=False, argv=(): worktrees.eligible(self.cfg, p, d, ro, live, argv)
        self.assertTrue(el())
        self.assertFalse(el({"tool": "agy", "level": "complex"}))            # never agy
        self.assertFalse(el({"tool": "claude", "level": "light"}))           # level not listed
        self.assertFalse(el(ro=True))
        self.assertFalse(el(live=True))                                      # live pane sessions: not in v1
        self.assertFalse(el(argv=["claude", "--resume", "x"]))
        self.cfg["worktree"]["when"] = "overlap"
        self.assertFalse(el(d="go"))
        self.assertFalse(el(d="split"))
        self.assertTrue(el(d="wait"))


class Lifecycle(Base):
    def test_create_makes_a_branch_from_head_in_the_owned_folder(self):
        wt = worktrees.create(self.cfg, "run-x1", self.ws)
        self.assertEqual(wt["branch"], "jarviee/run-x1")
        self.assertEqual(Path(wt["path"]), Path(self.cfg["worktree"]["dir"]) / "run-x1")
        self.assertEqual(git(wt["path"], "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(wt["path"], "branch", "--show-current"), "jarviee/run-x1")
        self.assertTrue((Path(wt["path"]) / "a.txt").is_file())
        self.assertEqual(git(self.ws, "branch", "--show-current"), "main")   # the project's checkout is untouched

    def test_create_refuses_a_folder_that_is_not_a_repository(self):
        self.assertIsNone(worktrees.create(self.cfg, "run-x2", Path(self.tmp)))

    def test_fast_forward_bring_back(self):
        rid = self.run_task("update a WRITE=a.txt:changed WRITE=c.txt:new")
        row = self.store.get(rid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["worktree_result"], "fast-forward")
        self.assertEqual(row["worktree_branch"], f"jarviee/{rid}")
        self.assertEqual((self.ws / "a.txt").read_text().strip(), "changed")
        self.assertEqual((self.ws / "c.txt").read_text().strip(), "new")
        self.assertEqual(sorted(row["files"]), ["a.txt", "c.txt"])
        self.assertEqual(git(self.ws, "log", "--format=%s", f"{self.base}..HEAD"),
                         gitops.clean_commit_message("update a WRITE=a.txt:changed WRITE=c.txt:new"))
        self.assertEqual(git(self.ws, "log", "-1", "--format=%an <%ae>"), "Owner <owner@example.com>")
        self.assertNotIn("Co-authored", git(self.ws, "log", "-1", "--format=%B"))
        self.assertEqual(self.wts(), [])                                      # folder removed
        self.assertEqual(git(self.ws, "worktree", "list").count("\n"), 0)    # and pruned
        self.assertEqual(git(self.ws, "branch", "--list", "jarviee/*"), "")  # merged branch deleted
        self.assertEqual(git(self.ws, "status", "--porcelain", "--", "a.txt", "c.txt"), "")

    def test_the_run_happens_in_the_worktree_not_the_project(self):
        rid = self.run_task("WRITE=c.txt:new SLEEP=1", wait=False)
        self.wait_for(rid, "worktree_path")
        time.sleep(0.4)
        self.assertFalse((self.ws / "c.txt").exists())                        # not in the project while running
        self.assertTrue((Path(self.store.get(rid)["worktree_path"]) / "c.txt").exists())
        self.assertTrue(launcher.wait(rid, 30))
        self.assertTrue((self.ws / "c.txt").exists())

    def test_merge_bring_back_when_the_project_moved_on(self):
        rid = self.run_task("WRITE=c.txt:new SLEEP=1", wait=False)
        self.wait_for(rid, "worktree_path")
        (self.ws / "b.txt").write_text("b changed\n")
        git(self.ws, "commit", "-q", "-am", "fix: b")
        self.assertTrue(launcher.wait(rid, 30))
        row = self.store.get(rid)
        self.assertEqual(row["worktree_result"], "merged")
        self.assertEqual(git(self.ws, "log", "-1", "--format=%p").count(" "), 1)   # a merge commit
        self.assertEqual((self.ws / "c.txt").read_text().strip(), "new")
        self.assertEqual((self.ws / "b.txt").read_text().strip(), "b changed")
        self.assertEqual(self.wts(), [])
        # the review inbox finds the run's own commit through the merge
        item = inbox.list_items(self.cfg, self.store)[0]
        self.assertTrue(item["committed"])
        self.assertEqual([c["subject"] for c in item["commits"]], [gitops.clean_commit_message("WRITE=c.txt:new SLEEP=1")])
        self.assertTrue(item["revert"]["ok"], item["revert"])

    def test_a_conflict_is_left_on_its_branch(self):
        rid = self.run_task("WRITE=a.txt:theirs SLEEP=1", wait=False)
        self.wait_for(rid, "worktree_path")
        (self.ws / "a.txt").write_text("mine\n")
        git(self.ws, "commit", "-q", "-am", "fix: a")
        head = git(self.ws, "rev-parse", "HEAD")
        self.assertTrue(launcher.wait(rid, 30))
        row = self.store.get(rid)
        self.assertEqual((row["status"], row["worktree_result"]), ("done", "conflict"))
        self.assertIn(f"left on branch jarviee/{rid}", row["activity"])
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), head)             # the project's branch did not move
        self.assertEqual((self.ws / "a.txt").read_text(), "mine\n")
        self.assertEqual(git(self.ws, "status", "--porcelain", "-uno"), "")   # no merge in progress
        self.assertEqual(self.wts(), [])                                      # folder gone
        self.assertEqual(git(self.ws, "show", f"jarviee/{rid}:a.txt"), "theirs")   # branch kept with the work

    def test_a_dirty_project_blocks_the_bring_back(self):
        rid = self.run_task("WRITE=a.txt:theirs SLEEP=1", wait=False)
        self.wait_for(rid, "worktree_path")
        (self.ws / "a.txt").write_text("uncommitted\n")                       # the owner is editing the same file
        head = git(self.ws, "rev-parse", "HEAD")
        self.assertTrue(launcher.wait(rid, 30))
        row = self.store.get(rid)
        self.assertEqual(row["worktree_result"], "dirty")
        self.assertIn("a.txt", row["worktree_note"])
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), head)
        self.assertEqual((self.ws / "a.txt").read_text(), "uncommitted\n")    # untouched
        self.assertEqual(git(self.ws, "show", f"jarviee/{rid}:a.txt"), "theirs")

    def test_staged_changes_block_a_merge(self):
        rid = self.run_task("WRITE=c.txt:new SLEEP=1", wait=False)
        self.wait_for(rid, "worktree_path")
        (self.ws / "b.txt").write_text("b2\n")
        git(self.ws, "commit", "-q", "-am", "fix: b")
        (self.ws / "d.txt").write_text("staged\n")
        git(self.ws, "add", "d.txt")
        self.assertTrue(launcher.wait(rid, 30))
        self.assertEqual(self.store.get(rid)["worktree_result"], "dirty")
        self.assertFalse((self.ws / "c.txt").exists())

    def test_a_failed_run_keeps_its_branch_and_loses_its_folder(self):
        rid = self.run_task("WRITE=c.txt:new EXIT=1")
        row = self.store.get(rid)
        self.assertEqual((row["status"], row["worktree_result"]), ("failed", "not_done"))
        self.assertFalse((self.ws / "c.txt").exists())
        self.assertEqual(self.wts(), [])
        self.assertEqual(git(self.ws, "show", f"jarviee/{rid}:c.txt"), "new")   # the work is on the branch

    def test_a_run_that_changes_nothing_leaves_nothing_behind(self):
        rid = self.run_task("just look")
        row = self.store.get(rid)
        self.assertEqual((row["status"], row["worktree_result"]), ("done", "no_changes"))
        self.assertEqual(self.wts(), [])
        self.assertEqual(git(self.ws, "branch", "--list", "jarviee/*"), "")

    def test_a_queue_task_reports_through_on_finish_like_any_run(self):
        seen = []
        rid = self.run_task("WRITE=c.txt:new", on_finish=lambda r, s, c: seen.append((r, s, c)), log_runs=False)
        self.assertEqual(seen, [(rid, "done", 0)])
        self.assertEqual(self.store.get(rid)["worktree_result"], "fast-forward")
        self.assertEqual(git(self.ws, "log", "-1", "--format=%s"), gitops.clean_commit_message("WRITE=c.txt:new"))   # committed on the branch, not twice

    def test_agy_never_gets_a_worktree(self):
        rid = self.run_task("WRITE=c.txt:new", tool="agy", tier="complex")
        self.assertFalse(self.store.get(rid).get("worktree_path"))
        self.assertEqual(self.wts(), [])

    def test_levels_filter(self):
        rid = self.run_task("look", tier="light")
        self.assertFalse(self.store.get(rid).get("worktree_path"))

    def test_overlap_mode_uses_a_worktree_instead_of_waiting(self):
        self.cfg["worktree"]["when"] = "overlap"
        self.store.upsert("other", status="running", workspace=str(self.ws), pid=os.getpid(),
                          pid_start=orphans.proc_start(os.getpid()))
        coord.register(self.store, "other", self.ws, "rework the text", ["a.txt"])
        rid = self.run_task("edit a.txt WRITE=c.txt:new")
        row = self.store.get(rid)
        self.assertEqual(row["worktree_result"], "fast-forward")              # it did not wait for "other"
        # no overlap: the normal path
        rid2 = self.run_task("edit b.txt")
        self.assertFalse(self.store.get(rid2).get("worktree_path"))


class Sweep(Base):
    def make(self, rid, **row):
        wt = worktrees.create(self.cfg, rid, self.ws)
        (Path(wt["path"]) / "c.txt").write_text("left over\n")
        self.store.upsert(rid, source="launcher", task="add c", base_head=wt["base"], workspace=str(self.ws),
                          worktree_path=wt["path"], worktree_branch=wt["branch"], **row)
        return wt

    def test_finished_runs_are_cleaned_up_and_live_ones_left_alone(self):
        self.make("run-done1", status="done", ended=time.time())
        self.make("run-fail1", status="failed", ended=time.time())
        live = self.make("run-live1", status="running", pid=os.getpid(), pid_start=orphans.proc_start(os.getpid()))
        cleaned = worktrees.sweep(self.cfg, self.store)
        self.assertEqual(sorted(cleaned), ["run-done1", "run-fail1"])
        self.assertEqual(self.wts(), ["run-live1"])
        self.assertTrue((Path(live["path"]) / "c.txt").is_file())
        self.assertEqual(self.store.get("run-done1")["worktree_result"], "fast-forward")
        self.assertEqual((self.ws / "c.txt").read_text().strip(), "left over")   # the done run's work arrived
        self.assertEqual(self.store.get("run-fail1")["worktree_result"], "not_done")
        self.assertEqual(git(self.ws, "branch", "--list", "jarviee/run-fail1").strip(), "jarviee/run-fail1")

    def test_a_row_that_never_reached_the_database_is_cleaned_without_bringing_anything_back(self):
        wt = worktrees.create(self.cfg, "run-ghost", self.ws)
        (Path(wt["path"]) / "c.txt").write_text("x\n")
        self.assertEqual(worktrees.sweep(self.cfg, self.store), ["run-ghost"])
        self.assertEqual(self.wts(), [])
        self.assertFalse((self.ws / "c.txt").exists())
        self.assertEqual(git(self.ws, "show", "jarviee/run-ghost:c.txt"), "x")


class Review(Base):
    def test_a_branch_left_behind_is_shown_from_its_branch(self):
        rid = self.run_task("WRITE=a.txt:theirs SLEEP=1", wait=False)
        self.wait_for(rid, "worktree_path")
        (self.ws / "a.txt").write_text("uncommitted\n")
        self.assertTrue(launcher.wait(rid, 30))
        row = self.store.get(rid)
        item = inbox.list_items(self.cfg, self.store)[0]
        self.assertFalse(item["committed"])
        self.assertEqual(item["worktree"]["result"], "dirty")
        self.assertEqual(item["revert"]["code"], "on_branch")
        self.assertEqual([(f["path"], f["added"]) for f in item["files"]], [("a.txt", 1)])
        diff = gitops.run_diff(row, self.cfg)
        self.assertIn("+theirs", diff)
        self.assertNotIn("uncommitted", diff)


if __name__ == "__main__":
    unittest.main()
