# Architecture

A short tour of how Jarviee is put together. It is a single local Python process (standard library only) plus a plain JavaScript dashboard. Everything runs on your machine.

```
 Claude Code / Antigravity sessions                 you
   │  hooks (hooks/report.py, 2 s, never blocks)     │
   │                                                 │ browser
   ▼                                                 ▼
 ┌──────────────────── Jarviee server (127.0.0.1:7777) ────────────────────┐
 │  POST /hook ──► ingest ──► store (SQLite)  ◄── GET /api/state (2 s poll) │ ──► dashboard
 │                               ▲                                          │     (hub/static/)
 │  POST /api/run, /api/plan     │                                          │
 │        │                      │                                          │
 │        ▼                      │                                          │
 │   router ──► launcher ──► scheduler / queue ──► run job ──┐              │
 │  (level,      (plan,       (2 lanes, deps,      │          │              │
 │   model,       prompt)      file claims)        │          ▼              │
 │   effort)                                       │   iTerm2 pane  or  headless process
 │                                    coord (claims) │   (hooks/pane_runner.py)  (claude / agy)
 │  watchers: alerts · push · usage · map updates · mail summaries          │
 └───────────────┬──────────────────────────────────────────────────────────┘
                 ▼
        ~/.jarviee  (db, logs, token, config, settings)      <project>/.brain/
```

## Components

| Part | Where | What it does |
| --- | --- | --- |
| CLI | `hub.py`, `bin/jarviee` | Commands for serving, running, planning, setup and checks. `run` talks to the server when it is up and runs inline otherwise. |
| Config | `hub/config.py`, `config.json` | The shipped defaults, your overlay in `~/.jarviee/config.json`, and dashboard switches in `~/.jarviee/settings.json`. See [CONFIGURATION.md](CONFIGURATION.md). |
| Server | `hub/server.py`, `hub/routes.py` | `ThreadingHTTPServer` bound to `127.0.0.1`. Host, Origin and Sec-Fetch-Site checks, and a per-install token on every POST. One decorated function per endpoint. |
| Hooks | `hooks/report.py`, `hooks/statusline.py`, `hub/install.py` | Small scripts the agent CLIs call on session events. They forward the event to the server and always exit 0. `install-hooks` merges them into each tool's config, with a backup. |
| Router | `hub/router.py` | `classify(task)` returns a level and the reasons. It also picks the effort and fits it to the chosen model. |
| Dispatch and fallback | `hub/dispatch.py`, `hub/fallback.py`, `hub/usage.py` | Maps a level to a tool and model, skips a tool that is missing or out of quota, and finds the next equivalent model. |
| Launcher and runs | `hub/launcher.py`, `hub/runplan.py`, `hub/runjob.py` | Builds the prompt and command line, starts the agent, watches the exit, and decides whether to retry, escalate or fall back. |
| Queue and scheduler | `hub/tasks.py`, `hub/scheduler.py`, `hub/planner.py`, `hub/pipeline.py` | Plans and tasks in SQLite, two lanes (agy, claude), dependency and file-claim checks. `/pipeline` is a state machine over normal queued tasks. |
| Panes | `hub/terminal.py`, `hub/tiling.py`, `hub/panes.py`, `hub/panerun.py`, `hooks/pane_runner.py` | The iTerm2 window: one pane per run, split or 4×2 grid layout. |
| Orchestrate | `hub/orch.py`, `hub/orch_cli.py` | A lead session and up to seven worker windows in one grid tab. |
| Coordination | `hub/coord.py`, `hub/snapshot.py`, `hub/gitops.py` | File claims between agents, a per-run record of changed files, and commits of only those files. |
| Store | `hub/store.py` | SQLite: agents, plans, tasks, board cards, claims. |
| Project brains | `hub/obsidian.py`, `hub/brain_init.py`, `hub/projmap.py`, `hub/mapwatch.py` | Read each project's `.brain/`, inject notes into prompts, append run lines, and keep `.brain/FLOW.json` maps. |
| Connectors | `hub/push.py`, `hub/briefing.py`, `hub/mailout.py`, `hub/wamsg.py`, `hub/voice.py`, `hub/omniroute.py` | Optional integrations. See [INTEGRATIONS.md](INTEGRATIONS.md). |
| Dashboard | `hub/dashboard.html`, `hub/static/` | Plain HTML, CSS and JavaScript modules. No build step, no CDN, no external requests. |

## Request flow

**Watching agents (hooks → server → dashboard).**

1. A Claude Code or Antigravity session fires a hook event (session start, prompt, tool use, stop, notification).
2. `hooks/report.py` posts it to `127.0.0.1:<port>/hook` with a 2 second timeout, swallows any error and exits 0. If the server is down, nothing happens and the agent is unaffected.
3. The server normalizes the event and updates the `agents` table in SQLite: status, activity, model, steps, process id.
4. The dashboard polls `GET /api/state` every 2 seconds (conditional GET, so unchanged data costs almost nothing) and re-renders what changed.
5. Watchers read the same table. When an agent enters `waiting` or `failed`, the alert, phone and voice modules fire if you enabled them.

**Launching work (command bar or `hub run`).**

1. `POST /api/run` carries the task text, an optional tool, level and project.
2. The router classifies the task. Your overrides win. The dispatch table gives a tool and model, and usage readings may move it to the other tool.
3. The launcher builds the prompt: the task, the project's `.brain/` notes (capped), and hand-off text from any agent it had to wait for.
4. The scheduler or run job waits for a free lane and for files other agents have claimed, then starts the agent in an iTerm2 pane or as a background process.
5. When the process exits, the run job decides: done, retry, escalate (agy to Claude), or fall back (tool to tool, then OmniRoute if enabled). It records changed files, makes a commit of only those files if the run changed any, and appends a line to `.brain/AGENT_RUNS.md`.
6. The run lands in the Review inbox until you accept, dismiss or revert it.

**Slash commands** are a small registry (`hub/slash.py`). Some are answered directly without a model (`/help`, `/status`, `/standup`, `/usage`); `/map`, `/pipeline` and `/orchestrate` start agents; `/startwork` and `/endwork` build a report from your connected sources.

## Launcher, queue and scheduler

Every run, whatever started it, goes through `launcher.launch`, so it shows on the dashboard the same way. Direct runs start at once (subject to file claims). Planned work (`plan`, `/pipeline`) is split into tasks stored in SQLite. The scheduler wakes regularly, and starts a task when:

- all of its dependencies are done;
- its lane has capacity (`lanes`, default agy 2, claude 1);
- no running task has claimed one of its files, in either lane;
- the queue is not paused.

Each task prompt lists the acceptance criteria and the files the agent must not touch. When a plan finishes, one read-only review runs over the combined changes.

## Panes

With iTerm2, each run gets a real terminal pane running `hooks/pane_runner.py <run_id>`. The pane fetches its run spec from the server once (`/api/run-spec`, token-protected), runs the agent on its own pty, and reports start, exit and close events back. The prompt and environment travel over this local HTTP call, never through AppleScript. Window state lives in `~/.jarviee/terminal.json`, so a restarted server finds the same window.

Layouts: `split` tiles panes as runs start; `grid` keeps a 4×2 grid of free panes that runs fill; `/orchestrate` always opens its own grid tab. Without iTerm2, `terminal.mode: "background"` runs everything headless and the dashboard log still works.

## Coordination and claims

Before a run starts, it registers its task and the files it expects to touch (`hub/coord.py`). Another agent in the same workspace that overlaps either waits until the claim is released, and then starts with the first agent's result handed to it. Claims are also written as `.brain/.locks/<task>.lock` for queued tasks and removed when the task ends. Agents you start yourself in the apps are registered from their first prompt. Optionally a Claude run can work in its own git worktree so two agents never share a folder.

## Data in `~/.jarviee`

| Path | Holds |
| --- | --- |
| `hub.db` | Agents, plans, tasks, board cards, claims |
| `logs/` | One log per run, and the server log |
| `token` | Per-install API token (mode 600) |
| `config.json`, `settings.json` | Your config overlay and dashboard switches |
| `alerts.json`, `push.json`, `limit-watch.json`, `map-watch.json` | Small state files for alerts, phone push, limits and map updates |
| `claude-limits.json` | Claude plan limits, written by the status-line script |
| `mailsum.json` | Cached email gists (mode 600) |
| `terminal.json` | iTerm2 window and pane state |
| `tts/`, `wallpaper/` | Optional voice models and wallpaper frames |

Remove the folder to reset everything. In your projects, Jarviee writes only inside `.brain/` (see the README).

## Design rules

These keep the project small and safe; they are also the rules for contributions ([CONTRIBUTING.md](../CONTRIBUTING.md)).

- Python standard library only; the dashboard has no build step and loads nothing from the network.
- Hooks observe and report. They never block, gate or change an agent.
- The server listens on `127.0.0.1` only and treats every browser request as untrusted until the Host, Origin and token checks pass. See [SECURITY.md](../SECURITY.md).
- Switching models happens at launch, because hooks cannot change the model of a running session.
