#!/usr/bin/env bash
# Mars test-browser guard: launch a headless/automation browser in its own
# cgroup slice, with a hard wall-clock limit and guaranteed cleanup.
#
# Why: on 2026-11-15 a headless Chromium launched from an agent chat session
# stayed in /system.slice/hermes-webui.service, ate the whole swap and pushed
# memory PSI to 95%, taking the WebUI down. A browser started through this
# wrapper cannot do that: it is charged to mars-testbrowsers.slice (own
# MemoryMax, no swap), it is killed when the deadline passes even if this
# script itself is SIGKILLed (systemd transient timer), and its temp profile is
# removed on the way out.
#
# Usage:
#   browser-guard.sh --name kv-pop --timeout 600 -- [chrome args...]
#   browser-guard.sh --name panel --timeout 900 --bin /snap/bin/chromium -- \
#       --headless=new --remote-debugging-port=0 about:blank
#
# Exit codes: browser's own exit code, 124 on timeout, 125 on setup failure.
set -uo pipefail

NAME="test"
TIMEOUT=600
MEMORY="1500M"
SLICE="${GUARD_SLICE:-mars-testbrowsers.slice}"
KEEP_PROFILE=0
DRY_RUN=0
BIN="${AGENT_BROWSER_EXECUTABLE_PATH:-google-chrome}"
LOG_DIR="${GUARD_LOG_DIR:-/var/log/mars-guard}"
RUN_DIR="${GUARD_RUN_DIR:-/run/mars-guard}"

usage() {
  cat <<'EOF'
Usage: browser-guard.sh [options] -- [browser args...]

  --name NAME        label for the unit/profile (default: test)
  --timeout SEC      hard wall-clock limit, enforced by systemd (default: 600)
  --memory MAX       cgroup memory cap for the browser (default: 1500M)
  --bin PATH         browser binary (default: $AGENT_BROWSER_EXECUTABLE_PATH)
  --keep-profile     do not delete the temp profile on exit
  --dry-run          print the plan, launch nothing
  -h, --help         this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    --memory) MEMORY="${2:?--memory needs a value}"; shift 2 ;;
    --bin) BIN="${2:?--bin needs a value}"; shift 2 ;;
    --keep-profile) KEEP_PROFILE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "browser-guard: unknown option $1" >&2; usage >&2; exit 125 ;;
    *) break ;;
  esac
done

case "$TIMEOUT" in
  ''|*[!0-9]*) echo "browser-guard: --timeout must be an integer" >&2; exit 125 ;;
esac
if [ "$TIMEOUT" -lt 10 ]; then
  echo "browser-guard: --timeout must be >= 10s" >&2
  exit 125
fi

SAFE_NAME="$(printf '%s' "$NAME" | tr -c 'a-zA-Z0-9_.-' '-' | cut -c1-24)"
UNIT="mars-tb-${SAFE_NAME}-$$"
mkdir -p "$LOG_DIR" "$RUN_DIR" 2>/dev/null || true

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) guard name=$NAME unit=$UNIT $*"
  printf '%s\n' "$line" >&2
  printf '%s\n' "$line" >>"$LOG_DIR/guard.log" 2>/dev/null || true
}

OWN_PROFILE=""
HAS_PROFILE=0
for arg in "$@"; do
  case "$arg" in
    --user-data-dir=*) HAS_PROFILE=1 ;;
  esac
done

if [ "$HAS_PROFILE" -eq 0 ]; then
  OWN_PROFILE="$(mktemp -d "/tmp/guard-${SAFE_NAME}-XXXXXX")" || {
    echo "browser-guard: cannot create temp profile" >&2
    exit 125
  }
  set -- --user-data-dir="$OWN_PROFILE" "$@"
fi

PROFILE_PATH=""
for arg in "$@"; do
  case "$arg" in
    --user-data-dir=*) PROFILE_PATH="${arg#--user-data-dir=}" ;;
  esac
done

if [ "$#" -eq 0 ]; then
  set -- about:blank
fi

if [ ! -x "$BIN" ] && ! command -v "$BIN" >/dev/null 2>&1; then
  echo "browser-guard: browser binary not found: $BIN" >&2
  [ -n "$OWN_PROFILE" ] && rm -rf "$OWN_PROFILE"
  exit 125
fi

DEADMAN="${UNIT}-deadman"
RC=0
CHILD=""
WATCHDOG=""

cleanup() {
  local rc=$?
  if [ "$DRY_RUN" -eq 0 ]; then
    # The .scope suffix is required: `systemctl kill NAME` resolves NAME as
    # NAME.service and fails with 5/NOTINSTALLED, leaving the browser running.
    systemctl kill -s SIGKILL --kill-whom=all "${UNIT}.scope" >/dev/null 2>&1 || true
    systemctl stop --no-block "${UNIT}.scope" >/dev/null 2>&1 || true
    systemctl stop "${DEADMAN}.timer" >/dev/null 2>&1 || true
    [ -n "$WATCHDOG" ] && kill "$WATCHDOG" >/dev/null 2>&1 || true
    [ -n "$CHILD" ] && kill -KILL "$CHILD" >/dev/null 2>&1 || true
  fi
  if [ -n "$OWN_PROFILE" ] && [ "$KEEP_PROFILE" -eq 0 ]; then
    rm -rf "$OWN_PROFILE"
  fi
  log "end rc=$rc"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Register this run so the janitor leaves it alone while it is legitimate.
if [ -n "$PROFILE_PATH" ] && [ "$DRY_RUN" -eq 0 ]; then
  printf '{"profile":"%s","unit":"%s","expires":%s}\n' \
    "$PROFILE_PATH" "$UNIT" "$(( $(date +%s) + TIMEOUT + 60 ))" \
    >>"$RUN_DIR/protected.jsonl" 2>/dev/null || true
fi

log "start bin=$BIN timeout=${TIMEOUT}s memory=$MEMORY slice=$SLICE profile=${PROFILE_PATH:-none}"
START_EPOCH=$(date +%s)

if [ "$DRY_RUN" -eq 1 ]; then
  echo "browser-guard: would run unit=$UNIT slice=$SLICE memory=$MEMORY timeout=${TIMEOUT}s" >&2
  echo "browser-guard: $BIN $*" >&2
  RC=0
  exit 0
fi

if command -v systemd-run >/dev/null 2>&1 && [ "${GUARD_NO_SYSTEMD:-0}" != "1" ]; then
  # Hard deadline that survives this script being killed: a transient timer
  # SIGKILLs the whole scope, whatever state the launcher is in. The .scope
  # suffix is mandatory — without it systemctl looks for a .service unit.
  # --no-block on stop: a blocking stop waits ~15s for the cgroup to drain,
  # which would stretch every deadline by that much.
  DEADMAN_CMD="systemctl kill -s SIGKILL --kill-whom=all '${UNIT}.scope' >/dev/null 2>&1; systemctl stop --no-block '${UNIT}.scope' >/dev/null 2>&1"
  if ! systemd-run --quiet --collect --unit="$DEADMAN" --on-active="${TIMEOUT}s" \
      /bin/sh -c "$DEADMAN_CMD" >/dev/null 2>&1; then
    log "warn: deadman timer not armed (timeout will not be enforced by systemd)"
  fi

  systemd-run --scope --quiet --collect --unit="$UNIT" --slice="$SLICE" \
    -p MemoryMax="$MEMORY" -p MemorySwapMax=0 -p TasksMax=1024 \
    -p Description="Mars test browser ($NAME)" \
    -- "$BIN" "$@"
  RC=$?
  ELAPSED=$(( $(date +%s) - START_EPOCH ))
else
  # Fallback for hosts without systemd: own session + watchdog subshell.
  setsid "$BIN" "$@" &
  CHILD=$!
  ( sleep "$TIMEOUT"; kill -KILL "-$CHILD" >/dev/null 2>&1; kill -KILL "$CHILD" >/dev/null 2>&1 ) &
  WATCHDOG=$!
  wait "$CHILD"
  RC=$?
  ELAPSED=$(( $(date +%s) - START_EPOCH ))
  kill "$WATCHDOG" >/dev/null 2>&1 || true
  WATCHDOG=""
fi

# Any non-zero exit at or past the deadline is a timeout, whatever signal the
# browser ended up dying from (SIGKILL, or SIGABRT from its own shutdown path).
if [ "$RC" -ne 0 ] && [ "${ELAPSED:-0}" -ge "$TIMEOUT" ]; then
  log "killed by deadline after ${ELAPSED}s (raw rc=$RC)"
  RC=124
fi
exit "$RC"
