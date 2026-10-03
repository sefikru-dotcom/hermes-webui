"""HTTP route for global session-list invalidation events."""

import queue


def handle_session_events_stream(
    handler,
    *,
    end_sse_headers,
    set_write_deadline,
    sse,
    disconnect_errors,
    heartbeat_interval,
    subscribe,
    unsubscribe,
):
    """Serve the global session-list SSE feed and release its subscription."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    end_sse_headers(handler)
    set_write_deadline(handler)

    q = subscribe()
    try:
        while True:
            try:
                event_data = q.get(timeout=heartbeat_interval)
            except queue.Empty:
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                continue
            sse(handler, event_data.get("type", "sessions_changed"), event_data)
    except disconnect_errors:
        pass
    finally:
        unsubscribe(q)
    return True
