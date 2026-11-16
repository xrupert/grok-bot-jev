from __future__ import annotations

import threading
from typing import Any

from typesafe_sdk import TypeSafeClient

from src.secrets import ensure_typesafe_api_key

DEFAULT_TIMEOUT_S = 3.0


class JevTimeout(TimeoutError):
    """The Jev call did not finish inside the wall-clock limit (2026-11-16: 3 s)."""


def system_one(state: dict[str, Any], questions: dict[str, Any], model: str,
               timeout_s: float = DEFAULT_TIMEOUT_S) -> Any:
    """One batched system_one call (all questions in one request). HTTP timeout = timeout_s,
    no SDK retries, and a hard wall-clock limit of timeout_s around the whole call."""
    ensure_typesafe_api_key()
    from typesafe_sdk import RetryPolicy
    box: dict[str, Any] = {}

    def run():
        try:
            with TypeSafeClient(model=model, timeout=timeout_s, retry=RetryPolicy(max_retries=0)) as client:
                box["r"] = client.system_one(state=state, questions=questions)
        except BaseException as exc:  # surfaced to the caller below
            box["e"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise JevTimeout(f"Jev call exceeded {timeout_s:g}s")
    if "e" in box:
        raise box["e"]
    return box["r"]
