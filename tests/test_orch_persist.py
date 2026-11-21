"""Persistence, restart recovery, worker reconciliation, pruning, and delivery semantics for hub/orch.py."""
import io
import json
import os
import stat
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import helpers
from hub import orch
from hub.store import Store


class PersistTests(unittest.TestCase):
    def setUp(self):
        self._t = helpers.TempDir()
        self.tmp = self._t.__enter__()
        self.home_patch = mock.patch("hub.orch.brand.home", return_value=Path(self.tmp))
        self.home_patch.start()
        self.cfg, _ = helpers.make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.n = 0
        self.callbacks = {}
        self.created_leads = []

        def launch(task, cfg, store, tool, tier, project, cwd, p=None, prompt=None, on_finish=None, **kw):
            role = (p or {}).get("orchestrate", {}).get("role")
            if role == "lead":
                rid = f"run-lead-{len(self.created_leads) + 1}"
                self.created_leads.append(rid)
                store.upsert(rid, source="launcher", status="running", tool=p["tool"], model=p["model"], task=task)
                return rid, p
            self.n += 1
            rid = f"run-w{self.n}"
            self.callbacks[rid] = on_finish
            store.upsert(rid, source="launcher", status="running", tool=p["tool"], model=p["model"], task=task)
            return rid, p

        self.launch_patch = mock.patch("hub.orch.launcher.launch", side_effect=launch)
        self.launch_patch.start()

    def tearDown(self):
        self.launch_patch.stop()
        self.home_patch.stop()
        orch._orchs.clear()
        self._t.__exit__()

    def start_lead(self, task="ship persistence", project="Alpha", cwd=None):
        rid, _ = orch.start(self.cfg, self.store, task, project=project, cwd=cwd)
        return rid

    def finish_cb(self, n, status="done", files=None, report="completed cleanly"):
        run = f"run-w{n}"
        log_path = Path(self.tmp) / f"{run}.log"
        log_path.write_text(f"{report}\n", encoding="utf-8")
        self.store.upsert(run, status=status, files=json.dumps(files if files is not None else ["a.py"]),
                          log_path=str(log_path), ended=time.time())
        self.callbacks[run](run, status, 0 if status == "done" else 1)

    def test_round_trip_save_and_restore_preserves_all_fields_and_permissions(self):
        lead_id = self.start_lead(task="build feature", project="Alpha", cwd="/tmp/alpha")
        r1 = orch.assign(self.cfg, self.store, lead_id, "job one", load="basic", files=["a.py"])
        self.finish_cb(r1["worker"], status="done", files=["a.py"], report="first draft")
        waited = orch.wait(self.cfg, self.store, lead_id, timeout=2)
        self.assertEqual([w["n"] for w in waited["finished"]], [1])

        orch.review(self.cfg, lead_id, 1, "rework", "fix edge case")
        r2 = orch.assign(self.cfg, self.store, lead_id, "job one", load="light", redo=1, note="fix edge case")
        self.finish_cb(r2["worker"], status="done", files=["a.py", "b.py"], report="edge case fixed")
        orch.wait(self.cfg, self.store, lead_id, timeout=2)
        orch.review(self.cfg, lead_id, 2, "accept", "looks good")
        orch.assign(self.cfg, self.store, lead_id, "job two", load="complex", tool="claude", model="sonnet")

        state_file = Path(self.tmp) / "orch" / f"{lead_id}.json"
        self.assertTrue(state_file.is_file())
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)
        self.assertEqual(list((Path(self.tmp) / "orch").glob("*.tmp")), [])

        before = orch._orchs[lead_id]
        before_workers = {n: dict(w) for n, w in before.workers.items()}
        before_next_n = before.next_n

        orch._orchs.clear()
        restored = orch.get(lead_id, self.store)
        self.assertEqual((restored.id, restored.task, restored.project, restored.cwd),
                         (lead_id, "build feature", "Alpha", "/tmp/alpha"))
        self.assertEqual(restored.next_n, before_next_n)
        for n, orig_w in before_workers.items():
            rest_w = restored.workers[n]
            for key in ("n", "task", "load", "tool", "model", "effort", "run_id", "status", "note",
                        "files", "summary", "delivered", "review", "review_note", "redo_of"):
                self.assertEqual(rest_w[key], orig_w[key], (n, key))

    def test_restart_answers_status_result_review_assign_and_wait(self):
        lead_id = self.start_lead()
        orch.assign(self.cfg, self.store, lead_id, "job 1", load="basic")
        self.finish_cb(1, status="done", files=["x.py"], report="done x")

        # Simulated server restart
        orch._orchs.clear()
        self.callbacks.clear()

        st = orch.status(self.cfg, self.store, lead_id)
        self.assertEqual(len(st["workers"]), 1)
        self.assertEqual(st["workers"][0]["status"], "done")

        res = orch.result(self.cfg, self.store, lead_id, 1)
        self.assertEqual(res["files"], ["x.py"])
        self.assertIn("done x", res["summary"])

        w_out = orch.wait(self.cfg, self.store, lead_id, timeout=1)
        self.assertEqual([w["n"] for w in w_out["finished"]], [1])

        rev = orch.review(self.cfg, lead_id, 1, "accept", "verified", store=self.store)
        self.assertTrue(rev["all_accepted"])

        r2 = orch.assign(self.cfg, self.store, lead_id, "job 2", load="light")
        self.assertEqual(r2["worker"], 2)

    def test_reconciles_workers_that_finished_while_server_was_down(self):
        lead_id = self.start_lead()
        orch.assign(self.cfg, self.store, lead_id, "job done", load="basic")
        orch.assign(self.cfg, self.store, lead_id, "job failed", load="basic")
        orch.assign(self.cfg, self.store, lead_id, "job stopped", load="basic")
        orch.assign(self.cfg, self.store, lead_id, "job fallback", load="basic")

        # Simulate server down: clear memory and callbacks, then finish rows directly in store
        orch._orchs.clear()
        self.callbacks.clear()

        log1 = Path(self.tmp) / "w1.log"
        log1.write_text("worked hard\nfinal answer for w1\n", encoding="utf-8")
        self.store.upsert("run-w1", status="done", files=json.dumps(["f1.py"]), log_path=str(log1),
                          activity="finished", ended=time.time())
        self.store.upsert("run-w2", status="failed", files="[]", activity="exit code 2", ended=time.time())
        self.store.upsert("run-w3", status="stopped", files="[]", activity="stopped by you", ended=time.time())

        # Worker 4 fell back to a child run while server was down
        log4b = Path(self.tmp) / "w4b.log"
        log4b.write_text("fallback finished\n", encoding="utf-8")
        self.store.upsert("run-w4", status="failed", activity="exit code 1 (fallback to claude:sonnet)",
                          ended=time.time() - 5)
        self.store.upsert("run-w4b", source="launcher", status="done", tool="claude", model="sonnet",
                          parent_run_id="run-w4", started=time.time(), files=json.dumps(["f4.py"]),
                          log_path=str(log4b), activity="finished", ended=time.time())

        # Calling status first reconciles them, and wait still delivers each once
        st = orch.status(self.cfg, self.store, lead_id)
        by_n = {w["n"]: w for w in st["workers"]}
        self.assertEqual(by_n[1]["status"], "done")
        self.assertEqual(by_n[2]["status"], "failed")
        self.assertEqual(by_n[3]["status"], "stopped")
        self.assertEqual((by_n[4]["status"], by_n[4]["run_id"], by_n[4]["tool"], by_n[4]["model"]),
                         ("done", "run-w4b", "claude", "sonnet"))

        w_out = orch.wait(self.cfg, self.store, lead_id, timeout=1)
        self.assertEqual([w["n"] for w in w_out["finished"]], [1, 2, 3, 4])
        fin = {w["n"]: w for w in w_out["finished"]}
        self.assertIn("final answer for w1", fin[1]["summary"])
        self.assertEqual(fin[1]["files"], ["f1.py"])
        self.assertEqual(fin[4]["files"], ["f4.py"])
        self.assertIn("fallback finished", fin[4]["summary"])

        # Subsequent wait sees no undelivered workers and is idle
        self.assertTrue(orch.wait(self.cfg, self.store, lead_id, timeout=1)["idle"])

    def test_wait_polls_store_for_still_running_worker_after_restart(self):
        lead_id = self.start_lead()
        orch.assign(self.cfg, self.store, lead_id, "slow job", load="basic")

        # Restart wipes in-memory _orchs and on_finish callbacks
        orch._orchs.clear()
        self.callbacks.clear()

        def finish_later():
            log1 = Path(self.tmp) / "polled.log"
            log1.write_text("polled output\n", encoding="utf-8")
            self.store.upsert("run-w1", status="done", files=json.dumps(["polled.py"]),
                              log_path=str(log1), activity="finished", ended=time.time())

        t = threading.Timer(0.2, finish_later)
        t.start()
        try:
            r = orch.wait(self.cfg, self.store, lead_id, timeout=5)
        finally:
            t.cancel()
        self.assertEqual([w["n"] for w in r["finished"]], [1])
        self.assertEqual(r["finished"][0]["status"], "done")
        self.assertEqual(r["finished"][0]["files"], ["polled.py"])
        self.assertIn("polled output", r["finished"][0]["summary"])

    def test_prunes_orchestrations_finished_more_than_7_days_ago_at_restore(self):
        now = time.time()
        old_lead = self.start_lead("old done task")
        old_stopped = self.start_lead("old stopped task")
        recent_lead = self.start_lead("recent task")
        active_lead = self.start_lead("active task")

        self.store.upsert(old_lead, status="done", ended=now - 8 * 86400)
        self.store.upsert(old_stopped, status="stopped", ended=now - 9 * 86400)
        self.store.upsert(recent_lead, status="done", ended=now - 3 * 86400)

        orch_dir = Path(self.tmp) / "orch"
        for lid in (old_lead, old_stopped, recent_lead, active_lead):
            self.assertTrue((orch_dir / f"{lid}.json").is_file())

        orch._orchs.clear()
        orch.status(self.cfg, self.store, active_lead)

        self.assertFalse((orch_dir / f"{old_lead}.json").exists())
        self.assertFalse((orch_dir / f"{old_stopped}.json").exists())
        self.assertTrue((orch_dir / f"{recent_lead}.json").is_file())
        self.assertTrue((orch_dir / f"{active_lead}.json").is_file())

        # Restoring an expired lead directly deletes its own file and raises ValueError
        self.store.upsert(recent_lead, status="done", ended=now - 8 * 86400)
        with self.assertRaises(ValueError):
            orch.get(recent_lead, self.store)
        self.assertFalse((orch_dir / f"{recent_lead}.json").exists())

    def test_corrupt_and_invalid_files_are_ignored_safely(self):
        orch_dir = Path(self.tmp) / "orch"
        orch_dir.mkdir(parents=True, exist_ok=True)
        (orch_dir / "bad-json.json").write_text("{broken", encoding="utf-8")
        (orch_dir / "bad-type.json").write_text("[1, 2, 3]", encoding="utf-8")
        (orch_dir / "bad-schema.json").write_text(
            json.dumps({"id": "bad-schema", "task": "t", "workers": []}), encoding="utf-8"
        )
        valid_id = self.start_lead("still good")
        orch._orchs.clear()

        err = io.StringIO()
        with redirect_stderr(err):
            for bad in ("bad-json", "bad-type", "bad-schema", "../escape"):
                with self.assertRaises(ValueError):
                    orch.get(bad, self.store)
        self.assertIn("ignoring corrupt state", err.getvalue())
        self.assertEqual(orch.status(self.cfg, self.store, valid_id)["task"], "still good")

    def test_wait_does_not_repeat_delivered_or_reviewed_workers(self):
        lead_id = self.start_lead()
        orch.assign(self.cfg, self.store, lead_id, "job 1", load="basic")
        orch.assign(self.cfg, self.store, lead_id, "job 2", load="basic")
        self.finish_cb(1, status="done")

        r1 = orch.wait(self.cfg, self.store, lead_id, timeout=2)
        self.assertEqual([w["n"] for w in r1["finished"]], [1])
        orch.review(self.cfg, lead_id, 1, "accept", "good", store=self.store)

        # Worker 2 is still running: worker 1 (delivered + accepted) must not be repeated
        r_to = orch.wait(self.cfg, self.store, lead_id, timeout=1)
        self.assertTrue(r_to.get("timeout"))
        self.assertEqual(r_to["finished"], [])

        # Restart after worker 1 accepted: worker 1 still must not be returned by wait
        orch._orchs.clear()
        self.callbacks.clear()
        r_to2 = orch.wait(self.cfg, self.store, lead_id, timeout=1)
        self.assertTrue(r_to2.get("timeout"))
        self.assertEqual(r_to2["finished"], [])

        # Worker 2 finishes; review it BEFORE calling wait -> wait must not report an already-reviewed worker
        self.store.upsert("run-w2", status="done", files="[]", ended=time.time())
        st = orch.status(self.cfg, self.store, lead_id)
        self.assertEqual({w["n"]: w["status"] for w in st["workers"]}, {1: "done", 2: "done"})
        orch.review(self.cfg, lead_id, 2, "accept", "good", store=self.store)
        r_idle = orch.wait(self.cfg, self.store, lead_id, timeout=1)
        self.assertTrue(r_idle.get("idle"))
        self.assertEqual(r_idle["finished"], [])

    def test_briefs_require_foreground_commands_and_report_before_ending_turn(self):
        wb = orch.worker_brief(1, "lead-1").lower()
        lb = orch.lead_brief(self.cfg).lower()
        for brief in (wb, lb):
            self.assertIn("foreground", brief)
            self.assertIn("background task or monitor", brief)
            self.assertIn("your turn ending is the end of your job", brief)
            self.assertIn("written report", brief)


if __name__ == "__main__":
    unittest.main()
