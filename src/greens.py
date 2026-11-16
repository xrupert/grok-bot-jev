"""Chris green registry (2026-11-16 Engineer, Chris green t3082s12374: "real green check").

A green is valid only if an id in it is recorded in the registry file (config/greens.yaml by
default), either as a recorded green or on the standing-permission list. Free text, placeholders
and ids nobody recorded are not greens.

Registry format: YAML with one top-level key `entries:` (a list, kept LAST in the file) so new
entries can be appended as one line each. Each entry:
  - {id: t3082s12374, kind: green, source: "...", added_by: Primo, added: 2026-11-16T10:19-05:00}
kind is green | standing | revoke. A later revoke entry for the same id disables it. Never edit
or delete earlier lines; append. Add one with:
  .venv/bin/python -m src.cli greens add <id> --source "<where Chris said it>" --by Primo [--standing]
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config" / "greens.yaml"
ET = ZoneInfo("America/New_York")

# Id shapes (all lowercased before matching):
#  - Chris message / card-answer ids: t2070u, t3082s12374, t13s2
#  - ledger-style ids minted when a green is recorded: chris-20261116-0659-t3066s12281-wk10-lineups,
#    chris-t2349u, logan-t4u-20261109-1718
#  - named standing permissions: standing-t2070u-standard-v2, t2070u-standard-v2, breaking-news-standard-v2
MSG_ID = re.compile(r"(?<![a-z0-9])t\d+(?:u|s\d+)(?![a-z0-9])")
SLUG_ID = re.compile(r"(?<![a-z0-9-])(?:chris|logan)-(?:\d{8}|t\d+(?:u|s\d+))(?:-[a-z0-9+]+)*")
STANDING_ID = re.compile(r"(?<![a-z0-9-])(?:standing-t\d+u(?:-[a-z0-9]+)*|t\d+u-standard-v\d+|breaking-news-standard-v\d+)")
ID_FULL = re.compile(r"^(?:t\d+(?:u|s\d+)|(?:chris|logan)-(?:\d{8}|t\d+(?:u|s\d+))(?:-[a-z0-9+]+)*|"
                     r"standing-t\d+u(?:-[a-z0-9]+)*|t\d+u-standard-v\d+|breaking-news-standard-v\d+)$")

NOT_A_GREEN = {"none", "null", "n/a", "na", "no", "false", "0", "pending", "tbd", "unknown", "-", "?", "nil"}
PLACEHOLDER_PREFIXES = ("n/a", "none", "no green", "not needed", "pending", "tbd")


def registry_path(cfg: dict[str, Any] | None = None) -> Path:
    p = os.environ.get("JEV_GREENS_PATH") or ((cfg or {}).get("greens") or {}).get("path")
    if not p:
        return DEFAULT_PATH
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def extract_ids(text: Any) -> list[str]:
    """Every id-shaped token in a green string, lowercased, in order, de-duplicated."""
    if not isinstance(text, str):
        return []
    t = text.lower()
    found: list[str] = []
    for rx in (STANDING_ID, SLUG_ID, MSG_ID):
        for m in rx.finditer(t):
            tok = m.group(0).strip("-+")
            if tok and tok not in found:
                found.append(tok)
    return found


def is_placeholder(green: Any) -> bool:
    """Empty, a placeholder word, or 'n/a (...)'-style: treated as no green at all."""
    if not isinstance(green, str):
        return True
    g = green.strip().lower()
    return len(g) < 3 or g in NOT_A_GREEN or g.startswith(PLACEHOLDER_PREFIXES)


class Registry:
    def __init__(self, entries: Iterable[dict[str, Any]], error: str = ""):
        self.greens: dict[str, dict] = {}
        self.standing: dict[str, dict] = {}
        self.error = error
        for e in entries:
            if not isinstance(e, dict):
                continue
            gid = str(e.get("id") or "").strip().lower()
            if not ID_FULL.match(gid):
                continue  # free text / placeholders never register
            kind = str(e.get("kind") or "green").lower()
            if kind == "revoke":
                self.greens.pop(gid, None); self.standing.pop(gid, None)
            elif kind == "standing":
                self.standing[gid] = e
            else:
                self.greens[gid] = e

    def __contains__(self, gid: str) -> bool:
        return gid in self.greens or gid in self.standing

    def check(self, green: Any) -> tuple[str, str | None]:
        """-> (status, matched_id). status: valid | standing | none | not_recorded | registry_unreadable."""
        if is_placeholder(green):
            return "none", None
        if self.error:
            return "registry_unreadable", None
        for gid in extract_ids(green):
            if gid in self.greens:
                return "valid", gid
            if gid in self.standing:
                return "standing", gid
        return "not_recorded", None


def load(path: Path | None = None, cfg: dict[str, Any] | None = None) -> Registry:
    path = path or registry_path(cfg)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = data.get("entries") or []
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
        return Registry(entries)
    except Exception as exc:  # fail closed: no greens recognised
        return Registry([], error=f"{type(exc).__name__}")


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def append(gid: str, kind: str, source: str, added_by: str, path: Path | None = None) -> dict[str, Any]:
    """Append one entry line. Validates the id shape; never rewrites earlier lines."""
    gid = (gid or "").strip().lower()
    if not ID_FULL.match(gid) or is_placeholder(gid):
        raise ValueError(f"not a green id: {gid!r} (want t123u, t123s456, chris-YYYYMMDD-HHMM-slug, or a standing-... id)")
    if kind not in ("green", "standing", "revoke"):
        raise ValueError("kind must be green, standing or revoke")
    if not (source or "").strip() or not (added_by or "").strip():
        raise ValueError("--source and --by are required")
    path = path or registry_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and "entries:" not in text:
        raise ValueError(f"{path} has no entries: list")
    prefix = "entries:\n" if not text else ("" if text.endswith("\n") else "\n")
    added = datetime.now(ET).isoformat(timespec="minutes")
    line = (f"  - {{id: {_q(gid)}, kind: {kind}, source: {_q(source.strip())}, "
            f"added_by: {_q(added_by.strip())}, added: {_q(added)}}}\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(prefix + line)
    # re-read to prove the file still parses
    reg = load(path)
    if reg.error:
        raise ValueError(f"registry no longer parses after append ({reg.error}); restore from backup")
    return {"id": gid, "kind": kind, "source": source.strip(), "added_by": added_by.strip(), "added": added}
