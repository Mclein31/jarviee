"""OmniRoute last-resort fallback gateway: Keychain lookup, health probe, and child env.

OmniRoute (https://github.com/diegosouzapw/OmniRoute) is a local AI gateway
(default ``http://127.0.0.1:20128``) that routes requests across multiple
providers and groups them into named fallback "combos". Jarviee uses it as a
third, last-resort tool only after both ``agy`` and ``claude`` are out of
quota.

Because OmniRoute speaks the Anthropic Messages API, Jarviee runs it through
the existing ``claude`` CLI (``tools.omniroute``) with ``ANTHROPIC_BASE_URL``
pointed at the gateway root (no ``/v1`` suffix) and ``ANTHROPIC_AUTH_TOKEN``
carrying the OmniRoute API key, while clearing ``ANTHROPIC_API_KEY`` so
Claude Code never falls back to the owner's personal Anthropic credentials.

The API key lives in ``OMNIROUTE_API_KEY`` or the macOS Keychain under service
``jarviee-omniroute``. Every function here (except ``store_key``) swallows
failures and never raises or exposes the key.
"""
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

KEYCHAIN_ACCOUNT = "jarviee"
DEFAULTS = {
    "enabled": True,
    "base_url": "http://127.0.0.1:20128",
    "keychain_service": "jarviee-omniroute",
    "health_path": "/v1/models",
    "health_timeout_s": 2,
    "health_cache_s": 30,
}

_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)\S+")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{4,}")

_cache_lock = threading.Lock()
_health_cache = {}   # base_url -> (checked_at, ok, reason)
_key_cache = {}      # keychain_service -> (checked_at, key)


def _positive_float(val, default, allow_zero=False):
    if isinstance(val, bool):
        return default
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if num < 0 or (num == 0 and not allow_zero):
        return default
    return num


def settings(cfg):
    """Return the ``omniroute`` config section merged over defaults."""
    out = dict(DEFAULTS)
    user = cfg.get("omniroute") if isinstance(cfg, dict) else None
    if isinstance(user, dict):
        for k, v in user.items():
            if v is not None:
                out[k] = v
    out["enabled"] = bool(out.get("enabled", True))
    base = str(out.get("base_url") or "").strip().rstrip("/")
    out["base_url"] = base or DEFAULTS["base_url"]
    svc = str(out.get("keychain_service") or "").strip()
    out["keychain_service"] = svc or DEFAULTS["keychain_service"]
    path = str(out.get("health_path") or "").strip() or DEFAULTS["health_path"]
    out["health_path"] = "/" + path.lstrip("/")
    out["health_timeout_s"] = _positive_float(out.get("health_timeout_s"), DEFAULTS["health_timeout_s"])
    out["health_cache_s"] = _positive_float(out.get("health_cache_s"), DEFAULTS["health_cache_s"], allow_zero=True)
    return out


def enabled(cfg):
    """True when OmniRoute is enabled and ``tools.omniroute`` is configured."""
    try:
        tools = cfg.get("tools") if isinstance(cfg, dict) else None
        return bool(settings(cfg)["enabled"] and isinstance(tools, dict) and tools.get("omniroute") is not None)
    except Exception:   # noqa: BLE001
        return False


def api_key(cfg, runner=None):
    """Return the OmniRoute API key from env ``OMNIROUTE_API_KEY`` or macOS Keychain, else None."""
    try:
        env_key = os.environ.get("OMNIROUTE_API_KEY")
        if isinstance(env_key, str) and env_key.strip():
            return env_key.strip()
        s = settings(cfg)
        service = s["keychain_service"]
        cache_s = s["health_cache_s"]
        use_cache = runner is None and cache_s > 0
        now_ts = time.time() if use_cache else 0.0
        if use_cache:
            with _cache_lock:
                cached = _key_cache.get(service)
            if cached is not None:
                checked_at, cached_key = cached
                if 0 <= now_ts - checked_at < cache_s:
                    return cached_key
        run = runner or subprocess.run
        res = run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        key = None
        if res is not None and getattr(res, "returncode", 1) == 0:
            out = getattr(res, "stdout", None)
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(out, str):
                key = out.strip() or None
        if use_cache:
            with _cache_lock:
                _key_cache[service] = (now_ts, key)
        return key
    except Exception:   # noqa: BLE001
        return None


def store_key(cfg, key, runner=None):
    """Save ``key`` in the macOS Keychain. Raises ValueError if empty, OSError on failure."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("OmniRoute API key must not be empty")
    secret = key.strip()
    service = settings(cfg)["keychain_service"]
    run = runner or subprocess.run
    try:
        res = run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w", secret],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        raise
    except subprocess.SubprocessError as e:
        raise OSError(f"Keychain write failed: {e}") from e
    if res is None or getattr(res, "returncode", 1) != 0:
        err = getattr(res, "stderr", "") if res is not None else ""
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        err = _redact_with_key(str(err or "").strip(), secret)
        code = getattr(res, "returncode", 1)
        raise OSError(err or f"Keychain returned exit code {code}")
    reset_cache()


def tool_env(cfg, runner=None):
    """Return env overrides for running ``claude`` against OmniRoute (None pops a key)."""
    try:
        s = settings(cfg)
        key = api_key(cfg, runner=runner)
        return {
            "ANTHROPIC_BASE_URL": s["base_url"],
            "ANTHROPIC_AUTH_TOKEN": key or None,
            "ANTHROPIC_API_KEY": None,
            "JARVIEE_TOOL": "omniroute",
        }
    except Exception:   # noqa: BLE001
        return {
            "ANTHROPIC_BASE_URL": DEFAULTS["base_url"],
            "ANTHROPIC_AUTH_TOKEN": None,
            "ANTHROPIC_API_KEY": None,
            "JARVIEE_TOOL": "omniroute",
        }


def apply_env(env, cfg, runner=None):
    """Return a copy of ``env`` with ``tool_env`` applied (None values pop keys)."""
    out = dict(env or {})
    for k, v in tool_env(cfg, runner=runner).items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = str(v)
    return out


def reset_cache():
    """Clear cached health and Keychain results (used by tests and after updating the key)."""
    with _cache_lock:
        _health_cache.clear()
        _key_cache.clear()


def _is_timeout(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower()


def _status_code(resp):
    code = getattr(resp, "status", None)
    if code is None and hasattr(resp, "getcode"):
        code = resp.getcode()
    return code


def _http_error_reason(code):
    if code in (401, 403):
        return f"HTTP {code}: key rejected"
    return f"HTTP {code}"


def health(cfg, now=None, opener=None):
    """Probe ``base_url + health_path`` and return ``(ok, reason)`` with per-URL caching."""
    try:
        if not enabled(cfg):
            return False, "disabled"

        s = settings(cfg)
        base_url = s["base_url"]
        cache_s = s["health_cache_s"]
        ts = time.time() if now is None else float(now)

        if cache_s > 0:
            with _cache_lock:
                cached = _health_cache.get(base_url)
            if cached is not None:
                checked_at, ok, reason = cached
                if 0 <= ts - checked_at < cache_s:
                    return ok, reason

        key = api_key(cfg)
        if not key:
            return False, "no API key (python3 hub.py omniroute connect)"

        url = f"{base_url}{s['health_path']}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {key}", "User-Agent": "jarviee"},
        )
        open_fn = opener or urllib.request.urlopen
        timeout = s["health_timeout_s"]

        try:
            resp = open_fn(req, timeout=timeout)
            if hasattr(resp, "__enter__"):
                with resp as r:
                    code = _status_code(r)
            else:
                code = _status_code(resp)
            if code is not None and 200 <= int(code) < 300:
                ok, reason = True, "ok"
            else:
                ok, reason = False, _http_error_reason(code if code is not None else "unknown")
        except urllib.error.HTTPError as e:
            ok, reason = False, _http_error_reason(e.code)
            try:
                e.close()
            except Exception:   # noqa: BLE001
                pass
        except Exception as e:   # noqa: BLE001
            if _is_timeout(e):
                ok, reason = False, "timed out"
            elif isinstance(e, (urllib.error.URLError, OSError)):
                ok, reason = False, f"gateway not running at {base_url}"
            else:
                ok, reason = False, _redact_with_key(str(e), key) or "health check failed"

        if cache_s > 0:
            with _cache_lock:
                _health_cache[base_url] = (ts, ok, reason)
        return ok, reason
    except Exception as e:   # noqa: BLE001
        return False, redact(str(e), cfg) or "health check failed"


def status(cfg, opener=None):
    """Return OmniRoute status summary for CLI and dashboard (never contains the key)."""
    try:
        s = settings(cfg)
        is_on = enabled(cfg)
        key = api_key(cfg)
        ok, reason = health(cfg, opener=opener)
        tools = cfg.get("tools") if isinstance(cfg, dict) else None
        omni_tool = tools.get("omniroute") if isinstance(tools, dict) else None
        raw_models = omni_tool.get("models") if isinstance(omni_tool, dict) else None
        models = dict(raw_models) if isinstance(raw_models, dict) else {}
        return {
            "enabled": is_on,
            "base_url": s["base_url"],
            "has_key": bool(key),
            "healthy": bool(ok),
            "reason": _redact_with_key(reason, key),
            "models": models,
        }
    except Exception:   # noqa: BLE001
        return {
            "enabled": False,
            "base_url": DEFAULTS["base_url"],
            "has_key": False,
            "healthy": False,
            "reason": "status unavailable",
            "models": {},
        }


def _redact_with_key(text, key):
    if text is None or text == "":
        return ""
    out = str(text)
    if key:
        out = out.replace(key, "***")
    out = _BEARER_RE.sub(r"\1***", out)
    out = _SK_RE.sub("***", out)
    return out


def redact(text, cfg=None):
    """Return ``text`` with the OmniRoute key and Bearer/sk- tokens replaced by ``***``."""
    try:
        if text is None or text == "":
            return ""
        return _redact_with_key(text, api_key(cfg))
    except Exception:   # noqa: BLE001
        return ""
