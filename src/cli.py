from __future__ import annotations

import json
import sys

from src.router import GoalError, route_task
from src.secrets import MissingKeyError

# Exit codes (2026-11-12 Engineer): nonzero never means proceed.
EXIT_OK = 0
EXIT_USAGE = 2        # no input, bad JSON, empty/whitespace goal
EXIT_KEY_MISSING = 4  # TYPESAFE_API_KEY not in env or box keyring: FAIL CLOSED
EXIT_JEV_ERROR = 5    # Jev/API error or timeout


def _fail(code: int, error: str, msg: str, state: dict | None = None) -> int:
    dtype = (state or {}).get("decision_type")
    choice = "ask_chris" if dtype == "needs_approval" else "hold"
    print(f"jev-cli: {msg}", file=sys.stderr)
    print(json.dumps({"error": error, "choice": choice, "jev_used": False, "exit_code": code}))
    return code


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python -m src.cli '<json state>'", file=sys.stderr)
        return EXIT_USAGE
    try:
        state = json.loads(argv[0])
        if not isinstance(state, dict):
            raise ValueError("top-level JSON must be an object")
    except ValueError as exc:
        return _fail(EXIT_USAGE, "bad_input", f"invalid JSON input ({exc})")
    try:
        out = route_task(state)
    except GoalError as exc:
        return _fail(EXIT_USAGE, "goal_required", str(exc), state)
    except MissingKeyError as exc:
        return _fail(EXIT_KEY_MISSING, "key_missing",
                     f"{exc}. Failing closed; do not treat as proceed. "
                     "Fix: export TYPESAFE_API_KEY or restore it to the box keyring.", state)
    except Exception as exc:  # API error, timeout
        return _fail(EXIT_JEV_ERROR, "jev_error", f"Jev call failed ({type(exc).__name__})", state)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
