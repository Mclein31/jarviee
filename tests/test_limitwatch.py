"""Phone alerts for the Claude 5-hour limit: spent once, back once (hub/limitwatch.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import limitwatch   # noqa: E402

NOW = 1_800_000_000.0


def limits(used, resets):
    return {"windows": [{"id": "five_hour", "used_pct": used, "resets_at": resets},
                        {"id": "seven_day", "used_pct": 100, "resets_at": NOW + 86400}]}


class FakeStore:
    def __init__(self, q):
        self.q = q

    def get_quota(self, tool, model=None, now=None):
        return self.q


class LimitWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"push": {"enabled": True}, "limit_state_path": os.path.join(self.tmp.name, "limit.json")}
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, cfg, title, message, at=None, seq=None, priority=4, tags=None, opener=None):
        self.sent.append({"title": title, "message": message, "at": at, "seq": seq})
        return True, ""

    def check(self, now, lim, store=None):
        return limitwatch.check(self.cfg, now=now, limits=lim, store=store, publish=self.publish)

    def test_room_left_sends_nothing(self):
        self.assertEqual(self.check(NOW, limits(82, NOW + 3600)), [])
        self.assertEqual(self.sent, [])

    def test_spent_sends_once_and_schedules_the_return(self):
        reset = NOW + 2 * 3600
        self.assertEqual(self.check(NOW, limits(100, reset)), ["spent", "back-scheduled"])
        self.assertIn("spent", self.sent[0]["title"])
        self.assertIn("2 h 00 min", self.sent[0]["message"])
        self.assertIsNone(self.sent[0]["at"])
        self.assertEqual(self.sent[1]["at"].timestamp(), reset)
        self.assertEqual(self.sent[1]["seq"], f"limit-back-{int(reset)}")
        # later polls of the same window, and the moment after the reset, stay quiet: ntfy holds the "back" alert
        self.assertEqual(self.check(NOW + 600, limits(100, reset)), [])
        self.assertEqual(self.check(reset + 60, limits(None, reset)), [])
        self.assertEqual(len(self.sent), 2)

    def test_next_window_is_announced_again(self):
        self.check(NOW, limits(100, NOW + 3600))
        self.assertEqual(self.check(NOW + 6 * 3600, limits(100, NOW + 9 * 3600)), ["spent", "back-scheduled"])

    def test_back_is_sent_locally_when_it_could_not_be_scheduled(self):
        reset = NOW + 10   # too close for ntfy to schedule
        self.assertEqual(self.check(NOW, limits(100, reset)), ["spent"])
        self.assertEqual(self.check(reset + 5, limits(None, reset)), ["back"])
        self.assertEqual(self.check(reset + 65, limits(None, reset)), [])

    def test_failed_publish_is_retried(self):
        def down(*a, **k):
            return False, "ntfy unreachable"
        self.assertEqual(limitwatch.check(self.cfg, now=NOW, limits=limits(100, NOW + 3600), publish=down), [])
        self.assertEqual(self.check(NOW + 60, limits(100, NOW + 3600)), ["spent", "back-scheduled"])

    def test_quota_error_from_a_run_counts_as_spent(self):
        store = FakeStore({"status": "exhausted", "until": NOW + 1800})
        self.assertEqual(self.check(NOW, limits(97, NOW + 1800), store=store), ["spent", "back-scheduled"])
        self.assertEqual(self.check(NOW, None, store=FakeStore({"status": "ok", "until": None})), [])

    def test_off_switches(self):
        self.cfg["push"]["limit_alerts"] = False
        self.assertEqual(self.check(NOW, limits(100, NOW + 3600)), [])
        self.cfg["push"] = {"enabled": False}
        self.assertEqual(self.check(NOW, limits(100, NOW + 3600)), [])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
