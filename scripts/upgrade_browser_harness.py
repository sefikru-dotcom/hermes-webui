#!/usr/bin/env python3
"""One-command driver for the WebUI upgrade browser regression suite.

WHY THIS EXISTS
  The suite needs a mock backend on a local port and a real Chromium. Doing that
  by hand invites two failure modes: picking a fixed port (collides with whatever
  else runs on the host) and stopping the server by port number (which can kill
  somebody else's process that grabbed the port in between). This driver never
  does either:

    * the server binds 127.0.0.1:0, so the kernel picks a free ephemeral port and
      prints it back — nothing on the host can be in the way;
    * the child is stopped through the Popen handle / its own PID only, never
      ``fuser -k <port>`` and never ``pkill -f <generic pattern>``;
    * the browser is closed by the runner (``browser.close()`` in a finally
      block), and this driver also kills the whole process group on exit.

  Everything is isolated: the app root is read-only input, fixtures are read-only
  input, and no live config, session store, password or LLM endpoint is touched.

USAGE
  python3 scripts/upgrade_browser_harness.py --app-root .
  python3 scripts/upgrade_browser_harness.py --app-root /root/workspace/integration \\
      --viewports desktop,mobile --only modes_live_quiet_to_full
  python3 scripts/upgrade_browser_harness.py --app-root . --baseline tests/test_upgrade_browser_baseline.json

  --app-root is the whole point of the post-merge story: point it at the
  integration tree and the same scenarios run against the merged code.

EXIT CODES  0 ok (or only known-baseline failures), 1 new failures, 2 setup problem.
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
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_FIXTURES = os.path.join(REPO, "tests", "fixtures", "upgrade_chat")
SERVER = os.path.join(HERE, "upgrade_browser_server.py")
RUNNER = os.path.join(HERE, "upgrade_browser_run.mjs")

# A Chromium build from the shared Playwright cache is enough; the harness does
# not need `playwright install` and must not touch the live venv.
PW_CACHE = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")


def log(msg: str) -> None:
    print(msg, flush=True)


def _read_port(proc: subprocess.Popen, timeout: float = 20.0) -> int:
    """Wait for the server's ``PORT <n>`` handshake line."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("mock server exited before printing its port")
            time.sleep(0.05)
            continue
        line = line.strip()
        if line.startswith("PORT "):
            return int(line.split()[1])
    raise RuntimeError("mock server did not report a port in time")


def _stop(proc: Optional[subprocess.Popen], name: str) -> None:
    """Stop ONLY the child we started, by handle first and PID as a fallback."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
    log(f"[harness] stopped {name} (pid {proc.pid})")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--app-root", default=os.environ.get("UPGRADE_APP_ROOT", REPO),
                    help="tree that contains static/ (worktree today, integration tree after merge)")
    ap.add_argument("--fixture-dir", default=os.environ.get("UPGRADE_FIXTURE_DIR", DEFAULT_FIXTURES))
    ap.add_argument("--viewports", default="desktop,mobile")
    ap.add_argument("--only", default="", help="comma separated scenario names")
    ap.add_argument("--out", default="", help="report path (default: temp file, echoed at the end)")
    ap.add_argument("--baseline", default="",
                    help="JSON with known-baseline failures (default: tests/test_upgrade_browser_baseline.json)")
    ap.add_argument("--no-seed", action="store_true", default=True,
                    help="start the mock backend with no sessions (scenarios seed themselves)")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args(argv)

    app_root = os.path.abspath(args.app_root)
    if not os.path.isfile(os.path.join(app_root, "static", "index.html")):
        log(f"SETUP FAIL: no static/index.html under {app_root}")
        return 2
    if not os.path.isfile(RUNNER):
        log(f"SETUP FAIL: runner missing: {RUNNER}")
        return 2

    out = args.out or os.path.join(tempfile.mkdtemp(prefix="upgrade-browser-"), "report.json")
    state_file = os.path.join(tempfile.mkdtemp(prefix="upgrade-mock-"), "state.json")

    log(f"[harness] app root : {app_root}")
    log(f"[harness] fixtures : {args.fixture_dir}")
    log(f"[harness] browsers : {PW_CACHE}")

    server_cmd = [
        sys.executable, SERVER,
        "--app-root", app_root,
        "--fixture-dir", args.fixture_dir,
        "--port", "0",              # ephemeral: the kernel picks, we never guess
        "--state-out", state_file,
    ]
    if args.no_seed:
        server_cmd.append("--no-seed")

    server = subprocess.Popen(
        server_cmd, cwd=app_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,
    )
    try:
        port = _read_port(server)
        log(f"[harness] mock server pid {server.pid} on 127.0.0.1:{port}")
        env = dict(os.environ)
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", PW_CACHE)
        node_cmd = [
            "node", RUNNER,
            "--base-url", f"http://127.0.0.1:{port}",
            "--viewports", args.viewports,
            "--out", out,
        ]
        if args.only:
            node_cmd += ["--only", args.only]
        if args.baseline:
            node_cmd += ["--baseline", os.path.abspath(args.baseline)]
        rc = subprocess.call(node_cmd, cwd=app_root, env=env, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        log("[harness] runner timed out")
        rc = 2
    except Exception as exc:  # pragma: no cover - setup diagnostics
        log(f"SETUP FAIL: {exc}")
        rc = 2
    finally:
        _stop(server, "mock server")
        # Belt and braces: reap anything left in the child's process group. This
        # targets the group we created above, never a port or a name pattern.
        try:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    report = None
    if os.path.isfile(out):
        with open(out, "r", encoding="utf-8") as fh:
            report = json.load(fh)

    if report is None:
        log(f"[harness] no report produced (runner rc={rc}); see output above")
        return rc if rc in (1, 2) else 2

    # Baseline classification lives in the runner (single implementation); the
    # harness only reports and maps it to an exit code.
    totals = report.get("totals", {})
    if args.baseline:
        known = totals.get("known_failures", 0)
        fixed = totals.get("baseline_fixed", 0)
        if known:
            log(f"[baseline] {known} known failure(s) reproduced (documented, not regressions)")
            for res in report["results"]:
                for check in res["checks"]:
                    if check.get("known"):
                        log(f"  ~ {res['name']} [{res['viewport']}] {check['name']}")
        if fixed:
            log(f"[baseline] {fixed} stale entry(ies) now pass — remove them from {args.baseline}")
        log("[baseline] no new failures" if report.get("ok") else "[baseline] NEW failures present")

    log("")
    totals = report.get("totals", {})
    log(f"[harness] {totals.get('passed', '?')}/{totals.get('scenarios', '?')} scenario runs passed "
        f"({totals.get('checks_failed', '?')} of {totals.get('checks', '?')} checks failed)")
    log(f"[harness] report: {out}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
