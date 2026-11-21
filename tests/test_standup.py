import tempfile
import time
import unittest
from pathlib import Path

from hub import standup
from hub.config import ROOT, load_config
from hub.store import Store


class StandupTests(unittest.TestCase):
    def test_counts_per_project_and_date_check(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(str(Path(d, "t.db")))
            cfg, now = load_config(ROOT / "config.json"), time.time()
            st.upsert("run-1", project="demo", status="done", started=now - 600, ended=now - 60, title="a")
            st.upsert("run-2", project="demo", status="failed", started=now - 300, ended=now - 30, title="b")
            s = standup.standup(cfg, st, now=now)
            p = s["projects"]["demo"]
            self.assertEqual((p["done"], p["failed"]), (1, 1))
            self.assertEqual(s["totals"]["seconds"], 540 + 270)
            self.assertIn("1 done", standup.render(s))
            with self.assertRaises(ValueError):
                standup.standup(cfg, st, date="nope")


if __name__ == "__main__":
    unittest.main()
