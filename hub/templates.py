"""Saved prompt templates (R2-6), kept outside the repo in ~/.jarviee/templates.json.
Bodies may hold {project} / {file}; the command bar fills {project} when inserting."""
import json
import os
import re
import tempfile

from . import brand

MAX_NAME, MAX_BODY, MAX_COUNT = 80, 8000, 200


def path(cfg=None):
    return brand.data_path((cfg or {}).get("templates_path") or "~/.jarviee/templates.json")


def list_all(cfg=None):
    try:
        data = json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("templates") if isinstance(data, dict) else None
    return [t for t in (items or []) if isinstance(t, dict) and t.get("name") and isinstance(t.get("body"), str)]


def _write(cfg, items):
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"templates": items}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def save(cfg, name, body):
    """Create or replace a template by name (case-insensitive). Raises ValueError on bad input."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    body = str(body or "").strip()
    if not name or not body:
        raise ValueError("a template needs a name and a body")
    if len(name) > MAX_NAME or len(body) > MAX_BODY:
        raise ValueError("template name or body too long")
    items = [t for t in list_all(cfg) if t["name"].lower() != name.lower()]
    if len(items) >= MAX_COUNT:
        raise ValueError("too many templates")
    items.append({"name": name, "body": body})
    items.sort(key=lambda t: t["name"].lower())
    _write(cfg, items)
    return items


def delete(cfg, name):
    items = list_all(cfg)
    kept = [t for t in items if t["name"].lower() != str(name or "").strip().lower()]
    if len(kept) == len(items):
        raise KeyError(name)
    _write(cfg, kept)
    return kept


def fill(body, project="", file=""):
    """Substitute {project} / {file}; a variable with no value is left as written."""
    out = body
    if project:
        out = out.replace("{project}", project)
    if file:
        out = out.replace("{file}", file)
    return out
