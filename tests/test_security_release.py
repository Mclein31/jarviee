"""Security hardening before the public release: cross-origin reads, the token, the svg icon, local file permissions,
pids a pane runner may report, and values that reach URLs or the terminal."""
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from helpers import ROOT, TempDir, make_cfg
from hub import auth, brand, panes, runs_api, server, terminal
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def load_hook(name):
    spec = importlib.util.spec_from_file_location(f"hook_{name}", ROOT / "hooks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mode_of(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class LiveServer(unittest.TestCase):
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

    def req(self, path, body=None, headers=None, method=None):
        """(status, headers, raw body). JSON POSTs carry no token unless the test passes one."""
        h = dict(headers or {})
        if body is not None:
            h.setdefault("Content-Type", "application/json")
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with _OPEN(r, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()


class CrossOriginReads(LiveServer):
    """A web page in the owner's browser must not read anything served here, nor learn the token."""

    def test_dns_rebinding_host_gets_nothing(self):
        for path in ("/", "/api/state", "/api/runs", "/api/briefing", "/static/core.js"):
            code, _, raw = self.req(path, headers={"Host": f"evil.example:{self.port}"})
            self.assertEqual(code, 403, path)
            self.assertNotIn(self.token.encode(), raw)

    def test_other_sites_and_ports_cannot_load_get_endpoints(self):
        cases = [
            {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"},
            {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},          # a link from another site
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"},            # <script>/<img> from :3000
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"},
            {"Origin": "http://127.0.0.1:3000"},
            {"Origin": "null"},                                                      # sandboxed iframe, file://
            {"Origin": f"http://127.0.0.1:{self.port}.evil.example"},
        ]
        for h in cases:
            for path in ("/", "/api/state", "/api/log?id=x"):
                code, _, raw = self.req(path, headers=h)
                self.assertEqual(code, 403, f"{path} {h}")
                self.assertNotIn(self.token.encode(), raw)

    def test_the_dashboard_itself_still_works(self):
        ok = [
            {},                                                                      # curl, hooks, the CLI
            {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"},                # typed URL / bookmark
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"},           # a link from another local port
            {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Origin": self.base},
            {"Sec-Fetch-Site": "same-origin", "Origin": f"http://localhost:{self.port}"},
        ]
        for h in ok:
            code, _, _ = self.req("/api/health", headers=h)
            self.assertEqual(code, 200, h)
        code, _, html = self.req("/", headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(code, 200)
        self.assertIn(self.token.encode(), html)   # only the dashboard's own page carries it

    def test_every_response_refuses_embedding_and_sniffing(self):
        for path in ("/", "/api/state", "/api/health", "/static/core.js", "/static/mark.svg", "/nope"):
            _, h, _ = self.req(path)
            self.assertEqual(h["X-Content-Type-Options"], "nosniff", path)
            self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin", path)
            self.assertEqual(h["X-Frame-Options"], "DENY", path)
            self.assertEqual(h["Referrer-Policy"], "no-referrer", path)
            self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"], path)
            self.assertNotIn("Access-Control-Allow-Origin", h, path)

    def test_json_and_text_responses_are_inert_documents(self):
        _, h, _ = self.req("/api/state")
        csp = h["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("sandbox", csp)

    def test_dashboard_csp_allows_only_its_own_scripts(self):
        code, h, html = self.req("/")
        self.assertEqual(code, 200)
        csp = h["Content-Security-Policy"]
        script_src = re.search(r"script-src ([^;]*)", csp).group(1)
        self.assertNotIn("unsafe-inline", script_src)
        self.assertNotIn("unsafe-eval", script_src)
        self.assertIn("'self'", script_src)
        inline = [m.group(2) for m in re.finditer(rb"<script(\s[^>]*)?>(.*?)</script>", html, re.S)
                  if b"src=" not in (m.group(1) or b"")]
        self.assertTrue(inline, "the dashboard has inline scripts (the token, J.start())")
        for body in inline:   # each inline script the page really has is allowed by its exact hash
            digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", script_src)
        for d in ("object-src 'none'", "base-uri 'none'", "connect-src 'self'"):
            self.assertIn(d, csp)


class TokenHandling(LiveServer):
    def test_token_in_the_query_string_is_ignored(self):
        code, _, _ = self.req(f"/api/classify?token={self.token}", body={"task": "fix typo"})
        self.assertEqual(code, 403)
        code, _, _ = self.req(f"/api/run-spec?id=run-abcd&token={self.token}")
        self.assertEqual(code, 403)

    def test_header_and_bearer_tokens_still_work(self):
        code, _, _ = self.req("/api/classify", body={"task": "fix typo"}, headers={"X-Jarviee-Token": self.token})
        self.assertEqual(code, 200)
        code, _, _ = self.req("/api/classify", body={"task": "fix typo"},
                              headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(code, 200)
        code, _, _ = self.req("/api/run-spec?id=run-abcd", headers={"X-Jarviee-Token": self.token})
        self.assertEqual(code, 410)   # authorised; just no such pane run

    def test_non_ascii_token_is_a_403_not_a_dropped_connection(self):
        code, _, raw = self.req("/api/classify", body={"task": "x"}, headers={"X-Jarviee-Token": "tökén"})
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(raw), {"error": "forbidden"})
        self.assertFalse(auth.verify_token("é" * 48, self.cfg))
        self.assertFalse(auth.verify_token("☃", self.cfg))
        self.assertTrue(auth.verify_token(f" {self.token} ", self.cfg))

    def test_cross_site_post_with_a_simple_content_type_is_refused(self):
        r = urllib.request.Request(self.base + "/api/run", data=b"task=rm", method="POST",
                                   headers={"Content-Type": "application/x-www-form-urlencoded",
                                            "X-Jarviee-Token": self.token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _OPEN(r, timeout=5)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)


class StaticSvg(LiveServer):
    def test_mark_svg_is_served_with_a_script_free_policy(self):
        code, h, body = self.req("/static/mark.svg")
        self.assertEqual(code, 200)
        self.assertEqual(h["Content-Type"], "image/svg+xml")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        csp = h["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("sandbox", csp)
        self.assertNotIn("script-src", csp)
        self.assertTrue(body.lstrip().startswith(b"<svg"))
        etag = h["ETag"]
        code, h304, _ = self.req("/static/mark.svg", headers={"If-None-Match": etag})
        self.assertEqual(code, 304)
        self.assertIn("sandbox", h304["Content-Security-Policy"])   # a revalidation keeps the svg's own policy

    def test_only_plain_names_in_the_static_folder(self):
        for name in ("../server.py", "..%2Fserver.py", "mark.SVG", "x/../mark.svg", ".mark.svg", "mark.svgz",
                     "%2e%2e/auth.py", "mark.svg/", "mark.svg%00.png"):
            code, _, _ = self.req("/static/" + name)
            self.assertEqual(code, 404, name)
        self.assertIsNone(runs_api.static_path("../dashboard.svg"))
        self.assertEqual(runs_api.TYPES["svg"], "image/svg+xml")

    def test_csp_helpers(self):
        page = b'<script src="/static/a.js"></script><script>J.start();</script><SCRIPT type="x">b()</SCRIPT>'
        csp = server.html_csp(page)
        for body in (b"J.start();", b"b()"):
            self.assertIn("'sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode() + "'", csp)
        self.assertNotIn(base64.b64encode(hashlib.sha256(b"").digest()).decode(), csp)   # the src= script is not hashed
        self.assertEqual(server.csp_for("image/svg+xml"), server.SVG_CSP)
        self.assertEqual(server.csp_for("application/json"), server.INERT_CSP)
        self.assertEqual(server.csp_for(None), server.INERT_CSP)


class LocalFiles(unittest.TestCase):
    """Other local users of the Mac: the token and Jarviee's data are the owner's alone."""

    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())

    def tearDown(self):
        self._t.__exit__()

    def test_new_token_file_is_0600_in_a_0700_folder(self):
        cfg = {"token_path": str(self.tmp / "fresh" / "token")}
        old = os.umask(0o022)
        try:
            tok = auth.get_token(cfg)
        finally:
            os.umask(old)
        p = Path(cfg["token_path"])
        self.assertEqual(p.read_text().strip(), tok)
        self.assertEqual(mode_of(p), 0o600)
        self.assertEqual(mode_of(p.parent), 0o700)
        self.assertEqual(auth.get_token(cfg), tok)
        self.assertEqual([f for f in os.listdir(p.parent) if f.startswith(".token-")], [])   # no temp file left

    def test_concurrent_first_starts_agree_on_one_token(self):
        cfg = {"token_path": str(self.tmp / "token")}
        got, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            got.append(auth.get_token(cfg))
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(set(got)), 1)
        self.assertEqual(Path(cfg["token_path"]).read_text().strip(), got[0])

    def test_empty_token_file_is_replaced(self):
        p = self.tmp / "token"
        p.write_text("\n")
        tok = auth.get_token({"token_path": str(p)})
        self.assertEqual(len(tok), 48)
        self.assertEqual(p.read_text().strip(), tok)
        self.assertEqual(mode_of(p), 0o600)

    def test_harden_closes_token_and_database_to_other_users(self):
        for name in ("token", "hub.db", "hub.db-wal", "hub.db-shm"):
            (self.tmp / name).write_text("x\n")
            os.chmod(self.tmp / name, 0o644)
        os.chmod(self.tmp, 0o755)
        cfg = {"token_path": str(self.tmp / "token"), "db_path": str(self.tmp / "hub.db")}
        auth.harden(cfg)
        for name in ("token", "hub.db", "hub.db-wal", "hub.db-shm"):
            self.assertEqual(mode_of(self.tmp / name), 0o600, name)
        self.assertEqual(mode_of(self.tmp), 0o755)   # not Jarviee's own folder: only the files are tightened

    def test_harden_closes_jarviees_own_folder(self):
        home = self.tmp / ".jarviee"
        home.mkdir(mode=0o755)
        os.chmod(home, 0o755)
        (home / "hub.db").write_text("")
        os.chmod(home / "hub.db", 0o644)
        with mock.patch.object(brand, "HOME", home), mock.patch.object(brand, "LEGACY_HOME", self.tmp / "none"):
            changed = auth.harden({"token_path": str(home / "token"), "db_path": str(home / "hub.db")})
        self.assertEqual(mode_of(home), 0o700)
        self.assertEqual(mode_of(home / "hub.db"), 0o600)
        self.assertIn(str(home.resolve()), changed)

    def test_private_leaves_symlinks_and_tight_files_alone(self):
        target = self.tmp / "target"
        target.write_text("")
        os.chmod(target, 0o644)
        link = self.tmp / "link"
        link.symlink_to(target)
        self.assertFalse(auth.private(link))
        self.assertEqual(mode_of(target), 0o644)
        os.chmod(target, 0o600)
        self.assertFalse(auth.private(target))
        self.assertFalse(auth.private(self.tmp / "missing"))

    def test_server_start_tightens_a_loose_token_file(self):
        cfg, _ = make_cfg(str(self.tmp))
        Path(cfg["token_path"]).write_text("a" * 48 + "\n")
        os.chmod(cfg["token_path"], 0o644)
        srv = make_server(cfg, Store(cfg["db_path"]), scheduler=False)
        try:
            self.assertEqual(mode_of(cfg["token_path"]), 0o600)
            self.assertEqual(mode_of(cfg["db_path"]), 0o600)
        finally:
            srv.server_close()


class PaneCommand(unittest.TestCase):
    def test_token_never_travels_in_the_pane_command(self):
        with TempDir() as tmp:
            path = Path(tmp) / "token"
            cfg = {"port": 7777, "token_path": str(path)}
            tok = auth.get_token(cfg)
            for kw in ({}, {"slot": True}):
                cmd = terminal.pane_command(cfg, "run-ab12", **kw)
                self.assertNotIn(tok, cmd)
                self.assertNotIn("JARVIEE_TOKEN=", cmd)
                self.assertIn(f"JARVIEE_TOKEN_PATH={path}", cmd)
            self.assertNotIn(tok, terminal.pane_command(cfg, None, slot=True))

    def test_pane_command_creates_no_token_file(self):
        with TempDir() as tmp:
            path = Path(tmp) / "nowhere" / "token"
            terminal.pane_command({"port": 7777, "token_path": str(path)}, "run-ab12")
            self.assertFalse(path.exists())

    def test_runner_reads_the_token_from_the_file(self):
        runner = load_hook("pane_runner")
        with TempDir() as tmp:
            p = Path(tmp) / "token"
            p.write_text("secret-from-file\n")
            env = {k: v for k, v in os.environ.items() if not k.endswith("_TOKEN")}
            env["JARVIEE_TOKEN_PATH"] = str(p)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(runner.load_token(), "secret-from-file")


class PanePids(unittest.TestCase):
    def tearDown(self):
        panes.drop("run-sec1")

    def test_start_refuses_pids_that_name_a_group_or_everyone(self):
        panes.register("run-sec1", {"argv": ["true"]})
        for pid in (0, -1, -42, 1, True, "0", "-1"):
            code, _ = panes.event("run-sec1", {"event": "start", "pid": pid})
            self.assertEqual(code, 400, repr(pid))
        self.assertFalse(panes.get("run-sec1").started.is_set())
        code, _ = panes.event("run-sec1", {"event": "start", "pid": os.getpid()})
        self.assertEqual(code, 200)

    def test_pane_proc_never_signals_a_group(self):
        with mock.patch("hub.panes.os.kill") as kill:
            for pid in (0, -1, 1):
                proc = panes.PaneProc(pid)
                proc.terminate()
                self.assertEqual(proc.poll(), 0)
            kill.assert_not_called()


class HookValues(unittest.TestCase):
    def test_report_port_must_be_a_number(self):
        report = load_hook("report")
        self.assertEqual(report._port("8123"), "8123")
        for bad in ("7777@evil.example", "evil.example", "", None, "0", "70000", "80/x", " 1 2"):
            self.assertEqual(report._port(bad), "7777", repr(bad))

    def test_report_event_is_quoted_into_the_url(self):
        report = load_hook("report")
        seen = []

        class Resp:
            def read(self):
                return b"{}"

        def fake_open(req, timeout=None):
            seen.append((req.full_url, timeout))
            return Resp()
        with mock.patch.object(report, "_OPEN", fake_open), \
                mock.patch.object(report.sys, "argv", ["report.py", "Stop&event=x#frag", "agy"]), \
                mock.patch.object(report.sys, "stdin", mock.Mock(read=lambda: "{}")), \
                mock.patch.object(report, "print_reply"):
            report.main()
        self.assertEqual(len(seen), 1)
        url, timeout = seen[0]
        self.assertTrue(url.startswith(f"http://127.0.0.1:{report.PORT}/hook?event=Stop%26event%3Dx%23frag"), url)
        self.assertLessEqual(timeout, 2)

    def test_runner_banner_strips_terminal_escapes(self):
        runner = load_hook("pane_runner")
        text = runner.banner({"title": "t\x1b]0;pwned\x07", "cwd": "/w/\x1b]1337;SetBadgeFormat=x\x07dir"}, "run-x")
        visible = text.replace("\033[1m", "").replace("\033[2m", "").replace("\033[0m", "")
        self.assertNotIn("\x1b", visible)
        self.assertNotIn("\x07", visible)
        self.assertIn("run-x", runner.banner({}, "run-x"))


if __name__ == "__main__":
    unittest.main()
