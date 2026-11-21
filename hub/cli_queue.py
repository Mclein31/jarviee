"""CLI: hub plan / queue / pause / resume / retry / reroute."""
import json
import sys
import time
import urllib.error
import urllib.request

from . import auth, brand, planner, review

TERMINAL = ("done", "failed", "cancelled")


def _url(cfg, path):
    return f"http://127.0.0.1:{cfg['port']}{path}"


def server_up(cfg):
    try:
        urllib.request.urlopen(_url(cfg, "/api/state"), timeout=1).read()
        return True
    except Exception:
        return False


def call(cfg, path, body=None, timeout=600):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", brand.TOKEN_HEADER: auth.get_token(cfg)}
    req = urllib.request.Request(_url(cfg, path), data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        raise SystemExit(f"error: {msg}")


def _need_server(cfg):
    if not server_up(cfg):
        raise SystemExit("The queue lives in the server: start it with `python3 hub.py serve`.")


def confirm(prompt="Type 'go' to queue these tasks: "):
    try:
        return input(prompt).strip().lower() == "go"
    except EOFError:
        return False


def cmd_plan(a, cfg):
    body = {"goal": a.goal, "project": a.project or "", "planner": a.planner}
    print(f"Planning with {'the mock splitter' if a.planner == 'mock' else 'Claude (sonnet, read-only)'}…",
          file=sys.stderr)
    if a.dry_run:
        try:
            plan = planner.make_plan(a.goal, cfg, a.project, planner=a.planner)
        except planner.PlanError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(plan, indent=2) if a.json else planner.format_table(plan))
        print("\n(dry run: nothing queued)")
        return
    if server_up(cfg):
        plan = call(cfg, "/api/plan", body)
        print(plan["table"])
        if not (a.yes or confirm()):
            call(cfg, "/api/plan/discard", {"id": plan["id"]})
            print(f"Discarded {plan['id']}.")
            return
        call(cfg, "/api/plan/approve", {"id": plan["id"]})
        print(f"Queued {plan['id']} → {_url(cfg, '/')}")
        return
    _plan_inline(a, cfg)


def _plan_inline(a, cfg):
    """No server: plan, confirm, then run an in-process scheduler until the plan is finished."""
    from .scheduler import Scheduler
    from .store import Store
    from .tasks import Tasks
    store = Store(cfg["db_path"])
    tasks = Tasks(store)
    plan = planner.make_plan(a.goal, cfg, a.project, planner=a.planner)
    print(planner.format_table(plan))
    if not (a.yes or confirm()):
        print("Not queued.")
        return
    pid = tasks.add_plan(a.goal, a.project or "", plan["tasks"], planner=plan["planner"], notes=plan["notes"])
    tasks.approve(pid)
    sched = Scheduler(cfg, store, tasks, interval=0.5)
    print(f"Running {pid} inline (server not running)…")
    last = ""
    while True:
        sched.tick()
        p = tasks.plan(pid)
        line = "  ".join(f"{t['id'].split('.')[-1]}:{t['status']}" for t in p["tasks"])
        if line != last:
            print(line)
            last = line
        if all(t["status"] in TERMINAL for t in p["tasks"]):
            break
        time.sleep(0.5)
    print("\nReview:\n" + (review.run_review(cfg, tasks, pid, quota=sched.quota) or "(skipped)"))


def print_queue(q):
    print("paused" if q["paused"] else "running", "· " + " · ".join(
        f"{v['label']}: {v['running']}/{v['capacity']} running, {v['queued']} waiting" for v in q["lanes"].values()))
    for plan in q["plans"]:
        print(f"\n{plan['id']}  {plan['status']}  {plan['goal'][:70]}")
        for t in plan["tasks"]:
            flag = " ↑escalated" if t["escalated"] else ""
            print(f"  {t['id'].split('.')[-1]:<4} {t['status']:<9} {t['level']:<7} {t['tool']:<6} "
                  f"{t['title'][:44]}{flag}" + (f"  ({t['route_note']})" if t.get("route_note") else ""))


def cmd_queue(a, cfg):
    _need_server(cfg)
    q = call(cfg, "/api/state")["queue"]
    print(json.dumps(q, indent=2)) if a.json else print_queue(q)


def cmd_simple(path):
    def run(a, cfg):
        _need_server(cfg)
        body = {"id": getattr(a, "id", "")}
        if getattr(a, "tool", None):
            body["tool"] = a.tool
        r = call(cfg, path, body)
        print(json.dumps(r, indent=2) if getattr(a, "json", False) else
              (f"{r.get('id', '')} → {r.get('status', '')} on {r.get('tool', '')}" if "status" in r else r))
    return run


def add_commands(sub):
    """Register the queue commands; returns {name: handler}."""
    p = sub.add_parser("plan", help="split a goal into routed tasks and queue them after you say go")
    p.add_argument("goal")
    p.add_argument("--project", help="workspace folder with a .brain/")
    p.add_argument("--yes", action="store_true", help="queue without asking")
    p.add_argument("--dry-run", action="store_true", help="plan and print only; queue nothing")
    p.add_argument("--planner", choices=["claude", "mock"], default="claude")
    p.add_argument("--json", action="store_true")
    q = sub.add_parser("queue", help="list plans and queued tasks")
    q.add_argument("--json", action="store_true")
    sub.add_parser("pause", help="stop starting new tasks (running ones finish)")
    sub.add_parser("resume", help="start queued tasks again")
    r = sub.add_parser("retry", help="re-queue a failed, cancelled or blocked task")
    r.add_argument("id")
    rr = sub.add_parser("reroute", help="send a task to the other tool")
    rr.add_argument("id")
    rr.add_argument("--tool", required=True, choices=["claude", "agy"])
    c = sub.add_parser("cancel", help="cancel a queued or running task")
    c.add_argument("id")
    return {"plan": cmd_plan, "queue": cmd_queue, "pause": cmd_simple("/api/queue/pause"),
            "resume": cmd_simple("/api/queue/resume"), "retry": cmd_simple("/api/task/retry"),
            "reroute": cmd_simple("/api/task/reroute"), "cancel": cmd_simple("/api/task/cancel")}
