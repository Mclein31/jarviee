"""Round-2 acceptance: the complex-job rule as the goal states it
("more than one step, more than one agent, or longer than N seconds"), with N from config."""
import json
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import jobs, jobs_api
from hub.store import Store
from hub.tasks import Tasks

ROOT = Path(__file__).resolve().parent.parent


class ComplexRuleMatchesGoal(unittest.TestCase):
    def test_config_json_ships_the_settings(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        for key in ("complex_job_duration_seconds", "complex_job_min_steps"):
            self.assertIsInstance(cfg[key], int)

    def test_one_step_one_agent_short_is_not_complex(self):
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude"], duration=5))

    def test_more_than_one_step_is_complex_with_defaults(self):
        # Goal: "more than one step" -> exactly 2 steps must already be complex.
        self.assertTrue(jobs.is_complex(steps=2, tools=["claude"], duration=5))

    def test_more_than_one_agent_is_complex(self):
        self.assertTrue(jobs.is_complex(steps=1, tools=["claude", "agy"], duration=5))

    def test_longer_than_configured_seconds_is_complex(self):
        self.assertTrue(jobs.is_complex(steps=1, tools=["claude"], duration=61))
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude"], duration=61, cfg={"complex_job_duration_seconds": 100}))

    def test_two_step_run_is_listed_by_the_endpoint_with_default_config(self):
        with TempDir() as tmp:
            cfg, _ = make_cfg(tmp)
            store = Store(cfg["db_path"])
            store.upsert("claude-twostep1", source="hook", tool="claude", status="running", steps=2, started=time.time() - 5)
            ids = [j["id"] for j in jobs_api.listing(cfg, store, Tasks(store))["jobs"]]
            self.assertIn("claude-twostep1", ids)


if __name__ == "__main__":
    unittest.main()
