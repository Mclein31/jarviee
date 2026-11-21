"""The photo behind the glass (owner, 2026-11-15: "follow my desktop").

`desktop` (the default) reads the current macOS desktop picture from the wallpaper store
(~/Library/Application Support/com.apple.wallpaper/Store/Index.plist), so no Automation permission is needed. A
system wallpaper is stored as a `.madesktop` reference; its still lives as `<name>.heic` in the downloaded wallpaper
assets or /System/Library/Desktop Pictures. A wallpaper with no still (an aerial video, a solid colour) falls back to
`big-sur-road`. Other choices name a still on this Mac. `none` means a plain dark ground.

The chosen file is converted once with `sips` to a 2560 px JPEG in ~/.jarviee/wallpaper/ (browsers can't show HEIC),
keyed by its path and mtime, and served from GET /api/wallpaper. Nothing leaves the Mac. A dynamic wallpaper keeps its
light and dark appearances as two images of one HEIC (`apple_desktop:apr` / `:h24` / `:solar` metadata); Jarviee is dark
only, so the dark one is taken, through a small ImageIO helper (hub/heic_frame.swift, compiled once with swiftc).
"""
import base64
import hashlib
import os
import re
import shutil
import plistlib
import subprocess
import threading
import urllib.parse
from pathlib import Path

from . import brand

HOME = Path.home()
ASSETS = HOME / "Library/Application Support/com.apple.mobileAssetDesktop"
SYSTEM = Path("/System/Library/Desktop Pictures")
STORE = HOME / "Library/Application Support/com.apple.wallpaper/Store/Index.plist"
CHOICES = {   # id -> (label, candidate files); "desktop" and "none" are special
    "desktop": ("Follow my desktop", ()),
    "big-sur-road": ("Big Sur Road", (ASSETS / "Big Sur Road.heic", SYSTEM / "Big Sur Road.heic")),
    "catalina-sunset": ("Catalina Sunset", (ASSETS / "Catalina Sunset.heic", SYSTEM / "Catalina Sunset.heic")),
    "catalina-clouds": ("Catalina Clouds", (ASSETS / "Catalina Clouds.heic", SYSTEM / "Catalina Clouds.heic")),
    "none": ("Plain dark", ()),
}
STILL = (".heic", ".jpg", ".jpeg", ".png", ".tif", ".tiff")
HELPER_SRC = Path(__file__).with_name("heic_frame.swift")
_META_RE = re.compile(rb'apple_desktop:(apr|h24|solar)="([A-Za-z0-9+/=]+)"')
_lock = threading.Lock()


def _extra_choices(cfg):
    items = ((cfg or {}).get("appearance") or {}).get("wallpapers")
    if not isinstance(items, list):
        return {}
    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        wid = str(item.get("id") or "").strip()
        raw_path = item.get("path")
        if not wid or wid in ("desktop", "none") or not isinstance(raw_path, str) or not raw_path.strip():
            continue
        label = str(item.get("label") or wid).strip() or wid
        out[wid] = (label, (Path(os.path.expanduser(raw_path.strip())),))
    return out


def choices(cfg=None):
    """Built-in presets plus any extra presets configured under cfg['appearance']['wallpapers']."""
    extra = _extra_choices(cfg)
    if not extra:
        return dict(CHOICES)
    merged = {k: v for k, v in CHOICES.items() if k != "none"}
    merged.update(extra)
    merged["none"] = CHOICES["none"]
    return merged


def choice(cfg):
    c = str(((cfg or {}).get("appearance") or {}).get("wallpaper") or "desktop")
    return c if c in choices(cfg) else "desktop"


def _url_path(u):
    u = str(u or "")
    return Path(urllib.parse.unquote(urllib.parse.urlparse(u).path)) if u.startswith("file://") else None


def _still_for(path):
    """A file the browser can be given (after conversion) for a desktop setting, or None."""
    if path is None:
        return None
    if path.suffix.lower() in STILL and path.is_file():
        return path
    if path.suffix == ".madesktop":   # a system wallpaper: its still is downloaded as <name>.heic
        for d in (ASSETS, SYSTEM):
            p = d / (path.stem + ".heic")
            if p.is_file():
                return p
    return None


def desktop_file(store=STORE):
    """The current desktop picture's still file, or None (read from the wallpaper store, no permission prompt)."""
    try:
        with open(store, "rb") as f:
            d = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    for top in ("AllSpacesAndDisplays", "SystemDefault"):
        try:
            choices = d[top]["Desktop"]["Content"]["Choices"]
        except (KeyError, TypeError):
            continue
        for ch in choices or []:
            try:
                conf = plistlib.loads(ch.get("Configuration") or b"")
            except Exception:   # noqa: BLE001  a provider we don't know
                continue
            url = (conf.get("url") or {}).get("relative") if isinstance(conf.get("url"), dict) else conf.get("url")
            still = _still_for(_url_path(url))
            if still:
                return still
    return None


def source(cfg):
    """(choice id, file or None)."""
    all_choices = choices(cfg)
    c = choice(cfg)
    if c == "none":
        return c, None
    if c == "desktop":
        f = desktop_file()
        if f:
            return c, f
        c = "big-sur-road"   # no still for this desktop (aerial video, colour): a quiet photo instead
    for p in all_choices.get(c, ("", ()))[1]:
        if p.is_file():
            return c, p
    return c, None


def dark_index(path):
    """The dark appearance's image index of a dynamic HEIC wallpaper, or None (a plain picture)."""
    if path.suffix.lower() != ".heic":
        return None
    try:
        with open(path, "rb") as f:
            m = _META_RE.search(f.read(1 << 20))   # the XMP sits near the start of the file
        meta = plistlib.loads(base64.b64decode(m.group(2))) if m else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    ap = meta.get("ap", meta) if isinstance(meta, dict) else {}   # apr: {l, d}; h24 / solar: {ap: {l, d}, ...}
    d = ap.get("d") if isinstance(ap, dict) else None
    return d if isinstance(d, int) and d > 0 else None


def _helper(cfg, runner=subprocess.run):
    """The compiled heic_frame helper (rebuilt when its source changes), or None without swiftc."""
    exe = _cache_dir(cfg).parent / "heic_frame"
    stamp = exe.with_suffix(".src")
    try:
        src = HELPER_SRC.read_text()
        if exe.is_file() and stamp.is_file() and stamp.read_text() == src:
            return exe
        if not shutil.which("swiftc"):
            return None
        exe.parent.mkdir(parents=True, exist_ok=True)
        r = runner(["swiftc", "-O", str(HELPER_SRC), "-o", str(exe)], capture_output=True, timeout=240)
        if getattr(r, "returncode", 1):
            return None
        stamp.write_text(src)
        return exe
    except (OSError, subprocess.SubprocessError):
        return None


def _cache_dir(cfg):
    return Path(cfg["wallpaper_dir"]) if (cfg or {}).get("wallpaper_dir") else brand.home() / "wallpaper"


def jpeg(cfg, runner=subprocess.run):
    """(path of the browser-ready JPEG, version) for the chosen wallpaper, or (None, "none")."""
    _, src = source(cfg)
    if src is None:
        return None, "none"
    try:
        st = src.stat()
    except OSError:
        return None, "none"
    v = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|dark".encode()).hexdigest()[:12]
    out = _cache_dir(cfg) / f"{v}.jpg"
    with _lock:
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            for old in out.parent.glob("*.jpg"):   # one wallpaper at a time
                try:
                    old.unlink()
                except OSError:
                    pass
            tmp = out.with_suffix(".tmp.jpg")
            conv = src
            idx = dark_index(src)
            exe = _helper(cfg, runner) if idx is not None else None
            if exe is not None:   # the dark appearance of a dynamic wallpaper
                frame = out.with_suffix(".frame.jpg")
                try:
                    r = runner([str(exe), str(src), str(idx), str(frame)], capture_output=True, timeout=60)
                except (OSError, subprocess.SubprocessError):
                    r = None
                if not getattr(r, "returncode", 1) and frame.is_file():
                    conv = frame
            try:
                r = runner(["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", "-Z", "2560",
                            str(conv), "--out", str(tmp)], capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                r = None
            if conv != src:
                try:
                    conv.unlink()
                except OSError:
                    pass
            if getattr(r, "returncode", 1) or not tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return None, "none"
            os.replace(tmp, out)
    return out, v


def info(cfg):
    all_choices = choices(cfg)
    c, src = source(cfg)
    return {"choice": choice(cfg), "shown": c, "name": src.stem if src else "",
            "options": [{"id": k, "label": v[0]} for k, v in all_choices.items()
                        if k in ("desktop", "none") or any(p.is_file() for p in v[1])]}
