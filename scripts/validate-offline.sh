#!/usr/bin/env bash
# NullOne LOCAL offline validation (issue #8).
#
# LOCAL_OFFLINE_VALIDATION (this script):
# - no network calls by NullOne test logic
# - no ~/.openclaw
# - no production state
# - temp output directories
# - uses the already-installed declared runtime dependencies
#
# This script does NOT create an isolated Python environment and does NOT
# install packages. It reuses the host interpreter, site-packages, and
# fonts, and fails clearly when the installed Pillow does not match the
# reviewed runtime manifest.
#
# CLEAN_ENVIRONMENT_VALIDATION is provided separately by CI's fresh
# GitHub runner, which installs dependencies FROM the reviewed
# requirements manifests before running the same offline suite.
# Containerization is NOT required.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/nullone-offline-XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
export TMPDIR="$TMPD"

echo "== NullOne LOCAL offline validation =="
echo "root: $ROOT"
echo "tmp:  $TMPD"
echo "NOTE: host interpreter/site-packages/fonts reused; no isolated env."
echo

echo "-- dependency compatibility (installed vs reviewed manifest) --"
python3 - <<'EOF'
import re
from pathlib import Path
pins = {}
for line in Path("requirements-runtime.txt").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "==" in line:
        name, _, version = line.partition("==")
        pins[name.strip().lower()] = version.strip()
expected = pins.get("pillow")
assert expected, "Pillow pin missing from requirements-runtime.txt"
import PIL
assert PIL.__version__ == expected, (
    f"installed Pillow {PIL.__version__} != reviewed manifest pin {expected}; "
    "install the reviewed requirements-runtime.txt")
print(f"INSTALLED_PILLOW_PIN_VERIFIED={expected}")
EOF
echo

echo "-- runtime versions (capture only) --"
python3 "$ROOT/scripts/capture-runtime-versions.py" > "$TMPD/versions.json"
python3 -c "import json; d=json.load(open('$TMPD/versions.json')); print('fonts:', {k: (v['exists'], (v['sha256'] or '')[:12]) for k, v in d['fonts'].items()})"
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

echo "LOCAL_OFFLINE_VALIDATION=PASS"
