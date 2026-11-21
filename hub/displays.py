"""Screens, window placement, the iTerm2 "Jarviee" dynamic profile and the macOS checks.

Pure helpers (pick_screen, to_top_left, window_bounds, profile, problems) are unit-tested;
the readers shell out to osascript / system_profiler / defaults and never raise.
"""
import json
import os
import subprocess
from pathlib import Path

PROFILE_NAME = "Jarviee"
PROFILE_GUID = "01195A94-BA5C-42F6-A96C-F25F4BABFCAB"
PROFILE_PATH = Path.home() / "Library/Application Support/iTerm2/DynamicProfiles/jarviee.json"
LEGACY_PROFILE_PATH = PROFILE_PATH.with_name("agent-hub.json")   # removed when the new one is written
# From iTerm2 sources/Settings/Profiles/ITAddressBookMgr.h (checked 2026-11-09):
WINDOW_TYPE_NORMAL = 0
WINDOW_TYPE_LION_FULL_SCREEN = 4   # macOS native full screen: the window gets its own Space
# KEY_SCREEN: -1 = no preference, -2 = screen with cursor, i = [NSScreen screens][i] (0 = main).
SCREEN_NO_PREFERENCE = -1

SCREENS_JXA = r'''ObjC.import("AppKit"); ObjC.import("CoreGraphics");
ObjC.bindFunction("CGDisplayIsBuiltin", ["bool", ["unsigned int"]]);
function run() {
  var s = $.NSScreen.screens, out = [];
  for (var i = 0; i < s.count; i++) {
    var sc = s.objectAtIndex(i), f = sc.frame, v = sc.visibleFrame;
    var id = ObjC.unwrap(sc.deviceDescription.objectForKey("NSScreenNumber"));
    out.push({index: i, id: id, name: ObjC.unwrap(sc.localizedName), builtin: $.CGDisplayIsBuiltin(id),
              frame: [f.origin.x, f.origin.y, f.size.width, f.size.height],
              visible: [v.origin.x, v.origin.y, v.size.width, v.size.height], scale: sc.backingScaleFactor});
  }
  return JSON.stringify(out);
}'''


def to_top_left(screens):
    """Cocoa rects (origin bottom-left of the main screen) → AppleScript bounds space (top-left)."""
    if not screens:
        return []
    h0 = screens[0]["frame"][3]
    out = []
    for s in screens:
        s = dict(s)
        for key in ("frame", "visible"):
            x, y, w, h = s.get(key) or s["frame"]
            s[key] = [x, h0 - (y + h), w, h]
        out.append(s)
    return out


def cross_check(screens, profiler):
    """Fill or confirm `builtin` from `system_profiler SPDisplaysDataType -json`. Returns (screens, note)."""
    internal = [d for g in (profiler or {}).get("SPDisplaysDataType", [])
                for d in g.get("spdisplays_ndrvs", []) if d.get("spdisplays_connection_type") == "spdisplays_internal"]
    flagged = [s for s in screens if s.get("builtin")]
    if not flagged and internal:   # CGDisplayIsBuiltin unavailable: fall back to names
        names = {d.get("_name", "").lower() for d in internal}
        for s in screens:
            n = str(s.get("name", "")).lower()
            s["builtin"] = n in names or "built-in" in n
        flagged = [s for s in screens if s.get("builtin")]
    note = ""
    if profiler and len(flagged) != len(internal):
        note = f"macOS reports {len(flagged)} built-in screen(s) but system_profiler lists {len(internal)}"
    return screens, note


def pick_screen(screens, setting="secondary"):
    """(screen, why) for the Jarviee window. screens[0] is the main display.

    "secondary" (default): the built-in screen when it isn't the main one, else the first other
    screen. "builtin", "main", or an index pick directly. One screen: that screen.
    """
    if not screens:
        return None, "macOS reported no screens"
    main = screens[0]
    if len(screens) == 1:
        return main, "only one screen is connected"
    s = str(setting if setting is not None else "secondary").strip().lower()
    if s == "main":
        return main, ""
    if s.lstrip("-").isdigit():
        i = int(s)
        if 0 <= i < len(screens):
            return screens[i], ""
        s, why = "secondary", f"no screen {i}; using the secondary screen"
    else:
        why = ""
    if s == "builtin":
        found = next((x for x in screens if x.get("builtin")), None)
        return (found, "") if found else (main, "no built-in screen; using the main screen")
    others = screens[1:]
    return next((x for x in others if x.get("builtin")), others[0]), why


def window_bounds(visible, position="right", width_pct=60):
    """{left, top, right, bottom} for a normal window inside a screen's visible rect (top-left space)."""
    x, y, w, h = visible
    pct = max(20, min(100, int(width_pct or 60)))
    width = round(w * pct / 100)
    left = x if position == "left" else x + w - width if position == "right" else x
    if position not in ("left", "right"):
        width = w
    return [int(left), int(y), int(left + width), int(y + h)]


WINDOW_PROFILE = "Jarviee (window)"
WINDOW_GUID = "6C2E8F4A-3B71-4D95-A0E2-58F1C9D7B342"
PROFILE_RELOAD_WAIT = 2.5   # seconds for iTerm2 to pick up a changed DynamicProfiles file


def profile(screen_index, font=None):
    """Both dynamic profiles: "Jarviee" opens native full screen on `screen_index`, "Jarviee (window)"
    opens a normal window (placed by bounds). Two profiles, so switching between them never rewrites the
    file: iTerm2 reloads it asynchronously, and a window opened right after a rewrite gets the old one.
    `font` ("<PostScript name> <size>") sets the pane font; everything else is inherited from the default profile."""
    title = {"Use Custom Window Title": True, "Custom Window Title": PROFILE_NAME}
    if font:
        title["Normal Font"] = font
    return {"Profiles": [
        {"Name": PROFILE_NAME, "Guid": PROFILE_GUID, "Window Type": WINDOW_TYPE_LION_FULL_SCREEN,
         "Screen": int(screen_index) if screen_index is not None else SCREEN_NO_PREFERENCE, **title},
        {"Name": WINDOW_PROFILE, "Guid": WINDOW_GUID, "Window Type": WINDOW_TYPE_NORMAL,
         "Screen": SCREEN_NO_PREFERENCE, **title},
    ]}


ITERM_PREFS = Path.home() / "Library/Preferences/com.googlecode.iterm2.plist"
FALLBACK_FONT = "Menlo-Regular"


def default_font(prefs=ITERM_PREFS):
    """PostScript name of the default iTerm2 profile's font (e.g. "MesloLGS-NF-Regular"), else Menlo."""
    import plistlib
    try:
        with open(prefs, "rb") as f:
            data = plistlib.load(f)
        books = data.get("New Bookmarks") or []
        guid = data.get("Default Bookmark Guid")
        book = next((b for b in books if b.get("Guid") == guid), books[0] if books else {})
        name = str(book.get("Normal Font") or "").rsplit(" ", 1)[0].strip()
        return name or FALLBACK_FONT
    except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
        return FALLBACK_FONT


def font_spec(size, name=None):
    """"<name> <size>" for the profile, or None when no size is set (keep the default profile's font)."""
    try:
        size = float(size)
    except (TypeError, ValueError):
        return None
    if not 6 <= size <= 36:
        return None
    return f"{name or default_font()} {size:g}"


def current_screen_index(path=PROFILE_PATH):
    """The full screen profile's Screen in the installed file, or None."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))["Profiles"][0]["Screen"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def inside(rect_lrtb, frame):
    """True when the centre of an AppleScript bounds {l,t,r,b} lies in a top-left frame [x, y, w, h]."""
    l, t, r, b = rect_lrtb
    x, y, w, h = frame
    cx, cy = (l + r) / 2, (t + b) / 2
    return x <= cx < x + w and y <= cy < y + h


def install_profile(data, path=PROFILE_PATH):
    """Write the profile only when it changed (iTerm2 reloads the folder on change). True if written."""
    path = Path(path)
    text = json.dumps(data, indent=2) + "\n"
    if path == PROFILE_PATH and LEGACY_PROFILE_PATH.is_file():
        try:   # the "Agent Hub" profiles from before the rename; iTerm2 drops them when the file goes
            LEGACY_PROFILE_PATH.unlink()
        except OSError:
            pass
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def problems(lion, spans):
    """Notices for settings native full screen needs. Values are `defaults read` output or None (unset)."""
    out = []
    if str(lion).strip() == "0":
        out.append("iTerm2 Settings › General › Window › “Native full screen windows” is off, so the "
                   "Jarviee window can't get its own Space. Turn it on.")
    if str(spans).strip() == "1":
        out.append("macOS System Settings › Desktop & Dock › “Displays have separate Spaces” is off, so full "
                   "screen on the laptop would blank the main monitor too. Turn it on, then log out and back in.")
    return out


def _out(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def read_screens(full=True):
    """Screens in top-left coordinates; `full` cross-checks `builtin` with system_profiler (~1 s).
    [] if osascript fails."""
    raw = _out(["osascript", "-l", "JavaScript", "-e", SCREENS_JXA])
    try:
        screens = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        screens = []
    try:
        prof = json.loads(_out(["system_profiler", "SPDisplaysDataType", "-json"], 20) or "{}") if full else {}
    except json.JSONDecodeError:
        prof = {}
    screens, _ = cross_check(to_top_left(screens), prof)
    return screens


ITERM_BUNDLE = "com.googlecode.iterm2"
FRONT_JXA = 'ObjC.import("AppKit"); ObjC.unwrap($.NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier)'


def frontmost():
    """Bundle id of the app in front ('' if unknown). Reading it needs no Automation permission."""
    return _out(["osascript", "-l", "JavaScript", "-e", FRONT_JXA], 5) or ""


def activate(bundle):
    """Bring an app back to the front through LaunchServices (`open -b`), no Apple Events needed."""
    if bundle and all(c.isalnum() or c in ".-" for c in bundle):
        _out(["open", "-b", bundle], 5)


def read_problems():
    return problems(_out(["defaults", "read", "com.googlecode.iterm2", "UseLionStyleFullscreen"]),
                    _out(["defaults", "read", "com.apple.spaces", "spans-displays"]))
