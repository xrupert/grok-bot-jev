"""Jev spend tracking and the $5 hard cap (2026-11-16 Engineer, Chris green t3082s12374).

Spend in the window = sum of cost_cents (what each Jev call cost, from r.usage) plus
committed_cents (the estimate a bot passed for a step the gate let through) over decisions.jsonl
rows whose ts falls in the window. Window default: the calendar month in America/New_York.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_CAP_CENTS = 500.0
DEFAULT_WINDOW = "calendar_month_et"
# Price per 1M tokens in USD. 0.042 input is the rate the 8787 service used (jev_decide.PRICE);
# TypeSafe returns token counts only, never a price, so this is an estimate. Config: cost.usd_per_mtok_*.
DEFAULT_USD_PER_MTOK_IN = 0.042
DEFAULT_USD_PER_MTOK_OUT = 0.0


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def window_start(window: str, now: datetime | None = None) -> datetime | None:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    if window in ("calendar_month_et", "month"):
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if window == "rolling_30d":
        return now - timedelta(days=30)
    if window == "all":
        return None
    raise ValueError(f"unknown cost window {window!r}")


def spent_cents(dec_path: Path, window: str = DEFAULT_WINDOW, now: datetime | None = None) -> float:
    start = window_start(window, now)
    total = 0.0
    if not dec_path.exists():
        return 0.0
    for line in dec_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("event") == "correction":
            continue
        if start is not None:
            t = parse_ts(r.get("ts"))
            if t is None or t < start:
                continue
        for k in ("cost_cents", "committed_cents"):
            n = _num(r.get(k))
            if n and n > 0:
                total += n
    return total


def request_estimate_cents(state: dict[str, Any]) -> float | None:
    """The larger of the call's est_cost_cents and budget_cents_cap (top level or context)."""
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    vals = [_num(src.get(k)) for src in (state, ctx) for k in ("est_cost_cents", "budget_cents_cap")]
    vals = [v for v in vals if v is not None and v >= 0]
    return max(vals) if vals else None


def cost_from_usage(usage: Any, cfg: dict[str, Any]) -> tuple[float | None, dict | None, str]:
    """-> (cost_cents, usage_dict, reason). reason is '' when cost was computed."""
    if usage is None:
        return None, None, "no_usage_from_api"
    def g(name):
        v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    tin, tout = g("input_tokens"), g("output_tokens")
    u = {"input_tokens": tin, "output_tokens": tout}
    if tin is None and tout is None:
        return None, u, "usage_has_no_token_counts"
    c = (cfg.get("cost") or {})
    rin = float(c.get("usd_per_mtok_input", DEFAULT_USD_PER_MTOK_IN))
    rout = float(c.get("usd_per_mtok_output", DEFAULT_USD_PER_MTOK_OUT))
    usd = (tin or 0) * rin / 1e6 + (tout or 0) * rout / 1e6
    return round(usd * 100, 6), u, ""


def cap_settings(cfg: dict[str, Any]) -> tuple[float, str]:
    c = cfg.get("cost") or {}
    return float(c.get("cap_cents", DEFAULT_CAP_CENTS)), str(c.get("window", DEFAULT_WINDOW))
