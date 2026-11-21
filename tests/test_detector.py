import unittest
import time
from hub import detector

AGY_429_TEXT = (
    '$ agy --model gemini-4-argon-high, effort high  (tier: complex)\n\n'
    'error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 151h36m47s.\r\n'
    'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. '
    'Please upgrade your subscription to increase your limits. Resets in 151h36m47s.",'
    '"status":"RESOURCE_EXHAUSTED","error_code":429,"code_kind":"http","retryable":true,'
    '"error_id":"c4fce703-c1ee-4dd6-940d-b0e90c5bac59-1"}\r\n'
)

CLAUDE_LIMIT_TEXT = (
    "You've reached your usage limit. Resets at 3:00 PM. Upgrade to increase limits."
)

CLAUDE_429_TEXT = (
    "API Error 429 rate_limit_error: Organization rate limit reached. Resets in 2h 15m."
)

TRANSIENT_529_TEXT = (
    "Error: 529 Overloaded. Claude is experiencing high traffic. Please try again later."
)

TRANSIENT_503_TEXT = (
    "HTTP 503 Service Unavailable: backend gateway connection reset."
)

AUTH_ERROR_TEXT = (
    "Error: Not logged in. Please run `claude login` to authenticate."
)

BINARY_MISSING_TEXT = (
    "bash: agy: command not found"
)

TASK_FAILURE_TEXT = (
    "Running test suite...\n"
    "AssertionError: Expected 200 OK but got 404 Not Found\n"
    "FAILED (failures=1)"
)


class DetectorTests(unittest.TestCase):
    def test_parse_reset_time_duration(self):
        now = 1700000000.0
        # 151h 36m 47s = 151*3600 + 36*60 + 47 = 543600 + 2160 + 47 = 545807
        t = detector.parse_reset_time("Resets in 151h36m47s", now=now)
        self.assertEqual(t, now + 545807)

        # resets in 2h 15m
        t2 = detector.parse_reset_time("Resets in 2h 15m", now=now)
        self.assertEqual(t2, now + 8100)

        # resets in 30s
        t3 = detector.parse_reset_time("resets in 30s", now=now)
        self.assertEqual(t3, now + 30)

    def test_parse_reset_time_clock(self):
        # 2026-11-09 10:00:00 local time
        now = time.mktime((2026, 11, 9, 10, 0, 0, 0, 0, -1))
        # resets at 3pm (today 15:00)
        t = detector.parse_reset_time("resets at 3pm", now=now)
        expected = time.mktime((2026, 11, 9, 15, 0, 0, 0, 0, -1))
        self.assertEqual(t, expected)

        # if reset time is in the past for today, target tomorrow
        t_past = detector.parse_reset_time("resets at 9am", now=now)
        expected_tomorrow = time.mktime((2026, 11, 10, 9, 0, 0, 0, 0, -1))
        self.assertEqual(t_past, expected_tomorrow)

    def test_detect_agy_individual_quota(self):
        now = 1700000000.0
        res = detector.detect_failure("agy", 3, AGY_429_TEXT, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.QUOTA)
        self.assertEqual(res.scope, "tool")
        self.assertEqual(res.reset_time, now + 545807)
        self.assertFalse(res.retryable)

    def test_detect_claude_usage_limit(self):
        now = time.mktime((2026, 11, 9, 10, 0, 0, 0, 0, -1))
        res = detector.detect_failure("claude", 1, CLAUDE_LIMIT_TEXT, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.QUOTA)
        expected_3pm = time.mktime((2026, 11, 9, 15, 0, 0, 0, 0, -1))
        self.assertEqual(res.reset_time, expected_3pm)

    def test_detect_claude_rate_limit_model_scope(self):
        now = 1700000000.0
        res = detector.detect_failure("claude", 1, CLAUDE_429_TEXT, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.QUOTA)
        self.assertEqual(res.reset_time, now + 8100)

    def test_detect_transient_529(self):
        now = 1700000000.0
        res = detector.detect_failure("claude", 1, TRANSIENT_529_TEXT, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.TRANSIENT)
        self.assertTrue(res.retryable)

    def test_detect_transient_503(self):
        now = 1700000000.0
        res = detector.detect_failure("agy", 1, TRANSIENT_503_TEXT, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.TRANSIENT)
        self.assertTrue(res.retryable)

    def test_detect_auth_error(self):
        res = detector.detect_failure("claude", 1, AUTH_ERROR_TEXT)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.AUTH)
        self.assertEqual(res.scope, "tool")

    def test_detect_binary_missing(self):
        res = detector.detect_failure("agy", 127, BINARY_MISSING_TEXT)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.AUTH)

    def test_detect_task_failure(self):
        res = detector.detect_failure("claude", 1, TASK_FAILURE_TEXT)
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, detector.TASK_FAILURE)
        self.assertFalse(res.retryable)


if __name__ == "__main__":
    unittest.main()
