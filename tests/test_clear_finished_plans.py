"""Clear finished hides finished plans from the dashboard lists (queue, Workflows library) without deleting them."""
import unittest

from helpers import TempDir, make_cfg
from hub import queue_api, runs_api
from hub.store import Store
from hub.tasks import Tasks


class ClearFinishedPlans(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.cfg, _ = make_cfg(self._t.__enter__())
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)
        task = {"id": "t1", "title": "x", "prompt": "do x", "level": "light", "tool": "agy", "model": "m",
                "effort": "", "files": [], "depends_on": [], "acceptance": ""}
        self.done = self.tasks.add_plan("finished goal", "proj", [dict(task)], planner="mock")
        self.live = self.tasks.add_plan("live goal", "proj", [dict(task)], planner="mock")
        self.store.db.execute("UPDATE plans SET status='done' WHERE id=?", (self.done,))
        self.store.db.execute("UPDATE plans SET status='running' WHERE id=?", (self.live,))
        self.store.db.commit()

    def tearDown(self):
        self._t.__exit__()

    def ids(self, **kw):
        return [p["id"] for p in self.tasks.plans(limit=10, **kw)]

    def test_clear_hides_only_finished_plans_from_visible_lists(self):
        self.tasks.clear_finished()
        self.assertEqual(self.ids(visible=True), [self.live])
        self.assertCountEqual(self.ids(), [self.done, self.live])          # still there for jobs and direct links
        self.assertIsNotNone(self.tasks.plan(self.done))
        q = queue_api.queue_state(self.cfg, self.tasks, None)
        self.assertEqual([p["id"] for p in q["plans"]], [self.live])
        listed = [r["id"] for r in runs_api.recent(self.cfg, self.store, self.tasks)]
        self.assertNotIn(self.done, listed)

    def test_a_cleared_plan_that_runs_again_is_listed_again(self):
        self.tasks.clear_finished()
        self.store.db.execute("UPDATE plans SET status='running' WHERE id=?", (self.done,))
        self.store.db.commit()
        self.assertIn(self.done, self.ids(visible=True))


if __name__ == "__main__":
    unittest.main()
