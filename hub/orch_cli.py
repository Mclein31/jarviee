"""CLI for the /orchestrate lead: hub.py orch assign | wait | result | review | status (hub/orch.py).
The lead's run id is JARVIEE_RUN_ID, which the hub sets for every agent it launches."""
import json
import os

from . import cli_queue


def _lead(a):
    lead = getattr(a, "lead", None) or os.environ.get("JARVIEE_RUN_ID") or ""
    if not lead:
        raise SystemExit("error: no orchestration: run this from the orchestrator session (or pass --lead <run id>)")
    return lead


def _show_worker(w):
    files = ", ".join(w.get("files") or []) or "none recorded"
    out = [f"worker {w['n']}: {w['status']} · {w['tool']} {w['model']} · load {w['load']}"
           + (f" · review {w['review']}" if w.get("review") else ""),
           f"  job: {w['task'][:200]}", f"  files: {files}"]
    if w.get("summary"):
        out.append("  report:\n    " + w["summary"].replace("\n", "\n    "))
    return "\n".join(out)


def _footer(r):
    bits = [f"running: {r['running'] or 'none'}", f"free windows: {r['free_windows']}",
            f"to review: {r['to_review'] or 'none'}"]
    if r.get("all_accepted"):
        bits.append("ALL ACCEPTED")
    return " | ".join(bits)


def cmd_orch(a, cfg):
    lead, act = _lead(a), a.action
    if act == "assign":
        r = cli_queue.call(cfg, "/api/orch/assign", {
            "lead": lead, "task": a.job, "load": a.load or "", "tool": a.tool or "", "model": a.model or "",
            "project": a.project or "", "files": a.files or "", "redo": a.redo or 0, "note": a.note or ""})
        print(f"worker {r['worker']} started on {r['tool']} {r['model']} (load {r['load']}, run {r['run_id']}); "
              f"free windows: {r['free_windows']}" + (f"\nrerouted: {r['fallback']}" if r.get("fallback") else ""))
    elif act == "wait":
        r = cli_queue.call(cfg, "/api/orch/wait", {"lead": lead, "timeout": a.timeout}, timeout=a.timeout + 30)
        if r.get("finished"):
            print("\n\n".join(_show_worker(w) for w in r["finished"]))
        elif r.get("idle"):
            print("no worker is running.")
        else:
            print("nothing finished yet: call wait again.")
        print(_footer(r))
    elif act == "status":
        r = cli_queue.call(cfg, "/api/orch/status", {"lead": lead})
        print("\n".join(f"worker {w['n']}: {w['status']} · {w['tool']} {w['model']} · {w['load']}"
                        + (f" · {w['review']}" if w["review"] else "") + f" · {w['task'][:90]}" for w in r["workers"])
              or "no workers yet.")
        print(_footer(r))
    elif act == "result":
        print(_show_worker(cli_queue.call(cfg, "/api/orch/result", {"lead": lead, "worker": a.n})))
    elif act == "review":
        r = cli_queue.call(cfg, "/api/orch/review", {"lead": lead, "worker": a.n, "verdict": a.verdict, "note": a.note or ""})
        print(f"worker {a.n}: {a.verdict}ed" if a.verdict == "accept" else f"worker {a.n}: marked for rework")
        print(_footer(r) + (f"\n{r['hint']}" if r.get("hint") else ""))
    if getattr(a, "json", False):
        print(json.dumps(r, indent=2))


def add_commands(sub):
    """Register `orch ...`; returns {name: handler}."""
    o = sub.add_parser("orch", help="the /orchestrate lead's team commands")
    sp = o.add_subparsers(dest="action", required=True)
    for name in ("assign", "wait", "status", "result", "review"):
        s = sp.add_parser(name)
        s.add_argument("--lead", help="the orchestrator's run id (default: $JARVIEE_RUN_ID)")
        s.add_argument("--json", action="store_true")
        if name == "assign":
            s.add_argument("job")
            s.add_argument("--load", choices=["basic", "light", "complex", "heavy"])
            s.add_argument("--tool", choices=["agy", "claude"])
            s.add_argument("--model")
            s.add_argument("--project")
            s.add_argument("--files", help="comma-separated files the job touches (claims)")
            s.add_argument("--redo", type=int, help="re-assign worker N's job")
            s.add_argument("--note", help="what was wrong with the earlier attempt")
        elif name == "wait":
            s.add_argument("--timeout", type=float, default=540.0)
        elif name == "result":
            s.add_argument("n", type=int)
        elif name == "review":
            s.add_argument("n", type=int)
            s.add_argument("verdict", choices=["accept", "rework"])
            s.add_argument("note", nargs="?")
    return {"orch": cmd_orch}
