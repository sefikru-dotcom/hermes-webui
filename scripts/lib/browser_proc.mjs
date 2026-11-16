// Shared Chrome launcher for the dev probes in scripts/.
//
// The probes used to call spawn('google-chrome', ...) directly and then
// chrome.kill('SIGKILL') at the end. That leaves the whole browser tree and its
// profile behind when the probe throws, is Ctrl-C'd, or the parent dies: the
// renderer/GPU children survive their parent, and on this host a leftover
// headless Chrome is charged to the WebUI cgroup (see ops/browser-guard/README.md).
//
// launchChrome() puts the browser in its own process group, arms a wall-clock
// watchdog and returns the child unchanged, so existing code that reads
// chrome.stderr keeps working. killTree() kills the entire group, not just the
// browser process, and is safe to call twice.

import { spawn } from 'node:child_process';

const DEFAULT_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * @param {string[]} args        chrome argv (without the binary)
 * @param {object}   [opts]
 * @param {number}   [opts.timeoutMs]  hard limit before the group is killed
 * @param {string}   [opts.bin]        browser binary (default google-chrome)
 * @param {object}   [opts.stdio]      stdio config passed to spawn
 */
export function launchChrome(args, opts = {}) {
  const {
    timeoutMs = DEFAULT_TIMEOUT_MS,
    bin = process.env.AGENT_BROWSER_EXECUTABLE_PATH || 'google-chrome',
    stdio = ['ignore', 'ignore', 'pipe'],
  } = opts;

  const child = spawn(bin, args, { stdio, detached: true });

  const timer = setTimeout(() => {
    process.stderr.write(`[browser_proc] timeout ${timeoutMs}ms — killing group ${child.pid}\n`);
    killTree(child);
  }, timeoutMs);
  timer.unref?.();

  child.once('exit', () => clearTimeout(timer));
  child.once('error', () => clearTimeout(timer));

  // Ctrl-C / kill on the probe must not leave the browser running.
  const onSignal = (sig) => {
    killTree(child);
    process.exit(sig === 'SIGINT' ? 130 : 143);
  };
  const signals = ['SIGINT', 'SIGTERM'];
  for (const sig of signals) process.once(sig, onSignal);
  child.once('exit', () => {
    for (const sig of signals) process.removeListener(sig, onSignal);
  });

  return child;
}

/** Kill the browser's whole process group (renderers, GPU, zygote). Idempotent. */
export function killTree(child) {
  if (!child || !child.pid) return;
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch {
    // group already gone or never created (not a leader) — fall back to the pid
    try {
      child.kill('SIGKILL');
    } catch {
      /* already dead */
    }
  }
}
