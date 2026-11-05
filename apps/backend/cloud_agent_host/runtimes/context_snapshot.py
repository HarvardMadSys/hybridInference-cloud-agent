"""What a CLI's server lists for a turn's request, normalized to one bounded snapshot.

Only OpenCode and Kilo are observed natively, and only without touching the
turn: the platform's bridge reads the server's own message list and its tool
definitions for the turn's model over the server's loopback HTTP API, from a
thread of its own, as soon as the turn's prompt is accepted. What it reports
is what the turn's first request carries after the server's compaction filter
(``filterCompacted``) -- its system prompt is not exposed by the server -- and
it is emitted only while that turn is still active, so it can never follow
the turn's settlement or delay it (``deploy/sandbox/http-agent-live-bridge``).

Claude Code and pi answer a context question only over the conversation's own
stdin/stdout, and Claude's answer costs token-counting calls to the model
provider; neither is asked (see the design record, section 6).

Counts are estimated from the text (:mod:`cloud_agent_shared.text_tokens`) and
say so. A category the server does not expose is listed as not exposed, never
as zero. Entries carry a short preview and the identifiers that join them to
stored events, never whole content.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cloud_agent_shared.text_tokens import (
    ESTIMATE_BASIS,
    estimate_from_counts,
)

#: 2: read when the turn starts, for its first request (1 was read after it settled).
SNAPSHOT_VERSION = 2
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


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return (
        [item for item in value if isinstance(item, Mapping)][:400]
        if isinstance(value, list)
        else []
    )


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


# -- OpenCode and Kilo, through the bridge --------------------------------------


def bridge_snapshot(context: Any, *, runtime: str) -> dict[str, Any]:
    """Normalize the bridge's turn-start ``context`` report (OpenCode 1.18.9, Kilo 7.4.17)."""
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
        observed="turn_start",
    )


def bridge_report(value: Mapping[str, Any], *, runtime: str) -> dict[str, Any]:
    """The lifecycle payload for one bridge ``context`` line: a snapshot, or why not."""
    context = value.get("context")
    if isinstance(context, Mapping):
        snapshot = bridge_snapshot(context, runtime=runtime)
    else:
        snapshot = failed_snapshot(
            f"{runtime}_session_messages",
            str(value.get("status") or "unavailable")[:32],
            str(value.get("detail") or ""),
        )
    return {"phase": "context_snapshot", "snapshot": snapshot}
