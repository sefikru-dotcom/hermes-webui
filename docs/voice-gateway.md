# Voice gateway — live dialog transport (`hermes.voice.v1`)

Server-side transport for a live voice dialog: **SSE downstream + POST upstream**.
Implemented in `api/voice_session.py`; routes registered in `api/routes.py`.

## Why not WebSocket

The production server is a `ThreadingHTTPServer` + `BaseHTTPRequestHandler`
(`server.py:117`, `server.py:309`) and the runtime has neither `aiohttp` nor
`websockets`. A WS server inside `BaseHTTPRequestHandler` would be a large risk
to the live service, so the gateway uses the transports the server already
speaks. Semantics are the same: one long-lived server→client channel plus
short client→server calls.

The gateway is a **transport adapter only**. It never generates a turn: the
dialog still runs through `POST /api/chat/start` (and `POST /api/chat/steer`),
so the session, its memory and its approvals are the ordinary ones. There is no
"second agent".

## Endpoints

All four require the native **device Bearer** (`Authorization: Bearer <token>`),
the same credential `/api/tts` and `/api/transcribe` accept. No cookie, no CSRF.

| Method | Path | Auth set | Purpose |
|---|---|---|---|
| POST | `/api/voice/session` | `NATIVE_BEARER_EXACT_PATHS` | create/attach the voice channel |
| GET | `/api/voice/stream?voice_id=…` | `NATIVE_BEARER_READONLY_EXACT_PATHS` (GET/HEAD only) | SSE downstream |
| POST | `/api/voice/interrupt` | `NATIVE_BEARER_EXACT_PATHS` | barge-in |
| POST | `/api/voice/close` | `NATIVE_BEARER_EXACT_PATHS` | tear the channel down |

### `POST /api/voice/session`

```json
{"session_id": "a21c0f4dd30c"}
```

`200` — exactly these three keys:

```json
{"voice_id": "v1_…", "session_id": "a21c0f4dd30c", "expires_in": 1800}
```

* `voice_id` is opaque. Do not parse it, do not persist it across app restarts as
  a long-term identity; it dies with its TTL.
* **Idempotent**: calling again for the same session returns the *same*
  `voice_id` (one voice channel per session). `expires_in` is refreshed.
* Errors: `400` (`session_id` missing/invalid), `404` (`unknown session`),
  `403` (session belongs to another profile), `401` (no/invalid Bearer).

### `GET /api/voice/stream?voice_id=…`

`Content-Type: text/event-stream`. Frame format:

```
event: agent.text.delta
id: v1_xxxx:42
data: {"text":"Привет, "}
```

`id` is `<voice_id>:<seq>` and is **strictly increasing** across a connection.
`retry: 2000` is sent first; a `: keepalive` comment arrives every ~5 s.

Only **one** stream per voice channel: a second concurrent GET returns `409`
(`{"error":"voice channel already has an active stream"}`) — treat it as
"reconnect later", not fatal. A dropped socket frees the slot within one poll
interval (~150 ms), so a reconnect right after a blip normally succeeds.

Errors: `400` (no `voice_id`), `401`, `403` (foreign profile), `404`
(unknown or closed), `409`, `503` (too many concurrent streams server-side).

#### Resuming: `Last-Event-ID`

Send the `id` of the last frame you applied (header `Last-Event-ID`, or the
`last_event_id` query parameter). The server replays what you missed from the
**durable run journal**, so a reconnect after a tunnel drop loses nothing —
frames are not buffered in a process-local queue that a restart would wipe.
`session.ready` reports `resume_status`: `fresh`, `ok`, `cursor_ahead`,
`cursor_invalid`, `cursor_ignored`. Dedupe by `id`; ids are never reused.

### `POST /api/voice/interrupt`

```json
{"voice_id": "v1_…"}
```

Emits `speech.stop` on the stream and returns:

```json
{"ok": true, "voice_id": "v1_…", "interrupted": true, "already_interrupted": false}
```

Idempotent: repeating it is safe (`already_interrupted: true`) and never breaks
the channel. It does **not** cancel the agent's turn — a barge-in is a steer, so
if you want the turn to change course follow up with `POST /api/chat/steer`.

### `POST /api/voice/close`

```json
{"voice_id": "v1_…"}
```

`{"ok": true, "voice_id": "v1_…", "closed": true}`. Frees the channel; a later
`create` for the same session mints a new `voice_id`. Subsequent stream/interrupt
calls for the old id return `404`.

## Events

Fixed vocabulary — do not invent names, do not rename:

| Event | When | `data` |
|---|---|---|
| `session.ready` | first frame on every connection | `protocol`, `voice_id`, `session_id`, `server_events[]`, `keepalive_seconds`, `resume_requested`, `resume_status` |
| `transcript.partial` | interim user speech | `text`, `session_id`, `final:false` |
| `transcript.final` | user utterance settled | `text`, `session_id`, `final:true` |
| `agent.started` | the agent's turn began | `session_id`, `run_id`, `started_at` |
| `agent.text.delta` | one chunk of the answer | `text` |
| `tool.started` | a tool call began | `name`, optional `preview`, `tid` |
| `tool.finished` | that tool call ended | `name`, `tid`, `ok`/`error` |
| `agent.finished` | exactly one per run | `run_id`, `session_id`, `ok`, `usage` (no session dump) |
| `speech.stop` | barge-in | `reason`, `at`, `run_id`, `session_id` |

`transcript.*` reach the stream through the existing `POST /api/transcribe`
when the client includes a `session_id` form field with the audio; the
transcript response itself is unchanged.

## Client flow

1. `POST /api/voice/session {session_id}` → `voice_id`.
2. `GET /api/voice/stream?voice_id=…` (long-lived; keep it open).
3. User speaks → record → `POST /api/transcribe` (with `session_id`) → you get
   `transcript.final` on the stream → `POST /api/chat/start` as today.
4. Render `agent.text.delta` as it arrives (that is the "answer as it is
   generated" the push-to-talk flow could not give you).
5. User speaks over the answer → `POST /api/voice/interrupt` → stop playback on
   `speech.stop`; optionally `POST /api/chat/steer`.
6. On background/exit: `POST /api/voice/close`.
7. On any drop: reconnect with `Last-Event-ID` set to the last applied `id`.

## Ops

* State: `HERMES_WEBUI_VOICE_STATE_DIR` (default: `<state>/voice`) — registry
  `registry.json`, per-channel replay pointers. Mode `0700`/`0600`.
* Tunables: `HERMES_WEBUI_VOICE_TTL_SECONDS` (1800),
  `HERMES_WEBUI_VOICE_HEARTBEAT_SECONDS` (5),
  `HERMES_WEBUI_VOICE_POLL_SECONDS` (0.15),
  `HERMES_WEBUI_VOICE_MAX_STREAMS` (8),
  `HERMES_WEBUI_VOICE_STREAM_MAX_SECONDS` (3600).
* A sweeper drops expired channels; a closed socket frees its slot immediately.
* Logs never contain the Bearer token and only a masked `voice_id`
  (`v1_-N…ka`).
