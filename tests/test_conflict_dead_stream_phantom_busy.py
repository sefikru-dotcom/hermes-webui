"""Behavioral regression tests: a 409 conflict must not adopt a DEAD stream.

Bug (frontend half)
-------------------
``send()`` treats every ``409 session already has an active stream`` as
"reattach the live run": it reads ``active_stream_id`` out of the error body,
then unconditionally sets ``S.activeStreamId``, calls ``setBusy(true)`` and
``attachLiveStream(..., {reconnecting:true})``.

When the backend's cancel is a soft interrupt, the run can stay in
``ACTIVE_RUNS`` (phase ``cancelling``) for a long time while its ``STREAMS``
entry is already gone. ``/api/chat/start`` then keeps answering 409 with that
stream id, but ``/api/chat/stream/status`` reports ``active:false`` and the SSE
has no terminal event to replay. The client adopts a stream that will never
settle: ``S.busy`` stays true, the composer turns into Stop/queue mode and the
user cannot send again after pressing Stop.

Contract under test (``static/run_state.js``)
---------------------------------------------
``applyConflictOwner({streamId, hooks, fetchImpl})`` probes stream liveness
before touching local run state and dispatches exactly one outcome:

  * ``live``    — status active:true            → ``hooks.adoptLive``
  * ``replay``  — inactive but journal replayable → ``hooks.adoptReplay``
  * ``none``    — inactive and not replayable   → ``hooks.release`` + ``notify``
  * ``unknown`` — probe failed                  → ``hooks.adoptLive`` (legacy)

``messages.js`` must call it before it sets busy, so ``setBusy(true)`` can only
happen through the ``adoptLive`` hook.

Both layers are covered: a node runtime harness that actually executes the
module with fake probes (behavior), plus source checks that pin the wiring
(messages.js calls the module before ``setBusy(true)``; the script is shipped
and pre-cached).
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).parent.parent
RUN_STATE_JS_PATH = REPO / "static" / "run_state.js"
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
SW_JS = (REPO / "static" / "sw.js").read_text(encoding="utf-8")


def _extract_conflict_branch(src: str) -> str:
    """Return the ``if(conflictActiveStream){...}`` block from send()'s catch."""
    marker = "const conflictActiveStream="
    start = src.index(marker)
    open_brace = src.index("{", src.index("if(conflictActiveStream)", start))
    depth = 1
    pos = open_brace + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    return src[start:pos]


CONFLICT_SRC = _extract_conflict_branch(MESSAGES_JS)


def _strip_comments(src: str) -> str:
    """Drop // and /* */ comments so structural checks match code, not prose."""
    without_block = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//[^\n]*$", "", without_block)


CONFLICT_CODE = _strip_comments(CONFLICT_SRC)


# ── Node runtime harness ───────────────────────────────────────────────────

_NODE_HARNESS = r'''
const MODULE_PATH = __MODULE_PATH__;
const mod = require(MODULE_PATH);

function makeHooks(calls) {
  return {
    adoptLive: (sid) => calls.push({ hook: 'adoptLive', streamId: String(sid) }),
    adoptReplay: (sid) => calls.push({ hook: 'adoptReplay', streamId: String(sid) }),
    release: (sid) => calls.push({ hook: 'release', streamId: String(sid) }),
    notify: (msg) => calls.push({ hook: 'notify', message: String(msg) }),
  };
}

function probe(status, body) {
  return async (url, opts) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
}

async function runCase(name, fetchImpl, streamId) {
  const calls = [];
  const result = await mod.applyConflictOwner({
    streamId: streamId === undefined ? 'stream-dead' : streamId,
    hooks: makeHooks(calls),
    fetchImpl,
  });
  return { name, result, calls };
}

(async () => {
  const out = {};
  out.dead = await runCase('dead', probe(200, { active: false, replay_available: false }));
  out.live = await runCase('live', probe(200, { active: true, replay_available: false }));
  out.replay = await runCase('replay', probe(200, { active: false, replay_available: true }));
  out.httpError = await runCase('httpError', probe(503, { error: 'unavailable' }));
  out.throws = await runCase('throws', async () => { throw new Error('boom'); });

  // URL the probe must build (encoded stream id, relative to the app base).
  let seenUrl = null;
  await mod.applyConflictOwner({
    streamId: 'a b/c',
    hooks: makeHooks([]),
    fetchImpl: async (url) => { seenUrl = String(url); return { ok: true, status: 200, json: async () => ({ active: true }) }; },
    baseURI: 'http://localhost:8787/',
  });
  out.probeUrl = seenUrl;

  // Empty stream id must release and never probe.
  let probed = 0;
  const emptyCalls = [];
  const emptyResult = await mod.applyConflictOwner({
    streamId: '',
    hooks: makeHooks(emptyCalls),
    fetchImpl: async () => { probed += 1; return { ok: true, status: 200, json: async () => ({ active: true }) }; },
  });
  out.empty = { result: emptyResult, calls: emptyCalls, probes: probed };

  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
'''


@pytest.fixture(scope="module")
def harness_output() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the runtime half of these tests")
    assert RUN_STATE_JS_PATH.exists(), (
        "static/run_state.js is missing: the 409 conflict path has no run-state "
        "reconciliation layer to decide whether the stream is still alive"
    )
    script = _NODE_HARNESS.replace("__MODULE_PATH__", json.dumps(str(RUN_STATE_JS_PATH)))
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Behavior: dead stream must not be adopted ──────────────────────────────

class TestConflictProbeBehavior:
    def test_dead_stream_releases_and_never_adopts(self, harness_output):
        """409 + status active:false + no replay => release local state, no busy.

        This is the reported regression: adopting this stream leaves the
        composer permanently busy with no terminal event to settle it.
        """
        case = harness_output["dead"]
        assert case["result"]["mode"] == "none", (
            "a stream whose /api/chat/stream/status says active:false and that has "
            "no replayable journal must resolve to 'none', not be adopted"
        )
        hooks = [c["hook"] for c in case["calls"]]
        assert "adoptLive" not in hooks, "dead stream must never take the live-adopt path"
        assert "adoptReplay" not in hooks, "dead stream without a journal has nothing to replay"
        assert hooks.count("release") == 1, "local run state must be released exactly once"
        assert "notify" in hooks, "the user needs a hint that the old run already ended"

    def test_live_stream_is_still_adopted(self, harness_output):
        """A genuinely running stream keeps the #active-stream-reconnect behavior."""
        case = harness_output["live"]
        assert case["result"]["mode"] == "live"
        hooks = [c["hook"] for c in case["calls"]]
        assert hooks == ["adoptLive"], f"live stream must only adopt, got {hooks}"
        assert case["calls"][0]["streamId"] == "stream-dead"

    def test_replayable_stream_uses_replay_hook(self, harness_output):
        """Inactive but journal-backed: replay the transcript, stay sendable."""
        case = harness_output["replay"]
        assert case["result"]["mode"] == "replay"
        hooks = [c["hook"] for c in case["calls"]]
        assert hooks == ["adoptReplay"], f"replayable stream must use the replay hook, got {hooks}"

    def test_probe_failure_falls_back_to_adopt(self, harness_output):
        """If liveness cannot be determined, keep the previous reconnect behavior."""
        for key in ("httpError", "throws"):
            case = harness_output[key]
            assert case["result"]["mode"] == "unknown", f"{key}: unknown liveness must not release state"
            hooks = [c["hook"] for c in case["calls"]]
            assert hooks == ["adoptLive"], f"{key}: fallback must adopt live, got {hooks}"

    def test_probe_uses_stream_status_endpoint_with_encoded_id(self, harness_output):
        url = harness_output["probeUrl"]
        assert url and "api/chat/stream/status" in url, f"probe must hit stream/status, got {url!r}"
        assert "stream_id=a%20b%2Fc" in url, f"stream id must be URL-encoded, got {url!r}"

    def test_empty_stream_id_never_probes(self, harness_output):
        case = harness_output["empty"]
        assert case["probes"] == 0, "an empty stream id must not trigger a network probe"
        assert [c["hook"] for c in case["calls"]] == ["release"], (
            "an empty stream id must just release local state"
        )


# ── Wiring: send()'s 409 branch must ask the module before going busy ──────

class TestConflictWiring:
    def test_messages_calls_run_state_before_setting_busy(self):
        assert "applyConflictOwner" in CONFLICT_SRC, (
            "the 409 branch must delegate adoption to HermesRunState.applyConflictOwner()"
        )
        delegate_at = CONFLICT_SRC.index("applyConflictOwner")
        busy_at = CONFLICT_SRC.index("setBusy(true)")
        assert delegate_at < busy_at, (
            "setBusy(true) must happen inside the adoptLive hook, i.e. AFTER the "
            "liveness decision — otherwise a dead stream leaves a phantom busy UI"
        )

    def test_busy_is_never_set_before_the_liveness_decision(self):
        """setBusy(true) may only appear inside adoptLive (or the legacy fallback).

        The bug was an unconditional ``setBusy(true)`` right after the 409 was
        parsed, so a dead stream id turned the composer busy before anything had
        checked whether the stream could still settle.
        """
        assert CONFLICT_CODE.count("setBusy(true)") == 2, (
            "expected exactly two: the adoptLive hook and the legacy (module-not-loaded) "
            "fallback"
        )
        decision_at = CONFLICT_CODE.index("applyConflictOwner")
        assert decision_at < CONFLICT_CODE.index("setBusy(true)"), (
            "setBusy(true) must not run before applyConflictOwner() resolves the owner"
        )
        markers = ["adoptLive:", "else if(_activeStreamId)"]
        for m in re.finditer(r"setBusy\(true\)", CONFLICT_CODE):
            owners = [mk for mk in markers if CONFLICT_CODE.rfind(mk, 0, m.start()) >= 0]
            assert owners, (
                "setBusy(true) at offset "
                f"{m.start()} is not inside adoptLive or the legacy fallback: an "
                "unconditional busy raise is exactly the phantom-busy bug"
            )
        live_hook = re.search(r"adoptLive\s*:\s*(?:async\s*)?\(", CONFLICT_CODE)
        assert live_hook, "the branch must pass an adoptLive hook"
        between = CONFLICT_CODE[live_hook.start():CONFLICT_CODE.index("setBusy(true)")]
        assert "adoptReplay" not in between, "setBusy(true) must be inside adoptLive, not after it"
        # ... and adoptLive must come after the reconciliation call, so the decision
        # is already made when busy is raised.
        assert decision_at < live_hook.start()

    def test_release_hook_clears_active_stream_id(self):
        release_hook = re.search(r"release\s*:\s*(?:async\s*)?\(", CONFLICT_CODE)
        assert release_hook, "the branch must pass a release hook"
        tail = CONFLICT_CODE[release_hook.start():]
        body = tail[: tail.index("},", 10) if "}," in tail[10:] else len(tail)]
        assert "S.activeStreamId=null" in body.replace(" ", ""), (
            "release() must clear S.activeStreamId so the next send is not treated as busy"
        )
        assert "S.busy=false" in body.replace(" ", ""), "release() must clear the busy flag"

    def test_release_does_not_auto_drain_the_queue(self):
        """Releasing after a 409 must not re-send the queued turn into the dead run.

        setBusy(false) drains one queued message after 120ms; doing that from the
        409 path would immediately re-POST /api/chat/start against the same dead
        stream (a 409 loop), so the release hook clears the flag directly.
        """
        release_hook = re.search(r"release\s*:\s*(?:async\s*)?\(", CONFLICT_CODE)
        assert release_hook
        tail = CONFLICT_CODE[release_hook.start():]
        body = tail[: tail.index("},", 10) if "}," in tail[10:] else len(tail)]
        assert "setBusy(false)" not in body, (
            "release() must not call setBusy(false): its queue drain would retry the "
            "rejected start against the same dead stream"
        )

    def test_queue_preserved_on_dead_stream(self):
        """The user's typed message must still be queued, not dropped."""
        queue_at = CONFLICT_SRC.index("queueSessionMessage")
        delegate_at = CONFLICT_SRC.index("applyConflictOwner")
        assert queue_at < delegate_at, (
            "queue the attempted turn BEFORE reconciling the owner so a dead stream "
            "never loses the message"
        )


# ── Shipping: the new module must load and be pre-cached ──────────────────

class TestRunStateShipping:
    def test_script_tag_present_and_versioned(self):
        m = re.search(r'<script src="static/run_state\.js\?v=([^"]+)"', INDEX_HTML)
        assert m, "static/run_state.js must be loaded from index.html"
        assert "__WEBUI_VERSION__" in m.group(1), "asset URL must keep the version token"

    def test_script_loaded_before_messages(self):
        run_state_at = INDEX_HTML.index('static/run_state.js')
        messages_at = INDEX_HTML.index('static/messages.js')
        assert run_state_at < messages_at, (
            "run_state.js must load before messages.js so HermesRunState exists when "
            "send() handles a 409"
        )

    def test_shell_asset_precached(self):
        assert "'./static/run_state.js' + VQ" in SW_JS, (
            "run_state.js must be in SHELL_ASSETS or the PWA keeps the stale bundle"
        )

    def test_cache_version_bumped(self):
        """Adding a shell asset requires a fresh cache name + query suffix."""
        cache = re.search(r"const CACHE_NAME = '([^']+)'", SW_JS)
        vq = re.search(r"const VQ = '\?v=__WEBUI_VERSION__-([^']+)'", SW_JS)
        assert cache and vq, "sw.js must define CACHE_NAME and VQ"
        assert cache.group(1) == "hermes-shell-__WEBUI_VERSION__-" + vq.group(1), (
            "CACHE_NAME and VQ must share the same suffix"
        )
        assert "conflict" in vq.group(1) or "run-state" in vq.group(1), (
            f"the shell cache suffix must be bumped for this change, got {vq.group(1)!r}"
        )
