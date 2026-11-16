#!/usr/bin/env python3
"""Read-only health probe for the Mars WebUI + test-browser guard.

Checks the things that actually failed during the 2026-11-15 incident:
  * WebUI answers HTTP (fast)
  * memory pressure (PSI full avg10) is not saturated
  * swap is not full
  * hermes-webui.service memory stays under its MemoryMax
  * no leaked test browsers sit in the service cgroup

It never allocates memory, never touches swap and never kills anything —
killing is the janitor's job. Cheap enough to run every 5 minutes.

Exit codes: 0 ok, 1 warn, 2 critical.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import guard_lib  # noqa: E402

LOG_DIR = os.environ.get("GUARD_LOG_DIR", "/var/log/mars-guard")

DEFAULTS = {
    "url": "http://127.0.0.1:8787/",
    "service": "hermes-webui.service",
    "http_timeout": 5.0,
    "http_warn_s": 2.0,
    "http_crit_s": 6.0,
    "psi_warn": 15.0,
    "psi_crit": 40.0,
    "swap_warn_pct": 70.0,
    "swap_crit_pct": 90.0,
    "mem_warn_mb": 4500.0,
    "mem_crit_mb": 5300.0,
    "leak_warn": 1,
    "leak_crit": 4,
}


def http_probe(url: str, timeout: float) -> dict:
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mars-guard-health/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            resp.read(256)
        elapsed = time.time() - started
        return {"ok": code in (200, 302), "code": code,
                "ms": round(elapsed * 1000), "error": None}
    except urllib.error.HTTPError as exc:
        elapsed = time.time() - started
        return {"ok": exc.code in (200, 302), "code": exc.code,
                "ms": round(elapsed * 1000), "error": None}
    except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
        elapsed = time.time() - started
        return {"ok": False, "code": None,
                "ms": round(elapsed * 1000), "error": f"{type(exc).__name__}: {exc}"}


def evaluate(args) -> dict:
    checks: list[dict] = []
    status = 0  # 0 ok, 1 warn, 2 crit

    def add(name: str, level: int, value, detail: str) -> None:
        nonlocal status
        status = max(status, level)
        checks.append({"check": name, "level": level, "value": value, "detail": detail})

    http = http_probe(args.url, args.http_timeout)
    if not http["ok"]:
        add("http", 2, http.get("code"), f"no answer: {http['error'] or http.get('code')}")
    elif http["ms"] / 1000.0 > args.http_crit_s:
        add("http", 2, http["ms"], f"{http['ms']} ms (> {args.http_crit_s}s)")
    elif http["ms"] / 1000.0 > args.http_warn_s:
        add("http", 1, http["ms"], f"{http['ms']} ms (> {args.http_warn_s}s)")
    else:
        add("http", 0, http["ms"], f"HTTP {http.get('code')} in {http['ms']} ms")

    psi = guard_lib.memory_psi()
    full10 = psi.get("full", {}).get("avg10", 0.0)
    some10 = psi.get("some", {}).get("avg10", 0.0)
    if full10 >= args.psi_crit:
        add("memory_psi", 2, full10, f"full avg10={full10}% — all tasks stalled")
    elif full10 >= args.psi_warn:
        add("memory_psi", 1, full10, f"full avg10={full10}%")
    else:
        add("memory_psi", 0, full10, f"full avg10={full10}% some avg10={some10}%")

    mem = guard_lib.meminfo()
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    swap_pct = (swap_used / swap_total * 100.0) if swap_total else 0.0
    avail_pct = (avail / total * 100.0) if total else 100.0
    # Swap at 100% with plenty of free RAM is a leftover, not an outage: during
    # the incident swap was full *and* MemAvailable was collapsing. Escalate to
    # critical only when both are true, otherwise warn.
    if swap_total and swap_pct >= args.swap_crit_pct and avail_pct < 15.0:
        add("swap", 2, round(swap_pct, 1),
            f"{swap_used // 1048576} MB of {swap_total // 1048576} MB used, "
            f"only {avail // 1048576} MB RAM available")
    elif swap_total and swap_pct >= args.swap_warn_pct:
        add("swap", 1, round(swap_pct, 1),
            f"{swap_used // 1048576} MB of {swap_total // 1048576} MB used "
            f"(RAM available {avail // 1048576} MB)")
    else:
        add("swap", 0, round(swap_pct, 1),
            f"{swap_used // 1048576} MB of {swap_total // 1048576} MB used")

    service_bytes = guard_lib.cgroup_memory_current(args.service)
    service_mb = round(service_bytes / 1048576, 1) if service_bytes else None
    if service_mb is None:
        add("service_memory", 1, None, f"{args.service} cgroup not readable")
    elif service_mb >= args.mem_crit_mb:
        add("service_memory", 2, service_mb, f"{service_mb} MB (MemoryMax 5500 MB)")
    elif service_mb >= args.mem_warn_mb:
        add("service_memory", 1, service_mb, f"{service_mb} MB")
    else:
        add("service_memory", 0, service_mb, f"{service_mb} MB")

    leaks = guard_lib.scan()
    if len(leaks) >= args.leak_crit:
        add("test_browsers", 2, len(leaks),
            f"{len(leaks)} test browsers with a /tmp profile in the service cgroup")
    elif len(leaks) >= args.leak_warn:
        add("test_browsers", 1, len(leaks),
            f"{len(leaks)} test browser(s) with a /tmp profile in the service cgroup")
    else:
        add("test_browsers", 0, len(leaks), "none")

    # Informational: any browser process in the service cgroup, managed or not.
    # Managed Hermes browsers are expected here; this is the number to eyeball
    # when the service memory climbs.
    zone_browsers = guard_lib.scan_all_browsers()
    oldest = max((b.get("age") or 0) for b in zone_browsers) if zone_browsers else 0.0
    add("browsers_in_service", 0, len(zone_browsers),
        f"{len(zone_browsers)} browser procs, oldest {round(oldest / 60)} min")

    load1, load5, _load15 = guard_lib.loadavg()
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": ("ok", "warn", "critical")[status],
        "exit": status,
        "checks": checks,
        "metrics": {
            "http_ms": http["ms"],
            "http_code": http.get("code"),
            "psi_full_avg10": full10,
            "psi_some_avg10": some10,
            "mem_total_mb": total // 1048576,
            "mem_available_mb": avail // 1048576,
            "swap_used_mb": swap_used // 1048576,
            "swap_total_mb": swap_total // 1048576,
            "service_mb": service_mb,
            "test_browsers": len(leaks),
            "browsers_in_service": len(zone_browsers),
            "load1": load1,
            "load5": load5,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Mars WebUI + browser-guard health probe.")
    for key, value in DEFAULTS.items():
        ap.add_argument("--" + key.replace("_", "-"), default=value, type=type(value))
    ap.add_argument("--json", action="store_true", help="print only the JSON line")
    ap.add_argument("--no-log", action="store_true", help="do not append to health.jsonl")
    ap.add_argument("--log-dir", default=LOG_DIR)
    args = ap.parse_args()

    result = evaluate(args)

    if not args.no_log:
        try:
            os.makedirs(args.log_dir, exist_ok=True)
            path = os.path.join(args.log_dir, "health.jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            # keep the log bounded: last 5000 lines
            try:
                if os.path.getsize(path) > 4 * 1024 * 1024:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.readlines()[-5000:]
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.writelines(lines)
            except OSError:
                pass
        except OSError:
            pass

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        metrics = result["metrics"]
        print(f"{result['ts']} mars-health {result['status'].upper()} "
              f"http={metrics['http_code']}/{metrics['http_ms']}ms "
              f"psi_full={metrics['psi_full_avg10']}% "
              f"swap={metrics['swap_used_mb']}/{metrics['swap_total_mb']}MB "
              f"svc={metrics['service_mb']}MB "
              f"testbrowsers={metrics['test_browsers']} "
              f"load={metrics['load1']}")
        for check in result["checks"]:
            if check["level"] > 0:
                print(f"  ! {check['check']}: {check['detail']}")
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main())
