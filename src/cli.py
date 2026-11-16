from __future__ import annotations

import json
import sys

from src.router import CreativeQCError, GoalError, route_task, write_error_row
from src.secrets import MissingKeyError

# Exit codes (2026-11-12 Engineer): nonzero never means proceed.
EXIT_OK = 0
EXIT_USAGE = 2        # no input, bad JSON, empty/whitespace goal
EXIT_KEY_MISSING = 4  # TYPESAFE_API_KEY not in env or box keyring: FAIL CLOSED
EXIT_JEV_ERROR = 5    # Jev/API error or timeout

GREENS_USAGE = """Usage:
  python -m src.cli greens add <id> --source "<where Chris said it>" --by <bot> [--standing]
  python -m src.cli greens revoke <id> --source "<why>" --by <bot>
  python -m src.cli greens check "<green text>"
Ids: t123u, t123s456, chris-YYYYMMDD-HHMM-slug, logan-..., standing-t123u-..., t123u-standard-vN.
Appends one line to config/greens.yaml (append-only). Free text and placeholders are rejected."""


def _fail(code: int, error: str, msg: str, state: dict | None = None) -> int:
    dtype = (state or {}).get("decision_type")
    choice = {"needs_approval": "ask_chris", "creative_qc": "human"}.get(dtype, "hold")
    print(f"jev-cli: {msg}", file=sys.stderr)
    payload = {"error": error, "choice": choice, "jev_used": False, "exit_code": code}
    if dtype == "creative_qc":
        payload.update(decision="human", reason=error.split(":")[0], decision_type="creative_qc")
    print(json.dumps(payload))
    return code


def _opt(args: list[str], name: str) -> str:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return ""


def greens_main(args: list[str]) -> int:
    """2026-11-16 Engineer: append-only way for Primo to record a Chris green."""
    from src import greens
    if not args or args[0] not in ("add", "revoke", "check") or len(args) < 2:
        print(GREENS_USAGE, file=sys.stderr)
        return EXIT_USAGE
    cmd, gid = args[0], args[1]
    if cmd == "check":
        status, matched = greens.load().check(gid)
        print(json.dumps({"green": gid, "status": status, "matched_id": matched,
                          "valid": status in ("valid", "standing")}))
        return EXIT_OK
    kind = "revoke" if cmd == "revoke" else ("standing" if "--standing" in args else "green")
    try:
        entry = greens.append(gid, kind, _opt(args, "--source"), _opt(args, "--by"))
    except ValueError as exc:
        print(f"jev-cli greens: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps({"appended": entry, "path": str(greens.registry_path())}))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python -m src.cli '<json state>'   |   python -m src.cli greens ...", file=sys.stderr)
        return EXIT_USAGE
    if argv[0] == "greens":
        return greens_main(argv[1:])
    try:
        state = json.loads(argv[0])
        if not isinstance(state, dict):
            raise ValueError("top-level JSON must be an object")
    except ValueError as exc:
        write_error_row({}, "bad_input", "hold")
        return _fail(EXIT_USAGE, "bad_input", f"invalid JSON input ({exc})")
    try:
        out = route_task(state)
    except GoalError as exc:
        return _fail(EXIT_USAGE, "goal_required", str(exc), state)
    except CreativeQCError as exc:  # row already written; decision human (fail closed)
        code = EXIT_KEY_MISSING if exc.err == "key_missing" else EXIT_JEV_ERROR
        print(f"jev-cli: creative_qc Jev call failed ({type(exc.cause).__name__}); decision human", file=sys.stderr)
        print(json.dumps(exc.out | {"exit_code": code}, default=str))
        return code
    except MissingKeyError as exc:
        return _fail(EXIT_KEY_MISSING, "key_missing",
                     f"{exc}. Failing closed; do not treat as proceed. "
                     "Fix: export TYPESAFE_API_KEY or restore it to the box keyring.", state)
    except Exception as exc:  # API error, timeout
        err = "jev_timeout" if "timeout" in type(exc).__name__.lower() else "jev_error"
        return _fail(EXIT_JEV_ERROR, err, f"Jev call failed ({type(exc).__name__})", state)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
