"""HTTP handlers for /api/orch/*: the lead's team commands (hub/orch.py). POST only, with the install token; the lead's
run id travels in the body (`hub.py orch` reads it from JARVIEE_RUN_ID)."""
from . import orch
from .respond import opt_str, req_str


def _files(body):
    raw = body.get("files")
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError("'files' must be a list of paths")
    return [x.strip() for x in raw if x.strip()][:40]


def _int(body, key):
    try:
        return int(body.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a worker number")


def post(path, body, cfg, store):
    """(http code, body), or None for a path that isn't an orchestration command."""
    action = path[len("/api/orch/"):]
    try:
        lead = req_str(body, "lead")
        if action == "assign":
            return 200, orch.assign(cfg, store, lead, opt_str(body, "task") or "", opt_str(body, "load") or "",
                                    opt_str(body, "tool") or "", opt_str(body, "model") or "",
                                    opt_str(body, "project") or "", _files(body),
                                    int(body.get("redo") or 0), opt_str(body, "note") or "")
        if action == "wait":
            try:
                timeout = float(body.get("timeout") or orch.WAIT_MAX)
            except (TypeError, ValueError):
                timeout = orch.WAIT_MAX
            return 200, orch.wait(cfg, store, lead, timeout)
        if action == "status":
            return 200, orch.status(cfg, store, lead)
        if action == "result":
            return 200, orch.result(cfg, store, lead, _int(body, "worker"))
        if action == "review":
            return 200, orch.review(cfg, lead, _int(body, "worker"), opt_str(body, "verdict") or "",
                                    opt_str(body, "note") or "")
    except ValueError as e:
        return 400, {"error": str(e)}
    return None
