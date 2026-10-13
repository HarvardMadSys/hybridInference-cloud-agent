"""Port: which host holds a sandbox group resident.

Establishment, heartbeat, release, and the reaper that expires what stopped
heartbeating.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class ResidencyRepository(Protocol):
    """Port: which host holds a sandbox group resident."""

    async def establish_residency(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        host: str,
        runtime_id: str,
        runtime_version: str | None,
        ttl_seconds: float,
        sandbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Take (or rotate) a chat's residency; ``None`` = not earned.

        Residency is something the exact worker attempt earns by *currently*
        running the thread's turn on this host. Attempt id, lease generation,
        current-job ownership, membership ownership, status, and lease expiry
        are checked in the same transaction as rotation, so a delayed request
        from a superseded attempt cannot revoke its successor's grant. The
        membership generation only ever increments, and every rotation clears
        the grant so the caller revokes the predecessor's
        (``previous_grant_id``) and mints anew.

        ``runtime_id`` and ``runtime_version`` are accepted for the worker
        contract and not stored: the sandbox the membership belongs to is the
        authority on what runs in it.
        """
        ...

    async def heartbeat_residency(
        self, *, thread_id: str, generation: int, ttl_seconds: float, host: str | None = None
    ) -> str | None:
        """Extend a residency's lease — only the generation that holds it.

        Returns ``None`` when nothing was extended (lost/taken over), else the
        residency's grant id — ``""`` when it runs without one — so the caller
        can renew the credential in the same pass. ``host`` narrows the write
        to the machine the row names: a beat is a holder's statement, and a
        guessed (thread, generation) from anywhere else must extend nothing.
        Expiry is equally final: a hot row waiting for the reaper cannot use a
        late beat to regain authority.
        """
        ...

    async def release_residency(
        self, *, thread_id: str, generation: int, host: str | None = None
    ) -> str | None:
        """End a residency (teardown/eviction) — only its own generation.

        Returns ``None`` when nothing was released, else the grant id to
        revoke (``""`` when the residency ran without one).
        """
        ...

    async def release_shared_residency_if_quiescent(
        self,
        *,
        thread_id: str,
        generation: int,
        process_generation: int,
        host: str,
        sandbox_id: str,
    ) -> str | None:
        """Release a stopped shared member unless a durable transition owns it.

        A host observation is sampled before its heartbeat transaction. Native
        Fork can rotate and mint a new residency after that sample but before
        the heartbeat reaches this method. The group's durable operation fence,
        member attempt, and observed process generation therefore participate
        in the same UPDATE that releases the grant. The membership must also
        have accepted the stopped/lost observation; checking any of these in
        Python would recreate the revoke-after-mint race this method exists to
        prevent.
        """
        ...

    async def resident_host(self, *, thread_id: str) -> dict[str, Any] | None:
        """The live hot residency for a thread, or ``None``.

        Live means unexpired: the reaper marks expiry on its own schedule,
        and scheduling decisions between passes must not trust a lease the
        clock has already taken away.
        """
        ...

    async def reap_expired_residencies(self) -> list[dict[str, Any]]:
        """Mark every hot residency whose lease lapsed as ``none``.

        The control plane's unilateral half of the fence: the host may still
        be running the sandbox, but its generation no longer opens anything —
        not heartbeats, not credentials — and the next turn goes COLD.
        """
        ...
