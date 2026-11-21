import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hub import push


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(title, start, mins=60, kind="event", all_day=False, where=""):
    return {"title": title, "start": iso(start), "end": iso(start + timedelta(minutes=mins if kind == "event" else 0)),
            "all_day": all_day, "calendar": "School", "location": where, "kind": kind}


class FakeNtfy:
    """Stands in for urlopen: records every request, answers 200 (or `fail` status)."""
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, req, timeout=None):
        import urllib.error
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body))
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "nope", {}, None)

        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    def posts(self):
        return [b for m, _, b in self.calls if m == "POST"]


class PushTests(unittest.TestCase):
    now = datetime(2026, 11, 11, 9, 0)
    digest = False   # the morning schedule has its own tests (MorningSchedule)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"calendar": {"enabled": True},
                    "push": {"enabled": True, "digest_at": "07:00", "digest_enabled": self.digest},
                    "push_state_path": str(Path(self.tmp.name) / "push.json")}

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, events, now=None, ntfy=None):
        ntfy = ntfy or FakeNtfy()
        st = push.sync(self.cfg, now=now or self.now, fetch=lambda back, ahead: events, opener=ntfy)
        return st, ntfy

    def test_topic_is_random_private_and_kept(self):
        t = push.topic(self.cfg)
        self.assertRegex(t, r"^jarviee-[0-9a-f]{24}$")
        self.assertEqual(push.topic(self.cfg), t)
        self.assertEqual(os.stat(self.cfg["push_state_path"]).st_mode & 0o777, 0o600)
        self.assertNotEqual(push.topic({"push_state_path": str(Path(self.tmp.name) / "other.json")}), t)

    def test_plan_schedules_skips_and_sends_late(self):
        events = [ev("Mobile Computing 1", self.now + timedelta(hours=2), where="Dorm 1"),   # schedule at 10:45
                  ev("Started already", self.now - timedelta(minutes=5)),                  # skip
                  ev("Soon", self.now + timedelta(minutes=6)),                             # late: send now
                  ev("Holiday", self.now + timedelta(hours=3), all_day=True),              # summary only
                  ev("Far away", self.now + timedelta(hours=40)),                          # past the horizon
                  ev("Submit lab", self.now + timedelta(hours=4), kind="reminder")]
        acts = push.plan(events, {}, self.now, push.settings(self.cfg))
        got = [(a[0], a[1]["title"], a[2] if a[0] == "schedule" else None) for a in acts]
        self.assertEqual(got, [("now", "Soon", None),
                               ("schedule", "Mobile Computing 1", self.now + timedelta(hours=1, minutes=45)),
                               ("schedule", "Submit lab", self.now + timedelta(hours=3, minutes=45))])

    def test_messages(self):
        it = push._item(ev("Fundamentals of Data Science", datetime(2026, 11, 11, 13, 0), mins=180, where="CLB"))
        self.assertEqual(push.message(it, 15)[:2], ("Fundamentals of Data Science starts in 15 min", "1 pm to 4 pm · CLB"))
        late = push.message(it, 15, now=datetime(2026, 11, 11, 12, 54))
        self.assertEqual(late[0], "Fundamentals of Data Science starts in 6 min")
        self.assertIn("asleep", late[1])
        r = push._item(ev("Submit case study", datetime(2026, 11, 11, 23, 59), kind="reminder"))
        self.assertEqual(push.message(r, 15)[:2], ("Due in 15 min: Submit case study", "Reminder due at 11:59 pm · School"))

    def test_rotate_topic_takes_back_queued_and_changes_name(self):
        old = push.topic(self.cfg)
        self.sync([ev("Quiz", self.now + timedelta(hours=2))], now=self.now.replace(hour=6))
        n = FakeNtfy()
        new = push.rotate_topic(self.cfg, opener=n)
        self.assertNotEqual(new, old)
        self.assertEqual(push.topic(self.cfg), new)
        self.assertEqual([m for m, _, _ in n.calls], ["DELETE"])
        self.assertEqual(self.sync([ev("Quiz", self.now + timedelta(hours=2))], now=self.now.replace(hour=6))[0]["queued"], 1)

    def test_muted_calendar_sends_nothing_and_is_listed(self):
        self.cfg["push"]["mute_calendars"] = ["School"]
        st, n = self.sync([ev("Quiz", self.now + timedelta(hours=2))], now=self.now.replace(hour=6))
        self.assertEqual(n.posts(), [])
        self.assertEqual(push.status(self.cfg)["calendars"], ["School"])

    def test_sync_schedules_once_then_cancels_when_the_event_goes(self):
        a = ev("Mobile Computing 1", self.now + timedelta(hours=2))
        st, n = self.sync([a], now=self.now.replace(hour=6))   # before the 07:00 summary
        self.assertEqual(st["queued"], 1)
        (body,) = n.posts()
        self.assertEqual(body["title"], "Mobile Computing 1 starts in 15 min")
        self.assertEqual(int(body["delay"]), int((self.now + timedelta(hours=1, minutes=45)).timestamp()))
        self.assertTrue(body["sequence_id"].startswith("j"))
        self.assertEqual(self.sync([a], now=self.now.replace(hour=6))[1].calls, [])   # already queued: nothing new
        st, n = self.sync([], now=self.now.replace(hour=6, minute=5))                   # deleted from the calendar
        self.assertEqual([(m, u.rsplit("/", 1)[1]) for m, u, _ in n.calls], [("DELETE", body["sequence_id"])])
        self.assertEqual(st["queued"], 0)

    def test_moved_event_replaces_its_alert(self):
        old = ev("Lab", self.now + timedelta(hours=2))
        _, n1 = self.sync([old], now=self.now.replace(hour=6))
        _, n2 = self.sync([ev("Lab", self.now + timedelta(hours=3))], now=self.now.replace(hour=6))
        self.assertEqual([m for m, _, _ in n2.calls], ["POST", "DELETE"])
        self.assertNotEqual(n2.posts()[0]["sequence_id"], n1.posts()[0]["sequence_id"])

    def test_failure_is_retried_next_round(self):
        a = ev("Lab", self.now + timedelta(hours=2))
        st, _ = self.sync([a], now=self.now.replace(hour=6), ntfy=FakeNtfy(fail=500))
        self.assertIn("500", st["error"])
        self.assertEqual(len(self.sync([a], now=self.now.replace(hour=6))[1].posts()), 1)

    def test_off_does_nothing_and_takes_back_queued_alerts(self):
        self.sync([ev("Lab", self.now + timedelta(hours=2))], now=self.now.replace(hour=6))
        self.cfg["push"]["enabled"] = False
        self.assertEqual(self.sync([ev("Other", self.now + timedelta(hours=2))])[1].calls, [])
        n = FakeNtfy()
        # cancel_all compares against the real clock, so queue one far enough ahead to still be pending
        st = push._load(self.cfg)
        for rec in st["sent"].values():
            rec["fire"] = datetime.now().timestamp() + 3600
        push._save(self.cfg, st)
        push.cancel_all(self.cfg, opener=n)
        self.assertEqual([m for m, _, _ in n.calls], ["DELETE"])
        self.assertEqual(push._load(self.cfg)["sent"], {})

    def test_ssl_context_verifies_even_without_python_certs(self):
        ctx = push._ssl_context()
        self.assertEqual(ctx.verify_mode, push.ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreater(len(ctx.get_ca_certs()) + ctx.cert_store_stats()["x509_ca"], 0)

    def test_calendar_error_is_reported_not_raised(self):
        def boom(*a):
            raise push.apple_calendar.CalendarError("Calendar access is off")
        st = push.sync(self.cfg, now=self.now, fetch=boom, opener=FakeNtfy())
        self.assertEqual(st["error"], "Calendar access is off")


class MorningSchedule(unittest.TestCase):
    """The day's schedule at `digest_at`: queued on ntfy ahead of time, automatic, separate from /startwork."""
    digest = True
    now = PushTests.now
    setUp, tearDown, sync = PushTests.setUp, PushTests.tearDown, PushTests.sync

    def digests(self, n):
        return [b for b in n.posts() if str(b.get("sequence_id", "")).startswith("digest-") or "sequence_id" not in b]

    def test_morning_summary_once_a_day(self):
        events = [ev("Mobile Computing 1", self.now + timedelta(hours=2), where="Dorm 1"),
                  ev("Spotify renewal", self.now.replace(hour=0), kind="reminder", all_day=True)]
        _, n = self.sync(events)
        summary = [b for b in n.posts() if "sequence_id" not in b]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["title"], "Today: 1 event, 1 heads-up")
        self.assertIn("11 am · Mobile Computing 1 (Dorm 1)", summary[0]["message"])
        self.assertIn("Spotify", summary[0]["message"])
        self.assertEqual([b for b in self.sync(events, now=self.now + timedelta(minutes=5))[1].posts() if "sequence_id" not in b], [])

    def test_no_summary_late_in_the_day(self):
        _, n = self.sync([ev("Night class", self.now.replace(hour=19))], now=self.now.replace(hour=16))
        self.assertEqual([b for b in n.posts() if "sequence_id" not in b], [])

    def test_queued_ahead_for_the_morning_and_replaced_when_the_day_changes(self):
        six = self.now.replace(hour=6)
        a = ev("Lab", self.now + timedelta(hours=2))
        _, n = self.sync([a], now=six)
        (d,) = self.digests(n)
        self.assertTrue(d["sequence_id"].startswith("digest-20261111-"))
        self.assertEqual(int(d["delay"]), int(self.now.replace(hour=7).timestamp()))
        self.assertEqual(d["tags"], ["calendar"])
        self.assertIn("11 am · Lab", d["message"])
        self.assertEqual(self.digests(self.sync([a], now=six + timedelta(minutes=5))[1]), [])   # unchanged: nothing sent
        _, n = self.sync([a, ev("Quiz", self.now + timedelta(hours=4))], now=six + timedelta(minutes=10))
        self.assertIn(("DELETE", d["sequence_id"]), [(m, u.rsplit("/", 1)[1]) for m, u, _ in n.calls])
        (d2,) = self.digests(n)
        self.assertIn("Quiz", d2["message"])
        # 07:30: ntfy delivered the queued one, so nothing is sent now; tomorrow's is queued
        _, n = self.sync([a], now=self.now.replace(hour=7, minute=30))
        (d3,) = self.digests(n)
        self.assertTrue(d3["sequence_id"].startswith("digest-20261112-"))
        self.assertEqual(json.loads(Path(self.cfg["push_state_path"]).read_text())["digest"], "2026-11-11")

    def test_an_empty_day_still_gets_its_schedule(self):
        _, n = self.sync([], now=self.now.replace(hour=6))
        (d,) = self.digests(n)
        self.assertEqual(d["title"], "Today: nothing on the calendar")
        self.assertEqual(d["message"], "No events, reminders or deadlines today.")

    def test_reminders_due_that_day_are_listed(self):
        r = ev("Pay rent", self.now.replace(hour=17), kind="reminder")
        title, body = push.digest([r, ev("Physics", self.now + timedelta(hours=2))], self.now)
        self.assertEqual(title, "Today: 1 event, 1 heads-up")
        self.assertIn("11 am · Physics", body)
        self.assertIn("⚠ Pay rent is due today at 5 pm.", body)

    def test_off_takes_back_the_queued_schedule(self):
        tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        _, n = self.sync([], now=tomorrow)   # cancel_all compares with the real clock
        seq = self.digests(n)[0]["sequence_id"]
        ntfy = FakeNtfy()
        push.cancel_all(self.cfg, opener=ntfy)
        self.assertEqual([(m, u.rsplit("/", 1)[1]) for m, u, _ in ntfy.calls], [("DELETE", seq)])

    def test_switch_off(self):
        self.cfg["push"]["digest_enabled"] = False
        self.assertEqual(self.digests(self.sync([], now=self.now.replace(hour=6))[1]), [])


if __name__ == "__main__":
    unittest.main()
