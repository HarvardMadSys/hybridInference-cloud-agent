"""Owner-scoped command-execution reads behind the Context view."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.command_executions import CommandFilter, CommandSort


class CommandExecutionQueries(Protocol):
    """Read one owner's conversation's command executions, filtered and paged."""

    async def command_executions(
        self,
        *,
        job_id: str,
        user_id: str,
        filter: CommandFilter = "all",
        sort: CommandSort = "latest",
        q: str = "",
        program: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        """One page of the caller's conversation; None when it is not theirs."""
        ...
