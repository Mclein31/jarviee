"""/orchestrate: Opus leads a team in a 4×2 tab of its own (window 1 = the lead, seven workers), everything else
auto-tiles (max 6 per tab) in tabs apart from it."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import helpers
from fake_iterm import FakeITerm, make_terminal
from hub import auth, iterm, orch, orchestrate, panerun, terminal, tiling
from hub.server import make_server
from hub.store import Store

LEAD = {"role": "lead"}


def worker(lead):
    return {"role": "worker", "lead": lead}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.TempDir()
        d = self.tmp.__enter__()
        self.cfg, _ = helpers.make_cfg(d)
        self.cfg["terminal"].update(layout="split", grid=[4, 2], display="main", fullscreen=False, position="full",
                                    focus_on_launch=False, max_panes_per_tab=6)
        self.fake = FakeITerm()
        self.term = make_terminal(self.cfg, self.fake, d, waiting=lambda tty: True)
        self.term.worker_wait = 0
        self.dir = Path(d)

    def tearDown(self):
        self.tmp.__exit__()


class Parse(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(orchestrate.requested("/orchestrate"), (True, ""))
        self.assertEqual(orchestrate.requested("  /Orchestrate: fix the bug "), (True, "fix the bug"))
        self.assertEqual(orchestrate.requested("/orchestrated x"), (False, "/orchestrated x"))
        self.assertEqual(orchestrate.requested("fix /orchestrate"), (False, "fix /orchestrate"))


class DefaultAutoTiles(Base):
    def test_default_runs_never_open_the_grid(self):
        for i in range(3):
            self.term.open_pane(f"run-{i}", f"cmd run-{i}")
        self.assertNotIn("grid", self.fake.names())
        self.assertEqual([len(t) for t in self.fake.tabs], [3])
        self.assertFalse(self.term.status()["orchestrate"])

    def test_seventh_pane_opens_a_tab_of_its_own(self):
        for i in range(7):
            self.term.open_pane(f"run-{i}", f"cmd run-{i}")
        self.assertEqual([len(t) for t in self.fake.tabs], [6, 1])


class Panel(Base):
    def test_lead_takes_window_one_of_a_new_tab_beside_the_auto_tiles(self):
        self.term.open_pane("run-a", "cmd run-a")
        sid = self.term.open_pane("lead-1", "ignored", LEAD)
        (args,) = self.fake.args_of("grid")
        self.assertEqual((args[0], args[8], args[9]), ("tab", "4", "2"))
        self.assertTrue(args[2].endswith("lead-1"))
        self.assertIn("JARVIEE_SLOT=1", args[2])             # its pane frees itself after the run
        self.assertTrue(args[3].endswith("--slot"))
        self.assertEqual(sid, tiling.reading_order(self.term.state["tabs"][1])[0])   # window 1: top left
        self.assertEqual(self.term.state["panels"], {"lead-1": sid})
        self.assertEqual([len(t) for t in self.fake.tabs], [1, 8])
        self.assertEqual(len(self.term.state["vacant"]), 7)
        self.assertTrue(self.term.status()["orchestrate"])

    def test_lead_without_a_window_builds_the_panel_in_a_new_one(self):
        self.term.open_pane("lead-1", "ignored", LEAD)
        (args,) = self.fake.args_of("grid")
        self.assertEqual(args[0], "window")
        self.assertEqual(len(self.term.state["vacant"]), 7)

    def test_workers_fill_the_leads_windows_in_order_and_never_the_leads_own(self):
        lead = self.term.open_pane("lead-1", "x", LEAD)
        order = [s for s in tiling.reading_order(self.term.state["tabs"][0]) if s != lead]
        got = [self.term.open_pane(f"w-{i}", "x", worker("lead-1")) for i in range(7)]
        self.assertEqual(got, order)
        self.assertEqual([a[2] for a in self.fake.args_of("write")], [f"@jarviee:w-{i}" for i in range(7)])
        with self.assertRaises(iterm.TerminalError):          # an eighth worker: every window is busy
            self.term.open_pane("w-7", "x", worker("lead-1"))
        self.assertEqual(self.fake.names().count("tab"), 0)

    def test_a_finished_workers_window_takes_the_next_job(self):
        self.term.open_pane("lead-1", "x", LEAD)
        sids = [self.term.open_pane(f"w-{i}", "x", worker("lead-1")) for i in range(7)]
        self.term.free_pane("w-3")                            # its runner reported `free`
        self.assertEqual(self.term.open_pane("w-9", "x", worker("lead-1")), sids[3])

    def test_default_runs_never_take_a_panel_window(self):
        self.term.open_pane("lead-1", "x", LEAD)
        sid = self.term.open_pane("run-a", "cmd run-a")
        self.assertNotIn(sid, tiling.leaves(self.term.state["tabs"][0]))
        self.assertEqual(self.fake.args_of("write"), [])
        self.assertEqual(len(self.term.state["vacant"]), 7)

    def test_two_leads_get_two_panels_and_their_workers_stay_apart(self):
        a = self.term.open_pane("lead-a", "x", LEAD)
        b = self.term.open_pane("lead-b", "x", LEAD)
        self.assertEqual(self.term.state["panels"], {"lead-a": a, "lead-b": b})
        wa = self.term.open_pane("w-a", "x", worker("lead-a"))
        wb = self.term.open_pane("w-b", "x", worker("lead-b"))
        self.assertIn(wa, tiling.leaves(self.term.state["tabs"][0]))
        self.assertIn(wb, tiling.leaves(self.term.state["tabs"][1]))

    def test_worker_of_a_closed_panel_is_an_error(self):
        with self.assertRaises(iterm.TerminalError):
            self.term.open_pane("w-1", "x", worker("lead-gone"))

    def test_closing_a_panel_window_tops_the_grid_up_but_not_an_auto_tile_tab(self):
        self.term.open_pane("run-a", "cmd run-a")
        self.term.open_pane("run-b", "cmd run-b")
        self.term.open_pane("lead-1", "x", LEAD)
        self.term.open_pane("w-1", "x", worker("lead-1"))
        before = len(self.fake.args_of("split"))
        self.assertTrue(self.term.close_pane("w-1"))
        self.assertEqual(len(self.fake.args_of("split")), before + 1)   # a free window took its place
        self.assertEqual(len(tiling.leaves(self.term.state["tabs"][1])), 8)
        self.assertTrue(self.term.close_pane("run-b"))
        self.assertEqual(len(self.fake.args_of("split")), before + 1)   # the auto-tile tab only shrinks
        self.assertEqual(len(tiling.leaves(self.term.state["tabs"][0])), 1)

    def test_panels_survive_a_restart(self):
        self.term.open_pane("lead-1", "x", LEAD)
        again = make_terminal(self.cfg, self.fake, self.dir, waiting=lambda tty: True)
        again.worker_wait = 0
        self.assertEqual(len(again._grid_tabs()), 1)
        again.open_pane("w-1", "x", worker("lead-1"))

    def test_pane_command_slot_override(self):
        self.assertNotIn("JARVIEE_SLOT", terminal.pane_command(self.cfg, "run-a"))
        self.assertIn("JARVIEE_SLOT=1", terminal.pane_command(self.cfg, "run-a", slot=True))
        self.assertTrue(terminal.pane_command(self.cfg, None, slot=True).endswith("--slot"))


class Team(unittest.TestCase):
    """hub/orch.py with the launcher mocked: assigning, waiting, reviewing, redoing."""

    def setUp(self):
        self._t = helpers.TempDir()
        self.tmp = self._t.__enter__()
        self.home_patch = mock.patch("hub.orch.brand.home", return_value=Path(self.tmp))
        self.home_patch.start()
        self.cfg, _ = helpers.make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.store.upsert("lead-1", source="launcher", status="running", tool="claude", model="opus", task="t")
        orch._orchs["lead-1"] = orch.Orch("lead-1", "build the thing", "", None)
        self.n = 0
        self.callbacks = {}

        def launch(task, cfg, store, tool, tier, project, cwd, p=None, prompt=None, on_finish=None, **kw):
            self.n += 1
            rid = f"run-w{self.n}"
            self.callbacks[rid] = on_finish
            self.launched = dict(task=task, p=p, prompt=prompt, on_finish=on_finish, kw=kw)
            store.upsert(rid, source="launcher", status="running", tool=p["tool"], model=p["model"])
            return rid, p

        self.patch = mock.patch("hub.orch.launcher.launch", side_effect=launch)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.home_patch.stop()
        orch._orchs.pop("lead-1", None)
        self._t.__exit__()

    def assign(self, task="edit a", **kw):
        return orch.assign(self.cfg, self.store, "lead-1", task, **kw)

    def finish(self, n, status="done"):
        run = f"run-w{n}"
        self.store.upsert(run, status=status, files=json.dumps(["a.py"]))
        self.callbacks[run](run, status, 0)

    def test_load_picks_the_tool_and_model_and_the_worker_is_wired_to_its_lead(self):
        r = self.assign("rename a to b", load="basic")
        self.assertEqual((r["worker"], r["load"], r["free_windows"]), (1, "basic", 6))
        self.assertEqual(self.launched["p"]["orchestrate"], {"role": "worker", "lead": "lead-1"})
        self.assertEqual(self.launched["kw"]["parent_run_id"], "lead-1")
        self.assertTrue(callable(self.launched["on_finish"]))
        self.assertIn("You are worker 1", self.launched["prompt"])
        r2 = self.assign("redesign the schema", load="heavy")
        self.assertNotEqual((r["tool"], r["model"]), (r2["tool"], r2["model"]))   # a heavier job, a stronger model

    def test_explicit_tool_and_model_win(self):
        r = self.assign("x", tool="claude", model="sonnet")
        self.assertEqual((r["tool"], r["model"]), ("claude", "sonnet"))

    def test_only_seven_workers_at_once(self):
        for i in range(7):
            self.assign(f"job {i}")
        with self.assertRaisesRegex(ValueError, "busy"):
            self.assign("one too many")
        self.finish(1)
        self.assign("fits now")

    def test_bad_input_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.assign("x", load="gigantic")
        with self.assertRaises(ValueError):
            self.assign("   ")
        with self.assertRaises(ValueError):
            orch.assign(self.cfg, self.store, "nope", "x")
        self.store.upsert("lead-1", status="stopped")
        with self.assertRaisesRegex(ValueError, "ended"):
            self.assign("x")

    def test_wait_reports_a_finished_worker_once_and_then_idle(self):
        self.assign("job a")
        self.assign("job b")
        threading.Timer(0.2, self.finish, (2,)).start()
        r = orch.wait(self.cfg, self.store, "lead-1", 10)
        self.assertEqual([w["n"] for w in r["finished"]], [2])
        self.assertEqual(r["finished"][0]["files"], ["a.py"])
        self.assertEqual((r["running"], r["free_windows"], r["to_review"]), ([1], 6, [2]))
        self.assertTrue(orch.wait(self.cfg, self.store, "lead-1", 1)["timeout"])   # worker 1 still working, 2 already told
        self.finish(1)
        r = orch.wait(self.cfg, self.store, "lead-1", 1)
        self.assertEqual([w["n"] for w in r["finished"]], [1])
        self.assertTrue(orch.wait(self.cfg, self.store, "lead-1", 1)["idle"])

    def test_review_and_redo(self):
        self.assign("job a")
        with self.assertRaisesRegex(ValueError, "still working"):
            orch.review(self.cfg, "lead-1", 1, "accept")
        self.finish(1)
        r = orch.review(self.cfg, "lead-1", 1, "rework", "tests fail")
        self.assertFalse(r["all_accepted"])
        self.assertIn("--redo 1", r["hint"])
        r = self.assign("job a", redo=1, note="tests fail")
        self.assertEqual(r["worker"], 2)
        self.assertIn("tests fail", self.launched["task"])
        self.assertIn("rejected an earlier attempt", self.launched["task"])
        self.finish(2)
        r = orch.review(self.cfg, "lead-1", 2, "accept")
        self.assertTrue(r["all_accepted"])                    # worker 1 was reworked by worker 2
        with self.assertRaises(ValueError):
            orch.review(self.cfg, "lead-1", 2, "maybe")

    def test_fallback_keeps_the_same_worker(self):
        self.assign("job a")
        self.store.upsert("run-w1b", status="done", tool="claude", model="sonnet", files="[]")
        self.callbacks["run-w1"]("run-w1b", "done", 0)        # the chain ended on another tool after a quota failure
        r = orch.result(self.cfg, self.store, "lead-1", 1)
        self.assertEqual((r["run_id"], r["tool"], r["model"], r["status"]), ("run-w1b", "claude", "sonnet", "done"))

    def test_lead_brief_names_the_commands_and_the_window_count(self):
        b = orch.lead_brief(self.cfg)
        for word in ("orch assign", "orch wait", "orch review", "7 worker windows", "OmniRoute", "never idle", "accepted"):
            self.assertIn(word.lower(), b.lower().replace("so it is never idle", "never idle"))


class Routes(unittest.TestCase):
    def setUp(self):
        self._t = helpers.TempDir()
        self.tmp = self._t.__enter__()
        self.home_patch = mock.patch("hub.orch.brand.home", return_value=Path(self.tmp))
        self.home_patch.start()
        self.cfg, _ = helpers.make_cfg(self.tmp)
        self.srv = make_server(self.cfg, Store(self.cfg["db_path"]), interval=0.1)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"

    def tearDown(self):
        self.srv.scheduler.stop()
        self.srv.scheduler._thread.join(3)
        self.srv.shutdown()
        self.srv.server_close()
        self.home_patch.stop()
        self._t.__exit__()

    def post(self, path, body):
        h = {"Content-Type": "application/json", "X-Jarviee-Token": auth.get_token(self.cfg)}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_orchestrate_always_needs_a_task(self):
        code, r = self.post("/api/run", {"task": "/orchestrate"})
        self.assertEqual(code, 400)
        self.assertIn("needs a task", r["error"])

    def test_without_iterm_panes_it_says_so_and_launches_nothing(self):
        with mock.patch("hub.orch.start") as start:
            code, r = self.post("/api/run", {"task": "/orchestrate build x"})
        self.assertEqual(code, 400)
        start.assert_not_called()

    def test_with_a_task_the_lead_starts_without_the_prefix(self):
        with mock.patch("hub.routes.terminal.active", return_value=object()), \
                mock.patch("hub.orch.start", return_value=("run-lead1", {"tool": "claude", "model": "opus"})) as start:
            code, r = self.post("/api/run", {"task": "/orchestrate build x"})
        self.assertEqual((code, r["orchestrate"], r["id"], r["model"]), (200, True, "run-lead1", "opus"))
        self.assertEqual(start.call_args[0][2], "build x")

    def test_plain_runs_are_not_orchestrated(self):
        with mock.patch("hub.launcher.launch", return_value=("run-abc12345", {"tool": "agy", "model": "m"})) as launch:
            self.post("/api/run", {"task": "rename getUser to fetchUser"})
        self.assertNotIn("orchestrate", launch.call_args[1]["p"])

    def test_classify_ignores_the_prefix(self):
        a = self.post("/api/classify", {"task": "/orchestrate rename getUser to fetchUser"})[1]
        b = self.post("/api/classify", {"task": "rename getUser to fetchUser"})[1]
        self.assertEqual((a["tier"], a["model"]), (b["tier"], b["model"]))

    def test_orch_endpoints_need_the_token_and_a_known_lead(self):
        req = urllib.request.Request(self.base + "/api/orch/status", data=b'{"lead":"x"}',
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertIn(cm.exception.code, (401, 403))
        cm.exception.close()
        code, r = self.post("/api/orch/status", {"lead": "run-nope"})
        self.assertEqual(code, 400)
        self.assertEqual(self.post("/api/orch/bogus", {"lead": "x"})[0], 404)


class PaneRunFlag(unittest.TestCase):
    def test_the_flag_reaches_open_pane_only_for_orchestrated_runs(self):
        seen = []

        class Term:
            def open_pane(self, run_id, command, **kw):
                seen.append(kw)
                raise RuntimeError("stop here")   # run_in_pane then falls back to the background

        class S:
            def upsert(self, *a, **k):
                pass

        for p in ({"tool": "agy", "tier": "light", "project": "x", "orchestrate": LEAD},
                  {"tool": "agy", "tier": "light", "project": "x"}):
            with mock.patch("hub.panerun.terminal.pane_command", return_value="cmd"), \
                    mock.patch("hub.panerun.runstate.launcher"), mock.patch("hub.panerun.interactive_argv", return_value=None):
                panerun.run_in_pane("run-a", Term(), {"port": 1, "terminal": {}}, S(), {}, p, "t", ["agy"], None, {}, "/dev/null")
        self.assertEqual(seen, [{"orchestrate": LEAD}, {}])


if __name__ == "__main__":
    unittest.main()
