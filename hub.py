#!/usr/bin/env python3
"""Jarviee CLI: serve the command center, launch tasks on the right model, install hooks."""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

from hub import auth, brand, launcher
from hub.config import load_config

FINISHED = ("done", "failed", "stopped")
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def _url(cfg, path):
    return f"http://127.0.0.1:{cfg['port']}{path}"


def _call(cfg, path, body=None, timeout=3):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", brand.TOKEN_HEADER: auth.get_token(cfg)}
    req = urllib.request.Request(_url(cfg, path), data=data, headers=headers)
    try:
        with _OPEN(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        raise SystemExit(f"error: {msg}")


def server_up(cfg):
    try:
        _OPEN(_url(cfg, "/api/state"), timeout=1).read()
        return True
    except Exception:
        return False


def _print_plan(p):
    eff = f"  effort {p['effort']}" if p.get("effort") else ""
    print(f"tier: {p['tier']}  →  {p['tool']} · {p['model']}{eff}")
    for r in p["reasons"]:
        print(f"  - {r}")


def cmd_serve(a, cfg):
    from hub.server import serve
    from hub.store import Store
    if server_up(cfg):
        raise SystemExit(f"Jarviee is already running at {_url(cfg, '/')}")
    if brand.migrate_home():   # first start after the rename: ~/.agent-hub → ~/.jarviee
        print(f"moved {brand.LEGACY_HOME} to {brand.HOME} (old path kept as a symlink)")
        cfg = load_config()
    serve(cfg, Store(cfg["db_path"]))


def cmd_classify(a, cfg):
    p = launcher.plan(a.task, cfg, a.tool, a.tier, a.project)
    print(json.dumps(p, indent=2)) if a.json else _print_plan(p)


def _clean(text):
    """Printable text only: a plan note may carry agent output, never terminal escapes."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(text))


def _pipeline_run(a, cfg, p):
    """A /pipeline task became a pipeline plan: say so and, with --wait, follow it to the end."""
    names = {"research": "research", "plan": "plan", "build": "build", "test": "test", "review": "review"}
    print("pipeline " + p["plan_id"] + ": " + " → ".join(names[x] for x in p.get("phases", names)))
    print(f"dashboard: {_url(cfg, '/')}#/workflows/{p['plan_id']}")
    if not a.wait:
        return
    last, down_since = "", None
    while True:
        time.sleep(1)
        try:
            plan = _call(cfg, f"/api/plan?id={p['plan_id']}")
            down_since = None
        except (OSError, ValueError, SystemExit) as e:    # the server restarting or a blip: keep trying for ~30 s
            down_since = down_since or time.time()
            if time.time() - down_since > 30:
                print(f"lost contact with the Jarviee server ({_clean(e)}); the pipeline may still be running: "
                      f"{_url(cfg, '/')}#/workflows/{p['plan_id']}")
                sys.exit(1)
            continue
        pipe = plan.get("pipeline") or {}
        line = f"{pipe.get('phase', '')} (round {pipe.get('round', 0)}/{pipe.get('max_rounds', 0)})"
        if line != last:
            print(line + (f" · {_clean(pipe['note'])}" if pipe.get("note") else ""))
            last = line
        if plan["status"] in ("done", "failed", "cancelled"):
            print(f"{plan['id']}: {plan['status']}")
            sys.exit(0 if plan["status"] == "done" else 1)


def cmd_run(a, cfg):
    body = {"task": a.task, "tool": a.tool, "tier": a.tier, "project": a.project, "cwd": a.cwd}
    if a.single:
        body["single"] = True
    if a.pipeline:
        body["pipeline"] = True
    if not a.inline and server_up(cfg):
        p = _call(cfg, "/api/run", body)
        _print_plan(p)
        if p.get("pipeline"):
            return _pipeline_run(a, cfg, p)
        print(f"launched {p['id']}  →  {_url(cfg, '/')}")
        if not a.wait:
            return
        while True:
            time.sleep(1)
            agent = next((x for x in _call(cfg, "/api/state")["agents"] if x["id"] == p["id"]), None)
            if not agent or agent["status"] in FINISHED:
                break
        print(f"{p['id']}: {agent['status'] if agent else 'cleared'}")
        sys.exit(0 if agent and agent["status"] == "done" else 1)

    from hub.store import Store
    store = Store(cfg["db_path"])
    try:
        from hub.pipeline import requested
        rid, p = launcher.launch(requested(a.task)[1], cfg, store, a.tool, a.tier, a.project, a.cwd)   # no server: one agent
    except ValueError as e:
        raise SystemExit(f"error: {e}")
    _print_plan(p)
    print(f"running {rid} inline (server not running), waiting for it to finish…\n")
    try:
        while not launcher.wait(rid, 0.5):
            pass
    except KeyboardInterrupt:
        launcher.stop(rid, store)
        launcher.wait(rid, 10)
    agent = store.get(rid)
    with open(agent["log_path"], encoding="utf-8", errors="replace") as f:
        print(f.read())
    print(f"{rid}: {agent['status']} ({agent['activity']})")
    sys.exit(0 if agent["status"] == "done" else 1)


def cmd_install_hooks(a, cfg):
    from hub import install
    targets = [t for t in ("agy", "claude") if getattr(a, t)] or ["agy", "claude"]
    ok = True
    for t in targets:
        ok &= install.run(t, dry_run=a.dry_run, uninstall=a.uninstall)
    sys.exit(0 if ok else 1)


def cmd_setup(a, cfg):
    from hub import setup_wizard
    sys.exit(0 if setup_wizard.run(yes=a.yes, dry_run=a.dry_run) else 1)


def cmd_doctor(a, cfg):
    from hub import doctor
    sys.exit(0 if doctor.run() else 1)


def cmd_brain(a, cfg):
    from hub import brain_init
    sys.exit(0 if brain_init.run(a.project_dir, cfg, force=a.force) else 1)


def cmd_service(a, cfg):
    from hub import service
    if a.action == "install":
        ok = service.install(cfg, dry_run=a.dry_run, force=a.force)
    elif a.action == "uninstall":
        ok = service.uninstall(force=a.force)
    elif a.action == "status":
        ok = service.status(cfg)
    elif a.action == "restart":
        ok = service.restart()
    elif a.action == "logs":
        service.logs(lines=a.lines)
        ok = True
    else:
        ok = False
    sys.exit(0 if ok else 1)


def cmd_app(a, cfg):
    from hub import app_launcher
    if a.action == "install":
        ok = app_launcher.install(cfg, dry_run=a.dry_run)
    elif a.action == "uninstall":
        ok = app_launcher.uninstall()
    else:
        ok = False
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="start the dashboard + hook receiver on 127.0.0.1")

    def task_args(sp):
        sp.add_argument("task")
        sp.add_argument("--tool", help="agy | claude | echo (default: the dispatch table)")
        sp.add_argument("--tier", choices=["basic", "light", "complex", "heavy", "standard"], help="override the router (standard = light)")
        sp.add_argument("--project", help="workspace folder with a .brain/ (adds its notes as context)")

    c = sub.add_parser("classify", help="show the tier/model a task would get")
    task_args(c)
    c.add_argument("--json", action="store_true")

    r = sub.add_parser("run", help="launch a task on the matching model")
    task_args(r)
    r.add_argument("--cwd", help="working directory (default: the project folder)")
    r.add_argument("--wait", action="store_true", help="when the server runs it, wait until it finishes")
    r.add_argument("--inline", action="store_true", help="run here even if the server is up")
    r.add_argument("--single", action="store_true",
                   help="one agent even when the task starts with /pipeline")
    r.add_argument("--pipeline", action="store_true",
                   help="run as the researcher -> project-manager -> coder pipeline (same as starting the task with /pipeline)")

    h = sub.add_parser("install-hooks", help="merge Jarviee hooks into Antigravity / Claude Code config")
    h.add_argument("--agy", action="store_true", help="~/.gemini/config/hooks.json")
    h.add_argument("--claude", action="store_true", help="~/.claude/settings.json")
    h.add_argument("--dry-run", action="store_true", help="show the result, write nothing")
    h.add_argument("--uninstall", action="store_true", help="remove Jarviee hooks again")

    su = sub.add_parser("setup", help="first-run wizard: detect your tools, write ~/.jarviee/config.json, offer hooks")
    su.add_argument("--yes", action="store_true", help="no questions: accept the detected defaults")
    su.add_argument("--dry-run", action="store_true", help="print what would be written, change nothing")
    sub.add_parser("doctor", help="read-only health check (exit 1 when something is broken)")
    br = sub.add_parser("brain", help="project brain helpers")
    br_sub = br.add_subparsers(dest="action", required=True)
    bi = br_sub.add_parser("init", help="create a starter .brain/ in a project (never overwrites)")
    bi.add_argument("project_dir")
    bi.add_argument("--force", action="store_true", help="allow a folder outside obsidian.workspace_root")

    # service
    svc_parser = sub.add_parser("service", help="manage launchd background service")
    svc_sub = svc_parser.add_subparsers(dest="action", required=True)
    svc_install = svc_sub.add_parser("install", help="install and bootstrap launchd service")
    svc_install.add_argument("--dry-run", action="store_true", help="preview plist without installing")
    svc_install.add_argument("--force", action="store_true", help="allow installing even when HOME differs from the real home directory")
    svc_uninstall = svc_sub.add_parser("uninstall", help="bootout and remove launchd service")
    svc_uninstall.add_argument("--force", action="store_true", help="allow uninstalling even when HOME differs from the real home directory")
    svc_sub.add_parser("status", help="check launchd service and HTTP health")
    svc_sub.add_parser("restart", help="kickstart launchd service")
    svc_logs = svc_sub.add_parser("logs", help="view service output logs")
    svc_logs.add_argument("-n", "--lines", type=int, default=40, help="number of lines to show (default 40)")

    # app
    app_parser = sub.add_parser("app", help="manage the macOS Jarviee.app launcher")
    app_sub = app_parser.add_subparsers(dest="action", required=True)
    app_install = app_sub.add_parser("install", help="create ~/Applications/Jarviee.app")
    app_install.add_argument("--dry-run", action="store_true", help="preview app contents without installing")
    app_sub.add_parser("uninstall", help="remove ~/Applications/Jarviee.app")

    st = sub.add_parser("standup", help="what the agents did today (or --date YYYY-MM-DD)")
    st.add_argument("--date", default=None)

    def cmd_standup(a, cfg):
        from hub import standup
        from hub.store import Store
        print(standup.render(standup.standup(cfg, Store(cfg["db_path"]), a.date)))

    sub.add_parser("briefing", help="what's ahead: calendar, Gmail, LinkedIn, GitHub, WhatsApp (what /startwork says)")
    cg = sub.add_parser("connect-gmail", help="store a Google App Password in the Keychain for the briefing")
    cg.add_argument("address", help="your Gmail address")

    cw = sub.add_parser("connect-whatsapp", help="send summaries to your WhatsApp through CallMeBot (stores its API key in the Keychain)")
    cw.add_argument("phone", help="your WhatsApp number in international form, e.g. +639171234567")

    def cmd_connect_whatsapp(a, cfg):
        from hub import wamsg
        ok, msg = wamsg.connect(a.phone, cfg)
        print(msg + ("\nRestart Jarviee to start using it: python3 hub.py service restart" if ok else ""))
        raise SystemExit(0 if ok else 1)

    sm = sub.add_parser("summaries", help="where summaries go (08:00 schedule, /startwork, /endwork): email | whatsapp | ntfy")
    sm.add_argument("to", choices=["email", "whatsapp", "ntfy"])

    def cmd_summaries(a, cfg):
        from hub import config as hubconfig
        cfg.setdefault("email_send", {})["enabled"] = a.to == "email"
        cfg.setdefault("whatsapp_send", {})["enabled"] = a.to == "whatsapp"
        hubconfig.save_settings(cfg)
        print(f"Summaries now go to {a.to}. Restart Jarviee to apply: python3 hub.py service restart")

    def cmd_briefing(a, cfg):
        from hub import briefing
        from hub.store import Store
        b = briefing.build(cfg, Store(cfg["db_path"]))
        print(b["text"] + "\n")
        for k in b["order"]:
            print(f"{b['icons'][k]} {b['labels'][k]}")
            print("\n".join("   " + l for l in b["lines"][k]))

    def cmd_connect_gmail(a, cfg):
        from hub import briefing
        ok, msg = briefing.connect_gmail(a.address)
        print(msg)
        raise SystemExit(0 if ok else 1)

    omni_parser = sub.add_parser("omniroute", help="manage the OmniRoute fallback gateway (status, connect, test)")
    omni_sub = omni_parser.add_subparsers(dest="action", required=True)
    omni_status = omni_sub.add_parser("status", help="check OmniRoute configuration, key presence and gateway health")
    omni_status.add_argument("--json", action="store_true", help="output status as JSON")
    omni_sub.add_parser("connect", help="store the OmniRoute API key in the macOS Keychain")
    omni_sub.add_parser("test", help="run a test prompt through the OmniRoute gateway")

    def cmd_omniroute(a, cfg):
        import getpass
        import os
        import subprocess
        from hub import omniroute, runplan

        action = a.action
        if action == "status":
            st = omniroute.status(cfg)
            if getattr(a, "json", False):
                print(json.dumps(st, indent=2))
            else:
                print(f"OmniRoute: {'enabled' if st.get('enabled') else 'disabled'}")
                print(f"Base URL:  {st.get('base_url', '')}")
                print(f"API key:   {'present' if st.get('has_key') else 'missing'}")
                health_str = "healthy" if st.get("healthy") else f"unhealthy ({st.get('reason', 'unknown')})"
                print(f"Gateway:   {health_str}")
                models = st.get("models") or {}
                if models:
                    print("Combos:")
                    for lvl, combo in models.items():
                        print(f"  {lvl} -> {combo}")
            sys.exit(0 if st.get("healthy") else 1)

        elif action == "connect":
            try:
                key = getpass.getpass("OmniRoute API key: ")
            except (KeyboardInterrupt, EOFError):
                print()
                sys.exit(1)
            if not key or not key.strip():
                print("error: key cannot be empty", file=sys.stderr)
                sys.exit(1)
            try:
                omniroute.store_key(cfg, key)
                print("Key stored in Keychain.")
            except Exception as e:
                print(f"error: failed to store key: {e}", file=sys.stderr)
                sys.exit(1)
            ok, reason = omniroute.health(cfg)
            health_str = "healthy" if ok else f"unhealthy ({reason})"
            print(f"Gateway health: {health_str}")
            sys.exit(0 if ok else 1)

        elif action == "test":
            if not omniroute.api_key(cfg):
                print("error: no API key found. Run 'python3 hub.py omniroute connect' first.", file=sys.stderr)
                sys.exit(1)
            ok, reason = omniroute.health(cfg)
            if not ok:
                print(f"error: gateway health check failed ({reason})", file=sys.stderr)
                sys.exit(1)

            tcfg = (cfg.get("tools") or {}).get("omniroute")
            if not tcfg or not tcfg.get("command"):
                print("error: OmniRoute is not configured in config.json (tools.omniroute)", file=sys.stderr)
                sys.exit(1)

            try:
                env = runplan.env_for(tcfg, cfg, os.environ)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                sys.exit(1)

            prompt = "Reply with the single word OK"
            model = (tcfg.get("models") or {}).get("basic", "claude/combo/jarviee-basic")
            effort = (tcfg.get("efforts") or {}).get("basic", "")
            argv = runplan.build_argv(tcfg, prompt, model, effort)

            print(f"Sending test prompt to {model} via OmniRoute...")
            try:
                res = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)
                stdout = omniroute.redact(res.stdout or "", cfg)
                stderr = omniroute.redact(res.stderr or "", cfg)
                if stdout.strip():
                    print(f"Reply:\n{stdout.strip()}")
                if stderr.strip():
                    print(f"Stderr:\n{stderr.strip()}", file=sys.stderr)
                if res.returncode == 0 and "OK" in (res.stdout or ""):
                    print("Test successful: reply contains OK.")
                    sys.exit(0)
                else:
                    print(f"Test failed (exit code {res.returncode}).", file=sys.stderr)
                    sys.exit(1)
            except subprocess.TimeoutExpired:
                print("error: test prompt timed out after 90s", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"error: failed to run test: {e}", file=sys.stderr)
                sys.exit(1)

    from hub import cli_queue, orch_cli
    handlers = {"serve": cmd_serve, "classify": cmd_classify, "run": cmd_run,
                "install-hooks": cmd_install_hooks, "setup": cmd_setup, "doctor": cmd_doctor, "brain": cmd_brain,
                "standup": cmd_standup, "briefing": cmd_briefing, "connect-gmail": cmd_connect_gmail, "connect-whatsapp": cmd_connect_whatsapp, "summaries": cmd_summaries, "service": cmd_service, "app": cmd_app,
                "omniroute": cmd_omniroute,
                **cli_queue.add_commands(sub), **orch_cli.add_commands(sub)}
    a = ap.parse_args(argv)
    cfg = load_config()
    handlers[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
