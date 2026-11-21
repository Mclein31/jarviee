# Changelog

All notable changes to Jarviee (formerly Agent Hub) will be documented in this file.

## [0.20.0] — 2026-11-21 — First public release

Jarviee is now open source (MIT). This release makes it work for anyone: generic defaults, a setup wizard, a health check, and a tightened security surface.

### Added
- **Layered configuration.** `config.json` in the repo holds generic public defaults. Your own choices live in an overlay, `~/.jarviee/config.json`, which is deep-merged on top and is the only file `hub.py setup` writes. Point `JARVIEE_USER_CONFIG` at another file to move it, or set it empty to switch the overlay off. See `docs/CONFIGURATION.md`.
- **`python3 hub.py setup`:** a first-run wizard that detects Claude Code, Antigravity (`agy`), iTerm2 and the rest, asks a few questions and writes your overlay. `--yes` accepts the detected defaults without questions; `--dry-run` prints what would be written and changes nothing. Hooks, the background service and the app are only offered, never installed without a yes.
- **`python3 hub.py doctor`:** a read-only health check (✓ fine, ! worth a look, ✗ broken; exit code 1 when something is broken).
- **`python3 hub.py brain init <project>`:** creates a starter `.brain/` from `examples/brain-template/`. It never overwrites an existing file.
- **A slash-command registry.** Every command-bar command now lives in one list (`hub/slash.py`), served at `GET /api/commands`. New: **`/help`** (every command with its arguments), **`/status`** (the fleet right now), **`/standup`** (what the agents did on a day), **`/usage`** (quota and token use from the last reading) and **`/map <project>`** (starts the agent that maps a project). `/status`, `/standup`, `/usage` and `/help` make no model calls.
- **Documentation:** a rewritten README, `docs/CONFIGURATION.md`, `docs/INTEGRATIONS.md`, `docs/ARCHITECTURE.md`, `docs/PLATFORM_SUPPORT.md`, and screenshots of every page made from invented demo data (`docs/screenshots/`).
- **A demo server** (`scripts/demo/`): an isolated Jarviee with made-up projects and runs for screenshots. It spends no tokens and never touches a real install.
- **Project files for contributors:** `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and pull-request templates, and a CI workflow that runs the tests.
- **Background-aware unattended runs.** A queue, pipeline or `/orchestrate` run whose turn ends while its own background task is still running (a backgrounded test suite, for example) is no longer closed early. It waits for the task to report back, up to `guards.background_wait_minutes` (30), then finishes as before.
- **`/orchestrate` survives a restart.** The review marks and worker state are saved under `~/.jarviee/orch/` and restored when the lead asks for them.

### Changed
- **Public defaults.** The workspace is `~/Projects`. OmniRoute, voice, map auto-update (`maps.auto_update`) and Apple Calendar (`calendar.enabled`) are all **off** until you turn them on. `/pipeline` names agent skills only when you configure some. The dashboard opens in your default browser on the main display, and CLI windows tile on the right of it.
- **`/pipeline` and the plan review name a skill only when it is installed** for the tool that will run that phase, so a prompt never asks an agent for a skill it does not have.
- **Unknown slash commands no longer start a paid run.** `/hepl` answers "Unknown command /hepl. Did you mean /help?" instead of being sent to an agent as a task. Paths such as `/src/app.py` still run as tasks.
- **Neutral names.** The launchd service is `dev.jarviee.server`, the app is `dev.jarviee.app` and the Calendar helper is `dev.jarviee.calendar`. `service install` removes the service installed under an earlier label.
- **The wallpaper presets are the macOS built-ins**, plus any of your own listed under `appearance.wallpapers` in your overlay.
- **Platform guards.** The server, hooks, queue and background runs start and degrade cleanly on Linux and Windows, and Pillow is optional (the app icon falls back to a generic one). macOS-only connectors report "not available" instead of failing. See `docs/PLATFORM_SUPPORT.md`.
- **Apple Calendar is opt-in.** The first `/startwork` no longer triggers a macOS Calendar permission prompt; turn it on with `calendar.enabled`.
- **First-run polish:** `setup` on a machine with no agent CLI installed gives a clear message and no zero-size lanes, and the first start creates `~/.jarviee` private (0700).

### Fixed
- **`/static/mark.svg` returned 404.** The dashboard's touch icon is now served, as an inert image (see Security).
- Settings files with unusual contents (a `hooks` value that is not an object, a symlinked `settings.json`, undecodable bytes, paths with spaces or quotes) are left alone or handled by `install-hooks` instead of being rewritten or crashing it.
- A handler bug now answers HTTP 500, and bad JSON types or NaN / Infinity answer 400, instead of dropping the connection.
- An image upload with a negative or garbled `Content-Length` no longer holds a server thread open, and a cut-short upload saves nothing.
- `service install` no longer touches your real launchd job when `HOME` points somewhere else (it refuses unless you pass `--force`).

### Security
- **Origin checks on every request, GETs too.** A page from another site, or from another port on 127.0.0.1, can no longer load or embed anything Jarviee serves. Hooks and the CLI (no browser headers) are unaffected.
- **Content-Security-Policy and security headers** on every response: `nosniff`, `same-origin` resource policy, no framing, no referrer. The dashboard runs scripts only from `/static/` and its own hashed inline scripts; JSON, logs and SVG are served with a policy that allows no script at all.
- **The token is read from headers only** (`X-Jarviee-Token` or `Authorization: Bearer`), never from the URL, where it would leak into history and logs.
- **Private data files.** The token is created `0600` in one step, and `~/.jarviee` (database, run logs, attachments, day book) is tightened to `0700` / `0600` at every start. Pasted screenshots are stored private.
- **A hook can no longer name pid 0 or -1**, which would have made Stop signal a whole process group or every process you own.
- A second endpoint pass added tests for forged hook events, state-changing GETs, every token-guarded POST, crafted bodies and paths, and the hook installer (`tests/test_security_endpoints.py`).
- **No personal data in the repository.** The public tree is produced by a history-free export with a privacy scan (`scripts/export_public.py`), and the tests no longer read any real project notes.

## [0.19.x] — 2026-11-12 to 2026-11-21

### Fixed (2026-11-21, /endwork lost closed windows)
- **Closing a CLI window no longer erases the work from the day.** The day book only counted `done` / `failed` jobs, so a window closed by hand (and a false "needs you" you closed) ended as `stopped` and never reached `/endwork`. They now count as "closed early" when the agent did work in them, in the spoken report, the email and the WhatsApp summary. A closed window is also no longer listed as "still needs you".
- **Summaries say what was built.** A live window's log is only the terminal screen, so the summary often said "I can't tell what it built". It now reads the agent's own transcript (Claude by session, agy by task), and each day book entry keeps the final answer so the history outlives the window and the log.
- **Git is part of the day report.** The commits each project recorded today are added to `/endwork`, so a day with closed or unreported windows still shows what got done.
- A pane closed by hand now stamps its end time.

### Added (2026-11-21, /orchestrate)
- **`/orchestrate <task>`:** type it in the command bar (always followed by a task) and Opus becomes the orchestrator in window 1 of a new 4 × 2 iTerm2 tab, with seven free windows for its workers. It splits the task into jobs, gives each worker a load (basic / light / complex / heavy) that picks the tool (agy or Claude) and model, checks every result when a worker finishes, sends rework back, and hands the freed window the next job. With no jobs left it keeps waiting and reviewing until every worker is accepted. When a tool runs out of tokens a worker moves to the other one, then to OmniRoute.
- **Auto-tile is the default again:** every other run opens a real CLI that tiles into the Jarviee window, six per tab, a new tab when full. The 4 × 2 grid only opens for `/orchestrate`, in its own tab apart from the auto-tiled ones.

### Added (2026-11-21)
- **Gemini 4 Neon on Antigravity:** agy's heavy level now runs Gemini 4 Neon, agy's strongest model (#1 on the Artificial Analysis Coding Agent Index). Its complex level runs **Orchestrator (Neon)**, where Neon plans and Gemini 4 Hydrogen does the edits and tests. Gemini 4 Argon stays as the spare: it has its own quota, so it takes over when the main Gemini quota runs out. Complex and heavy tasks still go to Claude first.
- **Routing graph:** the new models show in the Antigravity lane, Argon has its own Spare row, and clicking a model tells you what it's best at.

### Added (2026-11-18)
- **Project board:** a new **Board** tab in the sidebar gives each project five columns: To do, Plan, Build, Code review and Done. Cards carry a label (feature, bugfix, error, …) and a priority. Drag them between columns, or let them move by themselves: starting an agent from a card puts it in Build, the run finishing puts it in Code review, and accepting it in Review puts it in Done. When you save a card, Haiku tidies the wording into a clear title and acceptance points in the background; your own text is kept and one click brings it back.
- **A 4 × 2 grid of CLIs on your main screen:** opening Jarviee now opens one iTerm2 window filling the main display with eight free panes, at a smaller (9 pt) font. Each prompt you give goes into the next free pane, top-left first. A new tab of eight opens only when all eight are busy. When a session ends, its pane frees up for the next prompt; close a pane and a free one takes its place.
- **The dashboard gets its own Safari window** on the secondary screen, instead of a tab in the Safari window you're browsing in. Opening Jarviee again brings that same window back.

### Fixed (2026-11-16)
- **Planner borderline model (`m7`) & Hydrogen quota check:** updated `hub/planner.py` `make_plan` to pick `pick_model(tool, final, cfg)` instead of the light model when `prefer_agy_on_borderline` chooses `agy`; verified via `agy -p /usage --output-format json` that Gemini 4 Hydrogen shares the `"Gemini Models"` group (`5h` and `weekly` windows) and hardened `hub/static/command.js` so the `"agy"` quota-strip row targets Gemini instead of `"Claude and GPT models"` regardless of group order.
- **QA bug fixes (M1, M3, M4, M5, M6, m1):** capped `hub/router.py` classifier input and replaced the backtracking file regex with a linear pattern (`M1`); tuned path masking, clause negation, basic/structural tie-breaking, and `config.json` keywords so OAuth, auth header typos, and multi-file renames route accurately (`M4`); changed `hub/planner.py` `validate` to raise `PlanError` when `depends_on` is `null` or invalid (`M5`); guarded dash-leading prompts with `--` for `claude` and a leading space for `agy` across `runplan` callers (`M3`); restricted `/api/run` `cwd` to `workspace_root` (`M6`); and made JSON body parsing and handler validation return HTTP 400 on wrong types instead of dropping connections (`m1`).

### Added (2026-11-16)
- **OmniRoute last-resort fallback:** when both Antigravity and Claude Code run out of quota, tasks fall back to a local OmniRoute gateway (`http://127.0.0.1:20128`). Runs execute through the Claude Code CLI with injected gateway environment variables, fail fast without an API key before any task row or file claim is created, and run in an implicit 1-slot queue lane. Includes Keychain key storage (`jarviee-omniroute`), CLI commands (`hub.py omniroute connect|status|test`), an "OmniRoute as last resort" toggle in Settings › Routing with live status, and an OmniRoute status chip on the Quota KPI card.

### Added (2026-11-14)
- **Email gists:** `/startwork` now says who emailed you, what each email is about (with the amounts, dates and names) and what you need to do, important and to-do mail first. The full list is on screen.
- **Summaries on WhatsApp:** the 8:00 schedule, `/startwork` and `/endwork` can go to your WhatsApp (through CallMeBot) while quick alerts stay on ntfy. Tapping an event alert now opens Calendar instead of copying the text.
- **08:00 schedule on your phone:** every morning at 8, automatically, a 📆 notification with today's events, reminders and deadlines. It's queued in advance, so it arrives even while the Mac sleeps.
- **`/startwork`:** type it in the command bar and Jarviee tells you what's ahead: the rest of today's calendar, tomorrow's first event, due and renewal heads-ups, unread Gmail, LinkedIn (from its emails), GitHub notifications, unread WhatsApp chats and agents waiting for you. It speaks it, shows it, and sends it to your phone, one notification per source.
- **Icons on phone notifications:** 🤖 agents done, 🙋 needs you, 🚨 failed, 📅 event, ⏰ reminder, 📧 Gmail, 💼 LinkedIn, 🐙 GitHub, 💬 WhatsApp, 🌅 briefing, ⏳/🔋 Claude limit.
- `python3 hub.py briefing` prints the briefing; `python3 hub.py connect-gmail you@gmail.com` stores a Gmail App Password in the Keychain.

### Added (round 4)
- **Handy dictation:** hold Control+Option anywhere on the Mac, speak, release: Handy types what you said where the cursor is. On the Jarviee dashboard it goes straight into the command bar. The mic button uses Handy's offline model too.

### Fixed (round 4)
- Jarviee's spoken report said your prompt back and read out file paths. It now says what each job actually did and only how many files changed.
- Review: a run-planted `.brain/CHECK_COMMAND` no longer runs without your confirmation; budget-capped runs are no longer retried; several smaller fixes from the code review.

### Added (round 3)
- **Review page:** finished runs that changed files wait for you with their diff: Accept, a one-step Revert (only when it is provably safe), Run checks (test command per project).
- **Add calendar events from the command bar:** "add a team sync Friday 3 pm" shows a preview; nothing is written until you press Add to calendar; Undo for 30 s.
- **Run guards:** a dollar cap per level for headless Claude runs, a wall-clock cap for any headless run, and a daily queue ceiling that pauses the queue until you resume it.
- **Better Claude signals:** permission requests, tool failures, API errors and subagent start/stop come straight from Claude Code's hook events.
- **Worktree runs (opt-in):** a Claude run can work on its own branch and folder and is merged back only when that is safe.
- **`jarviee --disable` / `jarviee --enable`:** take Jarviee out of your setup (service, hooks, status line) and put it back; `--dry-run` shows the changes.
- **Report volume in Settings:** how loud the voice plays and how loud everything else plays while it speaks, both as a share of full volume.

### Fixed (round 3)
- The voice report was quieted with everything else when a browser refused to be paused: the system volume was dropped and the voice boost could not exceed it. Now the voice plays at its level and other audio is paused or turned down at the app, then restored exactly.
- Dashboard settings other than on/off switches (voice, phone timing, muted calendars) were lost on every restart.
- A recycled process ID could keep a task "running" forever after a restart; a run whose process vanished could be marked done by one path and failed by another.

### Added
- **Claude limit alerts on the phone:** a push when the 5-hour limit is all spent and another when the cooldown is over (the second is scheduled on ntfy, so it arrives while the Mac sleeps). Settings → Phone → Claude limit alerts.
- **Command palette (⌘K):** every page, live agent, project, plan and action from one box.
- **Coordination panel** on the Command Center: the last combined report, which agent holds which files, and the agents' channel.
- **Draft plan editing** in Task Queue: change a task's level or tool, or drop it, before Approve.
- **SwiftBar menu-bar plugin:** `extras/swiftbar/jarviee.5s.py` (symlink it into your SwiftBar plugin folder).

### Changed
- **New look:** a JARVIS-style HUD. Cool blue-black, arc cyan for the system, amber only for what needs you.
- **Much lighter polling:** an unchanged dashboard poll is now a 304 of about 300 bytes (was about 219 KB and 500 ms every 2 s); background tabs poll every 6 s.
- `hub/server.py` and `hub/launcher.py` split into focused modules; old import paths still work.

### Fixed
- The queue no longer sticks after a service restart: orphaned running tasks are settled and leftover lock files swept.
- Done and failed voice phrases inside the cooldown are spoken afterwards, not dropped; a pipeline plan is reported once.
- `/api/plan/edit` task edits (they wrote a column that does not exist).
- Page loads no longer hit reset connections (listen backlog), and the command bar keeps its text box at the 420px side-window width.

## [Unreleased] - 2026-11-11

### Added
- **Calendar writes** (`apple_calendar.apply`, `calendar_events apply <spec>`): create, rename, recolor and delete calendars; create and delete events and repeating series; set how long a series repeats, through the same Jarviee Calendar app that reads, so no second app asks for access.
- Subscription heads-ups now look 5 days ahead (was 2), in spoken schedule answers and the phone's morning summary; days 2 to 5 read "on Sunday (in 4 days)". The calendar is read far enough ahead to see them, and a title that already says "subscription" isn't doubled.
- **Phone notifications (ntfy):** a push 15 min before each calendar event and timed reminder, and a 07:00 summary of the day (`hub/push.py`, Settings → Phone, `/api/settings/push`, `/api/push/test`, `/api/push/sync`). Alerts are queued on the ntfy server so they arrive while the Mac sleeps; moved or deleted items have theirs cancelled.
- **Run again and reply (R2-9):** finished launched runs get a reply box and "Run again" (optional level/tool) in the agent drawer; `POST /api/rerun`. Claude replies resume the same session (`--session-id` at launch, `--resume` on reply; `session_args` / `resume_args` in `config.json`), other tools re-run with the brief, log tail and reply (`hub/followup.py`).
- **Per-run file snapshots and Changes tab (R2-8):** `hub/snapshot.py` records the files each run changed; `/api/diff` shows that diff against the HEAD the run started from, including new files and after auto-commit.
- **Tick brain TODOs (R2-11):** "Done" on Projects-rail items edits `.brain/TODO.md` (exact-line match, atomic, undo): `hub/todo_edit.py`, `POST /api/todo/tick|undo`.

### Fixed
- A fast ⌘ Enter on a schedule question showed the answer for a moment and then wiped it: the preview still pending from typing landed afterwards. Launch now cancels it.
- Alerts tests no longer fail on Macs with `terminal-notifier` installed (they pin `notifier: osascript`).
- The Calendar / Reminders permission prompt kept coming back: the reader app was rebuilt (and re-signed ad hoc, a new identity to macOS) whenever the Swift file's mtime moved, including on git checkouts. It now rebuilds only when the source's content hash changes.
- Class sessions tagged "(Lab)" were flagged as assignments due; renewal titles read "Claude subscription renews ends on Sunday". Both fixed.
- Phone notifications failed with `CERTIFICATE_VERIFY_FAILED` on the python.org Python build (no CA bundle until "Install Certificates" is run). `hub/push.py` now falls back to certifi or macOS `/etc/ssl/cert.pem`, still verifying.
- **Launch / ⌘ Enter showed "Load failed":** `/api/ask` assigned `sched` inside `do_POST`, which made the scheduler a local name and crashed `/api/classify`, `/api/run` and the queue/actions endpoints with `UnboundLocalError` (dropped connection). Renamed to `agenda`. Requests that only mention the calendar ("notify me before a schedule", "automate…", "send…") now launch an agent instead of being answered from Calendar.
- Auto-commit no longer runs `git add -A`: it commits only the files the run changed, and queue tasks now do the same. Runs are marked `verified` (done + known files) so the launcher commit actually happens.
- agy runs that finish with exit 0 only because print mode soft-denied a shell command and changed no files are failed instead of marked done.

### Added
- **Auto pipeline for complex and heavy tasks:** instead of one agent, Jarviee runs research → plan → build → test → review, with up to 2 automatic fix rounds when tests or the review fail. Every phase is a visible agent in Workflows. "Single agent" toggle / `hub run --single` opts out. See README › Auto pipeline.

### Changed
- **Renamed Agent Hub → Jarviee:** `jarviee` command (the old `agent-hub` command still works and forwards to it), `Jarviee.app`, the launchd service, `~/.jarviee` data folder (moved automatically on the next server start, with a symlink left at `~/.agent-hub`), `JARVIEE_*` environment variables, `X-Jarviee-Token` header. Old names are still accepted. The folder and GitHub repo keep the `agent-hub` name.

### Added
- **Use whichever tool is available (Settings switch, on by default):** when Claude Code or agy is out of quota, signed out or missing, tasks go to the other tool at the same level instead of waiting. Fallback settings now persist across restarts in `~/.jarviee/settings.json`.

## [Unreleased] - 2026-11-09

### Added
- **Multi-Agent Orchestration View (`#/run/<id>`, "Runs" Tab):** Interactive visualization for multi-agent runs and plans. Top half renders a zoomable, pannable orthogonal run tree mapping Orchestrator $\rightarrow$ phase columns (`Plan`, `Build`, `Review`, `Brain sync`) $\rightarrow$ task cards with role badges, status chips, duration, cost, and sub-steps/retries/fallbacks. Bottom half features a resizable splitter with summary KPI bar, waterfall Gantt timeline with live "now" line, tokens and cost accounting tables per agent and per model, and a chronological event log.
- **Hook Event Spans Storage:** Added SQLite `spans` table capturing model turns, tool calls, and notification events with indexed lookups by `run_id`.
- **Local Token & Cost Accounting Engine:** Incremental parser reading local Claude JSONL session transcripts and agy transcripts with configurable pricing table in `config.json`.
- **Runs API & Static Serving:** Endpoints `/api/runs`, `/api/run/<id>/tree`, `/api/run/<id>/trace`, and `/static/<name>` serving static assets with path whitelist.
- **Cross-Tool Automatic Fallback:** Cross-tool failure detector and equivalence router mapping `agy:flash` ↔ `claude:haiku`, `agy:argon` ↔ `claude:sonnet`, and `claude:opus` → `claude:sonnet` / `agy:argon`. Re-routes quota-exhausted runs across tools with lineage tracking (`parent_run_id`, `hops`), git change warnings on partially touched files, and a 2-hop cap.
- **Quota Circuit Breaker:** SQLite `quota_state` table storing tool/model quota statuses and parsed reset expiry timestamps. Pre-flight dispatcher check routes directly to available models before spawning failed panes. Auto-restores on expiry, with probe endpoint (`POST /api/quota/probe`) to clear flags early.
- **Dashboard Quota Strip & Fallback Controls:** Header quota strip showing per tool/model status chips with live countdowns (`resets in Xd Xh`) and "Re-check" probe action. Fallback badge on agent cards showing hop chains (`agy:argon → claude:sonnet`). Settings modal with toggles for automatic fallback and protecting Claude Opus. Pre-flight preview reflects pre-flight fallback decisions.

### Fixed
- **Borderline Complex Tasks Dispatched to agy:** Fixed rule in `config.json` and `hub/dispatch.py` (`prefer_agy_on_borderline: false`) that erroneously dispatched complex tasks to agy.
- **Agent Approval Questions Dispatched as Tasks:** Blocked questions and approval prompts from launching as tasks via regex validation (`APPROVAL_PROMPT_RE`) and removed execution trigger from owner items in the project task list.
- **Hanging `script` Panes:** Made `hooks/pane_runner.py` non-interactive by attaching `stdin=subprocess.DEVNULL` and adding a 600s execution timeout.
- **macOS Background Service (`launchd`):** Added `hub service install | uninstall | status | restart | logs` managing a launchd plist in `~/Library/LaunchAgents` with `RunAtLoad: true`, `KeepAlive: true`, and standard output logging to `~/.agent-hub/logs/server.log`.
- **Stable PATH & Node Resolution:** The service pins the Python interpreter, excludes Homebrew Cellar and node_modules paths in favor of stable `/opt/homebrew/bin` symlinks, and puts the terminal's active Node version before `/opt/homebrew/bin`.
- **macOS App Launcher (`Agent Hub.app`):** Created `~/Applications/Agent Hub.app` with custom design-token-matched `.icns` icon and launcher script sharing startup logic with CLI.
- **`agent-hub` Terminal Command:** Added `bin/agent-hub` symlinked to `~/.local/bin/agent-hub`. Supports health checking, automatic launchd kickstarting or nohup fallback with PID file (`~/.agent-hub/server.pid`), stale PID recovery, `--no-open`, and subcommands `status` (exit 0 running / 1 stopped), `stop`, `restart`, `logs`, `open`, `run`, and `plan` with automatic project detection from `cwd` `.brain/`.
- **Localhost Security Lockdown:** Server enforces strict binding to `127.0.0.1` (refusing `0.0.0.0`), DNS rebinding protection via `Host` header checks, cross-site origin blocking (`Sec-Fetch-Site` and `Origin` validation), no CORS headers, and per-install random token stored at `~/.agent-hub/token` (`chmod 600`) required for all POST endpoints.
- **Health Check Endpoint:** Added `GET /api/health` returning server status, port, and process ID.
