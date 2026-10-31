"""A job's usage: what its agent reported about its own model calls.

Turns run on the owner's own model connections, whose provider keeps the key,
the quota and the bill. What this service has is the agent's own token report
(see :mod:`cloud_agent.runtime_usage` for which counters each agent reports),
counted once per model call. It records no cost and estimates none.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cloud_agent.thread_stats import normalized_runtime_input_tokens, reported_token_totals


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
    #: The turn ran on its owner's own model connection: that provider bills it.
    external_provider: bool = False
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
    )


__all__ = ["JobUsageResponse", "RuntimeUsage", "summarize_job_usage"]
