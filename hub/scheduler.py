"""The queue runner: two lanes (agy, claude), dependencies, a file-overlap guard, escalation.

A task starts when every dependency is done, its lane has capacity and none of its files are
claimed by a running task (across both lanes). Runs go through launcher.launch, so they show up
on the dashboard like any other run.
"""
import threading
import time
from collections import Counter
from pathlib import Path

from . import (coord, detector, dispatch, fallback, finaltext, guards, launcher, obsidian, orphans, pipeline, usage,
               tasks as tasklocks)
from .router import fit_effort, pick_effort, pick_model

TERMINAL = ("done", "failed", "cancelled")
SETTLED_RUN = ("done", "failed", "stopped")   # a run row in one of these has ended
LOST_CALLBACK_S = 30    # a launched task whose run ended this long ago but never called back is settled anyway
LOCK_SWEEP_S = 30       # how often leftover lock files are looked for


def overlap(a, b):
    """Files two tasks share; a directory claim covers everything under it."""
    out = set()
    for x in a:
        for y in b:
            xs, ys = x.rstrip("/"), y.rstrip("/")
            if xs == ys or xs.startswith(ys + "/") or ys.startswith(xs + "/"):
                out.add(x)
    return out


def compose_prompt(t, forbidden):
    parts = [t["prompt"].strip()]
    if t.get("acceptance"):
        parts.append(f"Acceptance criteria: {t['acceptance']}")
    if t.get("files"):
        parts.append("Files you will likely change: " + ", ".join(t["files"]))
    if forbidden:
        parts.append("Do NOT modify these files; other agents are working on them right now: "
                     + ", ".join(sorted(forbidden)))
    if t.get("phase") in ("test", "review"):
        parts.append("Stay in scope: do only this task.")
    elif t.get("phase") == "research":
        parts.append("Stay in scope: research only, change nothing. Finish with your findings.")
    else:
        parts.append("Stay in scope: do only this task. When done, summarise what changed in 5 lines.")
    return "\n\n".join(parts)


def _alive(pid, started=None):
    """The pid is a live process and, when its launch-time start is known, the same one (hub/orphans.py)."""
    return orphans.alive(pid, started)


class Scheduler:
    def __init__(self, cfg, store, tasks, interval=1.0):
        self.cfg, self.store, self.tasks, self.interval = cfg, store, tasks, interval
        self.paused = False
        self.paused_reason = ""       # why: "paused by you" or a run-guard ceiling (hub/guards.py)
        self._ceiling_ack = None      # {"day", "runs", "cost"} when the owner last resumed after a ceiling
        self.quota = {}               # tool -> phrase that showed its quota is out
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.on_plan_done = None      # callback(plan_id), set by the server for the review pass
        self._quota_notified = False
        self._live = set()            # task ids whose run this process launched (callback will report)
        self._next_sweep = 0.0
        self.pipeline = pipeline.Pipeline(cfg, tasks, self)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarviee-scheduler")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:     # never let one bad row kill the loop
                print(f"[scheduler] {e!r}")
            self._stop.wait(self.interval)

    # ---- one pass ----
    def tick(self):
        with self.lock:
            self._reconcile()
            if not self.paused:
                self._check_ceiling()
            if not self.paused:
                self._start_ready()
            self._close_plans()

    def _reconcile(self):
        """Settle `running` tasks whose run ended while no callback was listening (service restart, or a worker
        that died): finished run -> finished(); dead process -> run row failed, lock released, task failed or
        re-queued; live process -> left alone. Also clears lock files nothing owns any more."""
        coord.sweep(self.store)   # claims of agents that are gone
        now = time.time()
        for t in self.tasks.list(statuses=["running"]):
            run = self.store.get(t["run_id"]) if t.get("run_id") else None
            if run and run.get("status") in SETTLED_RUN:
                if t["id"] in self._live and now - float(run.get("ended") or now) < LOST_CALLBACK_S:
                    continue   # the worker is about to call back
                self.finished(t["id"], t["run_id"], run["status"], 0 if run["status"] == "done" else 1)
                continue
            if t["id"] in self._live:
                continue
            if run and run.get("status") == "running" and orphans.run_alive(run):
                continue
            tasklocks.release(self.cfg, t["project"], t["id"])
            if run:
                if run.get("status") in ("running", "waiting"):   # nothing will ever update this row again
                    orphans.settle_lost(self.store, run, now)     # failed, never done: the shared rule
                self.finished(t["id"], t["run_id"], "failed", 1)
            else:
                self.tasks.set(t["id"], status="queued", route_note="requeued: its run was lost in a restart")
        if now >= self._next_sweep:
            self._next_sweep = now + LOCK_SWEEP_S
            self._sweep_locks()

    def _sweep_locks(self):
        for project in self.tasks.projects():
            try:
                tasklocks.sweep(self.cfg, project, self.tasks.idle_ids(project))
            except OSError as e:
                print(f"[scheduler] lock sweep {project}: {e!r}")

    def _check_ceiling(self):
        """Pause the queue when today's queue runs or estimated Claude spend pass the configured ceiling.
        Nothing but `resume()` (the owner) lifts it, and a resume allows another full allowance for that day."""
        max_runs, max_cost = guards.queue_limits(self.cfg)
        if not (max_runs or max_cost):
            return
        midnight = usage.local_midnight()
        runs = guards.day_runs(self.tasks.list(), midnight)
        cost = guards.claude_cost_today(usage.cached(self.cfg), midnight) if max_cost else None
        ack = self._ceiling_ack if (self._ceiling_ack or {}).get("day") == midnight else {}
        why = guards.ceiling_reason(self.cfg, runs, cost, ack.get("runs", 0), ack.get("cost", 0.0))
        if why:
            self.paused, self.paused_reason = True, why

    def _start_ready(self):
        everything = {t["id"]: t for t in self.tasks.list()}
        running = [t for t in everything.values() if t["status"] == "running"]
        load = Counter(t["tool"] for t in running)
        claimed = {f: t["id"] for t in running for f in t["files"]}
        lanes = self.cfg.get("lanes") or {}
        for t in everything.values():
            if t["status"] not in ("queued", "blocked", "waiting"):
                continue
            state, is_pipe = self.tasks.plan_state(t["plan_id"]) if t.get("plan_id") else ("", False)
            if state == "cancelled" or (is_pipe and state in ("done", "failed")):
                self.tasks.set(t["id"], status="cancelled", route_note="plan cancelled", ended=time.time())
                continue
            # If waiting for quota, check if quota has auto-restored
            if t["status"] == "waiting":
                cand_tool = t.get("tool") or "agy"
                cand_model = t.get("model") or ""
                q = self.store.get_quota(cand_tool, cand_model)
                if q.get("status") == "ok":
                    self._mark(t, "queued", "quota restored")
                else:
                    continue
            deps = [everything.get(d) for d in t["depends_on"]]
            bad = [d["id"] for d in deps if d and d["status"] in ("failed", "cancelled")]
            if bad or any(d is None for d in deps):
                self._mark(t, "blocked", f"dependency {', '.join(bad) or 'missing'} did not finish")
                continue
            waiting = [d["id"] for d in deps if d["status"] != "done"]
            if waiting:
                self._mark(t, "blocked", "waiting for " + ", ".join(w.split(".")[-1] for w in waiting))
                continue
            tool = self._usable_tool(t)
            if not tool or t["status"] == "waiting":
                continue
            if t.get("read_only") and not launcher.readonly_args(tool):   # never run a read-only task writable
                alt = next((x for x in ("claude", "agy")
                            if dispatch.tool_available(x, self.cfg, self.quota, store=self.store)[0]), None)
                if not alt:
                    self.tasks.set(t["id"], status="failed", ended=time.time(),
                                   route_note=f"{tool} has no read-only mode and neither claude nor agy is available")
                    continue
                model = pick_model(alt, t["level"], self.cfg)
                note = f"read-only task: {tool} has no read-only mode; routed to {alt}"
                self.tasks.set(t["id"], tool=alt, model=model, route_note=note)
                t.update(tool=alt, model=model, route_note=note)
                tool = alt
            # Unlisted fallback tools (e.g. omniroute) run in an implicit capacity-1 lane rather than stalling.
            lane_cap = lanes[tool] if tool in lanes and lanes[tool] is not None else 1
            if load[tool] >= lane_cap:
                self._mark(t, "queued", f"{tool} lane full")
                continue
            clash = overlap(t["files"], claimed)
            if clash:
                owner = claimed.get(next(iter(clash)), "")
                self._mark(t, "queued", f"waiting: {', '.join(sorted(clash))} claimed by {owner.split('.')[-1]}")
                continue
            forbidden = set(claimed) - set(t["files"])
            self.launch(t, tool, forbidden)
            load[tool] += 1
            claimed.update({f: t["id"] for f in t["files"]})

    def _mark(self, t, status, note):
        if t["status"] != status or t.get("route_note") != note:
            self.tasks.set(t["id"], status=status, route_note=note)

    def _usable_tool(self, t):
        """Fallback: a missing binary or exhausted quota sends the task to the other tool."""
        cur_tool = t["tool"]
        cur_model = t.get("model") or pick_model(cur_tool, t["level"], self.cfg)
        ok, why = dispatch.tool_available(cur_tool, self.cfg, self.quota, store=self.store, model=cur_model)
        if ok:
            return cur_tool

        fb_res = fallback.resolve_fallback(cur_tool, cur_model, self.cfg, store=self.store,
                                           hops=int(t.get("escalated") or 0), original_level=t["level"])
        if fb_res and fb_res.get("tool"):
            alt = fb_res["tool"]
            alt_model = fb_res["model"]
            alt_level = fb_res.get("level") or t["level"]
            via = " (use available tool)" if "use available tool" in fb_res.get("why", "") else ""
            note = f"fallback: {why}; routed to {alt}:{alt_model}{via}"
            self.tasks.set(t["id"], tool=alt, model=alt_model, level=alt_level, route_note=note)
            t.update(tool=alt, model=alt_model, level=alt_level)
            return alt
        elif fb_res and fb_res.get("waiting"):
            until_ts = fb_res.get("earliest_until")
            secs = int(until_ts - time.time()) if until_ts else 0
            h, m = divmod(secs // 60, 60)
            d, h = divmod(h, 24)
            t_str = f"{d}d {h}h" if d else f"{h}m" if m else f"{secs}s"
            self._mark(t, "waiting", f"waiting for quota (resets in {t_str})")
            if not getattr(self, "_quota_notified", False):
                self._quota_notified = True
            return None

        alt = dispatch.other_tool(cur_tool, self.cfg)
        if not dispatch.tool_available(alt, self.cfg, self.quota, store=self.store)[0]:
            return cur_tool           # neither works; let the run fail and say why
        model = pick_model(alt, t["level"], self.cfg)
        self.tasks.set(t["id"], tool=alt, model=model, route_note=f"fallback: {why}; routed to {alt}")
        t.update(tool=alt, model=model)
        return alt

    # ---- running one task ----
    def launch(self, t, tool, forbidden):
        effort = pick_effort(t["prompt"], tool, t["level"], self.cfg, brief=False, model=t["model"])[0]
        model, effort = fit_effort(tool, t["model"], effort, self.cfg)   # an agy slug carries the routed effort
        p = {"tool": tool, "tier": t["level"], "suggested_tier": t.get("router_level") or t["level"],
             "level": t["level"], "model": model, "effort": effort, "project": t["project"],
             "reasons": [f"plan task {t['id']}"] + ([t["route_note"]] if t.get("route_note") else [])}
        if t.get("read_only"):
            p["read_only"] = True
        if t.get("files"):
            p["files"] = list(t["files"])   # the claim it registers (hub/coord.py)
        cwd = None
        if t.get("phase") and t.get("plan_id"):    # a pipeline run without a project runs where it was asked to
            cwd = ((self.tasks.plan(t["plan_id"]) or {}).get("pipeline") or {}).get("cwd") or None
        self.tasks.set(t["id"], status="running", attempts=t["attempts"] + 1, started=time.time(),
                       ended=None, route_note=t.get("route_note") or "")
        tasklocks.claim(self.cfg, t["project"], t["id"], t["files"])
        try:
            rid, _ = launcher.launch(t["title"], self.cfg, self.store, project=t["project"] or None, cwd=cwd, p=p,
                                     prompt=compose_prompt(t, forbidden), log_runs=False,
                                     on_finish=lambda r, s, c, tid=t["id"]: self.finished(tid, r, s, c))
        except ValueError as e:
            tasklocks.release(self.cfg, t["project"], t["id"])
            self.tasks.set(t["id"], status="failed", route_note=str(e), ended=time.time())
            return None
        self.tasks.set(t["id"], run_id=rid)
        self._live.add(t["id"])
        return rid

    def _log_tail(self, run_id, n=None):
        a = self.store.get(run_id) or {}
        path = a.get("log_path")
        if not path or not Path(path).exists():
            return ""
        text = finaltext.after_mark(Path(path).read_text(encoding="utf-8", errors="replace"))   # a TUI run: its answer
        return text[-(n or self.cfg.get("escalation", {}).get("log_tail_chars", 4000)):]

    def finished(self, tid, run_id, status, code):
        with self.lock:
            self._live.discard(tid)
            t = self.tasks.get(tid)
            if not t:
                return
            tasklocks.release(self.cfg, t["project"], tid)
            log = self._log_tail(run_id)
            hit = dispatch.quota_hit(log, self.cfg)
            if hit:
                self.quota[t["tool"]] = hit
            summary = "\n".join([ln for ln in log.strip().splitlines() if ln.strip()][-5:])
            row = self.store.get(run_id) or {}
            closed_after_turn = row.get("activity") == launcher.FINISHED_CLOSED   # a TUI run's pane closed when done
            if t["status"] == "cancelled" or status == "stopped" or (
                    launcher.closed_by_user(self.store, run_id) and not closed_after_turn):
                self.tasks.set(tid, status="cancelled", ended=time.time(), summary=summary)
                self._record(t, "cancelled")
                return

            cap_note = (self.store.get(run_id) or {}).get("activity")
            if status == "failed" and guards.is_cap_note(cap_note):
                # the wall-clock cap is the run's own limit: no escalation, retry or fallback because of it
                self.tasks.set(tid, status="failed", ended=time.time(), summary=summary, route_note=cap_note)
                self._record(t, "failed")
                return

            det = detector.detect_failure(t["tool"], code, log)
            if det:
                if det.kind == detector.QUOTA:
                    if det.scope == "tool":
                        self.store.set_tool_quota(t["tool"], "exhausted", until=det.reset_time,
                                                  last_error=det.message, cfg=self.cfg)
                    else:
                        self.store.set_quota(t["tool"], t.get("model", "*"), "exhausted",
                                             until=det.reset_time, last_error=det.message)
                elif det.kind == detector.AUTH:
                    self.store.set_tool_quota(t["tool"], "exhausted", until=time.time() + 3600,
                                              last_error=det.message, cfg=self.cfg)
                elif det.kind == detector.TRANSIENT:
                    self.store.set_quota(t["tool"], t.get("model", "*"), "transient",
                                         until=det.reset_time, last_error=det.message)

                if det.kind in (detector.QUOTA, detector.AUTH, detector.TRANSIENT) and int(t["escalated"]) < 2:
                    fb = fallback.resolve_fallback(t["tool"], t.get("model") or "", self.cfg,
                                                   store=self.store, hops=int(t["escalated"]),
                                                   original_level=t["level"],
                                                   is_model_only_quota=(det.scope == "model"))
                    if fb and fb.get("tool"):
                        alt_tool, alt_model = fb["tool"], fb["model"]
                        alt_lvl = fb.get("level") or t["level"]
                        note = f"fallback: {det.message}; routed to {alt_tool}:{alt_model}"
                        self._record(t, "escalated")
                        self.tasks.set(tid, status="queued", tool=alt_tool, model=alt_model,
                                       level=alt_lvl, escalated=int(t["escalated"]) + 1,
                                       route_note=note, summary=summary)
                        return
                    elif fb and fb.get("waiting"):
                        until_ts = fb.get("earliest_until")
                        secs = int(until_ts - time.time()) if until_ts else 0
                        h, m = divmod(secs // 60, 60)
                        d, h = divmod(h, 24)
                        t_str = f"{d}d {h}h" if d else f"{h}m" if m else f"{secs}s"
                        self.tasks.set(tid, status="waiting",
                                       route_note=f"waiting for quota (resets in {t_str})", summary=summary)
                        return

            # Task failure: keep existing escalation rule
            reason = dispatch.escalation_reason(t["tool"], code, log, self.cfg, int(t["escalated"]))
            esc_tool = self.cfg.get("escalation", {}).get("to_tool", "claude")
            if reason and dispatch.tool_available(esc_tool, self.cfg, self.quota)[0]:
                esc = self.cfg.get("escalation", {})
                to_tool, to_level = esc_tool, esc.get("to_level", "complex")
                level = max(to_level, t["level"], key=["basic", "light", "complex", "heavy"].index)
                model = self.cfg["dispatch"].get(level, {}).get("model") or pick_model(to_tool, level, self.cfg)
                prompt = (t["prompt"] + f"\n\nA previous attempt with {t['tool']} did not finish ({reason}). "
                          f"Its log tail, for context:\n<<<\n{log[-2000:]}\n>>>")
                self._record(t, "escalated")
                self.tasks.set(tid, status="queued", tool=to_tool, level=level, model=model, prompt=prompt,
                               escalated=1, route_note=f"escalated: {reason}", summary=summary)
                return
            if hit and status != "done" and not int(t["escalated"]):
                alt = dispatch.other_tool(t["tool"], self.cfg)
                if dispatch.tool_available(alt, self.cfg, self.quota)[0]:
                    model = pick_model(alt, t["level"], self.cfg)
                    note = f"fallback: {t['tool']} quota exhausted ({hit}); routed to {alt}"
                    self._record(t, "escalated")
                    self.tasks.set(tid, status="queued", tool=alt, model=model,
                                   escalated=1, route_note=note, summary=summary)
                    return
            final = "done" if status == "done" else "failed"
            note = t.get("route_note", "") if final == "done" else f"exit code {code}"
            self.tasks.set(tid, status=final, ended=time.time(), summary=summary, route_note=note)
            self._record(t, final)
            if final == "done" and t.get("project") and not t.get("read_only"):
                proj_dir = obsidian.find_project(self.cfg, t["project"])
                run_row = self.store.get(run_id) or {}
                if proj_dir and not run_row.get("worktree_path"):   # a worktree run was committed on its own branch
                    launcher.auto_commit(str(proj_dir), t["title"], log_path=run_row.get("log_path"),
                                         files=run_row.get("files") or None)

    def _record(self, t, result):
        """One line per attempt in <project>/.brain/AGENT_RUNS.md."""
        if not t["project"]:
            return
        label = result + (" · escalated" if t["escalated"] and result != "escalated" else "")
        try:
            obsidian.log_run(self.cfg, {"project": t["project"], "tool": t["tool"], "model": t["model"],
                                        "tier": t["level"], "status": label, "task": f"[{t['id']}] {t['title']}"})
        except OSError:
            pass

    # ---- controls ----
    def pause(self):
        self.paused = True
        self.paused_reason = "paused by you"

    def resume(self):
        if self.paused_reason.startswith("queue ceiling"):   # the owner accepts the spend so far today
            midnight = usage.local_midnight()
            cost = guards.claude_cost_today(usage.cached(self.cfg), midnight)
            self._ceiling_ack = {"day": midnight, "runs": guards.day_runs(self.tasks.list(), midnight),
                                 "cost": cost or 0.0}
        self.paused, self.paused_reason = False, ""

    def cancel(self, tid):
        t = self._need(tid)
        if t["status"] == "running" and t.get("run_id"):
            self.tasks.set(tid, status="cancelled")
            launcher.stop(t["run_id"], self.store)
        elif t["status"] not in TERMINAL:
            self.tasks.set(tid, status="cancelled", ended=time.time())
        return self.tasks.get(tid)

    def retry(self, tid):
        t = self._need(tid)
        if t["status"] not in ("failed", "cancelled", "blocked"):
            raise ValueError(f"task {tid} is {t['status']}; only failed, cancelled or blocked tasks can be retried")
        self.tasks.set(tid, status="queued", route_note="retried by you", ended=None)
        return self.tasks.get(tid)

    def reroute(self, tid, tool):
        t = self._need(tid)
        if tool not in self.cfg["tools"]:
            raise ValueError(f"unknown tool '{tool}'")
        if t["status"] == "running":
            raise ValueError(f"task {tid} is running; cancel it first")
        status = "queued" if t["status"] in TERMINAL else t["status"]
        self.tasks.set(tid, tool=tool, model=pick_model(tool, t["level"], self.cfg), status=status,
                       route_note=f"rerouted to {tool} by you", ended=None if status == "queued" else t["ended"])
        return self.tasks.get(tid)

    def _need(self, tid):
        t = self.tasks.get(tid)
        if not t:
            raise ValueError(f"no task '{tid}'")
        return t

    def _close_plans(self):
        for plan in self.tasks.open_plans():   # every open plan, so an old pipeline keeps advancing
            if plan.get("pipeline"):      # a pipeline closes itself (its review is a real task)
                if plan["status"] in ("approved", "running"):
                    self.pipeline.advance(plan)
                continue
            if plan["status"] not in ("approved", "running") or not plan["tasks"]:
                continue
            states = [t["status"] for t in plan["tasks"]]
            if all(s in TERMINAL for s in states):
                self.tasks.set_plan(plan["id"], status="done" if all(s == "done" for s in states) else "failed")
                if self.on_plan_done:
                    threading.Thread(target=self.on_plan_done, args=(plan["id"],), daemon=True).start()
            elif any(s == "running" for s in states) and plan["status"] == "approved":
                self.tasks.set_plan(plan["id"], status="running")
