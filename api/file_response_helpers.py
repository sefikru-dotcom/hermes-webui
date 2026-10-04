"""Anchored file responses, immutable ETag snapshots, and HTML previews."""

from __future__ import annotations

from pathlib import Path
import stat as _stat
import hashlib
import os
import re

def _open_file_read_fd(target: Path, anchor_root: Path | None=None, *, open_anchored_fd) -> int:
    if anchor_root is None:
        flags = os.O_RDONLY
        # On Windows, files are opened in text mode by default; O_BINARY
        # prevents CRLF translation that would corrupt binary media files.
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        return os.open(str(target), flags)
    return open_anchored_fd(anchor_root, target.resolve(), want_dir=False)


def _close_fd_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _bytes_etag(data: bytes) -> str:
    """Weak ETag from a content digest of the bytes that will be served.

    The bytes must be an immutable snapshot (e.g. pread or an in-memory copy)
    so the validator cannot diverge from the body under TOCTOU.
    """
    return 'W/"%s"' % hashlib.sha256(data).hexdigest()


def _etag_and_snapshot(
    fd,
    *,
    file_size: int,
    _ETAG_SIZE_CAP,
    _bytes_etag,
) -> tuple[str | None, bytes | None, int]:
    """Return (weak ETag, snapshot bytes, actual size) for files under the size cap.

    Uses os.lseek + looped os.read to grab an immutable snapshot in a cross-platform
    and short-read-safe manner. The snapshot bytes can be sent directly so the ETag
    and the body can never diverge under TOCTOU (the file may change on disk after read).

    Returns (None, None, file_size) for files above the cap or on unexpected I/O failure.
    Returns (None, None, actual_size) if the file was truncated mid-read (short snapshot).
    The caller must use actual_size (not the original file_size) for Content-Length/Range
    calculations to avoid header/body mismatch.
    """
    if file_size > _ETAG_SIZE_CAP:
        return None, None, file_size

    # Cross-platform: os.lseek + looped os.read instead of POSIX-only os.pread
    # Short reads are possible (interrupted, EOF from truncation), so we loop.
    os.lseek(fd, 0, os.SEEK_SET)
    data = b""
    remaining = file_size
    while remaining > 0:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:  # EOF reached (file was truncated)
            break
        data += chunk
        remaining -= len(chunk)

    actual_size = len(data)
    if actual_size == 0:
        return None, None, 0

    # If the file was truncated after fstat (short snapshot), return the actual
    # size but no ETag — caller will fall back to streaming without ETag.
    # Round-6 fix: keep the captured `data` so the body path serves the immutable
    # snapshot instead of re-reading the fd after headers are committed.
    # Content-Length derives from actual_size == len(data), so header/body match.
    if actual_size != file_size:
        return None, data, actual_size

    return _bytes_etag(data), data, actual_size


def _serve_file_bytes(
    handler,
    target: Path,
    mime: str,
    disposition: str,
    cache_control: str,
    *,
    csp: str | None=None,
    anchor_root: Path | None=None,
    download_name: str | None=None,
    _CLIENT_DISCONNECT_ERRORS,
    _ETAG_SIZE_CAP,
    _close_fd_quietly,
    _content_disposition_value,
    _etag_and_snapshot,
    _open_file_read_fd,
    _parse_range_header,
    _sanitize_error,
    _security_headers,
    bad,
    j,
    logging,
):
    """Serve a file with correct MIME/disposition and optional byte-range support.

    Supports conditional GET via If-None-Match (ETag) — when the ETag matches,
    the request is short-circuited with 304 so revalidating clients (e.g.
    `no-cache` responses) do not re-download unchanged files.

    ``download_name`` overrides the Content-Disposition filename (used when
    serving an immutable media snapshot whose on-disk name is a content digest).
    """
    fd = None
    try:
        fd = _open_file_read_fd(target, anchor_root)
        st = os.fstat(fd)
        file_size = st.st_size
    except PermissionError:
        _close_fd_quietly(fd)
        return bad(handler, "Permission denied", 403)
    except FileNotFoundError:
        _close_fd_quietly(fd)
        return j(handler, {"error": "not found"}, status=404)
    except ValueError as e:
        _close_fd_quietly(fd)
        return bad(handler, _sanitize_error(e), 403)
    except Exception:
        _close_fd_quietly(fd)
        return bad(handler, "Could not stat file", 500)

    try:
        # Pre-commit phase: ETag/snapshot computation and response selection.
        # OSError here is still convertible into a clean 500 because no
        # status line has been written yet. After end_headers() the response
        # is committed, so body-transmission errors must never call bad()
        # again (that would attempt a second send_response on a stream the
        # client may have already closed — see the body phase below).
        try:
            no_store = "no-store" in cache_control
            if no_store or file_size > _ETAG_SIZE_CAP:
                etag = None
                snapshot = None
                # actual_size stays as file_size for over-cap/no-store cases
            else:
                etag, snapshot, actual_size = _etag_and_snapshot(fd, file_size=file_size)
                # If the file was truncated mid-read (short snapshot), use the
                # actual read size for all subsequent calculations so
                # Content-Length/Range match the body we can actually send.
                # Round-6 fix: the captured bytes are kept, so the body path
                # serves the immutable snapshot instead of re-reading the fd.
                if actual_size != file_size:
                    file_size = actual_size  # reconcile to avoid header/body mismatch
        except OSError:
            return bad(handler, "Could not serve file", 500)

        # RFC 7232 §3.2: If-None-Match uses weak comparison (W/ prefixes ignored)
        # and "*" matches any existing resource. On match, GET/HEAD is
        # short-circuited with 304 — processed before Range since a matched
        # conditional request skips the entity entirely.
        if_none_match = handler.headers.get("If-None-Match", "")
        if if_none_match and etag is not None:
            current = etag[2:] if etag.startswith("W/") else etag
            matched = if_none_match.strip() == "*" or any(
                (c.strip()[2:] if c.strip().startswith("W/") else c.strip()) == current
                for c in if_none_match.split(",")
                if c.strip()
            )
            if matched:
                handler.send_response(304)
                handler.send_header("ETag", etag)
                handler.send_header("Cache-Control", cache_control)
                _security_headers(handler)
                handler.end_headers()
                return True

        byte_range = _parse_range_header(handler.headers.get("Range", ""), file_size)
        if handler.headers.get("Range") and byte_range is None:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", "0")
            _security_headers(handler)
            handler.end_headers()
            return True

        start, end = byte_range if byte_range else (0, max(0, file_size - 1))
        content_length = end - start + 1 if file_size else 0
        handler.send_response(206 if byte_range else 200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if etag is not None:
            handler.send_header("ETag", etag)
        if byte_range:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Content-Disposition", _content_disposition_value(disposition, download_name or target.name))
        if csp:
            # Sandboxed inline HTML must remain frameable for workspace previews;
            # X-Frame-Options: DENY would block the iframe before CSP sandbox applies.
            handler.send_header("Content-Security-Policy", csp)
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Referrer-Policy", "same-origin")
            handler.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(self), geolocation=(), clipboard-write=(self)",
            )
        else:
            _security_headers(handler)
        handler.end_headers()

        # Body transmission: the response is committed once end_headers()
        # returns, so attempting bad()/send_response again here would corrupt
        # the stream with a second status line. Client disconnects are normal
        # (tab close, network switch) — log at debug and stop, same contract
        # as _safe_write(). Never emit a 500 after headers are out.
        if content_length:
            try:
                if snapshot is not None:
                    handler.wfile.write(snapshot[start:start + content_length])
                else:
                    with os.fdopen(fd, "rb", closefd=True) as f:
                        fd = None
                        f.seek(start)
                        remaining = content_length
                        while remaining:
                            chunk = f.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            handler.wfile.write(chunk)
                            remaining -= len(chunk)
            except _CLIENT_DISCONNECT_ERRORS as exc:
                logging.getLogger("hermes.webui").debug(
                    "Client disconnected mid-response (%s): %s",
                    type(exc).__name__,
                    getattr(handler, "path", "?"),
                )
            except Exception as exc:
                # Post-commit fail-closed: the status line is already on the
                # wire, so ANY body-transmission error that is not a client
                # disconnect (EIO from a truncated read, a generic OSError,
                # PermissionError, ...) must still be contained here. Letting
                # it escape would reach Handler.do_GET's 500 path and emit a
                # SECOND status line after the committed 200/206, corrupting
                # the HTTP stream. Log at debug and stop — the client already
                # received its headers (and possibly a partial body).
                logging.getLogger("hermes.webui").debug(
                    "Body transmission error after commit (%s): %s",
                    type(exc).__name__,
                    getattr(handler, "path", "?"),
                )
        return True
    finally:
        _close_fd_quietly(fd)


def _html_preview_with_blank_base(raw: bytes) -> bytes:
    base = '<base target="_blank">'
    text = raw.decode("utf-8", errors="replace")
    if re.search(r"<head(?:\s[^>]*)?>", text, flags=re.IGNORECASE):
        text = re.sub(r"(<head\b[^>]*>)", r"\1" + base, text, count=1, flags=re.IGNORECASE)
    elif re.search(r"<!doctype[^>]*>", text, flags=re.IGNORECASE):
        text = re.sub(
            r"(<!doctype[^>]*>)",
            r"\1<head>" + base + "</head>",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        text = "<head>" + base + "</head>" + text
    return text.encode("utf-8")


def _serve_inline_html_preview(
    handler,
    target: Path,
    cache_control: str,
    *,
    csp: str,
    anchor_root: Path | None=None,
    _content_disposition_value,
    _html_preview_with_blank_base,
    _open_file_read_fd,
    _sanitize_error,
    bad,
    j,
):
    """Serve sandboxed workspace HTML preview with links targeting a new tab."""
    fd = None
    try:
        fd = _open_file_read_fd(target, anchor_root)
        with os.fdopen(fd, "rb", closefd=True) as f:
            fd = None
            body = _html_preview_with_blank_base(f.read())
    except PermissionError:
        return bad(handler, "Permission denied", 403)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    except ValueError as e:
        return bad(handler, _sanitize_error(e), 403)
    except Exception:
        return bad(handler, "Could not read file", 500)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Accept-Ranges", "none")
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("Content-Disposition", _content_disposition_value("inline", target.name))
    handler.send_header("Content-Security-Policy", csp)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "same-origin")
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(self), geolocation=(), clipboard-write=(self)",
    )
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _read_anchored_file_bytes(ws_root: Path, target: Path, *, MAX_FILE_BYTES, open_anchored_fd) -> bytes:
    fd = open_anchored_fd(ws_root, target, want_dir=False)
    with os.fdopen(fd, "rb", closefd=True) as fh:
        st = os.fstat(fh.fileno())
        if not _stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(f"Not a file: {target}")
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError(f"File too large ({st.st_size} bytes, max {MAX_FILE_BYTES})")
        return fh.read(MAX_FILE_BYTES + 1)
