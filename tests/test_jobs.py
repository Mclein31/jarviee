"""Unit tests for hub/jobs.py.

Covers:
- Job grouping by stable (id, job_id) across runs, plans, and terminal sessions
- Pipeline and task linking without cross-job mixing
- Complexity evaluation based on steps, multi-tool, duration, and custom config
- Pane reuse isolation via ingest.new_job
- Strict exclusion of jobs marked closed_by_user
"""
import time
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import ingest, jobs
from hub.store import Store
from hub.tasks import Tasks


class TestJobGrouping(unittest.TestCase):
    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self._td.__exit__()

    def test_stable_key_tuple_and_string(self):
        """Verifies stable (id, job_id) tuple and string key representation."""
        k = jobs.key_of({"id": "run-abc", "job_id": "job-123"})
        self.assertEqual(k, ("run-abc", "job-123"))

        j = jobs.Job({"id": "claude-term1", "job_id": "job-456"})
        self.assertEqual(j.key, ("claude-term1", "job-456"))
        self.assertEqual(j.key_str, "claude-term1#job-456")
        self.assertEqual(j["key"], ("claude-term1", "job-456"))
        self.assertEqual(j["key_str"], "claude-term1#job-456")

    def test_aggregate_runs_and_terminal_sessions(self):
        """Groups standalone runs and terminal sessions with their stable keys."""
        # 1. Standalone launcher run
        self.store.upsert(
            "run-101",
            source="launcher",
            tool="claude",
            model="sonnet",
            task="Optimize database indexing",
            title="Optimize indexing",
            status="running",
            job_id="job-r101",
            steps=1,
            started=time.time() - 10,
        )

        # 2. Terminal hook session
        self.store.upsert(
            "claude-term1",
            source="hook",
            tool="claude",
            model="sonnet",
            task="Refactor routes",
            title="Refactor routes",
            status="waiting",
            activity="turn finished, waiting for you",
            job_id="job-t1",
            steps=2,
            started=time.time() - 20,
        )

        all_jobs = jobs.aggregate_jobs(self.store, self.tasks, self.cfg)
        self.assertEqual(len(all_jobs), 2)

        j_run = next(j for j in all_jobs if j.id == "run-101")
        self.assertEqual(j_run.job_id, "job-r101")
        self.assertEqual(j_run.key, ("run-101", "job-r101"))
        self.assertEqual(j_run.kind, "run")
        self.assertEqual(j_run.tool, "claude")
        self.assertEqual(j_run.steps, 1)

        j_term = next(j for j in all_jobs if j.id == "claude-term1")
        self.assertEqual(j_term.job_id, "job-t1")
        self.assertEqual(j_term.key, ("claude-term1", "job-t1"))
        self.assertEqual(j_term.kind, "terminal")
        self.assertEqual(j_term.tool, "claude")
        self.assertEqual(j_term.steps, 2)

    def test_plan_tasks_and_steps_linked_without_mixing_pipelines(self):
        """Links each job's tasks and steps without mixing pipelines across jobs."""
        # Plan 1 (e.g. Pipeline 1)
        tasks_p1 = [
            {"id": "t1", "title": "Research A", "tool": "claude", "model": "sonnet", "level": "complex", "prompt": "p1"},
            {"id": "t2", "title": "Build A", "tool": "agy", "model": "gemini", "level": "light", "prompt": "p2"},
        ]
        pid1 = self.tasks.add_plan("Goal Alpha", "Alpha", tasks_p1, planner="claude", status="running", pipeline={"phase": "build"})
        t1_rows = self.tasks.list(plan_id=pid1)

        # Runs for Plan 1 tasks
        self.store.upsert(
            "run-p1-t1",
            source="launcher",
            tool="claude",
            status="done",
            steps=4,
            reasons=f'["plan task {t1_rows[0]["id"]}"]',
            started=time.time() - 50,
            ended=time.time() - 30,
        )
        self.tasks.set(t1_rows[0]["id"], run_id="run-p1-t1", status="done")

        self.store.upsert(
            "run-p1-t2",
            source="launcher",
            tool="agy",
            status="running",
            steps=3,
            reasons=f'["plan task {t1_rows[1]["id"]}"]',
            started=time.time() - 25,
        )
        self.tasks.set(t1_rows[1]["id"], run_id="run-p1-t2", status="running")

        # Plan 2 (e.g. Pipeline 2)
        tasks_p2 = [
            {"id": "t1", "title": "Research B", "tool": "claude", "model": "sonnet", "level": "complex", "prompt": "p3"},
        ]
        pid2 = self.tasks.add_plan("Goal Beta", "Beta", tasks_p2, planner="claude", status="running", pipeline={"phase": "research"})
        t2_rows = self.tasks.list(plan_id=pid2)

        # Run for Plan 2 task
        self.store.upsert(
            "run-p2-t1",
            source="launcher",
            tool="claude",
            status="running",
            steps=2,
            reasons=f'["plan task {t2_rows[0]["id"]}"]',
            started=time.time() - 15,
        )
        self.tasks.set(t2_rows[0]["id"], run_id="run-p2-t1", status="running")

        # Standalone Run (not in any plan)
        self.store.upsert(
            "run-standalone",
            source="launcher",
            tool="claude",
            status="running",
            steps=1,
            started=time.time() - 5,
            job_id="job-solo",
        )

        all_jobs = jobs.aggregate_jobs(self.store, self.tasks, self.cfg)

        # Exactly 3 jobs: Plan 1, Plan 2, and Standalone Run
        # The runs belonging to Plan 1 and Plan 2 are NOT duplicated as standalone jobs
        self.assertEqual(len(all_jobs), 3)

        job_p1 = next(j for j in all_jobs if j.id == pid1)
        job_p2 = next(j for j in all_jobs if j.id == pid2)
        job_solo = next(j for j in all_jobs if j.id == "run-standalone")

        # Verify Plan 1: steps = 4 + 3 = 7, tools = {"claude", "agy"}
        self.assertEqual(job_p1.kind, "pipeline")
        self.assertEqual(job_p1.steps, 7)
        self.assertEqual(sorted(job_p1.tools), ["agy", "claude"])
        self.assertEqual(len(job_p1.tasks), 2)
        self.assertEqual(len(job_p1.runs), 2)

        # Verify Plan 2: steps = 2, tools = {"claude"}
        self.assertEqual(job_p2.kind, "pipeline")
        self.assertEqual(job_p2.steps, 2)
        self.assertEqual(job_p2.tools, ["claude"])
        self.assertEqual(len(job_p2.tasks), 1)
        self.assertEqual(len(job_p2.runs), 1)

        # Verify Standalone Run: steps = 1, tools = {"claude"}
        self.assertEqual(job_solo.kind, "run")
        self.assertEqual(job_solo.steps, 1)
        self.assertEqual(job_solo.tools, ["claude"])

        # Pipelines are not mixed: Plan 2 has none of Plan 1's steps or tools
        self.assertNotIn("agy", job_p2.tools)
        self.assertEqual(job_p2.steps, 2)


class TestJobComplexityEvaluation(unittest.TestCase):
    def test_step_count_threshold_default(self):
        """Steps >= complex_job_min_steps (default 2: more than one step) triggers complexity."""
        # Steps <= 2 -> not complex
        self.assertFalse(jobs.is_complex(steps=0, tools=["claude"], duration=10))
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude"], duration=10))
        self.assertTrue(jobs.is_complex(steps=2, tools=["claude"], duration=10))

        # Steps > 2 -> complex
        self.assertTrue(jobs.is_complex(steps=3, tools=["claude"], duration=10))
        self.assertTrue(jobs.is_complex(steps=10, tools=["claude"], duration=10))

    def test_multi_tool_threshold(self):
        """> 1 agent tool triggers complexity regardless of steps and duration."""
        # 1 tool -> not complex (if steps and duration below thresholds)
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude"], duration=10))
        self.assertFalse(jobs.is_complex(steps=1, tools=["agy"], duration=10))
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude", "claude"], duration=10))

        # > 1 tool -> complex
        self.assertTrue(jobs.is_complex(steps=1, tools=["claude", "agy"], duration=10))
        self.assertTrue(jobs.is_complex(steps=0, tools=["claude", "gemini", "agy"], duration=5))

    def test_duration_threshold_default(self):
        """Duration > complex_job_duration_seconds (default 60) triggers complexity."""
        # Duration <= 60 -> not complex
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude"], duration=30))
        self.assertFalse(jobs.is_complex(steps=1, tools=["claude"], duration=60.0))

        # Duration > 60 -> complex
        self.assertTrue(jobs.is_complex(steps=1, tools=["claude"], duration=60.1))
        self.assertTrue(jobs.is_complex(steps=1, tools=["claude"], duration=120))

    def test_custom_config_thresholds(self):
        """Custom configured thresholds are respected."""
        custom_cfg = {
            "complex_job_min_steps": 5,
            "complex_job_duration_seconds": 180,
        }

        # Below custom thresholds: steps 4 <= 5, duration 150 <= 180
        self.assertFalse(jobs.is_complex(steps=4, tools=["claude"], duration=150, cfg=custom_cfg))

        # Exceeds custom steps: 6 > 5
        self.assertTrue(jobs.is_complex(steps=6, tools=["claude"], duration=10, cfg=custom_cfg))

        # Exceeds custom duration: 181 > 180
        self.assertTrue(jobs.is_complex(steps=2, tools=["claude"], duration=181, cfg=custom_cfg))

    def test_evaluation_on_job_object(self):
        """Evaluates complexity directly on a Job instance."""
        j1 = jobs.Job({"id": "j1", "steps": 1, "tools": ["claude"], "duration": 15})
        j1.is_complex = jobs.is_complex(j1)
        self.assertFalse(j1.is_complex)

        j2 = jobs.Job({"id": "j2", "steps": 5, "tools": ["claude"], "duration": 15})
        j2.is_complex = jobs.is_complex(j2)
        self.assertTrue(j2.is_complex)


class TestPaneReuseIsolation(unittest.TestCase):
    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        self._td.__exit__()

    def test_pane_reuse_resets_counters_and_isolates_jobs(self):
        """Counters reset when a new job starts in the same pane using ingest.new_job."""
        pane_id = "pane-reuse-1"
        log = Path(self.tmp) / "pane.log"
        log.write_text("initial output", encoding="utf-8")

        # 1. First job in the pane
        self.store.upsert(
            pane_id,
            source="launcher",
            tool="claude",
            task="First complex task",
            title="First task",
            status="done",
            steps=8,
            started=time.time() - 100,
            ended=time.time() - 20,
            job_id="job-first",
            job_no=1,
            log_path=str(log),
        )

        first_job = jobs.get_job(pane_id, self.store, cfg=self.cfg)
        self.assertIsNotNone(first_job)
        self.assertEqual(first_job.job_id, "job-first")
        self.assertEqual(first_job.steps, 8)
        self.assertTrue(first_job.is_complex)

        # 2. Start a new job in the same pane using ingest.new_job
        existing = self.store.get(pane_id)
        new_fields = ingest.new_job(existing, launched=False)

        # Counters must be reset by ingest.new_job
        self.assertNotEqual(new_fields["job_id"], "job-first")
        self.assertEqual(new_fields["job_no"], 2)
        self.assertEqual(new_fields["steps"], 0)
        self.assertIsNone(new_fields["ended"])

        # Update store with new job fields and prompt
        self.store.upsert(
            pane_id,
            status="running",
            task="Second small task",
            title="Second task",
            **new_fields,
        )

        # 3. Aggregate jobs: verify the new job is completely isolated
        all_jobs = jobs.aggregate_jobs(self.store, cfg=self.cfg)
        self.assertEqual(len(all_jobs), 1)

        second_job = all_jobs[0]
        self.assertEqual(second_job.id, pane_id)
        self.assertEqual(second_job.job_id, new_fields["job_id"])
        self.assertEqual(second_job.key, (pane_id, new_fields["job_id"]))
        self.assertEqual(second_job.steps, 0)
        self.assertEqual(second_job.title, "Second task")

        # The new job is NOT complex because steps were reset to 0
        self.assertFalse(second_job.is_complex)


class TestClosedJobExclusion(unittest.TestCase):
    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self._td.__exit__()

    def test_run_closed_by_user_is_excluded(self):
        """Runs with closed_by_user=1 are excluded from aggregated jobs."""
        self.store.upsert(
            "run-active",
            source="launcher",
            tool="claude",
            status="running",
            job_id="job-act",
            closed_by_user=0,
        )
        self.store.upsert(
            "run-closed",
            source="launcher",
            tool="claude",
            status="stopped",
            job_id="job-cls",
            closed_by_user=1,
        )

        all_jobs = jobs.aggregate_jobs(self.store, cfg=self.cfg)
        ids = [j.id for j in all_jobs]
        self.assertIn("run-active", ids)
        self.assertNotIn("run-closed", ids)

    def test_terminal_session_closed_by_user_is_excluded(self):
        """Terminal sessions with closed_by_user=1 are excluded."""
        self.store.upsert(
            "claude-term-live",
            source="hook",
            tool="claude",
            status="waiting",
            job_id="job-tlive",
            closed_by_user=0,
        )
        self.store.upsert(
            "claude-term-killed",
            source="hook",
            tool="claude",
            status="stopped",
            job_id="job-tkill",
            closed_by_user=1,
        )

        all_jobs = jobs.aggregate_jobs(self.store, cfg=self.cfg)
        ids = [j.id for j in all_jobs]
        self.assertIn("claude-term-live", ids)
        self.assertNotIn("claude-term-killed", ids)

    def test_plan_closed_by_user_is_excluded(self):
        """Plans with closed_by_user or cancelled status or runs closed_by_user are excluded."""
        pid_open = self.tasks.add_plan(
            "Open plan",
            "Alpha",
            [{"id": "t1", "title": "T1", "tool": "claude", "model": "sonnet", "level": "light", "prompt": "p"}],
            status="running",
        )
        pid_closed = self.tasks.add_plan(
            "Closed plan",
            "Alpha",
            [{"id": "t2", "title": "T2", "tool": "claude", "model": "sonnet", "level": "light", "prompt": "p"}],
            status="cancelled",
        )
        t2_rows = self.tasks.list(plan_id=pid_closed)
        self.store.upsert(
            "run-p-closed",
            source="launcher",
            tool="claude",
            status="stopped",
            closed_by_user=1,
            reasons=f'["plan task {t2_rows[0]["id"]}"]',
        )
        self.tasks.set(t2_rows[0]["id"], run_id="run-p-closed", status="cancelled")

        all_jobs = jobs.aggregate_jobs(self.store, self.tasks, cfg=self.cfg)
        ids = [j.id for j in all_jobs]
        self.assertIn(pid_open, ids)
        self.assertNotIn(pid_closed, ids)

        # Also verify with a plan dict that explicitly has closed_by_user=True
        mock_tasks = type("MockTasks", (), {
            "open_plans": lambda self: [
                {"id": "plan-mock-open", "goal": "Open", "status": "running", "closed_by_user": False, "tasks": []},
                {"id": "plan-mock-closed", "goal": "Closed", "status": "running", "closed_by_user": True, "tasks": []},
            ],
            "plans": lambda self, limit=10: [],
            "list": lambda self, plan_id: [],
        })()
        mock_jobs = jobs.aggregate_jobs(None, mock_tasks, cfg=self.cfg)
        mock_ids = [j.id for j in mock_jobs]
        self.assertIn("plan-mock-open", mock_ids)
        self.assertNotIn("plan-mock-closed", mock_ids)


class TestJobStatusAndLookups(unittest.TestCase):
    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        self._td.__exit__()

    def test_job_status_mapping(self):
        """Maps various raw status inputs to normalized job statuses."""
        self.assertEqual(jobs.job_status({"status": "running"}), "running")
        self.assertEqual(jobs.job_status({"status": "waiting"}), "waiting")
        self.assertEqual(jobs.job_status({"status": "draft"}), "queued")
        self.assertEqual(jobs.job_status({"status": "approved"}), "queued")
        self.assertEqual(jobs.job_status({"status": "queued"}), "queued")
        self.assertEqual(jobs.job_status({"status": "done"}), "done")
        self.assertEqual(jobs.job_status({"status": "failed"}), "failed")
        self.assertEqual(jobs.job_status({"status": "stale"}), "failed")
        self.assertEqual(jobs.job_status({"status": "stopped"}), "stopped")
        self.assertEqual(jobs.job_status({"status": "cancelled"}), "stopped")
        self.assertEqual(jobs.job_status({"status": "running", "closed_by_user": 1}), "stopped")

    def test_outcome_of_agreement(self):
        """outcome_of matches batch report expectations."""
        self.assertIsNone(jobs.outcome_of({"status": "running"}))
        self.assertIsNone(jobs.outcome_of({"status": "queued"}))
        self.assertEqual(jobs.outcome_of({"status": "done"}), "done")
        self.assertEqual(jobs.outcome_of({"status": "failed"}), "failed")
        self.assertEqual(jobs.outcome_of({"status": "stopped"}), "closed")
        self.assertEqual(jobs.outcome_of({"status": "running", "closed_by_user": 1}), "closed")
        self.assertEqual(jobs.outcome_of({"status": "waiting", "activity": "turn finished, waiting for you"}), "replied")
        self.assertEqual(jobs.outcome_of({"status": "waiting", "activity": "asking confirmation"}), "needs")

    def test_get_job_lookup(self):
        """Retrieves jobs by tuple key, string key, or id."""
        self.store.upsert("run-lookup", source="launcher", tool="claude", status="running", job_id="job-target")

        # By tuple key
        j1 = jobs.get_job(("run-lookup", "job-target"), self.store, cfg=self.cfg)
        self.assertIsNotNone(j1)
        self.assertEqual(j1.id, "run-lookup")
        self.assertEqual(j1.job_id, "job-target")

        # By composite key string
        j2 = jobs.get_job("run-lookup#job-target", self.store, cfg=self.cfg)
        self.assertIsNotNone(j2)
        self.assertEqual(j2.id, "run-lookup")

        # By id
        j3 = jobs.get_job("run-lookup", self.store, cfg=self.cfg)
        self.assertIsNotNone(j3)
        self.assertEqual(j3.id, "run-lookup")

        # Non-existent job
        self.assertIsNone(jobs.get_job("no-such-job", self.store, cfg=self.cfg))

    def test_finished_job_retention_window(self):
        """Finished jobs are retained within finished_job_retention_seconds and dropped when expired."""
        now = 10000.0
        # Ended 100s ago (within 300s default retention)
        self.store.upsert(
            "run-recent-done",
            source="launcher",
            tool="claude",
            status="done",
            job_id="job-recent",
            started=now - 200,
            ended=now - 100,
        )
        # Ended 500s ago (beyond 300s default retention)
        self.store.upsert(
            "run-old-done",
            source="launcher",
            tool="claude",
            status="done",
            job_id="job-old",
            started=now - 600,
            ended=now - 500,
        )

        all_jobs = jobs.aggregate_jobs(self.store, cfg=self.cfg, now=now)
        ids = [j.id for j in all_jobs]
        self.assertIn("run-recent-done", ids)
        self.assertNotIn("run-old-done", ids)


if __name__ == "__main__":
    unittest.main()
