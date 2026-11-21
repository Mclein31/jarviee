"""Summaries to WhatsApp through CallMeBot (hub/wamsg.py); quick alerts stay on ntfy."""
import subprocess
import unittest
import urllib.parse
from datetime import datetime, timedelta

from helpers import TempDir   # noqa: F401  (puts the repo on sys.path)
from hub import briefing, daybook, push, wamsg

KEYCHAIN = 'keychain: "x"\n    "acct"<blob>="+639170000000"\n'


def keychain(argv, **k):
    if "-w" in argv:
        return subprocess.CompletedProcess(argv, 0, "123456\n", "")
    return subprocess.CompletedProcess(argv, 0, KEYCHAIN, "")


class Reply:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.text.encode()


class Send(unittest.TestCase):
    def setUp(self):
        self.urls = []

    def opener(self, text="<p>Message queued. You will receive it in a few seconds.</p>"):
        def op(req, timeout=None):
            self.urls.append(req.full_url)
            return Reply(text)
        return op

    def test_sends_title_and_body(self):
        ok, err = wamsg.send({}, "Today: 2 events", "9 am · Lab", opener=self.opener(), acct=("+639170000000", "123456"))
        self.assertTrue(ok, err)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.urls[0]).query)
        self.assertEqual(q["phone"], ["+639170000000"])
        self.assertEqual(q["apikey"], ["123456"])
        self.assertEqual(q["text"], ["*Today: 2 events*\n9 am · Lab"])
        self.assertTrue(self.urls[0].startswith("https://api.callmebot.com/"))

    def test_bad_key_is_an_error(self):
        ok, err = wamsg.send({}, "t", "b", opener=self.opener("<b>APIKey is invalid.</b>"), acct=("+63917", "x"))
        self.assertFalse(ok)
        self.assertIn("invalid", err)

    def test_not_connected(self):
        ok, err = wamsg.send({}, "t", "b", acct=("", None))
        self.assertFalse(ok)
        self.assertIn("connect-whatsapp", err)

    def test_long_text_is_cut(self):
        self.assertEqual(len(wamsg.text_of("T", "x" * 5000, 100)), 100)

    def test_account_and_handles(self):
        self.assertEqual(wamsg.account(keychain), ("+639170000000", "123456"))
        self.assertFalse(wamsg.handles({}, "digest", keychain))   # off until connected
        on = {"whatsapp_send": {"enabled": True}}
        self.assertTrue(wamsg.handles(on, "digest", keychain))
        self.assertTrue(wamsg.handles(on, "briefing", keychain))
        for quick in ("event", "reminder", "agent_needs", "agent_failed", "limit_spent", "gmail"):
            self.assertFalse(wamsg.handles(on, quick, keychain), quick)
        none = lambda argv, **k: subprocess.CompletedProcess(argv, 44, "", "")   # noqa: E731
        self.assertFalse(wamsg.handles(on, "digest", none))

    def test_connect_checks_the_number(self):
        ok, msg = wamsg.connect("0917 000 0000", runner=keychain, check=False)
        self.assertFalse(ok)
        self.assertIn("+63", msg)


class Routing(unittest.TestCase):
    def test_publish_routes_summary_kinds_only(self):
        sent, posts = [], []
        orig_handles, orig_send = wamsg.handles, wamsg.send
        wamsg.handles = lambda cfg, kind, runner=None: kind in ("digest", "briefing", "summary")
        wamsg.send = lambda cfg, title, body, **k: (sent.append(title) or (True, ""))
        try:
            with TempDir() as tmp:
                cfg = {"push": {"topic": "t"}, "push_state_path": f"{tmp}/p.json"}
                op = lambda req, timeout=None: (posts.append(req.full_url) or Reply("ok"))   # noqa: E731
                push.publish(cfg, "Today: 1 event", "x", kind="digest", opener=op)
                push.publish(cfg, "Lab starts in 15 min", "x", tags=push.icon("event"), kind="event", opener=op)
                push.publish(cfg, "Tomorrow", "x", kind="digest", at=datetime.now() + timedelta(hours=1), opener=op)
        finally:
            wamsg.handles, wamsg.send = orig_handles, orig_send
        self.assertEqual(sent, ["Today: 1 event"])
        self.assertEqual(len(posts), 2)   # the event alert and the scheduled one stay on ntfy

    def test_briefing_is_one_whatsapp_message(self):
        got = []
        sections = {"gmail": {"ok": True, "unread": 2, "messages": [{"from": "Alice", "subject": "secret", "important": False}]},
                    "github": {"ok": True, "unread": 1, "kinds": {"mention": 1}, "items": [{"kind": "mention", "repo": "site", "title": "x"}]}}
        n = briefing.send_to_phone({}, {"sections": sections}, wa_send=lambda cfg, t, b: (got.append((t, b)) or (True, "")))
        self.assertEqual(n, 1)
        title, body = got[0]
        self.assertEqual(title, "What's ahead")
        self.assertIn("📧 *Gmail: 2 unread emails*", body)
        self.assertIn("🐙 *GitHub: 1 notification*", body)
        self.assertNotIn("secret", body)

    def test_endwork_report_goes_to_whatsapp(self):
        got = []
        book = daybook.Daybook.__new__(daybook.Daybook)
        book.cfg = {}
        self.assertTrue(book.send_summary({"jobs": 2, "date": "2026-11-14", "text": "That's the day."},
                                          send=lambda cfg, t, b: got.append((t, b))))
        self.assertEqual(got, [("Your day · 2026-11-14", "That's the day.")])
        self.assertFalse(book.send_summary({"jobs": 0, "date": "x", "text": ""}, send=lambda *a: got.append(a)))

    def test_schedule_on_whatsapp_is_sent_at_eight_not_queued(self):
        sent = []
        orig_handles, orig_send = wamsg.handles, wamsg.send
        wamsg.handles = lambda cfg, kind, runner=None: kind == "digest"
        wamsg.send = lambda cfg, title, body, **k: (sent.append(title) or (True, ""))
        posts = []
        op = lambda req, timeout=None: (posts.append(req.full_url) or Reply("ok"))   # noqa: E731
        try:
            with TempDir() as tmp:
                cfg = {"calendar": {"enabled": True}, "push": {"enabled": True, "topic": "t", "digest_at": "08:00"}, "push_state_path": f"{tmp}/p.json"}
                push.sync(cfg, now=datetime(2026, 11, 15, 7, 0), fetch=lambda *a: [], opener=op)
                self.assertEqual((sent, posts), ([], []))      # nothing queued on ntfy ahead of time
                push.sync(cfg, now=datetime(2026, 11, 15, 8, 0, 3), fetch=lambda *a: [], opener=op)
                push.sync(cfg, now=datetime(2026, 11, 15, 8, 5), fetch=lambda *a: [], opener=op)
        finally:
            wamsg.handles, wamsg.send = orig_handles, orig_send
        self.assertEqual(sent, ["Today: nothing on the calendar"])   # once, at 8
        self.assertEqual(posts, [])

    def test_event_alerts_open_the_calendar(self):
        self.assertEqual(push.CLICKS["event"], "calshow://")

    def test_watcher_wakes_at_the_schedule_time(self):
        w = push.PushWatcher({"push": {"digest_at": "08:00", "check_s": 300}})
        self.assertEqual(w._next_wait(datetime(2026, 11, 15, 7, 58, 0)), 122)
        self.assertEqual(w._next_wait(datetime(2026, 11, 15, 9, 0, 0)), 300)


if __name__ == "__main__":
    unittest.main()
