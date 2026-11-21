"""End to end: server → fake iTerm2 that really runs the pane command → hooks/pane_runner.py → fake agent."""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

from fake_iterm import FakeITerm, make_terminal
from helpers import TempDir, make_cfg
from hub import auth, iterm, launcher, panes
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class PaneRuns(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.fake = FakeITerm(spawn=True, env=dict(os.environ, JARVIEE_PORT=str(self.cfg["port"])))
        self.cfg["_terminal"] = make_terminal(self.cfg, self.fake, self.tmp)
        self.cfg["terminal"]["close_on_finish"] = "immediately"
        self.cfg["retry"] = {"enabled": False}   # retries have their own tests (test_outcome_retry)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.saved = dict(panes.CLOSE_RULES)

    def tearDown(self):
        panes.CLOSE_RULES.update(self.saved)
        self.fake.stop_all()
        self.srv.scheduler.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def req(self, path, body=None):
        headers = {"Content-Type": "application/json", "X-Jarviee-Token": auth.get_token(self.cfg)}
        r = urllib.request.Request(f"http://127.0.0.1:{self.cfg['port']}{path}", headers=headers,
                                   data=None if body is None else json.dumps(body).encode())
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def launch(self, task):
        return self.req("/api/run", {"task": task, "tool": "agy", "tier": "basic"})[1]["id"]

    def until(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            v = cond()
            if v:
                return v
            time.sleep(0.1)
        self.fail("timed out")

    def finished(self, rid):
        return self.until(lambda: (a := self.store.get(rid)) and a["status"] in ("done", "failed", "stopped") and a)

    def test_run_in_pane_logs_output_reports_exit_and_closes(self):
        task = 'Say hi FAKE_SAY="pane-ok" then " & do shell script "touch /tmp/x" & "'
        rid = self.launch(task)
        a = self.finished(rid)
        self.assertEqual((a["status"], a["activity"]), ("done", "finished"))
        sid = a["pane_id"]
        self.assertEqual(self.fake.args_of("open")[0][0].split()[-1], rid)       # run id is the only argument
        self.assertIn("pane-ok", launcher.strip_tty(Path(a["log_path"]).read_text()))
        # "immediately", and it was the full screen window's only pane: the runner exits and iTerm2 closes the
        # pane and the window itself (no scripted close, no idle placeholder left behind)
        self.until(lambda: self.fake.procs[sid].poll() is not None)
        self.assertNotIn(["100", sid], self.fake.args_of("close"))
        self.assertIsNone(self.cfg["_terminal"].state.get("idle"))
        for _, args, script in self.fake.calls:                                  # task text never reaches osascript
            self.assertFalse(any("pane-ok" in x or "do shell script" in x for x in args + [script]))
        self.assertEqual(self.req(f"/api/run-spec?id={rid}")[0], 410)           # the spec is handed out once
        self.assertFalse(self.cfg["_terminal"].has_pane(rid))

    def test_run_status_carries_activity_and_steps(self):
        rid = self.launch("status please")
        self.finished(rid)
        code, st = self.req(f"/api/run-status?id={rid}")
        self.assertEqual(code, 200)
        self.assertEqual(st["activity"], "finished")
        self.assertIsInstance(st["steps"], int)

    def test_failed_exit_code_is_reported(self):
        a = self.finished(self.launch("fail please FAKE_EXIT=3"))
        self.assertEqual((a["status"], a["activity"]), ("failed", "exit code 3"))

    def test_close_rules_after_delay_and_never(self):
        panes.CLOSE_RULES["after_30s"] = 1
        self.cfg["terminal"]["close_on_finish"] = "after_30s"
        a = self.finished(self.launch("first"))
        self.until(lambda: self.fake.procs[a["pane_id"]].poll() is not None, timeout=6)   # last pane: ends itself
        self.assertFalse(self.cfg["_terminal"].has_pane(a["id"]))
        self.cfg["terminal"]["close_on_finish"] = "never"
        b = self.finished(self.launch("second"))
        proc = self.fake.procs[b["pane_id"]]
        self.until(lambda: proc.poll() is not None, timeout=6)                   # no terminal: Enter never comes
        self.assertNotIn(["100", b["pane_id"]], self.fake.args_of("close"))
        self.assertTrue(self.cfg["_terminal"].has_pane(b["id"]))

    def test_stop_goes_through_the_pane_runner(self):
        rid = self.launch("long FAKE_SLEEP=30")
        self.until(lambda: rid in launcher._procs)
        self.assertTrue(self.req("/api/stop", {"id": rid})[1]["ok"])
        a = self.finished(rid)
        self.assertEqual(a["status"], "stopped")
        runner = self.fake.procs.get(a["pane_id"]) or self.fake.procs["S1"]
        self.until(lambda: runner.poll() is not None, timeout=12)
        # it was the full screen window's last pane: no idle placeholder, the window is let go
        self.until(lambda: self.cfg["_terminal"].state["window"] is None)
        self.assertIsNone(self.cfg["_terminal"].state["idle"])

    def test_no_iterm_permission_falls_back_to_the_background(self):
        self.fake.fail = iterm.PermissionDenied("Not authorized to send Apple events to iTerm2. (-1743)")
        a = self.finished(self.launch("still runs FAKE_SAY=\"bg-ok\""))
        self.assertEqual(a["status"], "done")
        log = Path(a["log_path"]).read_text()
        self.assertIn("bg-ok", log)
        self.assertIn("[pane] iTerm2 pane unavailable", log)
        notes = self.req("/api/state")[1]["terminal"]["notices"]
        self.assertIn("Automation", notes[0]["text"])

    def test_dashboard_actions(self):
        self.cfg["terminal"]["close_on_finish"] = "never"
        rid = self.launch("keep me")
        self.finished(rid)
        self.assertTrue(self.until(lambda: next(x for x in self.req("/api/state")[1]["agents"] if x["id"] == rid)["pane"]))
        self.assertEqual(self.req("/api/terminal", {"action": "show", "id": rid})[1], {"ok": True, "reason": ""})
        self.assertIn("even", self.req("/api/terminal", {"action": "retile"})[1]["message"])
        self.assertIn("Closed 1", self.req("/api/terminal", {"action": "close-finished"})[1]["message"])
        self.assertEqual(self.req("/api/terminal", {"action": "disable"})[1]["enabled"], False)
        self.assertEqual(self.req("/api/terminal", {"action": "bogus"})[0], 400)


if __name__ == "__main__":
    unittest.main()


class InteractiveArgvTests(unittest.TestCase):
    def test_live_session_commands(self):
        from hub import launcher
        self.assertEqual(launcher.interactive_argv("claude", ["claude", "-p", "do it", "--model", "sonnet"]),
                         ["claude", "do it", "--model", "sonnet", "--permission-mode", "auto"])
        self.assertEqual(launcher.interactive_argv("agy", ["agy", "-p", "do it", "--model", "m"]), ["agy", "-i", "do it", "--model", "m", "--mode", "auto"])
        self.assertIsNone(launcher.interactive_argv("echo", ["python", "-c", "print(1)"]))
        self.assertIsNone(launcher.interactive_argv("claude", ["claude", "do it"]))   # already live

    def test_runner_gives_a_live_session_the_terminal_and_no_timeout(self):
        src = (Path(__file__).resolve().parent.parent / "hooks" / "pane_runner.py").read_text(encoding="utf-8")
        self.assertIn('spec.get("log"), live)', src)   # PtyProc forwards stdin only for live sessions
        self.assertIn('timeout = float("inf") if live else', src)


class PruneLogsTests(unittest.TestCase):
    def test_old_logs_removed_new_kept(self):
        import os, tempfile, time
        from hub import launcher
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d, "run-aaaa1111.log"), Path(d, "run-bbbb2222.log")
            old.write_text("x"); new.write_text("x")
            t = time.time() - 40 * 86400
            os.utime(old, (t, t))
            self.assertEqual(launcher.prune_logs({"logs_dir": d}), 1)
            self.assertFalse(old.exists()); self.assertTrue(new.exists())


class TabColor(unittest.TestCase):
    def _emit(self, state):
        import io
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
        import pane_runner
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            pane_runner.set_tab_color(state)
        finally:
            sys.stdout = old
        return buf.getvalue()

    def test_states_emit_distinct_colour_sequences(self):
        seen = set()
        for st in ("running", "waiting", "done", "failed"):
            out = self._emit(st)
            for ch in ("red", "green", "blue"):
                self.assertIn(f"\033]6;1;bg;{ch};brightness;", out)
            self.assertTrue(out.endswith("\007"))
            seen.add(out)
        self.assertEqual(len(seen), 4)

    def test_reset_sequence(self):
        self.assertEqual(self._emit(None), "\033]6;1;bg;*;default\007")

    def test_never_raises_on_broken_stdout(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
        import pane_runner
        old = sys.stdout
        sys.stdout = None
        try:
            pane_runner.set_tab_color("done")
        finally:
            sys.stdout = old


class Header(unittest.TestCase):
    """The pane header (badge + title + tab colour) follows the run's current job, never a previous one."""
    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
        import pane_runner
        self.pr = pane_runner
        pane_runner.header.update(job=None, shown=None, title="", polled=0.0)

    def _out(self, fn, *a, **k):
        import io
        import sys
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = fn(*a, **k)
        finally:
            sys.stdout = old
        return r, buf.getvalue()

    def test_colour_and_text_per_status(self):
        for status, dot, label in (("running", "⚪", "RUNNING"), ("waiting", "🟡", "NEEDS YOU"),
                                   ("done", "🟢", "DONE"), ("failed", "🔴", "FAILED")):
            self.pr.header.update(job=None, shown=None)
            kind, out = self._out(self.pr.header_from_status, {"job_id": "j1", "status": status, "title": "Fix login"})
            self.assertEqual(kind, status)
            self.assertIn(f"\033]1;{dot} {label} · Fix login\007", out)   # in the pane's title bar
            # no badge watermark over the pane: only ever the empty "clear" form
            self.assertNotIn("SetBadgeFormat=" + "⚪", out)
            for chunk in out.split("SetBadgeFormat=")[1:]:
                self.assertEqual(chunk.split("\007")[0], "")

    def test_running_job_never_shows_a_finish(self):
        for st in ("running", "queued", "running"):
            kind, _ = self._out(self.pr.header_from_status, {"job_id": "j1", "status": st, "title": "T"})
            self.assertEqual(kind, "running")

    def test_new_job_drops_the_previous_jobs_finish(self):
        self._out(self.pr.header_from_status, {"job_id": "j1", "status": "done", "title": "Old"}, "done")
        kind, out = self._out(self.pr.header_from_status, {"job_id": "j2", "status": "running", "title": "New"}, "done")
        self.assertEqual(kind, "running")
        self.assertIn("RUNNING · New", out)

    def test_the_panes_own_finish_wins_and_unchanged_header_is_not_rewritten(self):
        kind, _ = self._out(self.pr.header_from_status, {"job_id": "j1", "status": "running", "title": "T"}, "failed")
        self.assertEqual(kind, "failed")
        _, out = self._out(self.pr.header_from_status, {"job_id": "j1", "status": "running", "title": "T"}, "failed")
        self.assertEqual(out, "")

    def test_hub_down_at_exit_still_shows_the_local_finish(self):
        def boom(*a, **k):
            raise OSError("refused")
        with mock.patch.object(self.pr, "call", boom):
            self.pr.header["title"] = "T"
            _, out = self._out(self.pr.poll_header, "r1", local="done")
        self.assertIn("🟢 DONE · T", out)


class SilentAgentStatus(unittest.TestCase):
    """claude -p prints nothing until it finishes; the pane shows its own progress line meanwhile."""
    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
        import pane_runner
        self.pr = pane_runner

    def _proc(self):
        p = self.pr.PtyProc.__new__(self.pr.PtyProc)
        p.log, p.last_out, p.at_bol, p.status_shown = None, 0.0, True, False
        return p

    def test_status_line_shows_time_activity_and_steps(self):
        line = self.pr.status_line(130, {"activity": "Edit hub/x.py", "steps": 9}, 0)
        self.assertEqual(line, "⠋ working · 2m 10s · Edit hub/x.py · 9 steps")
        self.assertEqual(self.pr.status_line(5, {"activity": "working", "steps": 1}, 1), "⠙ working · 5s · 1 step")
        self.assertEqual(self.pr.status_line(5, {}, 0), "⠋ working · 5s")

    def test_output_wipes_the_status_line_and_status_never_eats_a_partial_line(self):
        from unittest import mock
        p, wrote = self._proc(), []
        with mock.patch.object(self.pr.os, "write", lambda fd, b: wrote.append((fd, b)) or len(b)):
            p.show_status("⠋ working · 3s")
            p._out(b"result\n")
            p._out(b"partial")
            p.show_status("⠙ working · 4s")
        self.assertTrue(wrote[0][1].startswith(b"\r\033[2K"))           # at line start: overwrite in place
        self.assertEqual(wrote[1][1], b"\r\033[2K")                       # wiped before the agent's output
        self.assertEqual(wrote[2][1], b"result\n")
        self.assertTrue(wrote[4][1].startswith(b"\r\n"))                 # after a partial line: new line first
        self.assertTrue(all(fd == 1 for fd, _ in wrote))                  # the pane only, never the run log
