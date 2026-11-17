#!/bin/sh
# Resolve a working Python interpreter for the browser-guard probes, then exec it.
#
# Why this exists: /opt/hermes-webui/.venv/bin/python3 is a symlink into a
# uv-managed CPython that `uv` garbage-collects. It went dangling on 2026-11-17,
# so every unit that hardcoded it died with status=203/EXEC. The guard scripts
# only need the standard library, so any working python3 will do; the agent
# runtime is preferred because that is the interpreter the WebUI itself runs on.
set -eu

usage() {
  echo "usage: run-guard.sh [--print] <script.py> [args...]" >&2
  exit 2
}

PRINT_ONLY=0
if [ "${1:-}" = "--print" ]; then
  PRINT_ONLY=1
  shift
fi

[ "$#" -ge 1 ] || usage
SCRIPT="$1"
shift

[ -f "$SCRIPT" ] || { echo "run-guard.sh: no such script: $SCRIPT" >&2; exit 66; }

# First candidate that exists, is executable and actually starts wins.
# A dangling symlink fails `-x`, which is exactly the failure mode we are
# guarding against, so no extra realpath check is needed.
for cand in \
  "${MARS_GUARD_PYTHON:-}" \
  /usr/local/lib/hermes-agent/venv/bin/python \
  /opt/hermes-webui/.venv/bin/python3 \
  /usr/bin/python3 \
  "$(command -v python3 2>/dev/null || true)"
do
  [ -n "$cand" ] || continue
  [ -x "$cand" ] || continue
  if "$cand" -c 'import sys' >/dev/null 2>&1; then
    if [ "$PRINT_ONLY" -eq 1 ]; then
      printf '%s\n' "$cand"
      exit 0
    fi
    exec "$cand" "$SCRIPT" "$@"
  fi
done

echo "run-guard.sh: no working python3 found (tried \$MARS_GUARD_PYTHON, agent runtime, WebUI venv, /usr/bin/python3)" >&2
exit 69
