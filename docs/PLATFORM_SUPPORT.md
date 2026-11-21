# Platform Support

Jarviee targets **Python 3.9+ standard library only** (`README.md:11`). CI ([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)) runs `unittest` on `macos-latest` for Python `3.9` and `3.13`.

- **macOS:** Primary supported platform.
- **Linux:** Core server, hooks, queue, and background runs work; macOS-only connectors (Keychain, Calendar, iTerm2, `launchd`, `.app`, `sips`) degrade cleanly without crashing or log spam.
- **Windows:** Core server, hooks, queue, and background runs degrade cleanly (`fcntl`, `os.getuid`, `os.killpg`/`SIGKILL`, `strftime("%-I")`, POSIX `chmod` checks, and `.cmd` `shutil.which` resolution are guarded); `hooks/report.py` / `hub/stopper.py` still use `ps`.
- **CI / bare stdlib Python:** `hub.py app install` and `tests/test_service_app.py` pass without `Pillow` (`PIL` is lazily imported in `hub/icon.py`, falling back to a generic `.app` icon).

Regression tests for every guard below live in [`tests/test_platform_guards.py`](../tests/test_platform_guards.py); they simulate other platforms by patching `sys.platform`, `os` attributes, `shutil.which` and the `PIL` import, never by needing another OS.

Tags below: **Verified** = observed by running Python tests or simulated cross-platform checks; **Code** = from reading the code.

## Requirements per Platform

- **macOS:** macOS 12+ (`hub/app_launcher.py:29` [Code]), Python 3.9+ stdlib (`Pillow` optional, only needed to render a custom `.icns` for `app install` [Verified]), `git`, `claude` or `agy`. Optional: iTerm2, `swiftc`, `terminal-notifier`, `gh`, `ffmpeg` + Handy/`whisper-cli`, `~/.jarviee/tts`, WhatsApp Desktop (Full Disk Access), Calendar opt-in (`"calendar": {"enabled": true}`).
- **Linux:** Python 3.9+ stdlib, `git`, `claude` or `agy`, `"terminal": {"mode": "background"}` (auto-set by `hub.py setup` [Verified]). Optional: `gh`, `ffmpeg` + `whisper-cli`, `OMNIROUTE_API_KEY`, ntfy.
- **Windows:** Python 3.9+ (`py -3`), `git`, `claude` or `agy`. Set `tools.echo.command[0]` to `python` (`README.md:59` [Code]); `hub/runjob.py` resolves CLI commands via `shutil.which` so `.cmd` shims work [Verified]. Same optional tools as Linux.

## Feature Matrix

| Feature | macOS | Linux | Windows | Notes & Evidence |
| --- | --- | --- | --- | --- |
| Dashboard HTTP server & UI (`hub/server.py`, `hub/static/*`) | works | works | works (simulated) | `fcntl` in `hub/voice.py` and `os.getuid()` in `hub/auth.py` are guarded; `MailWatcher` reports Keychain unavailable cleanly without `FileNotFoundError` spam [Verified]. |
| Hooks & statusline (`hub/install.py`, `hooks/report.py`, `hooks/statusline.py`) | works | works | degraded | Linux [Code]. Windows installs & forwards hooks, but `hooks/report.py:50` uses `ps`, so Stop cannot signal hook PIDs [Code]. |
| `classify`, `run`, `plan`, queue, `/pipeline`, Board, Brain | works | works | degraded | `runjob.py` (`shutil.which`), `orphans.py` (`PermissionError`), `inbox.py` / `entry.py` (`killpg`/`SIGKILL`) guarded [Verified]; `stopper.py:25` uses `ps` [Code]. |
| iTerm2 panes, `/orchestrate`, focus (`hub/terminal.py`, `hub/focus.py`) | works | not available | not available | Guarded: falls back to background; `/orchestrate` returns 400; `focus()` returns `(False, …)` [Verified]. |
| Background service (`hub/service.py` — `launchd`) | works | not available | not available | Gated on `sys.platform == "darwin"`, uses `os.pathsep` for `PATH`, and refuses mismatched `$HOME` unless `--force` is passed [Verified]. |
| `.app` launcher & SwiftBar (`hub/app_launcher.py`, `hub/icon.py`, `extras/swiftbar/`) | works | not available | not available | `hub/icon.py` lazy-imports `PIL`; without Pillow `app install` prints one line and uses a generic icon [Verified]. |
| OmniRoute fallback (`hub/omniroute.py`) | works | degraded | degraded | `OMNIROUTE_API_KEY` env var works (`hub/omniroute.py:90`); `omniroute connect` Keychain write fails cleanly [Verified Linux; Code Windows]. |
| Keychain connectors: Gmail, LinkedIn, Email, CallMeBot (`hub/briefing.py`, `hub/mailout.py`, `hub/wamsg.py`) | works | not available | not available | `/usr/bin/security` catches `(OSError, subprocess.SubprocessError)` and `connect*()` gates on `darwin` with `"not available on this platform"` [Verified]. |
| GitHub in `/startwork` (`hub/briefing.py:244-274`) | works | works | works | Uses `shutil.which("gh")` [Code]. |
| Apple Calendar & Reminders (`hub/apple_calendar.py`, `hub/calendar_add.py`) | opt-in | not available | not available | Off by default (`"calendar": {"enabled": false}` in `config.json`); readers/writers return `"not connected (turn it on: calendar.enabled)"` without running EventKit when disabled; Python 12 h clock replaces `%-I` [Verified]. |
| WhatsApp Desktop reader (`hub/briefing.py:277-318`) | works | not available | not available | Guarded: missing `ChatStorage.sqlite` reports `connected=False` [Verified]. |
| Voice TTS (`say`/Piper/Kokoro) & ducking (`hub/voice.py`, `hub/duck.py`, `hub/tts_*.py`) | works | not available | not available | Off by default; `sys.platform == "darwin"` guards all engines & ducking; `fcntl` is optional [Verified]. |
| Local STT (`hub/stt.py`) | works | degraded | degraded | `whisper-cli` + `ffmpeg` works; Handy uses macOS app path unless `JARVIEE_HANDY_BIN` is set [Code]. |
| Desktop wallpaper (`hub/wallpaper.py`) | works | not available | not available | `/usr/bin/sips` and helper calls catch `(OSError, subprocess.SubprocessError)` and fall back to `(None, "none")` [Verified]. |
| Local alerts (`hub/alerts.py`) & phone pushes (`hub/push.py`, `hub/limitwatch.py`) | works | not available | not available | `sys.platform != "darwin"` disables `osascript`/`afplay`/`ioreg` [Verified]; ntfy agent/limit pushes still work (non-macOS treated as away). |
| Screencapture & attachments (`hub/actions.py:16-28`) | works | works | works | No `screencapture` usage in repo [Verified]; `/api/attach` accepts image bytes on all OSes [Code]. |

## Server Boot Behavior on Non-macOS (`hub/server.py:187-244`)

- **Windows boot (`hub/voice.py`):** `fcntl` import is wrapped in `try/except ImportError: fcntl = None` and `_speech_lock()` skips locking when `None` [Verified].
- **Linux/Windows `MailWatcher` (`hub/briefing.py`):** `_keychain()` catches `(OSError, subprocess.SubprocessError)` and `read_mail()` reports `"Not available on this platform"` instead of raising `FileNotFoundError` [Verified].
- **Windows `auth.harden()` (`hub/auth.py`):** `private()` checks `hasattr(os, "getuid")` and returns `False` instead of raising `AttributeError` [Verified].
- **Browser launch (`hub/browser.py`, `bin/jarviee`, `hub/actions.py`):** `hub/browser.py` and `bin/jarviee` use `webbrowser.open(url)` off `darwin` and gate `osascript` on `darwin` [Verified]; `hub/entry.py` has no browser call (only its `launchctl` call is gated). `hub/actions.py:167` (the `browser.open_dashboard()` exception fallback) now calls `browser._open_default`, so it uses `webbrowser.open` off `darwin` too [Code].
- **Guarded / quiet at boot:** `Terminal`, `duck.recover`, `AlertWatcher`, `voice.Speaker`, `Daybook`, `MapWatcher`, `PushWatcher`, `LimitWatcher`, `usage.prewarm`, `launcher.prune_logs`, `worktrees.sweep` [Verified + Code].

## Known Crash Risks & Fresh-User Hazards

| Scope | Location | Behavior | Status & Fix | Evidence |
| --- | --- | --- | --- | --- |
| Windows boot | `hub/voice.py` | Top-level `import fcntl` crashed `serve`. | **Fixed:** `try/except ImportError: fcntl = None`; skip `flock` when `None`. | Verified |
| All OS / CI | `hub/icon.py`, `hub/app_launcher.py` | Top-level `PIL` import broke `app install` & `test_service_app`. | **Fixed:** lazy-import `PIL` in `hub/icon.py` and `build_icns` inside `app_launcher.install()`; generic icon + one-line notice without Pillow. | Verified |
| Linux/Win boot + CLI | `hub/briefing.py` | Unguarded `/usr/bin/security` spammed `MailWatcher` & crashed `connect-gmail`. | **Fixed:** catch `(OSError, subprocess.SubprocessError)` in `_keychain()` & `connect_gmail()`, gate `connect_gmail()` on `darwin`; the `runner` default is looked up at call time so a patched `subprocess.run` is honoured. | Verified |
| Linux/Win (enabled) | `hub/wamsg.py` | Unguarded `/usr/bin/security` crashed `account()` / `handles()` / `connect()`. | **Fixed:** catch `(OSError, subprocess.SubprocessError)` in `_security()` & `connect()`, gate `connect()` on `darwin`. | Verified |
| Linux/Win CLI + Win PATH + `$HOME` hazard | `hub/service.py`, `hub.py`, `hub/entry.py` | `os.getuid()` / `launchctl` crashed off macOS; `":".join` broke Windows `PATH`; `HOME` override affected the real `launchd` job. | **Fixed:** gate `service` commands on `darwin` (`entry.stop_server` too); `os.pathsep.join(dirs)`; refuse `install`/`uninstall` when `Path.home()` != real home unless `--force` is passed. | Verified |
| Linux/Win browser | `hub/browser.py`, `bin/jarviee`, `hub/actions.py:167` | Called macOS `open` / `osascript` (`FileNotFoundError` or Linux `openvt`). | **Fixed in `hub/browser.py` & `bin/jarviee`** (`webbrowser.open` off `darwin`, `osascript` gated on `darwin`); **and `hub/actions.py:167`** (the fallback now calls `browser._open_default`). | Verified |
| Linux/Win wallpaper | `hub/wallpaper.py` | Custom `appearance.wallpapers` called `/usr/bin/sips` unguarded. | **Fixed:** catch `(OSError, subprocess.SubprocessError)` around helper & `sips`; return `None, "none"`. | Verified |
| Windows auth | `hub/auth.py` | `private()` called `os.getuid()` (`AttributeError`). | **Fixed:** return `False` when `not hasattr(os, "getuid")`. | Verified |
| Windows stop/timeout | `hub/inbox.py`, `hub/entry.py` | `os.killpg` and `signal.SIGKILL` raised `AttributeError`. | **Fixed:** guard `os.killpg` & `signal.SIGKILL`; fall back to `proc.kill()` / `SIGTERM`. | Verified |
| Windows Calendar | `hub/apple_calendar.py` | `strftime("%-I:%M %p")` raised `ValueError` on MSVC. | **Fixed:** compute 12 h value in Python (`d.hour % 12 or 12`). | Verified |
| Fresh-user Calendar prompt | `config.json`, `hub/apple_calendar.py`, `hub/briefing.py`, `hub/push.py`, `hub/calendar_add.py` | `/startwork` silently triggered macOS Calendar permission prompt for new users. | **Fixed:** `"calendar": {"enabled": false}` opt-in in `config.json`; all readers/writers return `"not connected (turn it on: calendar.enabled)"` without invoking Swift/EventKit (`hub/routes.py` callers work via overlay fallback and should also pass `cfg=c.cfg`). | Verified |
| Windows `doctor` | `hub/doctor.py` | POSIX `st_mode & 0o077` is always non-zero on Windows. | **Fixed:** skip POSIX mode checks when `os.name == "nt"`. | Verified |
| Windows PID / `.cmd` | `hub/orphans.py`, `hub/runjob.py` | `WinError 87` treated as alive; `Popen` skipped `shutil.which` for `.cmd`. | **Fixed:** treat only `PermissionError` as alive in `orphans.alive()`; resolve `cmd_argv[0]` via `shutil.which` in `RunJob._run_headless()`. | Verified |

## Python 3.9 Compatibility & 3.10+ Constructs

- **AST & syntax:** `ast.parse(..., feature_version=(3, 9))` passed on all `206` `.py` files + `bin/jarviee` + `bin/agent-hub` (**0 errors**) [Verified].
- **3.10+ constructs:** AST + regex scan found **0 occurrences** of runtime `|` unions, `match`, parenthesized `with`, `zip(strict=)`, `dataclass(slots=/kw_only=)`, `pairwise`, `bit_count`, `datetime.UTC`, `tomllib`, `ExceptionGroup`/`except*`, `Self`/`ParamSpec`/`TypeAlias`, `StrEnum`, or `TaskGroup` (only 4 files have annotations — `hub/alerts.py`, `hub/detector.py`, `hub/fallback.py`, `hub/stt.py` — none use `|`) [Verified].
- **3.9 `fromisoformat` caveat:** `hooks/statusline.py:53`, `hub/subagents.py:87`, and `hub/usage.py:63` call `datetime.fromisoformat(s.replace("Z", "+00:00"))`. On Python `3.9.6` (vs `3.13.18`), 1/2/4/5/7–9 fractional digits raise `ValueError` (caught and degraded to `0.0`/fallback/`None`) [Verified].
- **3.9.6 test run:** Across all `98` test files (`1,281` items in 6 batches, temp copy), **1 real failure** occurred: `tests/test_service_app.py:11` (`No module named 'PIL'` from `hub/icon.py:6`, also reproduced on `3.13.18`); that is now fixed by the lazy `PIL` import (the test passes with `PIL` blocked). The remaining `173` errors/failures were sandbox blocks on loopback HTTP sockets and `ps`; **0 Python-3.9 incompatibilities** appeared elsewhere [Verified].
