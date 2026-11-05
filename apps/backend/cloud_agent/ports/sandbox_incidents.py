"""Port: incidents a host reports for its sandboxes, and what each chat was told.

Declared here, beside the code that calls it, so the heartbeat and the claim
depend on the operations they need rather than on the whole sandbox
repository. Incidents are rows of the sandbox's append-only event table
(``agent_sandbox_events``, ``incident.<kind>``); see
``docs/agents/specs/2026-11-04-sandbox-incidents.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class SandboxIncidentRepository(Protocol):
    """Port: store incidents, notify running turns, and track what a chat was told."""

    async def record_sandbox_incidents(
        self,
        *,
        host: str,
        sandbox_id: str,
        generation: int,
        incidents: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Store a host's rows for a sandbox that host holds; the rows newly stored.

        Each row is ``{event_type, details}``. A row whose ``incident_id`` is
        already stored is not stored again and is not returned. A sandbox the
        host does not hold stores nothing.
        """
        ...

    async def notify_running_turns(
        self, *, sandbox_id: str, thread_id: str | None, payload: Mapping[str, Any]
    ) -> int:
        """Append a ``lifecycle`` notice to the running turns it concerns; how many.

        Every member's running turn in the sandbox, or only ``thread_id``'s.
        Each append is fenced by its attempt's live lease.
        """
        ...

    async def untold_incidents(
        self, *, thread_id: str, kinds: Sequence[str], limit: int
    ) -> dict[str, Any] | None:
        """The incidents ``thread_id`` has not been told, of ``kinds``.

        ``{"through_id", "rows", "total"}``: ``through_id`` is the newest
        incident of the chat's sandbox, of any kind, which the cursor may move
        to; ``rows`` the newest ``limit`` of those to tell, each
        ``{id, event_type, details}``. A chat never told anything is told
        nothing and starts at ``through_id``. ``None`` when its sandbox has no
        incident.
        """
        ...

    async def mark_attempt_incidents(
        self, *, attempt_id: int, thread_id: str, through_id: int
    ) -> None:
        """Record what an attempt's prompt told; its ``started`` event moves the cursor there."""
        ...
