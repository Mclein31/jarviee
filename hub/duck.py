"""Audio ducking for voice reports: the report plays at `report_volume`, everything else drops to about `duck_volume`.

macOS has one output volume and no per-app mixer, so the system volume decides how loud the report is. Lowering it
makes the report quieter too: afplay's `-v` cannot play louder than the system volume allows. So the system volume
is never used to quiet other audio. Instead:

  1. The system output volume is set to `report_volume` while the report plays, and the report plays at full gain.
  2. Other audio is quieted at its source:
       pause   Music and Spotify are paused through AppleScript, and playing <video>/<audio> elements in Chrome, Arc,
               Brave, Edge and Safari tabs are paused with JavaScript sent over Apple Events.
       volume  the same sources keep playing at their own volume lowered to `duck_volume` / `report_volume`, so they
               come out at about `duck_volume` of full while the system sits at `report_volume`.
       both    pause what can be paused, lower what is still playing.
     In every mode, a player that refused to pause is lowered instead.
  3. A source that can't be paused or lowered (a browser with "Allow JavaScript from Apple Events" off, Podcasts)
     can't be quieted without quieting the report as well. The report still plays at `report_volume`; that source
     plays along at the same level and is listed in the state's `stuck` (and the log says how to fix it).
Both percentages are of the Mac's full volume, not of the volume it was at.

Restore: resume and re-raise only what Jarviee changed, then put the exact previous volume and mute state back, in a
finally. The state is written to disk before anything is touched, so a crash mid-report is undone the next time
Jarviee starts (`recover`). Several queued reports are ducked once (voice.Speaker holds the duck across its queue).
"""
import json
import os
import sys
import time
from pathlib import Path

from . import brand

MODES = ("pause", "volume", "both")
DEFAULTS = {"duck_enabled": True, "duck_mode": "pause", "duck_volume": 20, "report_volume": 70}

# Players with an AppleScript dictionary: (bundle id, label). Fixed literals, never built from input.
PLAYERS = (("com.apple.Music", "Music"), ("com.spotify.client", "Spotify"))
# Running but not controllable from AppleScript: forces the volume fallback while it runs.
UNCONTROLLABLE = (("com.apple.podcasts", "Podcasts"),)
CHROMIUM = (("com.google.Chrome", "Chrome"), ("company.thebrowser.Browser", "Arc"),
            ("com.brave.Browser", "Brave"), ("com.microsoft.edgemac", "Edge"))
SAFARI = (("com.apple.Safari", "Safari"),)

# Every <video>/<audio> on the page, including those inside open shadow roots (web components) and same-origin
# frames. Cross-origin frames (a YouTube player embedded on another site) can't be reached by page script.
_ALL_MEDIA = ("const all=(r,o=[])=>{try{r.querySelectorAll('*').forEach(e=>{if(e.matches&&e.matches('video,audio'))o.push(e);"
              "if(e.shadowRoot)all(e.shadowRoot,o);if(e.tagName==='IFRAME'){try{const d=e.contentDocument;if(d)all(d,o)}catch(x){}}})"
              "}catch(x){}return o};")
PAUSE_JS = ("(()=>{" + _ALL_MEDIA + "let n=0;all(document).forEach(m=>{if(!m.paused&&!m.ended){"
            "m.pause();m.dataset.jarvieePaused='1';n++}});return n})()")
RESUME_JS = ("(()=>{" + _ALL_MEDIA + "let n=0;all(document).forEach(m=>{if(m.dataset.jarvieePaused){"
             "delete m.dataset.jarvieePaused;try{m.play()}catch(e){};n++}});return n})()")

GET_VOLUME = 'set s to get volume settings\nreturn ((output volume of s) as text) & "," & ((output muted of s) as text)'
SET_VOLUME = "on run argv\nset volume output volume ((item 1 of argv) as integer)\nend run"
SET_MUTED = 'on run argv\nset volume output muted ((item 1 of argv) is "true")\nend run'


def running_script(bundle):
    return f'return (application id "{bundle}" is running) as text'


def player_pause_script(bundle):
    return (f'if application id "{bundle}" is not running then return "off"\n'
            f'tell application id "{bundle}"\n'
            '\tif (player state as text) is "playing" then\n\t\tpause\n\t\treturn "paused"\n\tend if\nend tell\n'
            'return "idle"')


def player_lower_script(bundle):
    """argv: the target app volume (0-100). Lowers a playing player; returns its previous volume, "idle" or "off"."""
    return (f'on run argv\nif application id "{bundle}" is not running then return "off"\n'
            f'tell application id "{bundle}"\n'
            '\tif (player state as text) is not "playing" then return "idle"\n'
            '\tset v to sound volume\n'
            '\tif v > ((item 1 of argv) as integer) then set sound volume to ((item 1 of argv) as integer)\n'
            '\treturn v as text\nend tell\nend run')


def player_volume_script(bundle):
    """argv: the app volume to put back."""
    return (f'on run argv\nif application id "{bundle}" is running then tell application id "{bundle}" to '
            'set sound volume to ((item 1 of argv) as integer)\nend run')


def lower_js(level):
    """Lower every playing media element to `level` (0-1), keeping its own volume to put back."""
    f = f"{max(0.0, min(1.0, float(level))):.3f}"
    return ("(()=>{" + _ALL_MEDIA + "let n=0;all(document).forEach(m=>{if(!m.paused&&!m.ended&&m.volume>" + f
            + "){m.dataset.jarvieeVol=String(m.volume);m.volume=" + f + ";n++}});return n})()")


RAISE_JS = ("(()=>{" + _ALL_MEDIA + "let n=0;all(document).forEach(m=>{if(m.dataset.jarvieeVol!==undefined){"
            "const v=parseFloat(m.dataset.jarvieeVol);if(!isNaN(v))m.volume=v;delete m.dataset.jarvieeVol;n++}});return n})()")


def player_resume_script(bundle):
    return f'if application id "{bundle}" is running then tell application id "{bundle}" to play'


def browser_script(bundle, safari=False):
    """argv: the JavaScript. Returns the summed count over every tab, "off", or "denied" (JS from Apple Events off)."""
    run_js = "set r to do JavaScript js in t" if safari else "tell t to set r to execute javascript js"
    return (f'on run argv\nset js to item 1 of argv\n'
            f'if application id "{bundle}" is not running then return "off"\nset n to 0\n'
            f'tell application id "{bundle}"\n'
            '\trepeat with w in windows\n\t\trepeat with t in tabs of w\n\t\t\ttry\n'
            f'\t\t\t\t{run_js}\n'
            '\t\t\t\ttry\n\t\t\t\t\tset n to n + (r as integer)\n\t\t\t\tend try\n'
            '\t\t\ton error errMsg number errNum\n'
            '\t\t\t\tif errNum is -1743 or errMsg contains "JavaScript" then return "denied"\n'
            '\t\t\tend try\n\t\tend repeat\n\tend repeat\nend tell\nreturn n as text\nend run')


def settings(cfg):
    v = dict(DEFAULTS)
    vc = (cfg or {}).get("voice")
    vc = vc if isinstance(vc, dict) else {}
    v.update({k: vc.get(k) for k in DEFAULTS if vc.get(k) is not None})
    v["duck_enabled"] = bool(v["duck_enabled"])
    if not isinstance(v["duck_mode"], str) or v["duck_mode"] not in MODES:
        v["duck_mode"] = DEFAULTS["duck_mode"]
    for k in ("duck_volume", "report_volume"):
        try:
            v[k] = max(0, min(100, int(v[k])))
        except (TypeError, ValueError, OverflowError):   # NaN, Infinity, a list, a string that is not a number
            v[k] = DEFAULTS[k]
    return v


def state_path(cfg):
    p = (cfg or {}).get("duck_state_path")
    return Path(os.path.expanduser(p)) if p else brand.home() / "duck.json"


def osascript(script, args=()):
    """The real runner: stdout of `osascript -e script args...`. Raises on failure."""
    import subprocess
    r = subprocess.run(["osascript", "-e", script, *[str(a) for a in args]], capture_output=True, text=True,
                       timeout=10)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "osascript failed").strip()[:200])
    return (r.stdout or "").strip()


class Ducker:
    def __init__(self, cfg, run=None):
        self.cfg = cfg
        self.run = run or osascript

    def _try(self, script, args=()):
        try:
            return str(self.run(script, args)).strip()
        except Exception as e:   # an app or browser that isn't installed, or refuses: skip it
            return f"error: {e}"

    def _save(self, st):
        p = state_path(self.cfg)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(st), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except OSError as e:
            print(f"[duck] state not saved: {e}", file=sys.stderr)

    def _clear(self):
        try:
            state_path(self.cfg).unlink()
        except OSError:
            pass

    def volume(self):
        """(volume 0-100 or None, muted bool or None) from `get volume settings`."""
        out = self._try(GET_VOLUME)
        vol, _, muted = out.partition(",")
        try:
            v = int(vol)
        except ValueError:
            v = None
        return v, {"true": True, "false": False}.get(muted.strip())

    def begin(self):
        """Duck. Returns the state to hand to end() (also on disk until end() finishes), or None when ducking is off."""
        s = settings(self.cfg)
        if not s["duck_enabled"] or sys.platform != "darwin" and self.run is osascript:
            return None
        vol, muted = self.volume()
        held = _held_by_other(self.cfg)
        if held and held.get("volume") is not None:   # another Jarviee is mid-report: the volume we read is its
            vol, muted = int(held["volume"]), held.get("muted")   # report level, so put back what it will
        st = {"pid": os.getpid(), "at": time.time(), "volume": vol, "muted": muted, "players": [], "browsers": [],
              "lowered": [], "lowered_browsers": [], "stuck": [], "ducked_volume": False, "gain": 1.0}
        self._save(st)   # before anything changes: a crash from here on is undone at the next start
        pause = s["duck_mode"] in ("pause", "both")
        lower = s["duck_mode"] in ("volume", "both")
        # other audio, as a share of full, ends up at duck/report once the system sits at report_volume
        app_level = min(100, round(100 * s["duck_volume"] / max(1, s["report_volume"])))
        for bundle, label in PLAYERS:
            r = self._try(player_pause_script(bundle)) if pause else ""
            if r == "paused":
                st["players"].append(bundle)
            elif r in ("off", "idle"):
                pass
            elif lower or r.startswith("error"):   # volume mode, or it would not pause: lower it instead
                old = self._try(player_lower_script(bundle), [app_level])
                if old.isdigit():
                    st["lowered"].append([bundle, int(old)])
                elif old.startswith("error") and self._try(running_script(bundle)) == "true":
                    st["stuck"].append(label)
            self._save(st)
        for bundle, label in UNCONTROLLABLE:
            if self._try(running_script(bundle)) == "true":
                st["stuck"].append(label)
        for group, safari in ((CHROMIUM, False), (SAFARI, True)):
            for bundle, label in group:
                js = PAUSE_JS if pause else lower_js(s["duck_volume"] / max(1, s["report_volume"]))
                r = self._try(browser_script(bundle, safari), [js])
                if r == "denied":
                    st["stuck"].append(label)
                elif r.isdigit() and int(r) > 0:
                    st["browsers" if pause else "lowered_browsers"].append([bundle, safari])
                self._save(st)
        if vol is not None and not muted:
            target = s["report_volume"]   # % of the Mac's full volume, not of where it was
            if target != vol:
                self._try(SET_VOLUME, [target])
                st["ducked_volume"] = True
                self._save(st)
        if st["stuck"]:
            print(f"[duck] can't quiet {', '.join(st['stuck'])} without quieting the report "
                  "(browsers: turn on Allow JavaScript from Apple Events)", file=sys.stderr)
        return st

    def end(self, st):
        """Resume only what begin() paused and restore the exact volume and mute state. Never raises."""
        if not st:
            return
        try:
            for bundle in st.get("players") or []:
                self._try(player_resume_script(bundle))
            for bundle, safari in st.get("browsers") or []:
                self._try(browser_script(bundle, bool(safari)), [RESUME_JS])
            for bundle, old in st.get("lowered") or []:
                self._try(player_volume_script(bundle), [int(old)])
            for bundle, safari in st.get("lowered_browsers") or []:
                self._try(browser_script(bundle, bool(safari)), [RAISE_JS])
            if st.get("ducked_volume") and st.get("volume") is not None:
                self._try(SET_VOLUME, [int(st["volume"])])
            if st.get("ducked_volume") and st.get("muted") is not None:
                self._try(SET_MUTED, ["true" if st["muted"] else "false"])
        finally:
            self._clear()


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except OSError:
        return True


def _held_by_other(cfg):
    """The saved state of a report another live Jarviee process is ducking right now, or None."""
    try:
        st = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if st.get("pid") and int(st["pid"]) != os.getpid() and _alive(st["pid"]) and time.time() - float(
            st.get("at") or 0) < 600:
        return st
    return None


def recover(cfg, run=None):
    """At startup: a report that was cut off by a crash left its state on disk; undo it. True if something was."""
    p = state_path(cfg)
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if st.get("pid") and int(st["pid"]) != os.getpid() and _alive(st["pid"]) and time.time() - float(
            st.get("at") or 0) < 600:
        return False   # another Jarviee process is speaking right now
    Ducker(cfg, run).end(st)
    return True
