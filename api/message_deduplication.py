"""Pure message identity and context de-duplication helpers.

The WebUI streaming module owns the Mars-specific helper policies and keeps
compatibility wrappers; this module only contains the reusable algorithms.
"""

import copy
import json


def deduplicate_context_messages(
    messages,
    *,
    is_context_compression_marker,
    message_replay_key,
    is_compressed_context_tool_result_summary_message,
    message_text,
):
    """Remove duplicate provider-context rows while preserving recovery rules."""
    if not messages:
        return messages
    seen = set()
    deduped = []
    user_exact_index = {}
    for msg in messages:
        if is_context_compression_marker(msg):
            marker_key = (
                "__context_compression_marker__",
                " ".join(message_text(msg.get("content", "")).split())[:500],
            )
            if marker_key in seen:
                continue
            seen.add(marker_key)
            if isinstance(msg, dict) and msg.get("role") != "assistant":
                msg = copy.deepcopy(msg)
                msg["role"] = "assistant"
            deduped.append(msg)
            continue
        if is_compressed_context_tool_result_summary_message(msg) and not msg.get("tool_call_id"):
            deduped.append(msg)
            continue
        key = message_replay_key(msg)
        if isinstance(msg, dict) and msg.get("role") == "user" and key is not None:
            user_exact_key = (
                key,
                msg.get("timestamp"),
                msg.get("_ts"),
                msg.get("_source") or "webui",
                json.dumps(msg.get("attachments") or [], sort_keys=True, ensure_ascii=False),
            )
            prior_exact_idx = user_exact_index.get(user_exact_key)
            if msg.get("_active_turn_token"):
                if prior_exact_idx is not None:
                    deduped[prior_exact_idx] = msg
                    continue
                if key in seen:
                    deduped.append(msg)
                    user_exact_index[user_exact_key] = len(deduped) - 1
                    continue
            elif prior_exact_idx is not None:
                continue
            user_exact_index[user_exact_key] = len(deduped)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        deduped.append(msg)
    return deduped


def message_identity(msg, *, message_text, strip_workspace_prefix):
    """Build the stable display/replay identity used by Mars streaming."""
    if not isinstance(msg, dict):
        return None
    role = str(msg.get("role") or "")
    text = message_text(msg.get("content", ""))
    if role == "user":
        text = strip_workspace_prefix(text, include_legacy=True)
    if not text and not msg.get("tool_call_id") and not msg.get("tool_calls"):
        if msg.get("_partial"):
            reasoning_key = " ".join(str(msg.get("reasoning") or "").split())[:200]
            return (role, "", "", "__partial__" + reasoning_key)
        return None
    return (
        role,
        " ".join(str(text or "").split())[:500],
        str(msg.get("tool_call_id") or ""),
        json.dumps(msg.get("tool_calls") or [], sort_keys=True, ensure_ascii=False),
    )

