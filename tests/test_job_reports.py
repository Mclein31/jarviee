"""Each job in a terminal gets its own id, and its report is built only from that job's output.

The bug: ask a pane to summarise how cats sleep, then how dogs sleep, and the second report was about cats again.
The row id is per terminal, and the spoken summary was cached per (row, status) and read from the whole log.
"""
import time
import types
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import voice
from hub.server import handle_hook
from hub.store import Store


def fake_llm(argv, prompt):
    """Stands in for the `claude -p` summariser: says which animals appear in the output tail it was given."""
    tail = prompt.split("OUTPUT TAIL:", 1)[1].lower()
    seen = [a for a in ("cats", "dogs") if a in tail]
    return types.SimpleNamespace(returncode=0, stdout="The agent summarised how " + " and ".join(seen) + " sleep.")


class TwoJobsOnePane(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        voice._TLDR_CACHE.clear()

    def tearDown(self):
        self._t.__exit__()

    def hook(self, event, rid, **data):
        return handle_hook(event, {"jarvieeRunId": rid, "jarvieeTool": "claude", **data}, self.cfg, self.store)

    def write(self, log, text):
        with open(log, "a", encoding="utf-8") as f:
            f.write(text)

    def test_second_report_covers_only_the_second_job_in_a_live_pane(self):
        log = Path(self.tmp) / "run-pane1.log"
        log.write_text("$ claude --model sonnet\n\n", encoding="utf-8")
        # a launched live pane run, as launcher.launch records it (job 1 opened by the launch)
        self.store.upsert("run-pane1", source="launcher", tool="claude", task="summarise how cats sleep",
                          title="Summarise how cats sleep", status="running", log_path=str(log),
                          job_id="job-first", job_no=0, job_log_offset=0)
        self.hook("UserPromptSubmit", "run-pane1", prompt="<brain notes>\nsummarise how cats sleep")
        a = self.store.get("run-pane1")
        self.assertEqual((a["job_id"], a["job_no"]), ("job-first", 1))   # the launch prompt is not a new job
        self.write(log, "Cats sleep 12 to 16 hours a day, mostly in short naps.\n" * 3)
        self.hook("PostToolUse", "run-pane1", tool_name="Read")
        self.store.upsert("run-pane1", status="waiting", activity="turn finished, waiting for you")
        first = voice.summarise_run(self.store.get("run-pane1"), "waiting", runner=fake_llm)
        self.assertIn("cats", first)

        # the owner types the next job into the same pane
        self.hook("UserPromptSubmit", "run-pane1", prompt="summarise how dogs sleep")
        b = self.store.get("run-pane1")
        self.assertNotEqual(b["job_id"], "job-first")
        self.assertEqual(b["job_no"], 2)
        self.assertEqual((b["steps"], b["files"], b["ended"]), (0, [], None))   # the previous job's state is cleared
        self.assertIn("dogs", b["title"].lower())
        self.write(log, "Dogs sleep 12 to 14 hours a day and dream like people do.\n" * 3)
        self.store.upsert("run-pane1", status="waiting", activity="turn finished, waiting for you")
        second = voice.summarise_run(self.store.get("run-pane1"), "waiting", runner=fake_llm)
        self.assertIn("dogs", second)
        self.assertNotIn("cats", second)

    def test_terminal_session_briefing_follows_the_new_job(self):
        sid = {"session_id": "term-1", "jarvieeTool": "claude"}
        aid = handle_hook("UserPromptSubmit", {**sid, "prompt": "summarise how cats sleep"}, self.cfg, self.store)
        for _ in range(4):
            handle_hook("PostToolUse", {**sid, "tool_name": "Read"}, self.cfg, self.store)
        handle_hook("Stop", sid, self.cfg, self.store)
        one = self.store.get(aid)
        handle_hook("UserPromptSubmit", {**sid, "prompt": "summarise how dogs sleep"}, self.cfg, self.store)
        handle_hook("PostToolUse", {**sid, "tool_name": "Read"}, self.cfg, self.store)
        handle_hook("Stop", sid, self.cfg, self.store)
        two = self.store.get(aid)
        self.assertNotEqual(one["job_id"], two["job_id"])
        self.assertEqual(two["steps"], 1)
        text = voice.build_briefing(two, "done")
        self.assertIn("dogs", text.lower())
        self.assertNotIn("cats", text.lower())

    def test_same_turn_repeats_are_not_new_jobs(self):
        sid = {"conversationId": "c1", "jarvieeTool": "agy", "lastUserInput": "fix the navbar"}
        aid = handle_hook("PreInvocation", sid, self.cfg, self.store)
        job = self.store.get(aid)["job_id"]
        handle_hook("PostToolUse", {**sid, "toolCall": {"name": "edit"}}, self.cfg, self.store)
        handle_hook("PreInvocation", sid, self.cfg, self.store)   # agy: every model call repeats the input
        a = self.store.get(aid)
        self.assertEqual((a["job_id"], a["steps"]), (job, 1))

    def test_a_new_job_reports_at_once_despite_the_cooldown(self):
        spoken = []
        self.cfg.setdefault("voice", {}).update(enabled=True, briefing=True, tldr=False, events=["done", "waiting"])
        sp = voice.Speaker(self.cfg, runner=lambda argv: spoken.append(argv[-1]), start_worker=False,
                           clock=time.time)
        row = {"id": "claude-x", "status": "done", "title": "Summarise how cats sleep", "job_id": "job-1"}
        self.assertTrue(sp.say_status(row, "done"))
        self.assertTrue(sp.say_status(dict(row, title="Summarise how dogs sleep", job_id="job-2"), "done"))
        self.assertFalse(sp.say_status(dict(row, title="Summarise how dogs sleep", job_id="job-2"), "done"))
        sp.drain()
        self.assertEqual(len(spoken), 2)
        self.assertIn("dogs", spoken[1].lower())


if __name__ == "__main__":
    unittest.main()
