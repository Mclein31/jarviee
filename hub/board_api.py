"""HTTP side of the Project Board (hub/board.py): the endpoints, plus the guarded auto-advance hooks.

routes.py delegates here (`get` / `post`), the same pattern as queue_api / jobs_api. A handler result is
(status, payload). A `board.NotFound` is a 404, any other ValueError a 400 `{error}` the owner can read.

`notify_run` / `notify_accept` are what the run lifecycle calls (launcher._finish, ingest, inbox.decide): they never raise,
so a board problem can never break a run.

This module only imports `board` at the top; launcher / obsidian / board_polish are imported when needed,
so launcher, ingest and inbox can import it without a cycle.
"""
import os
import threading
from urllib.parse import parse_qs

from . import board as boardlib
from .respond import opt_str, req_str

_boards = {}
_boards_lock = threading.Lock()


def get_board(cfg):
    """The one Board per database file (created on first use)."""
    path = os.path.realpath(os.path.expanduser(str(cfg["db_path"])))
    with _boards_lock:
        b = _boards.get(path)
        if b is None:
            b = _boards[path] = boardlib.Board(path)
        return b


# ---------------------------------------------------------------- auto-advance (called from the run lifecycle)

def notify_run(cfg, run_id, status, files=None):
    """A run's status changed: move its linked card. Never raises."""
    try:
        return get_board(cfg).on_run_status(run_id, status, files)
    except Exception:
        return []


def notify_accept(cfg, run_id):
    """The owner accepted the run in the review inbox: its card is done. Never raises."""
    try:
        return get_board(cfg).on_inbox_accept(run_id)
    except Exception:
        return []


# ---------------------------------------------------------------- helpers

def _polisher():
    """board_polish.polish_async, or None when polishing is unavailable."""
    try:
        from . import board_polish
        return board_polish.polish_async
    except Exception:
        return None


def _polish_enabled(cfg):
    pol = (cfg.get("board") or {}).get("polish") or {}
    return bool(pol.get("enabled", True))


def _start_polish(cfg, board, card_id, polisher):
    """Run the improvement in the background; a failure to even start it just marks the card failed."""
    try:
        polisher(board, card_id, cfg)
    except Exception:
        try:
            board.set_polish(card_id, "failed")
        except Exception:
            pass


def _card_id(body):
    cid = (req_str(body, "id") or "").strip()
    if not cid:
        raise ValueError("Which card? The id is missing.")
    return cid


def _default_project(cfg):
    from . import obsidian
    rows = obsidian.list_projects(cfg)
    return rows[0]["name"] if rows else ""


def _snapshot(cfg, board, project):
    return {"project": project,
            "columns": [{"key": k, "label": lbl} for k, lbl in boardlib.COLUMNS],
            "labels": list(boardlib.LABELS), "priorities": list(boardlib.PRIORITIES),
            "cards": board.list(project) if project else []}


def _projects(cfg, board):
    from . import obsidian
    counts = board.counts()
    names = [p["name"] for p in obsidian.list_projects(cfg)]
    names += sorted(n for n in counts if n not in names)   # a project with cards but no .brain/ any more
    return [{"name": n, "open": counts.get(n, {}).get("open", 0), "done": counts.get(n, {}).get("done", 0)}
            for n in names]


# ---------------------------------------------------------------- GET

def get(path, query, cfg, store=None, tasks=None, sched=None):
    board = get_board(cfg)
    if path == "/api/board":
        project = (parse_qs(query).get("project", [""])[0] or "").strip() or _default_project(cfg)
        return 200, _snapshot(cfg, board, project)
    if path == "/api/board/projects":
        return 200, {"projects": _projects(cfg, board)}
    return None


# ---------------------------------------------------------------- POST

def post(path, body, cfg, store, sched=None):
    handler = _POST.get(path)
    if handler is None:
        return None
    board = get_board(cfg)
    try:
        return handler(body, cfg, store, board)
    except boardlib.NotFound as e:
        return 404, {"error": str(e)}
    except ValueError as e:
        return 400, {"error": str(e)}


def _create(body, cfg, store, board):
    from . import obsidian
    project = req_str(body, "project").strip()
    folder = obsidian.find_project(cfg, project)   # the same check launcher._resolve makes before a run
    if folder is None:
        raise ValueError(f"Unknown project '{project[:80]}' (no {project[:80]}/.brain/ in the workspace).")
    project = folder.name   # the folder's own spelling, so the card lists under it
    polisher = _polisher() if (body.get("polish", True) and _polish_enabled(cfg)) else None
    card = board.create(project, req_str(body, "title"), opt_str(body, "description") or "",
                        opt_str(body, "label") or "feature", opt_str(body, "priority") or "normal",
                        polish="pending" if polisher else "off")
    if polisher:
        _start_polish(cfg, board, card["id"], polisher)
        card = board.get(card["id"]) or card
    return 200, {"ok": True, "card": card}


def _update(body, cfg, store, board):
    cid = _card_id(body)
    before = board.get(cid)
    if before is None:
        raise boardlib.NotFound("That card no longer exists.")
    fields = {}
    for key in ("title", "description", "label", "priority"):
        if key in body:
            fields[key] = req_str(body, key)
    card = board.update(cid, **fields) if fields else before
    changed = (card["title"], card["description"]) != (before["title"], before["description"])
    polisher = _polisher() if (changed and body.get("polish", True) and _polish_enabled(cfg)) else None
    if polisher:
        board.set_polish(cid, "pending")
        _start_polish(cfg, board, cid, polisher)
        card = board.get(cid) or card
    return 200, {"ok": True, "card": card}


def _delete(body, cfg, store, board):
    if not board.delete(_card_id(body)):
        raise boardlib.NotFound("That card no longer exists.")
    return 200, {"ok": True}


def _move(body, cfg, store, board):
    cid = _card_id(body)
    return 200, {"ok": True, "card": board.move(cid, req_str(body, "column"), body.get("position"))}


def _advance(body, cfg, store, board):
    return 200, {"ok": True, "card": board.advance(_card_id(body))}


def _revert(body, cfg, store, board):
    return 200, {"ok": True, "card": board.revert_polish(_card_id(body))}


def _start(body, cfg, store, board):
    """Launch an agent for the card: the same path as /api/run (plan + launch), then link it and move to build."""
    from . import launcher
    card = board.get(_card_id(body))
    if card is None:
        raise boardlib.NotFound("That card no longer exists.")
    tool, tier = opt_str(body, "tool") or None, opt_str(body, "tier") or None
    if card["run_id"]:
        linked = store.get(card["run_id"]) or {}
        if linked.get("status") in ("running", "queued"):
            raise ValueError("An agent is already working on this card.")
    task = card["title"] + (("\n\n" + card["description"]) if card["description"] else "")
    p = launcher.plan(task, cfg, tool, tier, card["project"])
    run_id, p = launcher.launch(task, cfg, store, tool, tier, card["project"], None, p=p)
    board.link_run(card["id"], run_id)
    if card["column"] != "build":
        board.move(card["id"], "build")
    status = (store.get(run_id) or {}).get("status")
    if status in ("done", "failed", "stopped"):   # a very quick run can finish before the link existed
        board.on_run_status(run_id, status)
    return 200, {"ok": True, "card": board.get(card["id"]), "run_id": run_id}


_POST = {
    "/api/board/create": _create,
    "/api/board/update": _update,
    "/api/board/delete": _delete,
    "/api/board/move": _move,
    "/api/board/advance": _advance,
    "/api/board/revert-polish": _revert,
    "/api/board/start": _start,
}
