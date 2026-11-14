"""Port: the models a deployment provides, owners' allowances, and the model relay's reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date


class OperatorModelRepository(Protocol):
    """Persist provided connections and count what the model relay accepts.

    Rows are plain dictionaries with the fields an owner's connection row has,
    ``api_key_ciphertext`` included; encrypting and decrypting is the caller's
    job, and a row is never answered to anyone as-is.
    """

    async def list_operator_connections(self) -> list[dict[str, Any]]:
        """Every live provided connection, oldest first."""
        ...

    async def get_operator_connection(self, *, connection_id: str) -> dict[str, Any] | None:
        """One provided connection, deleted ones included; ``None`` otherwise."""
        ...

    async def create_operator_connection(self, *, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a verified, disabled connection; ``ModelConnectionConflict`` on a taken name."""
        ...

    async def update_operator_connection(
        self, *, connection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply an edit that needs no verification; ``None`` when nothing live matched.

        Raises ``OperatorLimitsMissing`` for enabling while a listed model has
        no default allowance, and ``ValueError`` for a field that needs
        :meth:`commit_verified_update`.
        """
        ...

    async def commit_verified_operator_update(
        self,
        *,
        connection_id: str,
        expected: Mapping[str, Any],
        changes: dict[str, Any],
        model_checks: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Apply a verified edit only while the row is still ``expected``; ``None`` otherwise."""
        ...

    async def delete_operator_connection(self, *, connection_id: str) -> dict[str, Any] | None:
        """Tombstone a connection and erase its key; ``None`` when there was none."""
        ...

    async def mark_operator_key_refused(self, *, connection_id: str, refused: bool) -> None:
        """Record that the provider refuses the stored key, or accepts it again."""
        ...

    async def model_relay_host_count(self) -> int:
        """How many enrolled hosts advertise that their sandboxes reach the model relay."""
        ...

    async def operator_limits(
        self, *, connection_ids: list[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Each connection's default allowances: ``{connection: {model: limit}}``."""
        ...

    async def set_operator_limit(
        self,
        *,
        connection_id: str,
        model: str,
        user_daily_requests: int | None,
        global_concurrency: int | None,
    ) -> bool:
        """Set a listed model's default allowance; ``False`` when it is not listed."""
        ...

    async def set_operator_user_limit(
        self, *, connection_id: str, user_id: str, model: str, daily_requests: int | None
    ) -> bool:
        """Give one account its own allowance on a listed model; ``False`` when not listed."""
        ...

    async def clear_operator_user_limit(
        self, *, connection_id: str, user_id: str, model: str
    ) -> bool:
        """Return one account to the model's default; ``False`` when it had none of its own."""
        ...

    async def list_operator_users(
        self, *, connection_id: str, cursor: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Accounts with an allowance of their own or with use today, by account id."""
        ...

    async def operator_usage(
        self, *, connection_id: str, start: date, end: date, limit: int
    ) -> list[dict[str, Any]]:
        """Accepted requests per day, account and model, newest day first."""
        ...

    async def operator_sources_for_user(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every enabled connection with ``user_id``'s allowance per listed model."""
        ...

    async def release_stale_operator_residencies(
        self, *, connection_id: str
    ) -> list[dict[str, Any]]:
        """End every open residency started on an older revision, or on a dead connection."""
        ...

    async def resolve_model_relay_caller(
        self, *, thread_id: str, residency_generation: int, model: str | None
    ) -> dict[str, Any] | None:
        """Name the running turn a model relay placeholder speaks for; ``None`` when it buys nothing.

        Fenced like every relay's caller: the residency is the chat's hot,
        unexpired one and is serving the job's current, running, leased
        attempt on its own enrolled host, and the job has not been asked to
        stop. Answers the job, its owner, its provided ``connection`` row
        (``None`` when the turn runs on none), the revision this residency was
        bound to, and for ``model`` the owner's ``allowance`` and the model's
        ``global_concurrency``.
        """
        ...

    async def count_operator_request(
        self, *, connection_id: str, user_id: str, model: str
    ) -> dict[str, Any]:
        """Count one request unless the allowance is used up: ``{"outcome", "day"}``."""
        ...

    async def refund_operator_request(
        self, *, connection_id: str, user_id: str, model: str, day: date
    ) -> None:
        """Take back one counted request the provider never served."""
        ...
