# Jarviee demo (made-up data, for screenshots)

`demo_server.py` starts an isolated Jarviee with invented projects and runs, so screenshots for the README or a post
never show anyone's real work. It spends zero tokens: no real agent is ever launched.

```bash
python3 scripts/demo/demo_server.py            # http://127.0.0.1:7790  (--port N, --dir PATH, --keep)
```

Stop it with Ctrl+C. It deletes its throwaway folder (`/tmp/jarviee-demo`) unless you pass `--keep`. It never touches a
server on port 7777.

## What is isolated

- `HOME` points at `/tmp/jarviee-demo/home`, so `~/.jarviee`, `~/.claude`, `~/.gemini` and the macOS wallpaper store are
  empty or invented. The Keychain, Calendar, Mail and WhatsApp are never asked: every connector is off in the demo
  config, and the briefing, calendar and `/startwork` screens are not used.
- `PATH` is only a fake `agy` (it answers `agy -p /usage` with canned quota numbers) plus `/usr/bin:/bin`. Every tool in the
  config runs a harmless python command, so even a launched task prints one line and stops.
- `JARVIEE_USER_CONFIG=""` (no personal overlay), and ntfy push, voice, alerts, map auto-update, e-mail, WhatsApp, OmniRoute,
  the day-summary and mail-summary threads are off. `terminal.mode` is `background`.
- The workspace is five invented projects (`acme-storefront`, `weather-api`, `portfolio-site`, `habit-tracker-app`,
  `recipe-box`) with `.brain/` folders, `FLOW.json` maps, a `_BRAIN/PROJECT_STATUS.md` and tiny git repos.
- State is seeded through the real store: runs in every state (running, needs you, done, failed, handed to another tool),
  Claude (haiku, sonnet, opus) and Antigravity (Flash, Hydrogen, Neon) models, review items with diffs, board cards in every
  column, invented transcripts for the usage page. The Workflows page uses the built-in sample plan (`#/workflows/demo`).
- Appearance is "Plain dark": no wallpaper.

Known limit: the sample plan on the Workflows page names the project `agent-hub` and shows this repo's own file names
(it is built in `hub/rundemo.py`). Everything else is invented.

## Retake the screenshots

`take_screenshots.js` writes `docs/screenshots/*.png` (1600x1000, dark; two at 1280x800) from the running demo.
It needs Playwright, installed outside the repo:

```bash
mkdir -p /tmp/jarviee-pw && cd /tmp/jarviee-pw && npm init -y && npm i playwright@1.49.1 && npx playwright install chromium
cd <repo> && python3 scripts/demo/demo_server.py &        # wait a few seconds
NODE_PATH=/tmp/jarviee-pw/node_modules node scripts/demo/take_screenshots.js
```

(The committed screenshots were taken with the Playwright MCP tools using exactly these steps; the node script itself passed
a syntax check but its browser download did not finish on the machine that wrote it, so it has not been run end to end.)

Then **open every PNG** and check for anything real before publishing: names, `/Users/...` paths, e-mails, calendar events.
Seeded run times are relative to the start, so retake after restarting the demo.

## The 8-pane panel, the dashboard with 8 agents and a notification banner

`panel_demo.py` takes `docs/screenshots/14-orchestrate-panel.png` (the iTerm2 window with 8 panes at work),
`docs/screenshots/15-dashboard-8-agents.png` and `showcase/screenshots/notification-banner.png`. Still zero tokens:
every tool runs `fake_agent.py`, a fake agent CLI that only prints (header with the task and model, "Reading ...",
"Editing ... (+24 −6)", "Running tests ... 42 passed", a progress bar, a short summary; deterministic per task, no network).

```bash
python3 scripts/demo/panel_demo.py up       # demo server (port 7790) + a "Jarviee Demo" iTerm2 window, 8 fake agents running
python3 scripts/demo/panel_demo.py shots    # the panel, the banner and (with Playwright, see above) the dashboard
python3 scripts/demo/panel_demo.py down     # close the demo window, stop the server, remove /tmp/jarviee-demo
python3 scripts/demo/panel_demo.py all      # all three, with a wait before the pictures (--wait 60)
```

Options: `--duration 300` (seconds each fake agent works; take the pictures before they finish), `--display builtin|main|N`
(which screen the window fills), `--font-size 11`, `--no-dashboard`. The dashboard picture needs
`NODE_PATH=<dir>/node_modules` as for `take_screenshots.js`; without it the script says so and you save
`http://127.0.0.1:7790/` yourself (1600x1000). `take_panel_dashboard.js` also prints the browser console errors.
The 8 runs are one "Lead: ship the checkout redesign" and seven workers on the invented projects (Opus, Sonnet, Haiku,
Gemini 4 Hydrogen, Gemini 3.8 Flash and Gemini 4 Neon names).

What it needs: macOS, iTerm2 with Automation permission, and **Screen Recording permission** for the app that runs the
script (System Settings > Privacy & Security). Without it macOS returns a black or wallpaper-only picture: the script
refuses to save one and tells you. The banner needs **Notifications turned on for `terminal-notifier`**
(`brew install terminal-notifier`; Allow, style Banners, previews Always): with them off macOS refuses the banner and
the script says so. Closing the demo window pops iTerm2's "Close window? ... running 8 instances of Python" question;
the script presses its OK through System Events (Accessibility permission), or click OK on the "Jarviee Demo" window.

Safety (your real Jarviee keeps running untouched):
- Port 7790, never 7777; data in `/tmp/jarviee-demo`; the panes show `/tmp/jarviee-demo/projects/...`, no `/Users/...` path.
- The window is titled "Jarviee Demo". Jarviee names its window after its iTerm2 dynamic profile (`terminal.window_name` is
  not read by the hub), so `demo_server.py --iterm` points `hub.displays` at a separate "Jarviee Demo" profile
  (`~/Library/Application Support/iTerm2/DynamicProfiles/jarviee-demo.json`, deleted on exit). The real "Jarviee"
  profile is never rewritten.
- Before launching, `up` runs `demo_server.py --iterm --check` and refuses to continue unless the settings the server
  really loads are: port 7790, window "Jarviee Demo", mode iterm2, layout grid [4, 2], data under `/tmp/jarviee-demo`,
  every tool running `fake_agent.py`.
- The only window it ever closes is the one titled "Jarviee Demo" with the id the demo recorded.
- The screen the window fills is the built-in display by default, so it does not cover your main-screen windows.

**Open every PNG before using it.** A notification banner is translucent: whatever is behind it shows through as a blur.

