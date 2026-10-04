"""Anchor activity scene normalization and hydration."""

from __future__ import annotations

import copy
import hashlib
import json
import re

_ANCHOR_ACTIVITY_SCENE_MAX_BYTES = 256_000
_ANCHOR_ACTIVITY_SCENE_MAX_ROWS = 1_000


def _anchor_scene_message_ref_digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

_ANCHOR_ACTIVITY_SCENE_MAX_BYTES = 256_000
_ANCHOR_ACTIVITY_SCENE_MAX_ROWS = 1_000


def _anchor_scene_message_ref_digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

_ANCHOR_SCENE_SETTLED_SNIPPET_CAP = 4000

def _sanitize_anchor_activity_scene(scene):
    if not isinstance(scene, dict):
        raise ValueError("scene must be an object")
    if str(scene.get("version") or "") != "activity_scene_v1":
        raise ValueError("scene.version must be activity_scene_v1")
    rows = scene.get("activity_rows")
    if not isinstance(rows, list):
        raise ValueError("scene.activity_rows must be a list")
    if len(rows) > _ANCHOR_ACTIVITY_SCENE_MAX_ROWS:
        raise ValueError("scene.activity_rows is too large")
    scene_copy = copy.deepcopy(scene)
    encoded = json.dumps(scene_copy, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > _ANCHOR_ACTIVITY_SCENE_MAX_BYTES:
        raise ValueError("scene payload is too large")
    return json.loads(encoded.decode("utf-8"))

def _anchor_scene_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _anchor_scene_message_index_from_request(body):
    if not isinstance(body, dict):
        return None
    message_index = _anchor_scene_int_or_none(body.get("message_index"))
    message_offset = _anchor_scene_int_or_none(body.get("message_offset"))
    message_window_index = _anchor_scene_int_or_none(body.get("message_window_index"))
    if (
        message_window_index is not None
        and message_offset is not None
        and message_offset > 0
        and (message_index is None or message_index == message_window_index)
    ):
        return message_window_index + message_offset
    return message_index

def _anchor_scene_candidate_matches_scene(candidate, scene) -> bool:
    if not isinstance(scene, dict):
        return True
    final_key = _anchor_scene_text_key(scene.get("final_answer") or "")
    if not final_key:
        return True
    candidate_key = _anchor_scene_text_key(_anchor_scene_message_text(candidate))
    if not candidate_key:
        return False
    if candidate_key == final_key:
        return True
    if len(final_key) >= 16 and final_key in candidate_key:
        return True
    if len(candidate_key) >= 16 and candidate_key in final_key:
        return True
    return _anchor_scene_text_has_long_overlap(candidate_key, final_key)

def _normalize_anchor_scene_message_ref(message_ref) -> str:
    ref = str(message_ref or "").strip()
    if not ref:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{64}", ref):
        return ref.lower()
    try:
        payload = json.loads(ref)
    except (TypeError, ValueError):
        return ref
    if not isinstance(payload, dict):
        return ref
    canonical = {
        "role": str(payload.get("role") or ""),
        "content": " ".join(str(payload.get("content") or "").split()),
        "timestamp": payload.get("timestamp") or "",
    }
    return _anchor_scene_message_ref_digest(canonical)

def _anchor_scene_records(session) -> dict:
    records = getattr(session, "anchor_activity_scenes", None)
    return records if isinstance(records, dict) else {}

def _anchor_scene_message_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or part.get("input_text") or ""))
            else:
                parts.append(str(part or ""))
        return "\n".join(parts)
    return str(content or "")

def _anchor_scene_content_text(part) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part or "")
    return str(
        part.get("text")
        or part.get("content")
        or part.get("input_text")
        or part.get("output_text")
        or part.get("thinking")
        or part.get("reasoning")
        or part.get("summary")
        or ""
    )

def _anchor_scene_content_visible_text(part) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part or "")
    part_type = str(part.get("type") or "")
    if part_type in ("thinking", "reasoning"):
        return ""
    content_text = part.get("content") if part_type in ("text", "input_text", "output_text") else ""
    return str(part.get("text") or part.get("input_text") or part.get("output_text") or content_text or "")

def _anchor_scene_message_has_content_tool_use(message) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "tool_use" for part in content
    )

def _anchor_scene_final_answer_text(message) -> str:
    if not _anchor_scene_message_has_content_tool_use(message):
        return _anchor_scene_message_text(message)
    content = message.get("content") if isinstance(message, dict) else []
    last_tool_index = -1
    for idx, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") == "tool_use":
            last_tool_index = idx
    tail_text = "\n".join(
        text
        for text in (_anchor_scene_content_visible_text(part) for part in content[last_tool_index + 1 :])
        if _anchor_scene_clean_text(text)
    )
    return tail_text if _anchor_scene_clean_text(tail_text) else ""

def _anchor_scene_message_reasoning_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning", "_reasoning", "reasoning_content", "thinking"):
        value = message.get(key)
        if not value:
            continue
        if isinstance(value, list):
            parts = []
            for part in value:
                if isinstance(part, dict):
                    parts.append(
                        str(
                            part.get("text")
                            or part.get("content")
                            or part.get("reasoning")
                            or part.get("summary")
                            or ""
                        )
                    )
                else:
                    parts.append(str(part or ""))
            return "\n".join(parts)
        if isinstance(value, dict):
            return str(
                value.get("text")
                or value.get("content")
                or value.get("reasoning")
                or value.get("summary")
                or ""
            )
        return str(value or "")
    return ""

def _anchor_scene_clean_text(value) -> str:
    return " ".join(str(value or "").split()).strip()

def _anchor_scene_text_key(value) -> str:
    return _anchor_scene_clean_text(value).lower()

def _anchor_scene_string_payload(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except Exception:
        return str(value)

def _anchor_scene_is_bounded_tool_body_preview(settled, full) -> bool:
    settled_text = _anchor_scene_string_payload(settled)
    full_text = _anchor_scene_string_payload(full)
    return bool(
        settled_text
        and full_text
        and len(full_text) > len(settled_text)
        and len(settled_text) >= _ANCHOR_SCENE_SETTLED_SNIPPET_CAP
        and full_text.startswith(settled_text)
    )

def _anchor_scene_row_looks_like_final_answer(row_text_key: str, final_key: str) -> bool:
    if not row_text_key or not final_key:
        return False
    if row_text_key == final_key:
        return True
    # #4587: align with the renderer's _anchorSceneProseMatchesFinalAnswer — a
    # prefix-like overlap only counts as "the final answer" when it's a
    # NEAR-complete match (ratio >= 0.9). A shorter intermediate-prose row that
    # is merely a prefix of the final answer is legitimate progress narration and
    # must be preserved in the persisted scene, not filtered out.
    if not (final_key.startswith(row_text_key) or row_text_key.startswith(final_key)):
        return False
    shorter = min(len(row_text_key), len(final_key))
    longer = max(len(row_text_key), len(final_key))
    return shorter >= 80 and longer > 0 and (shorter / longer) >= 0.9

def _anchor_scene_text_has_long_overlap(text_key: str, final_key: str) -> bool:
    if len(text_key) < 80 or len(final_key) < 80:
        return False
    shorter, longer = (text_key, final_key) if len(text_key) <= len(final_key) else (final_key, text_key)
    window = 64
    scan_limit = min(len(shorter), 1400)
    if scan_limit < window:
        return False
    for start in range(0, scan_limit - window + 1, 24):
        chunk = shorter[start : start + window].strip()
        if len(chunk) >= 48 and chunk in longer:
            return True
    text_tokens = set(re.findall(r"[a-z0-9_./:-]{3,}", text_key))
    final_tokens = set(re.findall(r"[a-z0-9_./:-]{3,}", final_key))
    if text_tokens and final_tokens:
        common = text_tokens & final_tokens
        shorter_count = min(len(text_tokens), len(final_tokens))
        if shorter_count >= 3 and len(common) >= min(5, shorter_count) and (len(common) / shorter_count) >= 0.5:
            return True
    text_compact = re.sub(r"[\s`*_#|\[\](){}<>.,;:!?，。；：！？、/\\-]+", "", text_key)
    final_compact = re.sub(r"[\s`*_#|\[\](){}<>.,;:!?，。；：！？、/\\-]+", "", final_key)
    if len(text_compact) >= 40 and len(final_compact) >= 40:
        text_grams = {text_compact[idx : idx + 4] for idx in range(0, len(text_compact) - 3)}
        final_grams = {final_compact[idx : idx + 4] for idx in range(0, len(final_compact) - 3)}
        common_grams = text_grams & final_grams
        shorter_grams = min(len(text_grams), len(final_grams))
        if shorter_grams and len(common_grams) >= 12 and (len(common_grams) / shorter_grams) >= 0.35:
            return True
    return False

def _anchor_scene_row_is_stale_token_answer(row, row_text_key: str, final_key: str) -> bool:
    if not isinstance(row, dict) or row.get("role") not in ("prose", "thinking"):
        return False
    source_type = str(row.get("source_event_type") or row.get("source") or "")
    if source_type != "token":
        return False
    return _anchor_scene_text_has_long_overlap(row_text_key, final_key)

def _anchor_scene_message_turn_duration(message):
    if not isinstance(message, dict):
        return None
    for key in ("_turnDuration", "_turn_duration", "turn_duration"):
        value = message.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return value
    return None

def _anchor_scene_tool_id(tool) -> str:
    if not isinstance(tool, dict):
        return ""
    return str(
        tool.get("tid")
        or tool.get("id")
        or tool.get("tool_call_id")
        or tool.get("tool_use_id")
        or tool.get("call_id")
        or ""
    ).strip()

def _anchor_scene_tool_name(tool) -> str:
    if not isinstance(tool, dict):
        return "tool"
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(tool.get("name") or tool.get("tool_name") or fn.get("name") or "tool").strip() or "tool"

def _anchor_scene_tool_args(tool):
    if not isinstance(tool, dict):
        return {}
    for key in ("args", "input"):
        value = tool.get(key)
        if isinstance(value, dict):
            return copy.deepcopy(value)
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    raw = fn.get("arguments")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}

def _anchor_scene_content_tool(part):
    if not isinstance(part, dict):
        return {}
    fn = part.get("function") if isinstance(part.get("function"), dict) else {}
    tool_id = (
        part.get("id")
        or part.get("tid")
        or part.get("tool_call_id")
        or part.get("tool_use_id")
        or part.get("call_id")
    )
    return {
        "id": tool_id,
        "tid": part.get("tid") or tool_id,
        "tool_call_id": part.get("tool_call_id"),
        "tool_use_id": part.get("tool_use_id"),
        "call_id": part.get("call_id"),
        "name": part.get("name") or part.get("tool_name") or fn.get("name") or "tool",
        "tool_name": part.get("tool_name"),
        "args": part.get("args"),
        "input": part.get("input"),
        "function": copy.deepcopy(part.get("function")) if isinstance(part.get("function"), dict) else None,
        "command": part.get("command") or part.get("raw_command") or part.get("original_command") or part.get("display_command"),
        "preview": part.get("preview") or part.get("summary"),
        "snippet": part.get("snippet") or part.get("result") or part.get("output"),
        "result": copy.deepcopy(part.get("result")),
        "output": copy.deepcopy(part.get("output")),
        "is_error": part.get("is_error"),
        "error": part.get("error"),
        "duration": part.get("duration"),
        "started_at": part.get("started_at"),
    }

def _anchor_scene_row_base(role, kind, source_event_type, order_index, message_index, stream_id=""):
    return {
        "row_id": f"hydrated:{stream_id or 'stream'}:{role}:{message_index}:{order_index}",
        "order_index": order_index,
        "kind": kind,
        "role": role,
        "display_hint": {
            "prose": "main_prose",
            "thinking": "collapsed_thinking",
            "tool": "tool_row",
            "terminal": "terminal_status_row",
        }.get(role, "activity_row"),
        "display_hints": {
            "compact_worklog": {
                "prose": "main_prose",
                "thinking": "collapsed_thinking",
                "tool": "tool_row",
                "terminal": "terminal_status_row",
            }.get(role, "activity_row"),
            "transparent_stream": "chronological_activity",
        },
        "source_event_type": source_event_type,
        "event_id": None,
        "local_id": None,
        "run_id": None,
        "stream_id": stream_id or None,
        "seq": order_index,
        "status": "completed",
        "created_at": None,
        "identity": {"event_id": None, "local_id": None, "run_id": None, "stream_id": stream_id or None, "seq": order_index},
        "group": {
            "group_key": f"assistant:{message_index}" if isinstance(message_index, int) else f"activity:{order_index}",
            "activity_burst_id": None,
            "activity_segment_seq": None,
            "assistant_msg_idx": message_index if isinstance(message_index, int) else None,
        },
        "text": "",
        "thinking": None,
        "tool_call_id": None,
        "tool": None,
        "payload": {"assistant_msg_idx": message_index if isinstance(message_index, int) else None},
    }

def _anchor_scene_prose_row(text, order_index, message_index, stream_id=""):
    row = _anchor_scene_row_base("prose", "process_prose", "settled_message", order_index, message_index, stream_id)
    row["text"] = str(text or "")
    row["payload"]["text"] = row["text"]
    return row

def _anchor_scene_thinking_row(text, order_index, message_index, stream_id=""):
    row = _anchor_scene_row_base("thinking", "reasoning", "reasoning", order_index, message_index, stream_id)
    row["text"] = str(text or "")
    preview = _anchor_scene_clean_text(text)
    row["thinking"] = {
        "text": row["text"],
        "preview": (preview[:177] + "...") if len(preview) > 180 else preview,
        "dedupe_key": f"thinking:{preview.lower()}" if preview else "",
    }
    row["payload"]["text"] = row["text"]
    return row

def _anchor_scene_tool_row(tool, order_index, message_index, stream_id=""):
    row = _anchor_scene_row_base("tool", "tool_completed", "tool_complete", order_index, message_index, stream_id)
    tid = _anchor_scene_tool_id(tool)
    name = _anchor_scene_tool_name(tool)
    args = _anchor_scene_tool_args(tool)
    preview = str((tool or {}).get("preview") or (tool or {}).get("summary") or "")
    snippet = str((tool or {}).get("snippet") or (tool or {}).get("result") or (tool or {}).get("output") or "")
    row["row_id"] = f"hydrated:{stream_id or 'stream'}:tool:{tid}" if tid else row["row_id"]
    row["tool_call_id"] = tid or None
    row["tool"] = {
        "id": tid or None,
        "name": name,
        "args": args,
        "preview": preview,
        "snippet": snippet,
        "result": copy.deepcopy((tool or {}).get("result")) if isinstance(tool, dict) else None,
        "output": copy.deepcopy((tool or {}).get("output")) if isinstance(tool, dict) else None,
        "done": True,
        "is_error": bool((tool or {}).get("is_error") or (tool or {}).get("error")),
        "duration": (tool or {}).get("duration") if isinstance(tool, dict) else None,
        "started_at": (tool or {}).get("started_at") if isinstance(tool, dict) else None,
        "signature": f"{name}|{tid}|{json.dumps(args, sort_keys=True, default=str)}",
    }
    row["payload"].update({"tid": tid, "id": tid, "name": name, "args": args, "preview": preview, "snippet": snippet})
    return row

def _anchor_scene_tool_row_id(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return str(
        row.get("tool_call_id")
        or tool.get("id")
        or tool.get("tid")
        or tool.get("tool_call_id")
        or tool.get("tool_use_id")
        or tool.get("call_id")
        or payload.get("tid")
        or payload.get("id")
        or ""
    ).strip()

def _anchor_scene_tool_row_name(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return str(tool.get("name") or payload.get("name") or "tool").strip().lower()

def _anchor_scene_tool_rows_have_compatible_names(existing, incoming) -> bool:
    existing_name = _anchor_scene_tool_row_name(existing)
    incoming_name = _anchor_scene_tool_row_name(incoming)
    return (
        not existing_name
        or not incoming_name
        or existing_name == "tool"
        or incoming_name == "tool"
        or existing_name == incoming_name
    )

def _anchor_scene_tool_row_args(row):
    if not isinstance(row, dict):
        return None
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    args = tool.get("args") if isinstance(tool.get("args"), dict) else payload.get("args")
    return args if isinstance(args, dict) and args else None

def _anchor_scene_object_contains_subset(base, subset) -> bool:
    if not isinstance(base, dict) or not isinstance(subset, dict):
        return False
    for key, value in subset.items():
        if key not in base:
            return False
        if json.dumps(base[key], sort_keys=True, separators=(",", ":")) != json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ):
            return False
    return True

def _anchor_scene_tool_rows_have_compatible_invocation(existing, incoming) -> bool:
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return False
    existing_tool = existing.get("tool") if isinstance(existing.get("tool"), dict) else {}
    incoming_tool = incoming.get("tool") if isinstance(incoming.get("tool"), dict) else {}
    existing_payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
    incoming_payload = incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}
    existing_command = str(existing_tool.get("command") or existing_payload.get("command") or "").strip()
    incoming_command = str(incoming_tool.get("command") or incoming_payload.get("command") or "").strip()
    if existing_command and incoming_command:
        return existing_command == incoming_command
    existing_args = _anchor_scene_tool_row_args(existing)
    incoming_args = _anchor_scene_tool_row_args(incoming)
    if not existing_args or not incoming_args:
        return False
    return _anchor_scene_object_contains_subset(
        existing_args,
        incoming_args,
    ) or _anchor_scene_object_contains_subset(incoming_args, existing_args)

def _anchor_scene_tool_row_has_invocation_evidence(row) -> bool:
    if not isinstance(row, dict):
        return False
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    command = str(tool.get("command") or payload.get("command") or "").strip()
    args = _anchor_scene_tool_row_args(row)
    return bool(command or args)

def _anchor_scene_tool_rows_can_name_match(existing, incoming) -> bool:
    if not _anchor_scene_tool_rows_have_compatible_names(existing, incoming):
        return False
    if _anchor_scene_tool_row_has_invocation_evidence(existing) and _anchor_scene_tool_row_has_invocation_evidence(incoming):
        return _anchor_scene_tool_rows_have_compatible_invocation(existing, incoming)
    return True

def _anchor_scene_tool_rows_have_different_explicit_ids(existing, incoming) -> bool:
    existing_id = _anchor_scene_tool_row_id(existing)
    incoming_id = _anchor_scene_tool_row_id(incoming)
    return bool(existing_id and incoming_id and existing_id != incoming_id)

def _anchor_scene_tool_row_started_at(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    value = tool.get("started_at")
    if value is None or value == "":
        value = payload.get("started_at")
    return str(value) if value is not None and value != "" else ""

def _anchor_scene_tool_rows_have_same_started_at(existing, incoming) -> bool:
    existing_started_at = _anchor_scene_tool_row_started_at(existing)
    incoming_started_at = _anchor_scene_tool_row_started_at(incoming)
    return bool(existing_started_at and incoming_started_at and existing_started_at == incoming_started_at)

def _anchor_scene_tool_row_body_text(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for value in (
        tool.get("snippet"),
        payload.get("snippet"),
        tool.get("output"),
        payload.get("output"),
        tool.get("result"),
        payload.get("result"),
        tool.get("preview"),
        payload.get("preview"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""

def _anchor_scene_tool_rows_have_compatible_body(existing, incoming) -> bool:
    existing_body = _anchor_scene_tool_row_body_text(existing)
    incoming_body = _anchor_scene_tool_row_body_text(incoming)
    return bool(
        existing_body
        and incoming_body
        and (
            existing_body == incoming_body
            or existing_body.startswith(incoming_body)
            or incoming_body.startswith(existing_body)
        )
    )

def _anchor_scene_matching_content_tool_row_index(
    rows,
    content_tool_indexes,
    incoming_row,
    ordinal,
    used_indexes,
    incoming_total=0,
    id_flexible_indexes=None,
):
    if not isinstance(rows, list) or not isinstance(content_tool_indexes, list) or not isinstance(incoming_row, dict):
        return None
    incoming_id = _anchor_scene_tool_row_id(incoming_row)
    for index in content_tool_indexes:
        if index in used_indexes or index < 0 or index >= len(rows):
            continue
        existing_id = _anchor_scene_tool_row_id(rows[index])
        if existing_id and incoming_id and existing_id == incoming_id:
            return index
    if len(content_tool_indexes) == 1 and incoming_total == 1:
        index = content_tool_indexes[0]
        if (
            index not in used_indexes
            and 0 <= index < len(rows)
            and _anchor_scene_tool_rows_can_name_match(rows[index], incoming_row)
        ):
            return index
    available_indexes = [
        index for index in content_tool_indexes if index not in used_indexes and 0 <= index < len(rows)
    ]
    if len(available_indexes) == 1:
        index = available_indexes[0]
        if incoming_total == 1 and _anchor_scene_tool_rows_can_name_match(rows[index], incoming_row):
            return index
        if _anchor_scene_tool_rows_have_compatible_names(
            rows[index],
            incoming_row,
        ) and _anchor_scene_tool_rows_have_compatible_invocation(rows[index], incoming_row):
            return index
    reusable_indexes = [
        index for index in content_tool_indexes if index in used_indexes and 0 <= index < len(rows)
    ]
    if len(reusable_indexes) == 1 and incoming_total == 1:
        index = reusable_indexes[0]
        existing_id = _anchor_scene_tool_row_id(rows[index])
        id_flexible = isinstance(id_flexible_indexes, set) and index in id_flexible_indexes
        if _anchor_scene_tool_rows_have_compatible_names(
            rows[index],
            incoming_row,
        ) and (
            (existing_id and incoming_id and existing_id == incoming_id)
            or (
                id_flexible
                and _anchor_scene_tool_rows_have_same_started_at(rows[index], incoming_row)
                and _anchor_scene_tool_rows_have_compatible_body(rows[index], incoming_row)
            )
        ) and _anchor_scene_tool_rows_have_compatible_invocation(rows[index], incoming_row):
            return index
    for index in content_tool_indexes:
        if index in used_indexes or index < 0 or index >= len(rows):
            continue
        existing_id = _anchor_scene_tool_row_id(rows[index])
        if not existing_id and not incoming_id and _anchor_scene_tool_rows_can_name_match(rows[index], incoming_row):
            return index
    return None

def _anchor_scene_content_rows(message, order_index, message_index, stream_id="", *, is_final_message=False):
    if not _anchor_scene_message_has_content_tool_use(message):
        return None
    rows = []
    content = message.get("content") if isinstance(message, dict) else []
    last_tool_index = -1
    for idx, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") == "tool_use":
            last_tool_index = idx
    for idx, part in enumerate(content):
        if not isinstance(part, dict):
            if is_final_message and idx > last_tool_index:
                continue
            text = _anchor_scene_content_text(part)
            if _anchor_scene_clean_text(text):
                rows.append(_anchor_scene_prose_row(text, order_index + len(rows), message_index, stream_id))
            continue
        part_type = part.get("type")
        if part_type in ("text", "input_text", "output_text"):
            if is_final_message and idx > last_tool_index and _anchor_scene_content_visible_text(part):
                continue
            text = _anchor_scene_content_text(part)
            if _anchor_scene_clean_text(text):
                rows.append(_anchor_scene_prose_row(text, order_index + len(rows), message_index, stream_id))
            continue
        if part_type in ("thinking", "reasoning"):
            text = _anchor_scene_content_text(part)
            if _anchor_scene_clean_text(text):
                rows.append(_anchor_scene_thinking_row(text, order_index + len(rows), message_index, stream_id))
            continue
        if part_type == "tool_use":
            rows.append(
                _anchor_scene_tool_row(
                    _anchor_scene_content_tool(part),
                    order_index + len(rows),
                    message_index,
                    stream_id,
                )
            )
    return rows

def _anchor_scene_row_key(row) -> str:
    if not isinstance(row, dict):
        return ""
    if row.get("role") == "tool":
        tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
        return "tool:" + str(
            row.get("tool_call_id")
            or tool.get("id")
            or tool.get("tid")
            or tool.get("tool_call_id")
            or tool.get("tool_use_id")
            or tool.get("call_id")
            or row.get("row_id")
            or ""
        )
    if row.get("role") in ("prose", "thinking"):
        return f"{row.get('role')}:{_anchor_scene_text_key(row.get('text'))}"
    if row.get("role") == "lifecycle":
        source_type = str(row.get("source_event_type") or row.get("source") or "")
        if source_type in ("compressing", "compressed"):
            return "lifecycle:compression"
    return f"{row.get('role') or row.get('kind')}:{row.get('source_event_type') or ''}:{row.get('status') or ''}:{row.get('row_id') or ''}"

def _anchor_scene_row_has_live_identity(row) -> bool:
    if not isinstance(row, dict):
        return False
    values = [row.get("row_id"), row.get("local_id"), row.get("event_id")]
    identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
    values.extend([identity.get("local_id"), identity.get("event_id")])
    if any(str(value or "").startswith("live-") for value in values):
        return True
    group = row.get("group") if isinstance(row.get("group"), dict) else {}
    has_stream_owner = bool(
        row.get("stream_id")
        or row.get("run_id")
        or identity.get("stream_id")
        or identity.get("run_id")
    )
    has_assistant_message_index = group.get("assistant_msg_idx") is not None
    return has_stream_owner and not has_assistant_message_index

def _anchor_scene_settle_live_running_row(row, *, has_settled_thinking: bool):
    if not isinstance(row, dict):
        return row
    role = row.get("role")
    if role not in ("thinking", "prose", "tool"):
        return row
    if str(row.get("status") or "").lower() != "running":
        return row
    if not _anchor_scene_row_has_live_identity(row):
        return row
    if role == "thinking" and has_settled_thinking:
        return None
    next_row = copy.deepcopy(row)
    next_row["status"] = "completed"
    payload = next_row.get("payload")
    if isinstance(payload, dict):
        payload["status"] = "completed"
        if role == "tool":
            payload["done"] = True
    tool = next_row.get("tool")
    if role == "tool" and isinstance(tool, dict):
        tool["done"] = True
    return next_row

def _complete_hydrated_anchor_scene(messages, scene, message_index, *, message_offset=0, tool_calls=None, stream_id="", _assistant_anchor_scene_message_ref):
    if not isinstance(messages, list) or not isinstance(scene, dict) or not isinstance(message_index, int):
        return scene
    local_final_idx = message_index - int(message_offset or 0)
    if local_final_idx < 0 or local_final_idx >= len(messages):
        return scene
    final_message = messages[local_final_idx]
    if not isinstance(final_message, dict) or final_message.get("role") != "assistant":
        return scene
    turn_start = -1
    for idx in range(local_final_idx - 1, -1, -1):
        message = messages[idx]
        if isinstance(message, dict) and message.get("role") == "user":
            turn_start = idx
            break
    message_final_answer = _anchor_scene_final_answer_text(final_message)
    scene_final_answer = scene.get("final_answer") if isinstance(scene.get("final_answer"), str) else ""
    final_answer = message_final_answer if _anchor_scene_clean_text(message_final_answer) else scene_final_answer
    final_key = _anchor_scene_text_key(final_answer)
    rows = []
    seen = {}

    def merge_duplicate_tool_row(existing, incoming, *, prefer_incoming_body=False):
        if not isinstance(existing, dict) or not isinstance(incoming, dict):
            return existing
        merged = copy.deepcopy(existing)
        merged_tool = merged.get("tool") if isinstance(merged.get("tool"), dict) else {}
        incoming_tool = incoming.get("tool") if isinstance(incoming.get("tool"), dict) else {}
        merged_payload = merged.get("payload") if isinstance(merged.get("payload"), dict) else {}
        incoming_payload = incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}

        def empty(value):
            return value is None or value == "" or value == {}

        def merge_missing_args(existing_args, incoming_args):
            if not isinstance(incoming_args, dict) or not incoming_args:
                return existing_args, False
            base = copy.deepcopy(existing_args) if isinstance(existing_args, dict) else {}
            changed = not isinstance(existing_args, dict)
            for key, value in incoming_args.items():
                if key not in base:
                    base[key] = copy.deepcopy(value)
                    changed = True
            return base, changed

        for key in ("snippet", "result", "output"):
            incoming_value = incoming_tool.get(key)
            if not empty(incoming_value) and (
                empty(merged_tool.get(key))
                or (
                    prefer_incoming_body
                    and _anchor_scene_is_bounded_tool_body_preview(merged_tool.get(key), incoming_value)
                )
            ):
                merged_tool[key] = copy.deepcopy(incoming_value)
            incoming_value = incoming_payload.get(key)
            if not empty(incoming_value) and (
                empty(merged_payload.get(key))
                or (
                    prefer_incoming_body
                    and _anchor_scene_is_bounded_tool_body_preview(merged_payload.get(key), incoming_value)
                )
            ):
                merged_payload[key] = copy.deepcopy(incoming_value)
        for key in ("preview", "command", "duration", "started_at"):
            incoming_value = incoming_tool.get(key)
            if not empty(incoming_value) and empty(merged_tool.get(key)):
                merged_tool[key] = copy.deepcopy(incoming_value)
            incoming_value = incoming_payload.get(key)
            if not empty(incoming_value) and empty(merged_payload.get(key)):
                merged_payload[key] = copy.deepcopy(incoming_value)
        merged_args, args_changed = merge_missing_args(merged_tool.get("args"), incoming_tool.get("args"))
        if args_changed:
            merged_tool["args"] = merged_args
        merged_payload_args, payload_args_changed = merge_missing_args(
            merged_payload.get("args"),
            incoming_payload.get("args"),
        )
        if payload_args_changed:
            merged_payload["args"] = merged_payload_args
        merged["tool"] = merged_tool
        merged["payload"] = merged_payload
        return merged

    def push(row, *, prefer_incoming_tool_body=False):
        if not isinstance(row, dict):
            return
        row = _anchor_scene_settle_live_running_row(
            row,
            has_settled_thinking=any(existing.get("role") == "thinking" for existing in rows),
        )
        if row is None or not isinstance(row, dict):
            return
        text_key = _anchor_scene_text_key(row.get("text"))
        if row.get("role") in ("prose", "thinking") and _anchor_scene_row_looks_like_final_answer(text_key, final_key):
            return
        if _anchor_scene_row_is_stale_token_answer(row, text_key, final_key):
            return
        key = _anchor_scene_row_key(row)
        if key and key in seen:
            if key.startswith("tool:"):
                index = seen[key]
                rows[index] = merge_duplicate_tool_row(
                    rows[index],
                    row,
                    prefer_incoming_body=prefer_incoming_tool_body,
                )
                return
            if key == "lifecycle:compression":
                index = seen[key]
                next_row = copy.deepcopy(row)
                next_row["order_index"] = index
                next_row["seq"] = index
                rows[index] = next_row
            return
        if key:
            seen[key] = len(rows)
        next_row = copy.deepcopy(row)
        next_row["order_index"] = len(rows)
        next_row["seq"] = len(rows)
        rows.append(next_row)

    order = 0
    content_tool_indexes_by_idx = {}
    used_content_tool_indexes_by_idx = {}
    id_flexible_content_tool_indexes_by_idx = {}
    for local_idx in range(turn_start + 1, local_final_idx + 1):
        message = messages[local_idx]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        absolute_idx = int(message_offset or 0) + local_idx
        text = _anchor_scene_message_text(message)
        content_rows = _anchor_scene_content_rows(
            message,
            order,
            absolute_idx,
            stream_id,
            is_final_message=local_idx == local_final_idx,
        )
        content_tool_indexes = []
        used_content_tool_indexes = set()
        id_flexible_content_tool_indexes = set()
        if content_rows:
            for row in content_rows:
                previous_len = len(rows)
                push(row)
                if row.get("role") == "tool" and len(rows) > previous_len:
                    content_tool_indexes.append(len(rows) - 1)
                order += 1
            if content_tool_indexes:
                content_tool_indexes_by_idx[absolute_idx] = content_tool_indexes
                used_content_tool_indexes_by_idx[absolute_idx] = used_content_tool_indexes
                id_flexible_content_tool_indexes_by_idx[absolute_idx] = id_flexible_content_tool_indexes
        elif _anchor_scene_clean_text(text):
            push(_anchor_scene_prose_row(text, order, absolute_idx, stream_id))
            order += 1
        reasoning = _anchor_scene_message_reasoning_text(message)
        if _anchor_scene_clean_text(reasoning) and _anchor_scene_text_key(reasoning) != _anchor_scene_text_key(text):
            push(_anchor_scene_thinking_row(reasoning, order, absolute_idx, stream_id))
            order += 1
        for key in ("tool_calls", "_partial_tool_calls"):
            calls = message.get(key)
            if isinstance(calls, list):
                for tool_ordinal, call in enumerate(calls):
                    row = _anchor_scene_tool_row(call, order, absolute_idx, stream_id)
                    content_match_index = _anchor_scene_matching_content_tool_row_index(
                        rows,
                        content_tool_indexes,
                        row,
                        tool_ordinal,
                        used_content_tool_indexes,
                        len(calls),
                        id_flexible_content_tool_indexes,
                    )
                    if content_match_index is not None:
                        if _anchor_scene_tool_rows_have_different_explicit_ids(
                            rows[content_match_index],
                            row,
                        ):
                            id_flexible_content_tool_indexes.add(content_match_index)
                        rows[content_match_index] = merge_duplicate_tool_row(rows[content_match_index], row)
                        incoming_key = _anchor_scene_row_key(row)
                        if incoming_key:
                            seen[incoming_key] = content_match_index
                        used_content_tool_indexes.add(content_match_index)
                        order += 1
                        continue
                    push(row)
                    order += 1
    external_tool_counts = {}
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        try:
            absolute_idx = int(call.get("assistant_msg_idx"))
        except (TypeError, ValueError):
            continue
        external_tool_counts[absolute_idx] = external_tool_counts.get(absolute_idx, 0) + 1
    external_tool_ordinals = {}
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        try:
            absolute_idx = int(call.get("assistant_msg_idx"))
        except (TypeError, ValueError):
            continue
        local_idx = absolute_idx - int(message_offset or 0)
        if not (turn_start < local_idx <= local_final_idx):
            continue
        row = _anchor_scene_tool_row(call, order, absolute_idx, stream_id)
        tool_ordinal = external_tool_ordinals.get(absolute_idx, 0)
        external_tool_ordinals[absolute_idx] = tool_ordinal + 1
        content_match_index = _anchor_scene_matching_content_tool_row_index(
            rows,
            content_tool_indexes_by_idx.get(absolute_idx, []),
            row,
            tool_ordinal,
            used_content_tool_indexes_by_idx.setdefault(absolute_idx, set()),
            external_tool_counts.get(absolute_idx, 0),
            id_flexible_content_tool_indexes_by_idx.setdefault(absolute_idx, set()),
        )
        if content_match_index is not None:
            if _anchor_scene_tool_rows_have_different_explicit_ids(rows[content_match_index], row):
                id_flexible_content_tool_indexes_by_idx.setdefault(absolute_idx, set()).add(content_match_index)
            rows[content_match_index] = merge_duplicate_tool_row(
                rows[content_match_index],
                row,
                prefer_incoming_body=True,
            )
            incoming_key = _anchor_scene_row_key(row)
            if incoming_key:
                seen[incoming_key] = content_match_index
            used_content_tool_indexes_by_idx[absolute_idx].add(content_match_index)
            order += 1
            continue
        push(row, prefer_incoming_tool_body=True)
        order += 1
    for row in scene.get("activity_rows") or []:
        if isinstance(row, dict) and row.get("role") != "terminal":
            push(row)
    for row in scene.get("activity_rows") or []:
        if isinstance(row, dict) and row.get("role") == "terminal":
            push(row)
    repaired = copy.deepcopy(scene)
    repaired["version"] = "activity_scene_v1"
    repaired["mode"] = repaired.get("mode") or "compact_worklog"
    repaired["final_answer"] = final_answer if _anchor_scene_clean_text(final_answer) else repaired.get("final_answer", "")
    repaired["final_message_ref"] = _assistant_anchor_scene_message_ref(final_message)
    if repaired.get("turn_duration") is None:
        duration = _anchor_scene_message_turn_duration(final_message)
        if duration is not None:
            repaired["turn_duration"] = duration
    repaired["activity_rows"] = rows
    identity = repaired.get("identity") if isinstance(repaired.get("identity"), dict) else {}
    identity = dict(identity)
    identity["source_message_refs"] = [
        _assistant_anchor_scene_message_ref(message)
        for message in messages[turn_start + 1 : local_final_idx + 1]
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    repaired["identity"] = identity
    return repaired

def _hydrate_anchor_activity_scenes(messages, records, *, message_offset=0, tool_calls=None, _assistant_anchor_scene_message_ref):
    if not isinstance(messages, list) or not isinstance(records, dict) or not records:
        return messages
    by_ref = {}
    by_index = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        scene = record.get("scene")
        if not isinstance(scene, dict):
            continue
        ref = str(record.get("message_ref") or key or "")
        if ref:
            by_ref[ref] = record
        try:
            idx = int(record.get("message_index"))
        except (TypeError, ValueError):
            idx = None
        if idx is not None:
            by_index[idx] = record
    out = list(messages)
    # Read-side ref-ambiguity guard (parity with the write-side
    # _find_anchor_scene_message, which returns None when a ref matches >1
    # message). If two assistant messages ever share a ref (byte-identical
    # whitespace-normalized content + identical _ts), attaching the same scene
    # to both would render duplicate worklog groups. Count ref occurrences and
    # fall through to the index-based match (which is positional, unambiguous)
    # for any ref that resolves to more than one assistant message.
    _ref_counts: dict[str, int] = {}
    for _m in messages:
        if isinstance(_m, dict) and _m.get("role") == "assistant":
            _r = _assistant_anchor_scene_message_ref(_m)
            if _r:
                _ref_counts[_r] = _ref_counts.get(_r, 0) + 1
    for local_idx, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        absolute_idx = int(message_offset or 0) + local_idx
        _msg_ref = _assistant_anchor_scene_message_ref(message)
        record = by_ref.get(_msg_ref) if _ref_counts.get(_msg_ref, 0) <= 1 else None
        if not record:
            candidate = by_index.get(absolute_idx)
            if candidate and _anchor_scene_candidate_matches_scene(message, candidate.get("scene") or {}):
                record = candidate
        if not record:
            continue
        scene = record.get("scene")
        if not isinstance(scene, dict):
            continue
        next_message = dict(message)
        stream_id = record.get("stream_id")
        next_message["_anchor_activity_scene"] = _complete_hydrated_anchor_scene(
            messages,
            scene,
            absolute_idx,
            message_offset=message_offset,
            tool_calls=tool_calls,
            stream_id=str(stream_id or ""),
            _assistant_anchor_scene_message_ref=_assistant_anchor_scene_message_ref,
        )
        if stream_id:
            next_message["_anchor_stream_id"] = str(stream_id)
        out[local_idx] = next_message
    return out

