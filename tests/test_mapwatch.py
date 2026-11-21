"""Map auto-update: detect code that moved on since the map, launch one update when the project is quiet,
validate, restore a broken map, commit a good one, and don't re-launch for the same commits."""
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempDir, make_cfg
from hub import mapwatch, projmap
from hub.store import Store

FLOW = {"title": "Alpha", "nodes": [{"id": "user", "label": "User", "kind": "actor", "layer": "frontend"},
                                    {"id": "ok", "label": "Signed in?", "kind": "decision", "layer": "frontend"},
                                    {"id": "ui", "label": "Home", "kind": "screen", "layer": "frontend"}],
        "edges": [{"from": "user", "to": "ok"}, {"from": "ok", "to": "ui", "label": "yes"}],
        "flows": [{"name": "Open", "steps": ["user", "ok", "ui"]}]}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class MapWatchTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.cfg, _ = make_cfg(str(self.tmp))
        self.cfg["maps"] = {"settle_s": 0, "cooldown_s": 3600, "max_per_day": 8}
        self.store = Store(self.cfg["db_path"])
        self.proj = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha"
        git(self.proj, "init", "-q")
        git(self.proj, "config", "user.email", "t@example.com")
        git(self.proj, "config", "user.name", "T")
        (self.proj / ".brain" / "FLOW.json").write_text(json.dumps(FLOW), encoding="utf-8")
        (self.proj / "app.ts").write_text("export const a = 1\n", encoding="utf-8")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "-m", "init")
        mapwatch._status.clear()

    def tearDown(self):
        self._t.__exit__()

    def commit_feature(self, name="feature.ts"):
        (self.proj / name).write_text("export const b = 2\n", encoding="utf-8")
        (self.proj / "README.md").write_text("docs only\n", encoding="utf-8")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "-m", f"feat: {name}")

    def test_validate_knows_decisions_and_broken_journeys(self):
        flow, warn = projmap.validate(FLOW)
        self.assertEqual(flow["nodes"][1]["kind"], "decision")
        self.assertEqual(warn, [])
        _, warn = projmap.validate({**FLOW, "flows": [{"name": "Skip", "steps": ["user", "ui"]}]})
        self.assertTrue(any("journey 'Skip'" in w for w in warn))

    def test_current_then_behind_then_launch_once(self):
        self.assertEqual(mapwatch.check(self.cfg, self.store), [])
        self.assertEqual(mapwatch.status("Alpha")["state"], "current")
        self.commit_feature()
        base, changed, _ = mapwatch.behind(self.proj)
        self.assertEqual(changed, ["feature.ts"])                  # README.md isn't source
        with patch("hub.launcher.launch", return_value=("run-1", {})) as launch:
            self.assertEqual(mapwatch.check(self.cfg, self.store), ["Alpha"])
            task = launch.call_args[0][0]
            self.assertIn("feature.ts", task)
            self.assertNotIn("[", task)
            self.assertEqual(launch.call_args.kwargs["log_runs"], False)
            self.store.upsert("run-1", status="running", source="launcher", workspace=str(self.proj))
            self.assertEqual(mapwatch.check(self.cfg, self.store), [])   # already updating
            self.assertEqual(mapwatch.status("Alpha")["state"], "updating")
        self.assertTrue((self.tmp / "map-backups" / "Alpha.json").is_file())

    def test_waits_while_busy_and_respects_switch(self):
        self.commit_feature()
        self.store.upsert("claude-x", status="running", source="hook", workspace=str(self.proj))
        with patch("hub.launcher.launch") as launch:
            self.assertEqual(mapwatch.check(self.cfg, self.store), [])
            self.assertEqual(mapwatch.status("Alpha")["waiting"], "busy")
            self.store.upsert("claude-x", status="done")
            self.cfg["maps"]["auto_update"] = False
            self.assertEqual(mapwatch.check(self.cfg, self.store), [])
            launch.assert_not_called()
        self.assertEqual(mapwatch.status("Alpha")["state"], "behind")

    def test_finish_commits_restores_or_marks_checked(self):
        self.commit_feature()
        head = git(self.proj, "rev-parse", "HEAD")
        flow = self.proj / ".brain" / "FLOW.json"
        backup = self.tmp / "backup.json"
        backup.write_bytes(flow.read_bytes())
        # a broken map goes back to the backup
        flow.write_text("{broken", encoding="utf-8")
        mapwatch.finish(self.cfg, self.proj, "run-1", "done", backup, head, "update")
        self.assertEqual(flow.read_bytes(), backup.read_bytes())
        self.assertEqual(mapwatch.status("Alpha")["result"], "invalid")
        # unchanged: nothing committed, but these commits count as checked
        mapwatch.finish(self.cfg, self.proj, "run-2", "done", backup, head, "update")
        self.assertEqual(mapwatch.status("Alpha")["result"], "unchanged")
        self.assertEqual(mapwatch.check(self.cfg, self.store), [])
        self.assertEqual(mapwatch.status("Alpha")["state"], "current")
        # a real change is committed, alone
        new = {**FLOW, "nodes": FLOW["nodes"] + [{"id": "feat", "label": "Feature", "kind": "component", "layer": "frontend"}],
               "edges": FLOW["edges"] + [{"from": "ui", "to": "feat"}]}
        flow.write_text(json.dumps(new), encoding="utf-8")
        (self.proj / "wip.ts").write_text("uncommitted work\n", encoding="utf-8")
        mapwatch.finish(self.cfg, self.proj, "run-3", "done", backup, head, "update")
        self.assertEqual(mapwatch.status("Alpha")["result"], "updated")
        self.assertEqual(git(self.proj, "log", "-1", "--format=%s"), "docs: update project map (.brain/FLOW.json)")
        self.assertEqual(git(self.proj, "show", "--name-only", "--format=", "HEAD"), ".brain/FLOW.json")
        self.assertIn("wip.ts", git(self.proj, "status", "--porcelain"))   # other work left alone


if __name__ == "__main__":
    unittest.main()
