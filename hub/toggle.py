"""`jarviee --disable` / `jarviee --enable`: take Jarviee out of the owner's setup and put it back.

Disable undoes every place Jarviee hooks into the machine, so Claude Code and Antigravity run exactly as they did
before it:
  - the launchd service is stopped and its plist removed (so it doesn't come back at login), and any server started
    by hand is stopped;
  - Jarviee's hooks are removed from ~/.claude/settings.json and ~/.gemini/config/hooks.json (only ours; every other
    hook and setting stays, and each file is backed up first, see hub/install.py);
  - Jarviee's Claude Code status line is removed; it is kept in the disabled marker so enable can put it back;
  - a marker (~/.jarviee/disabled.json) stops `jarviee`, `jarviee open`, Jarviee.app and `jarviee run` from starting
    the server again until `jarviee --enable`.
Nothing is deleted: the database, logs, settings and the repo stay as they are.

Enable reverses it: hooks back in, the status line back (only if Claude Code has no other status line by then), the
service installed and started.
"""
import json
import time
from pathlib import Path

from . import brand, install

MARK = "disabled.json"
STATUSLINE_SCRIPT = "hooks/statusline.py"


def marker_path(cfg=None):
    p = (cfg or {}).get("disabled_marker_path")
    return Path(p).expanduser() if p else brand.home() / MARK


def is_disabled(cfg=None):
    return marker_path(cfg).is_file()


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None


def _ours(status_line):
    return isinstance(status_line, dict) and STATUSLINE_SCRIPT in str(status_line.get("command", ""))


def _claude_settings_path():
    return install.PATHS["claude"]()


def _take_status_line(dry_run):
    """Remove Jarviee's status line from Claude Code's settings. Returns what was removed, or None."""
    path = _claude_settings_path()
    data = _read_json(path)
    if not data or not _ours(data.get("statusLine")):
        return None
    removed = data["statusLine"]
    if not dry_run:
        data = dict(data)
        del data["statusLine"]
        install._write(path, data)
    print(f"[Claude Code] {'would remove' if dry_run else 'removed'} the Jarviee status line from {path}")
    return removed


def _give_status_line(saved):
    if not _ours(saved):
        return
    path = _claude_settings_path()
    data = _read_json(path)
    if data is None:
        print(f"[Claude Code] {path} is not valid JSON; status line not restored.")
        return
    if data.get("statusLine"):
        print("[Claude Code] another status line is set now; left it alone (Jarviee's limit readings stay off).")
        return
    install._write(path, {**data, "statusLine": saved})
    print(f"[Claude Code] restored the Jarviee status line in {path}")


def disable(cfg, dry_run=False, service=None, entry=None):
    """Take Jarviee out of the setup. Returns 0 on success."""
    if service is None:
        from . import service
    if entry is None:
        from . import entry
    was_installed = bool(service.is_installed())
    if dry_run:
        print("Dry run: nothing will change.")
        print(f"[service] would {'stop and remove the launchd service' if was_installed else 'stop any running server'}")
    else:
        if was_installed:   # first: with the service gone, KeepAlive cannot start the server again after the stop
            service.uninstall()
        try:
            entry.stop_server(cfg)
        except Exception as e:   # noqa: BLE001  (a server that is already down is fine)
            print(f"[server] {e}")
    ok = all(install.run(t, dry_run=dry_run, uninstall=True) for t in ("claude", "agy"))
    status_line = _take_status_line(dry_run)
    if dry_run:
        return 0 if ok else 1
    mark = marker_path(cfg)
    mark.parent.mkdir(parents=True, exist_ok=True)
    before = _read_json(mark) or {}   # disabling twice must not lose what the first disable saved
    mark.write_text(json.dumps({"at": time.time(), "service": was_installed or bool(before.get("service")),
                                "statusLine": status_line if status_line is not None else before.get("statusLine")},
                               indent=2), encoding="utf-8")
    print("Jarviee is disabled. Claude Code and Antigravity run without it; sessions already open stop reporting "
          "once they restart.\nTurn it back on with: jarviee --enable")
    return 0 if ok else 1


def enable(cfg, dry_run=False, service=None, entry=None):
    """Put Jarviee back. Returns 0 on success."""
    if service is None:
        from . import service
    if entry is None:
        from . import entry
    mark = marker_path(cfg)
    saved = _read_json(mark) or {}
    want_service = not mark.is_file() or bool(saved.get("service"))   # no marker: nothing known, install as before
    if dry_run:
        print("Dry run: nothing will change.")
    ok = all(install.run(t, dry_run=dry_run) for t in ("claude", "agy"))
    if dry_run:
        print("[Claude Code] would restore the Jarviee status line" if _ours(saved.get("statusLine")) else "")
        print("[service] would install and start the launchd service" if want_service else "[server] would start the server")
        return 0 if ok else 1
    _give_status_line(saved.get("statusLine"))
    try:
        mark.unlink()
    except FileNotFoundError:
        pass
    if want_service and not service.install(cfg):
        print("The service didn't start; run `jarviee` to start the server by hand.")
        return 1
    up, _ = entry.start_server_if_down(cfg)
    port = (cfg or {}).get("port", 7777)
    print(f"Jarviee is enabled → http://127.0.0.1:{port}/" if up else "Jarviee is enabled; the server is still starting.")
    return 0 if ok else 1


DISABLED_HINT = "Jarviee is disabled. Turn it back on with: jarviee --enable"
