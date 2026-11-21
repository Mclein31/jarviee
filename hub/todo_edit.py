"""Tick a project's brain TODO from the dashboard (R2-11).

The only place Jarviee edits an existing Brain document, and only when the owner presses the button:
the item text must match exactly one line (same parsing as the Projects rail), the file must not change
while we work, the write is atomic, and `undo` restores the exact previous line.
⬜ and `- [ ]` become ✅ / `- [x]` (a line holding just one marker also gets the date); 👤 becomes ✅ too."""
import os
import re
import tempfile
from datetime import date

from . import obsidian
from .obsidian import BOX_RE, DONE, OWNER, TODO, _is_legend, _parse_task


class TodoError(ValueError):
    pass


def _todo_path(cfg, project):
    d = obsidian.find_project(cfg, project)
    if not d:
        raise TodoError(f"unknown project '{project}'")
    p = d / ".brain" / "TODO.md"
    if not p.is_file():
        raise TodoError(f"{project} has no .brain/TODO.md")
    return p


def _candidates(lines):
    """(index, kind, marker offset) for every tickable item, parsed exactly like obsidian.todo_stats."""
    out, in_code, i0 = [], False, 0
    if lines and lines[0].strip() == "---":   # skip frontmatter
        for k in range(1, len(lines)):
            if lines[k].strip() == "---":
                i0 = k + 1
                break
    for i in range(i0, len(lines)):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or _is_legend(line):
            continue
        if TODO in line:
            it = _parse_task(line.split(TODO, 1)[1].split(DONE)[0].split(OWNER)[0])
            if it:
                out.append((i, "open", line.index(TODO), it["text"]))
        if OWNER in line:
            it = _parse_task(line.split(OWNER, 1)[1].split(DONE)[0].split(TODO)[0], is_owner=True)
            if it:
                out.append((i, "owner", line.index(OWNER), it["text"]))
        m = BOX_RE.match(line)
        if m and m.group(1) == " " and TODO not in line and OWNER not in line and DONE not in line:
            it = _parse_task(m.group(2))
            if it:
                out.append((i, "box", line.index("[ ]"), it["text"]))
    return out


def _write(path, text, expected_mtime):
    if path.stat().st_mtime_ns != expected_mtime:
        raise TodoError("TODO.md changed while it was being edited; nothing was written, try again")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".todo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read(path):
    st = path.stat().st_mtime_ns
    return path.read_bytes().decode("utf-8"), st


def tick(cfg, project, text, owner=False, today=None):
    """Mark one item done. Returns {'before': old line, 'after': new line} (pass both to undo())."""
    path = _todo_path(cfg, project)
    raw, mt = _read(path)
    lines = raw.split("\n")
    want = obsidian._clean(re.sub(r"\s+", " ", str(text or "")))
    hits = [c for c in _candidates(lines) if c[3] == want and (c[1] == "owner") == bool(owner)]
    if not hits:
        raise TodoError("that item is not in TODO.md any more (it may already be ticked or reworded)")
    if len(hits) > 1:
        raise TodoError(f"{len(hits)} lines have that exact text; tick it in TODO.md by hand")
    i, kind, off, _ = hits[0]
    before = lines[i]
    if kind == "box":
        after = before[:off] + "[x]" + before[off + 3:]
    else:
        mark = TODO if kind == "open" else OWNER
        after = before[:off] + DONE + before[off + len(mark):]
    if (kind == "box" or sum(before.count(m) for m in (DONE, TODO, OWNER)) == 1) and not re.search(r"\(\d{4}-\d{2}-\d{2}\)\s*$", before):
        after = after.rstrip() + f" ({(today or date.today()).isoformat()})"
    lines[i] = after
    _write(path, "\n".join(lines), mt)
    return {"before": before, "after": after}


def undo(cfg, project, before, after):
    """Put `before` back where `after` is, only when `after` is still on exactly one line."""
    path = _todo_path(cfg, project)
    raw, mt = _read(path)
    lines = raw.split("\n")
    at = [i for i, ln in enumerate(lines) if ln == after]
    if len(at) != 1:
        raise TodoError("that line has changed since it was ticked; undo it in TODO.md by hand")
    lines[at[0]] = before
    _write(path, "\n".join(lines), mt)
    return {"before": before, "after": after}
