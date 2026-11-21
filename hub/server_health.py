"""Health checking utilities for Jarviee HTTP server."""
import json
import time
import urllib.error
import urllib.request
from hub.config import load_config

_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def get_health_url(cfg=None):
    c = cfg or load_config()
    return f"http://127.0.0.1:{c['port']}/api/health"


def check_health(cfg=None):
    """Return True if /api/health responds with 200 and status ok."""
    ok, _ = check_health_info(cfg)
    return ok


def check_health_info(cfg=None):
    """Query /api/health and return (ok, payload_or_err_dict)."""
    url = get_health_url(cfg)
    try:
        req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{url.split(':')[-1].split('/')[0]}"})
        with _OPEN(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read() or b"{}")
                if data.get("status") == "ok":
                    return True, data
            return False, {"error": f"status {resp.status}"}
    except Exception as e:
        return False, {"error": str(e)}


def wait_healthy(cfg=None, timeout=10.0, interval=0.5):
    """Poll /api/health until healthy or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check_health(cfg):
            return True
        time.sleep(interval)
    return False
