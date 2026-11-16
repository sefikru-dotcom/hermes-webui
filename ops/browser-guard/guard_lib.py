"""Shared helpers for the Mars test-browser guard.

Read-only /proc scanning used by both the janitor and the health probe so that
"what counts as a test browser" is defined in exactly one place.

Nothing here writes to the system.
"""
from __future__ import annotations

import os
import time

# Binaries that count as a browser. Snap chromium runs as "chrome" too.
BROWSER_BASENAMES = {
    "chrome",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "headless_shell",
    "chrome-headless-shell",
}

# A browser process is considered "in the leak zone" only when it runs inside
# one of these cgroups. mars-browser.service (the persistent CDP browser used by
# Hermes browser tools) is deliberately NOT in this list.
LEAK_ZONE_MARKERS = (
    "hermes-webui.service",
    "mars-testbrowsers.slice",
)

# Temp-dir prefixes that belong to test browser runs.
TMP_PREFIXES = (
    "kv-",              # Kvartal test suite
    "guard-",           # this guard
    "upgrade-browser-",
    "upgrade-mock-",
    "jh-profile",       # JustHost / CDP panels
    "live-probe-profile",
    "trace-probe-profile",
    "touch-probe-profile",
    "verify-probe-profile",
    "e2e-probe-profile",
    "diag-probe-profile",
    "playwright_",
    "pw-",
)

CLK_TCK = 100
try:
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):
    pass


def _read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def uptime() -> float:
    try:
        return float(_read("/proc/uptime").split()[0])
    except (IndexError, ValueError):
        return 0.0


def proc_age(pid: int, up: float | None = None) -> float | None:
    """Seconds since the process started, or None when /proc entry is gone."""
    stat = _read(f"/proc/{pid}/stat")
    if not stat:
        return None
    rparen = stat.rfind(")")
    if rparen < 0:
        return None
    fields = stat[rparen + 2:].split()
    try:
        starttime = int(fields[19])  # field 22 overall
    except (IndexError, ValueError):
        return None
    if up is None:
        up = uptime()
    return max(0.0, up - starttime / CLK_TCK)


def proc_cmdline(pid: int) -> list[str]:
    raw = _read(f"/proc/{pid}/cmdline")
    if not raw:
        return []
    return [part for part in raw.split("\0") if part]


def proc_cgroup(pid: int) -> str:
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        if "::" in line:
            return line.split("::", 1)[1]
    return ""


def iter_pids():
    for name in os.listdir("/proc"):
        if name.isdigit():
            yield int(name)


def classify_browser(pid: int, up: float | None = None) -> dict | None:
    """Return a descriptor for a browser process, or None for anything else."""
    cmd = proc_cmdline(pid)
    if not cmd:
        return None
    base = os.path.basename(cmd[0])
    if base not in BROWSER_BASENAMES:
        return None

    profile = None
    port = None
    headless = False
    for arg in cmd[1:]:
        if arg.startswith("--user-data-dir="):
            profile = arg.split("=", 1)[1]
        elif arg.startswith("--remote-debugging-port="):
            value = arg.split("=", 1)[1]
            port = int(value) if value.isdigit() else None
        elif arg.startswith("--headless"):
            headless = True

    return {
        "pid": pid,
        "exe": cmd[0],
        "profile": profile,
        "port": port,
        "headless": headless,
        "cgroup": proc_cgroup(pid),
        "age": proc_age(pid, up),
    }


def in_leak_zone(info: dict, markers: tuple[str, ...] = LEAK_ZONE_MARKERS) -> bool:
    cgroup = info.get("cgroup") or ""
    return any(marker in cgroup for marker in markers)


def is_test_browser(info: dict | None, markers: tuple[str, ...] = LEAK_ZONE_MARKERS) -> bool:
    """A browser counts as a *killable* test browser when it is in the leak zone
    AND uses a temp profile.

    The temp-profile requirement is the safety discriminator: Hermes' own browser
    (browser tools / real-profile snapshots) uses
    ``$HERMES_HOME/browser-profile/<browser>`` and agent-browser keeps its profile
    outside /tmp, so a legitimate managed browser is never matched here — even
    when it is headless and lives in the same cgroup. Only ad-hoc launches with a
    throwaway /tmp profile (the class that caused the 2026-11-15 incident:
    ``--user-data-dir=/tmp/kv-pop-chrome-*``) are killed.
    """
    if not info or not in_leak_zone(info, markers):
        return False
    profile = info.get("profile") or ""
    if not profile:
        return False
    if "/.hermes/" in profile or profile.startswith("/root/.hermes"):
        return False
    return profile.startswith("/tmp/") or profile.startswith("/var/tmp/")


def scan_all_browsers(markers: tuple[str, ...] = LEAK_ZONE_MARKERS) -> list[dict]:
    """Every browser process in the leak zone, test or managed (read-only)."""
    up = uptime()
    found = []
    for pid in iter_pids():
        info = classify_browser(pid, up)
        if info and in_leak_zone(info, markers):
            found.append(info)
    return found


def scan(markers: tuple[str, ...] = LEAK_ZONE_MARKERS) -> list[dict]:
    """All test browsers currently in the leak zone."""
    up = uptime()
    found = []
    for pid in iter_pids():
        info = classify_browser(pid, up)
        if info and is_test_browser(info, markers):
            found.append(info)
    return found


# ── system counters (read-only) ───────────────────────────────────────────────

def memory_psi() -> dict:
    """Memory pressure stall information: avg10/avg60/avg300 for some/full."""
    out = {"some": {}, "full": {}}
    for line in _read("/proc/pressure/memory").splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        if kind not in out:
            continue
        for token in parts[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                try:
                    out[kind][key] = float(value)
                except ValueError:
                    pass
    return out


def meminfo() -> dict:
    values = {}
    for line in _read("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.split()
        if parts:
            try:
                values[key] = int(parts[0]) * 1024
            except ValueError:
                pass
    return values


def cgroup_memory_current(unit: str) -> int | None:
    """memory.current of a systemd unit's cgroup (bytes)."""
    base = f"/sys/fs/cgroup/system.slice/{unit}"
    raw = _read(f"{base}/memory.current")
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def loadavg() -> tuple[float, float, float]:
    parts = _read("/proc/loadavg").split()
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (IndexError, ValueError):
        return (0.0, 0.0, 0.0)


def dir_age(path: str) -> float:
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return 0.0
