from __future__ import annotations

from typing import Any

import hashlib
import json
import time

from src.config import gate_mode, load_config, resolve_decisions_path, resolve_log_path
from src.jev_client import system_one
from src.logger import log_run
from src import secrets as _secrets
from src.secrets import MissingKeyError


BYPASS_MARKERS = ("bypass jev", "bypass jev:", "no jev")


def _bypassed(state: dict[str, Any]) -> bool:
    raw = " ".join(
        str(state.get(k, ""))
        for k in ("goal", "raw", "user_message", "notes")
    ).lower()
    return any(m in raw for m in BYPASS_MARKERS)


def _jev_route(state: dict[str, Any], cfg: dict[str, Any], log_path) -> dict[str, Any]:
    """Original Jev routing (one system_one call). Expects state["goal"] already normalized."""

    # Keep the kill-switch path dependency-free so it works during incidents.
    from typesafe_sdk import Choice, Noul, Score

    thr = cfg.get("thresholds") or {}
    limits = cfg.get("limits") or {}
    model = cfg.get("model") or "jev-latest"

    # Normalize state for Jev
    jstate = {
        "goal": state.get("goal") or state.get("raw") or "",
        "kind_hint": state.get("kind") or state.get("kind_hint") or "unknown",
        "has_cached_artifact": bool(state.get("cached_artifact")),
        "cached_note": state.get("cached_note") or "",
        "prior_error": state.get("prior_error") or "",
        "same_error_count": int(state.get("same_error_count") or 0),
        "sources_found": int(state.get("sources_found") or 0),
        "constraints": state.get("constraints") or "",
    }

    questions = {
        "intent": Choice(
            instructions="What kind of work does this request mainly need?",
            criteria={
                "chat": "Short answer or conversation; no tools needed",
                "lookup": "Known file/API/status check; mostly deterministic",
                "research": "Need web/search and synthesis",
                "browser": "Need interactive browser clicks",
                "coding": "Edit code / tests / repo work",
                "write": "Draft long prose/email/doc",
                "account": "Send, publish, pay, delete, change permissions",
            },
        ),
        "reuse_cache": Noul(
            instructions="Is there already a fresh enough cached result that should be reused instead of doing new heavy work?"
        ),
        "needs_subagent": Noul(
            instructions="Does this clearly need an extra specialized bot (research/browser/coding) beyond one Grok Bot turn?"
        ),
        "stop_retry": Noul(
            instructions="Given prior_error and same_error_count, should we STOP retrying the same approach?"
        ),
        "complexity": Score(
            instructions="How much Grok Bot effort is justified?",
            criteria=[
                "Trivial — one step or cached",
                "Normal — short tool use",
                "Heavy — multi-step research/browser/coding",
            ],
        ),
    }

    result = system_one(jstate, questions, model=model)
    intent = result.choices["intent"]
    reuse = result.nouls["reuse_cache"]
    sub = result.nouls["needs_subagent"]
    stop = result.nouls["stop_retry"]
    complexity = result.scores["complexity"]

    reuse_n = float(reuse.noul)
    sub_n = float(sub.noul)
    stop_n = float(stop.noul)
    # Score 0..2 → normalize
    comp = float(complexity.score) / 2.0

    action = "proceed_full"
    reason = "default full Grok Bot work"

    if jstate["has_cached_artifact"] and reuse_n >= float(thr.get("reuse_min", 0.65)):
        action = "reuse_cache"
        reason = f"reuse_cache noul={reuse_n:.2f}"
    elif jstate["same_error_count"] >= int(limits.get("max_retries_same_error", 1)) and stop_n >= 0.55:
        action = "stop_retry"
        reason = f"stop_retry noul={stop_n:.2f} same_error_count={jstate['same_error_count']}"
    elif intent.choice == "lookup" and float(intent.confidence) >= float(thr.get("min_choice_confidence", 0.55)):
        action = "run_deterministic"
        reason = "intent=lookup"
    elif intent.choice == "chat" and float(intent.confidence) >= float(thr.get("min_choice_confidence", 0.55)):
        action = "chat_only"
        reason = "intent=chat"
    elif intent.choice == "account":
        action = "ask_human"
        reason = "account/irreversible class — require approval"
    elif sub_n >= float(thr.get("subagent_min", 0.75)):
        action = "allow_subagent"
        reason = f"needs_subagent noul={sub_n:.2f}"
    elif intent.choice in {"research", "browser"}:
        action = "research_capped"
        reason = f"cap sources at {limits.get('max_browser_sources', 5)}"

    details = {
        "intent": intent.choice,
        "intent_confidence": round(float(intent.confidence), 4),
        "intent_probs": {k: round(float(v), 4) for k, v in (intent.probabilities or {}).items()},
        "reuse_cache": round(reuse_n, 4),
        "needs_subagent": round(sub_n, 4),
        "stop_retry": round(stop_n, 4),
        "complexity_0_1": round(comp, 4),
        "max_browser_sources": int(limits.get("max_browser_sources", 5)),
    }

    out = {
        "action": action,
        "reason": reason,
        "mode": cfg.get("mode"),
        "jev_used": True,
        "details": details,
        "policy": {
            "honor_in_active_mode": True,
            "shadow_mode_is_advisory": cfg.get("mode") == "shadow",
        },
    }
    log_run(
        log_path,
        {
            "event": "route",
            "goal": jstate["goal"][:300],
            "action": action,
            "reason": reason,
            "mode": cfg.get("mode"),
            "details": details,
        },
    )
    return out


# ---- 2026-11-12 Engineer: gate modes, goal check, decisions ledger (jev-router skill) ----

class GoalError(ValueError):
    pass


ACTION_TO_CHOICE = {
    "proceed_full": "proceed", "allow_subagent": "proceed",
    "reuse_cache": "cheaper_lane", "run_deterministic": "cheaper_lane",
    "chat_only": "cheaper_lane", "research_capped": "cheaper_lane",
    "stop_retry": "hold", "ask_human": "ask_chris",
}
CAUTION = ["proceed", "cheaper_lane", "hold", "ask_chris"]
DECISION_TYPES = ("costly_action_gate", "needs_approval")


def extract_goal(state: dict[str, Any]) -> str:
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    for v in (state.get("goal"), ctx.get("goal"), state.get("raw")):
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise GoalError("goal is required: set context.goal (or goal) to a non-empty string under 300 chars")


def _step_down(choice: str) -> str:
    # 2026-11-16 Engineer (Chris green t3075s12340): low confidence steps proceed to cheaper_lane only.
    # It never turns cheaper_lane into hold, or hold into ask_chris (those came from Jev itself).
    return _more_cautious(choice, "cheaper_lane")


# ---- 2026-11-16 Engineer: Jev gate fixes (Karen REPORT jev-friction-2026-11-16) ----
NOT_A_GREEN = {"none", "null", "n/a", "na", "no", "false", "0", "pending", "tbd", "unknown", "-", "?", "nil"}


def valid_green(green: Any) -> bool:
    """A named green: non-empty, at least 3 chars, not a placeholder word."""
    if not isinstance(green, str):
        return False
    g = green.strip()
    return len(g) >= 3 and g.lower() not in NOT_A_GREEN


def decide(dtype: str, green: Any, jev_action: str, conf: Any, conf_min: float) -> str:
    """Pure gate rule (Fix 1). Used by route_task and by the offline replay.
    - needs_approval with no valid green: ask_chris, always (fail closed).
    - valid green named: proceed (Jev's account/ask_human read no longer re-asks Chris),
      except stop_retry stays hold (a green approves the action, not a retry loop).
    - otherwise Jev's choice; below conf_min, proceed steps to cheaper_lane (never to hold)."""
    choice = ACTION_TO_CHOICE.get(jev_action, "hold")
    if dtype == "needs_approval" and not valid_green(green):
        return "ask_chris"
    if valid_green(green):
        return "hold" if choice == "hold" else "proceed"
    if conf is not None and float(conf) < conf_min:
        choice = _step_down(choice)
    return choice


def key_present() -> bool:
    """Fix 2: True if TYPESAFE_API_KEY is in env or the box keyring. Bool only; never returns,
    prints or logs the value. Resolves via secrets.ensure path so the later call reuses it."""
    try:
        return bool(_secrets.ensure_typesafe_api_key())
    except MissingKeyError:
        return False
    except Exception:
        return False


def caller_present(state: dict[str, Any]) -> bool:
    c = state.get("caller")
    return isinstance(c, str) and bool(c.strip()) and c.strip().lower() != "unknown"


def _more_cautious(a: str, b: str) -> str:
    return a if CAUTION.index(a) >= CAUTION.index(b) else b


def _input_hash(state: dict[str, Any]) -> str:
    raw = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def route_task(state: dict[str, Any]) -> dict[str, Any]:
    """Validate goal, apply gate mode (off | log_only | enforce), run Jev when needed,
    and write one row to the decisions ledger on every path. Raises GoalError on an empty goal
    and MissingKeyError (after logging) when Jev must run but no key is found."""
    goal = extract_goal(state)
    cfg = load_config()
    log_path = resolve_log_path(cfg)
    dec_path = resolve_decisions_path(cfg)
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    dtype = state.get("decision_type") or "costly_action_gate"
    if dtype not in DECISION_TYPES:
        dtype = "costly_action_gate"
    green = str(ctx.get("green") or state.get("green") or "").strip()
    thr = cfg.get("thresholds") or {}
    conf_min = float(thr.get("gate_confidence_min", 0.70))

    gate = gate_mode(cfg, dtype)
    if not cfg.get("enabled", True) or _bypassed({**state, "goal": goal}):
        gate = "off"

    # needs_approval code rule runs before Jev, in every mode.
    if dtype == "needs_approval":
        baseline = "proceed" if valid_green(green) else "ask_chris"
    else:
        baseline = "proceed"

    row = {
        "caller": state.get("caller") or "unknown", "decision_type": dtype,
        "action": ctx.get("action") or state.get("action") or "", "input_hash": _input_hash(state),
        "choice": None, "confidence": None, "mode": gate, "acted_on": None,
        "green": green, "outcome": None,
        "cost_cents": None, "latency_ms": None, "error": "",
    }
    t0 = time.monotonic()

    def _write(**kw):
        row.update(kw)
        row["latency_ms"] = int((time.monotonic() - t0) * 1000)
        log_run(dec_path, dict(row))

    if gate == "off":
        out = {"action": "proceed_full", "reason": "gate off / disabled / bypass jev",
               "mode": cfg.get("mode"), "gate": "off", "jev_used": False, "details": {},
               "choice": baseline, "jev_choice": None, "confidence": None,
               "acted_on": "code_rule" if dtype == "needs_approval" else "old"}
        _write(choice=baseline, acted_on=out["acted_on"])
        log_run(log_path, {"event": "route", "goal": goal[:300], **{k: out[k] for k in ("action", "reason", "gate")}})
        return out

    # Fix 2 (2026-11-16): precheck key + caller before any Jev call. On a miss: skip, log, no raise.
    miss = "" if caller_present(state) else "precheck:caller_missing"
    if not miss and not key_present():
        miss = "precheck:key_missing"
    if miss:
        if gate == "enforce":
            # No Jev read: needs_approval uses the code rule, anything else holds (fail closed).
            choice, acted_on = (baseline if dtype == "needs_approval" else "hold"), "precheck_skip"
        else:
            choice, acted_on = baseline, "old"
        out = {"action": "skipped", "reason": miss, "mode": cfg.get("mode"), "gate": gate,
               "jev_used": False, "details": {}, "choice": choice, "jev_choice": None,
               "confidence": None, "acted_on": acted_on, "error": miss}
        _write(choice=choice, acted_on=acted_on, error=miss)
        return out

    jstate = dict(state)
    jstate["goal"] = goal
    for k in ("cached_artifact", "prior_error", "same_error_count"):
        if k not in jstate and k in ctx:
            jstate[k] = ctx[k]
    try:
        out = _jev_route(jstate, cfg, log_path)
    except MissingKeyError:
        _write(error="key_missing", acted_on="fail_closed")
        raise
    except Exception as exc:
        _write(error=f"jev_error:{type(exc).__name__}", acted_on="fail_closed")
        raise

    conf = out["details"].get("intent_confidence")
    jev_choice = decide(dtype, green, out["action"], conf, conf_min)

    if gate == "enforce":
        choice, acted_on = jev_choice, "jev"
    else:  # log_only
        choice, acted_on = baseline, "old"
    out.update({"gate": gate, "choice": choice, "jev_choice": jev_choice,
                "confidence": conf, "acted_on": acted_on})
    _write(choice=jev_choice, confidence=conf, acted_on=acted_on)
    return out
