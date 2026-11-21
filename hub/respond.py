"""HTTP response plumbing: strong ETags with 304s, gzip, and a short-lived rendered-response cache.

A polled JSON endpoint is rendered once per change: the body and its ETag are computed over the payload with the
volatile keys (`now`, `age`) left out, so an unchanged poll gets the same ETag and a 304.
"""
import gzip
import hashlib
import json
import threading
import time

VOLATILE = ("now", "age")
MIN_GZIP = 1024
COMPRESSIBLE = ("application/json", "text/html", "text/css", "text/javascript", "text/plain")
SNAPSHOT_TTL = 0.75   # seconds a rendered response is reused while the database has not changed


def compressible(ctype):
    return (ctype or "").split(";")[0].strip().lower() in COMPRESSIBLE


def accepts_gzip(header):
    for part in (header or "").split(","):
        name, _, q = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "x-gzip", "*"):
            q = q.strip().replace(" ", "")
            try:
                return not q.startswith("q=") or float(q[2:] or 0) > 0
            except ValueError:
                return True
    return False


def make_etag(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
        h.update(b"\0")
    return '"' + h.hexdigest()[:24] + '"'


def etag_matches(header, etag):
    """True when an If-None-Match header lists `etag` (weak or strong) or is `*`."""
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*" or (tag[2:] if tag.startswith("W/") else tag) == etag:
            return True
    return False


class Rendered:
    """One serialised response: body bytes, ETag, and a lazily compressed copy."""

    def __init__(self, body, etag, ctype="application/json"):
        self.body, self.etag, self.ctype = body, etag, ctype
        self._gz = None

    def gzipped(self):
        if self._gz is None:
            self._gz = gzip.compress(self.body, 4, mtime=0)
        return self._gz


def render_json(payload, volatile=VOLATILE):
    """Rendered JSON whose ETag ignores the volatile top-level keys (they are still sent, last)."""
    if not isinstance(payload, dict):
        body = json.dumps(payload).encode()
        return Rendered(body, make_etag(body))
    stable = {k: v for k, v in payload.items() if k not in volatile}
    moving = {k: v for k, v in payload.items() if k in volatile}
    core = json.dumps(stable).encode()
    if not moving:
        return Rendered(core, make_etag(core))
    tail = json.dumps(moving).encode()
    body = core[:-1] + (b", " if stable else b"") + tail[1:]
    return Rendered(body, make_etag(core))


class ResponseCache:
    """Rendered responses by key, reused for SNAPSHOT_TTL while `version()` is unchanged; cleared by a POST.

    Concurrent requests share one build (the first builds, the others wait and reuse it). A result built while a
    clear() happened is not stored, so no request after a state-changing POST can see a pre-POST snapshot."""

    def __init__(self, version, ttl=SNAPSHOT_TTL):
        self._version, self._ttl = version, ttl
        self._lock = threading.Lock()
        self._build = threading.Lock()
        self._entries = {}
        self._gen = 0

    def clear(self):
        with self._lock:
            self._gen += 1
            self._entries.clear()

    def _fresh(self, key, version):
        hit = self._entries.get(key)
        if hit and hit[0] == version and time.monotonic() - hit[1] < self._ttl:
            return hit[2]
        return None

    def get(self, key, build):
        """build() -> (code, payload). Returns (code, Rendered or payload); only 200 JSON dicts are rendered."""
        with self._lock:
            version, gen = self._version(), self._gen
            hit = self._fresh(key, version)
        if hit:
            return 200, hit
        with self._build:
            with self._lock:
                hit = self._fresh(key, version)
                if hit:
                    return 200, hit
            code, payload = build()
            if code != 200:
                return code, payload
            rendered = render_json(payload)
            with self._lock:
                if gen == self._gen:
                    self._entries[key] = (version, time.monotonic(), rendered)
            return 200, rendered


def opt_str(body, key):
    """Return body[key] when None or a string; raise ValueError otherwise."""
    v = body.get(key)
    if v is not None and not isinstance(v, str):
        raise ValueError(f"'{key}' must be a string")
    return v


def req_str(body, key):
    """Return body[key] when a string; raise ValueError otherwise."""
    v = body.get(key)
    if not isinstance(v, str):
        raise ValueError(f"'{key}' must be a string")
    return v
