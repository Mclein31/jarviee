"""/startwork: the what's-ahead briefing (hub/briefing.py) and the per-kind phone icons (hub/push.py)."""
import json
import sqlite3
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import TempDir, make_cfg
from hub import briefing, push, voice

NOW = datetime(2026, 11, 16, 8, 30)


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(title, start, mins=60, all_day=False, kind="event"):
    return {"title": title, "start": iso(start), "end": iso(start + timedelta(minutes=mins)), "all_day": all_day,
            "calendar": "School", "location": "", "kind": kind}


SECTIONS = {
    "calendar": {"ok": True, "today": [{"title": "Data Science", "all_day": False, "time": "9 am", "ongoing": False, "where": "", "start": ""},
                                       {"title": "Lab", "all_day": False, "time": "1 pm", "ongoing": False, "where": "B2", "start": ""}],
                 "tomorrow": [{"title": "Standup", "all_day": False, "time": "8 am", "ongoing": False, "where": "", "start": ""}],
                 "heads_up": ["Quiz 3 is due tomorrow."]},
    "gmail": {"ok": True, "unread": 3, "messages": [
        {"from": "Alice", "addr": "a@x.com", "subject": "Invoice overdue", "important": True,
         "gist": "The October invoice of 4,200 pesos is overdue.", "action": "Pay it by Friday"},
        {"from": "Bank", "addr": "b@x.com", "subject": "Your code is 123456", "important": False,
         "gist": "A sign-in code was sent.", "action": ""}]},
    "linkedin": {"ok": True, "unread": 3, "kinds": {"message": 2, "invitation": 1}, "messages": []},
    "github": {"ok": True, "unread": 2, "kinds": {"review request": 1, "CI run": 1},
               "items": [{"kind": "review request", "repo": "agent-hub", "title": "Fix x", "type": "PullRequest"},
                         {"kind": "CI run", "repo": "site", "title": "build failed", "type": "CheckSuite"}]},
    "whatsapp": {"ok": True, "unread": 4, "chats": [{"name": "Mom", "unread": 3, "group": False, "when": 0},
                                                    {"name": "Team", "unread": 1, "group": True, "when": 0}]},
    "agents": {"ok": True, "waiting": [{"id": "r1", "title": "Pick a colour", "project": "site"}]},
}


class Command(unittest.TestCase):
    def test_startwork_matches(self):
        for t in ("/startwork", "  /start work", "/start-work please", "/STARTWORK", "/startwokr"):
            self.assertTrue(briefing.is_startwork(t), t)
        for t in ("startwork", "/endwork", "/start the server", "fix /startwork"):
            self.assertFalse(briefing.is_startwork(t), t)


class Speech(unittest.TestCase):
    def test_speaks_every_source_by_name_and_count(self):
        t = briefing.speech(SECTIONS, NOW)
        self.assertTrue(t.startswith("Good morning. Here's what's ahead."))
        for part in ("2 more events today", "Next up is Data Science at 9 am", "then Lab at 1 pm", "Tomorrow starts with Standup",
                     "Quiz 3 is due tomorrow", "3 unread emails", "Alice: The October invoice of 4,200 pesos is overdue. To do: Pay it by Friday.",
                     "Bank: A sign-in code was sent.", "And 1 more email.", "2 messages and 1 invitation",
                     "1 review request", "on agent-hub", "2 chats with unread messages, from Mom and Team", "1 agent is waiting"):
            self.assertIn(part, t)

    def test_never_speaks_unimportant_subjects(self):
        self.assertNotIn("123456", briefing.speech(SECTIONS, NOW))

    def test_skips_sources_that_are_not_connected(self):
        off = {"gmail": {"ok": False, "connected": False, "error": "Not connected"},
               "calendar": {"ok": True, "today": [], "tomorrow": [], "heads_up": []}}
        t = briefing.speech(off, datetime(2026, 11, 16, 19, 0))
        self.assertIn("Good evening", t)
        self.assertIn("Nothing else on your calendar today", t)
        self.assertNotIn("Gmail", t)


class Phone(unittest.TestCase):
    def test_one_message_per_source_with_its_own_icon(self):
        kinds = [k for k, _, _ in briefing.phone_messages(SECTIONS)]
        self.assertEqual(kinds, ["briefing", "gmail", "linkedin", "github", "whatsapp", "agent_needs"])
        for k in kinds:
            self.assertTrue(push.icon(k), k)

    def test_heads_up_only_day(self):
        cal = {"calendar": {"ok": True, "today": [], "tomorrow": [], "heads_up": ["Rent is due today."]}}
        (_, title, body), = briefing.phone_messages(cal)
        self.assertEqual(title, "Today: nothing left on the calendar")
        self.assertIn("Rent", body)

    def test_no_subjects_or_message_text_leave_the_mac(self):
        blob = json.dumps(briefing.phone_messages(SECTIONS))
        self.assertNotIn("Invoice", blob)
        self.assertNotIn("123456", blob)
        self.assertIn("Alice", blob)

    def test_send_uses_kind_only_when_push_is_on(self):
        sent = []
        pub = lambda cfg, title, body, **k: (sent.append((title, k)) or (True, ""))   # noqa: E731
        brief = {"sections": SECTIONS}
        self.assertEqual(briefing.send_to_phone({"push": {"enabled": False}}, brief, pub), 0)
        self.assertEqual(briefing.send_to_phone({"push": {"enabled": True}}, brief, pub), 6)
        self.assertEqual(sent[1][1]["kind"], "gmail")
        self.assertEqual(briefing.send_to_phone({"push": {"enabled": True}, "briefing": {"phone": False}}, brief, pub), 0)


class Icons(unittest.TestCase):
    def test_icons(self):
        self.assertEqual(push.icon("agent_done"), ["robot"])
        self.assertEqual(push.icon("gmail"), ["email"])
        self.assertEqual(push.icon("nope"), [])
        self.assertEqual(push.icon("gmail", {"push": {"icons": {"gmail": "envelope"}}}), ["envelope"])

    def test_publish_sets_tags_and_click_from_kind(self):
        calls = []

        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(req, timeout=None):
            calls.append(json.loads(req.data))
            return R()
        with TempDir() as tmp:
            cfg = {"push": {"topic": "t"}, "push_state_path": str(Path(tmp) / "p.json")}
            push.publish(cfg, "Gmail: 2", "From A", kind="gmail", opener=opener)
            push.publish(cfg, "x", "y", tags=["bell"], kind="gmail", opener=opener)
            push.publish(cfg, "x", "y", kind="agent_done", opener=opener)
        self.assertEqual(calls[0]["tags"], ["email"])
        self.assertEqual(calls[0]["click"], push.CLICKS["gmail"])
        self.assertEqual(calls[1]["tags"], ["bell"])   # explicit tags win
        self.assertEqual(calls[2]["tags"], ["robot"])
        self.assertNotIn("click", calls[2])

    def test_reminder_and_event_icons(self):
        it = {"kind": "reminder", "title": "Pay", "start": NOW, "end": NOW, "where": "", "list": ""}
        self.assertEqual(push.message(it, 15)[2], ["alarm_clock"])
        self.assertEqual(push.message({**it, "kind": "event"}, 15)[2], ["date"])


class Calendar(unittest.TestCase):
    def test_today_left_tomorrow_and_heads_up(self):
        events = [ev("Done already", NOW - timedelta(hours=3)), ev("Data Science", NOW + timedelta(minutes=30)),
                  ev("Lab", NOW.replace(hour=13)), ev("Standup", NOW.replace(hour=8) + timedelta(days=1)),
                  ev("Quiz 3", NOW + timedelta(days=1), 0, kind="reminder")]
        c = briefing.read_calendar({"calendar": {"enabled": True}}, NOW, fetch=lambda *a: events)
        self.assertEqual([e["title"] for e in c["today"]], ["Data Science", "Lab"])
        self.assertEqual(c["tomorrow"][0]["title"], "Standup")
        self.assertTrue(any("Quiz 3" in h for h in c["heads_up"]))

    def test_error_is_reported(self):
        def boom(*a):
            raise RuntimeError("Calendar access is off")
        c = briefing.read_calendar({"calendar": {"enabled": True}}, NOW, fetch=boom)
        self.assertFalse(c["ok"])
        self.assertIn("access is off", c["error"])


class FakeIMAP:
    def __init__(self, boxes):
        self.boxes, self.calls = boxes, []

    def login(self, a, p):
        self.calls.append(("login", a, p))

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, args))
        if cmd == "SEARCH":
            key = "linkedin" if "linkedin" in args[-1] else "gmail"
            return "OK", [b" ".join(str(i).encode() for i in self.boxes[key])]
        out = []
        for u in args[0].split(","):
            frm, subj, imp = self.msgs[int(u)]
            labels = ("\\Important " if imp else "") + '"\\Inbox"'
            meta = f"{u} (X-GM-LABELS ({labels}) UID {u} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{99}}"
            out += [(meta.encode(), f"From: {frm}\r\nSubject: {subj}\r\n\r\n".encode()), b")"]
        return "OK", out

    def logout(self):
        self.calls.append(("logout",))


class Mail(unittest.TestCase):
    def setUp(self):
        self.imap = FakeIMAP({"gmail": [1, 2, 3], "linkedin": [3, 4, 5]})
        self.imap.msgs = {1: ("Alice <a@x.com>", "Invoice", True), 2: ("=?utf-8?q?Jos=C3=A9?= <j@x.com>", "Hi", False),
                          3: ("LinkedIn <messages-noreply@linkedin.com>", "Ana sent you a message", False),
                          4: ("LinkedIn <invitations@linkedin.com>", "Ben wants to connect", False),
                          5: ("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "10 new jobs for Data analyst", False)}

    def test_read_only_never_marks_read(self):
        g, li = briefing.read_mail({"briefing": {"gmail": {"summaries": False}}}, ("me@gmail.com", "pw"), imap_factory=lambda host: self.imap)
        self.assertIn(("select", "INBOX", True), self.imap.calls)
        fetches = [c for c in self.imap.calls if c[0] == "FETCH"]
        self.assertTrue(fetches and all("BODY.PEEK" in c[1][1] for c in fetches))
        searches = [c[1][-1] for c in self.imap.calls if c[0] == "SEARCH"]
        self.assertTrue(any("category:primary" in s for s in searches))
        self.assertTrue(any("from:linkedin.com" in s for s in searches))
        self.assertEqual(self.imap.calls[-1], ("logout",))
        self.assertEqual(g["unread"], 3)
        self.assertEqual([m["from"] for m in g["messages"]], ["José", "Alice"])   # LinkedIn mail left out, newest first
        self.assertTrue(g["messages"][1]["important"])
        self.assertEqual(li["kinds"], {"message": 1, "invitation": 1, "job": 1})

    def test_not_connected_without_password(self):
        g, li = briefing.read_mail({}, ("", None))
        self.assertFalse(g["connected"])
        self.assertIn("connect-gmail", g["error"])
        self.assertFalse(li["connected"])

    def test_bad_password(self):
        import imaplib

        class Bad(FakeIMAP):
            def login(self, a, p):
                raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        g, _ = briefing.read_mail({}, ("me@gmail.com", "pw"), imap_factory=lambda h: Bad({}))
        self.assertFalse(g["ok"])
        self.assertIn("app password", g["error"])

    def test_keychain_account(self):
        def runner(argv, **k):
            if "-w" in argv:
                return subprocess.CompletedProcess(argv, 0, "abcd efgh ijkl mnop\n", "")
            return subprocess.CompletedProcess(argv, 0, 'keychain: "x"\n    "acct"<blob>="me@gmail.com"\n', "")
        self.assertEqual(briefing.gmail_account({}, runner), ("me@gmail.com", "abcdefghijklmnop"))
        none = lambda argv, **k: subprocess.CompletedProcess(argv, 44, "", "not found")   # noqa: E731
        self.assertEqual(briefing.gmail_account({}, none), ("", None))


class GitHub(unittest.TestCase):
    def test_groups_and_ranks(self):
        rows = [{"reason": "ci_activity", "repository": {"name": "site"}, "subject": {"title": "failed", "type": "CheckSuite"}},
                {"reason": "review_requested", "repository": {"name": "agent-hub"}, "subject": {"title": "Fix", "type": "PullRequest"}}]
        run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")   # noqa: E731
        g = briefing.read_github({}, run)
        self.assertEqual(g["unread"], 2)
        self.assertEqual(g["items"][0]["kind"], "review request")
        self.assertEqual(g["kinds"], {"CI run": 1, "review request": 1})

    def test_not_logged_in(self):
        run = lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "To get started with GitHub CLI, please run:  gh auth login")   # noqa: E731
        g = briefing.read_github({}, run)
        self.assertFalse(g["connected"])


class WhatsApp(unittest.TestCase):
    def test_reads_unread_chats_from_a_copy(self):
        with TempDir() as tmp:
            db = Path(tmp) / "ChatStorage.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE ZWACHATSESSION (ZPARTNERNAME, ZUNREADCOUNT, ZLASTMESSAGEDATE, ZCONTACTJID, ZARCHIVED, ZLASTMESSAGETEXT)")
            recent = NOW.timestamp() - briefing.APPLE_EPOCH - 3600
            con.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?)", [
                ("Mom", 3, recent, "63900@s.whatsapp.net", 0, "secret text"),
                ("Team", 1, recent + 60, "123@g.us", 0, ""),
                ("Old", 9, recent - 30 * 86400, "1@s.whatsapp.net", 0, ""),
                ("Archived", 2, recent, "2@s.whatsapp.net", 1, ""),
                ("Status", 5, recent, "status@broadcast", 0, ""),
                ("Read", 0, recent, "3@s.whatsapp.net", 0, "")])
            con.commit()
            con.close()
            w = briefing.read_whatsapp({"briefing": {"whatsapp": {"db": str(db)}}}, NOW)
        self.assertTrue(w["ok"])
        self.assertEqual([c["name"] for c in w["chats"]], ["Mom", "Team"])
        self.assertEqual(w["unread"], 4)
        self.assertNotIn("secret", json.dumps(w))

    def test_not_installed(self):
        w = briefing.read_whatsapp({"briefing": {"whatsapp": {"db": "/nope/ChatStorage.sqlite"}}})
        self.assertFalse(w["connected"])
        self.assertIn("WhatsApp Desktop", w["error"])


class Gather(unittest.TestCase):
    def test_a_broken_or_slow_source_never_sinks_the_rest(self):
        def boom():
            raise RuntimeError("bad")
        readers = {"calendar": lambda: SECTIONS["calendar"], "gmail": boom, "linkedin": lambda: time.sleep(3),
                   "github": lambda: SECTIONS["github"], "whatsapp": lambda: SECTIONS["whatsapp"], "agents": lambda: SECTIONS["agents"]}
        t0 = time.time()
        out = briefing.gather({"briefing": {"timeout_s": 0.5, "gmail": {"summaries": False}}}, readers=readers)
        self.assertLess(time.time() - t0, 2)
        self.assertFalse(out["gmail"]["ok"])
        self.assertIn("no answer", out["linkedin"]["error"])
        self.assertTrue(out["github"]["ok"])

    def test_sources_can_be_turned_off(self):
        out = briefing.gather({"briefing": {"sources": ["agents"]}})
        self.assertEqual(list(out), ["agents"])


class Startwork(unittest.TestCase):
    def test_speaks_shows_and_pushes(self):
        said, sent = [], []

        class Sp:
            def speak(self, text, **kw):
                said.append((text, kw))
                return True
        readers = {k: (lambda v=v: v) for k, v in SECTIONS.items()}
        pub = lambda cfg, t, b, **k: (sent.append(k["kind"]) or (True, ""))   # noqa: E731
        b = briefing.startwork({"push": {"enabled": True}}, speaker=Sp(), readers=readers, publish=pub, now=NOW)
        self.assertTrue(b["spoken"])
        self.assertEqual(said[0][1]["event"], "startwork")
        self.assertEqual(b["phone"], 6)
        self.assertEqual(b["order"], list(briefing.SOURCES))
        self.assertIn("Data Science", "\n".join(b["lines"]["calendar"]))

    def test_voice_speaks_startwork_in_quiet_hours(self):
        with TempDir() as tmp:
            cfg, _ = make_cfg(tmp)
            cfg["voice"] = {"enabled": True}
            cfg["alerts"] = {"quiet_hours": ["00:00", "23:59"]}
            sp = voice.Speaker(cfg, runner=lambda *a, **k: None, start_worker=False)
            self.assertTrue(sp.speak("Good morning.", event="startwork"))
            self.assertFalse(sp.speak("Run complete.", event="done"))



class MailGists(unittest.TestCase):
    BODY = ("Hi Alex,\n\nThe October invoice (4,200 PHP) is overdue. Please pay it by Friday at "
            "https://pay.example.com/x?token=abc.\n\nOn Mon, 9 Nov 2026 at 10:00 Bob <b@x.com> wrote:\n> old thread text\n")

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg = {"mailsum_path": str(Path(self.tmp) / "mailsum.json")}
        self.prompts = []

    def tearDown(self):
        self._t.__exit__()

    def msgs(self):
        return [{"from": "Alice", "addr": "a@x.com", "subject": "Invoice", "msgid": "<1@x>", "body": self.BODY},
                {"from": "News", "addr": "n@x.com", "subject": "Weekly", "msgid": "<2@x>", "body": "Top stories this week."}]

    def good(self, prompt):
        self.prompts.append(prompt)
        return 'Sure: [{"i": 0, "gist": "October invoice of 4,200 PHP is overdue.", "action": "Pay by Friday"}, {"i": 1, "gist": "Weekly news digest.", "action": ""}]'

    def test_clean_drops_links_and_quoted_thread(self):
        from hub import mailsum
        t = mailsum.clean(self.BODY)
        self.assertIn("[link]", t)
        self.assertNotIn("token=abc", t)
        self.assertNotIn("old thread", t)
        self.assertNotIn("wrote:", t)

    def test_html_only_mail(self):
        import email
        from hub import mailsum
        raw = (b"From: a@x.com\r\nSubject: Hi\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               b"<html><style>p{}</style><p>Meeting moved to <b>3 pm</b>&nbsp;today.</p></html>")
        self.assertIn("Meeting moved to 3 pm", " ".join(mailsum.body_text(email.message_from_bytes(raw)).split()))

    def test_falls_back_to_the_next_model_then_caches(self):
        from hub import mailsum
        calls = []
        runners = (("haiku", lambda p: calls.append("haiku") or "You've hit your limit"), ("gemini-flash", lambda p: calls.append("gemini") or self.good(p)))
        ms = mailsum.summarise(self.cfg, self.msgs(), runners=runners)
        self.assertEqual(calls, ["haiku", "gemini"])
        self.assertEqual(ms[0]["gist"], "October invoice of 4,200 PHP is overdue.")
        self.assertEqual(ms[0]["action"], "Pay by Friday")
        self.assertEqual(ms[0]["gist_by"], "gemini-flash")
        self.assertIn("UNTRUSTED", self.prompts[0])
        self.assertNotIn("token=abc", self.prompts[0])
        self.assertEqual(oct(Path(self.cfg["mailsum_path"]).stat().st_mode & 0o777), "0o600")
        again = mailsum.summarise(self.cfg, self.msgs(), runners=(("haiku", lambda p: calls.append("again") or ""),))
        self.assertEqual(again[1]["gist"], "Weekly news digest.")
        self.assertNotIn("again", calls)   # both were cached

    def test_no_model_or_out_of_time_uses_first_sentence(self):
        from hub import mailsum
        ms = mailsum.summarise(self.cfg, self.msgs(), runners=(("haiku", lambda p: ""),))
        self.assertTrue(ms[0]["gist"].startswith("Hi Alex, The October invoice"))
        self.assertEqual(ms[0]["gist_by"], "")
        late = mailsum.summarise(self.cfg, self.msgs(), deadline=time.time() - 1, runners=(("haiku", self.good),))
        self.assertEqual(self.prompts, [])
        self.assertEqual(late[1]["gist"], "Top stories this week.")

    def test_parse_answer_ignores_junk(self):
        from hub import mailsum
        self.assertEqual(mailsum.parse_answer("no json here", 2), {})
        self.assertEqual(mailsum.parse_answer('[{"i": 5, "gist": "x"}, {"i": "a"}, {"i": 0, "gist": ""}, {"i": 1, "gist": "ok"}]', 2),
                         {1: {"gist": "ok", "action": ""}})

    def test_read_mail_attaches_gists_and_drops_the_text(self):
        imap = FakeIMAP({"gmail": [1], "linkedin": []})
        imap.msgs = {1: ("Alice <a@x.com>", "Invoice", True)}
        orig = imap.uid

        def uid(cmd, *args):   # serve a whole message for the body fetch
            if cmd == "FETCH" and "BODY.PEEK[]" in args[1]:
                raw = f"From: Alice <a@x.com>\r\nSubject: Invoice\r\nMessage-ID: <1@x>\r\n\r\n{self.BODY}".encode()
                return "OK", [(b'1 (X-GM-LABELS ("\\Important") UID 1 BODY[]<0> {300}', raw), b")"]
            return orig(cmd, *args)
        imap.uid = uid
        g, _ = briefing.read_mail(self.cfg, ("me@gmail.com", "pw"), imap_factory=lambda h: imap, runners=(("haiku", self.good),))
        m = g["messages"][0]
        self.assertEqual(m["gist"], "October invoice of 4,200 PHP is overdue.")
        self.assertNotIn("body", m)
        self.assertNotIn("4,200", json.dumps(briefing.phone_messages({"gmail": g})))
        self.assertIn("    → Pay by Friday", briefing.lines({"gmail": g})["gmail"])


if __name__ == "__main__":
    unittest.main()
