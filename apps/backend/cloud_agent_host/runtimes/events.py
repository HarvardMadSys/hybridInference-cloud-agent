"""Normalized event vocabulary and bounded text and harness projections."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from cloud_agent_shared.event_payloads import bound_event_payload

# Normalized event kinds. Kept in lockstep with agent_job_events.event_type and
# the frontend's types.ts; anything else must be reported as ``raw``.
THINKING = "thinking"
MESSAGE = "message"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
DIFF = "diff"
USAGE = "usage"
ERROR = "error"
LIFECYCLE = "lifecycle"
RAW = "raw"


@dataclass(frozen=True)
class NormalizedEvent:
    """One event in the shape the control plane and UI understand."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Bound the report before it crosses the sandbox manager or worker transport."""
        object.__setattr__(self, "payload", bound_event_payload(self.payload))


#: How many tool names one ``init`` event may republish. Claude Code ships a
#: menu of a few dozen and an MCP server adds more, so the bound sits far above
#: any real session. It exists because the alternative is unbounded: a CLI that
#: reported thousands would otherwise put the largest object in the stream on a
#: single lifecycle row. Overflow is reported rather than hidden — see
#: ``tools_truncated`` below.
MAX_INIT_TOOLS = 200


def _harness_inventory(event: dict[str, Any]) -> dict[str, Any] | None:
    """What an ``init`` event says the CLI started with, quoted not inferred.

    Claude Code composes its system prompt inside the CLI and never puts it on
    the wire, so there is no prompt here to republish. What the CLI does report
    is the configuration it actually started under: the tool menu it loaded, the
    MCP servers it reached, and the permission mode it ran in. The mode is the
    one a reader has a reason to check — the platform asks for
    ``bypassPermissions``, and the CLI's own copy of what it used is evidence
    the ask was honoured, which a flag in an argv is not.

    Returns ``None`` when the CLI reported none of it, so a lifecycle event
    carries an inventory only when there is one to carry.
    """
    tools, tools_truncated = _reported_tool_names(event.get("tools"))
    servers = _reported_mcp_servers(event.get("mcp_servers"))
    mode = event.get("permissionMode")
    permission_mode = mode.strip() if isinstance(mode, str) else ""
    if not tools and not servers and not permission_mode:
        return None
    inventory: dict[str, Any] = {}
    if tools:
        inventory["tools"] = tools
        if tools_truncated:
            inventory["tools_truncated"] = True
    if servers:
        inventory["mcp_servers"] = servers
    if permission_mode:
        inventory["permission_mode"] = permission_mode
    return inventory


def _reported_tool_names(value: Any) -> tuple[list[str], bool]:
    """The tool names an ``init`` event listed, in the order it listed them.

    Names are copied, never derived: a name the CLI did not send as a non-empty
    string names no tool this platform can vouch for. Repeats collapse because a
    menu is a set. The second value says whether the bound cut the list, so a
    short list is never mistaken for a short menu.
    """
    if not isinstance(value, list):
        return [], False
    seen: set[str] = set()
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names[:MAX_INIT_TOOLS], len(names) > MAX_INIT_TOOLS


def _reported_mcp_servers(value: Any) -> list[dict[str, Any]]:
    """The MCP servers an ``init`` event listed, with the status it gave each.

    A server the CLI named without a status keeps its name and reports no
    status. Which servers the platform meant to load is a different claim from
    which ones the CLI says it loaded, and only the second is evidence.
    """
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    servers: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        status = item.get("status")
        servers.append({"name": name, "status": status if isinstance(status, str) else None})
    return servers


def _truncate(value: Any, limit: int = 4000) -> str:
    """Render any payload fragment as bounded text."""
    return _payload_text(value)[:limit]


def _clipped(value: Any, limit: int = 4000) -> bool:
    """Whether ``_truncate`` would drop any of ``value``.

    A message that was clipped is not the model's whole answer, and the part
    that was dropped may be the part that matters — the goal report is at the
    *end* of a final message. The reader has to be able to tell that apart from
    a message that simply stopped, so the fact travels with the text.
    """
    return len(_payload_text(value)) > limit


def _payload_text(value: Any) -> str:
    """Render transcript content without silently losing its tail."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value


#: Models one result may report a context window for. A turn uses one model
#: and its delegated agents rarely more than one or two others; the bound only
#: keeps a malformed report from putting an unbounded object on a result row.
MAX_REPORTED_CONTEXT_WINDOWS = 8


def _token_count(value: Any) -> int | None:
    """A non-negative whole token count, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0 or value != int(value):  # NaN, negative, fraction
        return None
    return int(value)


def reported_compaction(
    *, trigger: Any, pre_tokens: Any = None, post_tokens: Any = None
) -> dict[str, Any]:
    """What a CLI said about one compaction, quoted rather than derived.

    The sizes are the CLI's own; a missing one stays missing, so a reader can
    tell "the CLI compacted and said nothing about size" from "it reported 0".
    """
    report: dict[str, Any] = {}
    if isinstance(trigger, str) and trigger.strip():
        report["trigger"] = trigger.strip()[:32]
    for key, value in (("pre_tokens", pre_tokens), ("post_tokens", post_tokens)):
        count = _token_count(value)
        if count is not None:
            report[key] = count
    return report


def reported_context_windows(model_usage: Any) -> dict[str, int] | None:
    """Each model's context window, as a result line reports it.

    Claude Code's result names every model the query used with the window it
    ran under (``modelUsage[model].contextWindow``). Only the window is kept:
    the per-model token counts there repeat what usage events already carry.
    """
    if not isinstance(model_usage, dict):
        return None
    windows: dict[str, int] = {}
    for model, usage in model_usage.items():
        if len(windows) >= MAX_REPORTED_CONTEXT_WINDOWS:
            break
        window = _token_count(usage.get("contextWindow")) if isinstance(usage, dict) else None
        if isinstance(model, str) and model and window:
            windows[model[:256]] = window
    return windows or None
