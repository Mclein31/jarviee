"""Shared start, stop, project detection, and lifecycle routines for the Jarviee CLI & app."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from hub import brand, service
from hub.config import ROOT, load_config
from hub.server_health import check_health, check_health_info, wait_healthy

DEFAULT_PID_PATH = brand.home() / "server.pid"
DEFAULT_LOG_PATH = brand.home() / "logs" / "server.log"


def is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def get_stored_pid(pid_path=DEFAULT_PID_PATH):
    p = Path(pid_path)
    if p.is_file():
        try:
            return int(p.read_text().strip())
        except (ValueError, OSError):
            pass
    return None


def clean_stale_pid(pid_path=DEFAULT_PID_PATH):
    pid = get_stored_pid(pid_path)
    if pid is not None:
        if not is_pid_alive(pid):
            try:
                Path(pid_path).unlink()
                return pid  # recovered stale pid
            except OSError:
                pass
    return None


def start_server_if_down(cfg=None, pid_path=DEFAULT_PID_PATH, log_path=DEFAULT_LOG_PATH):
    c = cfg or load_config()
    from . import toggle
    if toggle.is_disabled(c):   # `jarviee --disable`: nothing starts it again until `jarviee --enable`
        print(toggle.DISABLED_HINT)
        return False, False
    if check_health(c):
        return True, False  # already up

    # 1. Try kickstarting launchd service if installed
    if service.is_installed():
        service.restart()
        if wait_healthy(c, timeout=2.0, interval=0.25):
            return True, True

    # 2. Standalone nohup fallback with PID file
    clean_stale_pid(pid_path)
    existing_pid = get_stored_pid(pid_path)
    if existing_pid and is_pid_alive(existing_pid):
        # Wait a moment in case it's currently starting
        if wait_healthy(c, timeout=2.0, interval=0.25):
            return True, False

    py_exe = service.get_python_exe()
    hub_script = str(ROOT / "hub.py")
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(log_file, "a", encoding="utf-8")
    env = dict(os.environ, PATH=service.resolve_path_dirs(), PYTHONUNBUFFERED="1")

    proc = subprocess.Popen(
        [py_exe, hub_script, "serve"],
        stdout=f,
        stderr=f,
        stdin=subprocess.DEVNULL,
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
    )
    p = Path(pid_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(proc.pid), encoding="utf-8")

    ok = wait_healthy(c, timeout=10.0, interval=0.25)
    return ok, True


def stop_server(cfg=None, pid_path=DEFAULT_PID_PATH):
    c = cfg or load_config()
    ok, info = check_health_info(c)
    target_pid = info.get("pid") if ok else get_stored_pid(pid_path)

    stopped = False
    # If launchd service is loaded, bootout or restart
    if sys.platform == "darwin" and service.is_installed():
        subprocess.run(["launchctl", "kill", "SIGTERM", f"gui/{service.get_uid()}/{service.installed_label()}"],
                       capture_output=True)

    if target_pid and is_pid_alive(target_pid):
        try:
            os.kill(target_pid, signal.SIGTERM)
            stopped = True
            for _ in range(15):
                time.sleep(0.2)
                if not is_pid_alive(target_pid):
                    break
            if is_pid_alive(target_pid):
                kill_sig = getattr(signal, "SIGKILL", None) or getattr(signal, "SIGTERM", None)
                if kill_sig is not None:
                    os.kill(target_pid, kill_sig)
        except OSError:
            pass

    p = Path(pid_path)
    if p.is_file():
        try:
            p.unlink()
        except OSError:
            pass
    return stopped or not check_health(c)


def detect_project_from_dir(cwd=None, cfg=None):
    cur = Path(cwd or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / ".brain").is_dir():
            return p.name
    return None
