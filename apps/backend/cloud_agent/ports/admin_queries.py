"""Ports: the admin console's bounded, read-only projections across every owner.

Nothing here is fenced by ownership: the routes that reach these require an
Agent administrator, except the usage aggregate, which owner routes also read
for one owner at a time. Each protocol is implemented by the class of the same
name with a ``Postgres`` prefix under ``storage/repositories/admin/``.

The usage refusals live here, beside the protocol that raises them, so that a
caller can catch one without importing the implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from cloud_agent.schemas.admin_conversations import (
        AdminConversationDetail,
        AdminConversationList,
        ConversationCursor,
        ConversationTurnsCursor,
    )
    from cloud_agent.schemas.admin_database import (
        DatabaseQuery,
        DatabaseQueryResult,
        DatabaseTables,
    )
    from cloud_agent.schemas.admin_observability import AdminRunList, RunCursor, RunFilters
    from cloud_agent.schemas.admin_sandbox_inventory import (
        AdminSandboxInventoryPage,
        SandboxInventoryCursor,
    )
    from cloud_agent.schemas.admin_sandboxes import (
        AdminSandboxDetail,
        AdminSandboxList,
        AdminSandboxTrajectoryPage,
        SandboxCursor,
        SandboxStateFilter,
        SandboxTrajectoryCursor,
    )


class UsageQueryTooLarge(Exception):
    """The selection exceeds a published bound and is refused as a whole."""


class UsageQueryUnavailable(Exception):
    """The selection could not be read within its bounds."""


class AdminConversationQueries(Protocol):
    """Conversations whose turns match a run cohort, and one conversation's turns."""

    async def list_conversations(
        self,
        *,
        filters: RunFilters,
        since: datetime,
        until: datetime,
        cursor: ConversationCursor | None,
        limit: int,
    ) -> AdminConversationList:
        """Conversations ordered by their newest matching turn, then identity."""
        ...

    async def detail(
        self,
        thread_id: str,
        *,
        filters: RunFilters,
        since: datetime | None,
        until: datetime | None,
        cursor: ConversationTurnsCursor | None,
        limit: int,
    ) -> AdminConversationDetail | None:
        """Keep all turns in context, marking matches instead of hiding siblings."""
        ...


class AdminDatabaseQueries(Protocol):
    """Read-only, parameterized access to explicitly reviewed Agent columns."""

    async def list_tables(self) -> DatabaseTables:
        """The reviewed tables and the columns of each that may be read."""
        ...

    async def query(self, request: DatabaseQuery) -> DatabaseQueryResult:
        """One bounded, filtered page of a reviewed table.

        Raises:
            DatabaseQueryInvalid: If the request names an unreviewed column or value.
            DatabaseQueryTimeout: If the statement exceeds its timeout.
            DatabaseQueryUnavailable: If the database cannot answer.
        """
        ...


class AdminRunQueries(Protocol):
    """Content-free run metadata, attempts, usage inputs and resource rows."""

    async def list_runs(
        self,
        *,
        filters: RunFilters,
        since: datetime,
        until: datetime,
        cursor: RunCursor | None,
        limit: int,
    ) -> AdminRunList:
        """A creation-time window of runs and a keyset page within it."""
        ...

    async def get_run(self, job_id: str) -> dict[str, Any] | None:
        """Fetch the same explicit metadata fields as the list, never SELECT *."""
        ...

    async def attempts(self, job_id: str) -> list[dict[str, Any]]:
        """Read execution identities without lease credentials or failure text."""
        ...

    async def usage_inputs(self, job_id: str) -> dict[str, Any]:
        """Project numeric usage and hashed identities for accounting and timing."""
        ...

    async def resources(self, job_id: str, attempt_id: int) -> dict[str, Any] | None:
        """Read one attempt's resource rows with their original source and gaps."""
        ...


class AdminSandboxInventory(Protocol):
    """Consistent pages of every sandbox group, then only the groups that changed."""

    async def read(
        self,
        *,
        cursor: SandboxInventoryCursor | None,
        state: str,
        include_archived: bool,
        limit: int,
    ) -> AdminSandboxInventoryPage:
        """Return one projected snapshot page or one bounded change page."""
        ...


class AdminSandboxQueries(Protocol):
    """Sandbox groups, one group's detail, and its trajectory."""

    @property
    def inventory(self) -> AdminSandboxInventory:
        """The snapshot-and-changes reader behind the console's sandbox list."""
        ...

    async def list_running(self, *, cursor: SandboxCursor | None, limit: int) -> AdminSandboxList:
        """A page of the groups that are running now."""
        ...

    async def list_sandboxes(
        self,
        *,
        cursor: SandboxCursor | None,
        limit: int,
        state: SandboxStateFilter,
        include_archived: bool = False,
    ) -> AdminSandboxList:
        """A page of the groups in ``state``, archived ones only when asked for."""
        ...

    async def detail(self, sandbox_id: str) -> AdminSandboxDetail | None:
        """One group with its members, checkpoints and deletion attempts."""
        ...

    async def trajectory(
        self,
        sandbox_id: str,
        *,
        cursor: SandboxTrajectoryCursor | None,
        limit: int,
    ) -> AdminSandboxTrajectoryPage | None:
        """A page of one group's lifecycle events, or None when it does not exist."""
        ...


class AdminUsageQueries(Protocol):
    """Bounded usage aggregation over one repeatable-read view of the database."""

    async def selection(
        self,
        *,
        since: datetime,
        until: datetime,
        user_id: str | None,
        model: str | None,
        runtime: str | None,
        repo: str | None,
        max_runs: int,
        max_attempts: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read one selection's jobs and their attempts' usage from one view.

        Raises:
            UsageQueryTooLarge: If the selection exceeds any budget.
            UsageQueryUnavailable: If a statement fails or exceeds its timeout.
        """
        ...

    async def first_run_at(self, user_id: str) -> datetime | None:
        """When the owner's first run the cohort can select was created."""
        ...

    def batched_selection(
        self,
        *,
        since: datetime,
        until: datetime,
        user_id: str,
        thread_ids: list[str] | None,
        max_runs: int,
        max_attempts: int,
        deadline: float,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        """Yield one owner's selection as consecutive bounded batches.

        Raises:
            UsageQueryTooLarge: If even the smallest batch exceeds a budget.
            UsageQueryUnavailable: If a statement fails, times out, or the
                whole read passes ``deadline`` (a ``time.monotonic`` instant).
        """
        ...
