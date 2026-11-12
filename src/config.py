from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"

GATE_MODES = ("off", "log_only", "enforce")
DEFAULT_GATE = "log_only"


def normalize_gate(value: Any) -> str:
    """gate: off | log_only | enforce. YAML 1.1 reads a bare `off` as False, so map that.
    Anything unknown falls back to log_only (never silently to enforce)."""
    if value is False:
        return "off"
    if isinstance(value, str) and value.strip().lower() in GATE_MODES:
        return value.strip().lower()
    return DEFAULT_GATE


def gate_mode(cfg: dict[str, Any], decision_type: str) -> str:
    per_type = cfg.get("gate_types") or {}
    if decision_type in per_type:
        return normalize_gate(per_type[decision_type])
    return normalize_gate(cfg.get("gate", DEFAULT_GATE))


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("enabled", True)
    data.setdefault("mode", "shadow")
    data.setdefault("model", "jev-latest")
    data.setdefault("thresholds", {})
    data.setdefault("limits", {})
    data.setdefault("logging", {"path": "logs/runs.jsonl"})
    data.setdefault("gate", DEFAULT_GATE)
    data.setdefault("gate_types", {})
    return data


def _resolve(rel: str) -> Path:
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_log_path(cfg: dict[str, Any]) -> Path:
    return _resolve(cfg.get("logging", {}).get("path", "logs/runs.jsonl"))


def resolve_decisions_path(cfg: dict[str, Any]) -> Path:
    return _resolve(cfg.get("logging", {}).get("decisions_path", "logs/decisions.jsonl"))
