# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
One person: the owner, a CS student who runs many AI coding agents (Antigravity `agy` and Claude Code) at once across their personal projects. Jarviee is a personal tool. It can assume the owner's setup, projects and vocabulary, so it needs no onboarding for strangers.

The job: keep agents moving without babysitting each window. The owner needs to know which agents are waiting on them, what is running, and what just finished, and to start the next task on a model that fits it.

## Product Purpose
A local command center that:
1. classifies each task as basic / light / complex / heavy and launches the agent on the matching model and effort (cheap models for renames and typos, the strongest for refactors, debugging and architecture);
2. tracks every running agent through hooks, showing status, activity, model, tier, steps, elapsed time and "waiting for you";
3. reads the owner's project brain (`<Project>/.brain/` and the global `_BRAIN/` vault) for progress, owner tasks and status.

Success: at a glance the owner sees what needs them, so no agent sits idle waiting unnoticed, and heavy models aren't wasted on basic tasks.

## Positioning
Unlike each agent app's own window, Jarviee sees agents from both tools in one place, chooses the model before a session starts (hooks can't switch models mid-session), flags "overkill" when a heavy model runs a basic task, and ties every run to the owner's own project brain.

## Operating Context
- Used on a laptop / desktop browser only (owner, 2026-11-14: no mobile layout needed), open in a tab next to the editor and glanced at constantly rather than studied. Layouts are judged at desktop widths (about 1280px and up).
- Runs at `http://127.0.0.1:7777`, served by `hub.py serve`. It works offline.
- Data refreshes every 2 seconds by polling `/api/state`.
- Agents start either from the dashboard or `hub run`, or in the Antigravity / Claude Code apps, where hooks report them.
- Projects are workspace folders with `.brain/`. Progress comes from `TODO.md` markers (✅ done, ⬜ to do, 👤 needs the owner), and status, focus and blocker from `_BRAIN/PROJECT_STATUS.md`.

## Capabilities and Constraints
- **API contract (fields are only ever added; 2026-11-11 added `tool_status`, `dispatch`, `lanes_cfg` to `/api/state` and `GET /api/activity`):** `GET /api/state`, `GET /api/usage` (added 2026-11-09, cached 60 s), `GET /api/log?id=`, `POST /api/classify`, `/api/run`, `/api/stop` (now also returns `reason`), `/api/clear`. Fields are only ever added. Server and router logic stay as they are.
- **UI:** `hub/dashboard.html` (shell and icon sprite) plus plain modules in `hub/static/` served from `/static/` (no build step, no CDN, no external requests). System font stacks only. Routes: Command Center, Agents, Workflows, Task Queue, Knowledge, Analytics, Settings.
- **Backend:** Python standard library only, bound to 127.0.0.1.
- **Features that must survive any redesign:** the new-task box with live router preview (tier, model, effort, reasons) and tier override; tool and project selection; agent cards (tool, model, tier, suggested tier, overkill warning, activity, steps, elapsed time, project/workspace, Stop for launched runs, View log); grouping into Needs you / Running / Finished; Clear finished; the projects panel (progress, 👤 owner items, open tasks, status/focus/blocker, run-task-here shortcut); the read-only ecosystem status table; banners for a missing workspace or status file.
- **Terminology:** levels are `basic`, `light`, `complex`, `heavy`. Statuses are `running`, `waiting` (shown as "needs you"), `done`, `failed`, `stopped`. Tools are `agy`, `claude`, `echo` (a fake tool for testing). "Overkill" means a tier heavier than the router's suggestion.
- **Built:** macOS alerts (clickable via terminal-notifier) and phone push via ntfy when an agent needs you, fails or finishes.

## Brand Commitments
- Name: Jarviee.
- Look: dark glass over the owner's own desktop wallpaper (owner's call, 2026-11-15, from ten visionOS-style references; it replaced the graphite operations desk, which had replaced the cyan JARVIS HUD): the photo glows through frosted panes with specular rims, the app fills the window with the full labeled sidebar inside the glass, sentence-case headings, mono figures, and a full palette where each hue keeps one job: blue = running and the system, amber = needs you, green done, red failed or out, violet fallback, tier hues teal / lime / orchid / rose. Each KPI tile owns a hue with a mini chart of real data. Tools keep their own brand colour and real app logo (owner's call, 2026-11-14; local only): Claude orange, agy dark blue. Sidebar, KPI strip, routing graph Router → tool → model → job, critical-path workflow graph. No decorative illustration.
- Voice: terse and technical, like a good terminal or Linear. Short labels and exact facts, no chatter. Example: `agy · gemini-3.8-flash-low · 4 steps · 2m`.

## Evidence on Hand
- Real data comes from the live server: about 11 projects with `.brain/` folders and 7 rows in `_BRAIN/PROJECT_STATUS.md`. Agent cards appear only when agents run.
- There are no screenshots, logos or other brand assets, and none should be invented.

## Product Principles
1. What needs the owner comes first. Waiting agents and 👤 items must be impossible to miss, and everything else steps back.
2. Glanceable over complete. Every view answers "what changed, what needs me" within a second; details are one action away.
3. Honest about cost. Tier and model are always visible, and the router's reasoning is one hover or tap away.
4. Never in the way. Refreshes don't move content, steal focus or reset scroll.
5. Local and owned. It works offline and only reads the brain, except for appending run logs.

## Accessibility & Inclusion
- WCAG AA contrast in the dark theme (the only theme), with visible focus states.
- Fully usable from the keyboard: `/` or `n` focuses the new task, ⌘/Ctrl+Enter launches, Esc closes the log.
- Statuses and tiers are never shown by colour alone; each also has text or shape.
- Screen-reader friendly: proper landmarks and labels, and a polite live announcement when an agent starts waiting or finishes.
- Respects `prefers-reduced-motion`. Dark only, by the owner's choice (2026-11-11); there is no light theme.
