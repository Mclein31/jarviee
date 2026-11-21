import unittest
from pathlib import Path

from helpers import TempDir, make_cfg, ROOT  # noqa: F401
from hub import launcher, obsidian
from hub.config import load_config
from hub.router import classify


class RouterTests(unittest.TestCase):
    cfg = load_config(ROOT / "config.json")

    def tier(self, task):
        return classify(task, self.cfg)[0]

    def test_basic(self):
        for t in ["rename getUser to fetchUser", "fix typo in the header", "sort imports in utils.py"]:
            self.assertEqual(self.tier(t), "basic", t)

    def test_heavy(self):
        for t in ["refactor auth across the codebase and migrate the database schema",
                  "debug the race condition in the job queue",
                  "design the architecture for the sync service"]:
            self.assertEqual(self.tier(t), "heavy", t)

    def test_light(self):
        self.assertEqual(self.tier("add a dark mode toggle to the settings page and remember the choice per user"),
                         "light")

    def test_reasons_present(self):
        tier, reasons = classify("rename x", self.cfg)
        self.assertTrue(reasons)

    def test_plan_override_and_effort(self):
        p = launcher.plan("rename x to y", self.cfg, tool="claude", tier="heavy")
        # a hand-picked level picks the model; the effort is still routed per task ("rename" lowers it)
        self.assertEqual((p["tier"], p["suggested_tier"], p["model"], p["effort"]), ("heavy", "basic", "opus", "medium"))
        self.assertTrue(any("manual override" in r for r in p["reasons"]))
        self.assertEqual(launcher.plan("rename x", self.cfg, tool="claude")["effort"], "low")   # haiku takes effort

    def test_plan_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            launcher.plan("x", self.cfg, tool="nope")
        with self.assertRaises(ValueError):
            launcher.plan("x", self.cfg, tier="huge")

    def test_argv_single_pass(self):
        tool = {"command": ["bin", "-p", "{prompt}", "--model", "{model}"], "effort_args": ["--effort", "{effort}"]}
        argv = launcher.build_argv(tool, "use {model} and {effort} literally", "m1", "high")
        self.assertEqual(argv, ["bin", "-p", "use {model} and {effort} literally", "--model", "m1", "--effort", "high"])
        self.assertEqual(launcher.build_argv(tool, "x", "m1", ""), ["bin", "-p", "x", "--model", "m1"])

    def test_m1_200k_token_classifies_linearly(self):
        import time
        for prompt, want in [("rename x " + "y" * 200_000, "basic"),
                             ("y" * 200_000, "light"),
                             ("do not " + "a, " * 50_000, "complex")]:
            t0 = time.perf_counter()
            tier = self.tier(prompt)
            elapsed = time.perf_counter() - t0
            self.assertEqual(tier, want, prompt[:30])
            self.assertLess(elapsed, 0.1, f"took {elapsed:.4f}s")

    def test_m4_qa_router_examples(self):
        self.assertEqual(self.tier("add OAuth login"), "heavy")
        self.assertEqual(self.tier("Fix the authorization header typo"), "basic")
        self.assertEqual(self.tier("rename user.py, auth.py, db.py and api.py modules to snake case"), "light")
        self.assertEqual(self.tier("do not refactor anything, just fix the typo in header.html"), "basic")
        self.assertEqual(self.tier("change the login button colour"), "basic")
        self.assertEqual(self.tier("refactor auth and fix typo"), "heavy")

    def test_m3_dash_prompt_protected_from_flag_parsing(self):
        from hub import guards, runplan
        real = load_config(ROOT / "config.json")
        for tool_name in ("claude", "omniroute"):
            argv = runplan.build_argv(real["tools"][tool_name], "--dangerously-skip-permissions",
                                      real["tools"][tool_name]["models"]["complex"], "medium", session="s1")
            self.assertEqual(argv[-2:], ["--", "--dangerously-skip-permissions"])
            self.assertLess(argv.index("--model"), argv.index("--"))
            self.assertLess(argv.index("--session-id"), argv.index("--"))
            if tool_name == "claude":
                self.assertLess(argv.index("--effort"), argv.index("--"))
                guarded = guards.headless_argv({}, {"tool": "claude", "level": "light"}, argv)
                self.assertLess(guarded.index("--max-budget-usd"), guarded.index("--"))
                json_argv = runplan.headless_json_argv(guarded, "claude", {"tools": {"claude": {"json_output": True}}})
                self.assertLess(json_argv.index("--output-format"), json_argv.index("--"))
            live = runplan.interactive_argv(tool_name, argv)
            self.assertNotIn("-p", live)
            self.assertEqual(live[-2:], ["--", "--dangerously-skip-permissions"])
            ro = runplan.append_flags(
                runplan.build_argv(real["tools"][tool_name], "--version", "sonnet", "", read_only=True),
                runplan.readonly_args(tool_name),
            )
            self.assertEqual(ro[-2:], ["--", "--version"])
            self.assertLess(ro.index("--permission-mode"), ro.index("--"))
        agy_argv = runplan.build_argv(real["tools"]["agy"], "--dangerously-skip-permissions", "gemini-3.8-flash-low", "low")
        self.assertEqual(agy_argv[agy_argv.index("-p") + 1], " --dangerously-skip-permissions")
        agy_live = runplan.interactive_argv("agy", agy_argv)
        self.assertEqual(agy_live[agy_live.index("-i") + 1], " --dangerously-skip-permissions")


class BrainTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = self._t.__enter__()
        self.cfg, _ = make_cfg(self.tmp)
        self.ws = Path(self.cfg["obsidian"]["workspace_root"])

    def tearDown(self):
        self._t.__exit__()

    def test_todo_stats(self):
        s = obsidian.todo_stats((self.ws / "Alpha/.brain/TODO.md").read_text(encoding="utf-8"))
        self.assertEqual((s["done"], s["todo"], s["owner"], s["total"], s["progress"]), (3, 3, 1, 7, 43))
        self.assertEqual(s["owner_tasks"], ["Create the Stripe account"])
        self.assertIn("Still to fix: logout copy", s["open_tasks"])
        self.assertIn("Add billing", s["open_tasks"])
        self.assertIn("Old open checkbox", s["open_tasks"])

    def test_todo_empty(self):
        self.assertIsNone(obsidian.todo_stats("# nothing here")["progress"])

    def test_todo_priority_parsing_and_sorting(self):
        doc = """
- ⬜ Normal task one
- ⬜ [P2] Medium priority cleanup
- ⬜ [High] High priority refactor
- ⬜ [P0] Critical security fix
- ⬜ (Urgent) Rotate production token
- ⬜ [P1] Add rate limiting
- ⬜ [Priority] Ship audit log
- ⬜ ! Fix flaky build
- 👤 Standard owner item
- 👤 (P0) Urgent owner approval
"""
        s = obsidian.todo_stats(doc)
        self.assertEqual(
            s["open_tasks"],
            [
                "Critical security fix",
                "Rotate production token",
                "High priority refactor",
                "Add rate limiting",
                "Ship audit log",
                "Fix flaky build",
                "Medium priority cleanup",
                "Normal task one",
            ],
        )
        self.assertEqual(
            [(x["text"], x["priority"], x["rank"]) for x in s["open_task_items"]],
            [
                ("Critical security fix", "P0", 0),
                ("Rotate production token", "Urgent", 0),
                ("High priority refactor", "High", 1),
                ("Add rate limiting", "P1", 1),
                ("Ship audit log", "Priority", 1),
                ("Fix flaky build", "High", 1),
                ("Medium priority cleanup", "P2", 2),
                ("Normal task one", "", 99),
            ],
        )
        self.assertEqual(s["owner_tasks"], ["Urgent owner approval", "Standard owner item"])
        self.assertEqual(
            [(x["text"], x["priority"], x["rank"]) for x in s["owner_task_items"]],
            [
                ("Urgent owner approval", "P0", 0),
                ("Standard owner item", "High", 1),
            ],
        )
        self.assertEqual(s["priority_count"], 9)

    def test_todo_no_truncation_limit(self):
        lines = [f"- ⬜ Open task {i}" for i in range(15)] + [f"- 👤 Owner task {i}" for i in range(14)]
        s = obsidian.todo_stats("\n".join(lines))
        self.assertEqual(len(s["open_tasks"]), 15)
        self.assertEqual(len(s["owner_tasks"]), 14)
        self.assertEqual(len(s["open_task_items"]), 15)
        self.assertEqual(len(s["owner_task_items"]), 14)

    def test_status_table(self):
        rows = obsidian.status_rows(self.cfg)
        self.assertEqual([r["folder"] for r in rows], ["Alpha", "Beta Site", "NoSuchFolder"])
        self.assertEqual(rows[0]["link"], "AlphaApp")
        self.assertEqual(rows[0]["status"], "🟢 Active")
        self.assertEqual(rows[0]["blocker"], "Stripe account")

    def test_list_projects(self):
        ps = {p["name"]: p for p in obsidian.list_projects(self.cfg)}
        self.assertEqual(sorted(ps), ["Alpha", "Beta Site"])   # no NoBrain, no hidden, no _BRAIN
        a = ps["Alpha"]
        self.assertEqual((a["progress"], a["owner_count"], a["status"], a["focus"]), (43, 1, "🟢 Active", "Billing"))
        self.assertEqual(a["path"], str(self.ws / "Alpha"))
        self.assertTrue(ps["Beta Site"]["in_status_table"])
        self.assertIsNone(ps["Beta Site"]["progress"])

    def test_project_context(self):
        text, meta = obsidian.project_context(self.cfg, "alpha")
        self.assertIn("Alpha is a web app.", text)
        self.assertIn("Use SQLite.", text)
        self.assertNotIn("type: todo-list", text)        # frontmatter stripped
        self.assertEqual(meta["path"], str(self.ws / "Alpha"))
        self.cfg["obsidian"]["max_context_chars"] = 20
        self.assertTrue(obsidian.project_context(self.cfg, "Alpha")[0].endswith("…(trimmed)"))

    def test_find_project_is_contained(self):
        for bad in ["../Alpha", "Alpha/..", "..", "/etc", "NoBrain", "_BRAIN", ".hidden", ""]:
            self.assertIsNone(obsidian.find_project(self.cfg, bad), bad)

    def test_log_run_only_appends_agent_runs(self):
        brain = self.ws / "Alpha/.brain"
        before = {f.name: f.read_bytes() for f in brain.iterdir()}
        status_before = (self.ws / "_BRAIN/PROJECT_STATUS.md").read_bytes()
        run = {"project": "Alpha", "tool": "echo", "model": "fake-small", "tier": "basic",
               "status": "done", "task": "rename\nthing"}
        obsidian.log_run(self.cfg, run)
        obsidian.log_run(self.cfg, {**run, "status": "failed"})
        lines = [l for l in (brain / "AGENT_RUNS.md").read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("echo / fake-small (basic) · done · rename thing", lines[0])
        self.assertTrue(lines[1].startswith("- ❌"))
        for name, data in before.items():
            self.assertEqual((brain / name).read_bytes(), data, name)
        self.assertEqual((self.ws / "_BRAIN/PROJECT_STATUS.md").read_bytes(), status_before)
        self.assertEqual(sorted(p.name for p in (self.ws / "_BRAIN").iterdir()), ["PROJECT_STATUS.md"])
        self.assertIsNone(obsidian.log_run(self.cfg, {**run, "project": ""}))

    def test_store_wal_and_auto_commit_hygiene(self):
        import subprocess
        from hub.launcher import auto_commit, clean_commit_message
        from hub.server import _read_log_bytes
        from hub.store import Store

        store = Store(self.cfg["db_path"])
        mode = store.db.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

        # Commit message hygiene: zero Co-authored-by or AI tool names
        msg = clean_commit_message("Add billing endpoint with Claude and Antigravity\nCo-authored-by: Claude <noreply@anthropic.com>")
        self.assertTrue(msg.startswith("feat: add billing endpoint"))
        for forbidden in ("co-authored-by", "claude", "antigravity", "gemini", "agy"):
            self.assertNotIn(forbidden, msg.lower())

        # Auto-commit in a git repo uses repo's user.name and user.email with zero AI traces
        repo = self.ws / "Alpha"
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Jane Developer"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "jane@example.com"], cwd=repo, check=True)
        (repo / "feature.py").write_text("print('ok')\n", encoding="utf-8")
        log_file = Path(self.cfg["logs_dir"]) / "run-test.log"
        log_file.write_text("", encoding="utf-8")
        self.assertEqual(_read_log_bytes({"id": "run-test", "log_path": str(log_file)}),
                         b"(Log is currently empty or flushing...)")

        self.assertFalse(auto_commit(str(repo), "Fix login validation bug", log_path=log_file))   # no file list: never `git add -A`
        self.assertTrue(auto_commit(str(repo), "Fix login validation bug", log_path=log_file, files=["feature.py"]))
        git_log = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("Jane Developer <jane@example.com>", git_log)
        self.assertIn("fix: fix login validation bug", git_log)
        for forbidden in ("co-authored-by", "claude", "antigravity", "gemini"):
            self.assertNotIn(forbidden, git_log.lower())

    def test_stop_and_pid_capture_and_dashboard_symmetry(self):
        import signal
        from unittest.mock import MagicMock, patch
        import time
        from hub.launcher import stop, stop_with_reason, _procs
        from hub.server import handle_hook
        from hub.store import Store

        store = Store(self.cfg["db_path"])

        # 1. Stop for managed launcher process
        mock_proc = MagicMock()
        _procs["managed-run"] = mock_proc
        store.upsert("managed-run", status="running", activity="working", pid=12345)
        self.assertTrue(stop("managed-run", store))
        mock_proc.terminate.assert_called_once()
        row = store.get("managed-run")
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["activity"], "stopped by you")
        self.assertIsNotNone(row["ended"])
        _procs.pop("managed-run", None)

        # 2. Hook session: only a PID verified as this agent's process is signalled
        store.upsert("hook-run", source="hook", tool="claude", status="running", activity="thinking",
                     pid=99999, agent_pid=88888)
        with patch("hub.launcher.proc_name", side_effect=lambda pid: "claude" if int(pid) == 88888 else ""), \
                patch("os.kill") as mock_kill:
            self.assertTrue(stop("hook-run", store))
            mock_kill.assert_called_once_with(88888, signal.SIGTERM)
        row = store.get("hook-run")
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["activity"], "stopped by you")
        self.assertIsNotNone(row["ended"])

        # 2b. A live PID that isn't the agent (e.g. a reused PID) is never signalled
        store.upsert("hook-other", source="hook", tool="claude", status="running", pid=77777)
        with patch("hub.launcher.proc_name", return_value="zsh"), patch("os.kill") as mock_kill:
            ok, reason = stop_with_reason("hook-other", store)
            self.assertFalse(ok)
            self.assertIn("isn't this agent", reason)
            mock_kill.assert_not_called()
        self.assertEqual(store.get("hook-other")["status"], "running")

        # 2c. Antigravity app sessions share one process: refuse, with the reason
        store.upsert("agy-app", source="hook", tool="agy", status="running", agent_host="antigravity-app")
        ok, reason = stop_with_reason("agy-app", store)
        self.assertFalse(ok)
        self.assertIn("Antigravity app", reason)
        self.assertEqual(store.get("agy-app")["status"], "running")

        # 3. No PID reported yet: can't be stopped, row stays running
        store.upsert("orphan-run", source="hook", tool="claude", status="running", activity="working")
        ok, reason = stop_with_reason("orphan-run", store)
        self.assertFalse(ok)
        self.assertEqual(store.get("orphan-run")["status"], "running")

        # 3b. A quiet session whose process is gone just moves to Finished
        store.upsert("stale-run", source="hook", tool="claude", status="running", pid=66666)
        store.db.execute("UPDATE agents SET updated=? WHERE id='stale-run'", (time.time() - 3600,))
        with patch("hub.launcher.proc_name", return_value=""), patch("os.kill") as mock_kill:
            self.assertTrue(stop("stale-run", store))
            mock_kill.assert_not_called()
        self.assertEqual(store.get("stale-run")["status"], "stopped")

        # 4. Stop with empty run_id returns False
        self.assertFalse(stop("", store))
        self.assertFalse(stop(None, store))

        # 5. Hook captures PID
        aid = handle_hook("SessionStart", {"pid": 54321, "session_id": "test-sess", "jarvieeTool": "agy"}, self.cfg, store)
        hrow = store.get(aid)
        self.assertEqual(hrow["pid"], 54321)

        # 6. Dashboard (hub/dashboard.html + hub/static): one shared row grid, one-line titles, tier as a text pill,
        #    Stop on every running and waiting row (any source but a subagent, which stops with its parent),
        #    inline Yes / No instead of confirm()
        ui = "\n".join(f.read_text(encoding="utf-8") for f in [ROOT / "hub" / "dashboard.html", *sorted((ROOT / "hub" / "static").glob("*.[jc]s*"))])
        self.assertIn(".arow { display: grid; grid-template-columns:", ui)
        self.assertRegex(ui, r"\.c-t \.t \{[^}]*white-space: nowrap")
        self.assertIn('<span class="pill t-${esc(tier)}"', ui)
        self.assertIn('if (["running", "waiting"].includes(a.status) && a.source !== "subagent") {', ui)
        self.assertNotIn('a.source === "launcher" && a.status === "running"', ui)
        self.assertNotRegex(ui, r"(?<![\w.])confirm\(")
        self.assertIn('data-act="stop-yes"', ui)

    def test_m3_dash_prompt_launch_reaches_agent_as_text(self):
        import os
        import stat
        import sys
        from unittest.mock import patch
        from hub.store import Store

        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        # Mimic claude (argparse rejects flags before '--') and agy (rejects '-p <flag-like>')
        claude_shim = bindir / "claude"
        claude_shim.write_text(
            f"#!{sys.executable}\n"
            "import argparse, sys\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('-p', action='store_true')\n"
            "ap.add_argument('--model')\n"
            "ap.add_argument('--permission-mode')\n"
            "ap.add_argument('--effort')\n"
            "ap.add_argument('--max-budget-usd')\n"
            "ap.add_argument('--max-turns')\n"
            "ap.add_argument('--session-id')\n"
            "ap.add_argument('prompt')\n"
            "ns = ap.parse_args()\n"
            "print('PROMPT_TEXT:' + repr(ns.prompt))\n",
            encoding="utf-8",
        )
        claude_shim.chmod(claude_shim.stat().st_mode | stat.S_IXUSR)
        agy_shim = bindir / "agy"
        agy_shim.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "p = args[args.index('-p') + 1]\n"
            "if p.startswith('-'):\n"
            "    sys.stderr.write('Error: -p took flag as prompt: ' + p + '\\n')\n"
            "    sys.exit(2)\n"
            "print('PROMPT_TEXT:' + repr(p))\n",
            encoding="utf-8",
        )
        agy_shim.chmod(agy_shim.stat().st_mode | stat.S_IXUSR)

        real = load_config(ROOT / "config.json")
        self.cfg["tools"]["claude"] = real["tools"]["claude"]
        self.cfg["tools"]["agy"] = real["tools"]["agy"]
        self.cfg["retry"] = {"enabled": False}
        store = Store(self.cfg["db_path"])
        old_path = os.environ.get("PATH", "")
        with patch.dict(os.environ, {"PATH": f"{bindir}:{old_path}"}):
            for tool in ("claude", "agy"):
                rid, _ = launcher.launch("--dangerously-skip-permissions", self.cfg, store, tool=tool, tier="basic")
                self.assertTrue(launcher.wait(rid, 10))
                row = store.get(rid)
                self.assertEqual(row["status"], "done", row)
                log_text = Path(row["log_path"]).read_text(encoding="utf-8")
                self.assertIn("PROMPT_TEXT:", log_text)
                self.assertIn("--dangerously-skip-permissions", log_text)


class EffortRoutingTests(unittest.TestCase):
    def setUp(self):
        from hub.config import load_config as load
        self.cfg = load()

    def test_reasoning_words_raise_and_mechanical_words_lower_effort(self):
        from hub import router
        up = router.pick_effort("debug the intermittent race condition in the queue", "claude", "complex", self.cfg)
        down = router.pick_effort("rename the variable and fix a typo", "claude", "light", self.cfg)
        self.assertEqual(up[0], "high")
        self.assertEqual(down[0], "low")   # light base is low; stays at the floor

    def test_manual_level_and_unsupported_models_keep_base(self):
        from hub import router
        self.assertEqual(router.pick_effort("debug races", "claude", "complex", self.cfg, adjust=False)[0], "medium")
        self.assertEqual(router.pick_effort("debug races", "claude", "basic", self.cfg)[0], "medium")   # haiku 5.5: low..max
        self.cfg["tools"]["claude"]["effort_levels"] = {"haiku": [], "*": ["low", "medium", "high", "xhigh", "max"]}
        self.assertEqual(router.pick_effort("debug races", "claude", "basic", self.cfg)[0], "")         # no effort control

    def test_same_logic_for_agy_and_toggle(self):
        from hub import router
        self.assertEqual(router.pick_effort("investigate why the build is flaky", "agy", "light", self.cfg)[0], "high")
        self.cfg["effort_routing"] = False
        self.assertEqual(router.pick_effort("investigate why the build is flaky", "agy", "light", self.cfg)[0], "medium")
