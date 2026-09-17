"""Bound event previews while retaining their attribution and incompleteness."""

from __future__ import annotations

import json
from typing import Any

MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
TRUNCATION_NOTICE = "\n\n[Content truncated: event payload exceeded 256 KiB.]"
_MARKERS = frozenset({"truncated", "original_payload_bytes", "truncation_notice"})
_IDENTITY = frozenset(
    {
        "id",
        "tool_use_id",
        "name",
        "phase",
        "is_error",
        "usage",
        "usage_id",
        "usage_scope",
        "native_item_id",
    }
)
_TEXT_FIELDS = frozenset({"text", "content", "output", "detail", "input"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _size(value: Any) -> int:
    return len(_json(value).encode("utf-8"))


def _preview(value: Any, byte_budget: int) -> str:
    """Fit a visibly truncated preview, counting JSON escapes and UTF-8 bytes."""
    text = value if isinstance(value, str) else _json(value)
    if text.endswith(TRUNCATION_NOTICE):
        text = text[: -len(TRUNCATION_NOTICE)]
    low, high = 0, min(len(text), byte_budget)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(text[:middle] + TRUNCATION_NOTICE) <= byte_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low] + TRUNCATION_NOTICE


def bound_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the complete JSON payload within 256 KiB, including its markers.

    Small events are unchanged. Oversized events keep identity/usage ahead of
    bulk content; large structured values become labelled text previews. A
    field is omitted only when the other fields leave no room for its preview.
    This bounds multi-field payloads and huge keys as well as single strings.
    """
    original_bytes = _size(payload)
    if original_bytes <= MAX_EVENT_PAYLOAD_BYTES:
        return payload
    bounded = dict(payload)
    bounded.update(
        truncated=True,
        original_payload_bytes=original_bytes,
        truncation_notice=TRUNCATION_NOTICE.strip(),
    )
    # Keep incompleteness visible even when the UI uses a different text field
    # or stores the assistant message without the rest of the event metadata.
    for key in _TEXT_FIELDS & bounded.keys():
        if isinstance(bounded[key], str):
            bounded[key] += TRUNCATION_NOTICE
    current_bytes = _size(bounded)
    entries = [
        (key, _size(key) + 2, _size(value)) for key, value in bounded.items() if key not in _MARKERS
    ]
    entries.sort(key=lambda entry: (entry[0] in _IDENTITY, -sum(entry[1:])))
    for key, key_bytes, value_bytes in entries:
        budget = MAX_EVENT_PAYLOAD_BYTES - (current_bytes - value_bytes)
        if budget >= _size(TRUNCATION_NOTICE):
            bounded[key] = _preview(bounded[key], budget)
            return bounded
        if key in _TEXT_FIELDS and value_bytes > _size(TRUNCATION_NOTICE):
            bounded[key] = TRUNCATION_NOTICE
            current_bytes -= value_bytes - _size(TRUNCATION_NOTICE)
        else:
            del bounded[key]
            # Markers ensure the object still has other fields: remove the
            # key/value pair and its comma-space separator.
            current_bytes -= key_bytes + value_bytes + 2
        if current_bytes <= MAX_EVENT_PAYLOAD_BYTES:
            return bounded
    return bounded
