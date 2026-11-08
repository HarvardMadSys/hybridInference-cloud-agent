"""What a model request sends ahead of the conversation, measured as it is sent.

Every request a member's CLI makes carries, besides the conversation, a system
prompt and the schemas of the tools it offers. No CLI reports the size of
either and the platform stores neither, so the member's loopback service
measures them as each request passes -- in passthrough on a direct plan
(``cloud_agent_host.model_passthrough``), or as it translates one
(``cloud_agent_host.protocol_translator``) -- and writes a record on its CLI's
stderr, where the host reads it (``cloud_agent_host.request_prefixes``).

A record holds sizes, never text: for the system prompt and for the tools,
their characters and the ASCII and CJK counts the ``chars_cjk_v1`` estimate is
made from (:mod:`cloud_agent_shared.text_tokens`); each tool's name with the
same counts; and a fingerprint of the exact prefix. The meter
(``cloud_agent_host.protocol_translator.PrefixMeter``) writes a whole record
the first time it sees a fingerprint and a short repeat after that, so a long
turn adds a line per request and a few records in all.

What counts as the system prompt is what the CLI sends in its protocol's own
place for one, never what it puts among the conversation:

- Anthropic Messages: ``system``.
- Responses: ``instructions``, then the ``system`` and ``developer`` messages
  that open ``input``.
- Chat Completions: the ``system`` and ``developer`` messages that open
  ``messages``.

The tool schemas are ``tools``, each entry as the compact JSON it is sent as.
Both are estimates of what a provider counts, which renders them its own way.

Standard library only (Python 3.11): the member's loopback service imports it
from the helper zipapp the host installs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cloud_agent_shared.protocol_compat import ANTHROPIC_MESSAGES, CHAT_COMPLETIONS, RESPONSES
from cloud_agent_shared.text_tokens import split_counts

#: The record format; a reader ignores any other.
RECORD_VERSION = 1
#: Every record line starts with this. The leading ASCII record separator is
#: never the start of a line a CLI writes for a person to read.
RECORD_MARKER = "\x1emodel-request-prefix "
#: The longest record line a reader accepts, marker and newline included.
MAX_RECORD_BYTES = 64 * 1024
#: Tools one record names; the largest are kept and the rest counted.
MAX_TOOL_ENTRIES = 200
#: The largest request body the meter reads; a larger one is not measured.
MAX_MEASURED_BODY_BYTES = 16 * 1024 * 1024
#: Characters of a tool's name in a record.
LABEL_CHARS = 200
#: Characters of a model id in a record.
MODEL_CHARS = 256
#: How the service that measured the request reached its provider.
MODES = ("passthrough", "translated")
PROTOCOLS = (ANTHROPIC_MESSAGES, RESPONSES, CHAT_COMPLETIONS)
_FINGERPRINT_CHARS = 32
#: A model POST's path ends in its protocol's endpoint. ``/messages/count_tokens``
#: and ``/responses/compact`` do not: they are not requests the model answers.
_PATH_PROTOCOLS = (
    ("/chat/completions", CHAT_COMPLETIONS),
    ("/responses", RESPONSES),
    ("/messages", ANTHROPIC_MESSAGES),
)
_SYSTEM_ROLES = frozenset({"system", "developer"})


def protocol_for_path(path: str) -> str | None:
    """The model protocol a POST to ``path`` speaks, or None for any other endpoint."""
    path = path.split("?", 1)[0].rstrip("/")
    for suffix, protocol in _PATH_PROTOCOLS:
        if path.endswith(suffix):
            return protocol
    return None


def _texts(content: Any) -> list[str]:
    """The text of a message's content: a string, or the text of its parts."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]


def _leading_system(items: Any) -> list[Any]:
    """The ``system`` and ``developer`` messages a message list opens with."""
    leading: list[Any] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("role") not in _SYSTEM_ROLES:
            break
        leading.append(item)
    return leading


def system_prompt(protocol: str, body: dict[str, Any]) -> tuple[Any, list[str]]:
    """(the system prompt as sent, its texts) of one request ``body``."""
    if protocol == ANTHROPIC_MESSAGES:
        sent = body.get("system")
        return sent, _texts(sent)
    if protocol == RESPONSES:
        instructions = body.get("instructions")
        leading = _leading_system(body.get("input"))
        texts = [instructions] if isinstance(instructions, str) else []
        for message in leading:
            texts.extend(_texts(message.get("content")))
        return [instructions, leading], texts
    leading = _leading_system(body.get("messages"))
    return leading, [text for message in leading for text in _texts(message.get("content"))]


def _tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    for value in (
        tool.get("name"),
        function.get("name") if isinstance(function, dict) else None,
        tool.get("type"),
    ):
        if isinstance(value, str) and value:
            return value[:LABEL_CHARS]
    return "tool"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _counts(text: str) -> list[int]:
    """``[characters, ASCII, CJK]`` of ``text``."""
    ascii_chars, cjk, _ = split_counts(text)
    return [len(text), ascii_chars, cjk]


def fingerprint(system: Any, tools: Any) -> str:
    """Names one exact prefix: the system prompt and tools as sent, in order."""
    canonical = json.dumps(
        {"system": system, "tools": tools}, ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()[
        :_FINGERPRINT_CHARS
    ]


def measure(protocol: str, body: Any, *, mode: str) -> dict[str, Any] | None:
    """The whole record of one request's prefix; None for a body that is no model request."""
    if protocol not in PROTOCOLS or mode not in MODES or not isinstance(body, dict):
        return None
    sent, texts = system_prompt(protocol, body)
    tools = body.get("tools")
    tools = [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []
    system = [0, 0, 0]
    for text in texts:
        system = [a + b for a, b in zip(system, _counts(text), strict=True)]
    sized = sorted(
        ((_tool_name(tool), _counts(_compact(tool))) for tool in tools),
        key=lambda named: -named[1][0],
    )
    totals = [sum(counts[index] for _, counts in sized) for index in range(3)]
    entries: list[list[Any]] = [[name, *counts] for name, counts in sized]
    model = body.get("model")
    return {
        "v": RECORD_VERSION,
        "fp": fingerprint(sent, tools),
        "protocol": protocol,
        "mode": mode,
        "model": model[:MODEL_CHARS] if isinstance(model, str) and model else None,
        "system": {"chars": system[0], "ascii": system[1], "cjk": system[2], "parts": len(texts)},
        "tools": {"count": len(tools), "chars": totals[0], "ascii": totals[1], "cjk": totals[2]},
        "entries": entries[:MAX_TOOL_ENTRIES],
        "entries_omitted": max(0, len(entries) - MAX_TOOL_ENTRIES),
    }


def record_line(record: dict[str, Any]) -> bytes:
    """One record as the line the meter writes."""
    return (RECORD_MARKER + _compact(record) + "\n").encode("utf-8", "surrogatepass")


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _part(value: Any, keys: tuple[str, ...]) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    counts = {key: _whole(value.get(key)) for key in keys}
    if any(count is None for count in counts.values()):
        return None
    return {key: count for key, count in counts.items() if count is not None}


def _entry(value: Any) -> list[Any] | None:
    if not isinstance(value, list) or len(value) != 4 or not isinstance(value[0], str):
        return None
    counts = [_whole(count) for count in value[1:]]
    if any(count is None for count in counts):
        return None
    return [value[0][:LABEL_CHARS], *counts]


def read_record(line: str) -> dict[str, Any] | None:
    """A record from one stderr line, checked and bounded; None for anything else.

    A record that fails any check is not one: the line came from the CLI, and
    a CLI could write anything there.
    """
    if not line.startswith(RECORD_MARKER) or len(line.encode("utf-8", "replace")) > (
        MAX_RECORD_BYTES
    ):
        return None
    try:
        value = json.loads(line[len(RECORD_MARKER) :])
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("v") != RECORD_VERSION:
        return None
    fp = value.get("fp")
    if not isinstance(fp, str) or not 0 < len(fp) <= 64 or not fp.isalnum():
        return None
    if value.get("repeat") is True:
        return {"fp": fp, "repeat": True}
    return _whole_record(value, fp)


def _whole_record(value: dict[str, Any], fp: str) -> dict[str, Any] | None:
    system = _part(value.get("system"), ("chars", "ascii", "cjk", "parts"))
    tools = _part(value.get("tools"), ("count", "chars", "ascii", "cjk"))
    entries = value.get("entries")
    omitted = _whole(value.get("entries_omitted"))
    if (
        value.get("protocol") not in PROTOCOLS
        or value.get("mode") not in MODES
        or system is None
        or tools is None
        or omitted is None
        or not isinstance(entries, list)
        or len(entries) > MAX_TOOL_ENTRIES
    ):
        return None
    checked = [_entry(entry) for entry in entries]
    if any(entry is None for entry in checked):
        return None
    model = value.get("model")
    return {
        "fp": fp,
        "protocol": value["protocol"],
        "mode": value["mode"],
        "model": model[:MODEL_CHARS] if isinstance(model, str) and model else None,
        "system": system,
        "tools": tools,
        "entries": checked,
        "entries_omitted": omitted,
    }


__all__ = [
    "MAX_MEASURED_BODY_BYTES",
    "MAX_RECORD_BYTES",
    "MAX_TOOL_ENTRIES",
    "MODES",
    "PROTOCOLS",
    "RECORD_MARKER",
    "RECORD_VERSION",
    "fingerprint",
    "measure",
    "protocol_for_path",
    "read_record",
    "record_line",
    "system_prompt",
]
