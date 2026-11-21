"""The route table: path -> handler for every HTTP endpoint. Adding an endpoint is one decorated function.

A handler takes a `Ctx` and returns (status, payload) where payload is a dict (sent as JSON), bytes (sent as is,
with the content type given as a third element) or a respond.Rendered. Return None for "not found".
  @get(path, cond=True)      conditional GET: cached for a moment, ETag + 304 (volatile keys left out of the ETag)
  @post(path, token=False)   a POST that skips the API token (none do; every POST needs it by default)
  @get/@post(prefix, prefix=True)  matches every path that starts with the prefix
"""
import math
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

from . import (actions, alerts, apple_calendar, auth, calendar_add, focus, followup, glance, inbox, launcher, mapwatch, obsidian, omniroute, panes,
               board_api, briefing, daybook, jobs_api, orch, orch_api, orchestrate, pipeline, projmap, push, queue_api, runs_api, spans, standup, stt, subagents, templates, todo_edit,
               slash, terminal, usage, voice, wallpaper)
from .config import save_settings
from .dashstate import _read_log_bytes, state as build_state
from .ingest import handle_hook, hook_reply
from .respond import Rendered, make_etag, opt_str, req_str

DASHBOARD = Path(__file__).with_name("dashboard.html")
GET_ROUTES, GET_PREFIX, POST_ROUTES, POST_PREFIX = {}, [], {}, []


class Route:
    def __init__(self, fn, cond=False, token=True):
        self.fn, self.cond, self.token = fn, cond, token


class Ctx:
    """What a handler gets: the server's objects, the path and query, and the (lazily parsed) JSON body."""

    def __init__(self, handler, cfg, store, tasks, sched, path, query):
        self.h, self.cfg, self.store, self.tasks, self.sched = handler, cfg, store, tasks, sched
        self.path, self.query, self._body = path, query, None

    @property
    def q(self):
        return parse_qs(self.query)

    def arg(self, name, default=""):
        return self.q.get(name, [default])[0]

    @property
    def body(self):
        if self._body is None:
            try:
                body = self.h._json()
            except RecursionError:   # "[[[[…" a million deep: json.loads overflows the stack
                raise ValueError("JSON body is nested too deeply") from None
            if not _finite(body):
                raise ValueError("numbers in the JSON body must be finite")
            self._body = body
        return self._body


def _finite(value):
    """False when a parsed body holds NaN or ±Infinity. json.loads accepts both, and int() of one raises OverflowError,
    which no handler expects (the request then died without a response)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, float) and not math.isfinite(v):
            return False
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return True


def get(path, cond=False, prefix=False):
    def deco(fn):
        if prefix:
            GET_PREFIX.append((path, Route(fn, cond)))
        else:
            GET_ROUTES[path] = Route(fn, cond)
        return fn
    return deco


def post(path, token=True, prefix=False):
    def deco(fn):
        if prefix:
            POST_PREFIX.append((path, Route(fn, token=token)))
        else:
            POST_ROUTES[path] = Route(fn, token=token)
        return fn
    return deco


def resolve(method, path):
    table, prefixes = (GET_ROUTES, GET_PREFIX) if method == "GET" else (POST_ROUTES, POST_PREFIX)
    r = table.get(path)
    if r:
        return r
    return next((rt for pre, rt in prefixes if path.startswith(pre)), None)


# ---------------------------------------------------------------- GET

@get("/")
def dashboard(c):
    tok = auth.get_token(c.cfg)
    html = DASHBOARD.read_text(encoding="utf-8")
    injected = (f'<meta name="jarviee-token" content="{tok}">\n'
                f'<script>window.JARVIEE_TOKEN = "{tok}";</script>\n<head>')
    return 200, html.replace("<head>", injected, 1).encode("utf-8"), "text/html; charset=utf-8"


@get("/api/health")
def health(c):
    return 200, {"status": "ok", "port": c.cfg["port"], "pid": os.getpid()}


@get("/api/state", cond=True)
def api_state(c):
    return 200, build_state(c.cfg, c.store, c.tasks, c.sched)


@get("/api/plan")
def api_plan(c):
    pl = c.tasks.plan(c.arg("id")) if c.tasks else None
    return (200, pl) if pl else (404, {"error": "no such plan"})


@get("/api/standup")
def api_standup(c):
    try:
        return 200, standup.standup(c.cfg, c.store, c.arg("date", None))
    except ValueError:
        return 400, {"error": "date must be YYYY-MM-DD"}


@get("/api/stt/status")
def api_stt_status(c):
    return 200, stt.status()   # {"local", "engine": handy | whisper | "", "handy_installed", "handy_shortcut", "hint"}


@get("/api/commands")
def api_commands(c):   # every slash command of the command bar (hub/slash.py)
    return 200, {"commands": slash.registry()}


@get("/api/templates")
def api_templates(c):
    return 200, {"templates": templates.list_all(c.cfg)}


@get("/api/glance")
def api_glance(c):
    return 200, glance.glance(c.cfg, c.store, c.sched, usage.cached(c.cfg))


@get("/api/usage", cond=True)
def api_usage(c):
    return 200, usage.get_usage(c.cfg, c.store)


@get("/api/log")
@get("/api/run-output")
def api_log(c):
    a = c.store.get(c.arg("id"))
    if not a or not a.get("log_path"):
        return 404, {"error": "no log for this agent (only launched runs have one)"}
    text = launcher.strip_tty(_read_log_bytes(a, c.tasks).decode("utf-8", errors="replace"))
    return 200, text.encode("utf-8"), "text/plain; charset=utf-8"


@get("/api/diff")
def api_diff(c):
    rid = c.arg("id")
    a = c.store.get(rid)
    if not a:
        return 404, {"error": f"no agent '{rid}'"}
    return 200, launcher.run_diff(a, c.cfg).encode("utf-8"), "text/plain; charset=utf-8"


@get("/api/inbox", cond=True)
def api_inbox(c):   # finished runs that changed files and wait for the owner's decision (hub/inbox.py)
    items = inbox.list_items(c.cfg, c.store, c.arg("id") or None)
    return 200, {"items": items, "count": len(items)}


@get("/api/alerts")
def api_alerts(c):
    return 200, alerts.status(c.cfg)


@get("/api/flow")
def api_flow(c):   # a project's map (.brain/FLOW.json, validated) and the owner's saved positions
    return projmap.load(c.cfg, c.arg("project"))


@get("/api/activity", cond=True)
def api_activity(c):
    lim = c.arg("limit", "40")
    return 200, {"items": spans.recent(c.store, int(lim) if lim.isdigit() else 40)}


@get("/api/runs", cond=True)
def api_runs(c):
    return runs_api.get(c.path, c.query, c.cfg, c.store, c.tasks, c.sched)


@get("/api/board")
@get("/api/board/projects")
def api_board(c):   # the Project Board: one project's cards, and the project list with counts (hub/board_api.py)
    return board_api.get(c.path, c.query, c.cfg, c.store, c.tasks, c.sched)


@get("/api/jobs", cond=True)
@get("/api/jobs/", prefix=True)
def api_jobs(c):   # the Workflows carousel: complex jobs, and one job's own stages and steps
    return jobs_api.get(c.path, c.query, c.cfg, c.store, c.tasks, c.sched)


@get("/api/run-spec")
def api_run_spec(c):   # hooks/pane_runner.py, once per run
    spec = panes.claim(c.arg("id"))
    return (200, spec) if spec is not None else (410, {"error": "no run waiting for a pane with that id"})


_static = {}   # path -> (stat key, Rendered)
_wall = {}     # version -> Rendered


@get("/api/wallpaper/info")
def api_wallpaper_info(c):   # which photo sits behind the glass, its version (cache key) and the choices
    out = wallpaper.info(c.cfg)
    _, out["v"] = wallpaper.jpeg(c.cfg)
    return 200, out


@get("/api/wallpaper")
def api_wallpaper(c):   # the photo behind the glass, as a JPEG (converted once from the desktop's HEIC)
    path, v = wallpaper.jpeg(c.cfg)
    if path is None:
        return 404, b"no wallpaper", "text/plain"
    hit = _wall.get(v)
    if hit is None:
        _wall.clear()
        try:
            hit = _wall[v] = Rendered(path.read_bytes(), make_etag("wall", v), "image/jpeg")
        except OSError:
            return 404, b"no wallpaper", "text/plain"
    return 200, hit


@get("/static/", prefix=True)
def api_static(c):
    """A file of hub/static/ with an ETag from its size and mtime: reloads revalidate, an edit shows at once."""
    p = runs_api.static_path(c.path[len("/static/"):])
    if p is None:
        return 404, b"not found", "text/plain"
    try:
        st = p.stat()
    except OSError:
        return 404, b"not found", "text/plain"
    key = (st.st_size, st.st_mtime_ns)
    hit = _static.get(str(p))
    if not hit or hit[0] != key:
        body = p.read_bytes()
        hit = _static[str(p)] = (key, Rendered(body, make_etag(st.st_size, st.st_mtime_ns),
                                               runs_api.TYPES[p.name.rsplit(".", 1)[1]]))
    return 200, hit[1]


@get("/", prefix=True)
def runs_paths(c):   # /api/run/<id>/tree|trace, /api/run-status
    return runs_api.get(c.path, c.query, c.cfg, c.store, c.tasks, c.sched)


# ---------------------------------------------------------------- POST


def _validate_hook(body):
    for key in ("jarvieeRunId", "agentHubRunId", "jarvieeTool", "agentHubTool", "conversationId", "session_id",
                "hook_event_name", "cwd", "tool_name", "lastUserInput", "prompt", "jarvieeTitle", "agentHubTitle",
                "jarvieeSummary", "agentHubSummary", "notification_type", "message", "title",
                "terminationReason", "last_assistant_message", "error_details", "task_subject", "agentHost",
                "tool_use_id"):
        opt_str(body, key)
    if body.get("toolCall") is not None and not isinstance(body["toolCall"], dict):
        raise ValueError("'toolCall' must be an object")
    ws = body.get("workspacePaths")
    if ws is not None and (not isinstance(ws, list) or not all(isinstance(x, str) for x in ws)):
        raise ValueError("'workspacePaths' must be a list of strings")


@post("/api/stt")
def api_stt(c):   # raw audio from the mic button -> Handy's model (or whisper.cpp), see hub/stt.py
    try:
        n = int(c.h.headers.get("Content-Length") or 0)
    except ValueError:
        return 400, {"error": "invalid Content-Length"}
    if n <= 0 or n > stt.MAX_AUDIO:
        return 400, {"error": "recording is empty or too large"}
    try:
        return 200, {"text": stt.transcribe(c.h.rfile.read(n))}
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return 500, {"error": str(e) or "transcription timed out"}


@post("/api/attach")
def api_attach(c):   # raw image bytes from the command bar (paste / drop)
    return actions.save_attachment(c.cfg, c.h)


@post("/hook")
def api_hook(c):
    body = c.body
    _validate_hook(body)
    event = c.q.get("event", [body.get("hook_event_name", "")])[0]
    return 200, hook_reply(event, body, handle_hook(event, body, c.cfg, c.store), c.store)


@post("/api/classify")
def api_classify(c):
    body = c.body
    raw_task = req_str(body, "task")
    tool = opt_str(body, "tool")
    tier = opt_str(body, "tier")
    project = opt_str(body, "project")
    text = pipeline.requested(orchestrate.requested(raw_task)[1], body)[1]   # "/orchestrate …", "/pipeline …": no prefix
    p = launcher.plan(text, c.cfg, tool, tier or None, project, store=c.store)
    is_pipe = bool(c.sched and pipeline.applies(c.cfg, p, body))
    return 200, {**p, "pipeline": is_pipe, "phases": pipeline.phase_names(c.cfg) if is_pipe else []}


@post("/api/ask")
def api_ask(c):   # schedule questions answered from Apple Calendar; anything else is left for /api/run
    q = req_str(c.body, "question").strip()
    if calendar_add.parse(q) is not None or not apple_calendar.is_schedule_question(q):   # "add … Friday 3 pm" is never a read
        return 200, {"handled": False}
    try:
        al, agenda = apple_calendar.answer_parts(q)
    except (apple_calendar.CalendarError, subprocess.TimeoutExpired) as e:
        return 200, {"handled": True, "answer": str(e) or "the calendar took too long", "error": True}
    heads = ("Heads up. " + " ".join(al) + " ") if al else ""
    spoken = bool(voice.ACTIVE and voice.ACTIVE.speak(" ".join((heads + agenda).split())))
    return 200, {"handled": True, "alerts": al, "schedule": agenda, "answer": (heads + agenda).strip(), "spoken": spoken}


@post("/api/calendar/parse")
def api_calendar_parse(c):   # no side effects: what "add … Friday 3 pm" would create
    return 200, calendar_add.parse_request(opt_str(c.body, "text") or "")


@post("/api/calendar/add")
def api_calendar_add(c):   # the one write; only the explicit "Add to calendar" action calls it
    return calendar_add.add(c.body)


@post("/api/calendar/undo")
def api_calendar_undo(c):   # removes only the event the add above just created
    opt_str(c.body, "undo")
    return calendar_add.undo(c.body)


@get("/api/briefing")
def api_briefing(c):   # the same briefing as /startwork, without speaking it or sending it to the phone
    return 200, briefing.build(c.cfg, c.store)


@post("/api/run")
def api_run(c):
    body = c.body
    raw_task = req_str(body, "task")
    tool = opt_str(body, "tool")
    tier = opt_str(body, "tier")
    project = opt_str(body, "project")
    cwd = (opt_str(body, "cwd") or "").strip()
    task_text = raw_task.strip()
    if not task_text:
        return 400, {"error": "task is empty"}
    if cwd:
        ws_root = obsidian.workspace(c.cfg).expanduser().resolve()
        raw_p = Path(cwd).expanduser()
        target = (raw_p if raw_p.is_absolute() else ws_root / raw_p).resolve()
        if not target.is_relative_to(ws_root):
            return 400, {"error": "cwd must be inside the workspace_root"}
        if not target.is_dir():
            return 400, {"error": f"working directory not found: {cwd}"}
        cwd = str(target)
    if briefing.is_startwork(task_text):   # "/startwork": what's ahead, spoken and sent to the phone; launch nothing
        return 200, {"startwork": True, **briefing.startwork(c.cfg, c.store)}
    if daybook.is_endwork(task_text):   # "/endwork": read out today's summary, launch nothing
        if not daybook.ACTIVE:
            return 503, {"error": "the day book is not running"}
        return 200, {"endwork": True, **daybook.ACTIVE.endwork()}
    cmd, rest = slash.parse(task_text)
    if cmd and cmd.answers:   # "/help", "/status", "/standup", "/usage", "/map <project>": answered here, only /map launches
        return slash.answer(cmd, rest, c.cfg, c.store, c.tasks, c.sched)
    if actions.APPROVAL_PROMPT_RE.search(task_text):
        return 400, {"error": "Approval prompts and owner questions cannot be launched as automated tasks."}
    asked, task_text = orchestrate.requested(task_text)   # "/orchestrate <task>": Opus leads a team in a 4×2 tab
    if asked:
        if not task_text:
            return 400, {"error": "/orchestrate needs a task: /orchestrate <what to build>"}
        if terminal.active(c.cfg) is None:
            return 400, {"error": "/orchestrate needs the iTerm2 panes, and they are off or not available"}
        rid, p = orch.start(c.cfg, c.store, task_text, project or None, cwd or None)
        return 200, {"orchestrate": True, "id": rid, **p}
    is_pipe = bool(c.sched and pipeline.applies(c.cfg, None, body, task_text))
    task_text = pipeline.requested(task_text, body)[1]   # only "/pipeline …" starts one; the prefix is dropped
    if not task_text:
        return 400, {"error": "task is empty"}
    p = launcher.plan(task_text, c.cfg, tool, tier or None, project or None)
    if is_pipe:   # researcher -> project-manager (plan) -> coder (build)
        plan = c.sched.pipeline.start(task_text, project or "", p,
                                      cwd=cwd, tool=tool or "")
        c.sched.tick()
        return 200, {"pipeline": True, "plan_id": plan["id"], "id": plan["id"], **p, "phases": pipeline.phase_names(c.cfg)}
    launch_text = task_text if pipeline.PREFIX_RE.match(raw_task) else raw_task
    rid, p = launcher.launch(launch_text, c.cfg, c.store, tool, tier or None,
                             project or None, cwd or None, p=p)
    return 200, {"id": rid, **p}


@post("/api/rerun")
def api_rerun(c):   # run again / reply, from a finished launched run
    body = c.body
    rid_in = opt_str(body, "id") or ""
    reply = opt_str(body, "reply") or ""
    tool = opt_str(body, "tool") or None
    tier = opt_str(body, "tier") or None
    try:
        rid, p, mode = followup.start(c.store.get(rid_in), c.cfg, c.store, reply, tool, tier)
    except (followup.FollowupError, ValueError) as e:
        return 400, {"error": str(e)}
    return 200, {"id": rid, "mode": mode, **p}


def _inbox_call(c, fn):
    """Run an inbox action; a refusal is a 409 (404 for an unknown run) carrying the reason and its code."""
    try:
        return 200, fn(opt_str(c.body, "id") or "")
    except inbox.InboxError as e:
        return (404 if e.code == "no_run" else 409), {"error": str(e), "code": e.code}


@post("/api/inbox/accept")
def api_inbox_accept(c):
    return _inbox_call(c, lambda rid: {"ok": True, "id": rid, "reviewed": inbox.decide(c.store, rid, inbox.ACCEPTED, c.cfg)})


@post("/api/inbox/dismiss")
def api_inbox_dismiss(c):
    return _inbox_call(c, lambda rid: {"ok": True, "id": rid, "reviewed": inbox.decide(c.store, rid, inbox.DISMISSED)})


@post("/api/inbox/revert")
def api_inbox_revert(c):   # one new `git revert` commit, only when provably safe
    def go(rid):
        new, old = inbox.revert(c.cfg, c.store, rid)
        return {"ok": True, "id": rid, "reviewed": inbox.REVERTED, "commit": new, "reverted": old}
    return _inbox_call(c, go)


@post("/api/inbox/check")
def api_inbox_check(c):   # run the project's own test command (opt-in) and store pass / fail
    return _inbox_call(c, lambda rid: {"ok": True, "id": rid, "check": inbox.run_check(c.cfg, c.store, rid, c.body.get("trust_command"))})


@post("/api/todo/tick")
@post("/api/todo/undo")
def api_todo(c):   # owner presses a button in the Projects rail
    body = c.body
    proj = opt_str(body, "project") or ""
    try:
        if c.path.endswith("tick"):
            return 200, todo_edit.tick(c.cfg, proj, opt_str(body, "text"), bool(body.get("owner")))
        return 200, todo_edit.undo(c.cfg, proj, opt_str(body, "before") or "", opt_str(body, "after") or "")
    except (todo_edit.TodoError, OSError) as e:
        return (409 if isinstance(e, todo_edit.TodoError) else 500), {"error": str(e)}


@post("/api/templates")
def api_templates_save(c):
    try:
        return 200, {"templates": templates.save(c.cfg, opt_str(c.body, "name"), opt_str(c.body, "body"))}
    except ValueError as e:
        return 400, {"error": str(e)}


@post("/api/templates/delete")
def api_templates_delete(c):
    try:
        return 200, {"templates": templates.delete(c.cfg, opt_str(c.body, "name"))}
    except KeyError:
        return 404, {"error": "no such template"}


@post("/api/quota/probe")
def api_quota_probe(c):
    tool = opt_str(c.body, "tool") or "agy"
    model = opt_str(c.body, "model")
    ok, msg = actions.probe_tool_quota(tool, model, c.cfg, c.store)
    return 200, {"ok": ok, "message": msg, "quota": c.store.get_all_quota(c.cfg)}


@get("/api/omniroute/status")
def api_omniroute_status(c):
    return 200, omniroute.status(c.cfg)


@post("/api/settings/fallback")
def api_settings_fallback(c):
    body = c.body
    fb_cfg = c.cfg.setdefault("fallback", {})
    if "enabled" in body:
        fb_cfg["enabled"] = bool(body["enabled"])
    for key in ("never_fallback_to_opus", "use_available_tool"):
        if key in body:
            fb_cfg[key] = bool(body[key])
    if "omniroute" in body:
        c.cfg.setdefault("omniroute", {})["enabled"] = bool(body["omniroute"])
    omni_enabled = bool(c.cfg.get("omniroute", {}).get("enabled", True))
    try:
        save_settings(c.cfg)
    except OSError as e:
        return 200, {"ok": True, "fallback": fb_cfg, "omniroute": omni_enabled, "warning": f"not saved: {e}"}
    return 200, {"ok": True, "fallback": fb_cfg, "omniroute": omni_enabled}


@post("/api/alerts")
def api_alerts_set(c):
    if "muted" in c.body:
        alerts.set_muted(c.cfg, bool(c.body["muted"]))
    return 200, alerts.status(c.cfg)


@post("/api/action/", prefix=True)
def api_action(c):
    return actions.hub_action(c.path[len("/api/action/"):], c.cfg, c.store, c.sched)


@post("/api/stop")
def api_stop(c):
    ok, reason = launcher.stop_with_reason(opt_str(c.body, "id"), c.store)
    return 200, {"ok": ok, "reason": reason}


@post("/api/clear")
def api_clear(c):
    c.store.clear_finished()
    if c.tasks:
        c.tasks.clear_finished()   # finished plans leave the Workflows library and queue too
    return 200, {"ok": True}


@post("/api/run-event")
def api_run_event(c):
    opt_str(c.body, "id")
    opt_str(c.body, "event")
    return actions.run_event(c.cfg, c.body, c.store)


@post("/api/terminal")
def api_terminal(c):
    opt_str(c.body, "action")
    opt_str(c.body, "key")
    opt_str(c.body, "id")
    return actions.terminal_action(c.cfg, c.store, c.body)


@post("/api/flow/layout")
def api_flow_layout(c):
    opt_str(c.body, "project")
    opt_str(c.body, "view")
    return projmap.save_layout(c.cfg, c.body)


@post("/api/flow/generate")
def api_flow_generate(c):   # launch an agent that writes the project's .brain/FLOW.json
    body = c.body
    name = opt_str(body, "project") or ""
    tool = opt_str(body, "tool") or None
    proj = obsidian.find_project(c.cfg, name)
    if not proj:
        return 400, {"error": f"no project '{name}' with a .brain/ folder"}
    rid, p = slash.generate_map(c.cfg, c.store, proj, tool)
    return 200, {"id": rid, **p}


@post("/api/flow/update")
def api_flow_update(c):   # bring an existing map up to date now
    ok, msg, rid = mapwatch.launch(c.cfg, c.store, opt_str(c.body, "project") or "", force=bool(c.body.get("force")))
    return 200, {"ok": ok, "message": msg, "id": rid}


@post("/api/settings/appearance")
def api_settings_appearance(c):   # the photo behind the glass: follow the desktop, one of the Mac's stills, or none
    w = opt_str(c.body, "wallpaper") or ""
    if w not in wallpaper.choices(c.cfg):
        return 400, {"error": "unknown wallpaper"}
    c.cfg.setdefault("appearance", {})["wallpaper"] = w
    try:
        save_settings(c.cfg)
    except OSError as e:
        return 200, {"ok": True, "warning": f"not saved: {e}"}
    out = wallpaper.info(c.cfg)
    _, out["v"] = wallpaper.jpeg(c.cfg)
    return 200, {"ok": True, "appearance": out}


@post("/api/settings/voice")
def api_settings_voice(c):   # the spoken report: on/off, takes effect on the next run (no restart)
    v = c.cfg.setdefault("voice", {})
    for key in ("enabled", "tldr"):
        if key in c.body:
            v[key] = bool(c.body[key])
    for key in ("report_volume", "duck_volume"):   # % of the Mac's full volume (hub/duck.py)
        if re.fullmatch(r"\d{1,3}", str(c.body.get(key, ""))) and 0 <= int(c.body[key]) <= 100:
            v[key] = int(c.body[key])
    vc = voice.get_voice_cfg(c.cfg)
    out = {"enabled": bool(vc["enabled"]), "tldr": bool(vc["tldr"]),
           "report_volume": int(vc["report_volume"]), "duck_volume": int(vc["duck_volume"])}
    try:
        save_settings(c.cfg)
    except OSError as e:
        return 200, {"ok": True, "voice": out, "warning": f"not saved: {e}"}
    return 200, {"ok": True, "voice": out}


@post("/api/settings/push")
def api_settings_push(c):   # phone notifications (ntfy): on/off; off takes back queued alerts
    body, cfg = c.body, c.cfg
    if "enabled" in body:
        cfg.setdefault("push", {})["enabled"] = bool(body["enabled"])
    pc = cfg.setdefault("push", {})
    if re.fullmatch(r"\d{1,3}", str(body.get("lead_min", ""))) and 1 <= int(body["lead_min"]) <= 240:
        pc["lead_min"] = int(body["lead_min"])
    if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body.get("digest_at", ""))):
        pc["digest_at"] = body["digest_at"]
    if "limit_alerts" in body:
        pc["limit_alerts"] = bool(body["limit_alerts"])
    if isinstance(body.get("mute_calendars"), list):
        pc["mute_calendars"] = [str(x)[:200] for x in body["mute_calendars"] if isinstance(x, str)][:50]
    warn = ""
    try:
        save_settings(cfg)
    except OSError as e:
        warn = f"not saved: {e}"
    r = push.sync(cfg) if push.settings(cfg)["enabled"] else push.cancel_all(cfg)
    return 200, {"ok": True, "push": push.status(cfg), "warning": warn or r.get("error", "")}


@post("/api/push/rotate")
def api_push_rotate(c):   # topic leaked: new random name, queued alerts taken back
    push.rotate_topic(c.cfg)
    try:
        save_settings(c.cfg)
    except OSError:
        pass
    if push.settings(c.cfg)["enabled"]:
        push.sync(c.cfg)
    return 200, {"ok": True, "push": push.status(c.cfg)}


@post("/api/push/test")
def api_push_test(c):
    ok, err = push.test(c.cfg)
    return (200 if ok else 502), {"ok": ok, **({"error": err} if err else {})}


@post("/api/push/sync")
def api_push_sync(c):   # re-read the calendar now instead of waiting for the next round
    r = push.sync(c.cfg)
    return 200, {"ok": not r.get("error"), "push": push.status(c.cfg)}


@post("/api/settings/maps")
def api_settings_maps(c):
    m = c.cfg.setdefault("maps", {})
    if "auto_update" in c.body:
        m["auto_update"] = bool(c.body["auto_update"])
    try:
        save_settings(c.cfg)
    except OSError as e:
        return 200, {"ok": True, "maps": mapwatch.settings(c.cfg), "warning": f"not saved: {e}"}
    return 200, {"ok": True, "maps": mapwatch.settings(c.cfg)}


@post("/api/focus")
def api_focus(c):   # bring the agent's own window (pane, terminal tab, editor) forward
    aid = opt_str(c.body, "id") or ""
    a = c.store.get(subagents.parent_of(aid) or aid)   # a subagent lives in its parent's window
    if not a:
        return 404, {"error": "no such agent"}
    ok, msg = focus.focus(a, c.cfg)
    return 200, {"ok": ok, "message": msg}


@post("/api/orch/", prefix=True)
def api_orch(c):   # the /orchestrate lead's commands: assign, wait, result, review, status (hub/orch_api.py)
    return orch_api.post(c.path, c.body, c.cfg, c.store)


@post("/api/board/", prefix=True)
def api_board_post(c):   # create / update / delete / move / advance / revert-polish / start
    return board_api.post(c.path, c.body, c.cfg, c.store, c.sched)


@post("/api/plan", prefix=True)
@post("/api/queue", prefix=True)
@post("/api/task", prefix=True)
def api_queue(c):
    if not c.tasks:
        return None
    return queue_api.post(c.path, c.body, c.cfg, c.tasks, c.sched)
