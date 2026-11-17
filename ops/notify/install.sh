#!/usr/bin/env bash
# Install the versioned mars-notify.py onto the host.
#
# /root/tools/mars-notify.py was an unversioned, root-owned file with no upstream
# repo, so every interpreter fix there was invisible to review and easy to lose.
# This installer makes ops/notify/mars-notify.py the source of truth: it backs up
# the live file, installs the repo copy, and re-points /usr/local/bin/mars-notify.
#
# Nothing here runs on its own. --dry-run shows the plan and runs the same checks.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/mars-notify.py"
TARGET="${MARS_NOTIFY_TARGET:-/root/tools/mars-notify.py}"
LINK="${MARS_NOTIFY_LINK:-/usr/local/bin/mars-notify}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: install.sh [--dry-run]"
      echo "  env: MARS_NOTIFY_TARGET (default $TARGET)"
      echo "       MARS_NOTIFY_LINK   (default $LINK)"
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

echo "== mars-notify install =="
echo "source: $SRC"
echo "target: $TARGET"
echo "link:   $LINK"

[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

# Interpreter for the checks: the agent runtime is what the script itself wants.
PY=""
for cand in /usr/local/lib/hermes-agent/venv/bin/python /usr/bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "no working python3 for checks" >&2; exit 1; }
echo "checker: $PY"

echo "  syntax check"
"$PY" - "$SRC" <<'PY'
import sys

src = sys.argv[1]
with open(src, encoding="utf-8") as fh:
    compile(fh.read(), src, "exec")
print("  syntax OK")
PY

echo "  interpreter resolution (import, no re-exec, no DB writes)"
"$PY" - "$SRC" <<'PY'
import importlib.util
import os
import sys

sys.dont_write_bytecode = True  # keep the checkout clean
spec = importlib.util.spec_from_file_location("mars_notify", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

push_py = mod._push_python()
print(f"  push python: {push_py}")
print(f"  push PYTHONPATH: {mod.PUSH_PYTHONPATH}")
missing = [p for p in mod.PUSH_PYTHONPATH if not os.path.isdir(p)]
if missing:
    print(f"  ERROR: missing PYTHONPATH entries: {missing}", file=sys.stderr)
    raise SystemExit(1)
if not os.access(push_py, os.X_OK):
    print(f"  ERROR: not executable: {push_py}", file=sys.stderr)
    raise SystemExit(1)
PY

if [ -f "$TARGET" ]; then
  echo "  backup $TARGET -> $TARGET.bak-$STAMP"
  run cp -a "$TARGET" "$TARGET.bak-$STAMP"
fi
echo "  install mars-notify.py"
run install -m 0755 "$SRC" "$TARGET"
echo "  relink $LINK"
run ln -sfn "$TARGET" "$LINK"

if [ "$DRY_RUN" -eq 0 ]; then
  echo
  echo "== verify =="
  ls -l "$TARGET" "$LINK"
  # Empty stdin makes the script exit 2 ("нечего доставлять") before touching
  # state.db — a smoke test that cannot write history.
  out="$(printf '' | "$TARGET" 2>&1 || true)"
  echo "  smoke: $out"
  case "$out" in
    *"нечего доставлять"*) echo "  smoke OK" ;;
    *) echo "  smoke FAILED: expected 'пусто: нечего доставлять'" >&2; exit 1 ;;
  esac
fi
echo "done"
