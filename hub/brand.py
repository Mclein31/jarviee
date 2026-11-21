"""Jarviee's name, data folder and env vars, and the one place the old Agent Hub names are still accepted.

The product was called Agent Hub until 2026-11-11. Old installs keep working:
AGENT_HUB_* env vars, the X-Agent-Hub-Token header and ~/.agent-hub are read when the new
name is absent, and `migrate_home()` moves ~/.agent-hub to ~/.jarviee once, leaving a symlink.
"""
import os
from pathlib import Path

NAME = "Jarviee"
SLUG = "jarviee"
TOKEN_HEADER = "X-Jarviee-Token"
LEGACY_TOKEN_HEADER = "X-Agent-Hub-Token"
ENV_PREFIX = "JARVIEE_"
LEGACY_ENV_PREFIX = "AGENT_HUB_"

HOME = Path.home() / ".jarviee"
LEGACY_HOME = Path.home() / ".agent-hub"


def env(key, default=None):
    """JARVIEE_<key>, else the old AGENT_HUB_<key>, else default."""
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        v = os.environ.get(prefix + key)
        if v is not None:
            return v
    return default


def env_names(key):
    """Both spellings of one env var, new first (for setting or clearing it in a child env)."""
    return (ENV_PREFIX + key, LEGACY_ENV_PREFIX + key)


def home():
    """~/.jarviee, or the old ~/.agent-hub while it hasn't been migrated yet."""
    if HOME.exists() or not LEGACY_HOME.exists():
        return HOME
    return LEGACY_HOME


def data_path(p):
    """Expand a config path; a ~/.jarviee or ~/.agent-hub prefix resolves to home()."""
    s = os.path.expandvars(str(p))
    for prefix in ("~/.jarviee", "~/.agent-hub"):
        if s == prefix or s.startswith(prefix + "/"):
            return home() / s[len(prefix) + 1:] if s != prefix else home()
    return Path(os.path.expanduser(s))


def migrate_home(home_dir=None, legacy_dir=None):
    """Move ~/.agent-hub to ~/.jarviee once and leave a symlink so old hooks and processes still resolve.

    Returns True when it moved the folder. Never raises: a failed move leaves home() on the old path.
    """
    new, old = Path(home_dir or HOME), Path(legacy_dir or LEGACY_HOME)
    if new.exists() or old.is_symlink() or not old.is_dir():
        return False
    try:
        os.rename(old, new)
    except OSError:
        return False
    try:
        os.symlink(new, old)
    except OSError:
        pass
    return True
