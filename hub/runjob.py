"""The worker of one launched run: wait its turn, run the agent (pane or headless), then decide what the exit means.

`RunJob.run` is the whole story in order; each numbered step is a method with its inputs on the job:

  0. `_open_worktree`  opt-in (hub/worktrees.py): a Claude run that would wait works in its own git worktree instead
  1. `_wait_turn`      hold back while another agent owns the same files (hub/coord.py)
  2. `_begin`          remember the files and git state before the run
  3. `_execute`        iTerm2 pane (`panerun.run_in_pane`) or a headless subprocess (`_run_headless`)
  4. `_bridge_retry`   agy printed nothing on non-TTY stdout: run it again through the headless bridge
  5. `_decide`         exit code + log → (status, note); quota/auth failures may start a fallback run
  6. `_maybe_retry`    a genuine failure of an interactive run starts the same run again (up to retry.max)
  7. `_finalize`       write the final row, brain line and commit; call `on_finish`

Collaborators are looked up through `hub.launcher` at call time, so patching `launcher.<name>` still works.
"""
import shutil
import subprocess
import threading
import time

from . import claudejson, coord, detector, fallback, finaltext, guards, orphans, outcome, runplan, runstate, snapshot, worktrees

__all__ = ["RunJob"]


def _quota_wait_phrase(until_ts):
    secs = int(until_ts - time.time()) if until_ts else 0
    h, m = divmod(secs // 60, 60)
    d, h = divmod(h, 24)
    t_str = f"{d}d {h}h" if d else f"{h}m" if m else f"{secs}s"
    return f"waiting for quota (resets in {t_str})"


class RunJob:
    def __init__(self, *, run_id, task, cfg, store, p, prompt, argv_for, workdir, log_path, env, tracker, co,
                 title, term, ro, on_finish, log_runs, hops, fallback_from, retries):
        self.run_id, self.task, self.cfg, self.store = run_id, task, cfg, store
        self.p, self.prompt, self.argv_for = p, prompt, argv_for
        self.argv = argv_for(prompt)
        self.workdir, self.log_path, self.env, self.tracker = workdir, log_path, env, tracker
        self.co, self.title, self.term, self.ro = co, title, term, ro
        self.on_finish, self.log_runs, self.hops = on_finish, log_runs, hops
        self.fallback_from, self.retries = fallback_from, retries
        self.reported = False   # on_finish has been called
        self.start_time = 0.0
        self.git_before = ""
        self.snap_before = None
        self.project_dir = workdir   # the project's checkout; `workdir` is the worktree while a worktree run is going
        self.wt = None               # {path, workdir, branch, base, top} of this run's worktree (hub/worktrees.py)
        self.capped = ""        # the wall-clock cap note once the watchdog fired (hub/guards.py)
        self._cap_timer = None
        self._cap_off = True
        self._cap_lock = threading.Lock()

    def run_guarded(self):
        """The thread target: `run`, but a crash still ends the run and reports it, so a queue task never waits
        for a callback that will not come."""
        try:
            self.run()
        except Exception as e:
            print(f"[launcher] {self.run_id}: worker crashed: {e!r}")
            runstate.PROCS.pop(self.run_id, None)
            if self.reported:
                return
            self._wt_close("failed")
            try:
                runstate.launcher()._finish(self.run_id, "failed", f"launcher error: {str(e)[:150]}", self.p,
                                            self.task, self.cfg, self.store, log_runs=False, auto_git=False)
            except Exception as e2:
                print(f"[launcher] {self.run_id}: could not record the crash: {e2!r}")
            self._notify("failed", None)

    def _notify(self, status, code):
        """Call `on_finish` (the scheduler's callback) once."""
        if self.on_finish and not self.reported:
            self.reported = True
            self.on_finish(self.run_id, status, code)

    # ---- the story ----
    def run(self):
        L = runstate.launcher()
        self._open_worktree()
        if self.wt is None and self.co["decision"] == "wait" and not self._wait_turn():
            L._finish(self.run_id, "stopped", "stopped by you", self.p, self.task, self.cfg, self.store,
                      log_runs=False, auto_git=False)
            self._notify("stopped", None)
            return
        self._begin()
        code, start_err = self._execute()
        self._scrub_log_secret()
        if start_err:
            self._wt_close("failed")
            L._finish(self.run_id, "failed", start_err, self.p, self.task, self.cfg, self.store, self.log_runs,
                      workdir=self.workdir, log_path=self.log_path)
            self._notify("failed", 127)
            return
        code, used_bridge, empty_output = self._bridge_retry(code)
        self._scrub_log_secret()
        if self.capped:   # the cap fired during the bridge retry: the same end as in the first attempt
            self._wt_close("failed")
            L._finish(self.run_id, "failed", self.capped, self.p, self.task, self.cfg, self.store, self.log_runs,
                      workdir=self.workdir, log_path=self.log_path)
            self._notify("failed", 127)
            return
        status, note, relaunched = self._decide(code, used_bridge, empty_output)
        if relaunched:
            return
        files = snapshot.changed(self.snap_before, snapshot.take(self.workdir), self.workdir)
        if (status == "done" and self.p["tool"] == "agy" and not self.ro and not files
                and L.agy_soft_denied(self.start_time)):
            status, note = "failed", "agy refused a shell command in print mode and changed no files"
        if files:
            self.store.upsert(self.run_id, files=files)
        status, note, retried = self._maybe_retry(status, note, code, files)
        if retried:
            return
        self._finalize(status, note, code, files)

    # ---- 0. a worktree instead of waiting ----
    def _open_worktree(self):
        """Opt-in: eligible Claude runs work in `<jarviee>/worktrees/<run-id>` on branch `jarviee/<run-id>`. When it
        cannot be created the run goes on as before (and waits, if it has to)."""
        live = self.on_finish is None and bool(self.term) and self._live_in_pane()   # queue runs end after one turn
        if not self.workdir or not worktrees.eligible(self.cfg, self.p, self.co["decision"], self.ro, live, self.argv):
            return
        wt = worktrees.create(self.cfg, self.run_id, self.workdir)
        if not wt:
            return
        self.wt, self.workdir = wt, wt["workdir"]
        self.store.upsert(self.run_id, worktree_path=wt["path"], worktree_branch=wt["branch"],
                          activity=f"working in worktree {wt['branch']}")

    def _wt_close(self, status):
        """Finish the worktree once: commit its files on the branch, bring them back if safe, remove the folder.
        Returns the sentence to add to the run's note ('' when the work arrived or there is nothing to say)."""
        wt, self.wt = self.wt, None
        if wt is None:
            return ""
        try:
            res = worktrees.finish(self.cfg, wt, self.task, status, log_path=self.log_path)
        except Exception as e:   # a worktree problem must never lose or hang the run's result
            print(f"[worktree] {self.run_id}: {e!r}")
            res = {"result": "error", "note": f"worktree cleanup failed: {str(e)[:100]}", "sha": ""}
        self.store.upsert(self.run_id, worktree_result=res["result"], worktree_note=res["note"])
        return worktrees.describe(res, wt["branch"])

    # ---- 1. wait for the files ----
    def _wait_turn(self):
        """A run that overlaps an active agent starts when that agent releases its claim, with its hand-off."""
        L = runstate.launcher()
        store, run_id, co = self.store, self.run_id, self.co
        waiting_on = [c["agent_id"] for c in co["with"]]
        store.upsert(run_id, activity=f"waiting for {', '.join(waiting_on)}: {co['note']}"[:120])
        n = 0
        while coord.still_blocking(store, waiting_on):
            if L.closed_by_user(store, run_id):
                return False
            time.sleep(0.5)
            n += 1
            if n % 10 == 0:
                coord.sweep(store)   # a blocker that died without finishing must not hold this run forever
        note = coord.handoff_note(store, waiting_on)
        if note:
            self.prompt = self.prompt + "\n\n" + note
            self.argv = self.argv_for(self.prompt)
        store.upsert(run_id, activity="starting")
        return True

    # ---- 2. before ----
    def _begin(self):
        L = runstate.launcher()
        self.start_time = time.time()
        self.git_before = L._git_status(self.workdir)
        self.snap_before = snapshot.take(self.workdir)
        if self.snap_before and self.snap_before["head"]:
            self.store.upsert(self.run_id, base_head=self.snap_before["head"])

    # ---- 3. run it ----
    def _execute(self):
        """(exit code, start error) with the wall-clock cap around it: a capped run ends as a start error, which
        `run` records as failed with no retry, bridge or fallback."""
        try:
            code, start_err = self._execute_once()
        finally:
            self._disarm_cap()
        if self.capped:
            return None, self.capped
        return code, start_err

    def _arm_cap(self):
        """Start the watchdog for this attempt (headless runs only; live pane sessions are never capped). Every
        attempt (the first run, the bridge retry) gets its own clock."""
        secs, note = guards.wall_clock(self.cfg, self.p)
        with self._cap_lock:
            if self._cap_timer is not None:
                self._cap_timer.cancel()
            self._cap_timer = None
            self._cap_off = not secs
            if secs and not self.capped:
                self._start_cap_timer(secs, note)

    def _start_cap_timer(self, secs, note):   # caller holds _cap_lock
        self._cap_timer = threading.Timer(secs, self._cap_fired, args=(note,))
        self._cap_timer.daemon = True
        self._cap_timer.start()

    def _disarm_cap(self):
        """The attempt is over: from here on the watchdog can no longer mark the run capped."""
        with self._cap_lock:
            self._cap_off = True
            if self._cap_timer is not None:
                self._cap_timer.cancel()

    def _cap_fired(self, note):
        """Stop the process the way a user stop does, but without `closed_by_user`, so it ends as failed. The decision
        (capped, or already exited / disarmed) is made under the lock, so a normal exit is never reported as capped."""
        with self._cap_lock:
            if self._cap_off:
                return
            proc = runstate.PROCS.get(self.run_id)
            if proc is None:   # not registered yet (pane still opening) or already gone: look again shortly
                self._start_cap_timer(2.0, note)
                return
            poll = getattr(proc, "poll", None)
            if poll is not None and poll() is not None:   # it exited on its own just before the cap
                self._cap_off = True
                return
            self.capped = note
            self._cap_off = True
        self.store.upsert(self.run_id, activity=note)
        try:
            proc.terminate()
        except OSError:
            pass
        killer = threading.Timer(10.0, lambda: getattr(proc, "kill", lambda: None)())
        killer.daemon = True
        killer.start()

    def _execute_once(self):
        """In an iTerm2 pane when one is open, else a background subprocess."""
        L = runstate.launcher()
        if self.term:   # iTerm2 pane; falls back to the background when the pane can't open
            unattended = self.on_finish is not None   # a queue / pipeline run: nobody replies, a finished turn ends it
            live = self._live_in_pane()
            if not live or unattended:   # a queue run keeps its wall-clock cap in the TUI too
                self._arm_cap()
            code, start_err, in_pane = L._run_in_pane(self.run_id, self.term, self.cfg, self.store, self.tracker,
                                                      self.p, self.title,
                                                      self.argv if live else guards.headless_argv(self.cfg, self.p, self.argv),
                                                      self.workdir, self.env,
                                                      self.log_path, interactive=live, unattended=unattended)
            if in_pane:
                if live:   # the TUI's screen output is no report: copy the clean answer from the agent's transcript
                    finaltext.append(self.log_path,
                                     finaltext.final_text(self.p["tool"], self.argv, self.prompt, self.start_time))
                return code, start_err
        return self._run_headless(runplan.headless_json_argv(self.argv, self.p["tool"], self.cfg), "working")

    def _live_in_pane(self):
        """The real claude / agy TUI in the pane (terminal.interactive). Queue and pipeline runs too, unless
        terminal.queue_interactive is off: then they run as one-shot print runs, which show nothing until they end."""
        t = self.cfg.get("terminal") or {}
        if not t.get("interactive", True):
            return False
        return self.on_finish is None or bool(t.get("queue_interactive", True))

    def _run_headless(self, cmd_argv, activity_label="working"):
        """One subprocess with its output in the run's log; progress is observed every 0.2 s while it runs."""
        L = runstate.launcher()
        store, run_id, tracker = self.store, self.run_id, self.tracker
        ret_code = None
        cmd_argv = list(guards.headless_argv(self.cfg, self.p, cmd_argv))   # claude print runs: budget / turn ceilings
        if cmd_argv:
            path = self.env.get("PATH") if isinstance(self.env, dict) and "PATH" in self.env else None
            resolved = shutil.which(cmd_argv[0], path=path) if path is not None else shutil.which(cmd_argv[0])
            if resolved:
                cmd_argv[0] = resolved
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.flush()
            try:
                proc = subprocess.Popen(cmd_argv, stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, cwd=self.workdir, env=self.env)
            except OSError as e:
                log.write(f"\ncould not start '{cmd_argv[0]}': {e}\n")
                return None, f"{cmd_argv[0]} could not start"
            runstate.PROCS[run_id] = proc
            store.upsert(run_id, pid=proc.pid, pid_start=orphans.proc_start(proc.pid, fresh=True), activity=activity_label)
            self._arm_cap()
            while ret_code is None:
                try:
                    ret_code = proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    L._observe_log_steps(run_id, self.log_path, store, tracker, running=True)
                    cur_row = store.get(run_id)
                    if cur_row:
                        L.sync_run_progress(cur_row, store)
        self._disarm_cap()   # exited: the watchdog can no longer claim this run
        runstate.PROCS.pop(run_id, None)
        L._observe_log_steps(run_id, self.log_path, store, tracker, running=False)
        cur_row = store.get(run_id)
        if cur_row:
            L.sync_run_progress(cur_row, store)
            if int(cur_row.get("steps") or 0) > 0:
                tracker["has_output"] = True
        return ret_code, ""

    def _log_append(self, text):
        try:
            with open(self.log_path, "a", encoding="utf-8") as log:
                log.write(text)
        except OSError:
            pass

    def _log_text(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _scrub_log_secret(self):
        """Never leave an env_from secret in the run log if a child process echoed it."""
        tcfg = (self.cfg.get("tools") or {}).get(self.p["tool"]) or {}
        if tcfg.get("env_from") != "omniroute":
            return
        secret = (self.env or {}).get("ANTHROPIC_AUTH_TOKEN")
        if not secret or not self.log_path:
            return
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            if secret in text:
                self.log_path.write_text(text.replace(secret, "***"), encoding="utf-8")
        except OSError:
            pass

    # ---- 4. agy printed nothing ----
    def _bridge_retry(self, code):
        """(code, used_bridge, empty_output). agy -p prints nothing on non-TTY stdout (upstream bug #76):
        run it once more through the headless bridge when one is configured."""
        L = runstate.launcher()
        p, store, run_id = self.p, self.store, self.run_id
        used_bridge = False
        empty_output = False
        cur = store.get(run_id)
        if (cur and cur["status"] == "stopped") or p["tool"] != "agy" or code != 0 or self.tracker["has_output"]:
            return code, used_bridge, empty_output
        tool_cfg = self.cfg["tools"][p["tool"]]
        bridge_argv = L.build_bridge_argv(tool_cfg, self.prompt, p["model"], p["effort"], read_only=self.ro)
        if bridge_argv:
            if self.ro:
                bridge_argv += L.readonly_args(p["tool"])
            self._log_append("\n$ agy-headless-bridge (fallback: agy -p produced no output on non-TTY stdout)\n\n")
            b_code, b_err = self._run_headless(bridge_argv, "retrying via agy-headless-bridge")
            if not b_err:
                code = b_code
                used_bridge = True
        cur = store.get(run_id)
        if (not (cur and cur["status"] == "stopped") and code == 0
                and not self.tracker["has_output"] and int((cur or {}).get("steps") or 0) == 0):
            bridge_name = (tool_cfg.get("bridge_command") or ["agy-headless-bridge"])[0]
            why = "also produced no output" if used_bridge else "not found on PATH"
            self._log_append(f"\n[agy] warning: no output produced on non-TTY stdout (upstream bug #76; {bridge_name} {why})\n")
            empty_output = True
        return code, used_bridge, empty_output

    # ---- 5. what the exit means ----
    def _decide(self, code, used_bridge, empty_output):
        """(status, note, relaunched). `relaunched` means a fallback run took over and this one is finished."""
        L = runstate.launcher()
        store, run_id = self.store, self.run_id
        cur = store.get(run_id)
        if (cur or {}).get("activity") == runstate.FINISHED_CLOSED:
            return "done", runstate.FINISHED_CLOSED, False
        if L.closed_by_user(store, run_id):
            act = str(cur.get("activity") or "")
            return "stopped", (act if act.startswith("stopped (") else "stopped by you"), False
        if (cur or {}).get("interactive") == runstate.LIVE_UNATTENDED and cur.get("status") == "failed":
            # a queue run's TUI turn ended on an error and its pane closed it (the exit code is the close signal's)
            status, note, relaunched = self._decide_failure(code or 1)
            return status, str(cur.get("activity") or note), relaunched
        res = claudejson.settle(self.log_path, self.cfg) if claudejson.enabled(self.cfg, self.p["tool"]) else None
        if self.p["tool"] == "claude":
            hit = guards.limit_note(self.cfg, self.p, res, self._log_text() if code != 0 or res is None else "")
            if hit and (code != 0 or res is not None):   # its own ceiling: failed, never retried or handed on
                return "failed", hit, False
        if code != 0:
            return self._decide_failure(code)
        if res is not None:   # a structured result: an error result is a failure even on exit 0
            kind, why = claudejson.verdict(res)
            if kind == outcome.FAILED:
                return "failed", why, False
        if empty_output:
            note = "finished (no output produced)"
        elif used_bridge:
            note = "finished (via agy-headless-bridge)"
        else:
            note = "finished"
        status = "done"
        if self.on_finish is None:   # exit 0 is not proof: a question or block means it needs you
            # (queue runs are headless, nobody can answer; the scheduler's own escalation handles them)
            kind, why = outcome.classify(code, self._log_text(), empty_output=empty_output,
                                         extra_blocked=(self.cfg.get("escalation") or {}).get("blocked_patterns", []))
            if res is not None and claudejson.verdict(res)[0] == outcome.NEEDS_YOU:
                kind, why = claudejson.verdict(res)   # denied tool calls are a real blocker, whatever the text says
            if kind == outcome.NEEDS_YOU:
                status, note = "waiting", f"needs you: {why}"
        return status, note, False

    def _decide_failure(self, code):
        """A non-zero exit: record a quota / auth / transient problem, and hand over to another model if allowed."""
        L = runstate.launcher()
        p, store, cfg = self.p, self.store, self.cfg
        status, note = "failed", f"exit code {code}"
        det = detector.detect_failure(p["tool"], code, self._log_text())
        if not det:
            return status, note, False
        if det.kind == detector.QUOTA:
            if det.scope == "tool":
                store.set_tool_quota(p["tool"], "exhausted", until=det.reset_time, last_error=det.message, cfg=cfg)
            else:
                store.set_quota(p["tool"], p["model"], "exhausted", until=det.reset_time, last_error=det.message)
        elif det.kind == detector.AUTH:
            store.set_tool_quota(p["tool"], "exhausted", until=time.time() + 3600, last_error=det.message, cfg=cfg)
        elif det.kind == detector.TRANSIENT:
            store.set_quota(p["tool"], p["model"], "transient", until=det.reset_time, last_error=det.message)
        if (self.on_finish is None or p.get("orchestrate")) and cfg.get("fallback", {}).get("enabled", True) \
                and det.kind in (detector.QUOTA, detector.AUTH, detector.TRANSIENT):
            fb_res = fallback.resolve_fallback(
                p["tool"], p["model"], cfg, store=store, hops=self.hops,
                original_level=p.get("tier"), is_model_only_quota=(det.scope == "model"))
            if fb_res and fb_res.get("tool") and not L.closed_by_user(store, self.run_id):
                self._relaunch_on(fb_res, code)
                return status, note, True
            if fb_res and fb_res.get("waiting"):
                return "waiting", _quota_wait_phrase(fb_res.get("earliest_until")), False
        return status, note, False

    def _relaunch_on(self, fb_res, code):
        """Finish this run as failed and start the same task on the fallback tool / model."""
        L = runstate.launcher()
        p = self.p
        touched = self.git_before != L._git_status(self.workdir)
        next_prompt = self.prompt
        self._wt_close("failed")
        if time.time() - self.start_time > 30.0 or touched:
            next_prompt = ("A previous attempt on another model may have partially changed files. "
                           "Check git status/diff first and continue from there.\n\n") + self.prompt
        next_p = dict(p, tool=fb_res["tool"], model=fb_res["model"], tier=fb_res["level"], effort=fb_res["effort"],
                      fallback_from=f"{p['tool']}:{p['model']}")
        note = f"exit code {code} (fallback to {fb_res['tool']}:{fb_res['model']})"
        L._finish(self.run_id, "failed", note, p, self.task, self.cfg, self.store, log_runs=False,
                  workdir=self.workdir, log_path=self.log_path, auto_git=False)
        L.launch(self.task, self.cfg, self.store, project=p.get("project"), cwd=self.project_dir,
                 p=next_p, prompt=next_prompt, on_finish=self.on_finish, log_runs=self.log_runs,
                 parent_run_id=self.run_id, hops=self.hops + 1, fallback_from=f"{p['tool']}:{p['model']}")

    # ---- 6. try again ----
    def _maybe_retry(self, status, note, code, files):
        """(status, note, retried). A genuine failure of a non-queue run starts the same run again."""
        L = runstate.launcher()
        rcfg = self.cfg.get("retry") or {}
        max_retries = int(rcfg.get("max", 3))
        if not (status == "failed" and (self.on_finish is None or self.p.get("orchestrate")) and rcfg.get("enabled", True)
                and not guards.is_cap_note(note)
                and not L.closed_by_user(self.store, self.run_id)
                and code not in ((None, 0, 127) if rcfg.get("retry_timeouts") else (None, 0, 127, 124))):
            return status, note, False
        if self.retries < max_retries and not L.closed_by_user(self.store, self.run_id):
            n = self.retries + 1
            self._wt_close("failed")
            L._finish(self.run_id, "failed", f"retrying ({n}/{max_retries}): {note}", self.p, self.task, self.cfg,
                      self.store, log_runs=False, workdir=self.workdir, log_path=self.log_path, auto_git=False,
                      files=files)
            retry_prompt = self.prompt
            if files:
                retry_prompt = ("A previous attempt failed after changing some files. Check git status/diff "
                                "first and continue from there.\n\n" + self.prompt)
            L.launch(self.task, self.cfg, self.store, project=self.p.get("project"), cwd=self.project_dir,
                     p=dict(self.p), prompt=retry_prompt, on_finish=self.on_finish, log_runs=self.log_runs,
                     parent_run_id=self.run_id, hops=self.hops, fallback_from=self.fallback_from, retries=n)
            return status, note, True
        return status, f"failed after {max_retries} retries: {note}", False   # report back to the owner: pane stays open

    # ---- 7. the end ----
    def _finalize(self, status, note, code, files):
        L = runstate.launcher()
        in_worktree = self.wt is not None   # its files are committed on the branch, not by `_finish`
        note += self._wt_close(status)
        L._finish(self.run_id, status, note, self.p, self.task, self.cfg, self.store, self.log_runs,
                  workdir=self.project_dir, log_path=self.log_path,
                  auto_git=(self.on_finish is None and not in_worktree), files=files)
        self._notify(status, code)
