import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from hub import apple_calendar as ac


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CalendarTests(unittest.TestCase):
    now = datetime(2026, 11, 11, 9, 0)   # a Wednesday

    def setUp(self):
        p = mock.patch.object(ac, "is_enabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_detects_schedule_questions(self):
        for t in ("what's my schedule today", "what's coming next week", "am I free tomorrow"):
            self.assertTrue(ac.is_schedule_question(t), t)
        for t in ("fix the calendar bug", "refactor the router", "",
                  "i want to receive a notification 15 minutes before a schedule",
                  "can it automate reminders for my calendar events?",
                  "send me my schedule on my phone"):
            self.assertFalse(ac.is_schedule_question(t), t)

    def test_windows_only_today_and_tomorrow(self):
        lo, hi, label = ac.window("anything next week", self.now)
        self.assertEqual((lo.day, hi.day, label), (11, 13, "today and tomorrow"))
        self.assertEqual(ac.window("what's on tomorrow", self.now)[0].day, 12)

    def _runner(self, items):
        return lambda argv: SimpleNamespace(returncode=0, stdout=json.dumps(items))

    def test_schedule_and_alerts(self):
        s = self.now.replace(hour=13)
        ev = [{"kind": "event", "title": "Automata", "start": iso(s), "end": iso(s + timedelta(hours=1, minutes=30)), "all_day": False, "location": "RM 205", "calendar": "Automata"},
              {"kind": "reminder", "title": "Data Science assignment", "start": iso(self.now.replace(hour=0) + timedelta(days=2)), "end": iso(self.now.replace(hour=0) + timedelta(days=2)), "all_day": True, "calendar": "School"},
              {"kind": "reminder", "title": "Spotify renewal", "start": iso(self.now.replace(hour=0) + timedelta(days=1)), "end": iso(self.now.replace(hour=0) + timedelta(days=1)), "all_day": True, "calendar": "Reminders"},
              {"kind": "reminder", "title": "Essay far away", "start": iso(self.now.replace(hour=0) + timedelta(days=6)), "end": iso(self.now.replace(hour=0) + timedelta(days=6)), "all_day": True, "calendar": "School"},
              {"kind": "reminder", "title": "Netflix subscription", "start": iso(self.now.replace(hour=0) + timedelta(days=3)), "end": iso(self.now.replace(hour=0) + timedelta(days=3)), "all_day": True, "calendar": "Reminders"},
              {"kind": "reminder", "title": "iCloud+ plan", "start": iso(self.now.replace(hour=0) + timedelta(days=5)), "end": iso(self.now.replace(hour=0) + timedelta(days=5)), "all_day": True, "calendar": "Reminders"},
              {"kind": "reminder", "title": "Adobe subscription", "start": iso(self.now.replace(hour=0) + timedelta(days=6)), "end": iso(self.now.replace(hour=0) + timedelta(days=6)), "all_day": True, "calendar": "Reminders"}]
        seen = []
        runner = self._runner(ev)
        al, sched = ac.answer_parts("schedule today", self.now, lambda argv: seen.append(argv) or runner(argv))
        self.assertGreaterEqual(int(seen[0][2]), 6)   # reads far enough ahead to see a subscription 5 days out
        self.assertIn("Automata, 1 pm to 2:30 pm, at RM 205", sched)
        text = " ".join(al)
        self.assertIn("Spotify renewal tomorrow.", text)
        self.assertIn("Data Science assignment is due on Friday (in 2 days)", text)
        self.assertIn("Your Netflix subscription ends on Saturday (in 3 days)", text)
        self.assertIn("iCloud+ plan subscription ends on Monday (in 5 days)", text)
        self.assertNotIn("Adobe", text)      # subscription 6 days out is beyond 5
        self.assertNotIn("Essay", text)      # beyond 3 days
        self.assertTrue(ac.answer("schedule today", self.now, self._runner(ev)).startswith("HEADS UP"))

    def test_class_sessions_are_not_assignments_and_renewals_read_naturally(self):
        d = self.now.replace(hour=0)
        evs = [{"kind": "event", "title": "App Dev & Emerging Tech (Lab)", "start": iso(d.replace(hour=16) + timedelta(days=2)), "end": iso(d.replace(hour=19) + timedelta(days=2)), "all_day": False, "location": "CLD-1", "calendar": "App Dev & Emerging Tech"},
               {"kind": "event", "title": "Lab report", "start": iso(d + timedelta(days=1)), "end": iso(d + timedelta(days=2)), "all_day": True, "location": "", "calendar": "School"},
               {"kind": "event", "title": "Claude subscription renews", "start": iso(d + timedelta(days=4)), "end": iso(d + timedelta(days=5)), "all_day": True, "location": "", "calendar": "Claude"}]
        lines = ac.alert_lines(ac.alerts(evs, self.now))
        self.assertEqual(lines, ["Lab report is due tomorrow.", "Claude subscription renews on Sunday (in 4 days)."])

    def test_past_skipped_ongoing_marked(self):
        d = self.now.replace(hour=0)
        def e(t, a, b):
            return {"kind": "event", "title": t, "start": iso(d.replace(hour=a)), "end": iso(d.replace(hour=b)), "all_day": False, "location": "", "calendar": "x"}
        evs = [e("Done class", 7, 8), e("Current class", 8, 11), e("Later class", 13, 15)]   # now is 09:00
        _, sched = ac.answer_parts("schedule today", self.now, self._runner(evs))
        self.assertNotIn("Done class", sched)
        self.assertIn("Current class is ongoing, until 11 am", sched)
        self.assertIn("Later class, 1 pm to 3 pm", sched)

    def test_errors(self):
        denied = lambda argv: SimpleNamespace(returncode=2, stdout="")
        with self.assertRaises(ac.CalendarError):
            ac.fetch(runner=denied)
