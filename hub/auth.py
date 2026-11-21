"""Authentication & token management for local-only command center protection.

Other local users on the same Mac must not read the token or Jarviee's data: the token file is created 0600 in one
atomic step (never briefly world-readable), and `harden(cfg)` takes group/other access off Jarviee's own data folder
(~/.jarviee: hub.db, run logs, attachments, day book) and its database files at every server start.
"""
import hmac
import os
import secrets
import stat
import tempfile
from pathlib import Path

from . import brand


def token_path(cfg=None):
    if cfg and cfg.get("token_path"):
        return Path(os.path.expanduser(os.path.expandvars(str(cfg["token_path"]))))
    env = brand.env("TOKEN_PATH")
    return Path(os.path.expanduser(env)) if env else brand.home() / "token"


def _read(p):
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def get_token(cfg=None):
    """Get the existing secret token or generate and persist a new one (0600 from the first byte)."""
    p = token_path(cfg)
    tok = _read(p)
    if tok:
        return tok
    if not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tok = secrets.token_hex(24)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".token-")   # mkstemp: 0600, owned by us
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        try:
            os.link(tmp, p)        # appears complete or not at all; fails if another thread / process won the race
        except FileExistsError:
            won = _read(p)
            if won:
                return won
            os.replace(tmp, p)     # an empty or unreadable token file: replace it
        except OSError:            # a filesystem without hard links
            if _read(p):
                return _read(p)
            os.replace(tmp, p)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return tok


def verify_token(provided, cfg=None):
    """Constant-time comparison against expected token. Any header value is fine: bytes are compared, so a
    non-ASCII value is simply wrong instead of raising (hmac refuses non-ASCII str)."""
    if not provided:
        return False
    expected = get_token(cfg)
    return hmac.compare_digest(str(provided).strip().encode("utf-8", "replace"), expected.encode("utf-8", "replace"))


def private(path):
    """Take group and other permissions off a file or folder we own (0600 / 0700 for the usual modes).
    Symlinks and other users' files are left alone. True when it changed the mode. Never raises."""
    if not hasattr(os, "getuid"):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        return False
    mode = stat.S_IMODE(st.st_mode)
    if not mode & 0o077:
        return False
    try:
        os.chmod(path, mode & ~0o077)
        return True
    except OSError:
        return False


def harden(cfg):
    """Close Jarviee's private files to other local users: the token, the database (+ WAL / SHM) and, when it is
    Jarviee's own data folder (~/.jarviee), that whole folder. A database or token placed elsewhere by the owner keeps
    its folder's permissions; only the files themselves are tightened. Returns the paths it changed."""
    changed = []
    own = set()
    for home in (brand.HOME, brand.LEGACY_HOME, brand.home()):
        try:
            if home.is_dir() and not home.is_symlink():
                own.add(home.resolve())
        except OSError:
            pass
    files = [token_path(cfg)]
    db = cfg.get("db_path")
    if db:
        db = brand.data_path(db)
        files += [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]
    for f in files:
        if private(f):
            changed.append(str(f))
        try:
            parent = f.parent.resolve()
        except OSError:
            continue
        if parent in own and private(parent):
            changed.append(str(parent))
    return changed
