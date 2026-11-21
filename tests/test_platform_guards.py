"""Regression tests for platform guards, stdlib-only fallbacks, and fresh-user hazards."""
import builtins
import contextlib
import importlib
import io
import json
import os
import plistlib
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers
from helpers import TempDir, make_cfg
from hub import (
    app_launcher,
    apple_calendar as ac,
    auth,
    briefing,
    browser,
    calendar_add as ca,
    config as hub_config,
    doctor,
    entry,
    icon,
    inbox,
    orphans,
    push,
    runjob,
    service,
    voice,
    wallpaper,
    wamsg,
)
from hub.server import make_server
from hub.store import Store

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class _NoPilMixin:
    def block_pil(self):
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "PIL" or name.startswith("PIL."):
                raise ModuleNotFoundError("No module named 'PIL'", name="PIL")
            return real_import(name, globals, locals, fromlist, level)

        p1 = mock.patch.object(builtins, "__import__", side_effect=guarded_import)
        p2 = mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageDraw": None})
        p1.start()
        p2.start()
        self.addCleanup(p2.stop)
        self.addCleanup(p1.stop)


class PillowFallbackTests(unittest.TestCase, _NoPilMixin):
    def test_icon_and_app_launcher_import_and_install_without_pillow(self):
        self.block_pil()
        importlib.reload(icon)
        importlib.reload(app_launcher)
        try:
            with TempDir() as td:
                custom_app = Path(td) / "Jarviee.app"
                out = io.StringIO()
                with mock.patch.object(app_launcher, "APP_PATH", custom_app), \
                     contextlib.redirect_stdout(out):
                    ok = app_launcher.install({"port": 7777}, dry_run=False)
                self.assertTrue(ok)
                self.assertTrue((custom_app / "Contents" / "Info.plist").is_file())
                self.assertTrue((custom_app / "Contents" / "MacOS" / "jarviee-launcher").is_file())
                self.assertFalse((custom_app / "Contents" / "Resources" / "Jarviee.icns").exists())
                self.assertIn("Pillow is not installed", out.getvalue())

                with mock.patch.object(icon.subprocess, "run") as run:
                    with self.assertRaises(ImportError):
                        icon.build_icns(Path(td) / "x.icns")
                    run.assert_not_called()
        finally:
            self.addCleanup(lambda: (importlib.reload(icon), importlib.reload(app_launcher)))


class VoiceFcntlGuardTests(unittest.TestCase):
    def test_voice_imports_without_fcntl_in_fresh_interpreter(self):
        code = (
            "import sys; sys.modules['fcntl'] = None; "
            "from hub import voice; "
            "assert voice.fcntl is None; "
            "assert voice._speech_lock() is None; "
            "print('OK')"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=str(helpers.ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)

    def test_speaker_speaks_cleanly_when_fcntl_is_none(self):
        with TempDir() as tmp:
            cfg = {"alerts_state_path": str(Path(tmp) / "alerts.json"), "voice": {"enabled": True, "debounce_s": 0}}
            said = []
            sp = voice.Speaker(cfg, runner=lambda argv: said.append(argv[-1]), start_worker=False)
            with mock.patch.object(voice, "fcntl", None):
                self.assertIsNone(voice._speech_lock())
                self.assertTrue(sp.speak("Agent 1 is done"))
                sp.drain()
            self.assertEqual(said, ["Agent 1 is done"])


class KeychainGuardTests(unittest.TestCase):
    def test_briefing_keychain_handles_missing_security_binary_and_timeout(self):
        for exc in (FileNotFoundError(2, "No such file or directory", "/usr/bin/security"), subprocess.TimeoutExpired(["security"], 10)):
            with self.subTest(exc=type(exc).__name__):
                def boom(*a, **kw):
                    raise exc
                self.assertIsNone(briefing._keychain(["find-generic-password"], runner=boom))
                self.assertEqual(briefing.gmail_account({}, runner=boom), ("", None))

    def test_briefing_read_mail_and_connect_gmail_off_darwin(self):
        with mock.patch.object(briefing.sys, "platform", "linux"), \
             mock.patch.object(briefing.subprocess, "run", side_effect=FileNotFoundError("/usr/bin/security")):
            g, li = briefing.read_mail({})
            self.assertFalse(g["ok"])
            self.assertFalse(g["connected"])
            self.assertIn("not available on this platform", g["error"].lower())
            self.assertFalse(li["connected"])

            ok, msg = briefing.connect_gmail("user@example.com")
            self.assertFalse(ok)
            self.assertIn("not available on this platform", msg.lower())

        with mock.patch.object(briefing.sys, "platform", "darwin"):
            def missing(*a, **kw):
                raise FileNotFoundError("/usr/bin/security")
            ok, msg = briefing.connect_gmail("user@example.com", runner=missing)
            self.assertFalse(ok)
            self.assertIn("not available on this platform", msg.lower())

    def test_wamsg_security_guards_and_connect_off_darwin(self):
        for exc in (FileNotFoundError(2, "missing"), subprocess.TimeoutExpired(["security"], 10)):
            def boom(*a, **kw):
                raise exc
            self.assertIsNone(wamsg._security(["find-generic-password"], runner=boom))
            self.assertEqual(wamsg.account(runner=boom), ("", None))
            self.assertFalse(wamsg.handles({"whatsapp_send": {"enabled": True}}, "digest", runner=boom))

        with mock.patch.object(wamsg.sys, "platform", "win32"):
            ok, msg = wamsg.connect("+639171234567", check=False)
            self.assertFalse(ok)
            self.assertIn("not available on this platform", msg.lower())

        with mock.patch.object(wamsg.sys, "platform", "darwin"):
            def missing(*a, **kw):
                raise FileNotFoundError("/usr/bin/security")
            ok, msg = wamsg.connect("+639171234567", runner=missing, check=False)
            self.assertFalse(ok)
            self.assertIn("not available on this platform", msg.lower())


class ServiceGuardTests(unittest.TestCase):
    def test_non_darwin_gates_all_service_commands(self):
        for plat in ("linux", "win32"):
            with self.subTest(plat=plat), \
                 mock.patch.object(service.sys, "platform", plat), \
                 mock.patch.object(service.subprocess, "run") as run:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertFalse(service.is_installed())
                    self.assertFalse(service.install())
                    self.assertFalse(service.install(dry_run=True))
                    self.assertFalse(service.uninstall())
                    self.assertFalse(service.restart())
                    self.assertFalse(service.status())
                    self.assertFalse(service.logs())
                run.assert_not_called()
                self.assertIn("only available on macOS", err.getvalue())

    def test_resolve_path_dirs_uses_os_pathsep(self):
        with mock.patch.object(service.os, "pathsep", ";"):
            joined = service.resolve_path_dirs()
        self.assertIn(";", joined)

    def test_get_uid_without_os_getuid(self):
        fake_os = SimpleNamespace(path=os.path, environ=os.environ, pathsep=os.pathsep)
        self.assertFalse(hasattr(fake_os, "getuid"))
        with mock.patch.object(service, "os", fake_os):
            self.assertIsNone(service.get_uid())

    def test_refuses_mismatched_home_unless_forced(self):
        with TempDir() as td:
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            plist_dir = fake_home / "Library" / "LaunchAgents"
            log_path = fake_home / ".jarviee" / "logs" / "server.log"
            real_home = Path(td) / "real_home"
            real_home.mkdir()

            with mock.patch.object(service.sys, "platform", "darwin"), \
                 mock.patch.object(service, "_real_home", return_value=real_home.resolve()), \
                 mock.patch.object(service.Path, "home", return_value=fake_home), \
                 mock.patch.object(service, "DEFAULT_PLIST_DIR", plist_dir), \
                 mock.patch.object(service, "DEFAULT_LOG_PATH", log_path), \
                 mock.patch.object(service.subprocess, "run") as run:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertFalse(service.install())
                    self.assertFalse(service.uninstall())
                run.assert_not_called()
                self.assertFalse((plist_dir / service.PLIST_FILENAME).exists())
                self.assertIn("--force", err.getvalue())

                def ok_run(cmd, **kw):
                    if cmd[:2] == ["launchctl", "print"]:
                        return subprocess.CompletedProcess(cmd, 1, "", "")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                run.side_effect = ok_run
                with mock.patch.object(service, "get_uid", return_value=501):
                    self.assertTrue(service.install(force=True))
                    self.assertTrue((plist_dir / service.PLIST_FILENAME).is_file())
                    self.assertTrue(service.uninstall(force=True))
                    self.assertFalse((plist_dir / service.PLIST_FILENAME).exists())

    def test_hub_py_service_force_flag(self):
        import hub as _hub_pkg  # noqa: F401
        hub_cli = importlib.util.spec_from_file_location("hub_cli_mod", str(helpers.ROOT / "hub.py"))
        mod = importlib.util.module_from_spec(hub_cli)
        hub_cli.loader.exec_module(mod)

        with mock.patch("hub.config.load_config", return_value={}), \
             mock.patch.object(service, "install", return_value=True) as inst, \
             mock.patch.object(service, "uninstall", return_value=True) as uninst:
            with self.assertRaises(SystemExit) as cm:
                mod.main(["service", "install", "--force"])
            self.assertEqual(cm.exception.code, 0)
            inst.assert_called_once_with(mock.ANY, dry_run=False, force=True)

            with self.assertRaises(SystemExit) as cm:
                mod.main(["service", "uninstall", "--force"])
            self.assertEqual(cm.exception.code, 0)
            uninst.assert_called_once_with(force=True)


class BrowserAndJarvieeBinTests(unittest.TestCase):
    def test_browser_open_dashboard_and_osascript_off_darwin(self):
        for plat in ("linux", "win32"):
            with self.subTest(plat=plat), \
                 mock.patch.object(browser.sys, "platform", plat), \
                 mock.patch.object(browser.webbrowser, "open", return_value=True) as wb, \
                 mock.patch.object(browser.subprocess, "run") as run:
                self.assertEqual(browser.open_dashboard({"port": 7777}), "default")
                self.assertEqual(browser.open_dashboard({"port": 7777, "dashboard_window": {"browser": "safari"}}), "default")
                self.assertEqual(wb.call_count, 2)
                wb.assert_called_with("http://127.0.0.1:7777/")
                run.assert_not_called()
                with self.assertRaises(OSError):
                    browser._osascript("return 1", [])
                run.assert_not_called()

    def test_bin_jarviee_uses_webbrowser_and_skips_osascript_off_darwin(self):
        mod = runpy.run_path(str(helpers.ROOT / "bin" / "jarviee"))
        with mock.patch("sys.platform", "linux"), \
             mock.patch("subprocess.run") as run, \
             mock.patch("webbrowser.open", return_value=True) as wb:
            mod["show_failure_alert"]("/tmp/server.log")
            run.assert_not_called()
            with mock.patch.object(entry, "start_server_if_down", return_value=(True, False)), \
                 mock.patch.dict(mod["ensure_running"].__globals__, {"_call": lambda *a, **kw: None}):
                rc = mod["ensure_running"]({"port": 7777}, open_browser=True)
            self.assertEqual(rc, 0)
            run.assert_not_called()
            wb.assert_called_once_with("http://127.0.0.1:7777/")

    def test_entry_stop_server_skips_launchctl_off_darwin(self):
        with TempDir() as td:
            pid_path = Path(td) / "server.pid"
            with mock.patch.object(entry.sys, "platform", "linux"), \
                 mock.patch.object(entry, "check_health_info", return_value=(False, {})), \
                 mock.patch.object(entry, "check_health", return_value=False), \
                 mock.patch.object(entry.service, "is_installed", return_value=True), \
                 mock.patch.object(entry.subprocess, "run") as run:
                self.assertTrue(entry.stop_server({"port": 7777}, pid_path=pid_path))
                run.assert_not_called()


class WallpaperGuardTests(unittest.TestCase):
    def test_sips_missing_or_timeout_falls_back_to_no_wallpaper(self):
        with TempDir() as td:
            pic = Path(td) / "wall.png"
            pic.write_bytes(b"png")
            cfg = {"wallpaper_dir": str(Path(td) / "cache"), "appearance": {"wallpaper": "custom"}}
            for exc in (FileNotFoundError(2, "missing", "/usr/bin/sips"), subprocess.TimeoutExpired(["sips"], 60)):
                def boom(*a, **kw):
                    raise exc
                with mock.patch.dict(wallpaper.CHOICES, {"custom": ("Custom", (pic,))}):
                    self.assertEqual(wallpaper.jpeg(cfg, runner=boom), (None, "none"))
            self.assertEqual(list((Path(td) / "cache").glob("*.tmp.jpg")), [])


class AuthGuardTests(unittest.TestCase):
    def test_private_and_harden_skip_when_os_getuid_missing(self):
        with TempDir() as td:
            p = Path(td) / "token"
            p.write_text("secret\n")
            os.chmod(p, 0o644)
            fake_os = SimpleNamespace(lstat=os.lstat, chmod=os.chmod, path=os.path, environ=os.environ)
            self.assertFalse(hasattr(fake_os, "getuid"))
            with mock.patch.object(auth, "os", fake_os):
                self.assertFalse(auth.private(p))
                self.assertEqual(auth.harden({"token_path": str(p)}), [])
            self.assertEqual(p.stat().st_mode & 0o777, 0o644)


class SignalKillGuardsTests(unittest.TestCase):
    def test_inbox_check_timeout_without_killpg_or_sigkill_falls_back_to_proc_kill(self):
        with TempDir() as td:
            cfg, _ = make_cfg(td)
            store = Store(cfg["db_path"])
            store.upsert("run-1", source="launcher", status="done", files=["a.py"], task="t")

            fake_proc = mock.Mock()
            fake_proc.pid = 99999
            fake_proc.communicate.side_effect = [subprocess.TimeoutExpired(["cmd"], 1), ("", "")]
            fake_os = SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if k != "killpg" and not k.startswith("__")})
            fake_sig = SimpleNamespace(**{k: getattr(signal, k) for k in dir(signal) if k != "SIGKILL" and not k.startswith("__")})

            with mock.patch.object(inbox.subprocess, "Popen", return_value=fake_proc), \
                 mock.patch.object(inbox, "os", fake_os), \
                 mock.patch.object(inbox, "signal", fake_sig):
                res = inbox._run_check_locked(cfg, store, "run-1", ["cmd"], "cmd", td)
            fake_proc.kill.assert_called_once()
            self.assertEqual(res["status"], "fail")
            self.assertIn("timed out", res["output"])

    def test_entry_stop_server_without_sigkill_uses_sigterm(self):
        with TempDir() as td:
            pid_path = Path(td) / "server.pid"
            pid_path.write_text("42424", encoding="utf-8")
            fake_sig = SimpleNamespace(SIGTERM=signal.SIGTERM)
            with mock.patch.object(entry.sys, "platform", "win32"), \
                 mock.patch.object(entry, "check_health_info", return_value=(False, {})), \
                 mock.patch.object(entry, "check_health", return_value=False), \
                 mock.patch.object(entry, "is_pid_alive", return_value=True), \
                 mock.patch.object(entry.time, "sleep"), \
                 mock.patch.object(entry, "signal", fake_sig), \
                 mock.patch.object(entry.os, "kill") as kill:
                self.assertTrue(entry.stop_server({"port": 7777}, pid_path=pid_path))
            self.assertEqual(kill.call_args_list, [mock.call(42424, signal.SIGTERM), mock.call(42424, signal.SIGTERM)])


class CalendarClockFormatTests(unittest.TestCase):
    def test_clock_does_not_use_percent_dash_i(self):
        class WinDatetime(datetime):
            def strftime(self, fmt):
                if "%-" in fmt:
                    raise ValueError("Invalid format string")
                return super().strftime(fmt)

        cases = [
            (WinDatetime(2026, 11, 11, 0, 0), "12 am"),
            (WinDatetime(2026, 11, 11, 0, 5), "12:05 am"),
            (WinDatetime(2026, 11, 11, 9, 0), "9 am"),
            (WinDatetime(2026, 11, 11, 9, 30), "9:30 am"),
            (WinDatetime(2026, 11, 11, 12, 0), "12 pm"),
            (WinDatetime(2026, 11, 11, 12, 45), "12:45 pm"),
            (WinDatetime(2026, 11, 11, 13, 0), "1 pm"),
            (WinDatetime(2026, 11, 11, 23, 59), "11:59 pm"),
        ]
        for dt, expected in cases:
            self.assertEqual(ac._clock(dt), expected)


class DoctorWindowsGuardTests(unittest.TestCase):
    def test_doctor_skips_posix_mode_checks_on_nt(self):
        with TempDir() as td:
            tmp = Path(td)
            ws = tmp / "ws"
            (ws / "p" / ".brain").mkdir(parents=True)
            ov = tmp / "ov.json"
            ov.write_text(json.dumps({"obsidian": {"workspace_root": str(ws)}, "terminal": {"mode": "background"}}))
            os.chmod(ov, 0o644)
            data = tmp / "data"
            data.mkdir(mode=0o755)
            tok = data / "token"
            tok.write_text("x")
            os.chmod(tok, 0o644)

            fake_os = SimpleNamespace(
                name="nt",
                stat=os.stat,
                path=os.path,
            )
            with mock.patch.dict(os.environ, {
                "JARVIEE_USER_CONFIG": str(ov),
                "JARVIEE_CLAUDE_SETTINGS": str(tmp / "c.json"),
                "JARVIEE_AGY_HOOKS": str(tmp / "a.json"),
            }), \
                 mock.patch("hub.brand.home", return_value=data), \
                 mock.patch("hub.auth.token_path", return_value=tok), \
                 mock.patch.object(doctor, "os", fake_os), \
                 mock.patch.object(doctor.sys, "platform", "win32"):
                res = doctor.checks(
                    which=lambda n: "/bin/" + n,
                    runner=mock.Mock(return_value=mock.Mock(stdout="1.0\n", stderr="")),
                    health=lambda c: True,
                )
            self.assertNotIn("fail", {lvl for lvl, _, _ in res})
            self.assertFalse(any("chmod" in hint for _, _, hint in res))
            self.assertTrue(any("skipped on Windows" in text for _, text, _ in res))


class OrphansAndRunJobGuardTests(unittest.TestCase):
    def test_orphans_alive_treats_only_permission_error_as_alive(self):
        with mock.patch.object(orphans.os, "kill", side_effect=PermissionError("access denied")):
            self.assertTrue(orphans.alive(12345))
        with mock.patch.object(orphans.os, "kill", side_effect=OSError(22, "The parameter is incorrect")):
            self.assertFalse(orphans.alive(12345))

    def test_runjob_resolves_cmd_via_shutil_which(self):
        with TempDir() as td:
            cfg, _ = make_cfg(td)
            store = Store(cfg["db_path"])
            log_path = Path(td) / "run.log"
            job = runjob.RunJob(
                run_id="r1",
                task="hi",
                cfg=cfg,
                store=store,
                p={"tool": "claude", "model": "haiku", "tier": "basic", "effort": "low"},
                prompt="hi",
                argv_for=lambda pr: ["claude", "-p", pr],
                workdir=td,
                log_path=log_path,
                env={"PATH": r"C:\npm"},
                tracker={"has_output": False},
                co={"decision": "proceed", "with": []},
                title="hi",
                term=None,
                ro=False,
                on_finish=None,
                log_runs=False,
                hops=0,
                fallback_from=None,
                retries=0,
            )
            fake_proc = mock.Mock(pid=555)
            fake_proc.wait.return_value = 0
            shim = r"C:\npm\claude.cmd"
            with mock.patch.object(runjob.shutil, "which", return_value=shim) as wh, \
                 mock.patch.object(runjob.subprocess, "Popen", return_value=fake_proc) as popen, \
                 mock.patch.object(runjob.orphans, "proc_start", return_value=""), \
                 mock.patch.object(runjob.guards, "headless_argv", side_effect=lambda cfg, p, argv: argv), \
                 mock.patch("hub.launcher._observe_log_steps"), \
                 mock.patch("hub.launcher.sync_run_progress"):
                code, err = job._run_headless(["claude", "-p", "hi"])
            self.assertEqual((code, err), (0, ""))
            wh.assert_called_once_with("claude", path=r"C:\npm")
            self.assertEqual(popen.call_args[0][0][0], shim)


class CalendarOptInTests(unittest.TestCase):
    def test_config_json_disables_calendar_by_default(self):
        raw = json.loads((helpers.ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw.get("calendar"), {"enabled": False})
        self.assertFalse(ac.is_enabled(raw))
        self.assertFalse(ac.is_enabled({}))
        self.assertTrue(ac.is_enabled({"calendar": {"enabled": True}}))

    def test_all_calendar_readers_and_writers_refuse_without_running_helper_when_disabled(self):
        ca._cache.update(at=0.0, names=["Work"], fail_at=0.0, fail="")
        ca._PENDING.clear()
        with TempDir() as td:
            cfg, _ = make_cfg(td)
            self.assertFalse(ac.is_enabled(cfg))
            with mock.patch.object(ac, "build_app", side_effect=AssertionError("must not build")), \
                 mock.patch.object(ac, "_run_app", side_effect=AssertionError("must not run")), \
                 mock.patch.object(ac, "_run_apply", side_effect=AssertionError("must not apply")):
                with self.assertRaises(ac.CalendarError) as cm:
                    ac.fetch(cfg=cfg)
                self.assertEqual(str(cm.exception), ac.DISABLED_MSG)

                with self.assertRaises(ac.CalendarError) as cm:
                    ac.apply({}, cfg=cfg)
                self.assertEqual(str(cm.exception), ac.DISABLED_MSG)

                with self.assertRaises(ac.CalendarError) as cm:
                    ac.answer_parts("what's on today", cfg=cfg)
                self.assertEqual(str(cm.exception), ac.DISABLED_MSG)

                sec = briefing.read_calendar(cfg)
                self.assertFalse(sec["ok"])
                self.assertFalse(sec["connected"])
                self.assertEqual(sec["error"], ac.DISABLED_MSG)

                push_cfg = {**cfg, "push": {"enabled": True}, "push_state_path": str(Path(td) / "push.json")}
                pst = push.sync(push_cfg)
                self.assertEqual(pst["error"], ac.DISABLED_MSG)

                pr = ca.parse_request("add quiz Friday 3pm", cfg=cfg)
                self.assertTrue(pr["matched"])
                self.assertEqual(pr["calendars"], [])
                self.assertEqual(pr["calendars_error"], ac.DISABLED_MSG)

                code, body = ca.add({"event": {"title": "Quiz", "calendar": "Work", "start": "2026-11-13T15:00", "end": "2026-11-13T16:00"}}, cfg=cfg)
                self.assertEqual((code, body), (200, {"ok": False, "error": ac.DISABLED_MSG}))

                code, body = ca.undo({"undo": "tok"}, cfg=cfg)
                self.assertEqual((code, body), (200, {"ok": False, "error": ac.DISABLED_MSG}))

    def test_live_http_routes_do_not_invoke_swift_helper_when_calendar_disabled(self):
        ca._cache.update(at=0.0, names=[], fail_at=0.0, fail="")
        with TempDir() as td:
            cfg, _ = make_cfg(td)
            store = Store(cfg["db_path"])
            srv = make_server(cfg, store, scheduler=False)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{cfg['port']}"
            tok = auth.get_token(cfg)
            try:
                def post(path, payload):
                    req = urllib.request.Request(
                        base + path,
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json", "X-Jarviee-Token": tok},
                        method="POST",
                    )
                    with _OPEN(req, timeout=5) as resp:
                        return resp.status, json.loads(resp.read())

                with mock.patch.object(ac, "build_app", side_effect=AssertionError("must not build")), \
                     mock.patch.object(ac, "_run_app", side_effect=AssertionError("must not run")), \
                     mock.patch.object(ac, "_run_apply", side_effect=AssertionError("must not apply")):
                    code, r = post("/api/ask", {"question": "what's on today"})
                    self.assertEqual((code, r["handled"], r["error"], r["answer"]), (200, True, True, ac.DISABLED_MSG))

                    code, r = post("/api/calendar/parse", {"text": "add quiz Friday 3pm"})
                    self.assertEqual((code, r["matched"], r["calendars"], r["calendars_error"]), (200, True, [], ac.DISABLED_MSG))

                    code, r = post("/api/calendar/add", {"event": {"title": "Quiz", "calendar": "Work", "start": "2026-11-13T15:00", "end": "2026-11-13T16:00"}})
                    self.assertEqual((code, r), (200, {"ok": False, "error": ac.DISABLED_MSG}))

                    code, r = post("/api/calendar/undo", {"undo": "tok"})
                    self.assertEqual((code, r), (200, {"ok": False, "error": ac.DISABLED_MSG}))
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
