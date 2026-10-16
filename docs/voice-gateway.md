# Voice gateway — `GET /api/voice/session` (WebSocket)

Transport adapter that lets a voice client (browser or native app) talk to the
**existing** chat runtime over a WebSocket instead of SSE. It owns no chat
state: it opens a loopback call to `/api/chat/start` + `/api/chat/stream` under
the *same* identity and the *same* `session_id`, and translates the chat SSE
events into text WebSocket frames.

Implemented in `api/voice_gateway.py`; routed from `api/routes.py` (`handle_get`),
allow-listed in `api/auth.py` (`NATIVE_BEARER_EXACT_PATHS`).

## Handshake

```
GET /api/voice/session?session_id=<chat session id> HTTP/1.1
Host: <host>
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: <base64 16 bytes>
Sec-WebSocket-Version: 13
Optional: Sec-WebSocket-Protocol: hermes.voice.v1
Authorization: Bearer <device token>      # or the WebUI session cookie
```

* `101 Switching Protocols` with `Sec-WebSocket-Accept = base64(sha1(key + GUID))`
  (RFC 6455 §1.3); the subprotocol `hermes.voice.v1` is echoed only when the
  client offered it (`api/voice_gateway.py` — `VOICE_PROTOCOL`).
* A plain `GET` (no `Upgrade`) answers `400` + JSON — never `404`, and never a
  `101`-less hang.
* Authentication is identical to the text chat: the device Bearer token is
  validated by `api/auth.py` for this exact path, a browser cookie session also
  works, and a browser-cookie upgrade additionally requires a same-origin
  `Origin`/`Referer` (CSRF equivalent, `_check_same_origin_browser_request`).
  Failures answer `401`/`403` as ordinary JSON — no WebSocket frames.
* The socket only ever speaks what the chat runtime already allowed: approvals,
  confirmations and dangerous-tool gates are unchanged, because the turn runs
  through `/api/chat/start`. The gateway never calls the agent directly.

## Binding to an existing session

`session_id` comes from the query string. Every turn is started with exactly
that value, so the voice connection continues the very conversation the text
chat is on — same transcript, same memory, same approvals.

If `session_id` is omitted the first turn behaves like text chat with an empty
`session_id`: the runtime creates a session and reports it back, and the gateway
adopts it for the rest of the connection (`turn.started` / `agent.finished`
carry the new id). A `user.text` frame may also carry its own `session_id` to
switch conversations without reconnecting, subject to the same runtime
visibility rules as text chat.

## Event contract (all frames are UTF-8 JSON text)

Every frame: `{"type": ..., "v": 1, "seq": <int>, ...}`. `seq` increases by one
per frame, per connection. `session_id` (and `turn_id`, once known) is present
on every frame that belongs to a turn.

Client → server:

| frame | fields | meaning |
|---|---|---|
| `user.text` | `text` (required), `session_id` (optional) | run one turn with this utterance |
| `cancel` | — | cancel the turn currently in flight |
| `ping` | `id` (optional) | liveness probe; answered with `pong` |
| `user.audio` | — | reserved: rejected with `agent.error` `audio_not_supported` |
| `close` | — | graceful close (the server also accepts a WebSocket close frame) |

Server → client:

| frame | fields | meaning |
|---|---|---|
| `session.ready` | `protocol`, `session_id`, `server_events`, `client_events`, `respond_via`, `keepalive_seconds` | sent once, immediately after the upgrade |
| `turn.started` | `turn_id`, `session_id`, `stream_id` | the runtime accepted the turn |
| `agent.text.delta` | `text` | assistant text chunk (the answer is their concatenation) |
| `agent.text.interim` | `text` | interim assistant text |
| `agent.reasoning.delta` | `text` | reasoning/thinking chunk |
| `agent.tool` | `name`, `tool`, `data` | a tool started |
| `agent.tool.finished` | `name`, `tool`, `data` | a tool finished |
| `agent.approval.request` | `data` | a dangerous action needs the user's confirmation |
| `agent.clarify.request` | `data` | the agent asked a question |
| `agent.status` | `phase`, `text` | lifecycle note (`cancelling`, …) |
| `agent.error` | `code`, `message`, `status`, `data` | non-terminal problem report |
| `agent.finished` | `reason`, `text`, `usage` | **terminal** — exactly one per turn |
| `pong` | `id` | answer to `ping` |
| `protocol.error` | `code`, `message` | framing violation; a close frame follows |

`reason` on `agent.finished` is one of `done`, `stream_end`, `cancelled`,
`error`, `stream_closed`. `agent.error` is informational: it explains what went
wrong, and the turn still ends with its single `agent.finished`.

Unknown client frame types are answered with `agent.error`
`code=unknown_frame_type`. Text frames must be masked (RFC 6455 §5.1) — an
unmasked client frame is answered with `protocol.error` and close `1002`.

## Loopback hop

The adapter opens `POST /api/chat/start` and `GET /api/chat/stream` against
`127.0.0.1:<the port this request arrived on>`, forwarding the caller's
`Authorization`/`Cookie` headers. No credentials are minted, cached or logged.
The chat runtime therefore enforces auth, session visibility and approvals
exactly as it does for the WebUI.

## Manual check

```bash
HERMES_WEBUI_STATE_DIR=/tmp/voice-smoke HERMES_WEBUI_PASSWORD=voice-smoke-local-password \
  /usr/local/lib/hermes-agent/venv/bin/python scripts/voice-gateway-smoke.py
```

Runs a real server on a free port out of a scratch state dir: a text turn
creates the session, then the voice socket binds to that same `session_id` and
recalls the code word from it. Client-side framing in the smoke script is
independent of `api/voice_gateway.py` on purpose.

Hermetic tests: `tests/test_voice_gateway.py` (framing, SSE decoder, event
translator — pure logic) and `tests/test_voice_gateway_endpoint.py` (real
server, real Bearer, real socket).
