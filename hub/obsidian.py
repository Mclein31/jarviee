"""Read the project brain (per-project .brain/ + global _BRAIN/ vault).

Read-only except for one thing: finished runs are appended to
<Project>/.brain/AGENT_RUNS.md. Existing Brain docs are never edited, and
nothing is written to the global brain (_BRAIN/).
"""
import re
from datetime import datetime
from pathlib import Path

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
DONE, TODO, OWNER = "✅", "⬜", "\U0001f464"   # ✅ ⬜ 👤
MARKERS = (DONE, TODO, OWNER)
BOX_RE = re.compile(r"^\s*[-*+] \[( |x|X)\]\s*(.*)")
LEGEND_RE = re.compile("[" + DONE + TODO + "]|" + OWNER)
LEGEND_WORDS = re.compile(r"(✅|⬜|\U0001f464)️?\s*(done|to ?do|needs)", re.I)
WIKI_RE = re.compile(r"\[\[(.+?)\]\]")


# ---------- paths ----------

def _b(cfg):
    return cfg["obsidian"]


def workspace(cfg):
    return Path(_b(cfg)["workspace_root"])


def global_brain(cfg):
    return workspace(cfg) / _b(cfg)["global_brain"]


def status_path(cfg):
    return workspace(cfg) / _b(cfg)["status_file"]


def brain_ok(cfg):
    return workspace(cfg).is_dir()


def global_ok(cfg):
    return global_brain(cfg).is_dir()


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_frontmatter(text):
    m = FM_RE.match(text)
    if not m:
        return {}, text
    data = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data, text[m.end():]


# ---------- TODO.md progress ----------

PRIO_MARKER_RE = re.compile(
    r"(?:\[\s*(P0|P1|P2|Urgent|Critical|High(?:\s+Priority)?|Priority|!+)\s*\]"
    r"|\(\s*(P0|P1|P2|Urgent|Critical|High(?:\s+Priority)?|Priority|!+)\s*\)"
    r"|^(P0|P1|P2|Urgent|Critical|High|Priority)\s*:\s*"
    r"|^(!+)(?:\s+|$)"
    r"|(?:\s+)(!+)\s*$)",
    re.I,
)


def _norm_prio(token):
    t = token.strip().lower()
    if t == "p0":
        return "P0", 0
    if t in ("urgent", "critical") or (t.startswith("!") and len(t) >= 2):
        return "Urgent", 0
    if t == "p1":
        return "P1", 1
    if t in ("high", "high priority") or t == "!":
        return "High", 1
    if t == "priority":
        return "Priority", 1
    if t == "p2":
        return "P2", 2
    return "High", 1


def _clean(s, n=160):
    s = re.sub(r"\*\*|__|`", "", s).strip(" -*·:")
    return (s[: n - 1] + "…") if len(s) > n else s


HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)")
SECTION_HIGH_RE = re.compile(r"\b(p0|p1|urgent|critical|high\s+priority|blocker|blockers)\b", re.I)
SECTION_MED_RE = re.compile(r"\b(p2)\b", re.I)


def _parse_task(raw, is_owner=False, section_prio=("", 99)):
    s = re.sub(r"\*\*|__|`", "", raw).strip(" -*·:")
    best_prio, best_rank = ("High", 1) if is_owner else section_prio
    while True:
        m = PRIO_MARKER_RE.search(s)
        if not m:
            break
        tok = next(g for g in m.groups() if g is not None)
        p_label, p_rank = _norm_prio(tok)
        if p_rank < best_rank or (p_rank == best_rank and best_prio == "High" and p_label != "High"):
            best_prio, best_rank = p_label, p_rank
        s = (s[: m.start()] + " " + s[m.end() :]).strip(" -*·:")
    cleaned = _clean(re.sub(r"\s{2,}", " ", s))
    if not cleaned:
        return None
    return {"text": cleaned, "priority": best_prio, "rank": best_rank, "owner": bool(is_owner)}


def _is_legend(line):
    kinds = {m.group(0) for m in LEGEND_RE.finditer(line)}
    return len(kinds) >= 2 and LEGEND_WORDS.search(line) is not None


def todo_stats(text):
    """Count ✅/⬜/👤 markers plus - [x]/- [ ] checkboxes and detect priority tasks.

    A line can hold several markers ("✅ shipped X. ⬜ still to fix Y"), so each
    occurrence counts. The legend line ("✅ done · ⬜ to do · 👤 needs the
    owner") and fenced code blocks are skipped. Tasks are sorted with highest
    priority first and returned in full without truncation.
    """
    _, body = parse_frontmatter(text)
    done = todo = owner = 0
    open_items, owner_items = [], []
    in_code = False
    section_prio = ("", 99)
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or _is_legend(line):
            continue
        hm = HEADING_RE.match(line)
        if hm:
            htxt = hm.group(1)
            if SECTION_HIGH_RE.search(htxt):
                section_prio = ("High", 1)
            elif SECTION_MED_RE.search(htxt):
                section_prio = ("P2", 2)
            else:
                section_prio = ("", 99)
            continue
        d, t, o = line.count(DONE), line.count(TODO), line.count(OWNER)
        done, todo, owner = done + d, todo + t, owner + o
        if t:
            item = _parse_task(line.split(TODO, 1)[1].split(DONE)[0].split(OWNER)[0], is_owner=False, section_prio=section_prio)
            if item:
                open_items.append(item)
        if o:
            item = _parse_task(line.split(OWNER, 1)[1].split(DONE)[0].split(TODO)[0], is_owner=True, section_prio=section_prio)
            if item:
                owner_items.append(item)
        m = BOX_RE.match(line)
        if m and not (d or t or o):
            if m.group(1).lower() == "x":
                done += 1
            else:
                todo += 1
                item = _parse_task(m.group(2), is_owner=False, section_prio=section_prio)
                if item:
                    open_items.append(item)
    open_items.sort(key=lambda x: x["rank"])
    owner_items.sort(key=lambda x: x["rank"])
    total = done + todo + owner
    return {
        "done": done, "todo": todo, "owner": owner, "total": total,
        "progress": round(100 * done / total) if total else None,
        "open_tasks": [x["text"] for x in open_items],
        "owner_tasks": [x["text"] for x in owner_items],
        "open_task_items": open_items,
        "owner_task_items": owner_items,
        "priority_tasks": [x["text"] for x in owner_items] + [x["text"] for x in open_items if x["priority"]],
        "priority_count": len(owner_items) + sum(1 for x in open_items if x["priority"]),
    }


# ---------- _BRAIN/PROJECT_STATUS.md ----------

def _cells(line):
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", inner)]


def _plain(cell):
    cell = cell.replace("\\|", "|")
    cell = WIKI_RE.sub(lambda m: m.group(1).split("|")[-1], cell)
    return re.sub(r"\*\*|__|`", "", cell).strip()


def parse_status_table(text):
    """Rows of every markdown table whose first header cell is 'Project'.

    Project cells look like **[[Alpha\\|Alpha_App]]**: the alias after the
    pipe is the workspace folder; with no alias the link target is used.
    """
    rows, cols = [], None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            cols = None
            continue
        cells = _cells(s)
        if cols is None:
            if cells and _plain(cells[0]).lower() == "project":
                cols = [_plain(c).lower() for c in cells]
            continue
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
            continue
        row = dict(zip(cols, cells))
        pcell = row.get("project", "")
        m = WIKI_RE.search(pcell)
        if m:
            parts = m.group(1).replace("\\|", "|").split("|", 1)
            link, folder = parts[0].strip(), parts[-1].strip()
        else:
            link = folder = _plain(pcell)

        def pick(*keys):
            for k in keys:
                for col, val in row.items():
                    if k in col:
                        return _plain(val)
            return ""
        rows.append({
            "name": folder, "link": link, "folder": folder,
            "category": pick("category"), "status": pick("status"),
            "focus": pick("focus"), "blocker": pick("blocker", "risk"),
        })
    return rows


def status_rows(cfg):
    return parse_status_table(_read(status_path(cfg)))


def _match_row(rows, folder):
    f = folder.lower()
    for r in rows:
        if r["folder"].lower() == f:
            return r
    for r in rows:
        if r["link"].lower() == f:
            return r
    return None


# ---------- projects ----------

def _project_dirs(cfg):
    root, bdir = workspace(cfg), _b(cfg)["project_brain_dir"]
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith((".", "_")) or not p.is_dir():
            continue
        if (p / bdir).is_dir():
            out.append(p)
    return out


def list_projects(cfg):
    rows = status_rows(cfg)
    bdir = _b(cfg)["project_brain_dir"]
    projects = []
    for p in _project_dirs(cfg):
        brain = p / bdir
        files = sorted(f.name for f in brain.glob("*.md"))
        stats = todo_stats(_read(brain / "TODO.md"))
        row = _match_row(rows, p.name) or {}
        try:
            updated = max(f.stat().st_mtime for f in brain.glob("*.md"))
        except ValueError:
            updated = None
        projects.append({
            "name": p.name, "path": str(p), "brain": str(brain), "files": files,
            "progress": stats["progress"], "tasks_done": stats["done"],
            "tasks_total": stats["total"], "open_tasks": stats["open_tasks"],
            "owner_tasks": stats["owner_tasks"], "owner_count": stats["owner"],
            "open_task_items": stats["open_task_items"],
            "owner_task_items": stats["owner_task_items"],
            "priority_tasks": stats["priority_tasks"],
            "priority_count": stats["priority_count"],
            "status": row.get("status", ""), "category": row.get("category", ""),
            "focus": row.get("focus", ""), "blocker": row.get("blocker", ""),
            "in_status_table": bool(row), "updated": updated,
        })
    return projects


def find_project(cfg, name):
    """Project folder for a name, or None. Only real workspace folders with a .brain/."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    for p in _project_dirs(cfg):
        if p.name.lower() == name.lower():
            return p
    return None


def project_context(cfg, name):
    """(context_text, meta) from the project's configured .brain files."""
    p = find_project(cfg, name)
    if not p:
        return "", {}
    brain = p / _b(cfg)["project_brain_dir"]
    parts = []
    for fname in _b(cfg).get("context_files", []):
        _, body = parse_frontmatter(_read(brain / fname))
        if body.strip():
            parts.append(f"### {_b(cfg)['project_brain_dir']}/{fname}\n{body.strip()}")
    limit = int(_b(cfg).get("max_context_chars", 6000))
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…(trimmed)"
    return text, {"path": str(p)}


def log_run(cfg, run):
    """Append one line per finished run to <Project>/.brain/AGENT_RUNS.md."""
    p = find_project(cfg, run.get("project"))
    if not p:
        return None
    f = p / _b(cfg)["project_brain_dir"] / _b(cfg).get("runs_file", "AGENT_RUNS.md")
    now = datetime.now()
    icon = {"done": "✅", "failed": "❌"}.get(run.get("status"), "⏹")
    task = " ".join(str(run.get("task", "")).split())[:160]
    line = (f"- {icon} {now:%Y-%m-%d %H:%M} · {run.get('tool')} / {run.get('model')} "
            f"({run.get('tier')}) · {run.get('status')} · {task}\n")
    if not f.exists():
        f.write_text("# Agent runs\n\nAppended by Jarviee when a launched agent finishes. "
                     "One line per run.\n\n", encoding="utf-8")
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(line)
    return str(f)
