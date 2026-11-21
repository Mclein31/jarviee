"""Cross-tool fallback engine: routes to equivalent models when quota or rate limits hit.

Follows the equivalence map, enforces 2-hop maximums, respects 'never fallback to opus',
and guarantees heavy never drops more than one level silently.
"""
import shutil
import time
from typing import Dict, List, Optional, Tuple

from . import omniroute
from .router import LEVELS, rank as level_rank


def level_of_model(tool: str, model: str, cfg: dict) -> str:
    """Find the logical level (basic/light/complex/heavy) for a tool's model slug."""
    models = (cfg.get("tools", {}).get(tool, {}) or {}).get("models", {})
    for lvl, m in models.items():
        if m == model:
            return lvl
    # Fallback to level heuristic by name
    low = model.lower()
    if "flash" in low or "haiku" in low or "small" in low:
        return "basic"
    if "opus" in low or "fable" in low or "max" in low:
        return "heavy"
    if "orchestrator" in low:
        return "complex"
    if "neon" in low:
        return "heavy" if low.endswith(("-high", "-xhigh")) else "complex"
    if "high" in low:
        return "complex"
    return "light"


def is_available(tool: str, model: str, cfg: dict, store=None, now: Optional[float] = None) -> Tuple[bool, str]:
    """Pre-flight check if tool:model is ready to receive tasks."""
    if tool not in cfg.get("tools", {}):
        return False, f"tool '{tool}' is not configured"
    cmd = cfg["tools"][tool].get("command", [])
    if not cmd or not shutil.which(cmd[0]):
        return False, f"{cmd[0] if cmd else tool} not found on PATH"
    if store is not None:
        q = store.get_quota(tool, model, now=now)
        if q and q.get("status") in ("exhausted", "transient"):
            until_ts = q.get("until")
            ref = now if now is not None else time.time()
            if until_ts and until_ts > ref:
                secs = int(until_ts - ref)
                h, m = divmod(secs // 60, 60)
                d, h = divmod(h, 24)
                time_str = f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{secs}s"
                return False, f"{tool}:{model} {q['status']} (resets in {time_str})"
    if tool == "omniroute":
        if not omniroute.enabled(cfg):
            return False, "omniroute is disabled"
        ok, reason = omniroute.health(cfg, now=now)
        if not ok:
            return False, reason or "omniroute gateway unavailable"
    return True, ""


def get_candidates(tool: str, model: str, cfg: dict, is_model_only_quota: bool = False) -> List[Tuple[str, str]]:
    """Return raw list of (cand_tool, cand_model) candidates in preference order."""
    fb_cfg = cfg.get("fallback", {})
    equivalents: Dict[str, List[str]] = fb_cfg.get("equivalents", {})

    candidates = []
    # If per-model quota, try same tool's equivalent or higher models first
    if is_model_only_quota and tool in cfg.get("tools", {}):
        cur_lvl = level_of_model(tool, model, cfg)
        cur_rank = level_rank(cur_lvl)
        tool_models = cfg["tools"][tool].get("models", {})
        for lvl, m in tool_models.items():
            if m != model and level_rank(lvl) >= cur_rank:
                candidates.append((tool, m))

    # Cross-tool equivalents from config
    key = f"{tool}:{model}"
    if key in equivalents:
        for eq in equivalents.get(key, []):
            if ":" in eq:
                t, m = eq.split(":", 1)
                candidates.append((t, m))
    else:
        cur_lvl = level_of_model(tool, model, cfg)
        canon_model = (cfg.get("tools", {}).get(tool, {}).get("models") or {}).get(cur_lvl)
        canon_key = f"{tool}:{canon_model}" if canon_model else ""
        if canon_key and canon_key in equivalents:
            for eq in equivalents.get(canon_key, []):
                if ":" in eq:
                    t, m = eq.split(":", 1)
                    candidates.append((t, m))
        else:
            lanes = [t for t in cfg.get("lanes", {}) if t != tool]
            alt = lanes[0] if lanes else ("claude" if tool == "agy" else "agy")
            if alt in cfg.get("tools", {}):
                alt_m = (cfg["tools"][alt].get("models") or {}).get(cur_lvl)
                if alt_m:
                    candidates.append((alt, alt_m))
    return candidates


def use_available_tool(cfg: dict) -> bool:
    """The "Use whichever tool is available" switch (Settings): on by default."""
    return bool(cfg.get("fallback", {}).get("use_available_tool", True))


def other_tool_candidate(tool: str, level: str, cfg: dict) -> Optional[Tuple[str, str, str]]:
    """(tool, model, level): the other tool at the same level, for the switch. With never_fallback_to_opus
    on, Claude's heavy model steps down to complex instead (the one-level-drop rule still allows it)."""
    lanes = [t for t in cfg.get("lanes", {}) if t != tool]
    alt = lanes[0] if lanes else ("claude" if tool == "agy" else "agy")
    models = (cfg.get("tools", {}).get(alt) or {}).get("models") or {}
    if alt == tool or not models:
        return None
    lvl, m = level, models.get(level)
    if alt == "claude" and m == "opus" and cfg.get("fallback", {}).get("never_fallback_to_opus", True):
        lvl, m = "complex", models.get("complex")
    return (alt, m, lvl) if m else None


def resolve_fallback(tool: str, model: str, cfg: dict, store=None, hops: int = 0,
                     now: Optional[float] = None, original_level: Optional[str] = None,
                     is_model_only_quota: bool = False) -> Optional[dict]:
    """Find the best available fallback option.

    Returns dict with {tool, model, level, effort, hop_count, why} or None if no candidate matches.
    """
    fb_cfg = cfg.get("fallback", {})
    chain_on = fb_cfg.get("enabled", True)
    if not chain_on and not use_available_tool(cfg):
        return None

    max_hops = fb_cfg.get("max_hops", 2)
    if hops >= max_hops:
        return None

    orig_lvl = original_level or level_of_model(tool, model, cfg)
    orig_rank = level_rank(orig_lvl)
    never_opus = fb_cfg.get("never_fallback_to_opus", True)

    candidates = get_candidates(tool, model, cfg, is_model_only_quota=is_model_only_quota) if chain_on else []
    sw = other_tool_candidate(tool, orig_lvl, cfg) if use_available_tool(cfg) else None
    switch = sw[:2] if sw else None
    if switch and switch not in candidates:
        candidates.append(switch)

    for cand_tool, cand_model in candidates:
        if cand_tool == tool and cand_model == model:
            continue
        # Never fall back to opus if setting is enabled
        if never_opus and cand_tool == "claude" and cand_model == "opus":
            continue

        # the switch knows its level; a slug can map to two levels (Claude's sonnet is light and complex)
        cand_lvl = sw[2] if (cand_tool, cand_model) == switch else level_of_model(cand_tool, cand_model, cfg)
        cand_rank = level_rank(cand_lvl)

        # Rule: Never drop more than one level silently
        if orig_rank - cand_rank > 1:
            continue

        ok, _ = is_available(cand_tool, cand_model, cfg, store=store, now=now)
        if ok:
            effort = (cfg.get("tools", {}).get(cand_tool, {}).get("efforts") or {}).get(cand_lvl, "")
            via = " (use available tool)" if (cand_tool, cand_model) == switch else ""
            return {
                "tool": cand_tool,
                "model": cand_model,
                "level": cand_lvl,
                "effort": effort,
                "hops": hops + 1,
                "why": f"fallback from {tool}:{model} to {cand_tool}:{cand_model}{via}",
            }

    # Last resort: OmniRoute gateway (same level, never loops on itself, no opus rule)
    if tool != "omniroute" and omniroute.enabled(cfg):
        omni_cfg = cfg.get("tools", {}).get("omniroute") or {}
        omni_model = (omni_cfg.get("models") or {}).get(orig_lvl)
        if omni_model:
            ok, _ = is_available("omniroute", omni_model, cfg, store=store, now=now)
            if ok:
                effort = (omni_cfg.get("efforts") or {}).get(orig_lvl, "")
                return {
                    "tool": "omniroute",
                    "model": omni_model,
                    "level": orig_lvl,
                    "effort": effort,
                    "hops": hops + 1,
                    "why": "OmniRoute (agy and Claude are out)",
                }

    # If all candidates are exhausted, calculate earliest_until
    earliest_until = None
    ref = now if now is not None else time.time()
    if store is not None:
        for cand_tool, cand_model in candidates:
            q = store.get_quota(cand_tool, cand_model, now=now)
            u = q.get("until") if q else None
            if u and u > ref:
                if earliest_until is None or u < earliest_until:
                    earliest_until = u

    return {
        "tool": None,
        "model": None,
        "level": orig_lvl,
        "waiting": True,
        "earliest_until": earliest_until,
        "hops": hops,
        "why": f"all fallback options exhausted for {tool}:{model}",
    }
