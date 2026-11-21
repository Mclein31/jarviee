"""Choose the tool and model for a task.

Order of rules: manual override > dispatch table (by level) > borderline preference
(light/complex → agy first) > fallback when a tool's binary is missing or its quota is out.
Also decides whether a finished agy run should be escalated to Claude.
"""
import shutil

from . import fallback as fb_engine
from . import usage as usage_mod
from .router import assess, fit_effort, normalize, pick_effort, pick_model


def other_tool(tool, cfg):
    """The other lane's tool (agy <-> claude)."""
    lanes = [t for t in cfg.get("lanes", {}) if t != tool]
    return lanes[0] if lanes else ("claude" if tool == "agy" else "agy")


def _installed(tool, cfg):
    try:
        return bool(shutil.which(cfg["tools"][tool]["command"][0]))
    except (KeyError, IndexError, TypeError):
        return False


def tool_available(tool, cfg, quota=None, store=None, model=None, now=None):
    """(ok, reason). Checks binary, quota dict, and store quota_state circuit breaker."""
    if tool not in cfg["tools"]:
        return False, f"tool '{tool}' is not configured"
    argv0 = cfg["tools"][tool]["command"][0]
    if not shutil.which(argv0):
        return False, f"{argv0} not found on PATH"
    if quota and quota.get(tool):
        return False, f"{tool} quota exhausted ({quota[tool]})"
    if store is not None:
        ok, why = fb_engine.is_available(tool, model or "*", cfg, store=store, now=now)
        if not ok:
            return False, why
    return True, ""


def route(task, cfg, level=None, tool=None, model=None, quota=None, check_available=True,
          store=None, now=None, usage=None):
    """Return the routing decision for one task.

    {level, suggested_level, tool, model, effort, reasons, borderline, fallback, waiting_for_quota, earliest_until}
    """
    a = assess(task, cfg)
    suggested = a["level"]
    final = normalize(level) or suggested
    reasons = list(a["reasons"])
    if level and final != suggested:
        reasons.append(f"manual override (router suggested {suggested})")

    entry = cfg["dispatch"][final]
    picked_tool, picked_model = entry["tool"], entry.get("model") or ""
    borderline_agy = False
    if (not level and not tool and a["borderline"] and final == "complex"
            and cfg.get("prefer_agy_on_borderline", False)):
        light = cfg["dispatch"]["light"]
        picked_tool = light["tool"]
        picked_model = pick_model(picked_tool, final, cfg)
        borderline_agy = True
        reasons.append("borderline: agy first (credits expire soon); escalates to Claude if it fails")

    if tool:                                   # a manual tool choice always wins
        if tool not in cfg["tools"]:
            raise ValueError(f"unknown tool '{tool}' (configured: {', '.join(cfg['tools'])})")
        picked_tool = tool
        picked_model = model or pick_model(tool, final, cfg)
        reasons.append("manual tool override")
    elif model:
        picked_model = model
    if not picked_model:
        picked_model = pick_model(picked_tool, final, cfg)

    fallback_note = ""
    waiting_for_quota = False
    earliest_until = None
    orig_tool, orig_model = picked_tool, picked_model

    if check_available and (not tool or fb_engine.use_available_tool(cfg)):
        ok, why = tool_available(picked_tool, cfg, quota=quota, store=store, model=picked_model, now=now)
        if not ok:
            # Check cross-tool fallback engine
            fb_res = fb_engine.resolve_fallback(picked_tool, picked_model, cfg, store=store, hops=0,
                                                now=now, original_level=final)
            if fb_res and fb_res.get("tool"):
                picked_tool = fb_res["tool"]
                picked_model = fb_res["model"]
                final = fb_res.get("level") or final
                via = " (use available tool)" if "use available tool" in fb_res.get("why", "") else ""
                fallback_note = f"{why}; routed to {picked_tool}:{picked_model}{via}"
                if not _installed(orig_tool, cfg):
                    fallback_note = f"{orig_tool} is not installed; routed to {picked_tool}:{picked_model}{via}"
                reasons.append("fallback: " + fallback_note)
            elif fb_res and fb_res.get("waiting"):
                waiting_for_quota = True
                earliest_until = fb_res.get("earliest_until")
                if not _installed(picked_tool, cfg):
                    fallback_note = (f"{picked_tool} is not installed; waiting for quota ({why})"
                                     if _installed(other_tool(picked_tool, cfg), cfg) else
                                     "no agent CLI found: run python3 hub.py doctor")
                else:
                    fallback_note = f"waiting for quota ({why})"
                reasons.append(fallback_note)
            else:
                alt = other_tool(picked_tool, cfg)
                if tool_available(alt, cfg, quota=quota, store=store, now=now)[0]:
                    fallback_note = f"{why}; routed to {alt}"
                    if not _installed(picked_tool, cfg):
                        fallback_note = f"{picked_tool} is not installed; routed to {alt}"
                    picked_tool, picked_model = alt, pick_model(alt, final, cfg)
                    reasons.append("fallback: " + fallback_note)

    usage_warning = ""
    if check_available and not waiting_for_quota and (cfg.get("usage_routing") or {}).get("enabled", True):
        usage_warning, target = _usage_pressure(
            cfg, usage_mod.cached() if usage is None else usage, picked_tool, final,
            manual=bool(tool), quota=quota, store=store, now=now)
        if usage_warning:
            reasons.append(("usage: " if not target else "fallback: ") + usage_warning)
        if target:
            picked_tool, picked_model, final = target
            fallback_note = fallback_note or usage_warning

    if check_available and picked_tool == "agy" and not waiting_for_quota:
        data = usage_mod.cached() if usage is None else usage
        grp = usage_mod.agy_model_exhausted(data, picked_model)
        if grp:
            efforts = cfg["tools"]["agy"].get("efforts") or {}
            eff = {"max": "high", "xhigh": "high"}.get(efforts.get(final, "medium"), efforts.get(final, "medium"))
            for tmpl in cfg["tools"]["agy"].get("model_preference") or []:
                alt_m = tmpl.replace("{effort}", eff)
                if alt_m != picked_model and not usage_mod.agy_model_exhausted(data, alt_m):
                    reasons.append(f"fallback: agy {grp} quota at 0%; routed to agy:{alt_m}")
                    fallback_note = fallback_note or f"agy {grp} quota at 0%; routed to {alt_m}"
                    picked_model = alt_m
                    break

    # the effort is routed per task even when the level (and so the model) was picked by hand
    effort, effort_why = pick_effort(task, picked_tool, final, cfg, model=picked_model)
    if effort_why:
        reasons.append(effort_why)
    # the model and its effort must agree: agy's effort is part of the slug, claude's is a flag
    fitted, effort = fit_effort(picked_tool, picked_model, effort, cfg, keep_model=bool(model) and picked_model == model)
    if fitted != picked_model:
        reasons.append(f"effort {effort}: {picked_model} -> {fitted}")
        picked_model = fitted
    return {"level": final, "suggested_level": suggested, "tool": picked_tool, "model": picked_model,
            "effort": effort, "reasons": reasons, "borderline": a["borderline"],
            "borderline_agy": borderline_agy, "fallback": fallback_note,
            "waiting_for_quota": waiting_for_quota, "earliest_until": earliest_until,
            "original_tool": orig_tool, "original_model": orig_model, "usage_warning": usage_warning}


def _usage_pressure(cfg, data, tool, level, manual, quota, store, now):
    """(message, target). target is (tool, model, level) when the other lane should take the task.

    Warns only when the lane is manually chosen, the other lane is also near its limit, or it is
    unavailable. The other lane's model comes from fallback.other_tool_candidate, so
    never_fallback_to_opus still steps Claude's heavy model down to complex.
    """
    pressure = usage_mod.lane_pressure(data, cfg)
    why = pressure.get(tool)
    if not why:
        return "", None
    alt = other_tool(tool, cfg)
    if manual:
        return f"{tool} is near its limit ({why})", None
    if alt in pressure:
        return f"{tool} is near its limit ({why}) and so is {alt} ({pressure[alt]})", None
    cand = fb_engine.other_tool_candidate(tool, level, cfg)
    if not cand or not tool_available(cand[0], cfg, quota=quota, store=store, model=cand[1], now=now)[0]:
        return f"{tool} is near its limit ({why}); {alt} is unavailable", None
    return f"{tool} is near its limit ({why}); routed to {cand[0]}:{cand[1]}", cand


def _matches(text, patterns):
    low = (text or "").lower()
    return next((p for p in patterns if p.lower() in low), "")


def quota_hit(log_text, cfg):
    """The quota/rate-limit phrase found in a run's output, or ''."""
    return _matches(log_text, cfg.get("quota_patterns", []))


def escalation_reason(tool, exit_code, log_text, cfg, attempts_escalated=0):
    """Why an agy run should be re-queued on Claude, or '' when it should not."""
    esc = cfg.get("escalation", {})
    if tool == esc.get("to_tool", "claude") or attempts_escalated >= esc.get("max", 1):
        return ""
    if exit_code not in (0, None):
        return f"{tool} exited with code {exit_code}"
    hit = _matches(log_text, esc.get("blocked_patterns", []))
    return f"{tool} reported it was blocked ('{hit}')" if hit else ""
