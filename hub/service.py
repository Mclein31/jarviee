"""Launchd background service manager for Jarviee."""
import os
import plistlib
try:
    import pwd
except ImportError:
    pwd = None
import shutil
import subprocess
import time
import sys
from pathlib import Path

from hub import brand

ROOT = Path(__file__).resolve().parent.parent
PLIST_LABEL = "dev.jarviee.server"
LEGACY_LABELS = ("com.mclein.jarviee", "com.mclein.agenthub")   # previous service labels before open-source rename
PLIST_FILENAME = f"{PLIST_LABEL}.plist"
DEFAULT_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LOG_PATH = brand.home() / "logs" / "server.log"


CANONICAL_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"


def get_python_exe():
    if os.path.isfile(CANONICAL_PYTHON) and os.access(CANONICAL_PYTHON, os.X_OK):
        return CANONICAL_PYTHON
    return sys.executable


def service_python_exe(exe=None):
    """The interpreter launchd should start. A python.org framework `bin/python3` is only a stub that execs
    Resources/Python.app; macOS privacy checks (Full Disk Access, other apps' data such as WhatsApp's) then judge the
    stub, which can't be granted usefully. Starting Python.app's own binary makes the grant on "Python" count."""
    exe = exe or get_python_exe()
    b = Path(exe)
    if b.parent.name == "bin" and "Python.framework" in b.parts:
        app = b.parent.parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        if app.is_file() and os.access(app, os.X_OK):
            return str(app)
    return exe


def resolve_path_dirs():
    """Discover stable PATH directories containing python3, agy, claude, node, and system tools."""
    dirs = []

    def add(d):
        s = str(d)
        if not s or not os.path.isdir(s):
            return
        # Reject unstable Cellar and node_modules paths
        if "/Cellar/" in s or "/node_modules/" in s:
            return
        if s not in dirs:
            dirs.append(s)

    # 1. Python directory
    py_dir = str(Path(get_python_exe()).parent)
    add(py_dir)

    # 2. ~/.local/bin (where agy and jarviee live)
    add(Path.home() / ".local" / "bin")

    # 3. Active terminal node (e.g. from nvm) before homebrew node
    node_bin = shutil.which("node")
    if node_bin:
        add(Path(node_bin).parent)
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        for d in sorted(nvm_dir.glob("*/bin"), reverse=True):
            add(d)

    # 4. Stable Homebrew & system directories
    for c in [
        Path("/opt/homebrew/bin"),
        Path("/opt/homebrew/sbin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]:
        add(c)

    # 5. Include any other existing directories from current PATH (omitting Cellar / node_modules)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        add(p)

    return os.pathsep.join(dirs)


def generate_plist_dict(cfg=None):
    """Build the dictionary structure for dev.jarviee.server.plist."""
    py_exe = service_python_exe()
    hub_script = str((ROOT / "hub.py").resolve())
    log_file = str(DEFAULT_LOG_PATH.resolve())
    path_str = resolve_path_dirs()

    return {
        "Label": PLIST_LABEL,
        "ProgramArguments": [py_exe, hub_script, "serve"],
        "WorkingDirectory": str(ROOT.resolve()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_file,
        "StandardErrorPath": log_file,
        "EnvironmentVariables": {
            "PATH": path_str,
            "PYTHONUNBUFFERED": "1",
            "LANG": "en_US.UTF-8",
        },
    }


def render_plist_xml(cfg=None):
    """Render the XML string of the plist."""
    d = generate_plist_dict(cfg)
    return plistlib.dumps(d).decode("utf-8")


def plist_path(label=PLIST_LABEL):
    return DEFAULT_PLIST_DIR / f"{label}.plist"


def installed_label():
    """The label launchd knows the service by: Jarviee's, or the old Agent Hub one until reinstalled."""
    for label in (PLIST_LABEL, *LEGACY_LABELS):
        if plist_path(label).is_file():
            return label
    return PLIST_LABEL


def is_installed():
    return sys.platform == "darwin" and plist_path(installed_label()).is_file()


def _remove_legacy(uid):
    for label in LEGACY_LABELS:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        old = plist_path(label)
        if old.is_file():
            old.unlink()
            print(f"Removed old service {label}")


def get_uid():
    return os.getuid() if hasattr(os, "getuid") else None


def _require_darwin(action="service"):
    if sys.platform != "darwin":
        print(
            f"launchd background service ({action}) is only available on macOS (this platform is {sys.platform}). "
            "Run `python3 hub.py serve` directly instead.",
            file=sys.stderr,
        )
        return False
    return True


def _real_home():
    """The account's real home directory from the user database (ignoring $HOME), or None."""
    if pwd is None or not hasattr(os, "getuid"):
        return None
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError):
        return None


def _check_real_home(action, force=False):
    if force:
        return True
    real = _real_home()
    if real is None:
        return True
    cur = Path.home().resolve()
    if cur != real:
        print(
            f"Refusing `service {action}`: HOME is {cur}, which is not your real home directory ({real}). "
            "launchd always operates on your real login session and would affect your running service. "
            "Unset HOME or pass --force to override.",
            file=sys.stderr,
        )
        return False
    return True


def is_running():
    from hub.server_health import check_health
    return check_health()


def install(cfg=None, dry_run=False, force=False):
    """Write LaunchAgent plist and register with launchd."""
    if not _require_darwin("install"):
        return False
    target = plist_path()
    xml_str = render_plist_xml(cfg)
    if dry_run:
        print(f"=== DRY RUN: {target} ===")
        print(xml_str)
        return True
    if not _check_real_home("install", force=force):
        return False

    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(xml_str, encoding="utf-8")

    uid = get_uid()
    _remove_legacy(uid)
    # Unload first if previously bootstrapped
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
                   capture_output=True)
    # bootout returns before the old job is gone; bootstrapping too early fails with "5: Input/output error"
    for _ in range(20):
        if subprocess.run(["launchctl", "print", f"gui/{uid}/{PLIST_LABEL}"], capture_output=True).returncode != 0:
            break
        time.sleep(0.25)
    for attempt in range(3):
        res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)],
                             capture_output=True, text=True)
        if res.returncode == 0:
            break
        time.sleep(1 + attempt)
    if res.returncode != 0:
        print(f"error bootstrapping launchd service: {res.stderr.strip()}", file=sys.stderr)
        return False
    print(f"Installed and started launchd service: {PLIST_LABEL}")
    print(f"Plist: {target}")
    print(f"Log:   {DEFAULT_LOG_PATH}")
    return True


def uninstall(force=False):
    """Unregister launchd service and delete plist file."""
    if not _require_darwin("uninstall"):
        return False
    if not _check_real_home("uninstall", force=force):
        return False
    uid = get_uid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
                   capture_output=True)
    _remove_legacy(uid)
    target = plist_path()
    if target.is_file():
        target.unlink()
        print(f"Removed {target}")
    print(f"Uninstalled service {PLIST_LABEL}")
    return True


def restart():
    """Kickstart the launchd service."""
    if not _require_darwin("restart"):
        return False
    uid = get_uid()
    label = installed_label()
    res = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                         capture_output=True, text=True)
    if res.returncode == 0:
        print(f"Restarted service {label}")
        return True
    print(f"Could not kickstart service ({res.stderr.strip()}). Is it installed?", file=sys.stderr)
    return False


def status(cfg=None):
    """Print service status and health check."""
    if not _require_darwin("status"):
        return False
    uid = get_uid()
    label = installed_label()
    target = plist_path(label)
    installed = target.is_file()
    print(f"Service Label:  {label}")
    print(f"Plist path:     {target} ({'present' if installed else 'missing'})")
    # Query launchctl print
    res = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                         capture_output=True, text=True)
    loaded = res.returncode == 0
    print(f"Launchd status: {'loaded' if loaded else 'not loaded'}")

    # Query HTTP health
    from hub.server_health import check_health_info
    ok, info = check_health_info(cfg)
    if ok:
        print(f"HTTP Server:    healthy (PID: {info.get('pid')}, port: {info.get('port')})")
    else:
        print(f"HTTP Server:    not responding ({info.get('error', 'offline')})")
    return loaded and ok


def logs(lines=40):
    """Show the tail of the service log."""
    if not _require_darwin("logs"):
        return False
    if not DEFAULT_LOG_PATH.is_file():
        print(f"Log file not found: {DEFAULT_LOG_PATH}")
        return True
    try:
        res = subprocess.run(["tail", f"-n{lines}", str(DEFAULT_LOG_PATH)],
                             capture_output=True, text=True)
        print(res.stdout)
    except Exception as e:
        print(f"Error reading log: {e}", file=sys.stderr)
    return True
