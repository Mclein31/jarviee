"""An unattended run whose turn ends while its own background task still runs is not finished.

The owner's report (2026-11-21): 6 of 9 /orchestrate workers were killed mid-task. A Claude worker started the test
suite in the background, ended its turn to wait for the completion notice ("I'll write the report when the monitor
reports the end"), and Jarviee took the finished turn as the end of the run and closed the session; agy workers were
terminated with "1 task(s) · /tasks" in their footer. Now the hub reads the agent's transcript at the turn's end
(hub/finaltext.py), keeps such a run running (hub/ingest.py), and the pane only ends it once a turn ends with nothing
pending, or at the guards.background_wait_minutes cap (hooks/pane_runner.py).
"""
import json
import os
import signal
import stat
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, TempDir, make_cfg
from hub import finaltext, guards, ingest, runstate
from hub.server import handle_hook
from hub.store import Store

sys.path.insert(0, str(ROOT / "hooks"))
import pane_runner  # noqa: E402

SESSION = "0b5e7a1c-1111-4222-8333-944455556666"


# ---- fake Claude Code transcript lines (the shapes documented in hub/finaltext.py) ----
def _assistant_tool(tool_id, name, inp):
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def _result(tool_id, result, text="ok"):
    return {"type": "user", "isSidechain": False, "toolUseResult": result,
            "message": {"role": "user", "content": [{"tool_use_id": tool_id, "type": "tool_result", "content": text}]}}


def _note(task_id, tool_id="", status="completed", event=""):
    body = f"<task-notification>\n<task-id>{task_id}</task-id>\n"
    if tool_id:
        body += f"<tool-use-id>{tool_id}</tool-use-id>\n"
    body += f"<status>{status}</status>\n" if status else f"<event>{event}</event>\n"
    return body + "<summary>Background command finished</summary>\n</task-notification>"


def _delivered(text):
    return {"type": "user", "isSidechain": False, "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": text}}


def _absorbed(text):
    return {"type": "attachment", "isSidechain": False, "attachment": {"type": "queued_command", "prompt": text}}


def _queued(text):
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def _bash_bg(tool_id="toolu_bash1", task_id="bq1spkkab"):
    return [_assistant_tool(tool_id, "Bash", {"command": "python3 -m unittest discover -s tests -q",
                                              "run_in_background": True}),
            _result(tool_id, {"stdout": "", "stderr": "", "backgroundTaskId": task_id},
                    f"Command running in background with ID: {task_id}.")]


def write_claude(projects, lines, session=SESSION):
    d = Path(projects) / "-tmp-project"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{session}.jsonl", "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


# ---- fake agy transcript ----
def write_agy(brain_root, task, steps, conv="c0ffee00-aaaa-bbbb-cccc-000000000001"):
    logs = Path(brain_root) / conv / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    rows = [{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": task}] + steps
    with open(logs / "transcript.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return conv


def agy_started(conv, n=64):
    return {"step_index": n, "source": "MODEL", "type": "GENERIC", "status": "RUNNING",
            "content": f"Created At: 2026-11-21T13:07:40+08:00\nTool is running as a background task with task id: "
                       f"{conv}/task-{n}\nTask Description: python3 -m unittest discover -s tests -q\n"}


def agy_finished(conv, n=64, verb="finished with result"):
    return {"step_index": n + 5, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
            "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
                       f"[Message] timestamp=2026-11-21T05:09:35Z sender={conv}/task-{n} priority=MESSAGE_PRIORITY_HIGH "
                       f'content=Task id "{conv}/task-{n}" {verb}:\nCommand exited with code 0.\n'}


class ClaudeTranscript(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()

    def tearDown(self):
        self._t.__exit__()

    def pending(self, lines):
        write_claude(self.tmp, lines)
        return finaltext.claude_pending(SESSION, projects_dir=self.tmp)

    def test_a_background_command_without_its_notice_is_pending(self):
        self.assertEqual(self.pending(_bash_bg()), ["bq1spkkab"])

    def test_a_notice_only_queued_is_still_pending(self):
        # enqueued while the turn ended: the agent is about to take another turn to read it
        self.assertEqual(self.pending(_bash_bg() + [_queued(_note("bq1spkkab", "toolu_bash1"))]), ["bq1spkkab"])

    def test_a_delivered_notice_ends_it(self):
        for deliver in (_delivered, _absorbed):
            self.assertEqual(self.pending(_bash_bg() + [deliver(_note("bq1spkkab", "toolu_bash1"))]), [], deliver)

    def test_failed_and_killed_tasks_are_over_too(self):
        for status in ("failed", "killed", "stopped"):
            self.assertEqual(self.pending(_bash_bg() + [_delivered(_note("bq1spkkab", status=status))]), [], status)

    def test_monitors_events_are_not_its_end_but_its_expiry_is(self):
        mon = [_assistant_tool("toolu_mon", "Monitor", {"command": "tail -f x", "description": "suite end"}),
               _result("toolu_mon", {"taskId": "b0hi6bdi4", "timeoutMs": 600000, "persistent": False})]
        self.assertEqual(self.pending(mon), ["b0hi6bdi4"])
        self.assertEqual(self.pending(mon + [_delivered(_note("b0hi6bdi4", status="", event="Ran 900 tests"))]),
                         ["b0hi6bdi4"])
        expired = _note("b0hi6bdi4", status="", event="[Monitor expired after 10m with no events delivered.]")
        self.assertEqual(self.pending(mon + [_delivered(expired)]), [])

    def test_a_background_subagent_is_pending_until_its_notice(self):
        agent = [_assistant_tool("toolu_ag", "Agent", {"prompt": "review", "run_in_background": True}),
                 _result("toolu_ag", {"isAsync": True, "status": "async_launched", "agentId": "ae49df304c3db6297"})]
        self.assertEqual(self.pending(agent), ["ae49df304c3db6297"])
        self.assertEqual(self.pending(agent + [_delivered(_note("ae49df304c3db6297", "toolu_ag"))]), [])

    def test_todo_items_and_quoted_notices_are_ignored(self):
        todo = [_assistant_tool("toolu_t", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
                _result("toolu_t", {"success": True, "taskId": "1", "updatedFields": ["status"]})]
        self.assertEqual(self.pending(todo), [])
        # a grep whose output quotes a notification is a tool result, not a delivery
        quoted = _result("toolu_grep", {"stdout": "x"}, text=_note("bq1spkkab", "toolu_bash1"))
        self.assertEqual(self.pending(_bash_bg() + [quoted]), ["bq1spkkab"])

    def test_an_unknown_session_is_unknown_not_empty(self):
        self.assertIsNone(finaltext.claude_pending("no-such-session", projects_dir=self.tmp))
        self.assertIsNone(finaltext.claude_pending("", projects_dir=self.tmp))
        self.assertIsNone(finaltext.claude_pending("../etc", projects_dir=self.tmp))


class AgyTranscript(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)   # points JARVIEE_AGY_BRAIN_ROOT at tmp/agy_brain
        self.brain = Path(os.environ["JARVIEE_AGY_BRAIN_ROOT"])
        self.task = "Run the full suite and report the failures by owner"

    def tearDown(self):
        self._t.__exit__()

    def test_started_finished_cancelled(self):
        conv = write_agy(self.brain, self.task, [])
        write_agy(self.brain, self.task, [agy_started(conv)], conv)
        self.assertEqual(finaltext.agy_pending(self.task, time.time() - 5), [f"{conv}/task-64"])
        for end in (agy_finished(conv), agy_finished(conv, verb="was canceled with result"),
                    {"step_index": 70, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                     "content": f'Created At: x\nTask "{conv}/task-64" cancelled.'}):
            write_agy(self.brain, self.task, [agy_started(conv), end], conv)
            self.assertEqual(finaltext.agy_pending(self.task, time.time() - 5), [], end["content"][-40:])

    def test_a_timer_firing_ends_it(self):
        conv = write_agy(self.brain, self.task, [])
        timer = {"step_index": 120, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
                 "content": f"[Message] timestamp=x sender={conv}/task-64 priority=MESSAGE_PRIORITY_HIGH "
                            "content=30 minutes have elapsed."}
        write_agy(self.brain, self.task, [agy_started(conv), timer], conv)
        self.assertEqual(finaltext.agy_pending(self.task, time.time() - 5), [])

    def test_no_transcript_is_unknown(self):
        self.assertIsNone(finaltext.agy_pending("a task nobody ran", time.time()))


class Guard(unittest.TestCase):
    def test_default_override_off(self):
        self.assertEqual(guards.background_wait_seconds({}), 1800.0)
        self.assertEqual(guards.background_wait_seconds({"guards": {"background_wait_minutes": 5}}), 300.0)
        self.assertEqual(guards.background_wait_seconds({"guards": {"background_wait_minutes": 0}}), 0.0)
        self.assertEqual(guards.background_wait_seconds({"guards": {"background_wait_minutes": "x"}}), 1800.0)
        # turning the guards off must not make a run wait forever
        self.assertEqual(guards.background_wait_seconds({"guards": {"enabled": False}}), 1800.0)


class StopHook(unittest.TestCase):
    """hub/ingest.py at the Stop of a live pane run."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.projects = Path(self.tmp) / "claude_projects"
        self._p = mock.patch.object(finaltext, "CLAUDE_PROJECTS", str(self.projects))
        self._p.start()
        ingest._BG_SINCE.clear()

    def tearDown(self):
        self._p.stop()
        ingest._BG_SINCE.clear()
        self._t.__exit__()

    def run_row(self, rid="run-bg1", tool="claude", interactive=runstate.LIVE_UNATTENDED, task="run the suite"):
        self.store.upsert(rid, source="launcher", tool=tool, task=task, status="running", activity="working",
                          interactive=interactive, job_id="job-1", job_no=1, session_id=SESSION if tool == "claude" else "",
                          started=time.time() - 5)
        return rid

    def stop(self, rid, tool="claude", msg="The suite is still running; I'll write the report when it ends."):
        handle_hook("Stop", {"jarvieeRunId": rid, "jarvieeTool": tool, "session_id": SESSION,
                             "last_assistant_message": msg}, self.cfg, self.store)
        row = self.store.get(rid)
        return row["status"], row["activity"]

    def test_pending_keeps_the_run_running(self):
        write_claude(self.projects, _bash_bg())
        rid = self.run_row()
        status, act = self.stop(rid)
        self.assertEqual(status, "running")
        self.assertTrue(act.startswith(runstate.BG_WAIT), act)
        self.assertFalse(self.store.get(rid).get("ended"))

    def test_the_next_turn_with_nothing_pending_finishes_it(self):
        write_claude(self.projects, _bash_bg())
        rid = self.run_row()
        self.assertEqual(self.stop(rid)[0], "running")
        write_claude(self.projects, _bash_bg() + [_delivered(_note("bq1spkkab", "toolu_bash1"))])
        handle_hook("UserPromptSubmit", {"jarvieeRunId": rid, "jarvieeTool": "claude",
                                         "prompt": _note("bq1spkkab", "toolu_bash1")}, self.cfg, self.store)
        self.assertEqual(self.stop(rid, msg="All 900 tests pass. Report written."), ("done", "finished"))
        self.assertNotIn(rid, ingest._BG_SINCE)

    def test_nothing_pending_or_no_transcript_finishes_as_before(self):
        write_claude(self.projects, [])
        self.assertEqual(self.stop(self.run_row("run-bg2")), ("done", "finished"))
        self.assertEqual(self.stop(self.run_row("run-bg3"), msg="done"), ("done", "finished"))

    def test_the_cap_finishes_it(self):
        write_claude(self.projects, _bash_bg())
        rid = self.run_row()
        self.assertEqual(self.stop(rid)[0], "running")
        ingest._BG_SINCE[rid] = time.time() - 31 * 60   # its turns have been waiting longer than the 30 min cap
        self.assertEqual(self.stop(rid), ("done", "finished"))
        cfg0 = dict(self.cfg, guards={"background_wait_minutes": 0})   # 0: never waits
        rid = self.run_row("run-bg4")
        handle_hook("Stop", {"jarvieeRunId": rid, "jarvieeTool": "claude", "session_id": SESSION}, cfg0, self.store)
        self.assertEqual(self.store.get(rid)["status"], "done")

    def test_agy_pending_and_unknown(self):
        brain = Path(os.environ["JARVIEE_AGY_BRAIN_ROOT"])
        task = "Run the full suite and report the failures by owner"
        conv = write_agy(brain, task, [])
        write_agy(brain, task, [agy_started(conv)], conv)
        rid = self.run_row("run-agy1", tool="agy", task=task)
        status, act = self.stop(rid, tool="agy", msg="")
        self.assertEqual(status, "running")
        self.assertTrue(act.startswith(runstate.BG_WAIT))
        write_agy(brain, task, [agy_started(conv), agy_finished(conv)], conv)
        self.assertEqual(self.stop(rid, tool="agy", msg=""), ("done", "finished"))
        # no transcript found: finished, and the pane checks agy's footer instead
        rid = self.run_row("run-agy2", tool="agy", task="something with no transcript at all")
        self.assertEqual(self.stop(rid, tool="agy", msg=""), ("done", runstate.FINISHED_UNCHECKED))

    def test_an_attended_session_is_unchanged(self):
        write_claude(self.projects, _bash_bg())
        rid = self.run_row("run-att", interactive=1)
        self.assertEqual(self.stop(rid, msg="Implemented the fix and the tests pass."), ("done", "finished"))
        rid = self.run_row("run-att2", interactive=1)
        status, act = self.stop(rid, msg="Should I use Postgres or SQLite for this?")
        self.assertEqual(status, "waiting")
        self.assertNotIn(runstate.BG_WAIT, act)


class PaneRunner(unittest.TestCase):
    """hooks/pane_runner.py main(): a fake agent and a scripted /api/run-status answer."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.out, self.sigs = [], (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))

    def tearDown(self):
        signal.signal(signal.SIGTERM, self.sigs[0])
        signal.signal(signal.SIGHUP, self.sigs[1])
        self._t.__exit__()

    def agent(self, name="claude", prints="", lives=6.0):
        """An executable that prints `prints` and lives `lives` seconds unless signalled."""
        p = self.tmp / name
        p.write_text(f"#!{sys.executable}\nimport sys, time\nsys.stdout.write({prints!r}); sys.stdout.flush()\n"
                     f"time.sleep({lives})\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        return str(p)

    def run_pane(self, exe, statuses, unattended=True, cap=1.0, close="immediately"):
        """`statuses`: [(seconds since start, {status, activity})], the last one holds. Returns (seconds, summary)."""
        spec = {"argv": [exe], "cwd": str(self.tmp), "env": dict(os.environ), "log": str(self.tmp / "run.log"),
                "title": "job", "close": close, "interactive": True, "unattended": unattended,
                "live_close_delay": 0.2, "background_wait": cap}
        t0 = time.time()

        def status(rid, local=None):
            cur = statuses[0][1]
            for at, st in statuses:
                if time.time() - t0 >= at:
                    cur = st
            return dict(cur, job_id="job-1")

        pane_runner.state.update(stopped=0.0, done=False)
        with mock.patch.multiple(pane_runner, call=lambda path, **kw: spec, poll_header=status,
                                 report=lambda *a, **kw: None, set_header=lambda *a, **kw: None,
                                 set_tab_color=lambda *a, **kw: None, clear_header=lambda *a, **kw: None,
                                 close_now=lambda *a, **kw: None, close_rule=lambda *a, **kw: None,
                                 say=lambda text: self.out.append(text)):
            with mock.patch.dict(os.environ, {"JARVIEE_SLOT": ""}):
                pane_runner.main(["pane_runner.py", "run-pane1"])
        return time.time() - t0, self.out[-1] if self.out else ""

    WAIT = {"status": "running", "activity": runstate.BG_WAIT + " (1 running)"}
    DONE = {"status": "done", "activity": "finished"}

    def test_a_plain_finished_turn_still_closes_at_once(self):
        took, summary = self.run_pane(self.agent(), [(0, self.DONE)])
        self.assertLess(took, 2.5)
        self.assertIn("done", summary)

    def test_waiting_holds_until_the_next_turn_ends(self):
        took, summary = self.run_pane(self.agent(), [(0, self.WAIT), (1.5, self.DONE)], cap=60)
        self.assertGreaterEqual(took, 1.5)
        self.assertLess(took, 4.5)
        self.assertIn("✓ done", summary)

    def test_the_cap_ends_a_wait_as_done(self):
        took, summary = self.run_pane(self.agent(), [(0, self.WAIT)], cap=1.0)
        self.assertGreaterEqual(took, 1.0)
        self.assertLess(took, 4.5)
        self.assertIn("✓ done", summary)

    def test_attended_sessions_ignore_it(self):
        # an attended session never closes on a running status: the agent ends by itself
        took, summary = self.run_pane(self.agent(lives=1.5), [(0, self.WAIT)], unattended=False, cap=0.5)
        self.assertGreaterEqual(took, 1.4)
        self.assertIn("✓ done", summary)

    def test_agy_footer_holds_only_when_the_hub_could_not_check(self):
        footer = "\x1b[2m  1 task(s) \x1b[1C· /tasks\x1b[0m\r\n"
        unchecked = {"status": "done", "activity": runstate.FINISHED_UNCHECKED}
        took, _ = self.run_pane(self.agent("agy", footer), [(0, {"status": "running", "activity": "working"}),
                                                           (0.6, unchecked)], cap=1.5)
        self.assertGreaterEqual(took, 2.0)   # held for the cap
        took, _ = self.run_pane(self.agent("agy", footer), [(0, {"status": "running", "activity": "working"}),
                                                           (0.6, self.DONE)], cap=30)
        self.assertLess(took, 3.0)           # the hub read the transcript: trusted
        took, _ = self.run_pane(self.agent("agy", "plain output\r\n"),
                                [(0, {"status": "running", "activity": "working"}), (0.6, unchecked)], cap=30)
        self.assertLess(took, 3.0)           # no footer: nothing running

    def test_background_wait_rule(self):
        bw = pane_runner.background_wait
        self.assertTrue(bw("running", self.WAIT, 0, 0, False))
        self.assertFalse(bw("running", {"activity": "using Bash"}, 0, 0, False))
        un = {"activity": runstate.FINISHED_UNCHECKED}
        self.assertTrue(bw("done", un, 100.0, 105.0, True))
        self.assertFalse(bw("done", un, 100.0, 200.0, True))   # a footer long before the turn ended is stale
        self.assertFalse(bw("done", un, 100.0, 105.0, False))  # Claude: the hub's word only
        self.assertEqual((pane_runner.BG_WAIT, pane_runner.FINISHED_UNCHECKED),
                         (runstate.BG_WAIT, runstate.FINISHED_UNCHECKED))


if __name__ == "__main__":
    unittest.main()
