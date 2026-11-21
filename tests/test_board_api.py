"""Project Board HTTP endpoints (hub/board_api.py) and the auto-advance wiring in the run lifecycle."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from helpers import TempDir, make_cfg
from hub import auth, board_api, inbox, launcher
from hub.server import handle_hook, make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class FakePolisher:
    """Stands in for board_polish.polish_async: records the calls, finishes the card like the real one would."""

    def __init__(self, finish=True):
        self.calls, self.finish = [], finish

    def __call__(self, board, card_id, cfg):
        self.calls.append(card_id)
        if self.finish:
            card = board.get(card_id)
            board.set_polish(card_id, "done", title="Improved: " + card["title"], model="haiku")


class BoardApiTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.cfg, _ = make_cfg(self._t.__enter__())
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.polisher = FakePolisher()
        self._orig = board_api._polisher
        board_api._polisher = lambda: self.polisher

    def tearDown(self):
        board_api._polisher = self._orig
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def call(self, path, body=None, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Jarviee-Token"] = auth.get_token(self.cfg)
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def make(self, title="Add login", project="Alpha", **kw):
        code, out = self.call("/api/board/create", {"project": project, "title": title, "polish": False, **kw})
        self.assertEqual(code, 200, out)
        return out["card"]

    # ---- reads ------------------------------------------------------------------------------

    def test_list_defaults_to_first_project_and_has_the_vocabulary(self):
        code, out = self.call("/api/board")
        self.assertEqual(code, 200)
        self.assertEqual(out["project"], "Alpha")
        self.assertEqual([c["key"] for c in out["columns"]], ["todo", "plan", "build", "review", "done"])
        self.assertIn("bugfix", out["labels"])
        self.assertEqual(out["priorities"], ["low", "normal", "high"])
        self.assertEqual(out["cards"], [])

    def test_list_returns_only_that_projects_cards_in_order(self):
        a = self.make("first")
        b = self.make("second")
        self.make("other", project="Beta Site")
        code, out = self.call("/api/board?project=Alpha")
        self.assertEqual([c["id"] for c in out["cards"]], [a["id"], b["id"]])
        self.assertEqual(self.call("/api/board?project=Beta%20Site")[1]["cards"][0]["title"], "other")

    def test_projects_counts_open_and_done(self):
        a = self.make("one")
        self.make("two")
        self.call("/api/board/move", {"id": a["id"], "column": "done"})
        code, out = self.call("/api/board/projects")
        self.assertEqual(code, 200)
        rows = {p["name"]: p for p in out["projects"]}
        self.assertEqual((rows["Alpha"]["open"], rows["Alpha"]["done"]), (1, 1))
        self.assertEqual((rows["Beta Site"]["open"], rows["Beta Site"]["done"]), (0, 0))

    # ---- create / update / delete ------------------------------------------------------------

    def test_create_requires_the_token(self):
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "x"}, token=False)
        self.assertIn(code, (401, 403))
        self.assertIn("error", out)

    def test_create_without_polish_saves_an_off_card_in_todo(self):
        card = self.make("Plain", label="bugfix", priority="high", description="why")
        self.assertEqual((card["column"], card["label"], card["priority"], card["polish"]), ("todo", "bugfix", "high", "off"))
        self.assertEqual(self.polisher.calls, [])

    def test_create_with_polish_saves_pending_first_then_starts_the_polisher(self):
        self.polisher.finish = False
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "Rough idea"})
        self.assertEqual(code, 200)
        self.assertEqual(out["card"]["polish"], "pending")
        self.assertEqual(self.polisher.calls, [out["card"]["id"]])

    def test_create_polish_does_not_block_and_a_finishing_polisher_is_reflected(self):
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "Rough idea"})
        self.assertEqual(out["card"]["polish"], "done")
        self.assertEqual(out["card"]["original"]["title"], "Rough idea")

    def test_create_polish_defaults_on_but_config_can_turn_it_off(self):
        self.cfg["board"] = {"polish": {"enabled": False}}
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "x"})
        self.assertEqual(out["card"]["polish"], "off")
        self.assertEqual(self.polisher.calls, [])

    def test_create_without_a_polisher_module_still_saves_the_card(self):
        board_api._polisher = lambda: None
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "x"})
        self.assertEqual((code, out["card"]["polish"]), (200, "off"))

    def test_a_polisher_that_crashes_marks_the_card_failed(self):
        def boom(board, card_id, cfg):
            raise RuntimeError("no model")
        board_api._polisher = lambda: boom
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "x"})
        self.assertEqual((code, out["card"]["polish"]), (200, "failed"))

    def test_create_errors_are_400_with_a_readable_message(self):
        for body, word in [({"project": "Alpha", "title": "  "}, "title"),
                           ({"project": "Alpha", "title": "x", "label": "nope"}, "label"),
                           ({"project": "Alpha", "title": "x", "priority": "urgent"}, "priority"),
                           ({"project": "", "title": "x"}, "project"),
                           ({"project": "Alpha"}, "title")]:
            code, out = self.call("/api/board/create", body)
            self.assertEqual(code, 400, body)
            self.assertIn(word, out["error"].lower())

    def test_create_only_accepts_a_workspace_project_and_uses_its_spelling(self):
        for name in ("NoSuchProject", "../..", "Alpha/../Beta Site", "."):
            code, out = self.call("/api/board/create", {"project": name, "title": "x", "polish": False})
            self.assertEqual(code, 400, name)
            self.assertIn("unknown project", out["error"].lower())
        self.assertEqual(self.make("x", project="alpha")["project"], "Alpha")

    def test_update_changes_fields_and_ignores_unknown_ones(self):
        card = self.make("Old")
        code, out = self.call("/api/board/update", {"id": card["id"], "title": "New", "priority": "low", "polish": False,
                                                    "column": "done", "run_id": "x"})
        self.assertEqual(code, 200)
        self.assertEqual((out["card"]["title"], out["card"]["priority"], out["card"]["column"]), ("New", "low", "todo"))
        self.assertIsNone(out["card"]["run_id"])

    def test_update_repolishes_only_when_the_text_changed(self):
        card = self.make("Old")
        self.call("/api/board/update", {"id": card["id"], "label": "docs", "priority": "high"})
        self.assertEqual(self.polisher.calls, [], "label / priority edits do not re-polish")
        self.call("/api/board/update", {"id": card["id"], "title": "Old"})
        self.assertEqual(self.polisher.calls, [], "the same title is not a change")
        self.polisher.finish = False
        code, out = self.call("/api/board/update", {"id": card["id"], "description": "more detail"})
        self.assertEqual(self.polisher.calls, [card["id"]])
        self.assertEqual(out["card"]["polish"], "pending")
        self.polisher.calls.clear()
        self.call("/api/board/update", {"id": card["id"], "title": "Newer", "polish": False})
        self.assertEqual(self.polisher.calls, [], "polish:false skips it")

    def test_update_unknown_id_is_404_and_bad_value_is_400(self):
        self.assertEqual(self.call("/api/board/update", {"id": "c_nope", "title": "x"})[0], 404)
        card = self.make("x")
        self.assertEqual(self.call("/api/board/update", {"id": card["id"], "label": "nope"})[0], 400)
        self.assertEqual(self.call("/api/board/update", {"id": card["id"], "title": 5})[0], 400)
        self.assertEqual(self.call("/api/board/update", {"title": "x"})[0], 400)

    def test_delete(self):
        card = self.make("gone")
        self.assertEqual(self.call("/api/board/delete", {"id": card["id"]}), (200, {"ok": True}))
        self.assertEqual(self.call("/api/board/delete", {"id": card["id"]})[0], 404)
        self.assertEqual(self.call("/api/board?project=Alpha")[1]["cards"], [])

    # ---- move / advance / revert -------------------------------------------------------------

    def test_move_to_a_column_and_position(self):
        a, b = self.make("a"), self.make("b")
        code, out = self.call("/api/board/move", {"id": b["id"], "column": "todo", "position": a["position"] - 10})
        self.assertEqual(code, 200)
        ids = [c["id"] for c in self.call("/api/board?project=Alpha")[1]["cards"]]
        self.assertEqual(ids, [b["id"], a["id"]])
        code, out = self.call("/api/board/move", {"id": a["id"], "column": "done"})
        self.assertEqual((out["card"]["column"], bool(out["card"]["done_at"])), ("done", True))

    def test_move_errors(self):
        card = self.make("a")
        self.assertEqual(self.call("/api/board/move", {"id": card["id"], "column": "later"})[0], 400)
        self.assertEqual(self.call("/api/board/move", {"id": card["id"], "column": "plan", "position": "x"})[0], 400)
        self.assertEqual(self.call("/api/board/move", {"id": card["id"]})[0], 400)
        self.assertEqual(self.call("/api/board/move", {"id": "c_nope", "column": "plan"})[0], 404)

    def test_advance_walks_the_columns_and_stops_at_done(self):
        card = self.make("a")
        seen = [self.call("/api/board/advance", {"id": card["id"]})[1]["card"]["column"] for _ in range(5)]
        self.assertEqual(seen, ["plan", "build", "review", "done", "done"])
        self.assertEqual(self.call("/api/board/advance", {"id": "c_nope"})[0], 404)

    def test_revert_polish_restores_the_owner_text(self):
        code, out = self.call("/api/board/create", {"project": "Alpha", "title": "my words", "description": "mine"})
        card = out["card"]
        self.assertEqual(card["title"], "Improved: my words")   # the fake polisher renames the title
        code, out = self.call("/api/board/revert-polish", {"id": card["id"]})
        self.assertEqual((code, out["card"]["title"], out["card"]["description"]), (200, "my words", "mine"))
        self.assertEqual(out["card"]["polish"], "off")
        self.assertEqual(self.call("/api/board/revert-polish", {"id": card["id"]})[0], 400)   # nothing left to revert
        self.assertEqual(self.call("/api/board/revert-polish", {"id": "c_nope"})[0], 404)

    # ---- start an agent ----------------------------------------------------------------------

    def wait_status(self, rid, want, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if (self.store.get(rid) or {}).get("status") in want:
                return
            time.sleep(0.05)
        self.fail(f"run {rid} never reached {want}: {self.store.get(rid)}")

    def test_start_launches_a_run_links_it_and_the_finished_run_moves_the_card_to_review(self):
        card = self.make("Add login", description="with a form FAKE_SLEEP=0.4")
        code, out = self.call("/api/board/start", {"id": card["id"], "tool": "echo"})
        self.assertEqual(code, 200, out)
        rid = out["run_id"]
        self.assertTrue(rid.startswith("run-"))
        self.assertEqual((out["card"]["run_id"], out["card"]["column"]), (rid, "build"))
        row = self.store.get(rid)
        self.assertEqual(row["project"], "Alpha")
        self.assertIn("Add login", row["task"])
        self.assertIn("with a form", row["task"])
        self.wait_status(rid, ("done", "failed"))
        end = time.time() + 5
        while time.time() < end and self.call("/api/board?project=Alpha")[1]["cards"][0]["column"] != "review":
            time.sleep(0.05)
        self.assertEqual(self.call("/api/board?project=Alpha")[1]["cards"][0]["column"], "review")

    def test_start_refuses_a_card_whose_agent_is_still_working(self):
        card = self.make("slow", description="FAKE_SLEEP=3")
        code, out = self.call("/api/board/start", {"id": card["id"], "tool": "echo"})
        self.assertEqual(code, 200, out)
        code, out = self.call("/api/board/start", {"id": card["id"], "tool": "echo"})
        self.assertEqual(code, 400)
        self.assertIn("already", out["error"])

    def test_start_errors(self):
        self.assertEqual(self.call("/api/board/start", {"id": "c_nope"})[0], 404)
        card = board_api.get_board(self.cfg).create("NoSuchProject", "x")   # its folder was removed after
        code, out = self.call("/api/board/start", {"id": card["id"]})
        self.assertEqual(code, 400)
        self.assertIn("NoSuchProject", out["error"])

    def test_unknown_board_route_is_not_found(self):
        self.assertEqual(self.call("/api/board/nope", {})[0], 404)

    # ---- auto-advance wiring -----------------------------------------------------------------

    def linked(self, run_id, column="todo"):
        card = self.make("linked")
        board = board_api.get_board(self.cfg)
        board.link_run(card["id"], run_id)
        if column != "todo":
            board.move(card["id"], column)
        return card["id"]

    def column(self, cid):
        return board_api.get_board(self.cfg).get(cid)["column"]

    def test_launcher_finish_moves_a_linked_card_to_review_but_not_on_failure(self):
        ok_id, bad_id = self.linked("run-ok", "build"), self.linked("run-bad", "build")
        p = {"project": "Alpha", "tool": "echo", "model": "m", "tier": "basic"}
        launcher._finish("run-ok", "done", "finished", p, "t", self.cfg, self.store, log_runs=False)
        launcher._finish("run-bad", "failed", "boom", p, "t", self.cfg, self.store, log_runs=False)
        self.assertEqual(self.column(ok_id), "review")
        self.assertEqual(self.column(bad_id), "build")

    def test_a_pane_run_going_running_then_done_advances_the_card_through_the_hooks(self):
        cid = self.linked("run-pane")
        self.store.upsert("run-pane", source="launcher", tool="claude", task="t", status="waiting", activity="x")
        handle_hook("UserPromptSubmit", {"jarvieeRunId": "run-pane", "jarvieeTool": "claude", "prompt": "go"}, self.cfg, self.store)
        self.assertEqual(self.column(cid), "build")
        self.store.upsert("run-pane", status="running")
        handle_hook("SessionEnd", {"jarvieeRunId": "run-pane", "jarvieeTool": "claude"}, self.cfg, self.store)
        self.assertEqual(self.column(cid), "review")

    def test_a_hook_that_keeps_the_same_status_does_not_touch_the_board(self):
        cid = self.linked("run-same")
        self.store.upsert("run-same", source="hook", tool="claude", task="t", status="running")
        board_api.get_board(self.cfg).move(cid, "todo")
        handle_hook("PostToolUse", {"jarvieeRunId": "run-same", "jarvieeTool": "claude", "tool_name": "Read"}, self.cfg, self.store)
        self.assertEqual(self.column(cid), "todo", "no status change, no card move")

    def test_inbox_accept_moves_the_card_to_done_and_dismiss_does_not(self):
        acc, dis = self.linked("run-acc", "review"), self.linked("run-dis", "review")
        for rid in ("run-acc", "run-dis"):
            self.store.upsert(rid, source="launcher", tool="claude", task="t", status="done", ended=time.time(),
                              files='["a.py"]', project="Alpha")
        orig = inbox._reviewable
        inbox._reviewable = lambda store, rid: None
        try:
            inbox.decide(self.store, "run-dis", inbox.DISMISSED, self.cfg)
            self.assertEqual(self.column(dis), "review")
            inbox.decide(self.store, "run-acc", inbox.ACCEPTED, self.cfg)
            self.assertEqual(self.column(acc), "done")
            late = self.linked("run-late", "review")
            inbox.decide(self.store, "run-late", inbox.ACCEPTED)   # without cfg: the old call shape still works
            self.assertEqual(self.column(late), "review")
        finally:
            inbox._reviewable = orig

    def test_the_accept_route_passes_the_config_through(self):
        cid = self.linked("run-route", "review")
        self.store.upsert("run-route", source="launcher", tool="claude", task="t", status="done", ended=time.time(),
                          files='["a.py"]', project="Alpha")
        orig = inbox._reviewable
        inbox._reviewable = lambda store, rid: None
        try:
            code, out = self.call("/api/inbox/accept", {"id": "run-route"})
        finally:
            inbox._reviewable = orig
        self.assertEqual(code, 200, out)
        self.assertEqual(self.column(cid), "done")

    def test_every_static_file_the_dashboard_loads_is_served(self):
        import re
        from pathlib import Path
        from hub import runs_api
        html = (Path(board_api.__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
        names = re.findall(r'<(?:script src|link rel="stylesheet" href)="/static/([^"?#]+)"', html)
        self.assertIn("board.js", names)
        self.assertIn("board-editor.js", names)
        self.assertIn("board.css", names)
        for name in names:
            self.assertIsNotNone(runs_api.static_path(name), f"/static/{name} would 404 (name rule or missing file)")

    def test_notify_helpers_never_raise(self):
        self.assertEqual(board_api.notify_run({}, "run-x", "done"), [])          # no db_path in the config
        self.assertEqual(board_api.notify_accept({"db_path": "/nonexistent/x/y/z.db"}, "run-x"), [])
        self.assertEqual(board_api.notify_run(self.cfg, "run-unlinked", "done"), [])


if __name__ == "__main__":
    unittest.main()
