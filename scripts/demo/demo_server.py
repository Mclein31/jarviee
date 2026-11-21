#!/usr/bin/env python3
"""An isolated Jarviee server with made-up data, for screenshots (README, posts, docs).

    python3 scripts/demo/demo_server.py            # http://127.0.0.1:7790
    python3 scripts/demo/demo_server.py --port 7791 --keep

Nothing real is read or written, and no real agent ever runs:
  * HOME points at a throwaway folder, so ~/.jarviee, ~/.claude, ~/.gemini and the desktop wallpaper store are not the
    owner's. The Keychain, Calendar, Mail and WhatsApp are never asked (the briefing is not opened, every connector is
    off in the demo config).
  * PATH holds only a fake `agy` (it answers `agy -p /usage` with canned quota numbers) plus /usr/bin:/bin.
  * Every tool in the config runs the same harmless python "echo" command. Nothing is queued, so nothing launches.
  * The workspace is five invented projects under <demo dir>/projects with their own .brain/ folders and git repos.
Python 3.9+ standard library only. The server binds 127.0.0.1 and, like any Jarviee, is local only.

With --iterm (used by panel_demo.py) the tools run scripts/demo/fake_agent.py instead, and runs open panes in a 4×2
iTerm2 grid window named "Jarviee Demo" (its own dynamic profile, so the owner's real "Jarviee" window is never used).
Still no real agent and no token spent: the fake agent only prints.
"""
import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DEMO_ROOT = Path("/tmp") / "jarviee-demo"   # a fixed, neutral path: screenshots never show a user folder
FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"
DEMO_WINDOW = "Jarviee Demo"                # the iTerm2 window title; the owner's real window is "Jarviee"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)   # os.environ["HOME"] is the throwaway home by the time hub loads
DEMO_PROFILE_FILE = REAL_HOME / "Library/Application Support/iTerm2/DynamicProfiles/jarviee-demo.json"

# ---------------------------------------------------------------- the invented projects

PROJECTS = {
    "acme-storefront": dict(
        category="Web", status="🟢 Active", focus="Checkout rewrite", blocker="Payment provider sandbox keys",
        about="A small online shop: product pages, a cart and a Stripe-style checkout.",
        todo=["✅ Product listing page with filters", "✅ Cart drawer and quantity editing",
              "✅ Guest checkout form. ⬜ Still to fix: address autocomplete",
              "⬜ Order confirmation email template", "⬜ Add discount codes", "⬜ Cache product images behind a CDN",
              "👤 Create the payment provider sandbox account"],
        decisions=[("Server-rendered pages first", "The shop is mostly static content; hydrate only the cart."),
                   ("Money as integer cents", "Avoids float rounding in totals and refunds.")],
        files=["src/cart.ts", "src/checkout.ts", "src/pricing.ts", "tests/checkout.test.ts"]),
    "weather-api": dict(
        category="Backend", status="🟢 Active", focus="Rate limiting", blocker="None",
        about="A tiny HTTP API that returns a 5-day forecast from public weather data.",
        todo=["✅ GET /forecast?city=", "✅ Response cache with a 10 minute TTL", "⬜ Per-key rate limiting",
              "⬜ OpenAPI document", "⬜ Retry upstream calls with backoff", "👤 Pick a hosting plan"],
        decisions=[("In-memory cache, no Redis", "One instance is enough for the expected traffic.")],
        files=["api/forecast.py", "api/cache.py", "api/limits.py", "tests/test_limits.py"]),
    "portfolio-site": dict(
        category="Web", status="🟡 Maintenance", focus="Case study pages", blocker="None",
        about="A personal portfolio: projects, a short bio and a contact form.",
        todo=["✅ Home page and navigation", "✅ Dark mode", "⬜ Case study template", "⬜ Image lazy loading",
              "⬜ Contact form spam protection", "👤 Write the bio copy"],
        decisions=[("Plain HTML and CSS", "No framework: the site is a handful of pages.")],
        files=["index.html", "css/site.css", "js/theme.js"]),
    "habit-tracker-app": dict(
        category="Mobile", status="🟢 Active", focus="Streaks and reminders", blocker="Push notification certificate",
        about="A mobile habit tracker with streaks, reminders and a weekly summary.",
        todo=["✅ Habit list and check-in", "✅ Streak counter", "✅ Local reminders. ⬜ Still to fix: time zone change",
              "⬜ Weekly summary screen", "⬜ Export habits as CSV", "⬜ Home screen widget",
              "👤 Request the push notification certificate"],
        decisions=[("Local-first storage", "All data stays on the phone; sync is a later, optional feature."),
                   ("Streak resets at local midnight", "Matches how people think about a day.")],
        files=["lib/streaks.dart", "lib/reminders.dart", "lib/summary.dart", "test/streaks_test.dart"]),
    "recipe-box": dict(
        category="Tooling", status="🔵 Archived", focus="—", blocker="None",
        about="A command-line recipe collection with search and a shopping list.",
        todo=["✅ Add and list recipes", "✅ Full-text search", "✅ Shopping list from a recipe"],
        decisions=[("SQLite for storage", "One file, easy to back up.")],
        files=["recipes/db.py", "recipes/search.py"]),
}


def _node(i, label, kind, layer, detail, file=""):
    return {"id": i, "label": label, "kind": kind, "layer": layer, "detail": detail, "file": file}


FLOWS = {
    "acme-storefront": dict(
        title="Acme Storefront", summary="Shoppers browse products, fill a cart and pay through a hosted payment page.",
        nodes=[_node("shopper", "Shopper", "actor", "frontend", "Browses and buys."),
               _node("ui.list", "Product list", "screen", "frontend", "Grid with filters and sorting.", "src/ui/list.tsx"),
               _node("ui.cart", "Cart drawer", "component", "frontend", "Quantities and totals.", "src/cart.ts"),
               _node("ui.checkout", "Checkout", "screen", "frontend", "Address, shipping and payment.", "src/checkout.ts"),
               _node("api.products", "GET /products", "api", "backend", "Lists products with filters.", "src/api/products.ts"),
               _node("api.order", "POST /orders", "api", "backend", "Validates the cart and creates an order.", "src/api/orders.ts"),
               _node("pricing", "Pricing", "service", "backend", "Totals, tax and discount codes in cents.", "src/pricing.ts"),
               _node("dec.stock", "In stock?", "decision", "backend", "Refuses an order when an item sold out.", "src/api/orders.ts"),
               _node("db", "Orders database", "store", "backend", "Products, carts and orders.", "db/schema.sql"),
               _node("mailer", "Order emails", "job", "backend", "Sends the confirmation after payment.", "src/jobs/mail.ts"),
               _node("pay", "Payment provider", "external", "external", "Hosted payment page and webhooks.")],
        edges=[("shopper", "ui.list", "browses"), ("ui.list", "api.products", "loads"), ("ui.list", "ui.cart", "adds an item"),
               ("ui.cart", "ui.checkout", "checks out"), ("ui.checkout", "api.order", "places the order"),
               ("api.order", "pricing", "prices the cart"), ("api.order", "dec.stock", "checks stock"),
               ("dec.stock", "db", "yes: saves the order"), ("api.order", "pay", "creates a payment"),
               ("pay", "mailer", "paid webhook")],
        flows=[("Buy something", ["shopper", "ui.list", "ui.cart", "ui.checkout", "api.order", "dec.stock", "db"]),
               ("After payment", ["api.order", "pay", "mailer"])]),
    "weather-api": dict(
        title="Weather API", summary="A cached forecast API in front of a public weather source.",
        nodes=[_node("client", "API client", "actor", "frontend", "Any app calling the API."),
               _node("api.forecast", "GET /forecast", "api", "backend", "Returns 5 days for a city.", "api/forecast.py"),
               _node("limits", "Rate limiter", "service", "backend", "Counts calls per API key.", "api/limits.py"),
               _node("dec.cache", "Cached?", "decision", "backend", "A 10 minute TTL per city.", "api/cache.py"),
               _node("cache", "Forecast cache", "store", "backend", "In-memory, per city.", "api/cache.py"),
               _node("upstream", "Weather source", "external", "external", "Public weather data.")],
        edges=[("client", "api.forecast", "requests"), ("api.forecast", "limits", "checks the key"),
               ("api.forecast", "dec.cache", "looks up"), ("dec.cache", "cache", "yes: returns"),
               ("dec.cache", "upstream", "no: fetches"), ("upstream", "cache", "stores")],
        flows=[("Forecast request", ["client", "api.forecast", "limits", "dec.cache", "upstream", "cache"])]),
    "portfolio-site": dict(
        title="Portfolio Site", summary="A static personal site with a contact form.",
        nodes=[_node("visitor", "Visitor", "actor", "frontend", "Reads the site."),
               _node("ui.home", "Home", "screen", "frontend", "Intro and featured projects.", "index.html"),
               _node("ui.case", "Case study", "screen", "frontend", "One page per project.", "work/"),
               _node("ui.theme", "Theme switch", "component", "frontend", "Dark and light mode.", "js/theme.js"),
               _node("form", "Contact form", "api", "backend", "Posts a message to the mail service.", "api/contact.js"),
               _node("mail", "Mail service", "external", "external", "Delivers the message.")],
        edges=[("visitor", "ui.home", "opens"), ("ui.home", "ui.case", "clicks a project"), ("ui.home", "ui.theme", "toggles"),
               ("ui.case", "form", "sends a message"), ("form", "mail", "delivers")],
        flows=[("Get in touch", ["visitor", "ui.home", "ui.case", "form", "mail"])]),
    "habit-tracker-app": dict(
        title="Habit Tracker", summary="Check in daily, keep streaks, get reminders. Data stays on the phone.",
        nodes=[_node("user", "User", "actor", "frontend", "Checks in on habits."),
               _node("ui.today", "Today", "screen", "frontend", "Habits due today.", "lib/ui/today.dart"),
               _node("ui.summary", "Weekly summary", "screen", "frontend", "A chart of the week.", "lib/summary.dart"),
               _node("streaks", "Streak engine", "service", "backend", "Counts days in a row.", "lib/streaks.dart"),
               _node("dec.midnight", "New day?", "decision", "backend", "Resets at local midnight.", "lib/streaks.dart"),
               _node("reminders", "Reminders", "job", "backend", "Schedules local notifications.", "lib/reminders.dart"),
               _node("local.db", "Local database", "store", "backend", "SQLite on the device.", "lib/db.dart"),
               _node("os.notify", "OS notifications", "external", "external", "The phone's notification centre.")],
        edges=[("user", "ui.today", "checks in"), ("ui.today", "streaks", "records"), ("streaks", "dec.midnight", "evaluates"),
               ("dec.midnight", "local.db", "saves the streak"), ("ui.summary", "local.db", "reads"),
               ("reminders", "os.notify", "schedules"), ("os.notify", "user", "nudges"), ("user", "ui.summary", "opens")],
        flows=[("Daily check-in", ["user", "ui.today", "streaks", "dec.midnight", "local.db"]),
               ("Reminder", ["reminders", "os.notify", "user"])]),
    "recipe-box": dict(
        title="Recipe Box", summary="A CLI for saving recipes and building a shopping list.",
        nodes=[_node("cook", "Cook", "actor", "frontend", "Uses the command line."),
               _node("cli", "recipes CLI", "component", "frontend", "add, list, search, shop.", "recipes/cli.py"),
               _node("search", "Search", "service", "backend", "Full-text search.", "recipes/search.py"),
               _node("db", "recipes.db", "store", "backend", "SQLite file.", "recipes/db.py")],
        edges=[("cook", "cli", "types"), ("cli", "search", "queries"), ("search", "db", "reads"), ("cli", "db", "writes")],
        flows=[("Find a recipe", ["cook", "cli", "search", "db"])]),
}


def write_workspace(ws):
    (ws / "_BRAIN").mkdir(parents=True)
    rows = "\n".join(f"| **{name}** | {p['category']} | {p['status']} | {p['focus']} | {p['blocker']} |"
                     for name, p in PROJECTS.items())
    (ws / "_BRAIN" / "PROJECT_STATUS.md").write_text(
        "---\ntype: status-dashboard\n---\n# Status\n\n"
        "| Project | Category | Current Status | Primary Focus | Blocker / Risk |\n| :--- | :--- | :--- | :--- | :--- |\n"
        + rows + "\n", encoding="utf-8")
    for name, p in PROJECTS.items():
        d = ws / name
        b = d / ".brain"
        b.mkdir(parents=True)
        (b / "PROJECT_CONTEXT.md").write_text(f"# {name}\n\n{p['about']}\n\n## Current state\nFocus: {p['focus']}.\n", encoding="utf-8")
        (b / "TODO.md").write_text("---\ntype: todo-list\n---\n# TODO\n\nStatus key: ⬜ to do · ✅ done · 👤 needs the owner.\n\n"
                                   + "\n".join("- " + t for t in p["todo"]) + "\n", encoding="utf-8")
        (b / "DECISIONS.md").write_text("# Decisions\n\n" + "\n".join(f"## {t}\n{w}\n" for t, w in p["decisions"]), encoding="utf-8")
        flow = FLOWS[name]
        (b / "FLOW.json").write_text(json.dumps({
            "version": 1, "title": flow["title"], "summary": flow["summary"], "nodes": flow["nodes"],
            "edges": [{"from": a, "to": c, "label": lab} for a, c, lab in flow["edges"]],
            "flows": [{"name": n, "steps": s} for n, s in flow["flows"]]}, indent=2), encoding="utf-8")
        # a git repo with one commit, so the review inbox can show real diffs
        for f in p["files"]:
            fp = d / f
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(f"// {name}: {f}\n" + "".join(f"line {i}\n" for i in range(30)), encoding="utf-8")
        git(d, "init", "-q", "-b", "main")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "Initial commit")


def git(d, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Demo Dev", GIT_AUTHOR_EMAIL="dev@example.invalid",
               GIT_COMMITTER_NAME="Demo Dev", GIT_COMMITTER_EMAIL="dev@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, env=env)
    return r.stdout.strip()


def write_fake_bin(bindir):
    """A fake `agy` that only answers `-p /usage`; a fake `claude` that does nothing. Neither can reach a real service."""
    bindir.mkdir(parents=True)
    now = time.time()
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + s))   # noqa: E731
    usage = {"command": {"data": {"groups": [
        {"name": "Gemini Flash", "description": "Models within this group: Gemini 3.8 Flash",
         "buckets": [{"id": "flash-5h", "window": "5h", "remaining_fraction": 0.78, "reset_time": iso(3 * 3600 + 1500)},
                     {"id": "flash-weekly", "window": "weekly", "remaining_fraction": 0.91, "reset_time": iso(4 * 86400)}]},
        {"name": "Gemini Hydrogen", "description": "Models within this group: Gemini 4 Hydrogen, Gemini 4 Argon",
         "buckets": [{"id": "h-5h", "window": "5h", "remaining_fraction": 0.46, "reset_time": iso(2 * 3600 + 600)},
                     {"id": "h-weekly", "window": "weekly", "remaining_fraction": 0.63, "reset_time": iso(4 * 86400)}]},
        {"name": "Gemini Neon", "description": "Models within this group: Gemini 4 Neon, Gemini Pro Orchestrator",
         "buckets": [{"id": "n-5h", "window": "5h", "remaining_fraction": 0.88, "reset_time": iso(4 * 3600)},
                     {"id": "n-weekly", "window": "weekly", "remaining_fraction": 0.72, "reset_time": iso(4 * 86400)}]}]}}}
    # the reset times are fixed at start; the 20 s refresh keeps returning the same canned answer
    agy = bindir / "agy"
    agy.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(usage) + "\nEOF\n", encoding="utf-8")
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\necho 'demo: no agent runs here' >&2\nexit 1\n", encoding="utf-8")
    for f in (agy, claude):
        f.chmod(0o755)


def write_usage_files(home, now):
    """Claude transcripts (token and cost numbers) and the status-line rate limits, all invented."""
    proj = home / ".claude" / "projects" / "demo"
    proj.mkdir(parents=True)
    lines, n = [], 0
    models = [("claude-sonnet-5-5", 0.55), ("claude-opus-5-5", 0.25), ("claude-haiku-5-5", 0.20)]
    for h in range(0, 30):   # a few turns an hour over the last 30 hours, heavier in work hours
        ts = now - h * 3600 - 600
        for mi, (m, share) in enumerate(models):
            count = max(0, int((6 - (h % 7)) * share * 3)) + (1 if h < 6 else 0)
            for _ in range(count):
                n += 1
                t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts - mi * 120 - n % 50))
                lines.append(json.dumps({"type": "assistant", "timestamp": t, "requestId": f"req{n}",
                                         "message": {"id": f"msg{n}", "model": m,
                                                     "usage": {"input_tokens": 900 + n * 7 % 2400, "output_tokens": 600 + n * 13 % 3200,
                                                               "cache_read_input_tokens": 18000 + n * 211 % 60000,
                                                               "cache_creation_input_tokens": 1200 + n * 17 % 4000}}}))
    (proj / "session-demo.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    brain = home / ".gemini" / "antigravity-cli" / "brain" / "demo" / ".system_generated" / "logs"
    brain.mkdir(parents=True)
    rows = []
    for i in range(160):
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i * 600 - 120))
        rows.append(json.dumps({"created_at": t, "step_index": i, "input_tokens": 4000 + i * 37 % 9000,
                                "output_tokens": 300 + i * 11 % 1800, "cache_read_tokens": 30000 + i * 311 % 90000}))
    (brain / "transcript.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (home / ".jarviee").mkdir(exist_ok=True)
    (home / ".jarviee" / "claude-limits.json").write_text(json.dumps({"captured_at": now - 40, "rate_limits": {
        "five_hour": {"used_percentage": 38.0, "resets_at": now + 2 * 3600 + 1260},
        "seven_day": {"used_percentage": 57.0, "resets_at": now + 3 * 86400 + 7200}}}), encoding="utf-8")


def make_config(root, port, iterm=None):
    base = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    cfg_dir = root / "data"
    harmless = [sys.executable, "-c", "import sys; print('demo agent: nothing runs here')"]
    # --iterm: the tools "run" the fake agent (it only prints, for ~duration seconds); else a one-line harmless command
    fake = [sys.executable, str(FAKE_AGENT), "--model", "{model}", "--effort", "{effort}", "--duration",
            str((iterm or {}).get("duration", 240)), "--", "{prompt}"]
    for name, t in base["tools"].items():
        t["command"] = fake if iterm else harmless + ["{model}", "{prompt}"]
        for k in ("bridge_command", "env_from", "session_args", "resume_args", "effort_args"):
            t.pop(k, None)
    base.update(port=port, db_path=str(cfg_dir / "hub.db"), logs_dir=str(cfg_dir / "logs"),
                token_path=str(cfg_dir / "token"), alerts_state_path=str(cfg_dir / "alerts.json"),
                flow_layout_path=str(cfg_dir / "flow-layout.json"), map_watch_path=str(cfg_dir / "map-watch.json"),
                push_state_path=str(cfg_dir / "push.json"), settings_path=str(cfg_dir / "settings.json"),
                wallpaper_dir=str(cfg_dir / "wallpaper"), default_tool="echo")
    base["obsidian"]["workspace_root"] = str(root / "projects")
    base["terminal"] = {**base.get("terminal", {}), "mode": "background", "interactive": False, "queue_interactive": False,
                        "focus_on_launch": False}
    if iterm:   # a 4×2 grid of iTerm2 panes in its own window; one-shot runs (interactive off) print the title and folder
        base["terminal"].update(mode="iterm2", layout="grid", grid=[4, 2], window_name=DEMO_WINDOW, display=iterm["display"],
                                position="left", width_pct=100, fullscreen=False, close_on_finish="never",
                                font_size=iterm["font_size"], max_panes_per_tab=8)
    base["dashboard_window"] = {"browser": "default", "display": "main"}
    base["appearance"] = {"wallpaper": "none"}   # plain dark: never the owner's desktop picture
    base.setdefault("alerts", {}).update(enabled=False, notifier="osascript")
    base["voice"] = {**base.get("voice", {}), "enabled": False, "live_reports": False}
    base["omniroute"] = {**base.get("omniroute", {}), "enabled": False}
    base["maps"] = {"auto_update": False}
    base["push"] = {"enabled": False, "limit_alerts": False}
    base["whatsapp_send"] = {"enabled": False}
    base["email_send"] = {"enabled": False}
    base["pipeline"] = {**base.get("pipeline", {}), "enabled": False}
    base["board"] = {"polish": {"enabled": False}}
    base["fallback"] = {**base.get("fallback", {}), "enabled": True}
    (cfg_dir).mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.json").write_text(json.dumps({"push": {"enabled": False}, "voice": {"enabled": False},
                                                       "maps": {"auto_update": False}, "appearance": {"wallpaper": "none"}}), encoding="utf-8")
    path = cfg_dir / "config.json"
    path.write_text(json.dumps(base, indent=1), encoding="utf-8")
    return path


# ---------------------------------------------------------------- seeded state

def seed(cfg, store, ws, with_running=True):
    from hub import board_api, obsidian
    now = time.time()
    M = 60

    def run(id, tool, model, tier, effort, project, task, title, status, started_ago, dur=None, activity="", steps=0,
            files=None, source="hook", **extra):
        proj = ws / project
        fields = dict(source=source, tool=tool, model=model, tier=tier, suggested_tier=tier, effort=effort, project=project,
                      task=task, title=title, status=status, activity=activity, workspace=str(proj), steps=steps,
                      started=now - started_ago * M, updated=now - (started_ago - (dur or 0)) * M if dur else now - 20)
        if status in ("done", "failed", "stopped"):
            fields["ended"] = now - (started_ago - (dur or 3)) * M
            fields["updated"] = fields["ended"]
        if files:
            fields["files"] = files
        fields.update(extra)
        store.upsert(id, **fields)

    def change(project, files, subject, commit=True):
        """Edit files in the fake repo; return (base_head)."""
        d = ws / project
        base = git(d, "rev-parse", "HEAD")
        for f in files:
            with open(d / f, "a", encoding="utf-8") as fh:
                fh.write("".join(f"// added by the demo agent {i}\n" for i in range(7)))
        if commit:
            git(d, "add", "-A")
            git(d, "commit", "-q", "-m", subject)
        return base

    # running (left out with --iterm: the live panes are the running agents)
    live = (lambda *a, **k: None) if not with_running else run
    live("demo-run-01", "claude", "opus", "heavy", "high", "acme-storefront",
        "Rewrite the checkout flow to use integer cents and add guest checkout validation",
        "Rewrite checkout to integer cents", "running", 14, activity="Editing src/checkout.ts", steps=23)
    live("demo-run-02", "agy", "gemini-4-hydrogen-medium", "light", "medium", "weather-api",
        "Add per-key rate limiting to the forecast endpoint", "Add per-key rate limiting", "running", 6,
        activity="Running tests/test_limits.py", steps=11)
    live("demo-run-03", "claude", "sonnet", "complex", "medium", "habit-tracker-app",
        "Fix the streak counter when the phone's time zone changes", "Fix streaks on time zone change", "running", 9,
        activity="Reading lib/streaks.dart", steps=8)
    live("demo-run-04", "agy", "gemini-3.8-flash-low", "basic", "low", "portfolio-site",
        "Rename the nav label 'Work' to 'Projects' everywhere", "Rename nav label to Projects", "running", 2,
        activity="Searching for 'Work' in index.html", steps=3)
    # needs you
    run("demo-run-05", "claude", "opus", "heavy", "high", "acme-storefront",
        "Add discount codes with a percentage and a fixed amount", "Add discount codes", "waiting", 21,
        activity="Waiting: may I run `npm run migrate` against the dev database?", steps=17)
    run("demo-run-06", "claude", "sonnet", "complex", "medium", "habit-tracker-app",
        "Add a weekly summary screen with a bar chart", "Weekly summary screen", "waiting", 17,
        activity="Needs your choice: bar chart or heat map?", steps=12)
    # done with changes (review inbox), some committed
    base = change("acme-storefront", ["src/cart.ts", "tests/checkout.test.ts"], "Cart: edit quantities in the drawer")
    run("demo-run-07", "claude", "sonnet", "complex", "medium", "acme-storefront",
        "Let shoppers change item quantities inside the cart drawer", "Edit quantities in the cart drawer", "done", 95, 9,
        activity="Done: quantities update totals live; tests pass", steps=19, files=["src/cart.ts", "tests/checkout.test.ts"],
        source="launcher", base_head=base, reviewed=0, verified=1)
    base = change("weather-api", ["api/cache.py", "api/forecast.py"], "Cache forecasts for 10 minutes")
    run("demo-run-08", "agy", "gemini-4-hydrogen-medium", "light", "medium", "weather-api",
        "Cache forecast responses for 10 minutes per city", "Cache forecasts per city", "done", 70, 6,
        activity="Done: cache with TTL and 4 new tests", steps=14, files=["api/cache.py", "api/forecast.py"],
        source="launcher", base_head=base, reviewed=0)
    base = change("portfolio-site", ["css/site.css", "js/theme.js"], "Dark mode follows the system setting")
    run("demo-run-09", "claude", "haiku", "basic", "low", "portfolio-site",
        "Make dark mode follow the system setting", "Dark mode follows the system", "done", 130, 3,
        activity="Done: prefers-color-scheme respected", steps=6, files=["css/site.css", "js/theme.js"],
        source="launcher", base_head=base, reviewed=0)
    base = change("habit-tracker-app", ["lib/reminders.dart"], "", commit=False)
    run("demo-run-10", "agy", "gemini-4-neon-high", "heavy", "high", "habit-tracker-app",
        "Reschedule local reminders after a time zone change", "Reschedule reminders after time zone change", "done", 48, 11,
        activity="Done: reminders rebuilt on change; not committed yet", steps=21, files=["lib/reminders.dart"],
        source="launcher", base_head=base, reviewed=0)
    # failed and escalated
    run("demo-run-11", "agy", "gemini-3.8-flash-low", "basic", "low", "weather-api",
        "Add an OpenAPI document for the forecast endpoint", "Add an OpenAPI document", "failed", 55, 4,
        activity="Failed: output was cut off; the run was handed to a stronger model", steps=5)
    run("demo-run-12", "claude", "sonnet", "complex", "medium", "weather-api",
        "Add an OpenAPI document for the forecast endpoint", "Add an OpenAPI document", "done", 50, 8,
        activity="Done: openapi.yaml written and validated", steps=12, files=["openapi.yaml"],
        source="hook", fallback_from="agy:gemini-3.8-flash-low", parent_run_id="demo-run-11", hops=1)
    run("demo-run-13", "claude", "haiku", "basic", "low", "recipe-box",
        "Fix the typo in the shopping list heading", "Fix shopping list heading typo", "done", 185, 1,
        activity="Done", steps=3)
    run("demo-run-14", "agy", "gemini-4-hydrogen-medium", "light", "medium", "portfolio-site",
        "Lazy-load the images on the projects page", "Lazy-load project images", "done", 160, 5,
        activity="Done: loading=lazy on 12 images", steps=8)
    run("demo-run-15", "claude", "opus", "heavy", "xhigh", "acme-storefront",
        "Plan the migration of the product catalog to a search index", "Plan the catalog search migration", "done", 240, 26,
        activity="Done: plan written to .brain/PLAN.md", steps=37)
    run("demo-run-16", "claude", "sonnet", "complex", "medium", "habit-tracker-app",
        "Export habits as CSV", "Export habits as CSV", "failed", 40, 7,
        activity="Failed: 2 tests failed in test/export_test.dart", steps=14)

    # board: a few cards per project, in every column
    from hub import board as boardlib
    board = board_api.get_board(cfg)
    cards = {
        "acme-storefront": [("todo", "Order confirmation email template", "feature", "normal"),
                            ("todo", "Cache product images behind a CDN", "chore", "low"),
                            ("plan", "Search index for the product catalog", "research", "high"),
                            ("build", "Rewrite checkout to integer cents", "refactor", "high", "demo-run-01"),
                            ("build", "Add discount codes", "feature", "normal", "demo-run-05"),
                            ("review", "Edit quantities in the cart drawer", "feature", "normal", "demo-run-07"),
                            ("done", "Product listing page with filters", "feature", "normal"),
                            ("done", "Cart drawer", "feature", "normal"),
                            ("done", "Guest checkout form", "feature", "normal")],
        "weather-api": [("todo", "Retry upstream calls with backoff", "bugfix", "normal"),
                        ("todo", "Pick a hosting plan", "chore", "low"),
                        ("plan", "OpenAPI document", "docs", "normal"),
                        ("build", "Per-key rate limiting", "feature", "high", "demo-run-02"),
                        ("review", "Cache forecasts per city", "feature", "normal", "demo-run-08"),
                        ("done", "GET /forecast", "feature", "normal")],
        "portfolio-site": [("todo", "Case study template", "feature", "normal"),
                           ("todo", "Contact form spam protection", "bugfix", "high"),
                           ("build", "Rename nav label to Projects", "chore", "low", "demo-run-04"),
                           ("review", "Dark mode follows the system", "feature", "low", "demo-run-09"),
                           ("done", "Home page and navigation", "feature", "normal")],
        "habit-tracker-app": [("todo", "Home screen widget", "feature", "low"),
                              ("todo", "Export habits as CSV", "feature", "normal"),
                              ("plan", "Weekly summary: chart or heat map?", "research", "normal"),
                              ("build", "Fix streaks on time zone change", "bugfix", "high", "demo-run-03"),
                              ("build", "Weekly summary screen", "feature", "normal", "demo-run-06"),
                              ("review", "Reschedule reminders after time zone change", "bugfix", "high", "demo-run-10"),
                              ("done", "Habit list and check-in", "feature", "normal"),
                              ("done", "Streak counter", "feature", "normal")],
    }
    for project, items in cards.items():
        for it in items:
            col, title, label, prio = it[:4]
            c = board.create(project, title, label=label, priority=prio)
            cid = c["id"] if isinstance(c, dict) else c
            if len(it) > 4 and (with_running or it[4] not in ("demo-run-01", "demo-run-02", "demo-run-03", "demo-run-04")):
                board.link_run(cid, it[4])
            if col != "todo":
                board.move(cid, col)

    # one quota note so the sidebar tells the story of a handed-over run
    store.set_quota("agy", "gemini-3.8-flash-low", "ok")
    return obsidian


def use_demo_iterm_profile():
    """Point hub.displays at a "Jarviee Demo" iTerm2 profile of its own (own name, GUIDs and DynamicProfiles file).

    Jarviee names its window after the dynamic profile it writes ("Jarviee"), not after terminal.window_name, so without
    this the demo would open (and rewrite the profile of) the owner's real "Jarviee" window setup. The file lands in the
    REAL ~/Library/Application Support/iTerm2/DynamicProfiles (iTerm2 only reads that one) and is deleted on exit.
    Must run after `import hub` and before the server creates its Terminal."""
    from hub import displays
    displays.PROFILE_NAME = DEMO_WINDOW
    displays.PROFILE_GUID = "D3A1B0E2-5C47-4E0A-9B1F-0DEA0D3A0001"
    displays.WINDOW_PROFILE = DEMO_WINDOW + " (window)"
    displays.WINDOW_GUID = "D3A1B0E2-5C47-4E0A-9B1F-0DEA0D3A0002"
    displays.PROFILE_PATH = DEMO_PROFILE_FILE
    displays.LEGACY_PROFILE_PATH = DEMO_PROFILE_FILE.with_name("jarviee-demo-legacy.json")   # never the owner's agent-hub.json


def close_demo_window(state_path):
    """Close the iTerm2 window this demo opened, and ONLY a window titled "Jarviee Demo" with the id the demo recorded.
    Returns what happened as a short string. Never touches any other window."""
    try:
        wid = int(json.loads(Path(state_path).read_text(encoding="utf-8")).get("window") or 0)
    except (OSError, ValueError, TypeError):
        wid = 0
    if not wid:
        return "no demo window recorded"
    script = ('on run argv\n tell application id "com.googlecode.iterm2"\n repeat with w in windows\n'
              ' if (id of w) is ((item 1 of argv) as integer) and (name of w) starts with (item 2 of argv) then\n'
              ' close w\n return "closed"\n end if\n end repeat\n end tell\n return "not found"\nend run\n')
    # iTerm2 asks "Close Window? This window is running 8 instances of Python." and the script waits for the answer, so the
    # close is started without waiting and the dialog's OK (a sheet of the "Jarviee Demo" window only) is pressed through
    # System Events (needs Accessibility permission for the app running this; otherwise click OK yourself).
    try:
        closer = subprocess.Popen(["osascript", "-", str(wid), DEMO_WINDOW], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        closer.stdin.write(script)
        closer.stdin.close()
        click = ('tell application "System Events" to tell process "iTerm2" to click button "OK" of sheet 1 of window "%s"'
                 % DEMO_WINDOW)
        for _ in range(20):
            time.sleep(0.5)
            if closer.poll() is not None:
                break
            subprocess.run(["osascript", "-e", click], capture_output=True, text=True, timeout=10)
        if closer.poll() is None:
            closer.kill()
            return "asked iTerm2 to close it, but its confirmation dialog is still open: click OK on the \"Jarviee Demo\" window"
        return (closer.stdout.read() or "").strip() or "closed"
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not close: {e}"


def prefill_window(cfg):
    """Once serve() has made the Terminal, open the "Jarviee Demo" window with its 4×2 free panes."""
    for _ in range(100):
        term = cfg.get("_terminal")
        if term is not None and cfg.get("_serving"):
            print("prefill:", term.prefill(), flush=True)
            return
        time.sleep(0.1)
    print("prefill: the terminal never came up", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Jarviee demo server with made-up data (for screenshots)")
    ap.add_argument("--port", type=int, default=7790)
    ap.add_argument("--keep", action="store_true", help="keep the demo folder after the server stops")
    ap.add_argument("--dir", default=str(DEMO_ROOT), help="where the throwaway data goes (default: %(default)s)")
    ap.add_argument("--iterm", action="store_true",
                    help='runs open panes in an iTerm2 4×2 grid window "Jarviee Demo" and run the fake agent (see panel_demo.py)')
    ap.add_argument("--display", default="builtin", help="--iterm: which screen the window fills: main | builtin | N (default: %(default)s)")
    ap.add_argument("--font-size", type=float, default=11, help="--iterm: the panes' font size (default: %(default)s)")
    ap.add_argument("--check", action="store_true",
                    help="--iterm: print the settings this server really uses as JSON, open nothing, and exit")
    ap.add_argument("--duration", type=float, default=240, help="--iterm: seconds each fake agent 'works' (default: %(default)s)")
    a = ap.parse_args()
    if a.port == 7777:
        sys.exit("port 7777 is the real Jarviee; the demo never uses it")
    root = Path(a.dir)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()

    # everything below is set BEFORE importing hub: modules read HOME and these variables at import time
    os.environ["HOME"] = str(home)
    os.environ["PATH"] = f"{root / 'bin'}:/usr/bin:/bin"
    os.environ.pop("XDG_CONFIG_HOME", None)
    for k in list(os.environ):
        if k.startswith(("JARVIEE_", "AGENT_HUB_")):
            del os.environ[k]
    os.environ.update(JARVIEE_USER_CONFIG="", JARVIEE_ALERTS="0", JARVIEE_PUSH="0", JARVIEE_DAYBOOK="0", JARVIEE_MAILSUM="0",
                      JARVIEE_MAPWATCH="0", JARVIEE_CLAUDE_PROJECTS=str(home / ".claude" / "projects"),
                      JARVIEE_AGY_BRAIN=str(home / ".gemini" / "antigravity-cli" / "brain"),
                      JARVIEE_AGY_BRAIN_ROOT=str(root / "agy_brain"),
                      JARVIEE_CLAUDE_LIMITS=str(home / ".jarviee" / "claude-limits.json"))

    ws = root / "projects"
    write_workspace(ws)
    write_fake_bin(root / "bin")
    write_usage_files(home, time.time())
    iterm = dict(display=a.display, font_size=a.font_size, duration=a.duration) if a.iterm else None
    cfg_path = make_config(root, a.port, iterm)
    os.environ["JARVIEE_CONFIG"] = str(cfg_path)
    os.environ["JARVIEE_PORT"] = str(a.port)

    sys.path.insert(0, str(REPO))
    from hub.config import load_config
    from hub.server import serve
    from hub.store import Store
    if a.iterm:
        use_demo_iterm_profile()
    cfg = load_config(cfg_path)
    cfg["port"] = a.port
    if a.check:   # what the loaded config (overlay and defaults applied) really says: nothing is started or opened
        t = cfg["terminal"]
        print(json.dumps({"port": cfg["port"], "window_name": t["window_name"], "mode": t["mode"], "layout": t["layout"],
                          "grid": t["grid"], "display": t["display"], "profile": __import__("hub.displays", fromlist=["x"]).WINDOW_PROFILE,
                          "profile_file": str(__import__("hub.displays", fromlist=["x"]).PROFILE_PATH),
                          "db_path": cfg["db_path"], "root": str(root), "fake_agent": str(FAKE_AGENT),
                          "tool_commands": {k: v["command"] for k, v in cfg["tools"].items()}}))
        shutil.rmtree(root, ignore_errors=True)
        return
    store = Store(cfg["db_path"])
    seed(cfg, store, ws, with_running=not a.iterm)
    if a.iterm:   # say plainly what this server will really use, before it can open anything
        t = cfg["terminal"]
        print(f"iTerm2 panel: window \"{t['window_name']}\" (profile \"{DEMO_WINDOW} (window)\"), mode {t['mode']}, layout {t['layout']} "
              f"{t['grid']}, display {t['display']}, port {cfg['port']}, data {cfg['db_path']}", flush=True)
    print(f"Jarviee DEMO (made-up data, zero tokens) in {root}", flush=True)
    if a.iterm:   # open the window now, every pane free, so the runs that follow find their panes ready
        threading.Thread(target=prefill_window, args=(cfg,), daemon=True).start()
    try:
        serve(cfg, store)
    finally:
        if a.iterm:
            print("closing the demo window:", close_demo_window(Path(cfg["db_path"]).parent / "terminal.json"), flush=True)
            time.sleep(1.0)
            DEMO_PROFILE_FILE.unlink(missing_ok=True)
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
