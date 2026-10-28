"""Port: an owner's own model connections, and the residencies started on them."""

from __future__ import annotations

from typing import Any, Protocol


class ModelConnectionRepository(Protocol):
    """Persist model connections per owner.

    Rows are plain dictionaries carrying ``api_key_ciphertext``; encrypting and
    decrypting is the caller's job, and a row is never answered to an owner
    as-is. Every read and write is scoped to ``user_id``: another owner's
    connection reads as missing.
    """

    async def create_model_connection(
        self, *, user_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert a connection; raise ``ModelConnectionConflict`` on a taken name."""
        ...

    async def list_model_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every connection the owner has not deleted, oldest first."""
        ...

    async def get_model_connection(
        self, *, user_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        """One of the owner's connections, deleted ones included; ``None`` otherwise."""
        ...

    async def update_model_connection(
        self, *, user_id: str, connection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply ``changes`` to a live connection; ``None`` when it is gone.

        Only a value that actually differs counts. A changed key, Base URL or
        protocol declaration raises its credential revision, and so does
        disabling it: a member process started under the old revision must not
        serve another turn. A changed key or Base URL also raises its config
        revision and clears every model's verification; a changed model list
        drops only the verifications of models no longer listed.
        """
        ...

    async def delete_model_connection(
        self, *, user_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        """Tombstone a connection and erase its key; ``None`` when there was none."""
        ...

    async def reserve_model_connection_check(
        self, *, user_id: str, connection_id: str, limit: int, seconds: int
    ) -> dict[str, Any]:
        """Atomically count one check against the owner's limit, before it dials out.

        ``{"connection": row}`` with the live connection as it will be checked,
        or ``{"error": "not_found" | "limited"}``.
        """
        ...

    async def record_model_verification(
        self,
        *,
        user_id: str,
        connection_id: str,
        config_revision: int,
        credential_revision: int,
        model: str,
        result: dict[str, Any],
    ) -> bool:
        """Merge one model's verification, only while the connection is still enabled and at both revisions."""
        ...

    async def scan_key_ciphertexts(self, *, sandbox_id: str, limit: int) -> list[str]:
        """Scan-only copies of the key revisions handed to one sandbox group, at most ``limit``."""
        ...

    async def bind_residency(
        self,
        *,
        user_id: str,
        connection_id: str,
        revision: int,
        thread_id: str,
        generation: int,
        scan_key_ciphertext: str,
    ) -> bool:
        """Record that a residency's process runs on this connection revision.

        And, atomically with it, keep ``scan_key_ciphertext`` -- the key's
        scan-only copy -- attributed to the membership's sandbox group.

        Only for a hot, unexpired generation, and only while the connection is
        still the owner's, enabled, and at ``revision``, read under a lock a
        change to the connection waits for: either the change commits first and
        this refuses, or this commits first and the change's
        :meth:`release_stale_residencies` finds it.
        """
        ...

    async def release_stale_residencies(
        self, *, user_id: str, connection_id: str
    ) -> list[dict[str, Any]]:
        """End every open residency started on an older revision, or on a dead connection.

        Returns ``thread_id``, ``generation``, ``host`` and ``grant_id`` per
        released residency, for the caller to close its process.
        """
        ...
