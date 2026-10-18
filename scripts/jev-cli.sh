#!/bin/bash
# Wrapper for the Jev router CLI (python -m src.cli): guarantees .venv first.
D="$(cd "$(dirname "$0")/.." && pwd)"
"$D/scripts/ensure_venv.sh" "$D" || { echo "jev-cli: .venv unavailable, see $D/logs/venv-rebuild.log" >&2; exit 3; }
cd "$D" && exec "$D/.venv/bin/python" -m src.cli "$@"
