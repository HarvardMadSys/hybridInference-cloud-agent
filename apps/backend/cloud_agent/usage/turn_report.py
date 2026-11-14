"""A job's usage: what its agent reported about its own model calls.

Turns run on the owner's own model connections, whose provider keeps the key,
the quota and the bill. What this service has is the agent's own token report
(see :mod:`cloud_agent.usage.runtime_usage` for which counters each agent reports),
counted once per model call. It records no cost and estimates none.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cloud_agent.domain.thread_stats import normalized_runtime_input_tokens, reported_token_totals
from cloud_agent_shared.model_connections import is_operator_connection_id


class RuntimeUsage(BaseModel):
    """The agent's own token report for one job, across its retries.

    A counter is ``None`` when no model call reported it: unknown, never zero.
    ``reasoning_tokens`` is part of ``output_tokens``.
    """

    source: Literal["runtime"] = "runtime"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    normalized_input_tokens: int | None = None


class JobUsageResponse(BaseModel):
    """What one owner's run reported about its model use."""

    runtime: RuntimeUsage
    #: The turn ran on a model connection: that connection's provider bills it.
    external_provider: bool = False
    #: Whose connection that was: the owner's own (``personal``) or one the
    #: deployment provides (``provided``), which the deployment pays for.
    source: Literal["personal", "provided"] | None = None
    version: str | None = Field(
        default=None,
        description=(
            "Orders two reads of one job's usage, asked for or pushed: the one whose "
            "version compares greater as a string saw everything the other did. Opaque."
        ),
    )


def summarize_job_usage(
    *,
    runtime_inputs: dict[str, Any],
    runtime: str | None,
    external_provider: bool = False,
    source: Literal["personal", "provided"] | None = None,
) -> JobUsageResponse:
    """Use one accounting definition for owner and admin reads of a turn.

    Callers authorize their own read and supply explicitly projected inputs.
    """
    events = runtime_inputs["events"]
    return JobUsageResponse(
        runtime=RuntimeUsage.model_validate(
            {
                **reported_token_totals(events),
                "normalized_input_tokens": normalized_runtime_input_tokens(
                    runtime=runtime, events=events
                ),
            }
        ),
        external_provider=external_provider,
        source=source,
    )


def usage_source(connection_id: object) -> Literal["personal", "provided"] | None:
    """Whose connection a turn named: ``None`` for a turn that named none."""
    if not connection_id:
        return None
    return "provided" if is_operator_connection_id(connection_id) else "personal"


__all__ = ["JobUsageResponse", "RuntimeUsage", "summarize_job_usage", "usage_source"]
