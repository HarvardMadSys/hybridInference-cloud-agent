"""Negative control: objects that must NOT satisfy a port.

Deliberately wrong, and never imported by anything but the gate test.
``tests/unit/test_storage_ports_gate.py`` runs mypy over this file and fails if
mypy accepts it. That is what turns "the gate would have caught a mis-wired
port" into a checked claim: a gate nobody has ever seen fail is not evidence.

Three shapes, failing for three different reasons:

* ``NotAnAttemptStore`` has one of the port's methods and none of the others,
  so it fails on a missing member;
* ``WrongSignature`` declares **all ten** methods of ``AttemptCommands`` with
  one keyword spelled differently and is otherwise identical, so it fails on the
  signature alone. This is the case a ``runtime_checkable`` Protocol accepts,
  because that only asks whether an attribute exists;
* a decoded row offered where the capability that owns it was asked for, which
  is the boundary confusion this whole refactor exists to make impossible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Imported for types only, which is how every real caller takes a port.
    # The rejection below must not depend on the port existing at runtime.
    from cloud_agent.ports import AttemptCommands, HostPoolRepository


class NotAnAttemptStore:
    """One method of the port, and none of the rest."""

    async def claim_job(self, *, host: str) -> dict[str, Any] | None:
        """Present, and not enough."""
        raise NotImplementedError


class WrongSignature:
    """All ten methods, and ``hostname`` where the port says ``host``."""

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        hostname: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """The port declares ``host``, so this is a different operation."""
        raise NotImplementedError

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        """Correctly shaped, and irrelevant while claim_job is not."""
        raise NotImplementedError

    async def heartbeat(
        self, *, attempt_id: int, lease_generation: int, lease_ttl_seconds: float
    ) -> dict[str, Any]:
        """Correctly shaped."""
        raise NotImplementedError

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        """Correctly shaped."""
        raise NotImplementedError

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Correctly shaped."""
        raise NotImplementedError

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Correctly shaped."""
        raise NotImplementedError

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        """Correctly shaped."""
        raise NotImplementedError

    async def record_continuity_mode(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Correctly shaped."""
        raise NotImplementedError

    async def record_attempt_resources(
        self, *, attempt_id: int, lease_generation: int, rollup: dict[str, Any]
    ) -> bool:
        """Correctly shaped."""
        raise NotImplementedError

    async def sweep_queued_follow_ups(self) -> None:
        """Correctly shaped."""
        raise NotImplementedError


def wire_attempts(store: AttemptCommands) -> None:
    """Takes the attempt surface."""


def wire_host_pool(store: HostPoolRepository) -> None:
    """Takes the host-pool surface."""


def miswired_port() -> None:
    """A value that is missing members of the port it is passed as."""
    wire_attempts(NotAnAttemptStore())


def miswired_signature() -> None:
    """Every member present, one signature wrong."""
    wire_attempts(WrongSignature())


def mismapped_boundary() -> None:
    """A decoded row offered where the capability that owns it was required."""
    row: dict[str, Any] = {"host": "runner-a"}
    wire_host_pool(row)
