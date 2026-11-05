"""Port: what the GitHub relay reads and writes.

Two operations: resolving a residency's placeholder to the running turn it
speaks for, and recording a pull request GitHub created through the relay.
"""

from __future__ import annotations

from typing import Any, Protocol


class GitHubRelayRepository(Protocol):
    """Port: the GitHub relay's caller fence and pull request record."""

    async def resolve_github_caller(
        self, *, thread_id: str, residency_generation: int
    ) -> dict[str, Any] | None:
        """Name the running turn a residency's GitHub placeholder speaks for.

        Returns ``{"job_id", "attempt_id", "thread_id", "user_id", "repo"}``
        only while that residency generation is the chat's hot, unexpired one;
        its admitted attempt is the job's current, running, leased attempt on
        the membership's own host; the job is running and not asked to stop;
        and that host is still enrolled. Anything else is ``None``, so Stop,
        a superseded attempt, a rotated residency or a removed host leave the
        placeholder buying nothing.
        """
        ...

    async def record_pull_request(
        self,
        *,
        thread_id: str,
        job_id: str,
        user_id: str,
        repo: str,
        number: int,
        url: str,
        head_ref: str,
        base_ref: str,
        draft: bool,
    ) -> None:
        """Record a pull request GitHub created for this conversation.

        Idempotent per ``(thread_id, repo, number)``: a retried creation, or
        the same pull request reported twice, keeps the first record.
        """
        ...

    async def list_pull_requests(self, *, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The conversation's recorded pull requests, newest first."""
        ...
