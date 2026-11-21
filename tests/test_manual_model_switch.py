"""A model switched by hand in a live pane (/model) shows on the dashboard (hub/ingest.py, hub/dashstate.py).

The owner's report (2026-11-16): switching the model in an open terminal did not sync with Jarviee's routing. A
launched row ignored every hook model, so a live session kept showing the model it was launched with.
"""
import unittest
from unittest import mock

from helpers import TempDir, make_cfg
from hub import dashstate, launcher
from hub.ingest import tier_of_model
from hub.server import handle_hook
from hub.store import Store


class ManualModelSwitch(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])

    def tearDown(self):
        launcher.LIVE_RUNS.discard("run-live1")
        self._t.__exit__()

    def _launch(self, rid, tool, model, live):
        self.store.upsert(rid, source="launcher", tool=tool, status="running", task="x", model=model,
                          tier="light", interactive=1 if live else 0, session_id="11111111-2222-3333-4444-555555555555")
        if live:
            launcher.LIVE_RUNS.add(rid)

    def test_a_live_pane_follows_a_hook_model_switch(self):
        self._launch("run-live1", "agy", "gemini-3.8-flash-low", live=True)
        handle_hook("PreInvocation", {"jarvieeRunId": "run-live1", "jarvieeTool": "agy", "modelName": "Argon"},
                    self.cfg, self.store)
        row = self.store.get("run-live1")
        self.assertEqual(row["model"], "argon")
        self.assertEqual(row["tier"], tier_of_model("argon", self.cfg, effort=row.get("effort") or ""))

    def test_a_print_run_keeps_its_launch_routing(self):
        self._launch("run-print1", "agy", "gemini-3.8-flash-low", live=False)
        handle_hook("PreInvocation", {"jarvieeRunId": "run-print1", "jarvieeTool": "agy", "modelName": "Argon"},
                    self.cfg, self.store)
        row = self.store.get("run-print1")
        self.assertEqual((row["model"], row["tier"]), ("gemini-3.8-flash-low", "light"))

    def test_a_live_claude_pane_reads_the_switch_from_its_transcript(self):
        self._launch("run-live1", "claude", "sonnet", live=True)
        with mock.patch.object(dashstate, "transcript_model", return_value="claude-opus-5-5") as tm:
            agents = dashstate.state(self.cfg, self.store)["agents"]
        tm.assert_any_call("11111111-2222-3333-4444-555555555555")
        row = next(a for a in agents if a["id"] == "run-live1")
        self.assertEqual(row["model"], "claude-opus-5-5")
        self.assertEqual(self.store.get("run-live1")["model"], "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
