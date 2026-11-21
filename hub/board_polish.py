"""Cheap-model card rewriter for the Project Board (Haiku first, agy flash fallback)."""
import json
import os
import re
import subprocess
import tempfile
import threading

from . import brand
from .board import LABELS

DEFAULT_TIMEOUT = 45
DEFAULT_AGY_TIMEOUT = 60
MAX_TITLE_LEN = 80
MAX_DESC_LEN = 4000

PROMPT = """Rewrite this project board card so it is clear and actionable.
Keep the owner's exact meaning and never invent extra scope or requirements.

Return STRICT JSON only (no markdown fences, no commentary) with keys:
- "title": clear imperative title, <= 80 characters
- "description": a one-line goal, then a blank line, then 2 to 5 "- " acceptance bullets
- "label": one of {labels}

Card input (JSON):
{card_json}
"""


def _env():
    """Child env with hook-skip flags so polish calls never register as dashboard sessions."""
    return {**os.environ, "JARVIEE_HOOK_SKIP": "1", "AGENT_HUB_HOOK_SKIP": "1"}


def _polish_cfg(cfg):
    pol = ((cfg or {}).get("board") or {}).get("polish") or {}
    timeout = pol.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = max(1, int(timeout))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    agy_timeout = pol.get("agy_timeout", DEFAULT_AGY_TIMEOUT)
    try:
        agy_timeout = max(1, int(agy_timeout))
    except (TypeError, ValueError):
        agy_timeout = DEFAULT_AGY_TIMEOUT
    agy_default = (((cfg or {}).get("tools") or {}).get("agy") or {}).get("models", {}).get("basic") or "gemini-3.8-flash-low"
    return {
        "enabled": bool(pol.get("enabled", True)),
        "timeout": timeout,
        "agy_timeout": agy_timeout,
        "claude_model": str(pol.get("claude_model") or pol.get("model") or "haiku"),
        "agy_model": str(pol.get("agy_model") or agy_default),
    }


def run_claude(prompt, timeout=DEFAULT_TIMEOUT, model="haiku"):
    """Run Claude CLI in print mode with no tools; returns stdout or ''."""
    cwd = brand.home()
    cwd.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["claude", "-p", "--model", model, "--tools", "", "--setting-sources", "local"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=_env(),
    )
    return r.stdout if r.returncode == 0 else ""


def run_agy(prompt, timeout=DEFAULT_AGY_TIMEOUT, cfg=None, model=None):
    """Run agy CLI in sandboxed read-only plan mode; returns stdout or ''."""
    chosen = model or _polish_cfg(cfg)["agy_model"]
    with tempfile.TemporaryDirectory(prefix="jarviee-board-") as cwd:
        r = subprocess.run(
            [
                "agy", "-p", prompt,
                "--model", chosen,
                "--mode", "plan",
                "--sandbox",
                "--print-timeout", f"{int(timeout)}s",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 15,
            cwd=cwd,
            env=_env(),
        )
    return r.stdout if r.returncode == 0 else ""


def _extract_json(text):
    """Best-effort extraction of a JSON object from model output."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    for candidate in (s,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_answer(out, orig_description="", default_label="feature"):
    """Parse and validate model JSON; returns {"title","description","label"} or None."""
    data = _extract_json(out)
    if data is None:
        return None

    raw_title = data.get("title")
    raw_desc = data.get("description")
    if not isinstance(raw_title, str) or not isinstance(raw_desc, str):
        return None

    title = " ".join(raw_title.split())
    if not title:
        return None
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN].rstrip()

    desc = raw_desc.strip()
    if not desc:
        return None

    orig_len = len((orig_description or "").strip())
    if orig_len > 0:
        if len(desc) < 0.2 * orig_len or len(desc) > 4 * orig_len:
            return None
    if len(desc) > MAX_DESC_LEN:
        desc = desc[:MAX_DESC_LEN].rstrip()

    fallback_label = default_label if default_label in LABELS else "feature"
    raw_label = data.get("label")
    label = raw_label.strip().lower() if isinstance(raw_label, str) else ""
    if label not in LABELS:
        label = fallback_label

    return {"title": title, "description": desc, "label": label}


def _build_prompt(title, description, label, project):
    payload = json.dumps(
        {
            "project": str(project or ""),
            "label": str(label or "feature"),
            "title": str(title or ""),
            "description": str(description or ""),
        },
        ensure_ascii=False,
    )
    return PROMPT.format(labels=", ".join(LABELS), card_json=payload)


def improve(title, description, label, project, cfg=None, runners=None):
    """Ask a cheap model to tighten a card; returns {"title","description","label","model"} or None."""
    pcfg = _polish_cfg(cfg)
    if not pcfg["enabled"]:
        return None

    prompt = _build_prompt(title, description, label, project)
    chain = runners if runners is not None else (
        ("haiku", lambda p: run_claude(p, timeout=pcfg["timeout"], model=pcfg["claude_model"])),
        ("agy-flash", lambda p: run_agy(p, timeout=pcfg["agy_timeout"], cfg=cfg, model=pcfg["agy_model"])),
    )

    for name, run in chain:
        try:
            raw = run(prompt)
        except (OSError, subprocess.SubprocessError):
            continue
        parsed = parse_answer(raw, orig_description=description, default_label=label)
        if parsed is not None:
            return {**parsed, "model": str(name)}
    return None


def polish_async(board, card_id, cfg=None, runners=None):
    """Run improve() in a daemon thread and update board.set_polish(id, 'done'|'failed', ...)."""
    card = board.get(card_id)
    if not card:
        return None

    pcfg = _polish_cfg(cfg)
    if not pcfg["enabled"]:
        try:
            board.set_polish(card_id, "off")
        except Exception:
            pass
        return None
    if card["polish"] != "pending":
        card = board.set_polish(card_id, "pending")

    def _work():
        try:
            res = improve(
                card["title"],
                card["description"],
                card["label"],
                card["project"],
                cfg=cfg,
                runners=runners,
            )
        except Exception:   # never leave the card stuck on "pending"
            res = None
        try:
            now = board.get(card_id)
            if not now or now["polish"] != "pending" or (now["title"], now["description"]) != (
                    card["title"], card["description"]):
                return   # deleted, reverted or re-edited while the model ran: the newer state wins
            if res:
                board.set_polish(
                    card_id,
                    "done",
                    title=res["title"],
                    description=res["description"],
                    label=res["label"],
                    model=res["model"],
                )
            else:
                board.set_polish(card_id, "failed")
        except Exception:
            pass

    t = threading.Thread(target=_work, name=f"board-polish-{card_id}", daemon=True)
    t.start()
    return t
