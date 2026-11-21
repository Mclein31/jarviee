"""Second security pass before the public release: the HTTP endpoints and the hook installer.

Covers what a web page in the owner's browser can reach (hook ingestion, actions on GET), what crafted request bodies
and path-like values do (attachments, project names, cwd, NaN / Infinity, deep nesting), and how the installer treats
the owner's own settings files (malformed hooks, symlinks, quoting)."""
import contextlib
import io
import json
import os
import shlex
import socket
import stat
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from helpers import TempDir, make_cfg
from hub import auth, install, push, routes
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def mode_of(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class Live(unittest.TestCase):
    """A real server on a free port with a temp config (never the owner's running Jarviee)."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.cfg["port"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = auth.get_token(self.cfg)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def req(self, path, body=None, headers=None, method=None, raw=None, token=False):
        """(status, parsed JSON or raw bytes). JSON POSTs carry the token only when asked."""
        h = dict(headers or {})
        if token:
            h["X-Jarviee-Token"] = self.token
        data = raw
        if body is not None:
            h.setdefault("Content-Type", "application/json")
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with _OPEN(r, timeout=8) as resp:
                code, out = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                code, out = e.code, e.read()
        try:
            return code, json.loads(out)
        except ValueError:
            return code, out

    def raw_post(self, path, ctype, length, payload=b"", close_write=False):
        """Send a hand-made request (bad Content-Length values urllib refuses to send). (status line, seconds)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        t0 = time.time()
        try:
            s.sendall((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: {ctype}\r\n"
                       f"X-Jarviee-Token: {self.token}\r\nContent-Length: {length}\r\n\r\n").encode() + payload)
            if close_write:
                s.shutdown(socket.SHUT_WR)
            try:
                head = s.recv(200).split(b"\r\n", 1)[0]
            except socket.timeout:
                head = b"TIMEOUT"
        finally:
            s.close()
        return head, time.time() - t0


# ---------------------------------------------------------------- hook ingestion from a web page

class HookIngestion(Live):
    """POST /hook writes the agents table and the dashboard: a page in the owner's browser must not forge events."""

    EVENT = {"hook_event_name": "UserPromptSubmit", "session_id": "forged-session-1234", "prompt": "rm -rf ~",
             "cwd": "/tmp"}

    def assertNoRow(self):
        self.assertEqual(self.store.all(), [])

    def test_forged_hooks_are_refused(self):
        cases = [
            ({"Content-Type": "application/json"}, False),                                    # no token
            ({"Content-Type": "text/plain"}, True),                                           # a "simple" CORS request
            ({"Content-Type": "application/x-www-form-urlencoded"}, True),                    # an HTML form
            ({"Content-Type": "application/json", "Origin": "https://evil.example"}, True),
            ({"Content-Type": "application/json", "Origin": "null"}, True),                   # sandboxed iframe / file:
            ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, True),
            ({"Content-Type": "application/json", "Host": f"evil.example:{self.port}"}, True),  # DNS rebinding
            ({"Content-Type": "application/json", "X-Jarviee-Token": "0" * 48}, False),        # a guessed token
        ]
        for headers, tok in cases:
            code, _ = self.req("/hook?event=UserPromptSubmit", json.dumps(self.EVENT).encode(), headers=headers,
                               token=tok)
            self.assertEqual(code, 403, headers)
        self.assertNoRow()

    def test_a_browser_cannot_preflight_its_way_in(self):
        code, _ = self.req("/hook", method="OPTIONS", headers={"Origin": "https://evil.example",
                                                              "Access-Control-Request-Method": "POST"})
        self.assertNotIn(code, (200, 204))   # no CORS: a JSON POST from another site never leaves the browser

    def test_the_real_reporter_still_gets_in(self):
        code, _ = self.req("/hook?event=UserPromptSubmit", self.EVENT, token=True)
        self.assertEqual(code, 200)
        self.assertIsNotNone(self.store.get("agy-forged-session-1234"))   # no jarvieeTool: the reporter's default


# ---------------------------------------------------------------- no action on GET, every POST needs the token

class NoActionOnGet(Live):
    ACTIONS = ["/hook", "/api/run", "/api/stop", "/api/clear", "/api/rerun", "/api/action/pause",
               "/api/action/open-dashboard", "/api/settings/voice", "/api/settings/push", "/api/push/rotate",
               "/api/board/create", "/api/board/start", "/api/task/cancel", "/api/plan/approve", "/api/queue/pause",
               "/api/orch/assign", "/api/calendar/add", "/api/todo/tick", "/api/attach", "/api/templates/delete",
               "/api/inbox/accept", "/api/inbox/revert", "/api/inbox/check", "/api/flow/generate", "/api/terminal",
               "/api/run-event", "/api/focus"]

    def test_action_paths_do_nothing_on_get(self):
        self.store.upsert("run-abcd1234", source="launcher", status="running", tool="claude", task="t")
        for path in self.ACTIONS:
            code, _ = self.req(path + "?id=run-abcd1234&task=hi&muted=1&action=enable&name=x")
            self.assertEqual(code, 404, path)
        self.assertEqual(self.store.get("run-abcd1234")["status"], "running")

    def test_every_post_route_requires_the_token(self):
        self.assertEqual([p for p, r in routes.POST_ROUTES.items() if not r.token], [])
        self.assertEqual([p for p, r in routes.POST_PREFIX if not r.token], [])
        paths = list(routes.POST_ROUTES) + [p + "x" for p, _ in routes.POST_PREFIX]
        for path in paths:
            ctype = "image/png" if path == "/api/attach" else "application/json"
            code, _ = self.req(path, b"{}", headers={"Content-Type": ctype})
            self.assertEqual(code, 403, path)

    def test_run_spec_needs_the_token(self):
        self.assertEqual(self.req("/api/run-spec?id=run-abcd1234")[0], 403)


# ---------------------------------------------------------------- attachments

class Attachments(Live):
    def att_dir(self):
        return Path(self.cfg["logs_dir"]).expanduser().parent / "attachments"

    def test_negative_length_is_refused_at_once(self):
        head, secs = self.raw_post("/api/attach", "image/png", -1, b"\x89PNG not closed")
        self.assertIn(b" 400 ", head)            # used to read until the client hung up, past the 15 MB cap
        self.assertLess(secs, 3)
        self.assertFalse(self.att_dir().exists() and any(self.att_dir().iterdir()))

    def test_garbage_and_zero_lengths(self):
        for length in ("abc", "0", str(16_000_000)):
            head, _ = self.raw_post("/api/attach", "image/png", length)
            self.assertIn(b" 400 ", head, length)

    def test_a_cut_short_upload_saves_nothing(self):
        head, _ = self.raw_post("/api/attach", "image/png", 100, b"\x89PNG" * 2, close_write=True)
        self.assertIn(b" 400 ", head)
        self.assertFalse(self.att_dir().exists() and any(self.att_dir().iterdir()))

    def test_saved_images_are_private(self):
        code, out = self.req("/api/attach", raw=b"\x89PNG\r\n\x1a\nfake", headers={"Content-Type": "image/png"},
                             token=True, method="POST")
        self.assertEqual(code, 200, out)
        f = Path(out["path"])
        self.assertEqual(f.read_bytes(), b"\x89PNG\r\n\x1a\nfake")
        self.assertEqual(mode_of(f), 0o600)
        self.assertEqual(mode_of(f.parent), 0o700)
        self.assertEqual(f.parent, self.att_dir())

    def test_an_old_world_readable_folder_is_tightened(self):
        d = self.att_dir()
        d.mkdir(parents=True)
        os.chmod(d, 0o755)
        code, _ = self.req("/api/attach", raw=b"GIF89a", headers={"Content-Type": "image/gif"}, token=True,
                           method="POST")
        self.assertEqual(code, 200)
        self.assertEqual(mode_of(d), 0o700)

    def test_only_images(self):
        code, _ = self.req("/api/attach", raw=b"<html>", headers={"Content-Type": "text/html"}, token=True,
                           method="POST")
        self.assertEqual(code, 403)   # not JSON, not an image type: refused before the handler


# ---------------------------------------------------------------- crafted bodies

class CraftedBodies(Live):
    """NaN / Infinity and deep nesting used to kill the request thread (no response at all)."""

    def test_infinity_is_a_400_not_a_dropped_connection(self):
        for path, body in (("/api/orch/assign", b'{"lead": "run-abcd1234", "task": "x", "redo": Infinity}'),
                           ("/api/plan/prune", b'{"days": Infinity}'),
                           ("/hook?event=SessionStart", b'{"session_id": "s-12345", "pid": Infinity}'),
                           ("/api/settings/voice", b'{"report_volume": NaN}')):
            code, out = self.req(path, body, token=True)
            self.assertEqual(code, 400, (path, out))
            self.assertIn("finite", out["error"])
        self.assertEqual(self.store.all(), [])

    def test_deep_nesting_is_a_400(self):
        code, out = self.req("/api/run", b'{"task": ' + b"[" * 200_000 + b"]" * 200_000 + b"}", token=True)
        self.assertEqual(code, 400, out)
        self.assertEqual(self.req("/api/health")[0], 200)   # the server is still up

    def test_ordinary_numbers_still_work(self):
        code, out = self.req("/api/settings/voice", {"report_volume": 40, "enabled": False}, token=True)
        self.assertEqual(code, 200, out)
        self.assertEqual(out["voice"]["report_volume"], 40)

    def test_muted_calendar_names_are_bounded(self):
        with mock.patch.object(push, "sync", return_value={}), mock.patch.object(push, "cancel_all", return_value={}):
            code, out = self.req("/api/settings/push", {"mute_calendars": ["W" * 5000, 7, {"a": 1}, "Home"]},
                                 token=True)
        self.assertEqual(code, 200, out)
        self.assertEqual(self.cfg["push"]["mute_calendars"], ["W" * 200, "Home"])


# ---------------------------------------------------------------- path-like values

class PathValues(Live):
    def ws(self):
        return Path(self.cfg["obsidian"]["workspace_root"])

    def test_cwd_must_stay_in_the_workspace(self):
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        (self.ws() / "escape").symlink_to(outside)   # a link inside the workspace that points out of it
        for cwd in (str(outside), "../outside", "escape", str(self.ws() / "escape"), "~", "/"):
            code, out = self.req("/api/run", {"task": "say hi FAKE_SLEEP=0", "cwd": cwd}, token=True)
            self.assertEqual(code, 400, (cwd, out))
            self.assertIn("workspace", out["error"])
        self.assertEqual(self.store.all(), [])

    def test_project_names_never_become_paths(self):
        bad = ["../Alpha", "Alpha/../..", "..", ".", "/etc", "Alpha/.brain", "Alpha\\..", ".hidden", "NoBrain"]
        for name in bad:
            self.assertEqual(self.req(f"/api/flow?project={urllib.request.quote(name)}")[0], 404, name)
            code, _ = self.req("/api/flow/layout", {"project": name, "view": "map", "positions": {"a": [1, 2]}},
                               token=True)
            self.assertEqual(code, 400, name)
            code, _ = self.req("/api/todo/tick", {"project": name, "text": "Add billing"}, token=True)
            self.assertIn(code, (400, 404, 409), name)
            code, _ = self.req("/api/board/create", {"project": name, "title": "x"}, token=True)
            self.assertEqual(code, 400, name)
            code, _ = self.req("/api/run", {"task": "hi FAKE_SLEEP=0", "project": name}, token=True)
            self.assertEqual(code, 400, name)
        self.assertFalse(Path(self.cfg["flow_layout_path"]).exists())
        self.assertEqual(self.store.all(), [])

    def test_run_ids_in_paths_are_checked(self):
        for path in ("/api/run/..%2F..%2Fetc/tree", "/api/run/run-zz/trace", "/api/run-status?id=../../x",
                     "/static/..%2Fdashboard.html", "/static/../hub/server.py", "/api/log?id=../../etc/passwd"):
            code, out = self.req(path)
            self.assertIn(code, (400, 404), path)
            self.assertNotIn(b"import", out if isinstance(out, bytes) else json.dumps(out).encode())


# ---------------------------------------------------------------- the hook installer

class Installer(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())

    def tearDown(self):
        self._t.__exit__()

    def backups(self, name="settings.json"):
        return sorted(p.name for p in self.tmp.rglob(f"{name}.jarviee-backup-*"))

    def check_untouched(self, text, target="claude", name="settings.json"):
        f = self.tmp / name
        f.write_text(text, encoding="utf-8")
        for uninstall in (False, True):
            self.assertFalse(quiet(install.run, target, path=f, uninstall=uninstall) and not uninstall)
            self.assertEqual(f.read_text(encoding="utf-8"), text)
        self.assertEqual(self.backups(name), [])

    def test_hooks_that_are_not_an_object_are_left_alone(self):
        self.check_untouched(json.dumps({"theme": "dark", "hooks": [{"command": "~/mine.sh"}]}))
        self.check_untouched(json.dumps({"hooks": "~/mine.sh"}))

    def test_an_event_we_use_that_is_not_a_list_is_left_alone(self):
        text = json.dumps({"hooks": {"Stop": {"hooks": [{"type": "command", "command": "say done"}]}}})
        f = self.tmp / "settings.json"
        f.write_text(text, encoding="utf-8")
        self.assertFalse(quiet(install.run, "claude", path=f))
        self.assertEqual(f.read_text(encoding="utf-8"), text)
        self.assertEqual(self.backups(), [])

    def test_unknown_entries_and_empty_groups_are_kept(self):
        mine = {"hooks": {"Elicitation": {"odd": True},
                          "PreCompact": [],
                          "Stop": [{"hooks": []}, {"matcher": "x", "hooks": [{"type": "command", "command": "say hi"}]}]}}
        m = install.merge_claude(mine)
        self.assertEqual(m["hooks"]["Elicitation"], {"odd": True})
        self.assertEqual(m["hooks"]["PreCompact"], [])
        self.assertEqual(m["hooks"]["Stop"][:2], mine["hooks"]["Stop"])
        self.assertEqual(install.merge_claude(m, uninstall=True), mine)
        self.assertEqual(install.merge_claude({"hooks": {}}, uninstall=True), {"hooks": {}})

    def test_undecodable_file_is_left_alone(self):
        f = self.tmp / "settings.json"
        f.write_bytes(b'{"theme": "\xff\xfe"}')
        self.assertFalse(quiet(install.run, "claude", path=f))
        self.assertEqual(f.read_bytes(), b'{"theme": "\xff\xfe"}')
        self.assertEqual(self.backups(), [])

    def test_a_symlinked_settings_file_stays_a_symlink(self):
        dot = self.tmp / "dotfiles"
        dot.mkdir()
        real = dot / "settings.json"
        real.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        os.chmod(real, 0o600)
        link = self.tmp / "settings.json"
        link.symlink_to(real)
        self.assertTrue(quiet(install.run, "claude", path=link))
        self.assertTrue(link.is_symlink())                       # the dotfiles link survives
        data = json.loads(real.read_text(encoding="utf-8"))      # and its target got the hooks
        self.assertEqual(data["theme"], "dark")
        self.assertIn("Stop", data["hooks"])
        self.assertEqual(mode_of(real), 0o600)
        backups = list(dot.glob("settings.json.jarviee-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), {"theme": "dark"})
        self.assertTrue(quiet(install.run, "claude", path=link, uninstall=True))
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text(encoding="utf-8")), {"theme": "dark"})

    def test_paths_with_spaces_and_quotes_are_one_argument(self):
        odd = Path("/Users/someone/My Projects/it's here/hooks/report.py")
        with mock.patch.object(install, "REPORT", odd), \
                mock.patch.object(install.sys, "executable", "/opt/Python 3/bin/python3"):
            cmd = install.hook_command("Stop", "claude")
            self.assertEqual(shlex.split(cmd), ["/opt/Python 3/bin/python3", str(odd), "Stop", "claude"])
            self.assertTrue(install.is_ours({"command": cmd}))
            once = install.merge_claude({})
            self.assertEqual(install.merge_claude(once), once)   # re-running never adds a second copy
            self.assertEqual(install.merge_claude(once, uninstall=True), {})

    def test_backup_comes_before_the_write(self):
        f = self.tmp / "settings.json"
        f.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        with mock.patch.object(install.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                quiet(install.run, "claude", path=f)
        self.assertEqual(json.loads(f.read_text(encoding="utf-8")), {"theme": "dark"})   # original intact
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".settings.json.")], [])


if __name__ == "__main__":
    unittest.main()
