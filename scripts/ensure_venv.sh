#!/bin/bash
# ensure_venv.sh -- self-rebuild guard for this project's .venv.
# Why: the box's store sync never snapshots dirs named .venv/ or venv/ (platform default
# ignore list in sand-host box-store-sync), so every move to a fresh box instance comes
# back WITHOUT .venv. This recreates it from requirements.txt when .venv/bin/python is
# missing or can't import the needed modules. Cheap no-op (one python -c) when healthy.
# Usage: ensure_venv.sh [project_dir]   exit 0 = venv ready, 1 = rebuild failed (see log)
set -u
D="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
MODS="${ENSURE_VENV_MODULES:-typesafe_sdk yaml}"          # space-separated import names
PY="$D/.venv/bin/python"; REQ="$D/requirements.txt"
LOG="$D/logs/venv-rebuild.log"; LOCK="/tmp/venv-rebuild-$(printf %s "$D" | md5sum | cut -c1-12).lock"
imports="import $(echo $MODS | tr ' ' ',')"
ok() { [ -x "$PY" ] && timeout 60 "$PY" -c "$imports" >/dev/null 2>&1; }
ok && exit 0
mkdir -p "$D/logs"
log() { echo "$(date -Is) [$$] $*" >>"$LOG"; }
exec 8>"$LOCK"
if ! flock -w 900 8; then log "ERROR: timed out waiting for rebuild lock $LOCK"; exit 1; fi
ok && { log "venv already rebuilt by another process; nothing to do"; exit 0; }   # lost the race, fine
if [ -x "$PY" ]; then why="imports failed ($imports)"; elif [ -e "$D/.venv" ]; then why=".venv present but bin/python missing/broken"; else why=".venv missing"; fi
log "REBUILD start: $why; dir=$D req=$REQ host_python=$(/usr/bin/python3 -V 2>&1)"
[ -f "$REQ" ] || { log "ERROR: $REQ not found"; exit 1; }
t0=$(date +%s)
if [ -e "$D/.venv" ]; then old="$D/.venv.broken-$(date +%Y%m%d-%H%M%S)"; mv "$D/.venv" "$old"; log "moved old .venv aside to $old"; fi
if /usr/bin/python3 -m venv "$D/.venv" >>"$LOG" 2>&1 \
   && timeout 900 "$PY" -m pip install -q --disable-pip-version-check -r "$REQ" >>"$LOG" 2>&1 \
   && ok; then
  log "REBUILD ok in $(( $(date +%s) - t0 ))s: $("$PY" -m pip freeze 2>/dev/null | grep -iE '^(typesafe-sdk|pyyaml)==' | tr '\n' ' ')"
  [ -n "${old:-}" ] && rm -rf "$old"
  exit 0
fi
log "REBUILD FAILED after $(( $(date +%s) - t0 ))s (see pip output above)"
exit 1
