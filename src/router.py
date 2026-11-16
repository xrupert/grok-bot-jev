from __future__ import annotations

from typing import Any

import hashlib
import json
import time

from src.config import gate_mode, load_config, resolve_decisions_path, resolve_log_path
from src.jev_client import DEFAULT_TIMEOUT_S, JevTimeout, system_one
from src import cost as _cost
from src import greens as _greens
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

    timeout_s = float(limits.get("jev_timeout_s", DEFAULT_TIMEOUT_S))
    result = system_one(jstate, questions, model=model, timeout_s=timeout_s)
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
        # 2026-11-16 Engineer (t3082s12374): r.model and r.usage on every call.
        "meta": {"model": getattr(result, "model", None), "usage": getattr(result, "usage", None)},
    }
    log_run(
        log_path,
        {
            "event": "route",
            "caller": state.get("caller") or "unknown",
            "goal": jstate["goal"][:300],
            "action": action,
            "reason": reason,
            "mode": cfg.get("mode"),
            "details": details,
        },
    )
    return out


# ---- 2026-11-12 Engineer: gate modes, goal check, decisions ledger (jev-router skill) ----
# ---- 2026-11-16 Engineer, jevfix2 (Chris green t3082s12374): 0.5 floor, real green check,
#      $5 cap, cost tracking (cost_cents, model, usage), 3 s timeout, CLI writes every row. ----

class GoalError(ValueError):
    pass


ACTION_TO_CHOICE = {
    "proceed_full": "proceed", "allow_subagent": "proceed",
    "reuse_cache": "cheaper_lane", "run_deterministic": "cheaper_lane",
    "chat_only": "cheaper_lane", "research_capped": "cheaper_lane",
    "stop_retry": "hold", "ask_human": "ask_chris",
}
CAUTION = ["proceed", "cheaper_lane", "hold", "ask_chris"]
DECISION_TYPES = ("costly_action_gate", "needs_approval", "creative_qc")
DEFAULT_FLOOR = 0.5       # below: ask_chris ("ask me when it's really unsure (below 0.5)")
DEFAULT_GATE_MIN = 0.70   # 0.5 to 0.70: proceed steps down to cheaper_lane
NOT_A_GREEN = _greens.NOT_A_GREEN
GREEN_OK = ("valid", "standing")


def extract_goal(state: dict[str, Any]) -> str:
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    for v in (state.get("goal"), ctx.get("goal"), state.get("raw")):
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise GoalError("goal is required: set context.goal (or goal) to a non-empty string under 300 chars")


def _more_cautious(a: str, b: str) -> str:
    return a if CAUTION.index(a) >= CAUTION.index(b) else b


def _step_down(choice: str) -> str:
    # 0.5 to 0.70: proceed steps to cheaper_lane only; never makes a choice less cautious.
    return _more_cautious(choice, "cheaper_lane")


def thresholds(cfg: dict[str, Any]) -> tuple[float, float]:
    thr = cfg.get("thresholds") or {}
    return (float(thr.get("ask_chris_below", DEFAULT_FLOOR)),
            float(thr.get("gate_confidence_min", DEFAULT_GATE_MIN)))


def green_status(green: Any, registry: "_greens.Registry | None" = None) -> str:
    return (registry or _greens.load()).check(green)[0]


def valid_green(green: Any, registry: "_greens.Registry | None" = None) -> bool:
    """A real green: an id in it is recorded in config/greens.yaml (green or standing)."""
    return green_status(green, registry) in GREEN_OK


def decide_ex(dtype: str, green: Any, jev_action: str, conf: Any, conf_min: float = DEFAULT_GATE_MIN,
              floor: float = DEFAULT_FLOOR, registry: "_greens.Registry | None" = None) -> tuple[str, str]:
    """Pure gate rule -> (choice, reason). Order:
    1. green recorded (or standing): proceed at any confidence (Chris: "my named green is enough");
       stop_retry stays hold. A valid green beats the 0.5 floor.
    2. a green was given but is not recorded: ask_chris, green_not_recorded.
    3. needs_approval with no green: ask_chris, no_green (fail closed).
    4. confidence below floor (0.5): ask_chris, low_confidence.
    5. floor to conf_min (0.70): proceed steps to cheaper_lane.
    6. otherwise Jev's choice. (The $5 cap is checked before Jev runs, in route_task.)"""
    choice = ACTION_TO_CHOICE.get(jev_action, "hold")
    status = green_status(green, registry)
    if status in GREEN_OK:
        return ("hold", "green_but_stop_retry") if choice == "hold" else ("proceed", f"green_{status}")
    if status == "registry_unreadable":
        return "ask_chris", "green_registry_unreadable"
    if status == "not_recorded":
        return "ask_chris", "green_not_recorded"
    if dtype == "needs_approval":
        return "ask_chris", "no_green"
    if conf is not None and float(conf) < floor:
        return "ask_chris", "low_confidence"
    if conf is not None and float(conf) < conf_min:
        stepped = _step_down(choice)
        return stepped, ("step_down" if stepped != choice else f"jev:{jev_action}")
    return choice, f"jev:{jev_action}"


def decide(dtype: str, green: Any, jev_action: str, conf: Any, conf_min: float = DEFAULT_GATE_MIN,
           floor: float = DEFAULT_FLOOR, registry: "_greens.Registry | None" = None) -> str:
    """Choice only (kept for the 11/16 replay scripts)."""
    return decide_ex(dtype, green, jev_action, conf, conf_min, floor, registry)[0]


def key_present() -> bool:
    """True if TYPESAFE_API_KEY is in env or the box keyring. Bool only; never returns,
    prints or logs the value."""
    try:
        return bool(_secrets.ensure_typesafe_api_key())
    except MissingKeyError:
        return False
    except Exception:
        return False


def caller_present(state: dict[str, Any]) -> bool:
    c = state.get("caller")
    return isinstance(c, str) and bool(c.strip()) and c.strip().lower() != "unknown"


def _input_hash(state: dict[str, Any]) -> str:
    raw = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_test(state: dict[str, Any]) -> bool:
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    return bool(state.get("test") or ctx.get("test"))


def base_row(state: dict[str, Any], gate: str = "") -> dict[str, Any]:
    """Ledger row with the pre-11/16 fields (unchanged names) plus the jevfix2 fields."""
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    dtype = state.get("decision_type") or "costly_action_gate"
    if dtype not in DECISION_TYPES:
        dtype = "costly_action_gate"
    row = {
        "caller": state.get("caller") or "unknown", "decision_type": dtype,
        "action": ctx.get("action") or state.get("action") or "", "input_hash": _input_hash(state),
        "choice": None, "confidence": None, "mode": gate, "acted_on": None,
        "green": str(ctx.get("green") or state.get("green") or "").strip(), "outcome": None,
        "cost_cents": None, "latency_ms": None, "error": "",
        # jevfix2
        "reason": "", "green_status": None, "green_id": None, "model": None, "usage": None,
        "cost_reason": "no_jev_call", "est_cost_cents": _cost.request_estimate_cents(state),
        "committed_cents": None, "written_by": "jev-cli",
    }
    if _is_test(state):
        row["test"] = True
    return row


def write_error_row(state: dict[str, Any], error: str, choice: str | None = None) -> None:
    """For CLI paths that fail before route_task can build a row (bad goal, bad input)."""
    try:
        cfg = load_config()
        dec_path = resolve_decisions_path(cfg)
        row = base_row(state if isinstance(state, dict) else {}, "")
        row.update(error=error, reason=error, acted_on="fail_closed", choice=choice, latency_ms=0)
        log_run(dec_path, row)
    except Exception:
        pass  # never mask the original error


def route_task(state: dict[str, Any]) -> dict[str, Any]:
    """Validate goal, apply gate mode (off | log_only | enforce), check the $5 cap, run Jev when
    needed, and write one row to the decisions ledger on every path. Raises GoalError on an empty
    goal and re-raises Jev errors after logging a fail-closed row."""
    cfg = load_config()
    dec_path = resolve_decisions_path(cfg)
    if state.get("decision_type") == "creative_qc":
        return route_creative_qc(state, cfg, dec_path)
    try:
        goal = extract_goal(state)
    except GoalError:
        write_error_row(state, "goal_required",
                        "ask_chris" if state.get("decision_type") == "needs_approval" else "hold")
        raise
    log_path = resolve_log_path(cfg)
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    floor, conf_min = thresholds(cfg)
    cap_cents, window = _cost.cap_settings(cfg)
    registry = _greens.load(cfg=cfg)

    row = base_row(state)
    dtype, green = row["decision_type"], row["green"]
    gstatus, gid = registry.check(green)
    row.update(green_status=gstatus, green_id=gid)
    gate = gate_mode(cfg, dtype)
    if not cfg.get("enabled", True) or _bypassed({**state, "goal": goal}):
        gate = "off"
    row["mode"] = gate

    # needs_approval code rule runs before Jev, in every mode: only a recorded green proceeds.
    if dtype == "needs_approval":
        baseline = "proceed" if gstatus in GREEN_OK else "ask_chris"
    else:
        baseline = "proceed"
    t0 = time.monotonic()

    def _write(acted: str | None = None, **kw):
        row.update(kw)
        row["latency_ms"] = int((time.monotonic() - t0) * 1000)
        acted = acted or row.get("choice")
        if acted in ("proceed", "cheaper_lane") and not row.get("test") and row["est_cost_cents"]:
            row["committed_cents"] = row["est_cost_cents"]  # counts toward the $5 cap
        log_run(dec_path, dict(row))

    def _out(**kw):
        base = {"mode": cfg.get("mode"), "gate": gate, "jev_used": False, "details": {},
                "jev_choice": None, "confidence": None, "green_status": gstatus}
        base.update(kw)
        return base

    if gate == "off":
        out = _out(action="proceed_full", reason="gate off / disabled / bypass jev", choice=baseline,
                   acted_on="code_rule" if dtype == "needs_approval" else "old")
        _write(choice=baseline, acted_on=out["acted_on"], reason="gate_off")
        log_run(log_path, {"event": "route", "caller": row["caller"], "goal": goal[:300],
                           **{k: out[k] for k in ("action", "reason", "gate")}})
        return out

    # $5 hard cap (Chris kept it, t3082s12374): before any Jev call, every decision type, beats a green.
    spent = _cost.spent_cents(dec_path, window)
    est = row["est_cost_cents"] or 0.0
    remaining = cap_cents - spent
    if spent >= cap_cents or est > remaining:
        detail = (f"spent {spent:.4f}c of {cap_cents:.0f}c this {window}" if spent >= cap_cents
                  else f"estimate {est:.2f}c over remaining {remaining:.4f}c ({window})")
        acted = "jev" if gate == "enforce" else "old"
        choice = "ask_chris" if gate == "enforce" else baseline
        out = _out(action="skipped", reason=f"cost_cap: {detail}", choice=choice,
                   jev_choice="ask_chris", acted_on=acted, error="cost_cap")
        _write(choice, choice="ask_chris", acted_on=acted, reason="cost_cap", error=f"cost_cap: {detail}")
        return out

    # precheck key + caller before any Jev call. On a miss: skip, log, no raise.
    miss = "" if caller_present(state) else "precheck:caller_missing"
    if not miss and not key_present():
        miss = "precheck:key_missing"
    if miss:
        if gate == "enforce":
            choice, acted_on = (baseline if dtype == "needs_approval" else "hold"), "precheck_skip"
        else:
            choice, acted_on = baseline, "old"
        out = _out(action="skipped", reason=miss, choice=choice, acted_on=acted_on, error=miss)
        _write(choice=choice, acted_on=acted_on, error=miss, reason=miss)
        return out

    jstate = dict(state)
    jstate["goal"] = goal
    for k in ("cached_artifact", "prior_error", "same_error_count"):
        if k not in jstate and k in ctx:
            jstate[k] = ctx[k]
    fail_choice = "ask_chris" if dtype == "needs_approval" else "hold"
    try:
        out = _jev_route(jstate, cfg, log_path)
    except MissingKeyError:
        _write(error="key_missing", acted_on="fail_closed", choice=fail_choice, reason="key_missing")
        raise
    except Exception as exc:
        timed_out = isinstance(exc, (JevTimeout, TimeoutError)) or "timeout" in type(exc).__name__.lower()
        err = "jev_timeout" if timed_out else f"jev_error:{type(exc).__name__}"
        _write(error=err, acted_on="fail_closed", choice=fail_choice, reason=err.split(":")[0],
               cost_reason="jev_call_failed")
        raise

    meta = out.pop("meta", {}) or {}
    cents, usage, creason = _cost.cost_from_usage(meta.get("usage"), cfg)
    conf = out["details"].get("intent_confidence")
    jev_choice, reason = decide_ex(dtype, green, out["action"], conf, conf_min, floor, registry)

    if gate == "enforce":
        choice, acted_on = jev_choice, "jev"
    else:  # log_only
        choice, acted_on = baseline, "old"
    out.update({"gate": gate, "choice": choice, "jev_choice": jev_choice, "confidence": conf,
                "acted_on": acted_on, "gate_reason": reason, "green_status": gstatus})
    _write(choice, choice=jev_choice, confidence=conf, acted_on=acted_on, reason=reason,
           model=meta.get("model"), usage=usage, cost_cents=cents, cost_reason=creason or "")
    return out


# ---- 2026-11-16 Engineer (Chris green t3082s12374, "retire the second door"): creative_qc ----
# process-gate's QA.md judgment, moved off the retired 127.0.0.1:8787 service. Same question set
# (jev-decision-layer/questions.py), same state ({"review", "facts_from_code"}), same policy
# (jev_decide.policy, non-destructive): conf < 0.5 -> human; pass with blocking >= 0.5 -> human;
# else the verdict choice. Every miss (gate off, empty text, no caller, no key, $5 cap, timeout,
# API error) is decision "human" = BLOCK for human review. gate log_only and enforce act the same
# here (the caller only lets "pass" through).

QC_TEXT_MAX = 3500
QC_FAIL = "human"
QC_PRICE_USD_PER_TOKEN = 0.042 / 1e6  # 8787 estimate rate (jev_decide.PRICE)


class CreativeQCError(Exception):
    """Jev call failed for creative_qc; the row is already written. .out is the CLI payload."""

    def __init__(self, exc: BaseException, err: str, out: dict[str, Any]):
        super().__init__(f"{err} ({type(exc).__name__})")
        self.cause, self.err, self.out = exc, err, out


def qc_code_facts(text: str) -> dict[str, Any]:
    """Math/dates/counts done in code, never asked of Jev (copied from jev_decide.code_facts)."""
    import re
    m = re.search(r"(\d+)/10", text)
    return {"numbered_fixes": len(re.findall(r"(?:^|\s)\d\.\s", text)),
            "timecodes": len(re.findall(r"\d:\d\d", text)),
            "score_10": int(m.group(1)) if m else None}


def qc_questions():
    from typesafe_sdk import Choice, Noul, Score
    return {
        "verdict": Choice(
            instructions="Read this QC review of a video draft. What verdict does the reviewer's content support?",
            criteria={
                "pass": "No remaining defects block shipping; notes are optional polish.",
                "conditional": "Mostly approved; one or two contained fixes remain before shipping.",
                "fail": "Multiple visible defects (frozen shots, mask errors, warping) block shipping.",
            }),
        "blocking": Noul(instructions="Does the review list at least one defect a viewer would clearly notice that must be fixed before shipping?"),
        "severity": Score(
            instructions="How severe are the remaining defects overall?",
            criteria=["Cosmetic only, a viewer would not notice",
                      "Minor polish a careful viewer might notice",
                      "Noticeable defects that hurt quality",
                      "Severe defects; looks broken or amateur"]),
    }


def qc_policy(verdict: str | None, conf: Any, blocking: Any, floor: float = DEFAULT_FLOOR) -> tuple[str, str]:
    if conf is None or float(conf) < floor:
        return QC_FAIL, "low_confidence"
    if verdict == "pass" and blocking is not None and float(blocking) >= 0.5:
        return QC_FAIL, "pass_with_blocking_defect"
    if verdict not in ("pass", "conditional", "fail"):
        return QC_FAIL, "unknown_verdict"
    return verdict, f"jev:{verdict}"


def route_creative_qc(state: dict[str, Any], cfg: dict[str, Any], dec_path) -> dict[str, Any]:
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    text = str(ctx.get("text") if ctx.get("text") is not None else state.get("text") or "")[-QC_TEXT_MAX:]
    item = str(ctx.get("item_id") or state.get("item_id") or "")
    goal = (ctx.get("goal") or state.get("goal") or f"creative_qc verdict for {item or 'item'}")
    goal = str(goal).strip()[:300]
    log_path = resolve_log_path(cfg)
    floor, _ = thresholds(cfg)
    cap_cents, window = _cost.cap_settings(cfg)
    jstate = {"review": text, "facts_from_code": qc_code_facts(text)}

    # Ledger row: no review text in it (input_hash only).
    row = base_row({k: v for k, v in state.items() if k != "context"} | {"context": {
        k: v for k, v in ctx.items() if k != "text"} | {"text_sha256": hashlib.sha256(text.encode()).hexdigest()[:16]}})
    row.update(action="creative_qc", item_id=item, verdict=None, answers=None,
               input_hash=hashlib.sha256(text.encode()).hexdigest()[:12])
    if row["est_cost_cents"] is None:
        row["est_cost_cents"] = round((len(json.dumps(jstate)) / 3.5 + 400) * QC_PRICE_USD_PER_TOKEN * 100, 6)
    gate = gate_mode(cfg, "creative_qc")
    if not cfg.get("enabled", True) or _bypassed({**state, "goal": goal}):
        gate = "off"
    row["mode"] = gate
    t0 = time.monotonic()

    def _done(decision: str, reason: str, acted: str, jev_used: bool = False, **kw) -> dict[str, Any]:
        row.update(choice=decision, reason=reason, acted_on=acted, **kw)
        row["latency_ms"] = int((time.monotonic() - t0) * 1000)
        log_run(dec_path, dict(row))
        log_run(log_path, {"event": "creative_qc", "caller": row["caller"], "goal": goal,
                           "item_id": item, "decision": decision, "reason": reason, "gate": gate})
        return {"decision_type": "creative_qc", "decision": decision, "choice": decision, "reason": reason,
                "verdict": row.get("verdict"), "answers": row.get("answers"), "jev_used": jev_used,
                "gate": gate, "model": row.get("model"), "usage": row.get("usage"),
                "cost_cents": row.get("cost_cents"), "error": row.get("error") or ""}

    if gate == "off":
        return _done(QC_FAIL, "gate_off", "fail_closed")
    if not text.strip():
        return _done(QC_FAIL, "empty_text", "fail_closed", error="empty_text")
    spent = _cost.spent_cents(dec_path, window)
    est = row["est_cost_cents"] or 0.0
    if spent >= cap_cents or est > cap_cents - spent:
        detail = f"spent {spent:.4f}c of {cap_cents:.0f}c this {window}, estimate {est:.4f}c"
        return _done(QC_FAIL, "cost_cap", "fail_closed", error=f"cost_cap: {detail}")
    miss = "" if caller_present(state) else "precheck:caller_missing"
    if not miss and not key_present():
        miss = "precheck:key_missing"
    if miss:
        return _done(QC_FAIL, miss, "precheck_skip", error=miss)

    limits = cfg.get("limits") or {}
    timeout_s = float(limits.get("jev_timeout_s", DEFAULT_TIMEOUT_S))
    try:
        r = system_one(jstate, qc_questions(), model=cfg.get("model") or "jev-latest", timeout_s=timeout_s)
    except Exception as exc:
        timed_out = isinstance(exc, (JevTimeout, TimeoutError)) or "timeout" in type(exc).__name__.lower()
        if isinstance(exc, MissingKeyError):
            err = "key_missing"
        else:
            err = "jev_timeout" if timed_out else f"jev_error:{type(exc).__name__}"
        out = _done(QC_FAIL, err.split(":")[0], "fail_closed", error=err, cost_reason="jev_call_failed")
        raise CreativeQCError(exc, err, out) from exc

    v = r.choices["verdict"]
    conf = None if v.confidence is None else round(float(v.confidence), 4)
    blocking = round(float(r.nouls["blocking"].noul), 4)
    severity = r.scores["severity"].score
    verdict = {"choice": v.choice, "confidence": conf}
    decision, reason = qc_policy(v.choice, conf, blocking, floor)
    cents, usage, creason = _cost.cost_from_usage(getattr(r, "usage", None), cfg)
    return _done(decision, reason, "jev", jev_used=True, confidence=conf, verdict=verdict,
                 answers={"verdict": verdict, "blocking": blocking, "severity": severity},
                 model=getattr(r, "model", None), usage=usage, cost_cents=cents, cost_reason=creason or "")
