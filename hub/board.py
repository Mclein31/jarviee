"""Project Board storage: per-project kanban cards in hub.db, plus the auto-advance rules.

The board has its own SQLite connection to the same file as the run store (WAL), so it never
touches hub/store.py. Every public method takes the module lock. Errors the owner can fix
(blank title, unknown column, ...) raise ValueError with a short readable message.
"""
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_lock = threading.RLock()

COLUMNS = [
    ("todo", "To do"),
    ("plan", "Plan / conceptualize"),
    ("build", "Build"),
    ("review", "Code review / finalize"),
    ("done", "Implemented / done"),
]
LABELS = ["feature", "bugfix", "error", "refactor", "chore", "docs", "research"]
PRIORITIES = ["low", "normal", "high"]
POLISH_STATES = ["pending", "done", "skipped", "failed", "off"]

COLUMN_KEYS = [k for k, _ in COLUMNS]
STEP = 1024.0                 # gap between cards when a card is appended to a column
MIN_GAP = 1e-6                # neighbours closer than this get renumbered
TITLE_MAX = 120
DESCRIPTION_MAX = 4000
PROJECT_MAX = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_cards (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  col TEXT NOT NULL,
  position REAL NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT 'feature',
  priority TEXT NOT NULL DEFAULT 'normal',
  run_id TEXT,
  polish TEXT NOT NULL DEFAULT 'off',
  polish_model TEXT NOT NULL DEFAULT '',
  original TEXT,                  -- JSON {"title","description"}: the owner's text before polish
  created REAL NOT NULL,
  updated REAL NOT NULL,
  done_at REAL
);
CREATE INDEX IF NOT EXISTS idx_board_project ON board_cards(project, col, position);
CREATE INDEX IF NOT EXISTS idx_board_run ON board_cards(run_id);
"""

_SELECT = ("SELECT id, project, col, position, title, description, label, priority, run_id, polish, "
           "polish_model, original, created, updated, done_at FROM board_cards")
# fields update() accepts; "column" and "position" go through the move rules
_UPDATABLE = {"title", "description", "label", "priority", "column", "position", "run_id"}


class NotFound(ValueError):
    """No card with that id. A ValueError too, so a caller that only catches ValueError still reports it."""


def _card(row):
    original = None
    if row["original"]:
        try:
            original = json.loads(row["original"])
        except (TypeError, ValueError):
            original = None
    return {
        "id": row["id"], "project": row["project"], "column": row["col"], "position": row["position"],
        "title": row["title"], "description": row["description"], "label": row["label"],
        "priority": row["priority"], "run_id": row["run_id"], "polish": row["polish"],
        "polish_model": row["polish_model"], "original": original,
        "created": row["created"], "updated": row["updated"], "done_at": row["done_at"],
    }


def _title(value):
    text = " ".join(str(value if value is not None else "").split())   # one line, single spaces
    if not text:
        raise ValueError("A card needs a title.")
    if len(text) > TITLE_MAX:
        raise ValueError(f"The title is too long ({len(text)} characters, the limit is {TITLE_MAX}).")
    return text


def _description(value):
    text = str(value if value is not None else "").strip()
    if len(text) > DESCRIPTION_MAX:
        raise ValueError(f"The description is too long ({len(text)} characters, the limit is {DESCRIPTION_MAX}).")
    return text


def _label(value):
    if value not in LABELS:
        raise ValueError(f"Unknown label '{value}'. Use one of: {', '.join(LABELS)}.")
    return value


def _priority(value):
    if value not in PRIORITIES:
        raise ValueError(f"Unknown priority '{value}'. Use one of: {', '.join(PRIORITIES)}.")
    return value


def _column(value):
    if value not in COLUMN_KEYS:
        raise ValueError(f"Unknown column '{value}'. Use one of: {', '.join(COLUMN_KEYS)}.")
    return value


def _project(value):
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Pick a project for the card.")
    if len(text) > PROJECT_MAX:
        raise ValueError("The project name is too long.")
    return text


def _position(value):
    if isinstance(value, bool):
        raise ValueError("The position must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("The position must be a number.") from None
    if not math.isfinite(number):
        raise ValueError("The position must be a number.")
    return number


class Board:
    def __init__(self, db_path):
        path = Path(os.path.expanduser(str(db_path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _lock:
            self.db.execute("PRAGMA journal_mode=WAL;")
            self.db.execute("PRAGMA busy_timeout=5000;")
            self.db.execute("PRAGMA synchronous=NORMAL;")
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self):
        with _lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    # ---- reads ------------------------------------------------------------------------------

    def list(self, project):
        with _lock:
            rows = self.db.execute(f"{_SELECT} WHERE project=?", (project,)).fetchall()
        order = {k: i for i, k in enumerate(COLUMN_KEYS)}
        cards = [_card(r) for r in rows]
        cards.sort(key=lambda c: (order.get(c["column"], len(order)), c["position"], c["created"]))
        return cards

    def get(self, card_id):
        with _lock:
            row = self.db.execute(f"{_SELECT} WHERE id=?", (card_id,)).fetchone()
        return _card(row) if row else None

    def cards_for_run(self, run_id):
        if not run_id:
            return []
        with _lock:
            rows = self.db.execute(f"{_SELECT} WHERE run_id=? ORDER BY created", (run_id,)).fetchall()
        return [_card(r) for r in rows]

    def counts(self):
        """{project: {"open": n, "done": n}} for every project that has cards."""
        out = {}
        with _lock:
            rows = self.db.execute("SELECT project, col, COUNT(*) AS n FROM board_cards GROUP BY project, col").fetchall()
        for r in rows:
            entry = out.setdefault(r["project"], {"open": 0, "done": 0})
            entry["done" if r["col"] == "done" else "open"] += r["n"]
        return out

    def projects(self):
        """Names of the projects that have at least one card."""
        with _lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT project FROM board_cards ORDER BY project").fetchall()]

    # ---- writes -----------------------------------------------------------------------------

    def create(self, project, title, description="", label="feature", priority="normal", polish="off"):
        project, title, description = _project(project), _title(title), _description(description)
        label, priority = _label(label), _priority(priority)
        if polish not in POLISH_STATES:
            raise ValueError(f"Unknown polish state '{polish}'.")
        now = time.time()
        card_id = "c_" + uuid.uuid4().hex[:8]
        with _lock:
            while self.db.execute("SELECT 1 FROM board_cards WHERE id=?", (card_id,)).fetchone():
                card_id = "c_" + uuid.uuid4().hex[:8]
            position = self._end_position("todo", project)
            self.db.execute(
                "INSERT INTO board_cards (id, project, col, position, title, description, label, priority, "
                "polish, polish_model, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, project, "todo", position, title, description, label, priority, polish, "", now, now))
            self.db.commit()
            return self._get(card_id)

    def update(self, card_id, **fields):
        unknown = sorted(set(fields) - _UPDATABLE)
        if unknown:
            raise ValueError(f"Can't change: {', '.join(unknown)}.")
        with _lock:
            card = self._need(card_id)
            sets = {}
            if "title" in fields:
                sets["title"] = _title(fields["title"])
            if "description" in fields:
                sets["description"] = _description(fields["description"])
            if "label" in fields:
                sets["label"] = _label(fields["label"])
            if "priority" in fields:
                sets["priority"] = _priority(fields["priority"])
            if "run_id" in fields:
                sets["run_id"] = str(fields["run_id"]) if fields["run_id"] else None
            column = _column(fields["column"]) if "column" in fields else None
            position = _position(fields["position"]) if fields.get("position") is not None else None
            text_changed = (("title" in sets and sets["title"] != card["title"])
                            or ("description" in sets and sets["description"] != card["description"]))
            if text_changed:
                # the owner wrote new text: any earlier improvement and its saved original are stale
                sets.update(original=None, polish="off", polish_model="")
            if sets:
                self._write(card_id, sets)
            if position is not None or (column is not None and column != card["column"]):
                self._move(card_id, column or card["column"], position)
            return self._get(card_id)

    def delete(self, card_id):
        with _lock:
            cur = self.db.execute("DELETE FROM board_cards WHERE id=?", (card_id,))
            self.db.commit()
            return cur.rowcount > 0

    def move(self, card_id, column, position=None):
        column = _column(column)
        if position is not None:
            position = _position(position)
        with _lock:
            self._need(card_id)
            self._move(card_id, column, position)
            return self._get(card_id)

    def advance(self, card_id):
        with _lock:
            card = self._need(card_id)
            index = COLUMN_KEYS.index(card["column"])
            if index >= len(COLUMN_KEYS) - 1:
                return card
            self._move(card_id, COLUMN_KEYS[index + 1], None)
            return self._get(card_id)

    def link_run(self, card_id, run_id):
        with _lock:
            self._need(card_id)
            self._write(card_id, {"run_id": str(run_id) if run_id else None})
            return self._get(card_id)

    def set_polish(self, card_id, status, title=None, description=None, label=None, model=""):
        if status not in POLISH_STATES:
            raise ValueError(f"Unknown polish state '{status}'.")
        with _lock:
            card = self._need(card_id)
            sets = {"polish": status, "polish_model": str(model or "")}
            new_title = _title(title) if title is not None else card["title"]
            new_desc = _description(description) if description is not None else card["description"]
            if new_title != card["title"] or new_desc != card["description"]:
                if card["original"] is None:   # keep the owner's words the first time they are replaced
                    sets["original"] = json.dumps({"title": card["title"], "description": card["description"]})
                sets["title"], sets["description"] = new_title, new_desc
            if label is not None:
                sets["label"] = _label(label)
            self._write(card_id, sets)
            return self._get(card_id)

    def revert_polish(self, card_id):
        with _lock:
            card = self._need(card_id)
            original = card["original"]
            if not original:
                raise ValueError("There is no earlier text to go back to.")
            self._write(card_id, {"title": _title(original.get("title")), "description": _description(original.get("description")),
                                  "original": None, "polish": "off", "polish_model": ""})
            return self._get(card_id)

    # ---- auto-advance -----------------------------------------------------------------------

    def on_run_status(self, run_id, status, changed_files=None):
        """A linked run changed status. Returns the cards that moved (empty when nothing did)."""
        if status == "running":
            allowed, target = ("todo", "plan"), "build"
        elif status == "done":
            allowed, target = ("todo", "plan", "build"), "review"
        else:   # failed, stopped, waiting, ...: the card stays where it is
            return []
        return self._auto_move(run_id, allowed, target)

    def on_inbox_accept(self, run_id):
        """The owner accepted the run in the review inbox: the linked card is finished."""
        return self._auto_move(run_id, tuple(COLUMN_KEYS), "done")

    def _auto_move(self, run_id, allowed, target):
        moved = []
        with _lock:
            for card in self.cards_for_run(run_id):
                if card["column"] in allowed and card["column"] != target:
                    self._move(card["id"], target, None)
                    moved.append(self._get(card["id"]))
        return moved

    # ---- internals (call with the lock held) ------------------------------------------------

    def _get(self, card_id):
        row = self.db.execute(f"{_SELECT} WHERE id=?", (card_id,)).fetchone()
        return _card(row) if row else None

    def _need(self, card_id):
        card = self._get(card_id)
        if card is None:
            raise NotFound("That card no longer exists.")
        return card

    def _write(self, card_id, sets):
        names = {"column": "col"}
        sets = dict(sets)
        sets["updated"] = time.time()
        cols = ", ".join(f"{names.get(k, k)}=?" for k in sets)
        self.db.execute(f"UPDATE board_cards SET {cols} WHERE id=?", (*sets.values(), card_id))
        self.db.commit()

    def _end_position(self, column, project, exclude=None):
        row = self.db.execute(
            "SELECT MAX(position) FROM board_cards WHERE project=? AND col=? AND id IS NOT ?",
            (project, column, exclude)).fetchone()
        return (row[0] + STEP) if row and row[0] is not None else STEP

    def _move(self, card_id, column, position):
        card = self._get(card_id)
        if position is None:
            position = self._end_position(column, card["project"], exclude=card_id)
        sets = {"col": column, "position": position}
        if column == "done" and card["column"] != "done":
            sets["done_at"] = time.time()
        elif column != "done":
            sets["done_at"] = None
        self._write(card_id, sets)
        self._renumber_if_crowded(card["project"], column)

    def _renumber_if_crowded(self, project, column):
        rows = self.db.execute("SELECT id, position FROM board_cards WHERE project=? AND col=? ORDER BY position, created",
                               (project, column)).fetchall()
        gaps = [b["position"] - a["position"] for a, b in zip(rows, rows[1:])]
        if not gaps or min(gaps) >= MIN_GAP:
            return
        for i, r in enumerate(rows, 1):
            self.db.execute("UPDATE board_cards SET position=? WHERE id=?", (i * STEP, r["id"]))
        self.db.commit()
