# Configuration

Jarviee reads its configuration from three places, in this order. Later ones win.

1. **`config.json`** in the repository: the shipped defaults. Do not edit it; a `git pull` would conflict.
2. **`~/.jarviee/config.json`**: your overrides. `python3 hub.py setup` creates it. Set `JARVIEE_USER_CONFIG=/path/to/file.json` to use another file, or `JARVIEE_USER_CONFIG=""` to turn the overlay off.
3. **`~/.jarviee/settings.json`**: the switches you flip in the dashboard's Settings page. Jarviee writes this file; you rarely edit it.

Environment variables:

| Variable | Effect |
| --- | --- |
| `JARVIEE_CONFIG` | Use another file instead of the repo's `config.json` as the base. |
| `JARVIEE_USER_CONFIG` | Path of your overlay (default `~/.jarviee/config.json`). Empty turns it off. |
| `JARVIEE_PORT` | Override `port`. |

Restart the server after editing a file (`python3 hub.py service restart`, or stop and rerun `serve`).

## How overrides merge

Your `~/.jarviee/config.json` is **deep-merged** over `config.json`:

- Objects merge key by key. You only write the keys you want to change.
- Lists and plain values **replace** the default whole. To add one keyword to `classifier.basic_keywords`, you must write the full list.
- A file that is not valid JSON, or not an object, is ignored with one warning on stderr.

Example: change the heavy model and one terminal setting, leave everything else:

```json
{
  "dispatch": {"heavy": {"tool": "claude", "model": "opus"}},
  "terminal": {"font_size": 10}
}
```

`python3 hub.py setup` writes the file with mode 600. Edits made by Jarviee itself (setup, the dashboard) keep your other keys.

## The dashboard settings file

The Settings page saves a small set of switches to `~/.jarviee/settings.json`. They are applied over the merged config at load time, and only these keys are accepted:

| Section | Keys |
| --- | --- |
| `fallback` | `enabled`, `never_fallback_to_opus`, `use_available_tool` |
| `omniroute` | `enabled` |
| `maps` | `auto_update` |
| `push` | `enabled`, `lead_min`, `digest_at`, `mute_calendars`, `limit_alerts` |
| `voice` | `enabled`, `tldr`, `report_volume`, `duck_volume` |
| `whatsapp_send` | `enabled` |
| `email_send` | `enabled` |
| `appearance` | `wallpaper` |

A value in `settings.json` wins over the same key in your overlay. Types must match the default (a switch stays true/false, a number stays a number), otherwise the saved value is ignored. Delete the file to go back to your config.

## Top-level keys

| Key | Default | What it does |
| --- | --- | --- |
| `port` | `7777` | Port of the dashboard and hook receiver. It always binds `127.0.0.1`. |
| `db_path` | `~/.jarviee/hub.db` | SQLite database (agents, plans, tasks, board). |
| `logs_dir` | `~/.jarviee/logs` | Run logs and server log. |
| `default_tool` | `"agy"` | Tool used when a run names none and dispatch does not decide. `setup` sets it when only one tool is installed. |
| `complex_job_duration_seconds` | `60` | The dashboard marks a job as complex when it runs longer than this, has at least `complex_job_min_steps` steps, or used more than one tool. |
| `complex_job_min_steps` | `2` | Steps needed for the same. |
| `finished_job_retention_seconds` | `300` | How long finished jobs stay in the active list. |
| `prefer_agy_on_borderline` | `false` | Send a task between light and complex to agy first. |
| `settings_path` | `~/.jarviee/settings.json` | Where dashboard switches are saved. |
| `token_path` | `~/.jarviee/token` | Per-install API token file. |
| `wallpaper_dir` | `~/.jarviee/wallpaper` | Folder for the dashboard's wallpaper frames. |

## Sections

### `obsidian`: your projects

| Key | Default | What it does |
| --- | --- | --- |
| `workspace_root` | `~/Projects` | The folder that holds your projects. Each direct subfolder with a `.brain/` is a project. `setup` writes this. |
| `global_brain` | `_BRAIN` | Optional folder in the workspace root for cross-project notes. Jarviee only reads it. |
| `project_brain_dir` | `.brain` | Name of each project's notes folder. |
| `context_files` | `PROJECT_CONTEXT.md`, `TODO.md`, `DECISIONS.md` | Notes added to a run's prompt. |
| `status_file` | `_BRAIN/PROJECT_STATUS.md` | Optional status table, read for focus and blockers. |
| `runs_file` | `AGENT_RUNS.md` | File in `.brain/` that finished runs are appended to. |
| `inject_project_note` | `true` | Add the project notes to prompts. |
| `max_context_chars` | `6000` | Cap on the injected notes. |

### `tools`: agent CLIs

One entry per CLI. Shipped: `agy`, `claude`, `omniroute` (fallback only), `echo` (a fake agent for testing).

| Key | What it does |
| --- | --- |
| `command` | Argument list that starts the tool. Placeholders: `{prompt}`, `{model}`, `{effort}`, `{session}`. |
| `bridge_command` | (agy) Used when `agy -p` prints nothing on a non-terminal. Needs `agy-headless-bridge` on `PATH`. |
| `models` | Model per level: `basic`, `light`, `complex`, `heavy`. |
| `efforts` | Starting effort per level. |
| `effort_levels` | Efforts each model family accepts. `"*"` is the default. An empty list means the model sets its own effort. |
| `effort_in_model` | (agy) Put the effort in the model slug (`gemini-4-hydrogen-<effort>`) instead of a flag. |
| `effort_args` | Arguments added when a run has an effort (`["--effort", "{effort}"]`). |
| `model_preference` | (agy) Slug templates tried in order, for example when a quota bucket is empty. |
| `session_args`, `resume_args` | Arguments that start and resume a named session, so a reply continues the conversation. |
| `env_from` | (omniroute) Takes the gateway URL and key from the `omniroute` section. |

Both shipped commands use auto-approve modes: `agy ... --mode auto` and `claude ... --permission-mode auto`. Change them here if you want agents to ask first. Be aware that headless runs cannot answer a permission prompt, so they will fail instead of asking.

### `dispatch`

Which tool and model each level goes to when you do not choose.

```json
"dispatch": {
  "basic":   {"tool": "agy",    "model": "gemini-3.8-flash-low"},
  "light":   {"tool": "agy",    "model": "gemini-4-hydrogen-medium"},
  "complex": {"tool": "claude", "model": "sonnet"},
  "heavy":   {"tool": "claude", "model": "opus"}
}
```

If `model` is empty, the tool's `models.<level>` is used.

### `lanes`

Concurrent queued tasks per tool. Default `{"agy": 2, "claude": 1}`. Set a tool to `0` to turn its lane off.

### `classifier`

How a task gets its level.

| Key | What it does |
| --- | --- |
| `basic_keywords`, `light_keywords`, `complex_keywords`, `heavy_keywords` | Words and phrases that vote for a level. |
| `heavy_word_count` | A brief at least this long (words) leans heavy. Default 60. |
| `basic_max_word_count` | A brief longer than this is not basic. Default 20. |
| `llm_fallback` | `{"enabled": false, "timeout_s": 3}`. Ask a small model when the keywords are undecided. Off by default. |

### `fallback`, `usage_routing`, `escalation`, `retry`, `quota_patterns`

| Key | Default | What it does |
| --- | --- | --- |
| `fallback.enabled` | `true` | Move a task to the other tool when a quota is out or a CLI is missing. |
| `fallback.never_fallback_to_opus` | `true` | A fallback never lands on the most expensive Claude model. |
| `fallback.use_available_tool` | `true` | Prefer a tool that is installed and has quota. |
| `fallback.max_hops` | `2` | Most tools one task may move through. |
| `fallback.equivalents` | see file | Per model, the models to try next (`"claude:sonnet": ["agy:..."]`). |
| `usage_routing.claude_weekly_pct` | `95` | A Claude weekly window at or above this percent counts as out of quota for routing. |
| `escalation` | `to_level: complex`, `to_tool: claude`, `max: 1` | A failed or "blocked" agy run is re-queued once on this level and tool, with `log_tail_chars` (4000) of the log. `blocked_patterns` are the phrases that mean "blocked". |
| `retry` | `{"enabled": true, "max": 3}` | An interactive run that genuinely fails is started again, up to `max`. |
| `quota_patterns` | list | Phrases in a run's output that mean a quota or rate limit. |

### `omniroute`

Optional last-resort gateway. See [INTEGRATIONS.md](INTEGRATIONS.md#omniroute-fallback).

| Key | Default |
| --- | --- |
| `enabled` | `false` |
| `base_url` | `http://127.0.0.1:20128` |
| `keychain_service` | `jarviee-omniroute` |
| `health_path` | `/v1/models` |
| `health_timeout_s` | `2` |
| `health_cache_s` | `30` |

### `planner`, `review`, `pipeline`

| Section | Key | Default | What it does |
| --- | --- | --- | --- |
| `planner` | `tool`, `model` | `claude`, `opus` | The read-only agent that splits a goal for `plan` and `/pipeline`. |
| | `context_files` | `PROJECT_CONTEXT.md`, `ARCHITECTURE.md`, `TODO.md` | Notes it reads. |
| | `max_context_chars`, `timeout` | `12000`, `300` | Caps. |
| `review` | `enabled`, `tool`, `model` | `true`, `claude`, `sonnet` | One read-only review after a plan finishes. |
| | `skill`, `timeout` | `code-reviewer`, `600` | The Claude skill to use if it is installed, and the time limit. |
| `pipeline` | `enabled` | `true` | Allow `/pipeline`. |
| | `planner` | `claude` | Tool that plans. |
| | `research`, `analysis`, `fact_check`, `test` | each `{"enabled": ..., "level": ...}` | Phases and the level each runs at. Research, analysis and fact check are on; test is off. |
| | `review` | `{"enabled": true}` | Final review phase. |
| | `max_fix_rounds` | `2` | Fix rounds when test or review fails. |
| | `research_chars` | `6000` | How much research text goes into later phases. |
| | `skills` | all empty | Skill name per phase (`research`, `analysis`, `plan`, `fact_check`, `build`, `review`). A skill is used only if it is installed for the tool that runs the phase. An empty name means none. |

### `terminal` and `dashboard_window`

| Key | Default | What it does |
| --- | --- | --- |
| `terminal.mode` | `iterm2` | `iterm2` opens live panes; `background` runs without panes (use this without iTerm2). |
| `terminal.window_name` | `Jarviee` | Name of the iTerm2 window. |
| `terminal.layout` | `split` | `split` tiles panes as runs start. `grid` gives every run a free pane of a 4×2 grid. `/orchestrate` always uses a grid tab. |
| `terminal.max_panes_per_tab` | `6` | Panes per tab in `split` layout. |
| `terminal.grid` | `[4, 2]` | Columns × rows of the grid. |
| `terminal.font_size` | `null` | Pane font size; `null` keeps your profile's. |
| `terminal.display` | `main` | `main`, `builtin`, `secondary` or a screen index. |
| `terminal.fullscreen` | `false` | Open the window in native full screen. |
| `terminal.position`, `width_pct` | `right`, `60` | Where the window goes and how wide, when not full screen. |
| `terminal.focus_on_launch` | `true` | Bring a pane forward when a run starts in it. |
| `terminal.close_on_finish` | `never` | Whether a pane closes when its run ends. |
| `terminal.interactive`, `queue_interactive` | `true` | Run agents as live CLI sessions, for launched and for queued runs. |
| `dashboard_window.browser` | `default` | `default` hands the URL to your default browser; `safari` opens a dedicated Safari window. |
| `dashboard_window.display` | `main` | Screen for that window. |

### `alerts`

Mac banners and sounds when an agent needs you, fails or finishes. Not in the shipped `config.json`; these are the code defaults.

| Key | Default | What it does |
| --- | --- | --- |
| `enabled`, `banner`, `sound` | `true` | Master switch, banner, sound. |
| `events` | `waiting`, `failed`, `done` | Which status changes alert. |
| `sounds` | `Glass`, `Basso`, `Hero` | macOS sound per event. |
| `cooldown_s` | `20` | Minimum time between alerts for one agent. |
| `quiet_hours` | none | For example `["22:00", "08:00"]`: no sounds, banners stay. |
| `combined` | `true` | One report per batch of agents. |
| `phone_away_s`, `phone_min_s`, `phone_min_steps` | `120`, `120`, `5` | Phone pushes for agents only after you have been away this long, and only for runs that took this long or this many steps or changed a file. |

### `push`, `briefing`, `email_send`, `whatsapp_send`, `voice`

Connectors. Each is off until you enable it. See [INTEGRATIONS.md](INTEGRATIONS.md).

| Section | Defaults |
| --- | --- |
| `push` | `enabled: false`, `server: https://ntfy.sh`, `lead_min: 15`, `digest_at: "08:00"`, `digest_enabled: true`, `check_s: 300`, `ahead_h: 36`. Also `limit_alerts`, `mute_calendars`, `icons`. |
| `briefing` | `sources` (`calendar`, `gmail`, `linkedin`, `github`, `whatsapp`, `agents`), `phone: true`, `timeout_s: 15`, plus `gmail`, `github` and `whatsapp` sub-sections. `gmail.address` is empty until you run `connect-gmail`. |
| `email_send` | `enabled: false`, `to: ""` (yourself), `kinds`. |
| `whatsapp_send` | `enabled: false`, `kinds`, `max_chars: 1500`. |
| `voice` | `enabled: false`, `engine: "piper"`, `voice: "Daniel"`, `rate: 175`, `events`, `muted`, `debounce_s`, `tldr_llm`, ducking and volume keys. |

### `maps`

Project maps (`.brain/FLOW.json`).

| Key | Default | What it does |
| --- | --- | --- |
| `auto_update` | `false` in the shipped config | When on, an agent updates a map after the project's code has changed and been quiet for a while. Each update uses tokens. |
| `check_s` | `300` | How often to look for stale maps. |
| `settle_s` | `600` | Quiet time with no new commit before an update starts. |
| `cooldown_s` | `10800` | Minimum time between updates of one project. |
| `max_per_day` | `8` | Most updates per day, over all projects. |

### `board`

`board.polish` controls the "Improve with AI" rewrite of board cards: `{"enabled": true, "timeout": 45, "agy_timeout": 60}`.

### `appearance`

`appearance.wallpaper`: `"desktop"` (your macOS wallpaper behind the dashboard glass) or another named wallpaper. Set from Settings.

### `pricing`

Optional. `{"models": [{"match": "sonnet", "input": 3, "output": 15, "cache_read": 0.3, "tool": "claude"}]}` gives USD-per-million-token prices for run cost estimates (use your own current prices). Without it, Claude models use built-in first-party rates and agy runs show tokens only, unless a row has `"tool": "agy"`.

## Where Jarviee keeps data

Everything is under `~/.jarviee` unless you move it: `hub.db`, `logs/`, `token`, `config.json`, `settings.json`, `alerts.json`, `push.json`, the usage readings, and optional voice models in `tts/`. Remove the folder to reset Jarviee completely.
