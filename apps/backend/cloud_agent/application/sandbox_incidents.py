"""Sandbox incidents on the control plane: stored from heartbeats, then told.

A host reports what went wrong in its sandboxes with each heartbeat
(``cloud_agent_shared.sandbox_incidents``). This module stores those rows,
shows the ones that break a turn in the running turns they concern, and turns
what a chat has not been told into the note its next prompt carries. The
browser note is here too: it is the same kind of statement about the sandbox
the agent runs in. See ``docs/agents/specs/2026-11-04-sandbox-incidents.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent_shared.sandbox_incidents import (
    DROPPED_KIND,
    KINDS_SHOWN_TO_USER,
    KINDS_TOLD_TO_AGENT,
    REPEATED_SUFFIX,
    SandboxIncident,
    base_kind,
)
from cloud_agent_shared.shared_protocol import SANDBOX_BROWSER_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.ports.sandbox_incidents import SandboxIncidentRepository
    from cloud_agent.schemas.hosts import RetainedSandboxBeat

logger = get_logger(__name__)

#: The most incidents one prompt's note lists; the rest are counted.
MAX_TOLD = 5
#: The incident rows one note reads; the count covers the rest.
_TOLD_KINDS = sorted(KINDS_TOLD_TO_AGENT | {kind + REPEATED_SUFFIX for kind in KINDS_TOLD_TO_AGENT})

BROWSER_NOTE = (
    "[Platform note] This sandbox has a Chromium browser, shared with this chat's forks. "
    "`cloud-agent-browser start` prints its CDP WebSocket URL; any CDP client can drive it. "
    "Save screenshots and downloads under /workspace/.cloud-agent-artifacts, where the user "
    "sees them.\n\n"
)


def _row(incident: SandboxIncident) -> dict[str, Any]:
    """The event-table row of one reported incident: content-free details only."""
    details: dict[str, Any] = {
        "incident_id": incident.incident_id,
        "scope": incident.scope,
        "observed_at": datetime.fromtimestamp(incident.observed_at, UTC).isoformat(),
        "count": incident.count,
        **incident.details,
    }
    if incident.thread_id is not None:
        details["thread_id"] = incident.thread_id
    return {"event_type": f"incident.{incident.kind}", "details": details}


def _notice(row: dict[str, Any]) -> dict[str, Any] | None:
    """The ``lifecycle`` payload the chat shows for one stored row, if it shows one."""
    kind = row["event_type"].removeprefix("incident.")
    if kind not in KINDS_SHOWN_TO_USER:
        return None  # a summary of repeats, a drop count, or one nobody is shown
    details = row["details"]
    notice: dict[str, Any] = {"phase": "sandbox_incident", "incident": kind}
    if kind == "mcp_server_failed":
        notice["alias"] = details.get("alias")
        notice["server_phase"] = details.get("phase")
    return notice


async def record_heartbeat_incidents(
    store: SandboxIncidentRepository, host: str, retained: Sequence[RetainedSandboxBeat]
) -> bool:
    """Store each reported group's incidents, and show new ones in the turns they broke.

    Whether every row was stored, which is what the answer confirms: a host
    forgets its rows only on that confirmation, so a beat that stored some and
    failed on others has all of them sent again, and the stored ones are
    no-ops. It never raises into the heartbeat; diagnostics must never cost a
    host its liveness.
    """
    complete = True
    for group in retained:
        if not group.incidents:
            continue
        try:
            stored = await store.record_sandbox_incidents(
                host=host,
                sandbox_id=group.sandbox_id,
                generation=group.generation,
                incidents=[_row(incident) for incident in group.incidents],
            )
            for row in stored:
                notice = _notice(row)
                if notice is None:
                    continue
                thread_id = row["details"].get("thread_id")
                await store.notify_running_turns(
                    sandbox_id=group.sandbox_id,
                    thread_id=thread_id if isinstance(thread_id, str) else None,
                    payload=notice,
                )
        except Exception:
            complete = False
            logger.exception(
                "sandbox incidents could not be stored",
                extra={"event": "sandbox_incidents_failed", "sandbox_id": group.sandbox_id},
            )
    return complete


def _clock(details: dict[str, Any]) -> str:
    try:
        return datetime.fromisoformat(str(details.get("observed_at"))).strftime("%H:%M UTC")
    except ValueError:
        return "an unknown time"


def incident_line(event_type: str, details: dict[str, Any]) -> str:
    """One incident, as the agent's note says it."""
    kind = event_type.removeprefix("incident.")
    at = _clock(details)
    if kind.endswith(REPEATED_SUFFIX):
        times = int(details.get("count") or 1)
        return f"{incident_line('incident.' + base_kind(kind), details)} It happened {times} more time(s)."
    alias = f"`{details.get('alias')}`"
    return {
        "out_of_memory": f"The sandbox ran out of memory at {at}, and some processes were stopped.",
        "container_exited": (
            f"The sandbox stopped unexpectedly at {at} and was started again; "
            "processes that were running then are gone."
        ),
        "browser_crashed": f"The browser stopped at {at}; pages that were open are gone.",
        "mcp_server_failed": (
            f"The MCP server {alias} stopped unexpectedly at {at}."
            if details.get("phase") == "crashed"
            else f"The MCP server {alias} could not start at {at}."
        ),
        DROPPED_KIND: f"More incidents happened around {at} than were recorded.",
    }.get(kind, f"Something went wrong in the sandbox at {at}.")


@dataclass(frozen=True)
class SandboxNotes:
    """What a claim's prompt says about the sandbox, and the cursor it tells through."""

    browser: bool = False
    incident_lines: tuple[str, ...] = ()
    untold: int = 0
    through_id: int | None = None

    def incidents_text(self) -> str:
        """The incident note, prepended to the prompt; empty when there is nothing to tell."""
        if not self.incident_lines:
            return ""
        more = self.untold - len(self.incident_lines)
        lines = [f"- {line}" for line in self.incident_lines]
        if more > 0:
            lines.append(f"- And {more} more.")
        return (
            "[Platform note] Since you were last told, in this chat's sandbox:\n"
            + "\n".join(lines)
            + "\n\n"
        )


async def sandbox_notes(
    store: SandboxIncidentRepository, claim: Mapping[str, Any], capabilities: Sequence[str]
) -> SandboxNotes:
    """What this claim tells the agent about its sandbox; marks what it told.

    The browser note at a chat's first turn on a host whose image has the
    browser. The incidents the chat has not been told, at any turn: the mark
    moves the chat's cursor only once this attempt's ``started`` event is
    stored, so a claim that never starts tells them again.
    """
    thread_id = str(claim["thread_id"])
    attempt_id = int(claim["attempt_id"])
    browser = int(claim.get("turn_no") or 1) <= 1 and SANDBOX_BROWSER_CAPABILITY in capabilities
    try:
        untold = await store.untold_incidents(thread_id=thread_id, kinds=_TOLD_KINDS, limit=50)
        if untold is None:
            return SandboxNotes(browser=browser)
        await store.mark_attempt_incidents(
            attempt_id=attempt_id, thread_id=thread_id, through_id=int(untold["through_id"])
        )
    except Exception:
        logger.exception(
            "sandbox incidents could not be read for a claim",
            extra={"event": "sandbox_incidents_unread", "thread_id": thread_id},
        )
        return SandboxNotes(browser=browser)
    rows = untold["rows"]
    told = sorted(rows, key=lambda row: str(row["details"].get("observed_at")), reverse=True)
    return SandboxNotes(
        browser=browser,
        incident_lines=tuple(
            incident_line(row["event_type"], row["details"]) for row in told[:MAX_TOLD]
        ),
        untold=int(untold["total"]),
        through_id=int(untold["through_id"]),
    )


__all__ = [
    "BROWSER_NOTE",
    "SANDBOX_BROWSER_CAPABILITY",
    "SandboxNotes",
    "incident_line",
    "record_heartbeat_incidents",
    "sandbox_notes",
]
