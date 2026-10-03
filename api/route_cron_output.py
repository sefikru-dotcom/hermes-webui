"""Pure formatting helpers for cron output payloads."""

from __future__ import annotations

import re

CRON_OUTPUT_CONTENT_LIMIT = 8000
CRON_OUTPUT_HEADER_CONTEXT = 200


def response_marker_index(text: str) -> int:
    candidates = []
    for heading in ("## Response", "# Response"):
        if text.startswith(heading):
            candidates.append(0)
        idx = text.find(f"\n{heading}")
        if idx >= 0:
            candidates.append(idx + 1)
    return min(candidates) if candidates else -1


def content_window(text: str, limit: int = CRON_OUTPUT_CONTENT_LIMIT) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    response_idx = response_marker_index(text)
    if response_idx >= 0:
        header = text[:min(CRON_OUTPUT_HEADER_CONTEXT, response_idx)].rstrip()
        response = text[response_idx:].lstrip("\n")
        content = f"{header}\n...\n{response}" if header else response
        return content[:limit]
    return text[-limit:]


def usage_metadata(text: str) -> dict:
    head = text.split("## Response", 1)[0].split("# Response", 1)[0]
    usage: dict = {}

    def intish(value: str):
        cleaned = re.sub(r"[^0-9]", "", value or "")
        return int(cleaned) if cleaned else None

    def floatish(value: str):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", (value or "").replace(",", ""))
        return float(match.group(0)) if match else None

    for raw_line in head.splitlines():
        line = raw_line.strip()
        model_match = re.match(r"\*\*(?:Model|Model Used):\*\*\s*(.+)$", line, re.I)
        if model_match:
            usage["model"] = model_match.group(1).strip()
            continue
        provider_match = re.match(r"\*\*Provider:\*\*\s*(.+)$", line, re.I)
        if provider_match:
            usage["provider"] = provider_match.group(1).strip()
            continue
        cost_match = re.match(r"\*\*(?:Estimated cost|Cost):\*\*\s*(.+)$", line, re.I)
        if cost_match:
            cost = floatish(cost_match.group(1))
            if cost is not None:
                usage["estimated_cost_usd"] = cost
            continue
        duration_match = re.match(r"\*\*(?:Duration|Elapsed):\*\*\s*(.+)$", line, re.I)
        if duration_match:
            seconds = floatish(duration_match.group(1))
            if seconds is not None:
                usage["duration_seconds"] = seconds
            continue
        tokens_match = re.match(r"\*\*Tokens:\*\*\s*(.+)$", line, re.I)
        if tokens_match:
            value = tokens_match.group(1)
            input_match = re.search(r"([0-9][0-9,]*)\s*(?:input|in)\b", value, re.I)
            output_match = re.search(r"([0-9][0-9,]*)\s*(?:output|out)\b", value, re.I)
            total_match = re.search(r"([0-9][0-9,]*)\s*(?:total\s*)?tokens?\b", value, re.I)
            if input_match:
                usage["input_tokens"] = intish(input_match.group(1))
            if output_match:
                usage["output_tokens"] = intish(output_match.group(1))
            if total_match and "total_tokens" not in usage:
                usage["total_tokens"] = intish(total_match.group(1))

    if "total_tokens" not in usage:
        total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens"))
        if total:
            usage["total_tokens"] = total
    return usage


def snippet(text: str, limit: int = 600) -> str:
    lines = text.split("\n")
    response_idx = next((i for i, line in enumerate(lines) if line.startswith("## Response") or line.startswith("# Response")), -1)
    body = ("\n".join(lines[response_idx + 1:]) if response_idx >= 0 else "\n".join(lines)).strip()
    return body[:limit] or "(empty)"
