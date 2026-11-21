"""Project Board storage and auto-advance rules (hub/board.py)."""
import os
import sqlite3
import time
import unittest

from helpers import TempDir
from hub import board as board_mod
from hub.board import COLUMNS, LABELS, PRIORITIES, Board, NotFound


class BoardCase(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.db_path = os.path.join(self.tmp, "hub.db")
        self.b = Board(self.db_path)

    def tearDown(self):
        self.b.close()
        self._t.__exit__()

    def mk(self, title="Do it", project="alpha", **kw):
        return self.b.create(project, title, **kw)

    def titles(self, project="alpha", column=None):
        return [c["title"] for c in self.b.list(project) if column in (None, c["column"])]


class Constants(BoardCase):
    def test_columns_labels_priorities(self):
        self.assertEqual([k for k, _ in COLUMNS], ["todo", "plan", "build", "review", "done"])
        self.assertEqual(dict(COLUMNS)["review"], "Code review / finalize")
        self.assertEqual(LABELS, ["feature", "bugfix", "error", "refactor", "chore", "docs", "research"])
        self.assertEqual(PRIORITIES, ["low", "normal", "high"])


class Crud(BoardCase):
    def test_create_defaults_and_shape(self):
        c = self.mk("  Add   login  ", description="  body ")
        self.assertRegex(c["id"], r"^c_[0-9a-f]{8}$")
        self.assertEqual((c["project"], c["column"], c["label"], c["priority"]), ("alpha", "todo", "feature", "normal"))
        self.assertEqual((c["title"], c["description"]), ("Add login", "body"))
        self.assertIsNone(c["run_id"])
        self.assertIsNone(c["original"])
        self.assertIsNone(c["done_at"])
        self.assertEqual((c["polish"], c["polish_model"]), ("off", ""))
        self.assertAlmostEqual(c["created"], time.time(), delta=5)
        self.assertEqual(set(c), {"id", "project", "column", "position", "title", "description", "label", "priority",
                                  "run_id", "polish", "polish_model", "original", "created", "updated", "done_at"})

    def test_get_and_missing(self):
        c = self.mk()
        self.assertEqual(self.b.get(c["id"]), c)
        self.assertIsNone(self.b.get("c_nope"))

    def test_create_with_label_priority_polish(self):
        c = self.mk(label="bugfix", priority="high", polish="pending")
        self.assertEqual((c["label"], c["priority"], c["polish"]), ("bugfix", "high", "pending"))

    def test_update_whitelisted_fields(self):
        c = self.mk()
        u = self.b.update(c["id"], title="New", description="D", label="docs", priority="low")
        self.assertEqual((u["title"], u["description"], u["label"], u["priority"]), ("New", "D", "docs", "low"))
        self.assertGreaterEqual(u["updated"], c["updated"])
        self.assertEqual(self.b.get(c["id"]), u)

    def test_update_rejects_fields_outside_the_whitelist(self):
        c = self.mk()
        for field in ("id", "project", "created", "done_at", "polish", "original"):
            with self.assertRaises(ValueError, msg=field):
                self.b.update(c["id"], **{field: "x"})
        self.assertEqual(self.b.get(c["id"])["project"], "alpha")

    def test_update_column_and_run_id(self):
        c = self.mk()
        u = self.b.update(c["id"], column="plan", run_id="run-1")
        self.assertEqual((u["column"], u["run_id"]), ("plan", "run-1"))
        self.assertIsNone(self.b.update(c["id"], run_id=None)["run_id"])

    def test_update_with_the_same_column_keeps_the_place(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.b.update(a["id"], column="todo")
        self.assertEqual(self.titles(), ["a", "b"])

    def test_update_position_reorders(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.b.update(b_["id"], position=a["position"] - 10)
        self.assertEqual(self.titles(), ["b", "a"])

    def test_update_missing_card(self):
        with self.assertRaises(NotFound):
            self.b.update("c_nope", title="x")

    def test_delete(self):
        c = self.mk()
        self.assertTrue(self.b.delete(c["id"]))
        self.assertFalse(self.b.delete(c["id"]))
        self.assertIsNone(self.b.get(c["id"]))
        self.assertEqual(self.b.list("alpha"), [])

    def test_survives_reopen_and_shares_a_file_with_the_store(self):
        c = self.mk("keep me")
        self.b.close()
        from hub.store import Store
        Store(self.db_path)                      # the run store opens the same file
        self.b = Board(self.db_path)
        self.assertEqual(self.b.get(c["id"])["title"], "keep me")

    def test_tilde_in_the_path_is_expanded(self):
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp
        try:
            b = Board("~/sub/board.db")
            b.close()
        finally:
            if old is None:
                os.environ.pop("HOME")
            else:
                os.environ["HOME"] = old
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "sub", "board.db")))

    def test_table_is_created_idempotently(self):
        Board(self.db_path).close()
        Board(self.db_path).close()
        con = sqlite3.connect(self.db_path)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        self.assertIn("board_cards", names)


class Validation(BoardCase):
    def assertMsg(self, fn, *needles):
        with self.assertRaises(ValueError) as ctx:
            fn()
        msg = str(ctx.exception)
        self.assertTrue(msg and len(msg) < 200, msg)
        for n in needles:
            self.assertIn(n, msg)

    def test_create_errors(self):
        self.assertMsg(lambda: self.b.create("alpha", ""), "title")
        self.assertMsg(lambda: self.b.create("alpha", "   "), "title")
        self.assertMsg(lambda: self.b.create("alpha", None), "title")
        self.assertMsg(lambda: self.b.create("", "x"), "project")
        self.assertMsg(lambda: self.b.create("alpha", "x" * 121), "title")
        self.assertMsg(lambda: self.b.create("alpha", "x", "d" * 4001), "description")
        self.assertMsg(lambda: self.b.create("alpha", "x", label="nope"), "label", "feature")
        self.assertMsg(lambda: self.b.create("alpha", "x", priority="urgent"), "priority", "normal")
        self.assertEqual(self.b.list("alpha"), [])

    def test_limits_are_inclusive(self):
        c = self.b.create("alpha", "x" * 120, "d" * 4000)
        self.assertEqual((len(c["title"]), len(c["description"])), (120, 4000))

    def test_update_errors_leave_the_card_untouched(self):
        c = self.mk()
        self.assertMsg(lambda: self.b.update(c["id"], title=""), "title")
        self.assertMsg(lambda: self.b.update(c["id"], label="x"), "label")
        self.assertMsg(lambda: self.b.update(c["id"], priority="x"), "priority")
        self.assertMsg(lambda: self.b.update(c["id"], column="x"), "column", "todo")
        self.assertMsg(lambda: self.b.update(c["id"], position="abc"), "position")
        self.assertMsg(lambda: self.b.update(c["id"], title="ok", label="x"), "label")
        self.assertEqual(self.b.get(c["id"]), c)

    def test_move_errors(self):
        c = self.mk()
        self.assertMsg(lambda: self.b.move(c["id"], "doing"), "column")
        self.assertMsg(lambda: self.b.move(c["id"], "plan", "x"), "position")
        self.assertMsg(lambda: self.b.move(c["id"], "plan", float("nan")), "position")
        self.assertMsg(lambda: self.b.move(c["id"], "plan", float("inf")), "position")
        self.assertMsg(lambda: self.b.move(c["id"], "plan", True), "position")
        self.assertEqual(self.b.get(c["id"]), c)

    def test_missing_card_errors(self):
        for fn in (lambda: self.b.move("c_x", "plan"), lambda: self.b.advance("c_x"),
                   lambda: self.b.link_run("c_x", "r"), lambda: self.b.set_polish("c_x", "done"),
                   lambda: self.b.revert_polish("c_x")):
            with self.assertRaises(NotFound):
                fn()

    def test_set_polish_errors(self):
        c = self.mk()
        self.assertMsg(lambda: self.b.set_polish(c["id"], "weird"), "polish")
        self.assertMsg(lambda: self.b.set_polish(c["id"], "done", title=""), "title")
        self.assertMsg(lambda: self.b.set_polish(c["id"], "done", label="nope"), "label")
        self.assertEqual(self.b.get(c["id"]), c)


class Ordering(BoardCase):
    def test_new_cards_append_to_the_end_of_todo(self):
        for n in "abc":
            self.mk(n)
        self.assertEqual(self.titles(), ["a", "b", "c"])
        pos = [c["position"] for c in self.b.list("alpha")]
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(set(pos)), 3)

    def test_list_sorts_by_column_then_position(self):
        a, b_, c = self.mk("a"), self.mk("b"), self.mk("c")
        self.b.move(a["id"], "done")
        self.b.move(b_["id"], "build")
        self.b.move(c["id"], "plan")
        self.mk("d")
        self.assertEqual([(x["column"], x["title"]) for x in self.b.list("alpha")],
                         [("todo", "d"), ("plan", "c"), ("build", "b"), ("done", "a")])

    def test_projects_are_separate(self):
        self.mk("a", project="alpha")
        self.mk("b", project="beta")
        self.assertEqual(self.titles("alpha"), ["a"])
        self.assertEqual(self.titles("beta"), ["b"])
        self.assertEqual(self.b.list("none"), [])
        first_a = self.b.list("alpha")[0]["position"]
        self.assertEqual(self.b.list("beta")[0]["position"], first_a)   # positions are per column per project

    def test_move_with_a_position_goes_between_neighbours(self):
        a, b_, c = self.mk("a"), self.mk("b"), self.mk("c")
        mid = (a["position"] + b_["position"]) / 2
        self.b.move(c["id"], "todo", mid)
        self.assertEqual(self.titles(), ["a", "c", "b"])

    def test_move_to_another_column_at_a_position(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.b.move(a["id"], "plan")
        self.b.move(b_["id"], "plan", self.b.get(a["id"])["position"] - 5)
        self.assertEqual(self.titles(column="plan"), ["b", "a"])

    def test_move_without_a_position_goes_to_the_end(self):
        a, b_, c = self.mk("a"), self.mk("b"), self.mk("c")
        self.b.move(a["id"], "todo")
        self.assertEqual(self.titles(), ["b", "c", "a"])
        self.b.move(b_["id"], "plan")
        self.b.move(c["id"], "plan")
        self.assertEqual(self.titles(column="plan"), ["b", "c"])

    def test_move_into_an_empty_column_starts_at_the_step(self):
        a = self.mk("a")
        self.assertEqual(self.b.move(a["id"], "review")["position"], board_mod.STEP)

    def test_crowded_positions_are_renumbered(self):
        a, b_, c = self.mk("a"), self.mk("b"), self.mk("c")
        # drop c between a and b over and over: the gap halves each time
        for _ in range(60):
            lo = self.b.get(a["id"])["position"]
            hi = self.b.get(b_["id"])["position"]
            self.b.move(c["id"], "todo", (lo + hi) / 2)
            self.assertEqual(self.titles(), ["a", "c", "b"])
            # then put it back at the end and in again to keep halving
            self.b.move(c["id"], "todo", (lo + hi) / 2)
        pos = [x["position"] for x in self.b.list("alpha")]
        self.assertEqual(len(set(pos)), 3)
        self.assertGreaterEqual(min(y - x for x, y in zip(pos, pos[1:])), board_mod.MIN_GAP)

    def test_equal_positions_are_made_distinct(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.b.move(b_["id"], "todo", a["position"])
        pos = [x["position"] for x in self.b.list("alpha")]
        self.assertEqual(len(set(pos)), 2)


class MoveAndAdvance(BoardCase):
    def test_done_at_is_set_on_entering_done_and_cleared_on_leaving(self):
        c = self.mk()
        self.assertIsNone(c["done_at"])
        d = self.b.move(c["id"], "done")
        self.assertAlmostEqual(d["done_at"], time.time(), delta=5)
        again = self.b.move(c["id"], "done", 5)
        self.assertEqual(again["done_at"], d["done_at"])          # re-ordering inside done keeps the time
        self.assertIsNone(self.b.move(c["id"], "review")["done_at"])

    def test_update_to_done_sets_done_at(self):
        c = self.mk()
        self.assertIsNotNone(self.b.update(c["id"], column="done")["done_at"])

    def test_advance_walks_the_columns_and_stops_in_done(self):
        c = self.mk()
        seen = [c["column"]]
        for _ in range(4):
            seen.append(self.b.advance(c["id"])["column"])
        self.assertEqual(seen, ["todo", "plan", "build", "review", "done"])
        last = self.b.get(c["id"])
        again = self.b.advance(c["id"])
        self.assertEqual(again["column"], "done")
        self.assertEqual((again["position"], again["done_at"]), (last["position"], last["done_at"]))

    def test_advance_goes_to_the_end_of_the_next_column(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.b.advance(a["id"])
        self.b.advance(b_["id"])
        self.assertEqual(self.titles(column="plan"), ["a", "b"])

    def test_link_run_and_cards_for_run(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.assertEqual(self.b.link_run(a["id"], "run-1")["run_id"], "run-1")
        self.b.link_run(b_["id"], "run-1")
        self.assertEqual({c["title"] for c in self.b.cards_for_run("run-1")}, {"a", "b"})
        self.assertEqual(self.b.cards_for_run("run-2"), [])
        self.assertEqual(self.b.cards_for_run(None), [])
        self.assertEqual(self.b.cards_for_run(""), [])
        self.assertIsNone(self.b.link_run(a["id"], None)["run_id"])
        self.assertEqual([c["title"] for c in self.b.cards_for_run("run-1")], ["b"])

    def test_counts(self):
        a, b_ = self.mk("a"), self.mk("b")
        self.mk("c", project="beta")
        self.b.move(a["id"], "done")
        self.assertEqual(self.b.counts(), {"alpha": {"open": 1, "done": 1}, "beta": {"open": 1, "done": 0}})
        self.assertEqual(self.b.projects(), ["alpha", "beta"])


class Polish(BoardCase):
    def test_polish_saves_the_original_once(self):
        c = self.mk("fix thing", description="it is broken")
        p = self.b.set_polish(c["id"], "done", title="Fix the thing", description="Goal.\n- works", label="bugfix", model="haiku")
        self.assertEqual((p["title"], p["description"], p["label"]), ("Fix the thing", "Goal.\n- works", "bugfix"))
        self.assertEqual((p["polish"], p["polish_model"]), ("done", "haiku"))
        self.assertEqual(p["original"], {"title": "fix thing", "description": "it is broken"})
        q = self.b.set_polish(c["id"], "done", title="Fix the thing better", description="x", model="agy")
        self.assertEqual(q["original"], {"title": "fix thing", "description": "it is broken"})   # still the owner's words
        self.assertEqual(q["polish_model"], "agy")

    def test_pending_and_failed_change_nothing_else(self):
        c = self.mk("t", description="d")
        p = self.b.set_polish(c["id"], "pending")
        self.assertEqual((p["polish"], p["title"], p["description"], p["original"]), ("pending", "t", "d", None))
        f = self.b.set_polish(c["id"], "failed")
        self.assertEqual((f["polish"], f["original"], f["label"]), ("failed", None, "feature"))

    def test_identical_text_does_not_create_an_original(self):
        c = self.mk("t", description="d")
        p = self.b.set_polish(c["id"], "done", title="t", description="d", model="haiku")
        self.assertIsNone(p["original"])

    def test_label_only_polish(self):
        c = self.mk("t")
        p = self.b.set_polish(c["id"], "done", label="docs", model="haiku")
        self.assertEqual((p["label"], p["original"]), ("docs", None))

    def test_revert(self):
        c = self.mk("fix thing", description="it is broken")
        self.b.set_polish(c["id"], "done", title="Fix the thing", description="Goal", model="haiku")
        r = self.b.revert_polish(c["id"])
        self.assertEqual((r["title"], r["description"]), ("fix thing", "it is broken"))
        self.assertEqual((r["original"], r["polish"], r["polish_model"]), (None, "off", ""))
        with self.assertRaises(ValueError) as ctx:
            self.b.revert_polish(c["id"])
        self.assertIn("earlier text", str(ctx.exception))

    def test_revert_without_polish(self):
        with self.assertRaises(ValueError):
            self.b.revert_polish(self.mk()["id"])

    def test_owner_edit_drops_the_stale_original(self):
        c = self.mk("fix thing", description="d")
        self.b.set_polish(c["id"], "done", title="Fix the thing", description="Goal", model="haiku")
        u = self.b.update(c["id"], title="My new words")
        self.assertEqual((u["original"], u["polish"], u["polish_model"]), (None, "off", ""))

    def test_a_non_text_edit_keeps_the_polish(self):
        c = self.mk("fix thing")
        self.b.set_polish(c["id"], "done", title="Fix the thing", model="haiku")
        u = self.b.update(c["id"], priority="high", title="Fix the thing")
        self.assertEqual((u["polish"], u["original"]["title"]), ("done", "fix thing"))


class AutoAdvance(BoardCase):
    def linked(self, column="todo", run="run-1", title="a"):
        c = self.mk(title)
        if column != "todo":
            self.b.move(c["id"], column)
        self.b.link_run(c["id"], run)
        return c["id"]

    def col(self, cid):
        return self.b.get(cid)["column"]

    def test_running_moves_todo_and_plan_to_build(self):
        for start in ("todo", "plan"):
            cid = self.linked(start, run=f"r-{start}")
            moved = self.b.on_run_status(f"r-{start}", "running")
            self.assertEqual(self.col(cid), "build", start)
            self.assertEqual([m["id"] for m in moved], [cid])

    def test_running_leaves_the_other_columns_alone(self):
        for start in ("build", "review", "done"):
            cid = self.linked(start, run=f"r-{start}")
            before = self.b.get(cid)
            self.assertEqual(self.b.on_run_status(f"r-{start}", "running"), [])
            self.assertEqual(self.b.get(cid), before, start)

    def test_done_moves_todo_plan_build_to_review(self):
        for start in ("todo", "plan", "build"):
            cid = self.linked(start, run=f"r-{start}")
            self.b.on_run_status(f"r-{start}", "done", changed_files=["a.py"])
            self.assertEqual(self.col(cid), "review", start)

    def test_done_leaves_review_and_done_alone(self):
        for start in ("review", "done"):
            cid = self.linked(start, run=f"r-{start}")
            before = self.b.get(cid)
            self.b.on_run_status(f"r-{start}", "done")
            self.assertEqual(self.b.get(cid), before, start)

    def test_failed_and_stopped_do_not_move(self):
        for status in ("failed", "stopped"):
            for start in ("todo", "plan", "build", "review"):
                cid = self.linked(start, run=f"r-{status}-{start}")
                self.assertEqual(self.b.on_run_status(f"r-{status}-{start}", status), [])
                self.assertEqual(self.col(cid), start, (status, start))

    def test_other_statuses_do_not_move(self):
        cid = self.linked("todo")
        for status in ("queued", "waiting", "weird", None, ""):
            self.b.on_run_status("run-1", status)
        self.assertEqual(self.col(cid), "todo")

    def test_the_whole_run_lifecycle(self):
        cid = self.linked("todo")
        self.b.on_run_status("run-1", "running")
        self.assertEqual(self.col(cid), "build")
        self.b.on_run_status("run-1", "running")            # repeats are harmless
        self.assertEqual(self.col(cid), "build")
        self.b.on_run_status("run-1", "done")
        self.assertEqual(self.col(cid), "review")
        self.b.on_inbox_accept("run-1")
        card = self.b.get(cid)
        self.assertEqual(card["column"], "done")
        self.assertIsNotNone(card["done_at"])

    def test_a_card_the_owner_parked_in_review_is_not_pulled_back(self):
        cid = self.linked("review")
        self.b.on_run_status("run-1", "running")
        self.assertEqual(self.col(cid), "review")

    def test_inbox_accept_moves_from_any_column_to_done(self):
        for start in ("todo", "plan", "build", "review"):
            cid = self.linked(start, run=f"r-{start}")
            self.b.on_inbox_accept(f"r-{start}")
            self.assertEqual(self.col(cid), "done", start)

    def test_inbox_accept_on_a_done_card_keeps_done_at(self):
        cid = self.linked("done")
        first = self.b.get(cid)["done_at"]
        self.assertEqual(self.b.on_inbox_accept("run-1"), [])
        self.assertEqual(self.b.get(cid)["done_at"], first)

    def test_unlinked_runs_are_a_no_op(self):
        cid = self.mk("a")["id"]
        for fn in (lambda: self.b.on_run_status("run-x", "running"), lambda: self.b.on_run_status("run-x", "done"),
                   lambda: self.b.on_run_status(None, "done"), lambda: self.b.on_run_status("", "running"),
                   lambda: self.b.on_inbox_accept("run-x"), lambda: self.b.on_inbox_accept(None)):
            self.assertEqual(fn(), [])
        self.assertEqual(self.col(cid), "todo")

    def test_only_the_linked_card_moves(self):
        a = self.linked("todo", run="run-1", title="a")
        other = self.mk("b")["id"]
        self.b.on_run_status("run-1", "running")
        self.assertEqual((self.col(a), self.col(other)), ("build", "todo"))

    def test_two_cards_on_one_run_both_move(self):
        a = self.linked("todo", run="run-1", title="a")
        b_ = self.linked("plan", run="run-1", title="b")
        self.b.on_run_status("run-1", "done")
        self.assertEqual((self.col(a), self.col(b_)), ("review", "review"))

    def test_moved_cards_go_to_the_end_of_the_target_column(self):
        first = self.mk("first")["id"]
        self.b.move(first, "build")
        cid = self.linked("todo", title="auto")
        self.b.on_run_status("run-1", "running")
        self.assertEqual(self.titles(column="build"), ["first", "auto"])

    def test_auto_advance_works_across_connections(self):
        cid = self.linked("todo")
        other = Board(self.db_path)                 # the server and the launcher thread each hold one
        try:
            other.on_run_status("run-1", "running")
        finally:
            other.close()
        self.assertEqual(self.col(cid), "build")


if __name__ == "__main__":
    unittest.main()
