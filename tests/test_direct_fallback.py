import json
import os
import sys
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import launcher, store as hub_store


class DirectRunFallbackTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        import subprocess
        subprocess.run(["git", "init"], cwd=self.tmp, capture_output=True)
        self.cfg, _ = make_cfg(self.tmp)
        self.store = hub_store.Store(str(Path(self.tmp) / "hub.db"))

    def tearDown(self):
        self._t.__exit__()

    def test_direct_run_429_auto_fallback_and_warning_prepend(self):
        # We simulate a run that outputs 429 quota exhausted and touches files
        # Then launcher should auto-fallback to claude haiku
        fake_agy = (
            "import sys, pathlib; "
            "pathlib.Path('touched.txt').write_text('dirty'); "
            "print('error: Individual quota reached. Resets in 151h36m47s.\\n"
            "AGY_ERROR: {\"short_error\":\"RESOURCE_EXHAUSTED (code 429)\",\"status\":\"RESOURCE_EXHAUSTED\",\"error_code\":429}'); "
            "sys.exit(3)"
        )
        fake_claude = (
            "import sys; "
            "print('fallback agent received prompt: ' + sys.argv[2]); "
            "sys.exit(0)"
        )
        self.cfg["tools"]["agy"]["command"] = [sys.executable, "-c", fake_agy, "agy", "{prompt}", "{model}"]
        self.cfg["tools"]["claude"]["command"] = [sys.executable, "-c", fake_claude, "claude", "{prompt}", "{model}"]

        # Launch run on agy basic (flash)
        task_text = "fix typo in readme"
        rid, p = launcher.launch(task_text, self.cfg, self.store, tool="agy", tier="basic", cwd=self.tmp)
        self.assertTrue(launcher.wait(rid, timeout=10))

        # Check that the first run failed
        row1 = self.store.get(rid)
        self.assertEqual(row1["status"], "failed")
        self.assertEqual(row1["tool"], "agy")

        # Check quota_state in store: agy must now be marked exhausted
        q = self.store.get_quota("agy", "gemini-3.8-flash-low")
        self.assertEqual(q["status"], "exhausted")

        # Now check if fallback run was launched
        # In store, there should be another agent with parent_run_id == rid
        with self.store.db as db:
            row2 = db.execute("SELECT * FROM agents WHERE parent_run_id=?", (rid,)).fetchone()
        self.assertIsNotNone(row2, "Fallback run was not launched!")
        self.assertEqual(row2["tool"], "claude")
        self.assertEqual(row2["model"], "haiku")
        self.assertEqual(row2["fallback_from"], "agy:gemini-3.8-flash-low")
        self.assertEqual(row2["hops"], 1)

        # Wait for fallback run to complete
        self.assertTrue(launcher.wait(row2["id"], timeout=10))
        fb_agent = self.store.get(row2["id"])
        self.assertEqual(fb_agent["status"], "done")

        # Check that prompt contains warning because touched.txt changed git status
        fb_log = Path(fb_agent["log_path"]).read_text()
        self.assertIn("A previous attempt on another model may have partially changed files", fb_log)


if __name__ == "__main__":
    unittest.main()
