#!/usr/bin/env python3
"""Export a clean, history-free public copy of this repository.

Copies tracked files (from ``git ls-files``) minus private/local excludes into a
fresh target directory, scans all exported text files against an untracked local
denylist and built-in secret patterns, optionally runs the unittest suite, and
creates a single-commit git repository on ``main`` without any remote.
"""
import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

EXCLUDES = (
    ".brain/",
    ".design-review/",
    ".impeccable/",
    ".taste-skill/",
    ".omniroute-work/",
    ".playwright-mcp/",
    ".claude/",
    "HANDOFF.md",
    "showcase/",
    "scripts/private_denylist.txt",
    "*.db",
    "*.log",
    "__pycache__/",
)

DENYLIST_REL = Path("scripts/private_denylist.txt")

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".heic",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz",
    ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".wav", ".m4a", ".ogg",
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
})

SECRET_PATTERNS = (
    ("sk-ant-key", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{8,}")),
    ("sk-api-key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?!ant-)(?:proj-|live-|test-|svcacct-)?[A-Za-z0-9_-]{12,}")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9_])(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{16,}")),
    ("google-api-key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{20,}")),
    ("slack-token", re.compile(r"(?<![A-Za-z0-9_-])xox[abp]-[0-9A-Za-z-]{10,}")),
    ("aws-access-key", re.compile(r"(?<![A-Za-z0-9_])AKIA[0-9A-Z]{16}(?![A-Za-z0-9_])")),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "secret-assignment",
        re.compile(r"(?i)\b(?:[a-z0-9_]*(?:token|secret|key|password|passwd))\b\s*[:=]\s*['\"]?([A-Za-z0-9+/=_-]{24,})['\"]?"),
    ),
)

# Built-in allowlist for clearly fake fixtures in tests/ only (never bypasses denylist terms).
BUILTIN_TEST_ALLOWLIST = tuple(
    re.compile(pattern)
    for pattern in (
        "sk" + "-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk" + "-ABCDEF1234567890ABCDEF1234567890",
        "sk" + "-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        "sk" + "-1234567890abcdef",
        "sk" + r"-(?:test|fake|dummy|example)-[A-Za-z0-9_-]+",
        r"(?:ghp_|gho_|github_pat_)(?:test|fake|dummy|example)[A-Za-z0-9_]*",
        "AKIA" + "IOSFODNN7EXAMPLE",
    )
)


def is_excluded(rel_path, excludes=EXCLUDES):
    """Return True if a tracked relative POSIX path matches an exclude rule."""
    rel = rel_path[2:] if rel_path.startswith("./") else rel_path.lstrip("/")
    posix = PurePosixPath(rel)
    parts = posix.parts
    for pat in excludes:
        if pat.endswith("/"):
            dirname = pat.rstrip("/")
            if rel == dirname or rel.startswith(pat) or dirname in parts[:-1]:
                return True
        elif any(ch in pat for ch in "*?["):
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(posix.name, pat):
                return True
        elif rel == pat:
            return True
    return False


def _is_inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _looks_like_secret_value(val):
    """Distinguish long hex/base64/random secret values from benign snake_case identifiers."""
    if len(val) < 24:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{24,}", val):
        return True
    has_lower = any("a" <= c <= "z" for c in val)
    has_upper = any("A" <= c <= "Z" for c in val)
    has_digit = any("0" <= c <= "9" for c in val)
    has_b64 = any(c in "+/=" for c in val)
    classes = sum((has_lower, has_upper, has_digit, has_b64))
    if classes >= 3:
        return True
    if has_b64 and (has_lower or has_upper):
        return True
    if has_digit and (has_lower or has_upper) and "_" not in val and "-" not in val:
        return True
    return False


def load_denylist(denylist_path):
    """Load private denylist terms; return (terms, found)."""
    if not denylist_path.is_file():
        return [], False
    terms = []
    for raw in denylist_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms, True


def _is_binary_file(path):
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    return b"\x00" in chunk


def _line_allowed(rel, line, allow_patterns):
    return any(rx.search(rel) or rx.search(line) for rx in allow_patterns)


def scan_file(path, rel, denylist_terms, allow_patterns=()):
    """Scan a single exported text file and return [(rel, lineno, term_or_label), ...]."""
    if path.is_symlink() or not path.is_file() or _is_binary_file(path):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    hits = []
    seen = set()
    lowered_terms = [(term, term.lower()) for term in denylist_terms]
    in_tests = rel.startswith("tests/")

    for lineno, line in enumerate(text.splitlines(), 1):
        line_lower = line.lower()
        suppressed = None

        for term, term_lower in lowered_terms:
            if term_lower in line_lower:
                if suppressed is None:
                    suppressed = _line_allowed(rel, line, allow_patterns)
                if not suppressed and (lineno, term) not in seen:
                    seen.add((lineno, term))
                    hits.append((rel, lineno, term))

        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                if label == "secret-assignment" and not _looks_like_secret_value(match.group(1)):
                    continue
                if in_tests and any(rx.search(match.group(0)) for rx in BUILTIN_TEST_ALLOWLIST):
                    continue
                if suppressed is None:
                    suppressed = _line_allowed(rel, line, allow_patterns)
                if not suppressed and (lineno, label) not in seen:
                    seen.add((lineno, label))
                    hits.append((rel, lineno, label))
    return hits


def scan_export(target_dir, denylist_terms, allow_patterns=()):
    """Scan all exported files under target_dir in deterministic path order."""
    hits = []
    files = sorted(
        (p for p in target_dir.rglob("*") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.relative_to(target_dir).as_posix(),
    )
    for path in files:
        rel = path.relative_to(target_dir).as_posix()
        hits.extend(scan_file(path, rel, denylist_terms, allow_patterns=allow_patterns))
    return hits


def git_ls_files(source_root):
    res = subprocess.run(
        ["git", "-C", str(source_root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    raw = res.stdout.decode("utf-8", errors="surrogateescape")
    return [entry for entry in raw.split("\x00") if entry]


def copy_export(source_root, target_dir):
    """Copy included working-tree files from tracked git ls-files; return (copied, excluded)."""
    tracked = git_ls_files(source_root)
    included = []
    excluded = []
    for rel in tracked:
        if is_excluded(rel):
            excluded.append(rel)
        else:
            included.append(rel)

    print(f"Excluded {len(excluded)} tracked file(s):")
    for rel in excluded:
        print(f"  - {rel}")

    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for rel in included:
        src = source_root / rel
        dst = target_dir / rel
        if not src.exists() and not src.is_symlink():
            print(f"WARNING: tracked file missing in working tree, skipping: {rel}", file=sys.stderr)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            shutil.copy2(src, dst, follow_symlinks=False)
        else:
            shutil.copy2(src, dst)
        copied.append(rel)

    print(f"Exported {len(copied)} file(s) to {target_dir}")
    return copied, excluded


def run_tests(target_dir):
    print("Running unittest suite in exported copy...")
    res = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=str(target_dir),
    )
    if res.returncode != 0:
        print("Error: unit tests failed in exported copy.", file=sys.stderr)
        return False
    return True


def init_public_git(target_dir, author_name, author_email):
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email

    subprocess.run(["git", "-C", str(target_dir), "init", "-b", "main"], check=True, env=env)
    subprocess.run(["git", "-C", str(target_dir), "add", "-A"], check=True, env=env)
    subprocess.run(
        [
            "git",
            "-C",
            str(target_dir),
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Initial public release",
        ],
        check=True,
        env=env,
    )
    print(f"Initialized git repository on 'main' with commit 'Initial public release' ({author_name} <{author_email}>).")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a clean, history-free public copy of this repository.",
    )
    parser.add_argument("target", metavar="target-dir", help="Destination directory (must not exist or must be empty).")
    parser.add_argument("--author-name", help="Git author/committer name for the initial commit.")
    parser.add_argument("--author-email", help="Git author/committer email for the initial commit.")
    parser.add_argument("--no-git", action="store_true", help="Skip git init and initial commit in target-dir.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip running the unittest suite in target-dir.")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="REGEX",
        help="Regex matching relative path or line text to allow during privacy scan (repeatable).",
    )
    parser.add_argument(
        "--source",
        default=str(Path(__file__).resolve().parent.parent),
        help="Source git repository root (defaults to this repository).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    allow_patterns = []
    for raw in args.allow:
        try:
            allow_patterns.append(re.compile(raw))
        except re.error as exc:
            print(f"Error: invalid --allow regex {raw!r}: {exc}", file=sys.stderr)
            return 2

    if not args.no_git and (not args.author_name or not args.author_email):
        print(
            "Error: --author-name and --author-email are required when creating a git commit "
            "(or pass --no-git). Tip: use your GitHub noreply email, e.g. "
            "<id>+<username>@users.noreply.github.com.",
            file=sys.stderr,
        )
        return 2

    source_root = Path(args.source).expanduser().resolve()
    target_dir = Path(args.target).expanduser().resolve()

    probe = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print(f"Error: source is not a git repository: {source_root}", file=sys.stderr)
        return 2

    if _is_inside(target_dir, source_root):
        print(f"Error: target-dir must be outside the source repository ({source_root}).", file=sys.stderr)
        return 2

    if target_dir.exists():
        if not target_dir.is_dir():
            print(f"Error: target-dir exists and is not a directory: {target_dir}", file=sys.stderr)
            return 2
        if any(target_dir.iterdir()):
            print(f"Error: target-dir exists and is not empty: {target_dir}", file=sys.stderr)
            return 2

    copy_export(source_root, target_dir)

    denylist_path = source_root / DENYLIST_REL
    denylist_terms, found = load_denylist(denylist_path)
    if not found:
        print(
            f"WARNING: private denylist not found at {denylist_path}; "
            "skipping local denylist terms and running built-in secret scan only.",
            file=sys.stderr,
        )

    hits = scan_export(target_dir, denylist_terms, allow_patterns=allow_patterns)
    if hits:
        for rel, lineno, term in hits:
            print(f"{rel}:{lineno}:{term}")
        print(
            f"Privacy scan failed with {len(hits)} hit(s). Export left at {target_dir} for inspection.",
            file=sys.stderr,
        )
        return 1

    print("Privacy scan passed (0 hits).")

    if not args.skip_tests and not run_tests(target_dir):
        print(f"Export left at {target_dir} for inspection.", file=sys.stderr)
        return 1

    if not args.no_git:
        try:
            init_public_git(target_dir, args.author_name, args.author_email)
        except subprocess.CalledProcessError as exc:
            print(f"Error: git initialization failed: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
