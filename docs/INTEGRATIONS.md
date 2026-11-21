# Integrations

Everything on this page is optional and **off until you turn it on**. Jarviee ships with no keys or accounts: each integration uses your own account, and secrets go in the macOS Keychain, not in files. Nothing is sent to the author.

Settings can be put in `~/.jarviee/config.json` (see [CONFIGURATION.md](CONFIGURATION.md)) or switched in the dashboard's Settings page.

| Integration | What it needs | What leaves your Mac |
| --- | --- | --- |
| [Mac alerts](#mac-alerts) | nothing | nothing |
| [Phone push (ntfy)](#phone-push-ntfy) | the ntfy app | counts, names and event titles, to ntfy.sh (or your server) |
| [Gmail briefing](#gmail-briefing) | Google App Password | mail text goes only to the model you already use, for one-line gists |
| [Apple Calendar and Reminders](#apple-calendar-and-reminders) | macOS permission | event titles to ntfy, if phone push is on |
| [WhatsApp Desktop reader](#whatsapp-desktop-reader) | WhatsApp Desktop | nothing |
| [Summaries by email or WhatsApp](#summaries-by-email-or-whatsapp) | Gmail, or CallMeBot | the summary text |
| [Voice](#voice) | nothing (`say`), or Piper / Kokoro | nothing |
| [OmniRoute](#omniroute-fallback) | a local OmniRoute gateway | prompts go to the gateway you run |
| [Usage and quota](#usage-and-quota) | nothing | nothing |
| [Glance API and SwiftBar](#glance-api-and-swiftbar) | optional SwiftBar | nothing |

## Mac alerts

Jarviee watches agent statuses and shows a macOS banner and plays a sound when an agent starts waiting for you, fails or finishes. It is on by default and needs no setup.

- Triggers: an agent changes to `waiting`, `failed` or `done`. Stopped and cleared runs never alert.
- Sounds: `Glass` (waiting), `Basso` (failed), `Hero` (done), played with `afplay`.
- The **Alerts** switch in the dashboard header mutes everything. It is saved in `~/.jarviee/alerts.json`.
- A 20 second cooldown applies per agent. In `quiet_hours` (for example `["22:00", "08:00"]`) sounds are silenced and banners stay.
- With `terminal-notifier` installed, banners are clickable. Otherwise macOS may list them under **Script Editor**; allow it in System Settings › Notifications.

```json
"alerts": {"enabled": true, "banner": true, "sound": true, "cooldown_s": 20, "quiet_hours": ["22:00", "08:00"]}
```

## Phone push (ntfy)

A push to your phone 15 minutes before each Apple Calendar event and each timed reminder, plus your day's schedule every morning at 08:00, and alerts when an agent needs you or fails while you are away. It uses [ntfy](https://ntfy.sh): free, and no account is needed.

1. Install the **ntfy** app (iOS or Android), tap **+**, and subscribe to the topic shown in **Settings → Phone** (**Copy topic**).
2. Turn on **Heads-up on your phone**, then press **Send test**.

How it works:

- Every 5 minutes Jarviee reads the next three days of your calendar and hands each alert in the next 36 hours to the ntfy server as a scheduled message, so it arrives on time even while the Mac sleeps. A moved, deleted or completed item has its queued alert cancelled. An alert whose time passed while the Mac slept goes out at once if the item has not started.
- The **08:00 schedule** is separate from `/startwork`. It lists today's events, then reminders and deadlines. A day with nothing on still gets a message. Change the time in Settings or with `push.digest_at`; `push.digest_enabled: false` turns it off.
- All-day items and reminders without a time appear only in the morning schedule.
- Turning push off takes back every alert still queued.
- Agent alerts go to the phone only when you have been away from the Mac for a while, and only for runs that took a while, took many steps or changed a file (`alerts.phone_away_s`, `phone_min_s`, `phone_min_steps`).

**Privacy.** The topic is a random name kept in `~/.jarviee/push.json` (mode 600). Anyone who knows the topic can read the alerts, so never post it publicly. Event titles and places are sent to ntfy.sh. Set `push.server` to a self-hosted ntfy server to avoid that. The phone only receives counts and names: never an email subject, a message text or a code.

```json
"push": {"enabled": false, "server": "https://ntfy.sh", "lead_min": 15, "digest_at": "08:00", "digest_enabled": true, "check_s": 300, "ahead_h": 36}
```

Each kind of notification has its own emoji in front of the title, so you can tell them apart:

| Icon | Kind | Icon | Kind |
| --- | --- | --- | --- |
| 🤖 | agents finished | 📧 | Gmail |
| 🙋 | an agent needs you | 💼 | LinkedIn |
| 🚨 | an agent failed | 🐙 | GitHub |
| 📅 | event starting soon | 💬 | WhatsApp |
| ⏰ | reminder due | 🌅 | `/startwork` briefing |
| 📆 | morning calendar summary | ⏳ / 🔋 | Claude 5-hour limit spent / back |

Override an icon with `"push": {"icons": {"gmail": "envelope"}}` (any [ntfy emoji short code](https://docs.ntfy.sh/emojis/)).

## Gmail briefing

`/startwork` can list your unread Primary mail from the last two days, read-only over IMAP (nothing is marked read). Each email gets a one-line gist and, when it needs something from you, a to-do.

Set up:

1. Turn on Google 2-Step Verification for the account.
2. Make an [App Password](https://myaccount.google.com/apppasswords).
3. Run `python3 hub.py connect-gmail you@gmail.com` and paste the App Password. It is stored in the macOS Keychain as `jarviee-gmail`. Nothing is written to a file.
4. `python3 hub.py service restart` if the background service is running.

Check it with `python3 hub.py briefing`.

**Gists.** The first part of each email (links removed, quoted replies dropped, at most 1,500 characters) goes to a small model with no tools: Claude Haiku (`claude -p --tools ""`), or, if that fails, an agy Flash model in read-only mode in an empty folder. That model call uses your own plan. The prompt marks email text as untrusted, and only the model's JSON answer is read. With no model, the gist is the email's first sentence. Gists are cached in `~/.jarviee/mailsum.json` (mode 600, 14 days). A background thread summarizes new mail every 10 minutes so `/startwork` rarely waits.

**LinkedIn.** LinkedIn has no personal notification API, so the briefing reads LinkedIn's own emails in the same Gmail account (messages, invitations, mentions, jobs). Turn LinkedIn email notifications on in LinkedIn's settings.

**GitHub.** Unread notifications come from `gh api notifications`. Run `gh auth login` first.

What you hear and see: senders, subjects, gists, to-dos, chat and repo names. Email text stays on your Mac apart from the model call. A source that is not set up says how to connect it and is left out.

```json
"briefing": {"sources": ["calendar", "gmail", "linkedin", "github", "whatsapp", "agents"], "phone": true, "timeout_s": 15,
             "gmail": {"address": "", "days": 2, "max": 30, "summaries": true, "summary_wait_s": 40, "spoken": 5}}
```

Remove a source from `sources` to leave it out. Delete the Keychain item `jarviee-gmail` to disconnect Gmail.

## Apple Calendar and Reminders

Jarviee reads Apple Calendar and Reminders through a small Swift helper compiled on first use (`hub/calendar_events.swift`). macOS asks for Calendar and Reminders access the first time. It feeds the briefing, the phone alerts and the 08:00 schedule, and it can add events.

Google and other calendars appear if you add the account in System Settings › Internet Accounts. In Settings you can mute individual calendars from phone alerts (`push.mute_calendars`). Compiling needs the Xcode command line tools (`xcode-select --install`).

## WhatsApp Desktop reader

WhatsApp has no API for personal accounts. If you use WhatsApp Desktop, the briefing can read unread chats from its local database: a read-only copy, the last 7 days, people first. Nothing leaves your Mac.

Install WhatsApp Desktop and link it. If macOS blocks access, allow it when asked, or give the Python you run Full Disk Access. Set `briefing.whatsapp.days` to change the window, or remove `whatsapp` from `briefing.sources`.

## Summaries by email or WhatsApp

By default summaries (the 08:00 schedule, the `/startwork` briefing, the `/endwork` report) go to ntfy. You can send them elsewhere:

```bash
python3 hub.py summaries email      # your own Gmail, to yourself
python3 hub.py summaries whatsapp   # CallMeBot
python3 hub.py summaries ntfy       # back to the default
```

Quick alerts (event in 15 minutes, an agent needs you or failed, limit alerts) always stay on ntfy.

**Email.** Uses the Gmail connection above and Gmail's SMTP with the same App Password (`hub/mailout.py`). Every message has "Jarviee" in the subject and an `X-Jarviee` header, and the briefing skips them when it reads your inbox. Send to another address with `"email_send": {"to": "other@example.com"}`.

**WhatsApp (CallMeBot).** [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/) is a free third-party relay, so it sees the message text. Messages carry the same thin content as phone notifications: counts and names, never email text.

1. Add the WhatsApp number shown on CallMeBot's page to your contacts and send it: `I allow callmebot to send me messages`.
2. It replies with an API key. Run `python3 hub.py connect-whatsapp +15551234567` (your own number) and paste the key. It is stored in the Keychain as `jarviee-callmebot`, and a test message arrives.
3. `python3 hub.py service restart`.

WhatsApp messages cannot be queued ahead like ntfy's, so the 08:00 schedule is sent when Jarviee is running at 08:00, or as soon as the Mac wakes, up to five hours late.

## Voice

Jarviee can read reports aloud: when agents finish, `/startwork` and `/endwork`. It is off by default. Turn it on in Settings or with `"voice": {"enabled": true}`.

| Engine | `voice.engine` | Needs |
| --- | --- | --- |
| macOS `say` | `say` | nothing. Pick the voice with `voice` (for example `"Samantha"`) and `rate`. |
| Piper | `piper` | a Piper install and a voice model under `~/.jarviee/tts` (`hub/tts_piper.py`) |
| Kokoro | `kokoro` | a Kokoro install under `~/.jarviee/tts`, voice set with `kokoro_voice` (`hub/tts_kokoro.py`) |

Piper and Kokoro run from their own Python environment in `~/.jarviee/tts`, outside Jarviee's standard-library code. If the chosen engine is not available, Jarviee falls back to `say`.

Useful keys: `events` (which status changes are spoken), `muted`, `debounce_s`, `piper_speed`, `report_volume`, and `duck_enabled` / `duck_mode` / `duck_volume` to lower or pause your music while it speaks. `tldr_llm: true` makes a quick Claude Haiku call write the spoken summary (it counts against your Claude plan); `false` uses the end of the run's own output instead. Spoken text is stripped of URLs, tokens and secrets. Speech to text for dictation uses Handy or whisper.cpp if you have one installed.

## OmniRoute fallback

When **both** Antigravity and first-party Claude quotas are exhausted, Jarviee can route tasks to a local [OmniRoute](https://www.npmjs.com/package/omniroute) gateway as a last resort instead of waiting or failing. OmniRoute is a self-hosted gateway that multiplexes upstream providers and fails over between them with "combos". It exposes Anthropic-compatible endpoints, so Jarviee runs these tasks through the Claude Code CLI with `ANTHROPIC_BASE_URL` pointing at the gateway. Panes, hooks and logs work as usual. It is off by default.

1. Install and start OmniRoute (`npm install -g omniroute`, then `omniroute`; it listens on `http://127.0.0.1:20128`).
2. In its dashboard, connect your providers and create four combos matching Jarviee's levels: `jarviee-basic`, `jarviee-light`, `jarviee-complex`, `jarviee-heavy`. A task keeps its level when it falls back.
3. Create an API key in OmniRoute (Dashboard → API Manager), then run `python3 hub.py omniroute connect`. The key goes into the Keychain as `jarviee-omniroute`.
4. Check with `python3 hub.py omniroute test` and `python3 hub.py omniroute status`.
5. Turn on **OmniRoute as last resort** in Settings › Routing, or set `"omniroute": {"enabled": true}`.

`omniroute` is not in `dispatch` or `lanes`; it is never a first choice. Queued tasks that fall back to it use their own one-slot lane.

Safety: a run with no OmniRoute key fails at launch with a clear message and can never fall through to your personal Claude account. `ANTHROPIC_API_KEY` is removed from those child processes, and any echoed gateway token is scrubbed from run logs. The Claude status line ignores OmniRoute sessions, so they never change your Claude limit readings.

## Usage and quota

`GET /api/usage` (cached for 60 seconds) feeds the KPI strip and the Analytics page. Every value is read locally; what cannot be read shows "—" with the reason.

| Value | Source |
| --- | --- |
| Claude tokens, estimated cost, tokens by model, 5-hour block | `~/.claude/projects/**/*.jsonl` (cost at first-party API rates) |
| Claude 5-hour and weekly limits | `~/.jarviee/claude-limits.json`, written by `hooks/statusline.py` |
| agy quota and resets | `agy -p /usage --output-format json` |
| agy tokens | agy's local transcript files under `~/.gemini/antigravity-cli` |

Claude Code gives plan limits only to status-line scripts. To show them, set this in `~/.claude/settings.json`. It **replaces** any status line you already have:

```json
"statusLine": {"type": "command", "command": "python3 /path/to/jarviee/hooks/statusline.py"}
```

## Glance API and SwiftBar

`GET /api/glance` is a read-only summary for hotkeys, menu bars and status lights. It needs no token and reads only cached data. It returns `counts`, `level` (`needs_you`, `failed`, `running` or `idle`), `color`, `label`, `oldest_waiting`, `quota`, `paused` and `updated`.

`POST /api/action/<name>` needs the token (`~/.jarviee/token`, header `X-Jarviee-Token`): `pause`, `resume`, `open-dashboard`, `show-next`.

```bash
curl -s http://127.0.0.1:7777/api/glance
TOKEN=$(cat ~/.jarviee/token)
curl -s -X POST http://127.0.0.1:7777/api/action/pause \
  -H "Content-Type: application/json" -H "X-Jarviee-Token: $TOKEN" -d '{}'
```

The `jarviee` launcher wraps these: `jarviee glance` (one line, for example `● 2 need you · 3 running`; `--json` for the JSON), `jarviee pause`, `jarviee resume`, `jarviee next`. They exit 2 if the server is down. You can bind `jarviee next` to a macOS Shortcut, a Stream Deck button or a Raycast script command.

**SwiftBar.** `extras/swiftbar/jarviee.5s.py` is a menu-bar plugin that shows the worst state and how many agents need you, with pause, resume and focus actions. Install [SwiftBar](https://swiftbar.app), then symlink the file into your SwiftBar plugin folder. The `.5s.` in the name is the refresh interval.

```bash
ln -s "$PWD/extras/swiftbar/jarviee.5s.py" ~/path/to/swiftbar-plugins/
```

Optional environment variables: `JARVIEE_PORT` (default 7777) and `JARVIEE_BIN` (path of the `jarviee` CLI). The plugin never touches the token: its menu items call the `jarviee` CLI. When the server is down it shows a quiet "Jarviee off".
