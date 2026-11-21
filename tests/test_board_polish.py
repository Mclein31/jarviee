"""Tests for cheap-model Project Board card polishing (hub/board_polish.py)."""
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from helpers import TempDir
from hub import board_polish
from hub.board import Board


class ParseAnswerTests(unittest.TestCase):
    def test_valid_strict_json(self):
        out = json.dumps({
            "title": "  Add   retry  backoff ",
            "description": "Retry failed webhooks.\n\n- Exponential backoff\n- Cap at 5 tries",
            "label": "feature",
        })
        got = board_polish.parse_answer(out, orig_description="Retry failed webhooks with backoff and max tries.")
        self.assertEqual(got["title"], "Add retry backoff")
        self.assertEqual(got["label"], "feature")
        self.assertIn("- Exponential backoff", got["description"])

    def test_extracts_json_from_fences_and_prose(self):
        out = "Here you go:\n```json\n" + json.dumps({
            "title": "Fix login redirect",
            "description": "Preserve next URL on login.\n\n- Keep ?next= param\n- Add regression test",
            "label": "BUGFIX",
        }) + "\n```"
        got = board_polish.parse_answer(out, orig_description="Login loses the next URL after redirect.")
        self.assertIsNotNone(got)
        self.assertEqual(got["label"], "bugfix")

    def test_clamps_title_to_80_chars(self):
        out = json.dumps({
            "title": "A" * 120,
            "description": "Goal line.\n\n- Bullet one\n- Bullet two",
            "label": "chore",
        })
        got = board_polish.parse_answer(out, orig_description="A" * 50)
        self.assertEqual(len(got["title"]), 80)

    def test_rejects_malformed_and_non_dict(self):
        for bad in ("", None, "not json", "[1, 2, 3]", '{"title": "x"}', '{"title": 123, "description": "abc"}'):
            self.assertIsNone(board_polish.parse_answer(bad, orig_description="some original text"))

    def test_rejects_empty_title_or_description(self):
        self.assertIsNone(board_polish.parse_answer(
            json.dumps({"title": "   ", "description": "Valid description here.", "label": "feature"}),
            orig_description="Valid description here.",
        ))
        self.assertIsNone(board_polish.parse_answer(
            json.dumps({"title": "Valid", "description": "   ", "label": "feature"}),
            orig_description="Valid description here.",
        ))

    def test_rejects_description_too_short_or_too_long(self):
        orig = "x" * 100
        too_short = json.dumps({"title": "Do x", "description": "y" * 19, "label": "feature"})
        too_long = json.dumps({"title": "Do x", "description": "y" * 401, "label": "feature"})
        just_right = json.dumps({"title": "Do x", "description": "y" * 80, "label": "feature"})
        self.assertIsNone(board_polish.parse_answer(too_short, orig_description=orig))
        self.assertIsNone(board_polish.parse_answer(too_long, orig_description=orig))
        self.assertIsNotNone(board_polish.parse_answer(just_right, orig_description=orig))

    def test_empty_original_description_allows_expansion(self):
        out = json.dumps({
            "title": "Add dark mode toggle",
            "description": "Allow toggling dark mode.\n\n- Persist choice\n- Update theme tokens",
            "label": "feature",
        })
        got = board_polish.parse_answer(out, orig_description="")
        self.assertIsNotNone(got)

    def test_invalid_label_falls_back_to_default(self):
        out = json.dumps({
            "title": "Clean up cache",
            "description": "Prune stale cache entries.\n\n- Delete old files\n- Log count",
            "label": "enhancement",
        })
        got = board_polish.parse_answer(out, orig_description="Clean up stale cache entries.", default_label="refactor")
        self.assertEqual(got["label"], "refactor")


class ImproveTests(unittest.TestCase):
    def test_picks_first_valid_runner(self):
        calls = []

        def r1(prompt):
            calls.append(("r1", prompt))
            return json.dumps({
                "title": "Fix crash on empty config",
                "description": "Handle missing config safely.\n\n- Return defaults\n- Add test",
                "label": "bugfix",
            })

        def r2(prompt):
            calls.append(("r2", prompt))
            raise AssertionError("fallback should not be called")

        got = board_polish.improve("fix crash {when} empty", "handle missing config safely", "feature", "agent-hub",
                                  runners=(("haiku", r1), ("agy-flash", r2)))
        self.assertEqual(got["model"], "haiku")
        self.assertEqual(got["label"], "bugfix")
        self.assertEqual(len(calls), 1)
        self.assertIn("agent-hub", calls[0][1])

    def test_falls_back_on_error_or_rejection(self):
        def r1(_prompt):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=45)

        def r2(_prompt):
            return "not json"

        def r3(_prompt):
            return json.dumps({
                "title": "Document board API",
                "description": "Add endpoint docs.\n\n- List routes\n- Show payloads",
                "label": "docs",
            })

        got = board_polish.improve("doc board api", "document the board endpoints", "chore", "agent-hub",
                                  runners=(("haiku", r1), ("bad", r2), ("agy-flash", r3)))
        self.assertEqual(got["model"], "agy-flash")
        self.assertEqual(got["label"], "docs")

    def test_returns_none_when_all_fail(self):
        got = board_polish.improve(
            "Do thing",
            "Some description",
            "feature",
            "agent-hub",
            runners=(("haiku", lambda _: ""), ("agy-flash", lambda _: "{bad")),
        )
        self.assertIsNone(got)

    def test_disabled_in_config_returns_none_without_calling_runners(self):
        called = []
        got = board_polish.improve(
            "Do thing",
            "Some description",
            "feature",
            "agent-hub",
            cfg={"board": {"polish": {"enabled": False}}},
            runners=(("haiku", lambda p: called.append(p) or "{}"),),
        )
        self.assertIsNone(got)
        self.assertEqual(called, [])


class PolishAsyncTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.board = Board(os.path.join(self.tmp, "hub.db"))

    def tearDown(self):
        self.board.close()
        self._t.__exit__()

    def test_async_success_updates_card_and_preserves_original(self):
        card = self.board.create("alpha", "fix login", description="login fails on empty password", label="feature")
        t = board_polish.polish_async(
            self.board,
            card["id"],
            runners=(("haiku", lambda _: json.dumps({
                "title": "Reject empty login password",
                "description": "Validate password before auth.\n\n- Return 400 on empty\n- Add test",
                "label": "bugfix",
            })),),
        )
        self.assertTrue(t.daemon)
        t.join(timeout=2)
        updated = self.board.get(card["id"])
        self.assertEqual(updated["polish"], "done")
        self.assertEqual(updated["polish_model"], "haiku")
        self.assertEqual(updated["title"], "Reject empty login password")
        self.assertEqual(updated["label"], "bugfix")
        self.assertEqual(updated["original"], {"title": "fix login", "description": "login fails on empty password"})

    def test_async_failure_marks_failed(self):
        card = self.board.create("alpha", "do x", description="some description")
        t = board_polish.polish_async(self.board, card["id"], runners=(("haiku", lambda _: "bad"),))
        t.join(timeout=2)
        updated = self.board.get(card["id"])
        self.assertEqual(updated["polish"], "failed")

    def test_async_marks_pending_while_the_model_runs(self):
        import threading
        gate = threading.Event()
        card = self.board.create("alpha", "do x", description="some description")
        t = board_polish.polish_async(self.board, card["id"], runners=(("haiku", lambda _: gate.wait(2) and "bad"),))
        self.assertEqual(self.board.get(card["id"])["polish"], "pending")
        gate.set()
        t.join(timeout=2)
        self.assertEqual(self.board.get(card["id"])["polish"], "failed")

    def test_async_result_is_dropped_when_the_owner_edits_or_reverts_meanwhile(self):
        import threading
        answer = json.dumps({"title": "Do x well", "description": "Do x.\n\n- one\n- two", "label": "feature"})
        for change in ("edit", "delete"):
            gate = threading.Event()
            card = self.board.create("alpha", "do x", description="some description")
            t = board_polish.polish_async(self.board, card["id"], runners=(("haiku", lambda _: gate.wait(2) and answer),))
            if change == "edit":
                self.board.update(card["id"], title="do y instead")
            else:
                self.board.delete(card["id"])
            gate.set()
            t.join(timeout=2)
            now = self.board.get(card["id"])
            if change == "edit":
                self.assertEqual(now["title"], "do y instead")
                self.assertIsNone(now["original"])
            else:
                self.assertIsNone(now)

    def test_async_crash_in_improve_marks_failed_not_pending(self):
        def boom(_):
            raise RuntimeError("model CLI exploded")
        card = self.board.create("alpha", "do x", description="some description")
        t = board_polish.polish_async(self.board, card["id"], runners=(("haiku", boom),))
        t.join(timeout=2)
        self.assertEqual(self.board.get(card["id"])["polish"], "failed")

    def test_async_disabled_marks_off_and_unknown_card_is_noop(self):
        self.assertIsNone(board_polish.polish_async(self.board, "c_missing"))
        card = self.board.create("alpha", "do x")
        res = board_polish.polish_async(self.board, card["id"], cfg={"board": {"polish": {"enabled": False}}})
        self.assertIsNone(res)
        self.assertEqual(self.board.get(card["id"])["polish"], "off")


class RunnerCommandTests(unittest.TestCase):
    def test_run_claude_and_agy_flags_and_env(self):
        from pathlib import Path
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"ok": true}')
        with TempDir() as tmp, patch("hub.board_polish.brand.home", return_value=Path(tmp)):
            with patch("hub.board_polish.subprocess.run", return_value=fake) as mock_run:
                out = board_polish.run_claude("hello", timeout=45, model="haiku")
            self.assertEqual(out, '{"ok": true}')
            cmd = mock_run.call_args[0][0]
            kw = mock_run.call_args[1]
            self.assertEqual(cmd, ["claude", "-p", "--model", "haiku", "--tools", "", "--setting-sources", "local"])
            self.assertEqual(kw["input"], "hello")
            self.assertEqual(kw["timeout"], 45)
            self.assertEqual(kw["cwd"], str(Path(tmp)))
            self.assertEqual(kw["env"]["JARVIEE_HOOK_SKIP"], "1")

            with patch("hub.board_polish.subprocess.run", return_value=fake) as mock_run:
                out = board_polish.run_agy("hello", timeout=60, cfg={"tools": {"agy": {"models": {"basic": "gemini-3.8-flash-low"}}}})
            self.assertEqual(out, '{"ok": true}')
            cmd = mock_run.call_args[0][0]
            kw = mock_run.call_args[1]
            self.assertIn("--mode", cmd)
            self.assertIn("plan", cmd)
            self.assertIn("--sandbox", cmd)
            self.assertEqual(kw["env"]["JARVIEE_HOOK_SKIP"], "1")


if __name__ == "__main__":
    unittest.main()
