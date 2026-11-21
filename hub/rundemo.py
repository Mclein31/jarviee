"""Demo run for ?demo=1 (GET /api/run/demo/...): a plan with 5 agents across 4 phases, built through the same
code path as real data. Clearly fake: the plan is named "Demo" and every id starts with demo-."""
import itertools

_ids = itertools.count(1)


def _span(rid, kind, tool, args, start, end, status="done", parent=None, agent="claude", model=""):
    return {"id": f"demo-s{next(_ids)}", "run_id": rid, "parent": parent, "kind": kind, "agent": agent,
            "tool": tool, "args": args, "start": start, "end": end, "status": status, "model": model,
            "session": "", "call_id": ""}


def _calls(rid, t, calls, agent="claude", model="", open_last=False):
    """One model turn from t with tool calls [(tool, args, secs, status)] one after another."""
    out, cur = [], t
    turn = _span(rid, "turn", "turn", "", t, None, "running", agent=agent, model=model)
    out.append(turn)
    for i, (tool, args, secs, status) in enumerate(calls):
        last = open_last and i == len(calls) - 1
        out.append(_span(rid, "tool", tool, args, cur + 1, None if last else cur + 1 + secs,
                         "running" if last else status, turn["id"], agent, model))
        cur += 1 + secs
    if not open_last:
        turn.update(end=cur + 2, status="failed" if any(c[3] == "failed" for c in calls) else "done")
    return out


def _usage(tool, model, tin, tout, cache, cost):
    t = {"input": tin, "output": tout, "cache_read": cache, "cache_write": 0, "total": tin + tout + cache,
         "cost": cost}
    note = "" if cost is not None else "agy runs on plan quota; add a \"tool\": \"agy\" price in config pricing to estimate"
    return {"models": {model: t}, "tokens": t["total"], "cost": cost,
            "source": f"{tool} transcript (demo)", "note": note}


BRIEFS = {   # the planner's polished prompt per task: what the Tasks tab shows
    "t1": ("Read hub/store.py and hub/server.py and write down how hook events arrive.\n"
           "Propose a span model (run, turn, tool call) with start/end times and a parent link.\n"
           "Save the notes to .brain/research/spans.md.", "spans.md names every field and its source"),
    "t2": ("Measure span latency and token use per model from the recorded runs.\n"
           "Flag the slowest tool calls and the most expensive turns.", "a table of p50/p95 latency per tool"),
    "t3": ("Check every claim in the research notes against the code.\n"
           "Confirm file paths, function names and acceptance criteria exist.", "VERDICT line: PASS or FAIL with reasons"),
    "t4": ("Store hook events as spans in SQLite with an index on run_id.\n"
           "Close open spans when a run ends.\nAdd tests for nesting and a missing end.",
           "tests.test_runs_view passes"),
    "t5": ("Lay out the run tree top-down: orchestrator, phase columns, task cards.\n"
           "Route edges orthogonally with rounded corners.\nKeep the layout deterministic so polls never jump.",
           "no two cards overlap; node --check passes"),
    "t6": ("Review the combined diff read-only.\nFocus on the trace waterfall, cost totals and layout edge cases.",
           "a findings list ranked by severity"),
}


def data(now):
    t0 = now - 270 - now % 300   # the sample's clock moves: its running agents tick for five minutes, then it restarts
    p = "demo-plan"
    tasks = [
        {"id": f"{p}.t1", "name": "Researcher", "phase": "research", "title": "Research the span data model for hook events", "tool": "agy",
         "model": "gemini-3.8-flash-medium", "status": "done", "route_note": ""},
        {"id": f"{p}.t2", "name": "Data analyst", "phase": "analysis", "title": "Analyze span latency and token metrics", "tool": "agy",
         "model": "gemini-4-hydrogen-medium", "status": "done", "route_note": ""},
        {"id": f"{p}.t3", "name": "Fact-checker", "phase": "fact_check", "title": "Verify claims and acceptance criteria", "tool": "agy",
         "model": "gemini-3.8-flash-low", "status": "done", "route_note": ""},
        {"id": f"{p}.t4", "name": "Coder", "phase": "build", "title": "Store hook events as spans with a run index", "tool": "claude",
         "model": "sonnet", "status": "done", "route_note": ""},
        {"id": f"{p}.t5", "name": "Coder", "phase": "build", "title": "Draw the run tree with orthogonal SVG edges", "tool": "claude",
         "model": "sonnet", "status": "running", "route_note": ""},
        {"id": f"{p}.t6", "name": "Code reviewer", "phase": "review", "title": "Review the trace waterfall and diff", "tool": "claude",
         "model": "sonnet", "status": "blocked", "route_note": "waiting for coder"},
    ]
    for t in tasks:
        brief, acc = BRIEFS[t["id"].rsplit(".", 1)[1]]
        t.update(prompt=brief, acceptance=acc, level="complex" if t["tool"] == "claude" else "light",
                 depends_on=[f"{p}.t{int(t['id'][-1]) - 1}"] if t["id"][-1] != "1" else [])
    runs = {
        "demo-r1": {"tool": "agy", "model": "gemini-3.8-flash-medium", "status": "done", "started": t0 + 4,
                    "ended": t0 + 62, "activity": "Wrote the span model to .brain/research/spans.md",
                    "files": [".brain/research/spans.md"], "steps": 4},
        "demo-r2": {"tool": "agy", "model": "gemini-4-hydrogen-medium", "status": "done", "started": t0 + 65,
                    "ended": t0 + 108, "activity": "Reported p50/p95 latency per tool", "steps": 2},
        "demo-r3": {"tool": "agy", "model": "gemini-3.8-flash-low", "status": "done", "started": t0 + 112,
                    "ended": t0 + 135, "activity": "VERDICT: PASS"},
        "demo-r4a": {"tool": "claude", "model": "sonnet", "status": "failed", "started": t0 + 138,
                     "ended": t0 + 185, "activity": "2 tests failed: test_spans_nesting, test_missing_end"},
        "demo-r4b": {"tool": "claude", "model": "sonnet", "status": "done", "started": t0 + 190,
                     "ended": t0 + 240, "activity": "Fixed span nesting; the test suite passes",
                     "files": ["hub/spans.py", "tests/test_runs_view.py"], "steps": 3},
        "demo-r5": {"tool": "claude", "model": "sonnet", "status": "running", "started": t0 + 244,
                    "ended": None, "activity": "Running node --check on hub/static/workflows.js", "steps": 3,
                    "files": ["hub/static/workflows.js", "hub/runtree.py"]},
    }
    for rid, r in runs.items():
        r["id"] = rid
    sp = {
        "demo-r1": _calls("demo-r1", t0 + 4, [("read_file", "hub/store.py", 3, "done"),
                                              ("grep_search", "def handle_hook", 2, "done"),
                                              ("read_file", "hub/server.py", 4, "done"),
                                              ("write_file", ".brain/research/spans.md", 6, "done")],
                          "agy", "gemini-3.8-flash-medium"),
        "demo-r2": _calls("demo-r2", t0 + 65, [("read_file", "hub/runtree.py", 4, "done"),
                                               ("grep_search", "tokens", 3, "done")],
                          "agy", "gemini-4-hydrogen-medium"),
        "demo-r3": _calls("demo-r3", t0 + 112, [("read_file", "hub/spans.py", 3, "done"),
                                                ("read_file", "tests/test_runs_view.py", 4, "done")],
                          "agy", "gemini-3.8-flash-low"),
        "demo-r4a": _calls("demo-r4a", t0 + 138, [("Read", "hub/store.py", 2, "done"), ("Write", "hub/spans.py", 9, "done"),
                                                  ("Bash", "python3 -m unittest tests.test_runs_view", 18, "failed")]),
        "demo-r4b": _calls("demo-r4b", t0 + 190, [("Read", "tests/test_runs_view.py", 2, "done"),
                                                  ("Edit", "hub/spans.py", 6, "done"),
                                                  ("Bash", "python3 -m unittest tests.test_runs_view", 15, "done")]),
        "demo-r5": _calls("demo-r5", t0 + 244, [("Read", "hub/runtree.py", 3, "done"),
                                                ("Write", "hub/static/workflows.js", 14, "done"),
                                                ("Bash", "node --check hub/static/workflows.js", 0, "running")], open_last=True),
    }
    sp["demo-r5"].append(_span("demo-r5", "event", "needs_you", "Claude needs your permission to use Bash",
                               t0 + 268, t0 + 268, "needs_you", sp["demo-r5"][0]["id"]))
    grow = max(0, int(now - (t0 + 244)))   # the running coder's tokens and cost climb while it works
    usage = {"demo-r1": _usage("agy", "gemini-3.8-flash-medium", 182_400, 6_100, 1_240_000, None),
             "demo-r2": _usage("agy", "gemini-4-hydrogen-medium", 42_000, 3_200, 180_000, None),
             "demo-r3": _usage("agy", "gemini-3.8-flash-low", 31_000, 2_100, 110_000, None),
             "demo-r4a": _usage("claude", "claude-sonnet-5-5", 41_200, 9_800, 612_000, 0.3047),
             "demo-r4b": _usage("claude", "claude-sonnet-5-5", 28_900, 7_400, 455_000, 0.2318),
             "demo-r5": _usage("claude", "claude-sonnet-5-5", 36_100 + grow * 140, 12_300 + grow * 45,
                              701_500 + grow * 2_600, round(0.3367 + grow * 0.0011, 4))}
    attempts = {f"{p}.t1": ["demo-r1"], f"{p}.t2": ["demo-r2"], f"{p}.t3": ["demo-r3"],
                f"{p}.t4": ["demo-r4a", "demo-r4b"], f"{p}.t5": ["demo-r5"], f"{p}.t6": []}
    return {"kind": "plan", "id": "demo", "pipeline": {"phase": "build", "level": "complex", "round": 1, "max_rounds": 2, "research": "", "note": "Opus orchestrator: 6-work pipeline running", "rounds": [{"round": 0, "test": "skipped", "review": "fail", "note": ""}]}, "project": "agent-hub", "status": "running", "created": t0,
            "title": "Demo: 6-work pipeline (Opus orchestrator, agy + Claude)",
            "planner_model": "opus", "review_model": "sonnet", "review_enabled": True, "review": "",
            "tasks": tasks, "runs": runs, "attempts": attempts, "spans": sp, "usage": usage, "paused": False}
