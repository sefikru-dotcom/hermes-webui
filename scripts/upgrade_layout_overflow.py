#!/usr/bin/env python3
"""Driver for the layout overflow probe (scripts/upgrade_layout_overflow.mjs).

WHY THIS EXISTS
  The probe needs the mock backend on a local port and a real Chromium. This
  driver owns both lifetimes without the two classic footguns:

    * the server binds 127.0.0.1:0 and reports the port it got, so nothing on
      the host can already be sitting on it;
    * the server is stopped through its own Popen handle / process group, never
      by port (`fuser -k`) and never by a broad `pkill -f`.

  It is deliberately separate from scripts/upgrade_browser_harness.py: that
  driver belongs to the browser-scenario suite, this one only runs the layout
  probe so the layout owner can gate the shell geometry on its own.

USAGE
  python3 scripts/upgrade_layout_overflow.py --app-root .
  python3 scripts/upgrade_layout_overflow.py --app-root ../integration \
      --out /tmp/upgrade-layout-browser.json
  # one frame only (fast local loop while tuning a CSS rule)
  python3 scripts/upgrade_layout_overflow.py --only desktop-1024x720

EXIT CODES  0 all checks passed, 1 failures, 2 setup problem.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = HERE / "upgrade_browser_server.py"
PROBE = HERE / "upgrade_layout_overflow.mjs"
DEFAULT_FIXTURES = REPO / "tests" / "fixtures" / "upgrade_chat"


def log(msg: str) -> None:
    print(msg, flush=True)


def _read_port(proc: subprocess.Popen, timeout: float = 25.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"mock server exited early (rc={proc.returncode})")
            time.sleep(0.05)
            continue
        line = line.strip()
        if line.startswith("PORT "):
            return int(line.split()[1])
    raise RuntimeError("mock server did not report a port in time")


def _wait_ready(port: int, timeout: float = 20.0) -> None:
    """Poll `/` until the mock backend answers.

    A printed PORT only proves the socket is bound; Chromium's first navigation
    still raced the request handler in earlier runs (ERR_CONNECTION_REFUSED /
    empty shell). One successful HTTP response is the actual readiness signal.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                if r.status == 200:
                    return
                last = f"status {r.status}"
        except (urllib.error.URLError, OSError) as exc:  # not up yet
            last = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"mock server never became ready on :{port} ({last})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-root", default=str(REPO))
    ap.add_argument("--fixture-dir", default=str(DEFAULT_FIXTURES))
    ap.add_argument("--out", default="/tmp/upgrade-layout-browser.json")
    ap.add_argument("--only", default="", help="comma-separated frame names (desktop-1024x720,mobile-393)")
    ap.add_argument("--timeout", type=float, default=420.0, help="hard cap for the node probe")
    args = ap.parse_args()

    if not SERVER.is_file() or not PROBE.is_file():
        log(f"setup: missing {SERVER if not SERVER.is_file() else PROBE}")
        return 2

    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/root/.cache/ms-playwright")

    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--app-root", str(Path(args.app_root).resolve()),
         "--fixture-dir", str(Path(args.fixture_dir).resolve())],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True, cwd=str(REPO), env=env,
    )
    try:
        port = _read_port(server)
        _wait_ready(port)
        log(f"[layout-probe] app root : {Path(args.app_root).resolve()}")
        log(f"[layout-probe] mock server pid {server.pid} on 127.0.0.1:{port}")
        cmd = ["node", str(PROBE), "--base-url", f"http://127.0.0.1:{port}", "--out", args.out]
        if args.only:
            cmd += ["--only", args.only]
        try:
            proc = subprocess.run(cmd, cwd=str(REPO), env=env, text=True,
                                  timeout=args.timeout, start_new_session=True)
        except subprocess.TimeoutExpired:
            log(f"[layout-probe] node probe exceeded {args.timeout:.0f}s — treating as FAIL")
            return 1
        return proc.returncode
    finally:
        # Only this child (and its group) is stopped.
        try:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            server.wait(timeout=5)
        log(f"[layout-probe] stopped mock server (pid {server.pid})")


if __name__ == "__main__":
    sys.exit(main())
