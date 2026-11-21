#!/usr/bin/env python3
"""A fake agent CLI for screenshots: it LOOKS like an AI coding agent at work in a terminal, and does nothing.

    fake_agent.py --model opus --effort high [--duration 180] [--speed 1] -- "<prompt>"

No network, no model call, no file is read outside the working folder (only names are listed) and none is written.
It prints a header (task and model), then steps ("Reading src/cart.ts", "Editing src/checkout.ts (+24 −6)",
"Running tests ... 42 passed") with a progress bar, ANSI colours, and a short final summary. The run takes about
`--duration` seconds (default 180). Everything is deterministic: the same prompt gives the same file names, numbers
and pacing. Python 3.9+ standard library only.

The step lines start with a verb (Reading, Editing, Running ...) so Jarviee's log parser counts them as steps
(hub/progress.py TOOL_LINE_RE), exactly as it does for a real agent's output.
"""
import argparse
import os
import random
import re
import shutil
import sys
import time
import zlib

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = ("\033[3%dm" % i for i in (1, 2, 3, 4, 5, 6))

# the model slug Jarviee passes ({model}) -> the name shown in the header
MODEL_NAMES = {
    "opus": "Opus 5.5", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash", "gemini-4-hydrogen-medium": "Gemini 4 Hydrogen",
    "gemini-pro-orchestrator-neon": "Gemini Pro Orchestrator", "gemini-4-neon-high": "Gemini 4 Neon",
}
DEFAULT_FILES = ["src/index.ts", "src/utils.ts", "src/config.ts", "tests/main.test.ts", "README.md"]
SKIP_DIRS = {".git", ".brain", "node_modules", "__pycache__", ".locks", ".venv", "dist", "build"}


def model_name(slug):
    slug = str(slug or "").strip()
    return MODEL_NAMES.get(slug) or MODEL_NAMES.get(slug.lower()) or slug.replace("-", " ").title() or "Demo model"


def task_of(prompt):
    """The task itself: Jarviee wraps it as '... Task: <task>' when a project brain is injected."""
    text = str(prompt or "").strip()
    if "\nTask: " in text:
        text = text.rsplit("\nTask: ", 1)[1]
    return re.sub(r"\s+", " ", text).strip() or "demo task"


def project_files(cwd, rng):
    """File names (never contents) under the working folder, so the steps name real-looking files of the project."""
    found = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        if os.path.relpath(root, cwd).count(os.sep) >= 3:
            dirs[:] = []   # three levels down is plenty
        for f in sorted(files):
            if not f.startswith(".") and f.rsplit(".", 1)[-1] in ("ts", "tsx", "js", "py", "dart", "css", "html", "md", "json"):
                found.append(os.path.relpath(os.path.join(root, f), cwd))
        if len(found) > 60:
            break
    found = found or list(DEFAULT_FILES)
    rng.shuffle(found)
    return found


def is_test(name):
    return "test" in name.lower()


def build_script(task, files, rng):
    """[(weight, kind, text-or-None, data)] — what the agent 'does'. Weights set how long each step takes."""
    src = [f for f in files if not is_test(f)] or files
    tests = [f for f in files if is_test(f)] or ["tests/main.test.ts"]
    reads = src[:rng.randint(4, 6)]   # each file once; a small project just reads fewer
    edits = [(f, rng.randint(6, 48), rng.randint(0, 14)) for f in src[:rng.randint(2, 4)]]
    steps = [(2, "think", "Reading the task and the project notes", None)]
    for f in reads[:3]:
        steps.append((2, "read", f, rng.randint(40, 320)))
    steps.append((3, "plan", None, [f for f, _, _ in edits]))
    for f in reads[3:]:
        steps.append((2, "read", f, rng.randint(40, 320)))
    steps.append((2, "search", f"'{rng.choice(['TODO', 'handleError', 'formatPrice', 'useState', 'retry', 'cache'])}'", rng.randint(3, 19)))
    for i, (f, add, rem) in enumerate(edits):
        steps.append((5, "edit", f, (add, rem)))
        if i == 0:
            steps.append((2, "read", rng.choice(tests), rng.randint(40, 200)))
    steps.append((3, "edit", tests[0], (rng.randint(12, 40), rng.randint(0, 5))))
    total = rng.randint(31, 58)
    failed = rng.randint(1, 3)
    steps.append((7, "test", None, (total - failed, failed)))
    steps.append((3, "think", f"{failed} test{'s' if failed > 1 else ''} failing, checking the expected total", None))
    steps.append((4, "edit", edits[0][0], (rng.randint(2, 9), rng.randint(1, 6))))
    steps.append((7, "test", None, (total, 0)))
    steps.append((3, "lint", None, rng.randint(0, 2)))
    return steps, edits, tests, total


class Out:
    def __init__(self):
        self.width = max(40, shutil.get_terminal_size((80, 24)).columns - 1)

    def line(self, text="", plain_len=None):
        sys.stdout.write(text + RESET + "\n")
        sys.stdout.flush()

    def clip(self, text, room):
        return text if len(text) <= room else text[:max(1, room - 1)] + "…"

    def bar(self, frac, width=24):
        width = max(8, min(width, self.width - 12))
        done = int(round(frac * width))
        return f"{GREEN}{'█' * done}{DIM}{'░' * (width - done)}{RESET} {int(frac * 100):>3}%"


def main(argv):
    ap = argparse.ArgumentParser(description="fake agent CLI (screenshots only; no model is called)")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--effort", default="")
    ap.add_argument("--duration", type=float, default=180.0, help="seconds the 'work' takes (default 180)")
    ap.add_argument("--speed", type=float, default=1.0, help="2 = twice as fast (for tests)")
    ap.add_argument("prompt", nargs="*")
    a = ap.parse_args(argv)
    prompt = " ".join(a.prompt)
    task = task_of(prompt)
    rng = random.Random(zlib.crc32(task.encode("utf-8")))
    cwd = os.getcwd()
    steps, edits, tests, total = build_script(task, project_files(cwd, rng), rng)
    out = Out()
    scale = a.duration / max(1.0, a.speed) / sum(w for w, *_ in steps)
    w = out.width

    def wait(weight, label=None):
        """Between steps the agent 'thinks': an in-place spinner, as real agents draw. (Also keeps Jarviee's own pane status
        line away: it only shows while the agent is silent.)"""
        end = time.time() + max(0.0, weight * scale * rng.uniform(0.7, 1.3))
        label = label or rng.choice(["Thinking", "Working", "Reviewing", "Planning"])
        start, i = time.time(), 0
        while time.time() < end:
            sys.stdout.write(f"\r\033[2K{CYAN}{SPIN[i % len(SPIN)]}{RESET} {DIM}{label}… {int(time.time() - start)}s")
            sys.stdout.flush()
            i += 1
            time.sleep(min(0.2, max(0.0, end - time.time())))
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()

    effort = f" · {a.effort}" if a.effort else ""
    out.line(f"{BOLD}{MAGENTA}◆ {model_name(a.model)}{RESET}{DIM}{effort} · auto mode · demo agent")
    out.line(f"{DIM}{'─' * min(w, 56)}")
    for i, chunk in enumerate(re.findall(r".{1,%d}(?:\s+|$)" % max(20, min(w - 8, 48)), task)[:3]):
        out.line(f"{BOLD}Task{RESET} {chunk.strip()}" if i == 0 else f"     {chunk.strip()}")
    out.line()
    n, changed_add, changed_rem, touched = 0, 0, 0, []
    for k, (weight, kind, text, data) in enumerate(steps):
        frac = k / len(steps)
        if kind == "think":
            out.line(f"{DIM}{CYAN}✻{RESET}{DIM} {out.clip(text, w - 3)}")
        elif kind == "read":
            n += 1
            out.line(f"{CYAN}Reading{RESET} {out.clip(text, w - 24)} {DIM}· {data} lines")
        elif kind == "search":
            n += 1
            out.line(f"{CYAN}Searching{RESET} {out.clip(text, w - 28)} {DIM}· {data} matches")
        elif kind == "plan":
            out.line(f"{BOLD}Plan{RESET} {len(data)} files to change")
            for f in data:
                out.line(f"  {DIM}·{RESET} {out.clip(f, w - 6)}")
        elif kind == "edit":
            n += 1
            add, rem = data
            changed_add, changed_rem = changed_add + add, changed_rem + rem
            if text not in touched:
                touched.append(text)
            out.line(f"{YELLOW}Editing{RESET} {out.clip(text, w - 24)} {GREEN}(+{add}{RESET} {RED}−{rem}){RESET}")
        elif kind == "test":
            n += 1
            passed, failed = data
            sys.stdout.write(f"{BLUE}Running{RESET} tests {DIM}...{RESET} ")
            sys.stdout.flush()
            tick = max(3, min(10, w // 6))
            for _ in range(tick):
                sys.stdout.write(f"{DIM}.{RESET}")
                sys.stdout.flush()
                time.sleep(max(0.0, weight * scale * rng.uniform(0.7, 1.2) / tick))
            sys.stdout.write("\r\033[2K")
            if failed:
                out.line(f"{BLUE}Running{RESET} tests ... {GREEN}{passed} passed{RESET}, {RED}{failed} failed{RESET}")
            else:
                out.line(f"{BLUE}Running{RESET} tests ... {GREEN}{BOLD}{passed} passed{RESET}")
            weight = 0
        elif kind == "lint":
            n += 1
            out.line(f"{BLUE}Running{RESET} lint ... " + (f"{GREEN}clean{RESET}" if not data else f"{YELLOW}{data} fixed{RESET}"))
        if kind != "test":
            wait(weight)
        if kind in ("edit", "test", "lint", "plan"):
            out.line(f"{DIM}progress{RESET} {out.bar((k + 1) / len(steps))}")
    out.line()
    out.line(f"{GREEN}{BOLD}✓ Done{RESET} · {len(touched)} files changed {GREEN}+{changed_add}{RESET} {RED}−{changed_rem}{RESET} · "
             f"{GREEN}{total} tests passed{RESET}")
    for f in touched[:4]:
        out.line(f"  {DIM}·{RESET} {out.clip(f, w - 6)}")
    out.line(f"{DIM}Nothing was run for real: this is the Jarviee demo agent.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
