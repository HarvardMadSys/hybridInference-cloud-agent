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
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> dict[str, Any] | None:
        """Resolve Agent-owned job metadata for the MCP relay's live fence.

        Return the job's owner, model and MCP binding scope only while its
        current attempt has a live lease and the job is running or publishing.
        The relay uses that owner to resolve stored MCP connections and checks
        the admitted binding before forwarding a request.

        Only Agent job and attempt tables participate. Gateway account roles
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
