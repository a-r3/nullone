#!/usr/bin/env bash
# NullOne disposable offline validation (issue #8).
#
# Uses an isolated temp directory only. Never touches ~/.openclaw,
# never uses production state, never calls network/Zernio/Telegram/models.
# Containerization is NOT required.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/nullone-offline-XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
export TMPDIR="$TMPD"

echo "== NullOne disposable offline validation =="
echo "root: $ROOT"
echo "tmp:  $TMPD"
echo

echo "-- runtime versions (capture only) --"
python3 "$ROOT/scripts/capture-runtime-versions.py"
echo

echo "-- inventory contract tests --"
python3 "$ROOT/tests/test_runtime_inventory.py" 2>&1 | tail -n 3
echo

echo "-- offline render reproducibility --"
python3 "$ROOT/tests/test_offline_render_reproducibility.py" 2>&1 | tail -n 3
echo

echo "-- full offline regression suite --"
python3 "$ROOT/tests/run_offline.py" 2>&1 | tail -n 2
echo

echo "DISPOSABLE_OFFLINE_VALIDATION=PASS"
