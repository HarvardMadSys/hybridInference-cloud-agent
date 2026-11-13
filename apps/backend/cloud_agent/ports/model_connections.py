"""Port: an owner's own model connections, and the residencies started on them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


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
        """Insert a verified connection; raise ``ModelConnectionConflict`` on a taken name.

        ``values["model_checks"]`` maps every model to the verification the
        saving request made; nothing else creates a connection.
        Local account admission is checked under its row lock through commit;
        ``ModelConnectionAccessEnded`` refuses a revoked or suspended owner.
        """
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
        """Rename, remove models from, enable or disable a live connection; ``None`` otherwise.

        Only a value that actually differs counts; disabling raises the
        credential revision, and a removed model's verification goes with it.
        A Base URL, a key or an added model is refused (``ValueError`` for the
        field, ``None`` for a model list that is not a subset): those need
        :meth:`commit_verified_update`.
        """
        ...

    async def commit_verified_update(
        self,
        *,
        user_id: str,
        connection_id: str,
        expected: Mapping[str, Any],
        changes: dict[str, Any],
        model_checks: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Apply a verified edit only while the row is still ``expected``; ``None`` otherwise.

        Guarded by both revisions and the model list the verification read. A
        new key or Base URL raises both revisions; ``model_checks`` becomes the
        whole map, at the resulting config revision.
        ``ModelConnectionAccessEnded`` refuses a local owner whose admission
        ended during verification, checked under the account lock through commit.
        """
        ...

    async def delete_model_connection(
        self, *, user_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        """Tombstone a connection and erase its key; ``None`` when there was none."""
        ...

    async def reserve_model_connection_checks(
        self, *, user_id: str, count: int, limit: int, seconds: int
    ) -> bool:
        """Atomically count ``count`` provider calls against the owner's hourly limit, or none."""
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
