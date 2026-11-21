import json
import math
import os
import sys
import tempfile
from pathlib import Path

from . import brand

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(brand.env("CONFIG") or ROOT / "config.json")


def expand(p):
    return Path(os.path.expanduser(os.path.expandvars(str(p))))


def user_config_path():
    """The owner's overlay file: JARVIEE_USER_CONFIG (legacy AGENT_HUB_USER_CONFIG), else <data dir>/config.json.
    An empty JARVIEE_USER_CONFIG disables the overlay (returns None)."""
    v = brand.env("USER_CONFIG")
    if v is not None:
        return Path(os.path.expanduser(v)) if v.strip() else None
    return brand.home() / "config.json"


def _warn(msg):
    print(f"jarviee: {msg}", file=sys.stderr)


def read_user_config():
    """The user overlay as a dict. A missing file is {}; bad JSON or a non-object prints one warning and is {}."""
    p = user_config_path()
    if p is None:
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        _warn(f"cannot read {p}: {e}; ignoring it")
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        _warn(f"{p} is not valid JSON ({e}); ignoring it")
        return {}
    if not isinstance(data, dict):
        _warn(f"{p} must hold a JSON object; ignoring it")
        return {}
    return data


def deep_merge(base, over):
    """`over` merged into `base` in place: dicts merge recursively, lists and scalars replace. Returns base."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v
    return base


def write_user_config(updates):
    """Deep-merge `updates` into the overlay file and write it atomically (mode 0600). Returns the merged dict.
    A broken existing overlay is replaced by `updates` alone."""
    p = user_config_path()
    if p is None:
        raise RuntimeError("the user config is disabled (JARVIEE_USER_CONFIG is empty)")
    merged = deep_merge(read_user_config(), updates)
    p.parent.mkdir(parents=True, exist_ok=True)
    target = p.resolve() if p.is_symlink() else p
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return merged


def _mkdir_private(path):
    """Create `path`; ~/.jarviee itself is created 0700. An existing dir keeps its mode."""
    path = Path(path)
    home = brand.HOME
    if (path == home or home in path.parents) and not home.exists():
        try:
            home.mkdir(parents=True, mode=0o700, exist_ok=True)
            home.chmod(0o700)   # mkdir's mode is masked by umask
        except OSError:
            pass
    path.mkdir(parents=True, exist_ok=True)


def load_config(path=None, user=None):
    """config.json (or JARVIEE_CONFIG, or `path`) with the user overlay deep-merged over it.
    The overlay applies on the default load (path None) or when user=True; an explicit path skips it
    unless user=True. user=False never applies it."""
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if user is True or (user is None and path is None):
        deep_merge(cfg, read_user_config())
    if brand.env("PORT"):
        cfg["port"] = int(brand.env("PORT"))
    cfg["db_path"] = str(brand.data_path(cfg["db_path"]))
    cfg["logs_dir"] = str(brand.data_path(cfg["logs_dir"]))
    b = cfg["obsidian"]
    b["workspace_root"] = str(expand(b["workspace_root"]))
    _mkdir_private(expand(cfg["db_path"]).parent)
    _mkdir_private(expand(cfg["logs_dir"]))
    return apply_settings(migrate(cfg))


SETTINGS_KEYS = {"fallback": ("enabled", "never_fallback_to_opus", "use_available_tool"), "omniroute": ("enabled",),
                 "maps": ("auto_update",),
                 "push": ("enabled", "lead_min", "digest_at", "mute_calendars", "limit_alerts"),
                 "voice": ("enabled", "tldr", "report_volume", "duck_volume"),
                 "whatsapp_send": ("enabled",), "email_send": ("enabled",),
                 "appearance": ("wallpaper",)}


def _saved_value(v, default=None):
    """Values the dashboard saves: switches, small numbers, a "HH:MM" time, a list of calendar names.
    Not finite numbers (NaN, Infinity: json.loads reads them) are rejected. When the code default is known the saved
    value keeps its type: a switch stays a bool, a whole number stays an int. Anything else is None (ignored)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if default is not None:
        if isinstance(default, bool):
            return v if isinstance(v, bool) else None
        if isinstance(default, int):
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return v
            return int(v) if isinstance(v, float) and v == int(v) else None
        if isinstance(default, float):
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if isinstance(default, str) and not isinstance(v, str):
            return None
        if isinstance(default, list) and not isinstance(v, list):
            return None
    if isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str) and len(v) <= 40:
        return v
    if isinstance(v, list) and len(v) <= 50 and all(isinstance(x, str) for x in v):
        return v
    return None


def settings_file(cfg):
    """Owner toggles from the dashboard, kept out of the git-tracked config.json."""
    return brand.data_path(cfg.get("settings_path") or "~/.jarviee/settings.json")


def apply_settings(cfg):
    try:
        saved = json.loads(settings_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for section, keys in SETTINGS_KEYS.items():
        for k in keys:
            if isinstance(saved.get(section), dict) and k in saved[section]:
                known = cfg.get(section, {}).get(k) if isinstance(cfg.get(section), dict) else None
                v = _saved_value(saved[section][k], known)
                if v is not None:
                    cfg.setdefault(section, {})[k] = v
    return cfg


def save_settings(cfg):
    out = {section: {k: cfg.get(section, {})[k] for k in keys if k in cfg.get(section, {})}
           for section, keys in SETTINGS_KEYS.items()}
    p = settings_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


DEFAULT_DISPATCH = {"basic": {"tool": "agy"}, "light": {"tool": "agy"},
                    "complex": {"tool": "claude"}, "heavy": {"tool": "claude"}}

DEFAULT_COMPLEX_JOB_DURATION_SECONDS = 60
DEFAULT_COMPLEX_JOB_MIN_STEPS = 2
DEFAULT_FINISHED_JOB_RETENTION_SECONDS = 300


def migrate(cfg):
    """Accept configs written for three tiers (basic/standard/heavy).

    'standard' becomes 'light'; 'complex' borrows 'light' (or 'heavy') until set.
    Missing dispatch/lanes get defaults. The file on disk is not rewritten.
    """
    for tool in cfg.get("tools", {}).values():
        for key in ("models", "efforts"):
            m = tool.get(key)
            if not isinstance(m, dict):
                continue
            if "standard" in m and "light" not in m:
                m["light"] = m.pop("standard")
            if "complex" not in m and ("light" in m or "heavy" in m):
                m["complex"] = m.get("light", m.get("heavy"))
    k = cfg.get("classifier", {})
    for key in ("light_keywords", "complex_keywords"):
        k.setdefault(key, [])
    d = cfg.setdefault("dispatch", {})
    for level, entry in DEFAULT_DISPATCH.items():
        e = d.setdefault(level, dict(entry))
        if not e.get("model") and e["tool"] in cfg.get("tools", {}):
            e["model"] = cfg["tools"][e["tool"]]["models"].get(level, "")
    cfg.setdefault("lanes", {"agy": 2, "claude": 1})
    cfg.setdefault("prefer_agy_on_borderline", False)
    cfg.setdefault("fallback", {
        "enabled": True,
        "never_fallback_to_opus": True,
        "use_available_tool": True,
        "max_hops": 2,
        "equivalents": {}
    })
    cfg.setdefault("escalation", {"to_level": "complex", "to_tool": "claude", "max": 1,
                                  "log_tail_chars": 4000, "blocked_patterns": []})
    cfg.setdefault("quota_patterns", [])
    term = cfg.setdefault("terminal", {})
    for key, value in TERMINAL_DEFAULTS.items():
        term.setdefault(key, value)
    for key, default in (
        ("complex_job_duration_seconds", DEFAULT_COMPLEX_JOB_DURATION_SECONDS),
        ("complex_job_min_steps", DEFAULT_COMPLEX_JOB_MIN_STEPS),
        ("finished_job_retention_seconds", DEFAULT_FINISHED_JOB_RETENTION_SECONDS),
    ):
        if cfg.get(key) is None:
            cfg[key] = default
        else:
            try:
                cfg[key] = int(cfg[key])
            except (ValueError, TypeError):
                cfg[key] = default
    return cfg


# iTerm2 panes for launched runs (hub/terminal.py). mode "background" keeps the old behaviour.
TERMINAL_DEFAULTS = {"mode": "iterm2", "window_name": "Jarviee", "max_panes_per_tab": 6,
                     "display": "main", "fullscreen": False, "position": "right", "width_pct": 60,
                     "focus_on_launch": True, "close_on_finish": "never", "interactive": True,
                     "queue_interactive": True,
                     # "split" (default): a pane per run, auto-tiled as runs come, max_panes_per_tab per tab. `/orchestrate`
                     # opens a separate tab with the `grid` (columns × rows, 4×2 = 8) of free panes for its own runs.
                     # "grid": every run uses the grid (the old always-on 8-pane window).
                     "layout": "split", "grid": [4, 2], "font_size": None}

# The dashboard's own browser window (hub/browser.py): "safari" opens a separate Safari window on `display`,
# "default" hands the URL to `open` (the default browser picks the window).
DASHBOARD_WINDOW_DEFAULTS = {"browser": "default", "display": "main"}
