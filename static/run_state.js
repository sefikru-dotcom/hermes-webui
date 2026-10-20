/*
 * Run-state reconciliation for the chat control plane.
 *
 * The browser keeps three views of "is a run active for this session":
 * S.busy, S.activeStreamId and S.session.active_stream_id. /api/chat/start
 * answering 409 is the server telling us a stream id owns the session — but
 * "owns" is not the same as "alive". A cancelled run can linger in the
 * backend's active-run registry while its SSE channel is already gone, so the
 * id in the 409 body can point at a stream that will never emit a terminal
 * event. Adopting it locally makes the composer permanently busy: the Stop
 * button stays, sends get queued/steered into a dead run, and nothing ever
 * clears the flag.
 *
 * This module owns the decision. It probes /api/chat/stream/status once and
 * reports one of four outcomes; the caller (messages.js) supplies the hooks
 * that mutate its own state, so no run-state policy lives in the transcript
 * monolith.
 *
 *   live    — status active:true              → adopt and stream normally
 *   replay  — inactive but journal replayable → replay the transcript, stay idle
 *   none    — inactive and not replayable     → release local state, tell user
 *   unknown — probe failed                    → adopt (previous behavior)
 */
(function (root) {
  'use strict';

  const MODE_LIVE = 'live';
  const MODE_REPLAY = 'replay';
  const MODE_NONE = 'none';
  const MODE_UNKNOWN = 'unknown';

  const RELEASED_NOTICE = 'Previous run already ended — message queued.';

  function _baseURI(options) {
    if (options && options.baseURI) return String(options.baseURI);
    try {
      if (typeof document !== 'undefined' && document.baseURI) return document.baseURI;
    } catch (_) { /* noop */ }
    return 'http://localhost/';
  }

  function streamStatusUrl(streamId, options) {
    const rel = 'api/chat/stream/status?stream_id=' + encodeURIComponent(String(streamId || ''));
    try {
      return new URL(rel, _baseURI(options)).href;
    } catch (_) {
      return rel;
    }
  }

  /**
   * Ask the backend whether this stream id still owns a live SSE channel.
   * Never throws: an unreachable/slow probe is reported as `probed:false` so
   * the caller can fall back to the legacy reconnect path.
   */
  async function probeStreamLiveness(streamId, options) {
    const opts = options || {};
    const id = String(streamId || '');
    const fetchImpl = opts.fetchImpl || (typeof fetch === 'function' ? fetch : null);
    if (!id) return { probed: false, active: false, replayAvailable: false, error: 'no-stream-id' };
    if (!fetchImpl) return { probed: false, active: false, replayAvailable: false, error: 'no-fetch' };
    try {
      const res = await fetchImpl(streamStatusUrl(id, opts), { credentials: 'include' });
      if (!res || !res.ok) {
        return { probed: false, active: false, replayAvailable: false, error: 'http', status: res && res.status };
      }
      const body = await res.json();
      return {
        probed: true,
        active: !!(body && body.active),
        replayAvailable: !!(body && body.replay_available),
        error: null,
      };
    } catch (e) {
      return { probed: false, active: false, replayAvailable: false, error: String((e && e.message) || e) };
    }
  }

  /** Map a liveness probe onto the adoption mode (pure, no side effects). */
  function classifyLiveness(probe) {
    if (!probe || !probe.probed) return MODE_UNKNOWN;
    if (probe.active) return MODE_LIVE;
    if (probe.replayAvailable) return MODE_REPLAY;
    return MODE_NONE;
  }

  /**
   * Resolve a 409 `active_stream_id` into a single run-state outcome and run
   * the matching caller hook.
   *
   * hooks.adoptLive(streamId)   — mark busy + attach the live SSE
   * hooks.adoptReplay(streamId) — attach replay-only, keep the composer idle
   * hooks.release(streamId)     — clear activeStreamId/busy, keep the queue
   * hooks.notify(message)       — user-facing hint for the `none` outcome
   */
  async function applyConflictOwner(input) {
    const opts = input || {};
    const hooks = opts.hooks || {};
    const id = String(opts.streamId || '');
    if (!id) {
      if (typeof hooks.release === 'function') hooks.release('');
      return { mode: MODE_NONE, active: false, replayAvailable: false, probed: false };
    }
    const probe = await probeStreamLiveness(id, opts);
    const mode = classifyLiveness(probe);
    if (mode === MODE_LIVE || mode === MODE_UNKNOWN) {
      if (typeof hooks.adoptLive === 'function') hooks.adoptLive(id);
    } else if (mode === MODE_REPLAY) {
      if (typeof hooks.adoptReplay === 'function') hooks.adoptReplay(id);
    } else {
      if (typeof hooks.release === 'function') hooks.release(id);
      if (typeof hooks.notify === 'function') hooks.notify(RELEASED_NOTICE);
    }
    return { mode, active: probe.active, replayAvailable: probe.replayAvailable, probed: probe.probed };
  }

  const api = {
    MODE_LIVE,
    MODE_REPLAY,
    MODE_NONE,
    MODE_UNKNOWN,
    RELEASED_NOTICE,
    streamStatusUrl,
    probeStreamLiveness,
    classifyLiveness,
    applyConflictOwner,
  };

  if (root) root.HermesRunState = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
