"""Shared fixtures: a throwaway workspace with project brains and a temp config."""
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tests never read the real ~/.jarviee/config.json (the owner's overlay): an empty value disables the overlay.
os.environ["JARVIEE_USER_CONFIG"] = ""
os.environ.pop("AGENT_HUB_USER_CONFIG", None)

from hub.config import load_config  # noqa: E402

TODO_ALPHA = """---
type: todo-list
---
# Alpha TODO

Status key: ⬜ to do · ✅ done · 👤 needs the owner.

- ✅ Ship login
- ✅ Ship logout. ⬜ Still to fix: logout copy
- ⬜ Add billing
- 👤 Create the Stripe account
```
- ✅ inside a code block, ignored
```
- [x] Old checkbox item
- [ ] Old open checkbox
"""

STATUS = """---
type: status-dashboard
---
# Status

| Project | Category | Current Status | Primary Focus | Blocker / Risk |
| :--- | :--- | :--- | :--- | :--- |
| **[[AlphaApp\\|Alpha]]** | Web | 🟢 Active | Billing | Stripe account |
| **[[Beta Site]]** | Web | 🟡 Maintenance | Copy | None |
| **[[Ghost\\|NoSuchFolder]]** | Web | 🔵 Archived | — | None |
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_workspace(tmp):
    ws = Path(tmp) / "ws"
    (ws / "_BRAIN").mkdir(parents=True)
    (ws / "_BRAIN" / "PROJECT_STATUS.md").write_text(STATUS, encoding="utf-8")
    a = ws / "Alpha" / ".brain"
    a.mkdir(parents=True)
    (a / "PROJECT_CONTEXT.md").write_text("---\ntitle: x\n---\n# Alpha\nAlpha is a web app.\n", encoding="utf-8")
    (a / "TODO.md").write_text(TODO_ALPHA, encoding="utf-8")
    (a / "DECISIONS.md").write_text("# Decisions\nUse SQLite.\n", encoding="utf-8")
    b = ws / "Beta Site" / ".brain"
    b.mkdir(parents=True)
    (b / "PROJECT_CONTEXT.md").write_text("# Beta\n", encoding="utf-8")
    (ws / "NoBrain").mkdir()
    (ws / ".hidden" / ".brain").mkdir(parents=True)
    return ws


def make_cfg(tmp, port=None):
    from hub import usage as _usage
    _usage._cache["data"] = None   # routing reads the cached real quota; a test must not inherit it
    os.environ["JARVIEE_ALERTS"] = "0"
    os.environ["JARVIEE_PUSH"] = "0"
    os.environ["JARVIEE_AGY_BRAIN_ROOT"] = str(Path(tmp) / "agy_brain")
    ws = make_workspace(tmp)
    chosen_port = port or free_port()
    base = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    base.update(port=chosen_port, db_path=str(Path(tmp) / "hub.db"),
                logs_dir=str(Path(tmp) / "logs"),
                token_path=str(Path(tmp) / "token"),
                alerts_state_path=str(Path(tmp) / "alerts.json"),
                flow_layout_path=str(Path(tmp) / "flow-layout.json"),
                map_watch_path=str(Path(tmp) / "map-watch.json"),
                push_state_path=str(Path(tmp) / "push.json"),
                settings_path=str(Path(tmp) / "settings.json"),
                default_tool="echo")
    base["obsidian"]["workspace_root"] = str(ws)
    base.setdefault("terminal", {})["mode"] = "background"   # tests never drive the real iTerm2
    # the window behaviour the terminal tests were written for (config.json now opens a visible, focused,
    # live window); tests that need the live session set "interactive" themselves
    base.setdefault("alerts", {})["notifier"] = "osascript"   # banner tests assert the osascript form
    base["terminal"].update(fullscreen=True, focus_on_launch=False, close_on_finish="after_30s", interactive=False,
                            layout="split", display="secondary", position="right", width_pct=60, font_size=None)
    base.setdefault("pipeline", {})["enabled"] = False   # pipeline tests turn it on; the rest stay single-agent
    base["tools"]["echo"]["command"][0] = sys.executable
    # Never run the real agents in tests: both lanes become fake python agents.
    # FAKE_EXIT / FAKE_SAY in the prompt let a test make a run fail or print a phrase.
    fake = ("import sys,time,re; p=sys.argv[2]; time.sleep(float((re.search(r'FAKE_SLEEP=([0-9.]+)',p) or [0,'0.3'])[1])); "
            "s=re.search(r'FAKE_SILENT_'+sys.argv[1].upper()+r'=1|FAKE_SILENT=1',p); "
            "(not s) and print('fake', sys.argv[1], 'ran with model', sys.argv[3]); (not s) and print(p[:4000]); "
            "m=(not s) and re.search(r'FAKE_SAY=\"([^\"]+)\"',p); m and print(m[1]); "
            "e=re.search(r'FAKE_EXIT_'+sys.argv[1].upper()+r'=(\\d+)',p) or re.search(r'FAKE_EXIT=(\\d+)',p); "
            "sys.exit(int(e[1]) if e else 0)")
    for tool in ("agy", "claude"):
        base["tools"][tool]["command"] = [sys.executable, "-c", fake, tool, "{prompt}", "{model}"]
        base["tools"][tool].pop("effort_args", None)
        base["tools"][tool].pop("bridge_command", None)
        base["tools"][tool].pop("session_args", None)   # follow-up tests opt in
        base["tools"][tool].pop("resume_args", None)
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    cfg = load_config(path)
    cfg["port"] = chosen_port
    return cfg, path


class TempDir:
    def __enter__(self):
        self._t = tempfile.TemporaryDirectory()
        return self._t.name

    def __exit__(self, *a):
        self._t.cleanup()
