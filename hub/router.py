"""Decide how much model a task needs: basic, light, complex or heavy.

Pure heuristics: fast, free and explainable. Every decision comes with reasons.
- basic:   rename, typo, formatting, copy/text tweaks, single-line config, version bumps
- light:   small single-file feature or fix, simple component, docs, straightforward tests
- complex: multi-file feature, non-trivial bug, API/DB changes, refactor of one module
- heavy:   architecture, cross-cutting refactor/migration, security/auth, concurrency, hard debugging
"""
import re
import threading

LEVELS = ["basic", "light", "complex", "heavy"]
TIERS = LEVELS                      # older name, kept for callers
ALIASES = {"standard": "light"}     # pre-2026-11 tier name


def normalize(level):
    """Map a level or a legacy tier name to a level; '' stays ''."""
    if not level:
        return ""
    level = ALIASES.get(str(level).lower(), str(level).lower())
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS} (or 'standard')")
    return level


def rank(level):
    return LEVELS.index(normalize(level))


def _hits(text, keywords):
    """Keywords found as whole words; hyphens count as spaces and plurals match."""
    text = text.replace("-", " ")
    return [kw for kw in keywords
            if re.search(r"(?<![a-z])" + re.escape(kw.lower().replace("-", " ")) + r"(?:e?s)?(?![a-z])", text)]


MAX_CLASSIFY_CHARS = 8000
FILE_PATH_RE = re.compile(r"(?<![\w./-])[\w./-]+\.[a-z]{1,5}\b")
NEGATION = re.compile(
    r"\b(?:do not|don't|dont|never|without|no need to|avoid)\b[^.;\n]*?"
    r"(?=$|[.;\n]|,\s*(?:just|only|instead|please|but|however|fix|add|update|rename|change|create|make|write|remove|delete)\b)",
    re.I,
)
# Higher-level keywords that signal real engineering/structural scope rather than merely naming a topic in a typo/rename/comment.
STRUCTURAL_KEYWORDS = {
    "multi-file", "multiple files", "bug", "debug", "refactor", "integrate", "integration",
    "implement", "implement feature", "build a", "build an", "optimize", "rewrite", "end to end",
    "test suite", "architecture", "architect", "cross-cutting", "across the codebase",
    "whole project", "every module", "migrate", "concurrency", "race condition", "deadlock",
    "memory leak", "hard to reproduce", "intermittent", "flaky", "from scratch",
    "design system", "data model redesign",
}


def assess(task, cfg, brief=True, llm=None):
    """Return {level, reasons, borderline, signals} for a task description.

    Clauses after "do not / don't / never / without" are ignored ("don't refactor" is not a refactor).
    brief=False skips the word-count rules, for long generated prompts such as the planner's.
    llm: optional injected callable llm(task, level, timeout) -> level name. Used only when the
    result is borderline and classifier.llm_fallback.enabled is true (default off). Errors,
    timeouts and unknown answers keep the heuristic result.
    """
    c = cfg["classifier"]
    head = (task or "")[:MAX_CLASSIFY_CHARS]
    text = NEGATION.sub(" ", head).lower()
    words = len(head.split()) if brief else 0
    paths = set(FILE_PATH_RE.findall(text))
    files = len({p for p in paths if not p.endswith((".md", ".txt", ".rst"))})   # docs don't make it multi-file
    kw_text = FILE_PATH_RE.sub(" ", text)   # don't match auth/db/api inside filenames like auth.py
    hits = {lvl: _hits(kw_text, c.get(f"{lvl}_keywords", [])) for lvl in LEVELS}
    topic_only_basic = bool(hits["basic"]) and not any(
        k in STRUCTURAL_KEYWORDS for k in hits["complex"] + hits["heavy"]
    )
    active = {lvl: ([] if topic_only_basic and lvl in ("complex", "heavy") else hits[lvl]) for lvl in LEVELS}
    reasons = []
    names = {"basic": "simple-change", "light": "small-change", "complex": "multi-part", "heavy": "high-risk"}
    for lvl in LEVELS:
        if active[lvl]:
            reasons.append(f"{names[lvl]} words: " + ", ".join(active[lvl][:4]))

    found = [i for i, lvl in enumerate(LEVELS) if active[lvl]]
    base = max(found) if found else 1
    if not found:
        reasons.append("no strong signals; defaulting to light")
    bumps = 0
    if brief and words >= c.get("heavy_word_count", 60):
        bumps += 1
        reasons.append(f"long brief ({words} words)")
    if files >= 4:
        bumps += 1
        reasons.append(f"touches {files} files")
    elif files >= 2 and base < 2:
        base = 1 if base == 0 else 2
        reasons.append(f"touches {files} files")
    if brief and base == 0 and bumps == 0 and words > c.get("basic_max_word_count", 20):
        base = 1
        reasons.append(f"simple words but a {words}-word brief")
    idx = min(3, base + bumps)
    level = LEVELS[idx]

    # Borderline light/complex: complex rests on a single signal and nothing high-risk.
    complex_signals = len(active["complex"]) + bumps + (1 if files >= 2 else 0)
    borderline = level == "complex" and not active["heavy"] and complex_signals <= 1
    if borderline:
        reasons.append("borderline between light and complex")
        fb = c.get("llm_fallback") or {}
        if llm is not None and fb.get("enabled", False):
            ans = _ask_llm(llm, task, level, float(fb.get("timeout_s", 3)))
            if ans:
                if ans != level:
                    reasons.append(f"llm fallback: {level} -> {ans}")
                level = ans
    return {"level": level, "reasons": reasons, "borderline": borderline,
            "signals": {lvl: active[lvl] for lvl in LEVELS if active[lvl]}}


def _ask_llm(llm, task, level, timeout):
    """Call llm(task, level, timeout) with a hard time limit; return a valid level or ''."""
    box = {}

    def run():
        try:
            box["v"] = llm(task, level, timeout)
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    try:
        return normalize(box.get("v")) if not t.is_alive() else ""
    except (ValueError, TypeError):
        return ""


def classify(task, cfg, llm=None):
    """Return (level, reasons)."""
    a = assess(task, cfg, llm=llm)
    return a["level"], a["reasons"]


def pick_model(tool, level, cfg):
    models = cfg["tools"][tool]["models"]
    level = normalize(level)
    if level in models:
        return models[level]
    for legacy, new in ALIASES.items():   # configs written before the 4-level change
        if new == level and legacy in models:
            return models[legacy]
    raise ValueError(f"tool '{tool}' has no model for level '{level}'")


# ---- effort routing: how hard to think, chosen per task, independent of which model ----
EFFORT_SCALE = ["low", "medium", "high", "xhigh", "max"]   # accepted by both `claude --effort` and `agy --effort`
EFFORT_UP = ["debug", "root cause", "investigate", "diagnose", "why does", "why is", "race condition",
             "deadlock", "intermittent", "flaky", "memory leak", "performance", "optimize", "algorithm",
             "architecture", "design", "trade-off", "tradeoff", "edge case", "security", "vulnerability",
             "migrate", "migration", "refactor", "concurrency", "reverse engineer", "prove", "analyze",
             "analyse", "compare", "plan", "strategy", "complex", "tricky", "subtle"]
EFFORT_DOWN = ["rename", "typo", "format", "reformat", "bump", "comment", "docstring", "whitespace",
               "wording", "spelling", "label", "copy", "move file", "delete file", "sort imports",
               "simple", "trivial", "tiny", "quick", "just"]


EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")


def model_family(model):
    """'gemini-4-hydrogen-medium' -> 'gemini-4-hydrogen'; a model without an effort suffix is its own family."""
    return EFFORT_SUFFIX.sub("", str(model or "").strip().lower())


def model_efforts(tool, model, cfg):
    """The effort levels `model` accepts on `tool`, low to high.

    tools.<tool>.effort_levels is either one list for every model, or a dict per model family
    ({"gemini-4-hydrogen": ["low", "medium", "high", "xhigh"], "*": [...]}); a family is matched exactly,
    then by the longest key it starts with, then "*". An empty list means the model has no effort control."""
    levels = (cfg["tools"].get(tool) or {}).get("effort_levels")
    if isinstance(levels, dict):
        fam = model_family(model)
        hit = levels.get(fam)
        if hit is None:
            keys = sorted((k for k in levels if k != "*" and fam.startswith(k.lower())), key=len, reverse=True)
            hit = levels[keys[0]] if keys else levels.get("*", EFFORT_SCALE)
        levels = hit
    if levels is None:
        levels = EFFORT_SCALE
    return sorted((e for e in levels if e in EFFORT_SCALE), key=EFFORT_SCALE.index)


def clamp_effort(effort, allowed):
    """The allowed level nearest to `effort` (a tie goes to the lower one); '' when nothing is allowed."""
    if not allowed or effort not in EFFORT_SCALE:
        return ""
    i = EFFORT_SCALE.index(effort)
    return min(allowed, key=lambda e: (abs(EFFORT_SCALE.index(e) - i), EFFORT_SCALE.index(e)))


def fit_effort(tool, model, effort, cfg, keep_model=False):
    """(model, effort) that agree with each other and with what the model accepts.

    claude takes the effort as a flag (--effort), so the model stays and the effort is clamped to the model's
    levels. agy carries the effort in the slug (tools.agy.effort_in_model: gemini-4-hydrogen-<effort>), so the
    slug is rewritten to the routed effort. keep_model (a model the owner picked by name) keeps the slug and
    reads its effort from it instead."""
    tcfg = cfg["tools"].get(tool) or {}
    allowed = model_efforts(tool, model, cfg)
    if tcfg.get("effort_in_model"):
        m = EFFORT_SUFFIX.search(str(model or ""))
        if not m or not allowed:                      # no effort in this slug (orchestrator, auto): leave it
            return model, ""
        if keep_model:
            return model, m.group(1)
        eff = clamp_effort(effort or m.group(1), allowed)
        return f"{model_family(model)}-{eff}", eff
    return model, clamp_effort(effort, allowed) if effort else ""


def pick_effort(task, tool, level, cfg, adjust=True, brief=True, model=None):
    """Return (effort, reason) for a task on a tool.

    The level's configured effort is the baseline. Unless adjust is False (a manual level
    override, or effort_routing off), the prompt moves it one step: reasoning words
    (debug, root cause, race, design...) raise it, mechanical words (rename, typo, bump...)
    lower it. A baseline of "" stays "". The result is clamped to the levels the model accepts
    (`model_efforts`; `model` defaults to the tool's model for the level), so claude and agy stay valid.
    Use `fit_effort` afterwards to make an agy slug carry the chosen effort.
    """
    tcfg = cfg["tools"].get(tool, {})
    base = (tcfg.get("efforts") or {}).get(normalize(level), "")
    if model is None:
        try:
            model = pick_model(tool, level, cfg)
        except (ValueError, KeyError):
            model = ""
    allowed = model_efforts(tool, model, cfg) if tool in cfg["tools"] else EFFORT_SCALE
    if base in EFFORT_SCALE:
        base = clamp_effort(base, allowed)
    if not base or not adjust or not cfg.get("effort_routing", True):
        return base, ""
    if base not in EFFORT_SCALE:
        return base, ""
    c = cfg.get("classifier", {})
    head = (task or "")[:MAX_CLASSIFY_CHARS]
    text = FILE_PATH_RE.sub(" ", NEGATION.sub(" ", head).lower())
    up = _hits(text, c.get("effort_up_keywords", EFFORT_UP))
    down = _hits(text, c.get("effort_down_keywords", EFFORT_DOWN))
    score = min(2, len(up)) - min(2, len(down))
    if brief and len(head.split()) >= c.get("heavy_word_count", 60):
        score += 1
    step = (score > 0) - (score < 0)
    if not step:
        return base, ""
    i = EFFORT_SCALE.index(base) + step
    target = EFFORT_SCALE[max(0, min(len(EFFORT_SCALE) - 1, i))]
    # nearest allowed effort in the direction of the move
    if step > 0:
        cands = [e for e in allowed if EFFORT_SCALE.index(e) <= EFFORT_SCALE.index(target)]
        eff = max(cands, key=EFFORT_SCALE.index) if cands else base
    else:
        cands = [e for e in allowed if EFFORT_SCALE.index(e) >= EFFORT_SCALE.index(target)]
        eff = min(cands, key=EFFORT_SCALE.index) if cands else base
    if eff == base:
        return base, ""
    why = ", ".join((up if step > 0 else down)[:3]) or "long brief"
    return eff, f"effort {base} -> {eff} ({why})"
