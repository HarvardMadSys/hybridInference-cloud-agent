"""What a CLI says its context holds, normalized to one bounded snapshot.

After a turn settles, a driver may ask its CLI what the conversation's context
now carries (see ``HarnessTurn.after_settle_events``). Each CLI answers in its
own shape; these functions turn each answer into one schema the control plane
stores as a ``context_snapshot`` lifecycle event:

- **Claude Code** answers ``get_context_usage`` with its own token meter (the
  ``/context`` view): system prompt, built-in and MCP tool definitions, memory
  files, skills, agents, and the messages split by kind and by tool. Counts
  are the CLI's (``token_basis = "cli_meter"``); it counts with the provider's
  token-counting endpoint when that answers and estimates otherwise. It names
  sources (files, tools, skills) but returns no content.
- **pi** answers ``get_messages`` with the agent's context messages, compaction
  summaries included, and ``get_session_stats`` with its own context estimate.
  Its system prompt and tool definitions are not exposed over RPC.
- **OpenCode and Kilo** are read by the platform's bridge from the server's
  message list, filtered the way the server filters compacted history, and
  its tool definitions for the session's model. Their system prompt is not
  exposed by the server.

Counts the CLI did not give are estimated from the text
(:mod:`cloud_agent_shared.text_tokens`) and say so. A category the CLI does not
expose is listed as not exposed, never as zero. Entries carry a short preview
and the identifiers that join them to stored events, never whole content.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from cloud_agent_shared.text_tokens import (
    ESTIMATE_BASIS,
    estimate_from_counts,
    estimate_text_tokens,
)

SNAPSHOT_VERSION = 1
#: Entries one snapshot keeps; the largest are kept and the rest counted.
MAX_SNAPSHOT_ENTRIES = 300
#: Characters of each entry's preview.
PREVIEW_CHARS = 160
#: Characters of a label (a path, a tool or skill name).
LABEL_CHARS = 200

#: Canonical categories, in display order.
CATEGORIES: tuple[str, ...] = (
    "system_prompt",
    "tool_definitions",
    "mcp_tools",
    "instructions",
    "skills",
    "agents",
    "injected",
    "compaction_summary",
    "user_messages",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
    "other",
)

_CLAUDE_CATEGORY = {
    "System prompt": "system_prompt",
    "System tools": "tool_definitions",
    "MCP tools": "mcp_tools",
    "Memory files": "instructions",
    "Skills": "skills",
    "Custom agents": "agents",
}
#: Claude's own rows that are space, not content.
_CLAUDE_NOT_CONTENT = frozenset({"Free space", "Autocompact buffer"})
_CLAUDE_MESSAGE_SPLIT = (
    ("userMessageTokens", "user_messages"),
    ("assistantMessageTokens", "assistant"),
    ("toolCallTokens", "tool_calls"),
    ("toolResultTokens", "tool_results"),
    ("attachmentTokens", "injected"),
    ("redirectedContextTokens", "injected"),
    ("unattributedTokens", "other"),
)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:
        return None
    return int(value)


def _label(value: Any) -> str:
    return str(value or "")[:LABEL_CHARS]


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


def _text_entry(
    category: str, label: str, text: str, *, ref: Mapping[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "category": category,
        "label": _label(label),
        "chars": len(text),
        "tokens": estimate_text_tokens(text),
        "token_basis": ESTIMATE_BASIS,
        "preview": _preview(text),
    }
    if ref:
        entry["ref"] = {key: str(value)[:256] for key, value in ref.items() if value}
    entry.update(extra)
    return entry


def _finish(
    *,
    source: str,
    token_basis: str,
    entries: list[dict[str, Any]],
    categories: dict[str, dict[str, Any]],
    total_tokens: int | None,
    context_window: int | None,
    not_exposed: Iterable[str] = (),
    **extra: Any,
) -> dict[str, Any]:
    """Bound the entries and lay the categories out in display order."""
    for category in not_exposed:
        categories.setdefault(category, {"tokens": None, "exposed": False})
    kept = sorted(entries, key=lambda entry: -(entry.get("tokens") or 0))[:MAX_SNAPSHOT_ENTRIES]
    ordered = [
        {"key": key, **categories[key]}
        for key in (*CATEGORIES, *sorted(set(categories) - set(CATEGORIES)))
        if key in categories
    ]
    return {
        "v": SNAPSHOT_VERSION,
        "source": source,
        "status": "ok",
        "token_basis": token_basis,
        "total_tokens": total_tokens,
        "context_window": context_window,
        "categories": ordered,
        "entries": kept,
        "entries_total": len(entries),
        "entries_omitted": max(0, len(entries) - len(kept)),
        **extra,
    }


def failed_snapshot(source: str, status: str, detail: str = "") -> dict[str, Any]:
    """A snapshot that was asked for and not obtained, saying why."""
    report: dict[str, Any] = {"v": SNAPSHOT_VERSION, "source": source, "status": status}
    if detail:
        report["detail"] = detail[:300]
    return report


# -- Claude Code ------------------------------------------------------------


def _claude_entries(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def add(category: str, label: Any, tokens: Any, source: Any = None, **extra: Any) -> None:
        entry: dict[str, Any] = {
            "category": category,
            "label": _label(label),
            "tokens": _count(tokens),
            "token_basis": "cli_meter",
        }
        if source:
            entry["source"] = _label(source)
        entry.update(extra)
        entries.append(entry)

    for item in _dicts(response.get("memoryFiles")):
        add("instructions", item.get("path"), item.get("tokens"), item.get("type"))
    for item in _dicts(response.get("mcpTools")):
        loaded = item.get("isLoaded")
        add(
            "mcp_tools",
            item.get("name"),
            item.get("tokens"),
            item.get("serverName"),
            **({"deferred": True} if loaded is False else {}),
        )
    for item in _dicts(response.get("systemTools")):
        add("tool_definitions", item.get("name"), item.get("tokens"), "built-in")
    for item in _dicts(response.get("deferredBuiltinTools")):
        add("tool_definitions", item.get("name"), item.get("tokens"), "built-in", deferred=True)
    for item in _dicts(response.get("systemPromptSections")):
        add("system_prompt", item.get("name"), item.get("tokens"))
    for item in _dicts(response.get("agents")):
        add("agents", item.get("agentType"), item.get("tokens"), item.get("source"))
    skills = response.get("skills")
    if isinstance(skills, Mapping):
        for item in _dicts(skills.get("skillFrontmatter")):
            add("skills", item.get("name"), item.get("tokens"), item.get("source"))
    breakdown = response.get("messageBreakdown")
    if isinstance(breakdown, Mapping):
        for item in _dicts(breakdown.get("toolCallsByType")):
            add("tool_calls", item.get("name"), item.get("callTokens"))
            add("tool_results", item.get("name"), item.get("resultTokens"))
        for item in _dicts(breakdown.get("attachmentsByType")):
            add("injected", item.get("name"), item.get("tokens"))
    return entries


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return (
        [item for item in value if isinstance(item, Mapping)][:400]
        if isinstance(value, list)
        else []
    )


def claude_snapshot(response: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a ``get_context_usage`` answer (pinned 2.1.220)."""
    categories: dict[str, dict[str, Any]] = {}
    breakdown = response.get("messageBreakdown")
    for item in _dicts(response.get("categories")):
        name = str(item.get("name") or "")
        if name in _CLAUDE_NOT_CONTENT:
            continue
        if name == "Messages" and isinstance(breakdown, Mapping):
            continue
        key = _CLAUDE_CATEGORY.get(name, "other" if name != "Messages" else "messages")
        known = categories.setdefault(key, {"tokens": 0, "exposed": True, "labels": []})
        known["tokens"] += _count(item.get("tokens")) or 0
        known["labels"].append(_label(name))
    if isinstance(breakdown, Mapping):
        for field, key in _CLAUDE_MESSAGE_SPLIT:
            tokens = _count(breakdown.get(field))
            if tokens:
                known = categories.setdefault(key, {"tokens": 0, "exposed": True, "labels": []})
                known["tokens"] += tokens
    usage = response.get("apiUsage")
    api_usage = (
        {
            key: _count(usage.get(key))
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            )
        }
        if isinstance(usage, Mapping)
        else None
    )
    return _finish(
        source="claude_get_context_usage",
        token_basis="cli_meter",
        entries=_claude_entries(response),
        categories=categories,
        total_tokens=_count(response.get("totalTokens")),
        context_window=_count(response.get("maxTokens")) or None,
        auto_compact_threshold=_count(response.get("autoCompactThreshold")),
        model=_label(response.get("model")) or None,
        api_usage=api_usage,
    )


# -- pi -----------------------------------------------------------------------


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, Mapping) and block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _pi_assistant(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    response = message.get("responseId")
    ref = {"usage_id": response} if isinstance(response, str) and response else None
    entries = []
    for block in message.get("content") or []:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            entries.append(
                _text_entry("assistant", "assistant", str(block.get("text") or ""), ref=ref)
            )
        elif kind == "thinking":
            entries.append(
                _text_entry("reasoning", "thinking", str(block.get("thinking") or ""), ref=ref)
            )
        elif kind == "toolCall":
            arguments = json.dumps(block.get("arguments"), ensure_ascii=False, default=str)
            entries.append(
                _text_entry(
                    "tool_calls",
                    str(block.get("name") or "tool"),
                    arguments,
                    ref={"tool_use_id": block.get("id"), **(ref or {})},
                )
            )
    return entries


def _pi_message(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    if role == "assistant":
        return _pi_assistant(message)
    if role == "user":
        return [_text_entry("user_messages", "user", _blocks_text(message.get("content")))]
    if role == "toolResult":
        return [
            _text_entry(
                "tool_results",
                str(message.get("toolName") or "tool"),
                _blocks_text(message.get("content")),
                ref={"tool_use_id": message.get("toolCallId")},
                **({"is_error": True} if message.get("isError") else {}),
            )
        ]
    if role == "bashExecution":
        text = f"{message.get('command') or ''}\n{message.get('output') or ''}"
        return [_text_entry("tool_results", "bash (user)", text)]
    if role in {"compactionSummary", "branchSummary"}:
        return [_text_entry("compaction_summary", str(role), str(message.get("summary") or ""))]
    if role == "custom":
        return [
            _text_entry(
                "injected",
                str(message.get("customType") or "custom"),
                _blocks_text(message.get("content")),
            )
        ]
    return [_text_entry("other", str(role or "message"), _blocks_text(message.get("content")))]


def _sum_categories(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for entry in entries:
        known = categories.setdefault(
            entry["category"], {"tokens": 0, "chars": 0, "entries": 0, "exposed": True}
        )
        known["tokens"] += entry.get("tokens") or 0
        known["chars"] += entry.get("chars") or 0
        known["entries"] += 1
    return categories


def pi_snapshot(messages: Any, stats: Any) -> dict[str, Any]:
    """Normalize pi's ``get_messages`` and ``get_session_stats`` answers (pinned 0.73.1)."""
    entries = [
        entry
        for message in (messages if isinstance(messages, list) else [])
        if isinstance(message, Mapping)
        for entry in _pi_message(message)
    ]
    usage = stats.get("contextUsage") if isinstance(stats, Mapping) else None
    usage = usage if isinstance(usage, Mapping) else {}
    report = _finish(
        source="pi_rpc",
        token_basis=ESTIMATE_BASIS,
        entries=entries,
        categories=_sum_categories(entries),
        total_tokens=_count(usage.get("tokens")),
        context_window=_count(usage.get("contextWindow")) or None,
        not_exposed=("system_prompt", "tool_definitions"),
        total_basis="cli_estimate",
        messages=len(messages) if isinstance(messages, list) else 0,
        messages_read=isinstance(messages, list),
    )
    if not isinstance(messages, list):
        # Only pi's own total and window: the messages were not read.
        report["status"] = "partial"
        report["detail"] = "messages_not_read"
    return report


# -- OpenCode and Kilo, through the bridge --------------------------------------


def bridge_snapshot(context: Any, *, runtime: str) -> dict[str, Any]:
    """Normalize the bridge's ``context_result`` (OpenCode 1.18.9, Kilo 7.4.17)."""
    context = context if isinstance(context, Mapping) else {}
    entries = []
    for item in _dicts(context.get("entries")):
        counts = [_count(item.get(key)) or 0 for key in ("ascii", "cjk", "other")]
        entry: dict[str, Any] = {
            "category": str(item.get("category") or "other")
            if str(item.get("category") or "") in CATEGORIES
            else "other",
            "label": _label(item.get("label")),
            "chars": _count(item.get("chars")) or 0,
            "tokens": estimate_from_counts(*counts),
            "token_basis": ESTIMATE_BASIS,
            "preview": _preview(str(item.get("preview") or "")),
        }
        ref = item.get("ref")
        if isinstance(ref, Mapping):
            entry["ref"] = {str(k)[:32]: str(v)[:256] for k, v in ref.items() if v}
        for flag in ("pruned", "synthetic", "is_error"):
            if item.get(flag) is True:
                entry[flag] = True
        entries.append(entry)
    excluded = context.get("excluded")
    return _finish(
        source=f"{runtime}_session_messages",
        token_basis=ESTIMATE_BASIS,
        entries=entries,
        categories=_sum_categories(entries),
        total_tokens=None,
        context_window=None,
        not_exposed=("system_prompt",),
        excluded={
            key: _count(value) or 0
            for key, value in (excluded.items() if isinstance(excluded, Mapping) else [])
        },
        messages=_count(context.get("messages")) or 0,
        provider=_label(context.get("provider")) or None,
    )
