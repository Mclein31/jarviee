"""The Claude rate limits the status line saves (hooks/statusline.py): idle sessions can't drag a figure down,
but a session working right now sets the current figure even when it is lower."""
import importlib.util
import os
import time
import unittest
from pathlib import Path

from helpers import ROOT, TempDir

spec = importlib.util.spec_from_file_location("statusline", ROOT / "hooks" / "statusline.py")
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)

WEEK = 1794934800


def rl(five, week, wreset=WEEK):
    return {"five_hour": {"used_percentage": five, "resets_at": 1794688200}, "seven_day": {"used_percentage": week, "resets_at": wreset}}


class Merge(unittest.TestCase):
    def test_idle_session_cannot_lower_it(self):
        self.assertEqual(sl.merge(rl(40, 100), rl(30, 60))["seven_day"]["used_percentage"], 100)

    def test_fresh_session_sets_the_current_figure(self):
        m = sl.merge(rl(40, 100), rl(43, 61), is_fresh=True)
        self.assertEqual(m["seven_day"]["used_percentage"], 61)
        self.assertEqual(m["five_hour"]["used_percentage"], 43)

    def test_new_window_wins_and_old_window_is_ignored(self):
        self.assertEqual(sl.merge(rl(40, 100), rl(5, 2, WEEK + 7 * 86400))["seven_day"]["used_percentage"], 2)
        self.assertEqual(sl.merge(rl(40, 50), rl(40, 90, WEEK - 7 * 86400), is_fresh=True)["seven_day"]["used_percentage"], 50)

    def test_fresh_means_transcript_written_recently(self):
        with TempDir() as tmp:
            t = Path(tmp) / "s.jsonl"
            t.write_text("{}")
            self.assertTrue(sl.fresh({"transcript_path": str(t)}))
            old = time.time() - 600
            os.utime(t, (old, old))
            self.assertFalse(sl.fresh({"transcript_path": str(t)}))
        self.assertFalse(sl.fresh({}))
        self.assertFalse(sl.fresh({"transcript_path": "/nope"}))


if __name__ == "__main__":
    unittest.main()
