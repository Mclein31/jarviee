"""Command center: local web dashboard + receiver for agent hook events.

The HTTP plumbing (guard, conditional GET, gzip) lives here; the endpoints are in hub/routes.py, hook ingestion in
hub/ingest.py, the /api/state build in hub/dashstate.py and the multi-step POST actions in hub/actions.py. Names that
tests and other modules import from `hub.server` are re-exported below.
"""
import base64
import gzip
import hashlib
import json
import re
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from . import alerts, auth, batch, coord, outcome, brand, detector, dispatch, fallback, focus, followup, glance, todo_edit, standup, templates, runcost, launcher, obsidian, mapwatch, panes, pipeline, projmap, queue_api, subagents, review, runs_api, spans, terminal, usage, voice, stt, apple_calendar, push, limitwatch, worktrees
from . import routes
from .actions import (ACTION_RE, APPROVAL_PROMPT_RE, MAX_ATTACH, _IMG_EXT, close_verdict, hub_action, probe_tool_quota,  # noqa: F401
                      run_event, save_attachment, terminal_action)
from .dashstate import _read_log_bytes, state, tool_status  # noqa: F401
from .ingest import (CONTEXT_EVENTS, GENERIC_NOTIFICATION_TITLES, INJECTED_PROMPT_RE, NON_WAITING_NOTIFICATION_TYPES,  # noqa: F401
                     WAITING_EVENTS, _edited_path, _extract_effort, _format_notification, coordinate, handle_hook,
                     hook_reply, new_job, normalize_model, tier_of_model, transcript_model)
from .respond import MIN_GZIP, ResponseCache, accepts_gzip, compressible, etag_matches
from .router import TIERS, classify  # noqa: F401
from .routes import DASHBOARD  # noqa: F401
from .scheduler import Scheduler
from .tasks import Tasks

MAX_BODY = 1_000_000

# Sent with every response: no other site may embed, frame or read-through-a-tag anything served here.
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Cross-Origin-Resource-Policy", "same-origin"),
                    ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"))
# Anything that is not the dashboard (JSON, logs, images, svg) renders with no script, ever, even when opened directly.
INERT_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; sandbox"
_INLINE_SCRIPT_RE = re.compile(rb"<script(\s[^>]*)?>(.*?)</script\s*>", re.S | re.I)


def html_csp(body):
    """The dashboard's policy: scripts only from /static/ plus the page's own inline scripts by hash (the served HTML
    holds no user data, so whatever a later innerHTML injects, an inline handler or a new script, never runs)."""
    hashes = "".join(" 'sha256-" + base64.b64encode(hashlib.sha256(m.group(2)).digest()).decode() + "'"
                     for m in _INLINE_SCRIPT_RE.finditer(body or b"") if b"src=" not in (m.group(1) or b"").lower())
    return ("default-src 'self'; script-src 'self'" + hashes + "; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def csp_for(ctype, body=b""):
    kind = (ctype or "").split(";")[0].strip().lower()
    if kind == "text/html":
        return html_csp(body)
    return SVG_CSP if kind == "image/svg+xml" else INERT_CSP


def make_handler(cfg, store, tasks=None, sched=None):
    allowed_hosts = {f"127.0.0.1:{cfg['port']}", f"localhost:{cfg['port']}"}
    cache = ResponseCache(store.version)   # rendered GET responses, reused briefly while the database is unchanged

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _headers(self, code, ctype, length, etag=None, cache_control="no-store", gzipped=False, body=b"",
                     csp_type=None):
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache_control)
            if etag:
                self.send_header("ETag", etag)
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            if etag or ctype and compressible(ctype):
                self.send_header("Vary", "Accept-Encoding")
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Security-Policy", csp_for(csp_type or ctype, body))   # a 304 keeps its type's policy
            self.end_headers()

        def _send(self, code, body, ctype="application/json"):
            """Send bytes / a JSON-able value / a respond.Rendered. A Rendered carries an ETag: a matching
            If-None-Match gets a 304 with no body. Compressible bodies over 1 KB are gzipped when the client accepts it."""
            want_gz = accepts_gzip(self.headers.get("Accept-Encoding"))
            try:
                if hasattr(body, "etag"):   # respond.Rendered
                    if code == 200 and etag_matches(self.headers.get("If-None-Match"), body.etag):
                        return self._headers(304, None, None, body.etag, "no-cache", body=body.body, csp_type=body.ctype)
                    zip_it = want_gz and len(body.body) > MIN_GZIP and compressible(body.ctype)
                    raw = body.gzipped() if zip_it else body.body
                    self._headers(code, body.ctype, len(raw), body.etag, "no-cache", zip_it, body.body)
                else:
                    plain = raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                    zip_it = want_gz and len(raw) > MIN_GZIP and compressible(ctype)
                    if zip_it:
                        raw = gzip.compress(raw, 4, mtime=0)
                    self._headers(code, ctype, len(raw), None, "no-store", zip_it, plain)
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _request_token(self):
            """The install token from a header only. Never from the query string: a URL leaks into history, logs and
            Referer headers."""
            tok = self.headers.get(brand.TOKEN_HEADER) or self.headers.get(brand.LEGACY_TOKEN_HEADER)
            if not tok and self.headers.get("Authorization", "").startswith("Bearer "):
                tok = self.headers.get("Authorization", "").split(" ", 1)[1]
            return tok

        def _trusted(self, post, token=True):
            """Block DNS rebinding and requests from other web pages. GETs too: only the dashboard itself (same
            origin), a typed URL / bookmark, or a non-browser client (hooks, CLI: no Sec-Fetch headers) gets through."""
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in allowed_hosts:
                return False
            site = self.headers.get("Sec-Fetch-Site", "")
            if site == "cross-site":
                return False
            if site == "same-site" and self.headers.get("Sec-Fetch-Mode", "") != "navigate":
                return False   # a page on another 127.0.0.1 port: it may link here, never load or embed what we serve
            if post:
                ctype = (self.headers.get("Content-Type") or "").lower()
                is_img = urlparse(self.path).path == "/api/attach" and ctype.split(";")[0].strip() in _IMG_EXT
                if not (ctype.startswith("application/json") or is_img):   # image uploads still need the token
                    return False
                if token and not auth.verify_token(self._request_token(), cfg):
                    return False
            return True

        def _json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("invalid Content-Length") from None
            if n < 0:
                raise ValueError("invalid Content-Length")
            if not n:
                return {}
            if n > MAX_BODY:
                raise ValueError("request body too large")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError("invalid JSON body") from None
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def do_GET(self):
            if not self._trusted(False):
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            if u.path == "/api/run-spec" and not auth.verify_token(self._request_token(), cfg):
                return self._send(403, {"error": "forbidden"})
            route = routes.resolve("GET", u.path)
            if not route:
                return self._send(404, {"error": "not found"})
            c = routes.Ctx(self, cfg, store, tasks, sched, u.path, u.query)
            try:
                if route.cond:
                    r = cache.get(u.path + "?" + u.query, lambda: route.fn(c) or (404, {"error": "not found"}))
                else:
                    r = route.fn(c)
            except (ValueError, TypeError) as e:
                r = (400, {"error": str(e)})
            except Exception as e:   # a handler bug answers 500 instead of dropping the connection
                print(f"[server] GET {u.path}: {e!r}")
                r = (500, {"error": "internal error"})
            self._send(*(r or (404, {"error": "not found"})))

        def do_POST(self):
            route = routes.resolve("POST", urlparse(self.path).path)
            if not self._trusted(True, route.token if route else True):
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            try:
                r = route.fn(routes.Ctx(self, cfg, store, tasks, sched, u.path, u.query)) if route else None
            except (ValueError, TypeError) as e:
                r = (400, {"error": str(e)})
            except Exception as e:   # a handler bug answers 500 instead of dropping the connection
                print(f"[server] POST {u.path}: {e!r}")
                r = (500, {"error": "internal error"})
            if u.path != "/hook":   # hooks only write the store, which the cache already watches
                cache.clear()       # before the reply, so the next poll after any action is never a stale snapshot
            self._send(*(r or (404, {"error": "not found"})))

    return H


def make_server(cfg, store, scheduler=True, interval=1.0):
    """The HTTP server plus the queue: a Tasks table and a started Scheduler (srv.scheduler)."""
    host = cfg.get("host", "127.0.0.1")
    assert host == "127.0.0.1", f"Refusing to bind to {host}; Jarviee binds to 127.0.0.1 only."
    auth.get_token(cfg)   # Ensure per-install token is initialized and persisted
    try:
        auth.harden(cfg)   # other local users: no reading the token, hub.db or ~/.jarviee (0600 / 0700)
    except Exception as e:   # never keep the dashboard from starting over a permission
        print(f"[security] could not tighten data file permissions: {e}")
    tasks = Tasks(store)
    cfg["_serving"] = True   # launched runs may open iTerm2 panes (they need this server's run-spec API)
    if "_terminal" not in cfg:
        cfg["_terminal"] = terminal.Terminal(cfg)
    sched = Scheduler(cfg, store, tasks, interval=interval) if scheduler else None
    if sched:
        sched.on_plan_done = lambda pid: review.run_review(cfg, tasks, pid, quota=sched.quota)
        sched.start()
    alerts_cfg = alerts.get_alerts_cfg(cfg)
    voice_on = voice.get_voice_cfg(cfg)["enabled"]
    watcher = None
    if (alerts_cfg.get("enabled", True) or voice_on) and brand.env("ALERTS") != "0":
        try:   # a report cut off by a crash left music paused / the volume ducked: put it back (hub/duck.py)
            from . import duck
            if duck.recover(cfg):
                print("[voice] restored audio left ducked by an interrupted report")
        except Exception as e:
            print(f"[voice] duck recovery failed: {e}")
        speaker = voice.ACTIVE = voice.Speaker(cfg, store=store)
        watcher = alerts.AlertWatcher(cfg, store, speaker=speaker).start()
    from . import daybook
    daybook.ACTIVE = daybook.Daybook(cfg, store)   # /endwork reads it; the background summaries run only on the real server
    if scheduler and brand.env("DAYBOOK") != "0":
        daybook.ACTIVE.start()
    if scheduler and brand.env("MAILSUM") != "0":   # gists of new mail ahead of /startwork (Gmail connected only)
        from . import mailsum
        mailsum.MailWatcher(cfg).start()
    mapw = None
    if scheduler and brand.env("MAPWATCH") != "0":   # real server only: tests never launch map updates
        mapw = mapwatch.MapWatcher(cfg, store)
        mapw.start()
    class Server(ThreadingHTTPServer):
        request_queue_size = 128   # the default of 5 reset connections when a page load opened 20 at once
        daemon_threads = True
    srv = Server(("127.0.0.1", cfg["port"]), make_handler(cfg, store, tasks, sched))
    pushw = None
    if scheduler and brand.env("PUSH") != "0":   # real server only: tests never reach ntfy
        pushw = push.PushWatcher(cfg)
        pushw.start()
        limitwatch.LimitWatcher(cfg, store).start()   # phone: Claude 5-hour limit spent / back
    srv.tasks, srv.scheduler, srv.watcher, srv.mapwatch, srv.push = tasks, sched, watcher, mapw, pushw
    if scheduler:   # real server: read transcripts once in the background; tests skip it
        usage.prewarm(cfg, store)
        launcher.prune_logs(cfg)   # logs older than 30 days
        if worktrees.settings(cfg)["enabled"]:   # finish worktrees of runs that ended while the hub was away
            threading.Thread(target=worktrees.sweep, args=(cfg, store, lambda r: r in launcher._threads and
                                                           launcher._threads[r].is_alive()), daemon=True).start()
    return srv


def serve(cfg, store):
    srv = make_server(cfg, store)
    print(f"Jarviee command center → http://127.0.0.1:{cfg['port']}")
    ws = obsidian.workspace(cfg)
    print(f"Workspace: {ws}" + ("" if obsidian.brain_ok(cfg) else "  (NOT FOUND — set obsidian.workspace_root)"))
    if obsidian.global_ok(cfg):
        print(f"Global brain: {obsidian.global_brain(cfg)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if srv.scheduler:
            srv.scheduler.stop()
        if getattr(srv, "watcher", None):
            srv.watcher.stop()
        if getattr(srv, "mapwatch", None):
            srv.mapwatch.stop()
        if getattr(srv, "push", None):
            srv.push.stop()
        srv.server_close()