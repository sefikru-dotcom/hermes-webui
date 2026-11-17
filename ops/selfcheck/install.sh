#!/usr/bin/env bash
# Install the versioned mars-selfcheck plugin and its bounded mars-test runner.
#
# /root/.hermes/plugins/mars-selfcheck/ and /root/tools/mars-test.py used to be
# unversioned, root-owned copies with no upstream repo: the runner copied the
# whole workspace (2.3 GB) into tmpfs on every write and left the half-copy
# behind when the 35 s timeout killed it. This installer makes ops/selfcheck the
# source of truth: it backs up the live files, installs the repo copies and
# re-points /usr/local/bin/mars-test.
#
# Nothing here runs on its own. --dry-run prints the plan and runs the same
# checks without touching a single live path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_SRC="$HERE/mars-test.py"
PLUGIN_SRC="$HERE/plugin"

RUNNER_TARGET="${MARS_SELFCHECK_TARGET:-/root/tools/mars-test.py}"
LINK="${MARS_SELFCHECK_LINK:-/usr/local/bin/mars-test}"
PLUGIN_DIR="${MARS_SELFCHECK_PLUGIN_DIR:-/root/.hermes/plugins/mars-selfcheck}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: install.sh [--dry-run]"
      echo "  env: MARS_SELFCHECK_TARGET      (default $RUNNER_TARGET)"
      echo "       MARS_SELFCHECK_LINK        (default $LINK)"
      echo "       MARS_SELFCHECK_PLUGIN_DIR  (default $PLUGIN_DIR)"
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

echo "== mars-selfcheck install =="
echo "runner:  $RUNNER_SRC -> $RUNNER_TARGET"
echo "plugin:  $PLUGIN_SRC -> $PLUGIN_DIR"
echo "link:    $LINK"
echo "dry-run: $DRY_RUN"

[ -f "$RUNNER_SRC" ] || { echo "missing $RUNNER_SRC" >&2; exit 1; }
[ -f "$PLUGIN_SRC/__init__.py" ] || { echo "missing $PLUGIN_SRC/__init__.py" >&2; exit 1; }
[ -f "$PLUGIN_SRC/plugin.yaml" ] || { echo "missing $PLUGIN_SRC/plugin.yaml" >&2; exit 1; }

# Interpreter for the checks: the agent runtime is what the runner itself wants.
PY=""
for cand in /usr/local/lib/hermes-agent/venv/bin/python /usr/bin/python3; do
  if [ -x "$cand" ] && "$cand" -c 'import sys' 2>/dev/null; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "no working python3 for checks" >&2; exit 1; }
echo "checker: $PY"

echo "  syntax check (runner + plugin)"
"$PY" - "$RUNNER_SRC" "$PLUGIN_SRC/__init__.py" <<'PY'
import sys

for src in sys.argv[1:]:
    with open(src, encoding="utf-8") as fh:
        compile(fh.read(), src, "exec")
    print(f"  syntax OK: {src}")
PY

echo "  bounds self-check (tiny fixture, nothing copied)"
TMP_CHECK="$(mktemp -d /var/tmp/mars-selfcheck-check-XXXXXX)"
trap 'rm -rf "$TMP_CHECK"' EXIT
mkdir -p "$TMP_CHECK/project/.git" "$TMP_CHECK/project/tests" "$TMP_CHECK/scratch/tests"
printf 'def test_ok():\n    assert True\n' >"$TMP_CHECK/project/tests/test_ok.py"
printf 'x = 1\n' >"$TMP_CHECK/scratch/tests/test_any.py"

rc=0
plan="$("$PY" "$RUNNER_SRC" --print-plan --dir "$TMP_CHECK/project" 2>/dev/null)" || rc=$?
[ "$rc" -eq 0 ] || { echo "  ERROR: valid project rejected (exit $rc)" >&2; exit 1; }
case "$plan" in
  *'"code": 0'*) echo "  valid project: plan OK" ;;
  *) echo "  ERROR: unexpected plan: $plan" >&2; exit 1 ;;
esac

rc=0
"$PY" "$RUNNER_SRC" --print-plan --dir "$TMP_CHECK/scratch" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 4 ] || { echo "  ERROR: markerless scratch dir not refused (exit $rc)" >&2; exit 1; }
echo "  scratch dir without project marker: refused (exit 4)"

rc=0
"$PY" "$RUNNER_SRC" --print-plan --dir "$TMP_CHECK/project" --max-bytes 10 >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 3 ] || { echo "  ERROR: oversize not refused before copy (exit $rc)" >&2; exit 1; }
echo "  oversize budget: refused before copy (exit 3)"

echo "  plugin import + workspace guard"
"$PY" - "$PLUGIN_SRC" "$TMP_CHECK/scratch/tests/test_any.py" <<'PY'
import importlib.util
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "mars_selfcheck_plugin", sys.argv[1] + "/__init__.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert callable(mod.register), "register() missing"
assert mod._find_project_root(__import__("pathlib").Path(sys.argv[2]).parent) is None, (
    "markerless scratch dir must not resolve to a project root"
)
print("  plugin import OK; markerless tree ignored")
PY

if [ -e "$RUNNER_TARGET" ]; then
  echo "  backup $RUNNER_TARGET -> $RUNNER_TARGET.bak-$STAMP"
  run cp -a "$RUNNER_TARGET" "$RUNNER_TARGET.bak-$STAMP"
fi
if [ -e "$PLUGIN_DIR" ]; then
  echo "  backup $PLUGIN_DIR -> $PLUGIN_DIR.bak-$STAMP"
  run cp -a "$PLUGIN_DIR" "$PLUGIN_DIR.bak-$STAMP"
fi

echo "  install mars-test.py"
run install -d -m 0755 "$(dirname "$RUNNER_TARGET")"
run install -m 0755 "$RUNNER_SRC" "$RUNNER_TARGET"
echo "  relink $LINK"
run ln -sfn "$RUNNER_TARGET" "$LINK"

echo "  install plugin"
run install -d -m 0755 "$PLUGIN_DIR"
run install -m 0644 "$PLUGIN_SRC/__init__.py" "$PLUGIN_DIR/__init__.py"
run install -m 0644 "$PLUGIN_SRC/plugin.yaml" "$PLUGIN_DIR/plugin.yaml"
if [ "$DRY_RUN" -eq 0 ]; then
  # Stale bytecode of the old plugin would shadow nothing, but it confuses audits.
  rm -rf "$PLUGIN_DIR/__pycache__"
fi

if [ "$DRY_RUN" -eq 0 ]; then
  echo
  echo "== verify =="
  ls -l "$RUNNER_TARGET" "$LINK" "$PLUGIN_DIR/__init__.py" "$PLUGIN_DIR/plugin.yaml"
  out="$("$LINK" --print-plan --dir "$TMP_CHECK/project" 2>&1)" || {
    echo "  smoke FAILED: $out" >&2; exit 1; }
  case "$out" in
    *'"code": 0'*) echo "  smoke OK: installed runner plans a valid project" ;;
    *) echo "  smoke FAILED: unexpected output: $out" >&2; exit 1 ;;
  esac
  "$PY" - "$PLUGIN_DIR" <<'PY'
import importlib.util
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("installed_plugin", sys.argv[1] + "/__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(f"  installed plugin OK (deny roots: {[str(p) for p in mod._deny_roots()]})")
PY
fi
echo "done"
