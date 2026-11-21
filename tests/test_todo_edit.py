import datetime
import unittest
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import todo_edit

D = datetime.date(2026, 11, 11)


class TodoEditTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.path = Path(self.cfg["obsidian"]["workspace_root"]) / "Alpha/.brain/TODO.md"
        self.orig = self.path.read_text(encoding="utf-8")

    def tearDown(self):
        self._t.__exit__()

    def tick(self, text, **kw):
        return todo_edit.tick(self.cfg, "Alpha", text, today=D, **kw)

    def test_tick_open_item_adds_date_and_touches_nothing_else(self):
        r = self.tick("Add billing")
        self.assertEqual(r, {"before": "- ⬜ Add billing", "after": "- ✅ Add billing (2026-11-11)"})
        new = self.path.read_text(encoding="utf-8")
        self.assertEqual(new, self.orig.replace(r["before"], r["after"]))

    def test_checkbox_and_owner_items(self):
        self.assertEqual(self.tick("Old open checkbox")["after"], "- [x] Old open checkbox (2026-11-11)")
        self.assertEqual(self.tick("Create the Stripe account", owner=True)["after"],
                         "- ✅ Create the Stripe account (2026-11-11)")

    def test_multi_marker_line_flips_only_that_marker_without_date(self):
        r = self.tick("Still to fix: logout copy")
        self.assertEqual(r["after"], "- ✅ Ship logout. ✅ Still to fix: logout copy")

    def test_undo_restores_exact_bytes(self):
        r = self.tick("Add billing")
        todo_edit.undo(self.cfg, "Alpha", r["before"], r["after"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.orig)

    def test_refuses_missing_ambiguous_and_wrong_kind(self):
        for text, kw in (("not an item", {}), ("Ship login", {}), ("inside a code block, ignored", {}),
                         ("Add billing", {"owner": True}), ("Create the Stripe account", {})):
            with self.assertRaises(todo_edit.TodoError, msg=text):
                self.tick(text, **kw)
        self.path.write_text(self.orig + "- ⬜ Add billing\n", encoding="utf-8")
        with self.assertRaises(todo_edit.TodoError):
            self.tick("Add billing")
        with self.assertRaises(todo_edit.TodoError):
            todo_edit.tick(self.cfg, "Nope", "x")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.orig + "- ⬜ Add billing\n")

    def test_undo_refuses_when_line_changed(self):
        r = self.tick("Add billing")
        self.path.write_text(self.path.read_text(encoding="utf-8").replace("Add billing (2026", "Add billing!! (2026"), encoding="utf-8")
        with self.assertRaises(todo_edit.TodoError):
            todo_edit.undo(self.cfg, "Alpha", r["before"], r["after"])

    def test_write_aborts_when_file_changes_underneath(self):
        raw, mt = todo_edit._read(self.path)
        self.path.write_text(raw + "\n- ⬜ sneaky\n", encoding="utf-8")
        with self.assertRaises(todo_edit.TodoError):
            todo_edit._write(self.path, raw, mt)
        self.assertIn("sneaky", self.path.read_text(encoding="utf-8"))

    def test_no_temp_files_left_and_stats_agree(self):
        from hub import obsidian
        before = obsidian.todo_stats(self.orig)
        self.tick("Add billing")
        self.assertEqual([p.name for p in self.path.parent.glob(".todo-*")], [])
        after = obsidian.todo_stats(self.path.read_text(encoding="utf-8"))
        self.assertEqual((after["todo"], after["done"]), (before["todo"] - 1, before["done"] + 1))


class TodoApiTests(TodoEditTests):
    """The same flow through POST /api/todo/tick and /api/todo/undo."""
    def test_api_tick_and_undo(self):
        import json, threading, urllib.error, urllib.request
        from hub import auth
        from hub.server import make_server
        from hub.store import Store
        srv = make_server(self.cfg, Store(self.cfg["db_path"]))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(path, body):
            r = urllib.request.Request(f"http://127.0.0.1:{self.cfg['port']}{path}", method="POST", data=json.dumps(body).encode(),
                                       headers={"Content-Type": "application/json", "X-Jarviee-Token": auth.get_token(self.cfg)})
            try:
                with op.open(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())
        try:
            code, r = post("/api/todo/tick", {"project": "Alpha", "text": "Add billing"})
            self.assertEqual(code, 200)
            self.assertIn("✅ Add billing", self.path.read_text(encoding="utf-8"))
            self.assertEqual(post("/api/todo/tick", {"project": "Alpha", "text": "Add billing"})[0], 409)
            self.assertEqual(post("/api/todo/undo", r and {"project": "Alpha", **r})[0], 200)
            self.assertEqual(self.path.read_text(encoding="utf-8"), self.orig)
        finally:
            if srv.scheduler:
                srv.scheduler.stop()
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
