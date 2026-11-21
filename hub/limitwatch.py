"""Phone alerts for the Claude 5-hour limit: one when it is all spent, one when the cooldown is over.

The reading comes from the status line (hooks/statusline.py -> ~/.jarviee/claude-limits.json) and, as a second
source, from the quota state a launched run records when Claude answers with a limit error. When the window is
spent, the "spent" alert goes out now and the "back" alert is handed to ntfy as a scheduled message for the reset
time, so it reaches the phone even if this Mac is asleep by then. Each window is announced once
(state in ~/.jarviee/limit-watch.json).
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import brand, push, usage

DEFAULTS = {"limit_alerts": True, "limit_spent_pct": 100}
MIN_DELAY_S = 30   # ntfy will not schedule a message closer than this; a nearer reset is announced when it happens


def settings(cfg):
    return {**DEFAULTS, **push.settings(cfg)}


def _state_file(cfg):
    if (cfg or {}).get("limit_state_path"):
        return Path(cfg["limit_state_path"])
    return brand.home() / "limit-watch.json"


def _load(cfg):
    try:
        data = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cfg, data):
    f = _state_file(cfg)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass


def _span(seconds):
    m = max(0, int(round(seconds / 60)))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def spent_until(cfg, now, store=None, limits=None):
    """The reset time (epoch) of a spent 5-hour window, or None while there is still room."""
    s = settings(cfg)
    if limits is None:
        limits, _ = usage.claude_limits(now)
    for w in (limits or {}).get("windows") or []:
        if w.get("id") == "five_hour" and w.get("used_pct") is not None and w.get("resets_at"):
            if w["used_pct"] >= float(s["limit_spent_pct"]) and w["resets_at"] > now:
                return float(w["resets_at"])
    if store is not None:   # a launched run was refused with a limit error
        try:
            q = store.get_quota("claude", now=now)
        except Exception:   # noqa: BLE001
            q = None
        if q and q.get("status") != "ok" and q.get("until") and now < q["until"] <= now + 5 * 3600 + 600:
            return float(q["until"])
    return None


def check(cfg, now=None, store=None, limits=None, publish=None):
    """Send what is due. Returns the list of alerts sent this call ("spent", "back-scheduled", "back")."""
    s = settings(cfg)
    if not s["enabled"] or not s["limit_alerts"]:
        return []
    now = time.time() if now is None else now
    publish = publish or push.publish
    st, sent = _load(cfg), []
    until = spent_until(cfg, now, store=store, limits=limits)
    if until and st.get("spent_for") != int(until):
        at = datetime.fromtimestamp(until)
        ok, _ = publish(cfg, "Claude 5-hour limit is spent",
                        f"All of it is used. It resets at {at.strftime('%H:%M')}, in {_span(until - now)}.", priority=4, tags=push.icon("limit_spent"))
        if ok:
            st.update(spent_for=int(until), back_sent=False, back_scheduled=False)
            sent.append("spent")
            if until - now >= MIN_DELAY_S:
                ok2, _ = publish(cfg, "Claude 5-hour limit is back", "The cooldown is over. Agents can run again.",
                                 at=at, seq=f"limit-back-{int(until)}", priority=4, tags=push.icon("limit_back"))
                if ok2:
                    st["back_scheduled"] = True
                    sent.append("back-scheduled")
            _save(cfg, st)
    # the reset has passed and ntfy was not holding the message: say it now
    if st.get("spent_for") and now >= st["spent_for"] and not st.get("back_sent") and not st.get("back_scheduled"):
        ok, _ = publish(cfg, "Claude 5-hour limit is back", "The cooldown is over. Agents can run again.", priority=4,
                        tags=push.icon("limit_back"))
        if ok:
            st["back_sent"] = True
            sent.append("back")
            _save(cfg, st)
    return sent


class LimitWatcher(threading.Thread):
    def __init__(self, cfg, store=None, interval=60):
        super().__init__(daemon=True, name="jarviee-limits")
        self.cfg, self.store, self.interval, self._stop = cfg, store, interval, threading.Event()

    def run(self):
        if self._stop.wait(15):   # let the server settle first
            return
        while not self._stop.is_set():
            try:
                check(self.cfg, store=self.store)
            except Exception as e:   # noqa: BLE001
                print(f"[limits] {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
