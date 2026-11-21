# Contributing to Jarviee

Thank you for helping improve Jarviee! Please read the guidelines and architectural invariants below before opening a pull request.

## Development Setup

Jarviee runs on **Python 3.9+** with **zero third-party pip dependencies** for the core hub and dashboard.

1. Clone the repository and enter the directory:
   ```bash
   git clone https://github.com/Mclein31/jarviee.git
   cd agent-hub
   ```
2. Verify Python 3.9 or newer is available:
   ```bash
   python3 --version
   ```
3. Start the local command center:
   ```bash
   python3 hub.py serve
   ```
   The dashboard is served at `http://127.0.0.1:7777`.
4. (Optional) Customize settings without dirtying `config.json` by placing overrides in `~/.jarviee/config.json` (or setting `JARVIEE_USER_CONFIG=/path/to/overlay.json`).
5. (Optional) Install agent hooks for local end-to-end testing:
   ```bash
   python3 hub.py hooks install
   ```

## Running Tests

Run the full test suite using Python's built-in `unittest` runner:

```bash
python3 -m unittest discover -s tests -q
```

Run a single test file during development:

```bash
python3 -m unittest tests/test_router.py -v
```

Notes on the test environment (`tests/helpers.py`):
- `JARVIEE_USER_CONFIG=""` is set automatically so tests never read your personal `~/.jarviee/config.json`.
- Real agent CLIs (`agy`, `claude`) are replaced with lightweight Python fakes inside temporary directories.
- Never write tests that modify or stop a running Jarviee instance on port `7777`; use `helpers.free_port()` and temporary directories for server tests.

## Non-Negotiable Architecture Rules

1. **Python 3.9+ standard library only:**
   - Do not add third-party packages or `requirements.txt` dependencies to `hub.py`, `hub/`, `hooks/`, or `scripts/`.
   - Write syntax compatible with Python 3.9 (avoid `X | Y` runtime type unions, `match`/`case`, or 3.10+ stdlib additions).
   - Keep the dashboard (`hub/dashboard.html` and `hub/static/`) plain HTML, CSS, and vanilla JS with no build step and no external CDN scripts.
2. **Hooks must never block an agent:**
   - Hook scripts (`hooks/report.py`, `hooks/statusline.py`) must enforce short timeouts (2 seconds max), swallow unexpected errors, and always exit `0`.
   - Never output gating or blocking decisions from a hook; hooks only observe and report events (or emit optional non-blocking context).
3. **Server stays `127.0.0.1`-only:**
   - `hub/server.py` enforces binding strictly to `127.0.0.1` (`host == "127.0.0.1"`), rejects cross-site / DNS-rebinding `Host`, `Origin`, and `Sec-Fetch-Site` headers, and requires the per-install `X-Jarviee-Token` header on all state-mutating `POST` routes and `/api/run-spec`.
   - Do not weaken host binding, token verification, or CSRF/DNS-rebinding guards.

## Adding a New Agent CLI under `config.json` `"tools"`

Jarviee configures agent CLIs declaratively under the `"tools"` map in `config.json`. To add a new CLI tool:

1. **Add an entry under `"tools"` in `config.json`** (or test it first in `~/.jarviee/config.json`):
   ```json
   "mytool": {
     "command": [
       "mytool",
       "-p",
       "{prompt}",
       "--model",
       "{model}"
     ],
     "models": {
       "basic": "model-fast",
       "light": "model-standard",
       "complex": "model-strong",
       "heavy": "model-max"
     },
     "efforts": {
       "basic": "low",
       "light": "medium",
       "complex": "high",
       "heavy": "high"
     },
     "effort_args": [
       "--effort",
       "{effort}"
     ]
   }
   ```
   Supported command placeholders in `hub/runplan.py` are `{prompt}`, `{model}`, `{effort}`, and `{session}` (with optional `effort_levels`, `session_args`, `resume_args`, and `bridge_command`).
2. **Wire routing and concurrency (optional):**
   - Point one or more tiers in `"dispatch"` (`basic`, `light`, `complex`, `heavy`) to `"tool": "mytool"`.
   - Add `"mytool": 1` under `"lanes"` if queued runs should use their own concurrency lane.
   - Add fallback entries under `"fallback.equivalents"` if quota/availability fallback should include the new tool.
3. **Optional code touchpoints for deeper integration:**
   - `hub/runplan.py`: add read-only flags in `readonly_args()` and interactive pane command translation in `interactive_argv()`.
   - `hooks/report.py`: add process names in `AGENT_NAMES` if your CLI emits hook events.

## Pull Request Checklist

- Keep changes focused and minimal; include unit tests covering new behavior or bug fixes.
- Ensure `python3 -m unittest discover -s tests -q` passes completely.
- Do not include personal paths, private workspace names, emails, or real tokens/keys anywhere in code, tests, or fixtures.
