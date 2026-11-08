"""Sandbox incidents: what went wrong in a chat's sandbox, as a host reports it.

A host observes a few kinds of trouble in each retained sandbox (Docker's
``oom`` and unexpected ``die`` events, resource peaks near a limit, and the
journals the browser service and the MCP runners keep) and reports each as
one row with its heartbeat. The control plane stores the row in the sandbox's
append-only event table and confirms it; the host forgets it only then.

Every row is final when the host makes it. Repeats are counted on the host and
sent as one ``<kind>.repeated`` row when their window closes, and rows past a
sandbox's hourly cap as one ``dropped`` row, so nothing is ever counted by
changing a stored row. A row's id is deterministic, so a row sent twice is the
same row.

A row is content-free: no stderr, no URL, no command line, no environment. A
program's stderr may hold its keys. See
``docs/agents/specs/2026-11-04-sandbox-incidents.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError

#: What a host observes. ``memory_high`` and ``pids_high`` are its own peaks.
INCIDENT_KINDS = frozenset(
    {
        "out_of_memory",
        "container_exited",
        "memory_high",
        "pids_high",
        "browser_crashed",
        "mcp_server_failed",
    }
)
REPEATED_SUFFIX = ".repeated"
DROPPED_KIND = "dropped"
#: Kinds that concern one chat rather than the whole sandbox.
MEMBER_KINDS = frozenset({"mcp_server_failed"})
#: Kinds the chat shows a line for, in the turn they broke.
KINDS_SHOWN_TO_USER = frozenset({"out_of_memory", "container_exited", "mcp_server_failed"})
#: Kinds the agent is told about at its next turn.
KINDS_TOLD_TO_AGENT = frozenset(
    {"out_of_memory", "container_exited", "browser_crashed", "mcp_server_failed"}
)
MCP_PHASES = frozenset({"start", "crashed"})
MAX_INCIDENTS_PER_BEAT = 256
#: A heartbeat answer's word that this control plane stored the incidents.
INCIDENTS_ACCEPTED: Literal["accepted"] = "accepted"

_DETAIL_KEYS = frozenset(
    {
        "exit_code",
        "signal",
        "oom_killed",
        "limit",
        "peak",
        "alias",
        "phase",
        "window_end",
    }
)


def base_kind(kind: str) -> str:
    """The observed kind a row is about, without its ``.repeated`` suffix."""
    return kind.removesuffix(REPEATED_SUFFIX)


def known_kind(kind: str) -> bool:
    """Whether ``kind`` is one a host may report: observed, repeated, or dropped."""
    return kind == DROPPED_KIND or base_kind(kind) in INCIDENT_KINDS


class SandboxIncident(BaseModel):
    """One final incident row, as a host sends it."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    incident_id: str = Field(min_length=1, max_length=200, pattern=r"^[!-~]+$")
    kind: str = Field(min_length=1, max_length=64)
    scope: Literal["sandbox", "member"]
    #: The chat a member-scoped incident concerns, resolved when it was seen.
    thread_id: str | None = Field(None, min_length=1, max_length=128)
    observed_at: StrictFloat = Field(ge=0)
    count: StrictInt = Field(1, ge=1, le=1_000_000)
    details: dict[str, StrictInt | StrictFloat | str | bool | None] = Field(
        default_factory=dict, max_length=len(_DETAIL_KEYS)
    )

    def is_valid(self) -> bool:
        """Whether this row is one the control plane stores."""
        if not known_kind(self.kind) or set(self.details) - _DETAIL_KEYS:
            return False
        if self.scope == "member" and self.thread_id is None:
            return False
        if any(isinstance(value, str) and len(value) > 64 for value in self.details.values()):
            return False
        phase = self.details.get("phase")
        return phase is None or phase in MCP_PHASES

    def wire(self) -> dict[str, Any]:
        """The row as JSON."""
        return self.model_dump(mode="json")


def parse_incidents(raw: object) -> list[SandboxIncident]:
    """The valid rows of a beat's ``incidents``; anything else is dropped, never refused."""
    if not isinstance(raw, list):
        return []
    parsed: list[SandboxIncident] = []
    for item in raw[:MAX_INCIDENTS_PER_BEAT]:
        try:
            incident = SandboxIncident.model_validate(item)
        except ValidationError:
            continue
        if incident.is_valid():
            parsed.append(incident)
    return parsed


__all__ = [
    "DROPPED_KIND",
    "INCIDENTS_ACCEPTED",
    "INCIDENT_KINDS",
    "KINDS_SHOWN_TO_USER",
    "KINDS_TOLD_TO_AGENT",
    "MCP_PHASES",
    "MEMBER_KINDS",
    "REPEATED_SUFFIX",
    "SandboxIncident",
    "base_kind",
    "known_kind",
    "parse_incidents",
]
