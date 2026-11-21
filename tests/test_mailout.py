"""Summaries by email from the owner's Gmail to itself (hub/mailout.py, hub/channels.py)."""
import smtplib
import unittest

from helpers import TempDir   # noqa: F401  (puts the repo on sys.path)
from hub import briefing, channels, mailout, wamsg

ACCT = ("me@gmail.com", "apppassword")


class FakeSMTP:
    def __init__(self, fail=None):
        self.fail, self.sent, self.login_as = fail, [], None

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        if self.fail:
            raise self.fail
        self.login_as = (user, pw)

    def send_message(self, msg):
        self.sent.append(msg)


class Mailout(unittest.TestCase):
    def test_sends_to_yourself_marked_as_jarviee(self):
        smtp = FakeSMTP()
        ok, err = mailout.send({}, "Jarviee · Today's work", "• one\n• two", account=ACCT, smtp_factory=smtp)
        self.assertTrue(ok, err)
        self.assertEqual(smtp.login_as, ACCT)
        (m,) = smtp.sent
        self.assertEqual(m["Subject"], "Jarviee · Today's work")   # not "Jarviee · Jarviee · …"
        self.assertEqual(m["To"], "me@gmail.com")
        self.assertIn("Jarviee", m["From"])
        self.assertEqual(m["X-Jarviee"], "summary")
        self.assertIn("• two", m.get_content())

    def test_other_recipient_and_errors(self):
        smtp = FakeSMTP()
        mailout.send({"email_send": {"to": "work@x.com"}}, "Today: 1 event", "x", account=ACCT, smtp_factory=smtp)
        self.assertEqual(smtp.sent[0]["To"], "work@x.com")
        self.assertEqual(smtp.sent[0]["Subject"], "Jarviee · Today: 1 event")
        ok, err = mailout.send({}, "t", "b", account=ACCT, smtp_factory=FakeSMTP(smtplib.SMTPAuthenticationError(535, b"no")))
        self.assertFalse(ok)
        self.assertIn("app password", err)
        self.assertFalse(mailout.send({}, "t", "b", account=("", None))[0])

    def test_handles_only_summaries_when_on(self):
        on = {"email_send": {"enabled": True}}
        self.assertFalse(mailout.handles({}, "digest", ACCT))
        for k in ("digest", "briefing", "summary"):
            self.assertTrue(mailout.handles(on, k, ACCT), k)
        for k in ("event", "agent_needs", "gmail"):
            self.assertFalse(mailout.handles(on, k, ACCT), k)
        self.assertFalse(mailout.handles(on, "digest", ("", None)))


class Channels(unittest.TestCase):
    def test_whatsapp_wins_then_email_then_ntfy(self):
        orig = (wamsg.handles, mailout.handles)
        try:
            wamsg.handles = lambda cfg, kind: kind == "digest"
            mailout.handles = lambda cfg, kind: kind in ("digest", "briefing")
            self.assertIs(channels.summary_sender({}, "digest"), wamsg.send)
            self.assertIs(channels.summary_sender({}, "briefing"), mailout.send)
            self.assertIsNone(channels.summary_sender({}, "event"))
        finally:
            wamsg.handles, mailout.handles = orig


class BriefingEmail(unittest.TestCase):
    def test_the_whole_briefing_goes_to_your_inbox(self):
        brief = {"text": "Good morning. Here's what's ahead.", "order": ["gmail"],
                 "sections": {"gmail": {"ok": True, "unread": 1, "messages": []}},
                 "lines": {"gmail": ["Alice · Invoice", "    The October invoice is overdue.", "    → Pay by Friday"]}}
        t = briefing.email_text(brief)
        self.assertTrue(t.startswith("Good morning."))
        self.assertIn("📧 Gmail", t)
        self.assertIn("→ Pay by Friday", t)
        got = []
        orig_send, orig_pick = mailout.send, channels.summary_sender
        mailout.send = lambda cfg, title, body, **k: (got.append((title, body)) or (True, ""))
        channels.summary_sender = lambda cfg, kind: mailout.send
        try:
            self.assertEqual(briefing.send_to_phone({}, brief), 1)
        finally:
            mailout.send, channels.summary_sender = orig_send, orig_pick
        self.assertEqual(got[0][0], "What's ahead")
        self.assertIn("The October invoice is overdue.", got[0][1])

    def test_html_alternative_is_attached_when_provided(self):
        smtp = FakeSMTP()
        ok, err = mailout.send({}, "Your day", "plain body", html="<html><body><b>HTML</b></body></html>",
                               account=ACCT, smtp_factory=smtp)
        self.assertTrue(ok, err)
        (m,) = smtp.sent
        self.assertTrue(m.is_multipart())
        self.assertIn("plain body", m.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("<b>HTML</b>", m.get_body(preferencelist=("html",)).get_content())

    def test_inbox_search_skips_our_own_reports(self):
        import inspect
        self.assertIn("-subject:jarviee", inspect.getsource(briefing.fetch_mail))


if __name__ == "__main__":
    unittest.main()
