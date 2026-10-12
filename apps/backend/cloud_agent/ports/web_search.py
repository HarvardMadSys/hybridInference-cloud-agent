"""Port: durable admission for the platform-owned Web Search tool."""

from __future__ import annotations

from typing import Literal, NamedTuple, Protocol

WebSearchAdmissionReason = Literal[
    "admitted",
    "stale_fence",
    "concurrency_limited",
    "global_rate_limited",
    "user_rate_limited",
]

# Port ownership tests require the protocol to be this module's only class;
# the functional form is still an immutable, statically typed tuple.
WebSearchAdmission = NamedTuple(  # noqa: UP014
    "WebSearchAdmission", [("call_id", str | None), ("reason", WebSearchAdmissionReason)]
)


class WebSearchRepository(Protocol):
    """Coordinate Web Search limits across every control-plane replica."""

    async def acquire_web_search(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> WebSearchAdmission:
        """Admit one call under its current attempt fence and shared limits.

        The implementation derives the user from the fenced job. Callers never
        choose the quota identity, and a superseded or expired attempt cannot
        consume provider capacity.
        """
        ...

    async def release_web_search(self, call_id: str) -> bool:
        """End an admitted call's concurrency lease while retaining its rate receipt."""
        ...
