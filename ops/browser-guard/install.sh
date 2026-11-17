#!/usr/bin/env bash
# Install the Mars test-browser guard systemd units.
# Idempotent: existing units are backed up with a timestamp suffix first.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY_RUN=0

UNITS=(
  mars-testbrowsers.slice
  mars-browser-janitor.service
  mars-browser-janitor.timer
  mars-browser-health.service
  mars-browser-health.timer
)

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: install.sh [--dry-run]"
      exit 0
      ;;
    *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  would: $*"
  else
    "$@"
  fi
}

echo "== Mars browser-guard install =="
echo "source: $HERE"
echo "target: $SYSTEMD_DIR"

# Preflight: refuse to install units that cannot start. A hardcoded venv path is
# exactly what broke on 2026-11-17 (dangling uv symlink -> 203/EXEC), so the
# interpreter is resolved by the same helper the units use.
chmod 0755 "$HERE/run-guard.sh" "$HERE/browser-guard.sh" "$HERE/browser_janitor.py" "$HERE/browser_health.py" 2>/dev/null || true
if ! PY="$("$HERE/run-guard.sh" --print "$HERE/browser_health.py")"; then
  echo "preflight FAILED: no working python3 for the guard scripts" >&2
  echo "set MARS_GUARD_PYTHON=/path/to/python3 and retry" >&2
  exit 1
fi
echo "interpreter: $PY"

for unit in "${UNITS[@]}"; do
  src="$HERE/systemd/$unit"
  dst="$SYSTEMD_DIR/$unit"
  [ -f "$src" ] || { echo "missing $src" >&2; exit 1; }
  if [ -f "$dst" ]; then
    echo "  backup $dst -> $dst.bak-$STAMP"
    run cp -a "$dst" "$dst.bak-$STAMP"
  fi
  echo "  install $unit"
  run install -m 0644 "$src" "$dst"
done

echo "  chmod scripts"
run chmod 0755 "$HERE/browser-guard.sh" "$HERE/run-guard.sh" "$HERE/browser_janitor.py" "$HERE/browser_health.py"

echo "  systemctl daemon-reload"
run systemctl daemon-reload
echo "  start mars-testbrowsers.slice"
run systemctl start mars-testbrowsers.slice
echo "  enable timers"
run systemctl enable --now mars-browser-janitor.timer mars-browser-health.timer

if [ "$DRY_RUN" -eq 0 ]; then
  echo
  echo "== verify =="
  systemctl is-active mars-testbrowsers.slice || true
  systemctl show mars-testbrowsers.slice -p MemoryHigh -p MemoryMax -p MemorySwapMax
  systemctl list-timers 'mars-browser-*' --no-pager || true
fi
echo "done"
