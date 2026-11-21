"""/orchestrate <task>: Opus leads a team in its own tab of the Jarviee iTerm2 window (see hub/orch.py). This module is
only the command prefix; without it runs auto-tile in the split tabs (hub/terminal.py, max_panes_per_tab per tab)."""
import re

PREFIX_RE = re.compile(r"^\s*/orchestrate(?![\w-])[\s:,-]*", re.I)


def requested(text):
    """(asked, task text without the /orchestrate prefix)."""
    m = PREFIX_RE.match(text or "")
    return bool(m), ((text or "")[m.end():].strip() if m else (text or ""))
