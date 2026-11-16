#!/usr/bin/env python3
"""Janitor for leaked Mars test browsers and their temp profiles.

Kills browser processes that are (a) inside the hermes-webui.service cgroup or
the isolated mars-testbrowsers.slice, (b) look like automated/headless launches
with a temp profile, and (c) are older than --min-age. Also removes stale temp
profile directories that no live process references.

Deliberately conservative:
  * mars-browser.service (persistent CDP browser) is never in the leak zone.
  * Runs registered by browser-guard.sh in /run/mars-guard/protected.jsonl are
    skipped until their deadline.
  * Only paths under /tmp with known test prefixes are removed, and only when
    older than --dir-age and not referenced by a live process.

Exit codes: 0 nothing to do, 1 leaks found (and killed unless --dry-run),
2 error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import guard_lib  # noqa: E402

RUN_DIR = os.environ.get("GUARD_RUN_DIR", "/run/mars-guard")
LOG_DIR = os.environ.get("GUARD_LOG_DIR", "/var/log/mars-guard")
PROTECTED = os.path.join(RUN_DIR, "protected.jsonl")


def log(line: str, log_dir: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    text = f"{stamp} janitor {line}"
    print(text, file=sys.stderr)
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "janitor.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": stamp, "msg": line}) + "\n")
    except OSError:
        pass


def load_protected(run_dir: str, now: float) -> tuple[set[str], list[dict]]:
    """Profiles of runs that are still within their own deadline."""
    path = os.path.join(run_dir, "protected.jsonl")
    keep: list[dict] = []
    profiles: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if float(entry.get("expires", 0)) > now:
                    keep.append(entry)
                    if entry.get("profile"):
                        profiles.add(str(entry["profile"]))
    except OSError:
        return set(), []
    # rewrite the file with only live entries
    try:
        os.makedirs(run_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for entry in keep:
                fh.write(json.dumps(entry) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
    return profiles, keep


def ppid_map() -> dict[int, int]:
    result = {}
    for pid in guard_lib.iter_pids():
        stat = guard_lib._read(f"/proc/{pid}/stat")
        if not stat:
            continue
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        fields = stat[rparen + 2:].split()
        try:
            result[pid] = int(fields[1])
        except (IndexError, ValueError):
            continue
    return result


def roots_of(pids: list[int], parents: dict[int, int]) -> list[int]:
    """Processes whose parent is not itself in the set."""
    selected = set(pids)
    roots = []
    for pid in pids:
        cur = pid
        seen = set()
        is_root = True
        while cur in parents and cur > 1 and cur not in seen:
            seen.add(cur)
            parent = parents[cur]
            if parent in selected:
                is_root = False
                break
            cur = parent
        if is_root:
            roots.append(pid)
    return roots


def kill_pids(pids: list[int], grace: float, dry_run: bool) -> dict:
    """SIGTERM the roots (whole group where possible), then SIGKILL survivors."""
    parents = ppid_map()
    roots = roots_of(pids, parents)
    result = {"roots": roots, "signalled": [], "killed": [], "survived": []}

    if dry_run:
        result["signalled"] = list(roots)
        return result

    for pid in roots:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
                result["signalled"].append(pid)
                continue
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
            result["signalled"].append(pid)
        except OSError:
            pass

    deadline = time.time() + grace
    while time.time() < deadline:
        alive = [p for p in pids if os.path.exists(f"/proc/{p}")]
        if not alive:
            break
        time.sleep(0.5)

    for pid in pids:
        if not os.path.exists(f"/proc/{pid}"):
            result["killed"].append(pid)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            result["killed"].append(pid)
        except OSError:
            result["survived"].append(pid)

    time.sleep(0.3)
    result["survived"] = [p for p in result["survived"] if os.path.exists(f"/proc/{p}")]
    return result


def live_profiles() -> set[str]:
    """Every --user-data-dir currently referenced by a running process."""
    found = set()
    for pid in guard_lib.iter_pids():
        for arg in guard_lib.proc_cmdline(pid):
            if arg.startswith("--user-data-dir="):
                found.add(arg.split("=", 1)[1])
    return found


def stale_dirs(min_age: float, protected: set[str]) -> list[tuple[str, float]]:
    """Temp dirs with test prefixes, older than min_age, not in use."""
    in_use = live_profiles()
    result = []
    try:
        entries = os.listdir("/tmp")
    except OSError:
        return result
    now = time.time()
    for name in entries:
        if not any(name.startswith(prefix) for prefix in guard_lib.TMP_PREFIXES):
            continue
        path = os.path.join("/tmp", name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if path in in_use or path in protected:
            continue
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age >= min_age:
            result.append((path, age))
    return result


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep leaked test browsers and temp profiles.")
    ap.add_argument("--min-age", type=float, default=2700.0,
                    help="kill test browsers older than this many seconds (default 2700)")
    ap.add_argument("--dir-age", type=float, default=21600.0,
                    help="remove temp dirs older than this many seconds (default 21600)")
    ap.add_argument("--grace", type=float, default=10.0,
                    help="seconds between SIGTERM and SIGKILL (default 10)")
    ap.add_argument("--dry-run", action="store_true", help="report only, kill nothing")
    ap.add_argument("--no-dirs", action="store_true", help="skip temp dir cleanup")
    ap.add_argument("--zone-marker", action="append", default=None,
                    help="override leak-zone cgroup markers (repeatable; testing)")
    ap.add_argument("--log-dir", default=LOG_DIR)
    args = ap.parse_args()

    markers = tuple(args.zone_marker) if args.zone_marker else guard_lib.LEAK_ZONE_MARKERS
    now = time.time()
    protected, _live = load_protected(RUN_DIR, now)

    candidates = []
    skipped_young = 0
    skipped_protected = 0
    for info in guard_lib.scan(markers):
        if info.get("profile") and info["profile"] in protected:
            skipped_protected += 1
            continue
        age = info.get("age") or 0.0
        if age < args.min_age:
            skipped_young += 1
            continue
        candidates.append(info)

    summary: dict = {
        "dry_run": args.dry_run,
        "min_age": args.min_age,
        "leaks": [
            {
                "pid": info["pid"],
                "age_min": round((info.get("age") or 0) / 60, 1),
                "profile": info.get("profile"),
                "port": info.get("port"),
                "cgroup": info.get("cgroup"),
            }
            for info in candidates
        ],
        "skipped_young": skipped_young,
        "skipped_protected": skipped_protected,
        "kill": {},
        "dirs": {"removed": 0, "freed_mb": 0.0, "paths": []},
    }

    rc = 0
    if candidates:
        rc = 1
        pids = [info["pid"] for info in candidates]
        summary["kill"] = kill_pids(pids, args.grace, args.dry_run)
        log(f"leaks={len(pids)} dry_run={args.dry_run} pids={pids}", args.log_dir)

    if not args.no_dirs:
        dirs = stale_dirs(args.dir_age, protected)
        freed = 0
        removed = []
        for path, _age in dirs:
            size = dir_size(path)
            if not args.dry_run:
                shutil.rmtree(path, ignore_errors=True)
                if os.path.isdir(path):
                    continue
            freed += size
            removed.append(path)
        summary["dirs"] = {
            "removed": len(removed),
            "freed_mb": round(freed / (1024 * 1024), 1),
            "paths": removed[:20],
            "truncated": len(removed) > 20,
        }
        if removed:
            log(f"dirs_removed={len(removed)} freed_mb={round(freed / 1048576, 1)} "
                f"dry_run={args.dry_run}", args.log_dir)

    print(json.dumps(summary, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
