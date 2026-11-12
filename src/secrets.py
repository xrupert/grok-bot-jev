from __future__ import annotations

import os
import stat
from pathlib import Path

# 2026-11-12 Engineer: same key path as fleet-ops/jev-decision-layer (jev_decide.sh / get_key):
# env var first, else the box keyring (gnome-keyring SecretService) with the session bus pinned,
# so callers whose shells lack DBUS_SESSION_BUS_ADDRESS (e.g. Loop) still find the key.
# Missing key FAILS CLOSED: MissingKeyError -> src.cli exits nonzero. Never print the value.
KEY_NAME = "TYPESAFE_API_KEY"
BUS_SOCKET = Path("/tmp/xdg-runtime-box/bus")


class MissingKeyError(RuntimeError):
    pass


def _is_socket(p: Path) -> bool:
    try:
        return stat.S_ISSOCK(p.stat().st_mode)
    except OSError:
        return False


def _bus_usable(addr: str) -> bool:
    # Only trust an inherited address that points at a live unix socket.
    if not addr.startswith("unix:path="):
        return False
    return _is_socket(Path(addr[len("unix:path="):].split(",", 1)[0]))


def _pin_bus() -> None:
    # 2026-11-12 Engineer: also override a stale/dead inherited bus (Loop's shell had one -> NoKeyringError).
    if _bus_usable(os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")):
        return
    if _is_socket(BUS_SOCKET):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={BUS_SOCKET}"
        os.environ["XDG_RUNTIME_DIR"] = str(BUS_SOCKET.parent)


def _keyring_lookup() -> str:
    import keyring  # imported only after the bus is pinned

    return (keyring.get_password(KEY_NAME, KEY_NAME) or "").strip()


def get_typesafe_api_key() -> str:
    key = os.environ.get(KEY_NAME, "").strip()
    if key:
        return key
    _pin_bus()
    try:
        key = _keyring_lookup()
    except Exception as exc:  # no backend, locked keyring, dbus down
        raise MissingKeyError(
            f"{KEY_NAME} not in env and keyring lookup failed ({type(exc).__name__})"
        ) from None
    if not key:
        raise MissingKeyError(f"{KEY_NAME} not in env or box keyring (service=username={KEY_NAME})")
    return key


def ensure_typesafe_api_key() -> str:
    """Resolve the key and export it in-process for TypeSafeClient. Raises MissingKeyError."""
    key = get_typesafe_api_key()
    os.environ[KEY_NAME] = key
    return key
