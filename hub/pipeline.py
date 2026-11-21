"""Opt-in multi-agent pipeline: only a task that starts with `/pipeline` (or a run with "pipeline": true) uses it.
Every other task, whatever its level, runs as one direct agent.

research (read-only, the researcher skill) -> planning (the planner, as the project-manager skill, fed the
research) -> build (the planner's tasks, each with the coder skill). Test, review and fix rounds
(fix -> test -> review) are still available but off unless pipeline.test / pipeline.review are enabled. Every phase is a normal queued task
run by the scheduler, so it shows up on the dashboard like any other run; only the planning call is a
thread. The pipeline's state lives in plans.pipeline (JSON); `Pipeline.advance` moves it one step each
time the scheduler ticks, and does nothing while the current phase still has open tasks.
"""
import json
import re
import subprocess
import threading
from pathlib import Path

from . import dispatch, launcher, obsidian, planner, review
from .router import fit_effort, pick_effort, pick_model, rank

TERMINAL = ("done", "failed", "cancelled")
DEFAULTS = {"enabled": True, "planner": "claude",
            "research": {"enabled": True, "level": "complex"},
            "analysis": {"enabled": False, "level": "light"},
            "fact_check": {"enabled": False, "level": "light"},
            "test": {"enabled": False, "level": "complex"},
            "review": {"enabled": False}, "max_fix_rounds": 2, "research_chars": 6000,
            "skills": {"research": "researcher", "analysis": "", "plan": "project-manager",   # no analyst / fact-checker
                       "fact_check": "", "build": "coder", "review": "code-reviewer"}}         # skill exists yet
PREFIX_RE = re.compile(r"^\s*/pipeline(?![\w-])[\s:,-]*", re.I)   # "/pipeline <task>" asks for the pipeline
# "VERDICT: PASS" at the start of a line, tolerating markdown (**bold**, "> ", "- "), "=" / dashes and mixed case;
# the same words mid-line (a prompt echo) never match
VERDICT_RE = re.compile(r"(?im)^[ \t>*_`#-]*VERDICT[ \t*_`]*[:=\u2014\u2013-]+[ \t*_`]*(PASS|FAIL)\b(.*)$")
VERDICT_HINT = ("Finish with a final line that is exactly VERDICT: PASS or exactly VERDICT: FAIL, "
                "followed (for FAIL) by one line per failure.")
NOTE_CAP = 3000       # failure / finding text carried into a fix prompt
NOTES_IN_TASK = 2000  # research notes carried into each build task


def settings(cfg):
    """The pipeline config with defaults filled in."""
    pc = cfg.get("pipeline") or {}
    out = {**DEFAULTS, **pc}
    for k in ("research", "analysis", "fact_check", "test", "review", "skills"):
        out[k] = {**DEFAULTS[k], **(pc.get(k) or {})}
    return out


def requested(text, body=None):
    """(asked, task text without the /pipeline prefix). Asked: the text starts with /pipeline or body.pipeline."""
    text = str(text or "")
    m = PREFIX_RE.match(text)
    return bool(m or (body or {}).get("pipeline")), (text[m.end():].strip() if m else text)


def applies(cfg, p, body=None, text=None):
    """True when a task should run as a pipeline: only when asked for (/pipeline), never by level alone.
    `p` (the routed plan dict) is kept for callers; the level no longer decides."""
    body = body or {}
    asked, _ = requested(body.get("task", "") if text is None else text, body)
    return bool(settings(cfg)["enabled"] and asked and not body.get("single"))


def _phase_tool(cfg, phase):
    """Best-effort tool that will run `phase` according to `cfg` when no explicit tool is given."""
    pc = settings(cfg)
    if phase == "plan":
        if pc["planner"] == "mock":
            return "mock"
        return (cfg.get("planner") or {}).get("tool") or pc["planner"] or "claude"
    if phase == "review":
        return (cfg.get("review") or {}).get("tool", "claude")
    level = (pc.get(phase) or {}).get("level") or ("complex" if phase in ("research", "test") else "light")
    entry = (cfg.get("dispatch") or {}).get(level) or {}
    if entry.get("tool"):
        return entry["tool"]
    return "claude" if level in ("complex", "heavy") else "agy"


def _planner_tool(cfg, quota=None):
    pc = settings(cfg)
    if pc["planner"] == "mock":
        return "mock"
    tool = (cfg.get("planner") or {}).get("tool") or pc["planner"] or "claude"
    if cfg.get("tools") and not dispatch.tool_available(tool, cfg, quota)[0]:
        alt = dispatch.other_tool(tool, cfg)
        if dispatch.tool_available(alt, cfg, quota)[0]:
            return alt
    return tool


def skill_line(cfg, phase, tool=None, skill_dirs=None):
    """'Use the <skill> skill.' for a phase when installed for the phase's tool, else ''."""
    name = (settings(cfg)["skills"].get(phase) or "").strip()
    if not name:
        return ""
    target = tool or _phase_tool(cfg, phase)
    if not review.skill_installed(name, tool=target, skill_dirs=skill_dirs):
        return ""
    return f"Use the {name} skill."


def phase_names(cfg):
    pc = settings(cfg)
    return ([n for n in ("research", "analysis") if pc[n]["enabled"]] + ["plan", "build"]
            + [n for n in ("fact_check", "test", "review") if pc[n]["enabled"]])


def parse_verdict(text):
    """(verdict, detail): the last 'VERDICT: PASS|FAIL' line in the text, or ('unknown', '')."""
    last = None
    for m in VERDICT_RE.finditer(text or ""):
        last = m
    if not last:
        return "unknown", ""
    detail = (last.group(2).strip(" :-\t") + "\n" + text[last.end():]).strip()
    return last.group(1).lower(), detail


def _git_head(cfg, project):
    """The project's HEAD commit when the pipeline starts (the review diffs from it), or ''."""
    proj = obsidian.find_project(cfg, project) if project else None
    if not proj or not (proj / ".git").exists():
        return ""
    try:
        r = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _trim(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


class Pipeline:
    def __init__(self, cfg, tasks, sched):
        self.cfg, self.tasks, self.sched = cfg, tasks, sched
        self._plock = threading.RLock()
        self._threads = {}      # plan id -> its planner thread

    # ---- starting ----
    def start(self, goal, project, p, cwd="", tool=""):
        """Create the pipeline plan and queue its first phase. Returns the plan dict.
        `cwd` is where project-less agents run; `tool` pins research, build, test and fix to one tool."""
        with self._plock:
            return self._start(goal, project, p, cwd, tool)

    def _start(self, goal, project, p, cwd, tool):
        if project and not obsidian.find_project(self.cfg, project):
            raise ValueError(f"unknown project '{project}' (no {project}/.brain/ in the workspace)")
        cwd = "" if project else (cwd or "")
        if cwd and not Path(cwd).is_dir():
            raise ValueError(f"working directory not found: {cwd}")
        pc = settings(self.cfg)
        research = pc["research"]["enabled"]
        analysis = pc["analysis"]["enabled"]
        first_phase = "research" if research else ("analysis" if analysis else "planning")
        state = {"phase": first_phase, "level": p.get("level") or "complex",
                 "base": _git_head(self.cfg, project), "round": 0, "max_rounds": int(pc["max_fix_rounds"]),
                 "research": "", "analysis": "", "rounds": [],
                 "cwd": cwd, "tool": tool or "", "note": "started"}
        pid = self.tasks.add_plan(goal, project or "", [], planner="pipeline", status="approved", pipeline=state)
        if research:
            prompt = ("Research this goal before anything is built. Read the code and notes; do NOT edit any file.\n"
                      f"GOAL: {goal}\n\nReport: the files and modules involved, existing patterns to follow, "
                      "how to run the project's tests (exact command), risks and unknowns. Be concrete and brief.")
            task = self._task(
                "research", f"Research: {goal[:50]}", prompt, "research", pc["research"]["level"], read_only=True,
                tool=tool or None)
            line = skill_line(self.cfg, "research", tool=task["tool"])
            if line:
                task["prompt"] += "\n\n" + line
            self.tasks.add_tasks(pid, project or "", [task])
        elif analysis:
            self._queue_analysis(self.tasks.plan(pid), "")
        else:
            self._plan_async(pid, goal, project or "", "", "")
        return self.tasks.plan(pid)

    def _task(self, tid, title, prompt, phase, level, read_only=False, tool=None, model=None):
        r = dispatch.route(prompt, self.cfg, level=level, tool=tool, model=model,
                           quota=self.sched.quota if self.sched else None,
                           store=self.sched.store if self.sched else None)
        return {"id": tid, "title": title[:80], "prompt": prompt, "level": r["level"], "planner_level": level,
                "router_level": r["suggested_level"], "tool": r["tool"], "model": r["model"],
                "effort": r["effort"], "route_note": r["fallback"], "phase": phase, "read_only": read_only}

    # ---- state ----
    def _set(self, pid, plan_status=None, **changes):
        with self._plock:
            plan = self.tasks.plan(pid)
            if not plan or plan["status"] == "cancelled":
                return None
            state = dict(plan["pipeline"] or {}, **changes)
            fields = {"pipeline": state}
            if plan_status:
                fields["status"] = plan_status
            self.tasks.set_plan(pid, **fields)
            return state

    def _fail(self, pid, note):
        self._set(pid, "failed", phase="failed", note=note)

    # ---- advancing ----
    def advance(self, plan):
        with self._plock:
            plan = self.tasks.plan(plan["id"])
            if not plan or not plan.get("pipeline") or plan["status"] not in ("approved", "running"):
                return
            p = plan["pipeline"]
            if plan["status"] == "approved" and any(t["status"] == "running" for t in plan["tasks"]):
                self.tasks.set_plan(plan["id"], status="running")
            if p.get("phase") == "planning":
                return self._resume_planning(plan, p)
            handler = {"research": self._after_research, "analysis": self._after_analysis,
                       "build": self._after_build, "fact_check": self._after_fact_check,
                       "test": self._after_test, "review": self._after_review,
                       "fix": self._after_fix}.get(p.get("phase"))
            if handler:
                handler(plan, p)

    @staticmethod
    def _by_id(plan, name):
        tid = f"{plan['id']}.{name}"
        return next((t for t in plan["tasks"] if t["id"] == tid), None)

    def _settled(self, plan, name):
        """(settled, task): a missing task counts as settled."""
        t = self._by_id(plan, name)
        return (t is None or t["status"] in TERMINAL), t

    def _log(self, t, n):
        text = self.sched._log_tail(t["run_id"], n) if t and t.get("run_id") else ""
        return launcher.strip_tty(text)

    def _after_research(self, plan, p):
        ok, t = self._settled(plan, "research")
        if not ok:
            return
        pc = settings(self.cfg)
        if t and t["status"] == "done":
            text = _trim(self._log(t, int(pc["research_chars"])), int(pc["research_chars"]))
            note = "research done"
        else:
            text, note = "", "research failed; continuing without it"
        if pc["analysis"]["enabled"]:
            if self._set(plan["id"], phase="analysis", research=text, note=note + "; analyzing") is None:
                return
            self._queue_analysis(plan, text)
            return
        if self._set(plan["id"], phase="planning", research=text, note=note + "; planning") is None:
            return    # cancelled meanwhile
        self._plan_async(plan["id"], plan["goal"], plan["project"], text, "")

    def _queue_analysis(self, plan, research_text):
        pc = settings(self.cfg)
        tool = (plan.get("pipeline") or {}).get("tool") or None
        prompt = ("Analyze the goal, requirements, data structures, and metrics before planning. Read-only; do NOT edit any file.\n"
                  f"GOAL: {plan['goal']}")
        if research_text:
            prompt += f"\n\nResearch notes:\n{_trim(research_text, NOTES_IN_TASK)}"
        task = self._task(
            "analysis", f"Data analysis: {plan['goal'][:45]}", prompt, "analysis", pc["analysis"]["level"],
            read_only=True, tool=tool)
        line = skill_line(self.cfg, "analysis", tool=task["tool"])
        if line:
            task["prompt"] += "\n\n" + line
        self.tasks.add_tasks(plan["id"], plan["project"], [task])

    def _after_analysis(self, plan, p):
        ok, t = self._settled(plan, "analysis")
        if not ok:
            return
        pc = settings(self.cfg)
        if t and t["status"] == "done":
            text = _trim(self._log(t, int(pc["research_chars"])), int(pc["research_chars"]))
            note = "analysis done; planning"
        else:
            text, note = "", "analysis failed; planning without it"
        if self._set(plan["id"], phase="planning", analysis=text, note=note) is None:
            return
        self._plan_async(plan["id"], plan["goal"], plan["project"], p.get("research") or "", text)

    def _plan_async(self, pid, goal, project, research, analysis=""):
        th = threading.Thread(target=self._plan_worker, args=(pid, goal, project, research, analysis), daemon=True,
                              name=f"jarviee-pipeline-{pid}")
        self._threads[pid] = th
        th.start()

    def _resume_planning(self, plan, p):
        """Phase 'planning' with no planner thread (the server restarted): start it again, twice at most."""
        th = self._threads.get(plan["id"])
        if th and th.is_alive():
            return
        n = int(p.get("planning_restarts") or 0)
        if n >= 2:
            return self._fail(plan["id"], "planning stalled and was restarted twice; giving up")
        self._set(plan["id"], planning_restarts=n + 1, note=f"planner thread was gone; restarting (attempt {n + 1})")
        self._plan_async(plan["id"], plan["goal"], plan["project"], p.get("research") or "", p.get("analysis") or "")

    def _plan_worker(self, pid, goal, project, research, analysis=""):
        pc = settings(self.cfg)
        try:
            kind = pc["planner"]
            ask = goal
            if kind != "mock":    # the mock splitter would cut the added text into bogus tasks
                ask = goal + (f"\n\nResearch notes from a read-only research agent:\n{research}" if research else "")
                if analysis:
                    ask += f"\n\nData analysis notes:\n{analysis}"
                if pc["fact_check"]["enabled"] or pc["test"]["enabled"] or pc["review"]["enabled"]:
                    ask += ("\n\nSeparate verification/review agents run after the build; "
                            "keep build tasks focused on implementation and assign levels honestly so basic/light work routes to agy and complex/heavy work routes to claude.")
                plan_tool = _planner_tool(self.cfg, quota=self.sched.quota if self.sched else None)
                plan_skill = skill_line(self.cfg, "plan", tool=plan_tool)
                if plan_skill:
                    ask += (f"\n\n{plan_skill} Break the goal down the way it would, but your "
                            "answer must still be only the JSON array described above.")
            cur = self.tasks.plan(pid)
            if not cur:
                return
            cwd, tool = (cur["pipeline"] or {}).get("cwd") or None, (cur["pipeline"] or {}).get("tool") or ""
            made = planner.make_plan(ask, self.cfg, project or None, planner=kind,
                                     quota=self.sched.quota if self.sched else None, cwd=cwd)
            cur = self.tasks.plan(pid)
            if not cur or cur["status"] in ("cancelled", "done", "failed"):
                return
            existing = {t["id"].split(".", 1)[1] for t in cur["tasks"]}
            rows = self._build_rows(made["tasks"], existing, research)
            if tool:    # a pinned tool: the planner's routing is overridden, models picked by level
                for r in rows:
                    m = pick_model(tool, r["level"], self.cfg)
                    m, eff = fit_effort(tool, m, pick_effort(r["prompt"], tool, r["level"], self.cfg, brief=False, model=m)[0],
                                        self.cfg)
                    r.update(tool=tool, model=m, effort=eff, route_note=f"pinned to {tool}")
            for r in rows:
                line = skill_line(self.cfg, "build", tool=r.get("tool"))
                if line:
                    r["prompt"] += "\n\n" + line
            self.tasks.add_tasks(pid, project, rows)
            if made.get("notes"):
                self.tasks.set_plan(pid, notes=json.dumps(made["notes"]))
            self._set(pid, phase="build", note=f"planned {len(rows)} build task(s) via {made['planner']}")
        except Exception as e:     # PlanError or anything else: the pipeline stops, the plan says why
            self._fail(pid, f"planning failed: {e}")

    @staticmethod
    def _build_rows(tasks, existing, research, skill=""):
        """Give planner tasks the build phase, unique ids and a trimmed research section."""
        ids, used = {}, set(existing)
        for t in tasks:
            new, n = t["id"], 2
            while new in used:
                new, n = f"{t['id']}-{n}", n + 1
            used.add(new)
            ids[t["id"]] = new
        rows = []
        for t in tasks:
            prompt = t["prompt"]
            if research:
                prompt += "\n\nResearch notes (from a read-only research agent):\n" + _trim(research, NOTES_IN_TASK)
            if skill:
                prompt += "\n\n" + skill
            rows.append({**t, "id": ids[t["id"]], "prompt": prompt, "phase": "build", "read_only": False,
                         "depends_on": [ids.get(d, d) for d in t.get("depends_on", [])]})
        return rows

    def _after_build(self, plan, p):
        builds = {t["id"]: t for t in plan["tasks"] if t["phase"] == "build"}
        # a task that can never run (a dependency failed or was cancelled) is cancelled, not waited on
        dead = {i for i, t in builds.items() if t["status"] in ("failed", "cancelled")}
        changed = True
        while changed:
            changed = False
            for i, t in builds.items():
                if i not in dead and t["status"] in ("queued", "blocked", "draft", "waiting") \
                        and any(d in dead or d not in builds for d in t["depends_on"]):
                    dead.add(i)
                    self.tasks.set(i, status="cancelled", route_note="skipped: dependency failed")
                    t["status"] = "cancelled"
                    changed = True
        if any(t["status"] not in TERMINAL for t in builds.values()):
            return
        if not any(t["status"] == "done" for t in builds.values()):
            return self._fail(plan["id"], "no build task finished")
        self._queue_checks(plan, p, "build finished")

    def _queue_checks(self, plan, p, why):
        """Start fact-checking (or test / review when disabled, or finish when all are off)."""
        pc = settings(self.cfg)
        rnd = p["round"]
        if pc["fact_check"]["enabled"]:
            self.tasks.add_tasks(plan["id"], plan["project"], [self._fact_check_task(plan, p, rnd)])
            self._set(plan["id"], phase="fact_check", note=f"{why}; fact-checking (round {rnd})")
        else:
            self._queue_after_fact_check(plan, p, "skipped", "", why)

    def _fact_check_task(self, plan, p, rnd):
        pc = settings(self.cfg)
        builds = [t for t in plan["tasks"] if t["phase"] == "build" and t["status"] == "done"]
        lines = "\n".join(f"- {t['title']}: {t['acceptance'] or '(no acceptance criteria)'}" for t in builds)
        prompt = (f"You are the fact-checker for this goal (fact-check round {rnd}):\n{plan['goal']}\n\n"
                  f"Completed build tasks and acceptance criteria:\n{lines}\n\n"
                  "Verify all claims, file paths, symbols, behavior, and acceptance criteria against the actual codebase. "
                  "Read-only; do NOT edit any file. FAIL if any claim or criterion is false or unverified.\n\n")
        task = self._task(f"fact_check-{rnd}", f"Fact-check (round {rnd}): {plan['goal'][:40]}", prompt, "fact_check",
                          pc["fact_check"]["level"], read_only=True, tool=p.get("tool") or None)
        line = skill_line(self.cfg, "fact_check", tool=task["tool"])
        if line:
            task["prompt"] += line + "\n\n"
        task["prompt"] += VERDICT_HINT
        return task

    def _after_fact_check(self, plan, p):
        ok, t = self._settled(plan, f"fact_check-{p['round']}")
        if not ok:
            return
        if t and t["status"] == "done":
            verdict, detail = parse_verdict(self._log(t, 6000))
            if verdict == "fail" and not detail:
                detail = _trim(self._log(t, 1500), 1500)
        else:
            verdict, detail = "fail", "the fact-checker did not finish: " + ((t or {}).get("route_note") or "no result")
        self._queue_after_fact_check(plan, p, verdict, detail, f"fact-check {verdict}")

    def _queue_after_fact_check(self, plan, p, fc_v, fc_detail, why):
        pc = settings(self.cfg)
        rnd = p["round"]
        state = self._set(plan["id"], _fact_check={"verdict": fc_v, "detail": _trim(fc_detail, NOTE_CAP)})
        if state is None:
            return
        if pc["test"]["enabled"]:
            self.tasks.add_tasks(plan["id"], plan["project"], [self._test_task(plan, state, rnd)])
            self._set(plan["id"], phase="test", note=f"{why}; testing (round {rnd})")
        else:
            self._queue_review(plan, state, "skipped", "", why)

    def _test_task(self, plan, p, rnd):
        pc = settings(self.cfg)
        builds = [t for t in plan["tasks"] if t["phase"] == "build" and t["status"] == "done"]
        lines = "\n".join(f"- {t['title']}: {t['acceptance'] or '(no acceptance criteria)'}" for t in builds)
        hints = f"\n\nResearch notes (see the test command here):\n{_trim(p.get('research'), 1500)}" if p.get("research") else ""
        prompt = (f"You are the test agent for this goal (test round {rnd}):\n{plan['goal']}\n\n"
                  f"Build tasks and their acceptance criteria:\n{lines}{hints}\n\n"
                  "Run the project's existing test suite. Add or update tests that check each acceptance criterion. "
                  "Do not change non-test code; if a test exposes a real bug, leave it failing and report it.\n\n"
                  f"{VERDICT_HINT}")
        return self._task(f"test-{rnd}", f"Test (round {rnd}): {plan['goal'][:40]}", prompt, "test",
                          pc["test"]["level"], tool=p.get("tool") or None)

    def _after_test(self, plan, p):
        ok, t = self._settled(plan, f"test-{p['round']}")
        if not ok:
            return
        if t and t["status"] == "done":
            verdict, detail = parse_verdict(self._log(t, 6000))
            if verdict == "fail" and not detail:
                detail = _trim(self._log(t, 1500), 1500)
        else:
            verdict, detail = "fail", "the test agent did not finish: " + ((t or {}).get("route_note") or "no result")
        self._queue_review(plan, p, verdict, detail, f"tests {verdict}")

    def _queue_review(self, plan, p, test_v, test_detail, why):
        pc = settings(self.cfg)
        state = self._set(plan["id"], _test={"verdict": test_v, "detail": _trim(test_detail, NOTE_CAP)})
        if state is None:
            return
        if not pc["review"]["enabled"]:
            return self._decide(plan, state, "skipped", "")
        rnd = p["round"]
        work = {**plan, "base": p.get("base"), "tasks": [t for t in plan["tasks"] if t["phase"] in ("build", "fix")]}
        rtool, _ = review.review_tool(self.cfg, self.sched.quota if self.sched else None)
        prompt = (review.review_prompt(self.cfg, work, tool=rtool) + "\n\nThis is a read-only review. FAIL means at least one "
                  f"critical or high severity finding; lower severities do not fail it. Review round {rnd}.\n\n"
                  + VERDICT_HINT)
        self.tasks.add_tasks(plan["id"], plan["project"], [self._review_task(plan, f"review-{rnd}", prompt)])
        self._set(plan["id"], phase="review", note=f"{why}; reviewing (round {rnd})")

    def _review_task(self, plan, tid, prompt):
        rc = self.cfg.get("review", {})
        rnd = tid.split("-")[1]
        return self._task(tid, f"Review (round {rnd}): {plan['goal'][:40]}", prompt, "review", "complex",
                          read_only=True, tool=rc.get("tool", "claude"), model=rc.get("model", "sonnet"))

    def _after_review(self, plan, p):
        name = f"review-{p['round']}"
        ok, t = self._settled(plan, name)
        if not ok:
            return
        if t and t["status"] != "done":     # the review itself did not run: one retry, then stop
            rt = self._by_id(plan, name + "-retry")
            if rt is None:
                self.tasks.add_tasks(plan["id"], plan["project"], [self._review_task(plan, name + "-retry", t["prompt"])])
                self._set(plan["id"], note=f"review {t['status']}; retrying once")
                return
            if rt["status"] not in TERMINAL:
                return
            t = rt
            if rt["status"] != "done":
                self.tasks.set_plan(plan["id"], review="(the review agent did not finish)")
                return self._fail(plan["id"], "review could not run; changes are unreviewed")
        if t and t["status"] == "done":
            text = self._log(t, 6000)
            verdict, detail = parse_verdict(text)
            self.tasks.set_plan(plan["id"], review=_trim(text, 6000))
            if verdict == "fail" and not detail:
                detail = _trim(text, 1500)
        else:
            verdict, detail = "unknown", ""
        self._decide(plan, p, verdict, detail)

    def _decide(self, plan, p, review_v, review_detail):
        pid, rnd = plan["id"], p["round"]
        test = p.get("_test") or {"verdict": "skipped", "detail": ""}
        fc = p.get("_fact_check") or {"verdict": "skipped", "detail": ""}
        good = ("pass", "unknown", "skipped")
        entry = {"round": rnd, "test": test["verdict"], "review": review_v,
                 "note": "no verdict line found" if "unknown" in (fc["verdict"], test["verdict"], review_v) else ""}
        if fc["verdict"] != "skipped":
            entry["fact_check"] = fc["verdict"]
        rounds = list(p.get("rounds") or []) + [entry]
        if fc["verdict"] in good and test["verdict"] in good and review_v in good:
            return self._set(pid, "done", phase="done", rounds=rounds, _test=None, _fact_check=None,
                             note=f"finished after {rnd} fix round(s)" if rnd else "finished")
        if rnd < p["max_rounds"]:
            nxt = rnd + 1
            level = max(p["level"], "complex", key=rank)
            parts = ["Fix only the problems named below; do not widen the change.", f"GOAL: {plan['goal']}"]
            if fc["verdict"] == "fail":
                parts.append("Fact-check findings:\n" + _trim(fc["detail"] or "(no details)", NOTE_CAP))
            if test["verdict"] == "fail":
                parts.append("Test failures:\n" + (test["detail"] or "(no details)"))
            if review_v == "fail":
                parts.append("Review findings:\n" + _trim(review_detail, NOTE_CAP))
            task = self._task(
                f"fix-{nxt}", f"Fix (round {nxt}): {plan['goal'][:40]}", "\n\n".join(parts), "fix", level,
                tool=p.get("tool") or None)
            line = skill_line(self.cfg, "build", tool=task["tool"])
            if line:
                parts.insert(2, line)
                task["prompt"] = "\n\n".join(parts)
            self.tasks.add_tasks(pid, plan["project"], [task])
            return self._set(pid, phase="fix", round=nxt, rounds=rounds, _test=None, _fact_check=None,
                             note=f"round {rnd} failed; fixing (round {nxt})")
        self._set(pid, "failed", phase="failed", rounds=rounds, _test=None, _fact_check=None,
                  note=f"still failing after {p['max_rounds']} fix rounds")

    def _after_fix(self, plan, p):
        ok, _ = self._settled(plan, f"fix-{p['round']}")
        if ok:
            self._queue_checks(plan, p, "fix finished")
