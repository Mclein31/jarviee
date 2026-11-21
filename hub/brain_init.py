"""`hub.py brain init <project-dir>`: create a starter .brain/ from examples/brain-template/.

Never overwrites an existing file. Refuses a folder outside the workspace root unless force=True."""
import datetime
from pathlib import Path

from .config import ROOT, expand

TEMPLATE_DIR = ROOT / "examples" / "brain-template"
FILES = ("PROJECT_CONTEXT.md", "ARCHITECTURE.md", "DECISIONS.md", "TODO.md", "NOTES.md", "CHANGELOG.md")


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def init(project_dir, cfg, force=False, template_dir=None, today=None):
    """Returns (ok, created, skipped, message)."""
    project = Path(expand(project_dir)).resolve()
    if not project.is_dir():
        return False, [], [], f"{project} is not a folder."
    root = Path(expand(cfg["obsidian"]["workspace_root"])).resolve()
    if not force and not _inside(project, root):
        return False, [], [], (f"{project} is outside the workspace root {root}. "
                               "Pass --force, or change obsidian.workspace_root (python3 hub.py setup).")
    tdir = Path(template_dir or TEMPLATE_DIR)
    brain = project / cfg["obsidian"].get("project_brain_dir", ".brain")
    brain.mkdir(exist_ok=True)
    today = today or datetime.date.today().isoformat()
    created, skipped = [], []
    for name in FILES:
        dest = brain / name
        src = tdir / name
        if dest.exists():
            skipped.append(name)
            continue
        if not src.is_file():
            skipped.append(name + " (template missing)")
            continue
        text = src.read_text(encoding="utf-8").replace("{{name}}", project.name).replace("{{date}}", today)
        try:
            with open(dest, "x", encoding="utf-8") as f:   # "x": never clobbers a file that appeared meanwhile
                f.write(text)
            created.append(name)
        except FileExistsError:
            skipped.append(name)
    return True, created, skipped, str(brain)


def run(project_dir, cfg, force=False):
    ok, created, skipped, msg = init(project_dir, cfg, force=force)
    if not ok:
        print(f"error: {msg}")
        return False
    print(f"Brain folder: {msg}")
    for n in created:
        print(f"  created  {n}")
    for n in skipped:
        print(f"  skipped  {n} (already exists)" if "(" not in n else f"  skipped  {n}")
    print("Edit PROJECT_CONTEXT.md first: agents read it before every run.")
    return True
