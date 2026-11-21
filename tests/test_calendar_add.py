"""Add calendar events from the command bar: the parser (fixed 'now'), and the routes with the writer mocked.
Nothing here touches the real Calendar app: fetch / apply / build_app are always replaced."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from unittest import mock

from helpers import TempDir, make_cfg
from hub import apple_calendar as ac, auth, calendar_add as ca
from hub.server import make_server
from hub.store import Store

NOW = datetime(2026, 11, 12, 14, 0)   # a Thursday, 2 pm
CALS = ["Classes", "Personal", "Data Science"]
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def p(text, cals=CALS, now=NOW):
    return ca.parse(text, now, cals)


class ParserTests(unittest.TestCase):
    def check(self, text, title, start, end=None, **kw):
        r = p(text)
        self.assertIsNotNone(r, text)
        self.assertEqual(r["title"], title, text)
        self.assertEqual(r["start"], start, text)
        if end:
            self.assertEqual(r["end"], end, text)
        for k, v in kw.items():
            self.assertEqual(r[k], v, f"{text}: {k}")
        return r

    def test_the_example(self):
        r = self.check("add a Data Science quiz Friday 3 pm", "Data Science quiz", "2026-11-13T15:00", "2026-11-13T16:00",
                       all_day=False, calendar=None, day_label="Friday, Nov 13")
        self.assertEqual((r["start_label"], r["end_label"]), ("3:00 PM", "4:00 PM"))

    def test_verbs(self):
        for t in ("create a quiz Friday 3pm", "schedule a quiz Friday 3pm", "please add quiz Friday 3pm",
                  "can you add quiz Friday 3pm", "book a quiz Friday 3pm", "pencil in quiz Friday 3pm"):
            self.check(t, "Quiz", "2026-11-13T15:00")
        self.check("remind me to call mom tomorrow 5pm", "Call mom", "2026-11-13T17:00")
        self.check("remind me about the quiz Friday", "Quiz", "2026-11-13T00:00", all_day=True)
        self.check("put the quiz on my calendar Friday 3pm", "Quiz", "2026-11-13T15:00")

    def test_days(self):
        self.check("add x today 5pm", "X", "2026-11-12T17:00")
        self.check("add x tomorrow 5pm", "X", "2026-11-13T17:00")
        self.check("add x day after tomorrow 5pm", "X", "2026-11-14T17:00")
        self.check("add x on Monday 9am", "X", "2026-11-16T09:00")
        self.check("add x this Friday 9am", "X", "2026-11-13T09:00")
        self.check("add x next Friday 9am", "X", "2026-11-20T09:00")         # the following week, not tomorrow
        self.check("add x next Monday 9am", "X", "2026-11-16T09:00")
        self.check("add x Thursday 3pm", "X", "2026-11-12T15:00")             # today, still ahead
        self.check("add x Nov 20 9am", "X", "2026-11-20T09:00")
        self.check("add x November 20th 9am", "X", "2026-11-20T09:00")
        self.check("add x 20 Nov 9am", "X", "2026-11-20T09:00")
        self.check("add x 11/20 9am", "X", "2026-11-20T09:00")
        self.check("add x 11/20/2027 9am", "X", "2027-11-20T09:00")
        self.check("add x on the 20th 9am", "X", "2026-11-20T09:00")
        self.check("add x on the 5th 9am", "X", "2026-12-05T09:00")           # this month's 5th has passed
        self.check("add x Nov 5 9am", "X", "2027-11-05T09:00")                # no year: next occurrence

    def test_times(self):
        self.check("add x Friday 3:30pm", "X", "2026-11-13T15:30")
        self.check("add x Friday 15:00", "X", "2026-11-13T15:00")
        self.check("add x Friday at noon", "X", "2026-11-13T12:00")
        self.check("add x Friday 9 am", "X", "2026-11-13T09:00")
        self.check("add x Friday at 3", "X", "2026-11-13T15:00")              # 1-6 reads as afternoon
        self.check("add x Friday at 9", "X", "2026-11-13T09:00")
        self.check("add x Friday 3-4 pm", "X", "2026-11-13T15:00", "2026-11-13T16:00")
        self.check("add x Friday 11-1 pm", "X", "2026-11-13T11:00", "2026-11-13T13:00")
        self.check("add x Friday from 3 to 4:30", "X", "2026-11-13T15:00", "2026-11-13T16:30")
        self.check("add x Friday 3pm - 5pm", "X", "2026-11-13T15:00", "2026-11-13T17:00")
        self.check("add x Friday 15:00-16:30", "X", "2026-11-13T15:00", "2026-11-13T16:30")
        self.check("add x Friday 10pm-1am", "X", "2026-11-13T22:00", "2026-11-14T01:00")

    def test_duration_and_default(self):
        self.check("add x Friday 3pm for 2 hours", "X", "2026-11-13T15:00", "2026-11-13T17:00")
        self.check("add x Friday 3pm for 90 minutes", "X", "2026-11-13T15:00", "2026-11-13T16:30")
        self.check("add x Friday 3pm for half an hour", "X", "2026-11-13T15:00", "2026-11-13T15:30")
        self.check("add x Friday 3pm for an hour", "X", "2026-11-13T15:00", "2026-11-13T16:00")
        self.check("add x Friday 3pm", "X", "2026-11-13T15:00", "2026-11-13T16:00")

    def test_all_day_when_no_time(self):
        r = self.check("add hackathon Nov 20", "Hackathon", "2026-11-20T00:00", all_day=True)
        self.assertEqual((r["start_label"], r["end_label"]), ("", ""))

    def test_past_time_rolls_only_when_day_not_stated(self):
        self.check("add dentist at 9 am", "Dentist", "2026-11-13T09:00", day_stated=False)   # 9 am has passed today
        self.check("add gym at 5pm", "Gym", "2026-11-12T17:00", day_stated=False)            # still ahead today
        r = self.check("add dentist today at 9 am", "Dentist", "2026-11-12T09:00", day_stated=True)
        self.assertIn("passed", r["note"])

    def test_place(self):
        self.check("schedule dentist tomorrow at 9am in Room 204", "Dentist", "2026-11-13T09:00", location="Room 204")
        self.check("add quiz Friday 3pm in the library", "Quiz", "2026-11-13T15:00", location="the library")
        self.check("add lunch Friday noon at Cafe Nero", "Lunch", "2026-11-13T12:00", location="Cafe Nero")
        self.check("add check in with Sam Friday 3pm", "Check in with Sam", "2026-11-13T15:00", location="")

    def test_calendar_matched_against_real_ones(self):
        self.check("add quiz Friday 3pm in Classes", "Quiz", "2026-11-13T15:00", calendar="Classes")
        self.check("add quiz Friday 3pm on my Classes calendar", "Quiz", "2026-11-13T15:00", calendar="Classes")
        self.check("add quiz on Friday 3pm to my personal calendar", "Quiz", "2026-11-13T15:00", calendar="Personal")
        self.check("add quiz Friday 3pm in my Data Science calendar", "Quiz", "2026-11-13T15:00", calendar="Data Science")
        r = self.check("add quiz Friday 3pm on my Gym calendar", "Quiz", "2026-11-13T15:00", calendar=None)
        self.assertEqual(r["calendar_hint"], "Gym")        # said, but it does not exist: the UI shows the picker
        self.assertIsNone(p("add quiz Friday 3pm")["calendar"])
        self.assertEqual(ca.parse("add quiz Friday 3pm", NOW, ["Only"])["calendar"], "Only")
        self.assertIsNone(self.check("add quiz Friday 3pm in the library", "Quiz", "2026-11-13T15:00")["calendar"])

    def test_refuses_what_is_not_clearly_an_event(self):
        for t in ("notify me 15 minutes before a schedule", "automate sending reminders for my schedule",
                  "send the schedule to Sam", "send me the calendar every Friday", "what's on my schedule tomorrow",
                  "am I free on Friday at 3 pm", "fix the calendar bug tomorrow", "add a login button on Friday",
                  "add a notification 15 minutes before a schedule", "add dark mode to the dashboard tomorrow",
                  "create an endpoint for calendar events by Friday", "add tests for the parser Friday",
                  "refactor the scheduler on Friday", "schedule a call", "add a quiz", "add weekly standup Monday 9am",
                  "add quiz every Friday 3pm", "put the button on the left", "put it in my calendar",
                  "", "   ", "add", "build an app and add a quiz Friday 3pm", "x" * 300,
                  "add a Data Science quiz\nFriday 3 pm", "remind me when the build finishes",
                  "create a plan for the calendar feature tomorrow"):
            self.assertIsNone(p(t), t)

    def test_regression_tasks_that_mention_the_calendar_still_launch_agents(self):
        for t in ("notify me before a schedule", "automate reminders for my calendar events",
                  "send the weekly schedule by email", "make the schedule page show tomorrow"):
            self.assertIsNone(p(t), t)
            self.assertFalse(ac.is_schedule_question(t), t)


class _Server(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.token = auth.get_token(self.cfg)
        ca._cache.update(at=0.0, names=[], fail_at=0.0, fail="")
        ca._PENDING.clear()
        self.events = [{"kind": "event", "title": t, "calendar": c, "start": "2026-11-13T01:00:00Z", "end": "2026-11-13T02:00:00Z",
                        "all_day": False, "location": ""} for t, c in (("Lecture", "Classes"), ("Gym", "Personal"))]
        self.fetch = mock.patch.object(ac, "fetch", side_effect=lambda *a, **k: list(self.events)).start()
        self.apply = mock.patch.object(ac, "apply", return_value={"created": [], "skipped": [], "errors": []}).start()
        mock.patch.object(ac, "is_enabled", return_value=True).start()
        mock.patch.object(ac, "build_app", side_effect=AssertionError("the helper app must never be built in tests")).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def post(self, path, body, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Jarviee-Token"] = self.token
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")


EV = {"title": "Quiz", "calendar": "Classes", "start": "2026-11-13T15:00", "end": "2026-11-13T16:00", "all_day": False, "location": "Room 4"}


class RouteTests(_Server):
    def test_parse_has_no_side_effects_and_lists_real_calendars(self):
        code, r = self.post("/api/calendar/parse", {"text": "add a Data Science quiz Friday 3 pm"})
        self.assertEqual(code, 200)
        self.assertTrue(r["matched"])
        self.assertEqual(r["calendars"], ["Classes", "Personal"])
        self.assertEqual(r["event"]["title"], "Data Science quiz")
        self.apply.assert_not_called()

    def test_parse_of_an_ordinary_task_reads_nothing(self):
        code, r = self.post("/api/calendar/parse", {"text": "notify me before a schedule"})
        self.assertEqual((code, r), (200, {"matched": False}))
        self.fetch.assert_not_called()
        self.apply.assert_not_called()

    def test_parse_survives_a_calendar_that_cannot_be_read(self):
        self.fetch.side_effect = ac.CalendarError("Calendar access is off")
        code, r = self.post("/api/calendar/parse", {"text": "add quiz Friday 3pm"})
        self.assertTrue(r["matched"])
        self.assertEqual((r["calendars"], r["calendars_error"]), ([], "Calendar access is off"))

    def test_a_failed_read_is_remembered_for_a_minute_not_retried_per_keystroke(self):
        self.fetch.side_effect = ac.CalendarError("Calendar access is off")
        for text in ("add quiz Friday 3pm", "add quiz Friday 3pm now", "add quiz Friday 3pm now ok"):
            r = self.post("/api/calendar/parse", {"text": text})[1]
            self.assertEqual(r["calendars_error"], "Calendar access is off")
        self.assertEqual(self.fetch.call_count, 1)
        ca._cache["fail_at"] -= ca.FAIL_CACHE_S + 1   # a minute later it asks again
        self.post("/api/calendar/parse", {"text": "add quiz Friday 3pm"})
        self.assertEqual(self.fetch.call_count, 2)

    def test_parse_never_builds_the_reader_app(self):
        mock.patch.stopall()
        mock.patch.object(ac, "is_enabled", return_value=True).start()
        build = mock.patch.object(ac, "build_app", side_effect=AssertionError("must not build")).start()
        mock.patch.object(ac, "app_built", return_value=False).start()
        ran = mock.patch.object(ac, "_run_app", side_effect=AssertionError("must not run")).start()
        r = self.post("/api/calendar/parse", {"text": "add quiz Friday 3pm"})[1]
        self.assertTrue(r["matched"])
        self.assertIn("not built", r["calendars_error"])
        build.assert_not_called()
        ran.assert_not_called()

    def test_write_routes_need_the_token(self):
        for path, body in (("/api/calendar/add", {"event": EV}), ("/api/calendar/undo", {"undo": "x"}), ("/api/calendar/parse", {"text": "x"})):
            self.assertEqual(self.post(path, body, token=False)[0], 403, path)
        self.apply.assert_not_called()

    def test_ask_never_answers_an_add_request_as_a_schedule_read(self):
        for t in ("schedule a quiz Friday 3pm", "add a quiz tomorrow"):
            self.assertEqual(self.post("/api/ask", {"question": t})[1], {"handled": False}, t)
        self.fetch.assert_not_called()

    def test_add_writes_one_validated_event(self):
        self.apply.return_value = {"created": ["Quiz"], "skipped": [], "errors": []}
        code, r = self.post("/api/calendar/add", {"event": EV})
        self.assertEqual((code, r["ok"], r["created"]), (200, True, True))
        spec = self.apply.call_args[0][0]
        self.assertEqual(spec, {"events": [{"calendar": "Classes", "title": "Quiz", "start": "2026-11-13T15:00",
                                            "end": "2026-11-13T16:00", "all_day": False, "location": "Room 4"}]})
        self.assertTrue(r["undo"])

    def test_add_rejects_bad_input_without_writing(self):
        for ev in ({**EV, "title": " "}, {**EV, "calendar": "Nope"}, {**EV, "start": "tomorrow"},
                   {**EV, "end": "2026-11-13T14:00"}, {**EV, "start": "2026-13-45T15:00", "end": "2026-13-45T16:00"}):
            self.assertEqual(self.post("/api/calendar/add", {"event": ev})[0], 400, ev)
        self.assertEqual(self.post("/api/calendar/add", {})[0], 400)
        self.apply.assert_not_called()

    def test_writer_errors_are_reported_not_hidden(self):
        self.apply.return_value = {"created": [], "skipped": [], "errors": ["event Quiz: bad calendar/date"]}
        code, r = self.post("/api/calendar/add", {"event": EV})
        self.assertEqual((r["ok"], r["error"]), (False, "event Quiz: bad calendar/date"))
        self.apply.side_effect = ac.CalendarError("the calendar writer returned nothing")
        self.assertFalse(self.post("/api/calendar/add", {"event": EV})[1]["ok"])

    def test_undo_deletes_only_what_was_just_created_and_only_once(self):
        self.apply.return_value = {"created": ["Quiz"], "skipped": [], "errors": []}
        tok = self.post("/api/calendar/add", {"event": EV})[1]["undo"]
        self.apply.return_value = {"deleted": ["Quiz (Classes)"], "errors": []}
        code, r = self.post("/api/calendar/undo", {"undo": tok})
        self.assertEqual((code, r["ok"]), (200, True))
        self.assertEqual(self.apply.call_args[0][0], {"delete_events": [{"calendar": "Classes", "title": "Quiz"}]})
        n = self.apply.call_count
        self.assertEqual(self.post("/api/calendar/undo", {"undo": tok})[0], 410)      # single use
        self.assertEqual(self.post("/api/calendar/undo", {"undo": "guess"})[0], 410)   # arbitrary deletes are impossible
        self.assertEqual(self.apply.call_count, n)

    def test_undo_expires(self):
        self.apply.return_value = {"created": ["Quiz"], "skipped": [], "errors": []}
        tok = self.post("/api/calendar/add", {"event": EV})[1]["undo"]
        ca._PENDING[tok] = (*ca._PENDING[tok][:2], 0)
        n = self.apply.call_count
        self.assertEqual(self.post("/api/calendar/undo", {"undo": tok})[0], 410)
        self.assertEqual(self.apply.call_count, n)

    def test_no_undo_when_the_event_already_existed_or_the_title_is_shared(self):
        self.apply.return_value = {"created": [], "skipped": ["Quiz"], "errors": []}
        r = self.post("/api/calendar/add", {"event": EV})[1]
        self.assertEqual((r["ok"], r["skipped"], r["undo"]), (True, True, ""))       # never offer to delete what was not made here
        self.events.append({"kind": "event", "title": "Quiz", "calendar": "Classes", "start": "2026-11-01T01:00:00Z",
                            "end": "2026-11-01T02:00:00Z", "all_day": False, "location": ""})
        self.apply.return_value = {"created": ["Quiz"], "skipped": [], "errors": []}
        r = self.post("/api/calendar/add", {"event": EV})[1]
        self.assertEqual(r["undo"], "")
        self.assertIn("Undo is off", r["undo_note"])


if __name__ == "__main__":
    unittest.main()
