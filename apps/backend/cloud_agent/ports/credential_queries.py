"""Port: resolving the model credential an attempt runs with.

One operation, because the credential lookup is the only piece of the store
the inference relay needs.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class CredentialQueries(Protocol):
    """Port: resolving the model credential an attempt runs with."""

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        residency_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Resolve Agent-owned job metadata for the MCP relay's live fence.

        When supplied, the residency must still be hot, unexpired and serving
        this exact attempt. Revalidation must never move a request to a later turn.

        Return the job's owner, model and MCP binding scope only while its
        current attempt has a live lease and the job is running and not stopped.
        The relay uses that owner to resolve stored MCP connections and checks
        the admitted binding before forwarding a request.

        Only Agent-owned execution and connection state participates. Gateway account roles
        and status do not belong to this database. Inference authorization
        uses Gateway-issued grants separately from this MCP lookup.

        The bindings are read fresh on every call rather than carried on the
        job, so narrowing a session takes effect on its next tool call instead
        of at its next turn.

        Disabled bindings are filtered out here rather than refused later. A
        binding turned off with no capabilities selected would otherwise be
        handed to the runner as a server whose every call fails, and the model
        would spend its budget discovering that one tool at a time instead of
        being offered nothing at all.
        """
        ...

    async def resolve_residency_attempt(
        self, *, thread_id: str, residency_generation: int
    ) -> dict[str, Any] | None:
        """Name the attempt a resident process is serving right now.

        Return ``{"job_id", "attempt_id", "lease_generation"}`` only while
        ``residency_generation`` is the chat's current, unexpired residency and
        the chat's running turn was admitted to that same membership on the
        residency's host. ``None`` between turns, after a rotation, release or
        expiry, and for a deleted chat. The caller still applies
        :meth:`resolve_model_credential` to the answer, which owns Stop, the
        lease and host removal.
        """
        ...
