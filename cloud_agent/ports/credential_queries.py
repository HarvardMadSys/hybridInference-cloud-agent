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

        Return the job's owner, model and MCP server scope only while its
        current attempt has a live lease and the job is running or publishing.
        The relay uses that owner to resolve stored MCP connections and checks
        the admitted server scope before forwarding a request.

        Only Agent job and attempt tables participate. Gateway account roles
        and status do not belong to this database. Inference authorization
        uses Gateway-issued grants separately from this MCP lookup.
        """
        ...
